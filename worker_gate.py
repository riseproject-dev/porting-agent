# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Trusted structural gate for uncredentialed worker proposal artifacts."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
from pathlib import Path

import commit_rules
import schemas
import worker_report
from common import validate_scope_paths
from work import WRITE_TASKS

MAX_MBOX_BYTES = 4 * 1024 * 1024
FORBIDDEN_PATCH_TEXT = (
    "pull_request_target",
    "${{ secrets.",
    "permissions: write-all",
)


class WorkerGateError(ValueError):
    pass


def _regular(path: Path, *, required: bool) -> bytes | None:
    if not path.exists() and not required:
        return None
    if path.is_symlink() or not path.is_file():
        raise WorkerGateError(f"{path.name}: expected regular file")
    data = path.read_bytes()
    if len(data) > MAX_MBOX_BYTES:
        raise WorkerGateError(f"{path.name}: exceeds artifact limit")
    return data


def _patch_paths(text: str) -> list[str]:
    paths = []
    for line in text.splitlines():
        if not line.startswith("diff --git "):
            continue
        match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
        if not match or match.group(1) != match.group(2):
            raise WorkerGateError("mbox contains a quoted path or rename")
        path = match.group(1)
        if path not in paths:
            paths.append(path)
    return paths


_GH_ACT = re.compile(r"(?:^|[\s;&|(])gh\s+act\b")


def _is_gh_act(command: str) -> bool:
    """Whether a reproduction command runs the optional ``gh act`` extension."""
    return bool(_GH_ACT.search(command))


def _direct(command: str, image: str) -> bool:
    """Whether a reproduction command runs directly in the exact riscv64
    image: ``docker run --platform linux/riscv64`` of that exact reference,
    not through ``gh act``."""
    return not _is_gh_act(command) and "docker run" in command \
        and "--platform linux/riscv64" in command and image in command


