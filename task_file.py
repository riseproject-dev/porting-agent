# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Resolve the task of a claimed task from the task file its payload names.

The work queue carries a fixed-size reference to the task file of a task
(``task_file``: the control repository, the pinned commit, the path, the
SHA-256 and the evidence id), and the controller commits the task itself as
the ``task`` evidence file the reference names (``work_task``). Every reader
of a claimed task (the trusted pre-agent steps of the worker and the planner,
the trusted jobs after them, the queue result resolver and
the controller) resolves the claim's task with ``resolve``, and then sees the
payload shape the task always had: the task, with ``resource_scope`` and
``effect_contract`` from the queue and ``ref`` (and the plan ``url``) filled in
the references the task file names without a commit.

A payload that carries the task inline (it has ``instructions`` and no
``task_file``; a Work queued before task files existed) is accepted unchanged.

Standard library only: the pre-agent steps run it with the runner's
``python3`` and ``gh``.

    python3 task_file.py resolve --claim CLAIM.json --out TASK.json

prints the evidence id of the task file and exits 0; exits 1 when the
reference or the file is refused, and 3 when the file cannot be read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

CONTROL_REPO = "riseproject-dev/porting-agent"
KIND = "task"
# The bytes of the file the queue's reference names, at most (the bound of a
# state record).
MAX_FILE_BYTES = 262144
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^task-[0-9a-f]{32}$")
_EVIDENCE_PATH = re.compile(r"^\.state/evidence/[A-Za-z0-9._-]+\.json$")
# What the queue carries beside the reference, and what a task file never
# holds (the queue's fields are the queue's).
QUEUE_FIELDS = ("resource_scope", "effect_contract", "task_file")
# The fields of the queue payload that name the task the file must be of.
IDENTITY = ("task", "contribution_id", "revision", "generation", "dedupe_key")


class Refused(ValueError):
    """The reference or the file it names is not the task of this claim."""


class Unavailable(OSError):
    """The file could not be read from the control repository."""


def evidence_id(dedupe_key: str) -> str:
    """The id of the task file of the task ``dedupe_key``."""
    return "task-" + hashlib.sha256(dedupe_key.encode()).hexdigest()[:32]


def path_of(eid: str) -> str:
    return f".state/evidence/{eid}.json"


def reference(dedupe_key: str, ref: str, sha256: str) -> dict:
    """The reference a queue payload carries to the task file of
    ``dedupe_key`` at the commit ``ref``."""
    eid = evidence_id(dedupe_key)
    return {"repository": CONTROL_REPO, "ref": ref, "path": path_of(eid), "sha256": sha256,
            "evidence_id": eid}


def gh_fetch(repository: str, ref: str, path: str) -> bytes:
    """The bytes of ``path`` of ``repository`` at the commit ``ref``, read
    with the job's token."""
    try:
        done = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github.raw+json",
             f"/repos/{repository}/contents/{path}?ref={ref}"],
            capture_output=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unavailable(f"the task file could not be read: {error}") from error
    return done.stdout


def _check_reference(ref: object) -> dict:
    if not isinstance(ref, dict) or set(ref) != {"repository", "ref", "path", "sha256",
                                                 "evidence_id"}:
        raise Refused("the task file reference is malformed")
    if ref["repository"] != CONTROL_REPO:
        raise Refused("bad task file repository")
    if not isinstance(ref["ref"], str) or not _COMMIT.fullmatch(ref["ref"]):
        raise Refused("bad task file ref")
    if not isinstance(ref["evidence_id"], str) or not _TASK_ID.fullmatch(ref["evidence_id"]):
        raise Refused("bad task file evidence id")
    if ref["path"] != path_of(ref["evidence_id"]):
        raise Refused("task file path and id differ")
    if not isinstance(ref["sha256"], str) or not _DIGEST.fullmatch(ref["sha256"]):
        raise Refused("bad task file digest")
    return ref


