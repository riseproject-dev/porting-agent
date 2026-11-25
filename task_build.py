# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The task file of one task: everything its agent and its trusted jobs need.

The controller commits the whole content of a requested task as one
``task`` evidence file (``.state/evidence/task-<hex>.json``,
addressed by an id derived from the task id, as the history of the task is), in the same
state commit that records the task and its history. The work queue carries
only a fixed-size reference to it (``task_file``: the control repository,
the pinned commit, the path, the SHA-256 and the evidence id), so the size of
a task never meets a limit of the queue. Every reader resolves the reference
with ``task_file`` (standard library only) and gets the payload shape that
``tasks.payload`` builds.

The file holds ``tasks.task_data`` of the request. The plan, the report and
the history it names are references (path, SHA-256, evidence id) without the
commit, which a file cannot name: the reader takes it from the reference
that names the task file, and evidence is never removed, so that commit
holds them.

Evidence is append-only: a task file is never removed, like the history.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

import schemas
import task_file
import task_history
import tasks
import worker_report
from common import STATE_ROOT, canonical_json, safe_id

KIND = "task"
# The bytes a task file takes at most: below the bound of a state record
# (``storage.max_record_bytes``) with the fields around the task.
TASK_FILE_MAX_BYTES = 192 * 1024
# The kinds of reason evidence a task file reduces last: the plan (its
# reference is fetched by the trusted pre-agent step, so only its inlined
# index is cut) and the relayed human feedback, whose review and comment ids
# the trusted gates read.
KEPT_WHOLE = ("plan", "human_relay")
# The bytes the summary of a condensed reason evidence keeps.
CONDENSED_SUMMARY = 300


class TaskFileError(ValueError):
    """The content of the task cannot be built as a task file."""


def evidence_id(dedupe_key: str) -> str:
    """The id of the task file of the task ``dedupe_key``: derived from the
    task id, like the id of its history."""
    return task_file.evidence_id(dedupe_key)


def path(eid: str) -> str:
    return f"{STATE_ROOT}/evidence/{safe_id(eid)}.json"


def file_bytes(record: dict) -> bytes:
    """The bytes of ``record`` as the state store writes them."""
    import state

    return canonical_json(state.normalize_state_text(record)).encode("utf-8")


def file_digest_reader(root: Path | None, evidence: dict[str, dict],
                       staged: frozenset[str] = frozenset()) -> Callable[[str], str]:
    """The SHA-256 of the committed file of an evidence record: of the bytes
    the store writes for the record when it is staged in this tick
    (``staged``, evidence ids: a file of the checkout under the same id is
    replaced by it), else of the file under ``root`` when it is there, else
    of the bytes the store writes for the record."""
    def digest(eid: str) -> str:
        if root is not None and eid not in staged:
            file = root / path(eid)
            if file.is_file():
                return hashlib.sha256(file.read_bytes()).hexdigest()
        if eid not in evidence:
            raise TaskFileError(f"the evidence {eid} is not committed")
        return hashlib.sha256(file_bytes(evidence[eid])).hexdigest()
    return digest


def _reference(eid: str, digest: Callable[[str], str]) -> dict:
    return {"path": path(eid), "sha256": digest(eid)}


def _condense(item: dict) -> dict:
    reduced = {"evidence_id": item.get("evidence_id"), "kind": item.get("kind"),
               "summary": tasks._short(item.get("summary") or "", CONDENSED_SUMMARY),
               "evidence_path": path(str(item.get("evidence_id"))), "condensed": True}
    if item.get("reference") is not None:
        # The plan is committed evidence the pre-agent step fetches by this
        # reference and digest: the reference stays.
        reduced["reference"] = item["reference"]
    return reduced


def _shrink_relay(item: dict) -> dict:
    """A relayed human feedback with its bodies cut short: the ids the
    trusted gates read stay."""
    item = dict(item)
    data = dict(item.get("data") or {})
    for key in ("reviews", "comments", "conversation_comments"):
        data[key] = [dict(i, body=tasks._short(i.get("body") or "", 200))
                     for i in data.get(key) or []]
    item["data"] = data
    item["summary"] = tasks._short(item.get("summary") or "", CONDENSED_SUMMARY)
    return item


def _size(data: dict) -> int:
    return len(canonical_json(data).encode())