def validate(task: dict, proposal: dict, mbox_path: Path, report_path: Path | None = None) -> dict:
    schemas.validate("proposal", proposal)
    if task.get("task") not in WRITE_TASKS:
        raise WorkerGateError("worker gate accepts only patch tasks")
    for proposal_key, task_key in (
        ("contribution_id", "contribution_id"),
        ("expected_revision", "revision"),
        ("expected_head", "expected_head"),
    ):
        if proposal.get(proposal_key) != task.get(task_key):
            raise WorkerGateError(f"proposal {proposal_key} does not match task")
    if proposal.get("ref") != task.get("ref"):
        raise WorkerGateError("proposal ref does not match task")
    operation = proposal.get("operation")
    if proposal.get("outcome") == "no_change" and operation != "none":
        raise WorkerGateError("no-change proposal must use operation none")
    if operation not in (task.get("allowed_operations") or []):
        if not (operation == "none" and proposal.get("outcome") != "patch"):
            raise WorkerGateError("proposal operation is not allowed")

    allowed = validate_scope_paths(list(task.get("allowed_paths") or []))
    changed = list(proposal.get("changed_files") or [])
    if len(changed) != len(set(changed)) or len({path.casefold() for path in changed}) != len(changed):
        raise WorkerGateError("proposal changed_files contains duplicates or case collisions")
    validate_scope_paths(changed) if changed else None
    outside = [path for path in changed if not any(fnmatch.fnmatchcase(path, item) for item in allowed)]
    if outside:
        raise WorkerGateError(f"proposal paths outside approved scope: {outside[:3]}")

    required_mbox = operation in {"append", "rewrite"}
    mbox = _regular(mbox_path, required=required_mbox)
    actual_paths = []
    if required_mbox:
        if not mbox or not mbox.startswith(b"From "):
            raise WorkerGateError("patch operation requires a format-patch mbox")
        digest = hashlib.sha256(mbox).hexdigest()
        if proposal.get("patch_digest") != digest:
            raise WorkerGateError("patch digest mismatch")
        text = mbox.decode("utf-8", "strict")
        actual_paths = _patch_paths(text)
        if sorted(actual_paths) != sorted(changed):
            raise WorkerGateError("mbox paths do not match proposal changed_files")
        if task.get("upstream_facing") and "{debug}" in text:
            raise WorkerGateError("debug commit is forbidden on an upstream-facing branch")
        additions = "\n".join(
            line for line in text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        if any(value in additions for value in FORBIDDEN_PATCH_TEXT):
            raise WorkerGateError("patch adds forbidden workflow content")
        # Attribution trailers: the worker commits under the App's identity,
        # and the gate has no approved commits to compare with, so every
        # ``*-by:`` trailer of the series must name the App.
        attributed = commit_rules.attribution_problems(commit_rules.mbox_messages(mbox), [])
        if attributed:
            raise WorkerGateError("the series attributes work to someone else: "
                                  + "; ".join(attributed[:4]))
        rules = (task.get("context") or {}).get("commit_rules")
        if rules is not None:
            # The measurable commit conventions of the verified plan, from
            # the trusted Work payload. ``{debug}`` commits are exempt; the
            # number of commits is checked at submission, where the review
            # may squash.
            try:
                rules = commit_rules.normalize(rules)
            except commit_rules.RuleError as exc:
                raise WorkerGateError(f"the Work carries invalid commit rules: {exc}") from None
            broken = commit_rules.series_problems(commit_rules.mbox_messages(mbox), rules,
                                                  count_commits=False)
            if broken:
                raise WorkerGateError("the series breaks the project's commit conventions: "
                                      + "; ".join(broken[:8]))
        required = (task.get("context") or {}).get("series_requirements")
        if required:
            # The unmet series requirements of the plan this Work answers,
            # from the trusted Work payload: a non-debug commit must change a
            # matching path, or the worker reports blocked.
            if not isinstance(required, list) or not all(
                    isinstance(r, dict) and isinstance(r.get("id"), str)
                    and isinstance(r.get("paths"), list)
                    and all(isinstance(p, str) for p in r["paths"]) for r in required):
                raise WorkerGateError("the Work carries invalid series requirements")
            _met, unmet = commit_rules.changed_paths_by_requirement(
                required, commit_rules.mbox_non_debug_paths(mbox))
            if unmet:
                raise WorkerGateError("the series still does not meet the project's series "
                                      "requirements: " + ", ".join(r["id"] for r in unmet))
    elif mbox is not None or proposal.get("patch_digest") is not None or changed:
        raise WorkerGateError("no-change proposal must not carry patch bytes or changed files")

    reproduction = proposal.get("reproduction")
    if not isinstance(reproduction, dict):
        raise WorkerGateError("worker proposal lacks reproduction evidence")
    image = reproduction.get("image", "")
    if not re.fullmatch(
        r"ghcr\.io/riseproject-dev/porting-agent/ubuntu-24\.04-riscv@sha256:[0-9a-f]{64}",
        image,
    ):
        raise WorkerGateError("reproduction image is not exact-digest pinned")
    if reproduction.get("architecture") != "riscv64":
        raise WorkerGateError("reproduction architecture is not riscv64")
    commands = reproduction.get("commands") or []
    if not commands or not all(isinstance(item.get("exit_status"), int) for item in commands):
        raise WorkerGateError("reproduction command evidence is incomplete")
    # ``gh act`` is an optional aid: its workflow, job and runner-mapping
    # evidence is recorded only when the worker used it, and then in full.
    workflows = reproduction.get("workflows") or []
    if any(not item.get("jobs") or not item.get("runner_mappings") for item in workflows):
        raise WorkerGateError(
            "gh act evidence is incomplete: a recorded workflow needs its jobs and runner mappings")
    direct = [item for item in commands if _direct(item["command"], image)]
    if proposal.get("outcome") in ("patch", "no_change") \
            and not any(item["exit_status"] == 0 for item in direct):
        # The reproduction is the build and test run directly in the exact
        # image; the exit status of a ``gh act`` command is never read here,
        # so a failing ``gh act`` beside passing direct commands is accepted.
        raise WorkerGateError("reproduction lacks a passing direct command in the exact riscv64 image")
    gate_status = "accepted"
    failure_class = None
    # Plan 8.6: a valid exact-task no_change Result is preserved. It is
    # accepted here so trusted preflight records ``no_change`` without minting
    # a destination token; it is never a validation failure that would
    # allocate repair Work at a new revision.
    if proposal.get("outcome") not in ("patch", "no_change"):
        cluster = str(proposal.get("failure_cluster") or "")
        if cluster.startswith("reproduction-image-infrastructure") and not any(
                item["exit_status"] != 0 and not _is_gh_act(item["command"]) for item in commands):
            raise WorkerGateError("a gh act failure alone is not a reproduction infrastructure "
                                  "blocker: build and test directly in the image")
        gate_status = "infrastructure_blocked" if cluster.startswith(
            "reproduction-image-infrastructure"
        ) else "blocked"
        failure_class = "infrastructure" if gate_status == "infrastructure_blocked" else "code"
    else:
        # The report of the run, checked once the Result is otherwise valid
        # and classified: required when the Work carries a history, and
        # stored for later Work only if this Result is accepted and
        # published. A blocked Result is classified as blocked whatever its
        # report, which is discarded with it.
        try:
            worker_report.check_path(task, report_path)
        except worker_report.ReportError as error:
            raise WorkerGateError(f"the worker report is missing or invalid: {error}") from None
    return {
        "status": gate_status,
        "failure_class": failure_class,
        "reason": proposal.get("diagnosis") if gate_status != "accepted" else None,
        "changed_files": actual_paths,
        "patch_digest": proposal.get("patch_digest"),
        "image": image,
        "commands": len(commands),
        "workflows": len(workflows),
        "direct_commands": len(direct),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker_gate")
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--mbox", type=Path, required=True)
    parser.add_argument("--report", type=Path,
                        help="the worker's report.json (required when the Work carries a history)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        task = json.loads(args.task.read_text())
        proposal = json.loads(args.proposal.read_text())
        report = validate(task, proposal, args.mbox, args.report)
        code = 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, schemas.SchemaError,
            WorkerGateError, ValueError) as error:
        report = {"status": "rejected", "reason": str(error)[:2000]}
        code = 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