def _fill(item: object, ref: str, *, url: bool) -> None:
    """Name the commit in a reference of the task file (and the URL of a
    plan), after checking its shape."""
    if item is None:
        return
    if not isinstance(item, dict) or not isinstance(item.get("path"), str) \
            or not _EVIDENCE_PATH.fullmatch(item["path"]) \
            or not isinstance(item.get("sha256"), str) or not _DIGEST.fullmatch(item["sha256"]) \
            or {"repository", "ref", "url"} & set(item):
        raise Refused("a reference of the task file is malformed")
    item["repository"], item["ref"] = CONTROL_REPO, ref
    if url:
        item["url"] = f"https://raw.githubusercontent.com/{CONTROL_REPO}/{ref}/{item['path']}"


def resolve(work: object, fetch: Callable[[str, str, str], bytes] | None = None,
            validate: Callable[[dict], None] | None = None) -> dict:
    """The task payload of the claimed ``work`` (the ``work`` of a Claim of
    the assignment): ``work`` itself when it carries the task inline, else
    the task file its ``task_file`` names, read with ``fetch(repository,
    ref, path)`` and checked against every field of the claim. Raises
    ``Refused`` when the reference or the file is not the task of the
    claim, and lets ``Unavailable`` of the fetch through (``gh_fetch`` unless
    ``fetch`` is given). ``validate`` (the trusted jobs that have the schemas
    pass ``schemas.validate("task", ...)``) checks the resolved task, and a
    ``ValueError`` it raises is a refusal; the pre-agent steps, which have
    only the standard library, check the fields they use."""
    fetch = fetch or gh_fetch
    if not isinstance(work, dict):
        raise Refused("the claimed task is not an object")
    if "task_file" not in work:
        if "instructions" in work:
            return work
        raise Refused("the claimed task carries neither a task file reference nor its task")
    ref = _check_reference(work["task_file"])
    key = work.get("dedupe_key")
    if not isinstance(key, str) or ref["evidence_id"] != evidence_id(key):
        raise Refused("the task file id is not derived from the task id")
    raw = fetch(ref["repository"], ref["ref"], ref["path"])
    if len(raw) > MAX_FILE_BYTES:
        raise Refused("the task file is over its bound")
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise Refused("task file digest mismatch")
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise Refused("the task file is not JSON") from error
    if not isinstance(document, dict) or document.get("kind") != KIND \
            or document.get("evidence_id") != ref["evidence_id"]:
        raise Refused("task file identity mismatch")
    data = document.get("data")
    if not isinstance(data, dict) or set(data) & set(QUEUE_FIELDS):
        raise Refused("the task file holds no task, or holds the fields of the queue")
    for name in IDENTITY:
        if data.get(name) != work.get(name) or work.get(name) is None:
            raise Refused(f"the task file and the claim differ in {name}")
    if data.get("review") != work.get("review"):
        raise Refused("the task file and the claim differ in review")
    resolved = dict(data)
    context = resolved["context"] = dict(resolved.get("context") or {})
    for name in ("history", "report"):
        if context.get(name) is not None:
            context[name] = dict(context[name])
            _fill(context[name], ref["ref"], url=False)
    items = []
    for item in context.get("reason_evidence") or []:
        item = dict(item)
        if item.get("reference") is not None:
            item["reference"] = dict(item["reference"])
            _fill(item["reference"], ref["ref"], url=True)
        items.append(item)
    if "reason_evidence" in context:
        context["reason_evidence"] = items
    for name in ("resource_scope", "effect_contract"):
        if not isinstance(work.get(name), dict):
            raise Refused(f"the claim carries no {name}")
        resolved[name] = work[name]
    resolved["task_file"] = ref
    if validate is not None:
        try:
            validate(resolved)
        except ValueError as error:
            raise Refused(f"the resolved task is invalid: {str(error)[:300]}") from error
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task_file.py")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("resolve")
    one.add_argument("--claim", required=True, type=Path,
                     help="the `work` of the Claim of the assignment")
    one.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        work = json.loads(args.claim.read_text())
        resolved = resolve(work)
    except Refused as error:
        print(f"task file refused: {error}", file=sys.stderr)
        return 1
    except Unavailable as error:
        print(str(error), file=sys.stderr)
        return 3
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(resolved, indent=2))
    ref = resolved.get("task_file")
    print(f"resolved task {ref['evidence_id']}" if ref else "task carried inline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