def condensed(data: dict, limit: int = TASK_FILE_MAX_BYTES) -> dict:
    """``data`` within ``limit`` bytes: the oldest reason evidence is reduced
    to its id, kind, a short summary and the path of its evidence file, one
    by one, until the task fits (``context.reason_evidence_condensed`` counts
    them). The evidence that is not the plan or the relayed human feedback
    goes first, then the plan (its reference to the committed file stays),
    and last the bodies of the relayed feedback (its ids stay). Raises
    ``TaskFileError`` only when that is not enough."""
    if _size(data) <= limit:
        return data
    context = dict(data["context"])
    items = [dict(i) for i in context.get("reason_evidence") or []]
    count = 0

    def fits() -> bool:
        nonlocal data
        context["reason_evidence"], context["reason_evidence_condensed"] = items, max(count, 1)
        data = dict(data, context=context)
        return _size(data) <= limit
    for kinds in (lambda k: k not in KEPT_WHOLE, lambda k: k == "plan"):
        for index, item in enumerate(items):
            if not kinds(item.get("kind")) or item.get("condensed"):
                continue
            items[index] = _condense(item)
            count += 1
            if fits():
                return data
    for index, item in enumerate(items):
        if item.get("kind") == "human_relay":
            items[index] = _shrink_relay(item)
            count += 1
            if fits():
                return data
    raise TaskFileError(f"the task takes {_size(data)} bytes, over {limit}, and its reason "
                        "evidence cannot be condensed further")


def build_data(record: dict, evidence: dict[str, dict], *,
               file_digest: Callable[[str], str]) -> dict:
    """The content of the task the record requests (``active_work``), from the
    committed ``evidence`` and the record: the content of its task file.
    Raises ``TaskFileError`` when it cannot be built."""
    request = tasks.request_from_active(record)
    if request is None:
        raise TaskFileError("the record requests no task to dispatch")
    if not record["upstream"].get("repository_id") or not record["upstream"].get("base_sha"):
        raise TaskFileError("the upstream identity or base is not bound")
    if not (record["active_work"].get("tracking_issue") or {}).get("url"):
        raise TaskFileError("the task carries no frozen tracking-issue description")
    history_id = task_history.evidence_id(request["dedupe_key"])
    if history_id not in evidence:
        raise TaskFileError("the history of the task is not recorded")
    try:
        allowed_paths, context = tasks.payload_inputs(
            request, record, evidence,
            plan_reference=lambda eid: _reference(eid, file_digest))
        context["history"] = dict(_reference(history_id, file_digest), evidence_id=history_id)
        data = tasks.task_data(request, record, allowed_paths=allowed_paths, context=context)
    except (KeyError, TypeError, ValueError) as error:
        raise TaskFileError(str(error)[:300]) from error
    data = condensed(data)
    try:
        schemas.validate("task", data)
    except schemas.SchemaError as error:
        raise TaskFileError(str(error)[:300]) from error
    return data


def build(record: dict, evidence: dict[str, dict], *, policy: dict, now: str,
          file_digest: Callable[[str], str]) -> dict:
    """The ``task`` evidence of the task the record requests, as the state
    store stages it."""
    data = build_data(record, evidence, file_digest=file_digest)
    dedupe_key = data["dedupe_key"]
    task_record = {
        "schema_version": 1, "evidence_id": evidence_id(dedupe_key),
        "contribution_id": record["contribution_id"], "kind": KIND,
        "policy_version": policy.get("policy_version", "unknown"), "collected_at": now,
        "summary": f"Task file of the {data['task']} task {dedupe_key}",
        "data": data,
        "digests": {"task_id": hashlib.sha256(dedupe_key.encode()).hexdigest()},
    }
    try:
        task_record = worker_report.scrub(task_record)
        schemas.validate("evidence", task_record)
    except (ValueError, schemas.SchemaError) as error:
        raise TaskFileError(str(error)[:300]) from error
    return task_record


def dry_run(record: dict, evidence: dict[str, dict]) -> str | None:
    """Why the content of the task ``record`` requests cannot be built or queued,
    or ``None`` when it can: the controller asks before it allocates the task,
    with the history it would record in the same commit (the history is
    bounded, so it stands in for itself), and the dispatch recovery asks for
    a task that was allocated without. The digests are those of a task built
    later, and have the same size, and the queue payload is measured with the
    longest control repository id."""
    request = tasks.request_from_active(record)
    if request is None:
        return None
    history_id = task_history.evidence_id(request["dedupe_key"])
    if history_id not in evidence:
        evidence = dict(evidence, **{history_id: {"evidence_id": history_id}})
    try:
        data = build_data(record, evidence, file_digest=lambda eid: "0" * 64)
        probe = {"schema_version": 1, "evidence_id": evidence_id(data["dedupe_key"]),
                 "contribution_id": record["contribution_id"], "kind": KIND,
                 "policy_version": "x", "collected_at": record["updated_at"],
                 "summary": "x", "data": data, "digests": {"task_id": "0" * 64}}
        if len(canonical_json(probe).encode()) > TASK_FILE_MAX_BYTES + 4096:
            return "the task file is over its size bound"
        queued = tasks.queue_payload(
            request, control_repo_id="9" * tasks.CONTROL_REPO_ID_MAX_DIGITS,
            reference=task_file.reference(request["dedupe_key"], "0" * 40, "0" * 64))
        size = len(json.dumps(queued, separators=(",", ":"), ensure_ascii=False).encode())
        if size > tasks.MAX_QUEUE_PAYLOAD_BYTES:
            return f"the queue payload takes {size} bytes, over {tasks.MAX_QUEUE_PAYLOAD_BYTES}"
    except TaskFileError as error:
        return str(error)
    return None
