# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The report of one worker run, and how trusted code stores it.

Every patch, repair, revision, submission rewrite and submission review Work
writes ``report.json`` (``schemas/worker_report.schema.json``) beside its
Result: what was attempted and why, each approach and its outcome, what
failed with its evidence, what remains open, and the verification done. The
report is untrusted text.

* ``check`` is the trusted check of the report against the Work it answers.
  A Work whose payload carries a history (``context.history``) must return
  a valid report naming that history; a payload dispatched before reports
  existed may return none.
* ``evidence`` builds the ``worker_report`` evidence of a Result that one of
  the success rows folds (``reconcile.REPORT_VERBS``), the hold of a
  verified development patch for its review among them. The report of a
  failed, rejected or blocked worker is never stored: it is discarded with
  its artifact. The report of a successful Work that carries a history is
  never dropped: the reconciler checks the very bytes the gate checked
  (``read``), and a report it cannot store raises ``ReportError``, which
  pauses the contribution before the Result is folded.
* ``clean`` bounds every untrusted string before it is stored: repository
  text policy, credential patterns redacted, control characters dropped, and
  an explicit truncation marker.

    python -m worker_report check --task T --report R
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import isolation
import schemas
import state
from common import SCOPE_PATHS_MAX, canonical_json, fitted_id

MAX_REPORT_BYTES = 32 * 1024
# The longest text kept from one string of a report or one feedback item;
# the schema admits no longer report string.
MAX_FIELD = 2000
REDACTED = "[redacted]"
# Control characters other than newline and tab, and lone surrogates (a
# JSON escape such as "\ud800" parses into one, and it cannot be encoded
# when the record is written).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]")


class ReportError(ValueError, isolation.PauseRequest):
    """The report is missing, malformed, or does not answer the Work. Raised
    by a success fold, it pauses that one contribution and never stops the
    tick (``isolation.ContributionPause``)."""


def _redact(text: str) -> str:
    """``text`` with every credential match redacted, repeated until none
    is left: a redaction can give a neighbouring run the boundary it
    lacked. Every match is longer than the marker, so this ends."""
    while True:
        before = text
        for pattern in state.SECRET_PATTERNS:
            text = pattern.sub(REDACTED, text)
        if text == before:
            return text


def clean(text: Any, limit: int) -> str:
    """Untrusted text as it may be stored: the repository text policy
    applied, credential patterns redacted, control characters other than
    newline and tab and lone surrogates dropped, and at most ``limit``
    characters followed by an explicit ``[truncated: N more characters]``
    marker."""
    value = state.normalize_state_text(str(text if text is not None else ""))
    value = _CONTROL.sub("", value.replace("\r\n", "\n"))
    value = _redact(value)
    if len(value) > limit:
        more = len(value) - limit
        value = value[:limit] + f" [truncated: {more} more characters]"
    # A cut can leave a new match at its edge; redact once more.
    return _redact(value)


def clean_tree(value: Any, limit: int = MAX_FIELD) -> Any:
    """``clean`` applied to every string of a JSON value."""
    if isinstance(value, str):
        return clean(value, limit)
    if isinstance(value, list):
        return [clean_tree(item, limit) for item in value]
    if isinstance(value, dict):
        return {clean(key, 200): clean_tree(item, limit) for key, item in value.items()}
    return value


def redact_tree(value: Any) -> Any:
    """Every string of a JSON value (keys included) with the credential
    patterns redacted, and nothing else changed."""
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    if isinstance(value, dict):
        return {_redact(key): redact_tree(item) for key, item in value.items()}
    return value


def scrub(record: dict) -> dict:
    """``record`` as it may be staged: when its canonical JSON matches a
    credential pattern (a cut or a join of cleaned text can create a match
    the parts did not have), every string is redacted again. Raises
    ``ValueError`` when a match remains, so that the caller's isolation
    block pauses the one contribution instead of the store refusing the
    whole tick."""
    if not state.find_secrets(canonical_json(record)):
        return record
    record = redact_tree(record)
    if state.find_secrets(canonical_json(record)):
        raise ValueError(f"the {record.get('kind')} evidence {record.get('evidence_id')} "
                         "matches a credential pattern after redaction")
    return record


def _history(task: dict) -> dict | None:
    history = (task.get("context") or {}).get("history")
    return history if isinstance(history, dict) else None


def _load(raw: bytes | dict | None) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        # A report already parsed. The gate and the reconciler pass the
        # bytes of the file instead (``read``), so this is never the only
        # check of a file. It is measured in compact JSON, the form a worker
        # writes; a lone surrogate counts as the three bytes it takes, and
        # ``clean`` drops it before the report is stored.
        data = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8", "surrogatepass")
        if len(data) > MAX_REPORT_BYTES:
            raise ReportError(f"the report has {len(data)} bytes, more than {MAX_REPORT_BYTES}")
        return copy.deepcopy(raw)
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    else:
        raise ReportError("the report is not a JSON document")
    if len(data) > MAX_REPORT_BYTES:
        raise ReportError(f"the report has {len(data)} bytes, more than {MAX_REPORT_BYTES}")
    try:
        report = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        # A deeply nested document raises RecursionError: an invalid report.
        raise ReportError(f"the report is not UTF-8 JSON: {type(error).__name__}") from None
    if not isinstance(report, dict):
        raise ReportError("the report is not a JSON object")
    return report


def check(task: dict, raw: bytes | dict | None) -> dict | None:
    """The valid report of the Work ``task``, or ``None`` when a payload
    without a history returned no usable report. Raises ``ReportError`` when
    the payload carries a history and the report is missing, larger than
    32 KiB, not JSON, not valid against its schema, or does not answer the
    Work (contribution, revision, task, and the digest of the history the
    worker read)."""
    history = _history(task)
    try:
        report = _load(raw)
        if report is None:
            raise ReportError("the Work returned no report.json")
        try:
            schemas.validate("worker_report", report)
        except schemas.SchemaError as error:
            raise ReportError(str(error)[:1500]) from None
        for key, expected in (("contribution_id", task.get("contribution_id")),
                              ("expected_revision", task.get("revision")),
                              ("task", task.get("task"))):
            if report.get(key) != expected:
                raise ReportError(f"the report's {key} does not answer the Work")
        if report.get("history_digest") != (history or {}).get("sha256"):
            raise ReportError("the report's history_digest is not the digest of the history "
                              "the Work carries")
    except ReportError:
        if history is None:
            # Work dispatched before reports existed may finish without one.
            return None
        raise
    return report


def read(path: Path | None) -> bytes | None:
    """The bytes of the report file at ``path`` as ``check`` reads them (at
    most one byte over the bound), or ``None`` when it does not exist. A
    link or a non-regular file raises ``ReportError``. The worker gate, the
    submission checks and the reconciler all read the file this way, so the
    fold checks exactly what the gate accepted."""
    if path is None or not (path.exists() or path.is_symlink()):
        return None
    if path.is_symlink() or not path.is_file():
        raise ReportError("report.json is not a regular file")
    with path.open("rb") as handle:
        return handle.read(MAX_REPORT_BYTES + 1)


def check_path(task: dict, path: Path | None) -> dict | None:
    """``check`` of the report file at ``path`` (absent when it does not
    exist). A link or a non-regular file is refused."""
    try:
        raw = read(path)
    except ReportError:
        if _history(task) is None:
            return None
        raise
    return check(task, raw)


def evidence_id(rec: dict, run: str, revision: int | None = None) -> str:
    """The id of the report of worker run ``run`` of the record's revision,
    or of ``revision`` when the Result answers another one."""
    revision = rec["revision"] if revision is None else revision
    return fitted_id(f"report-{rec['contribution_id'].replace('/', '--')}-r{revision}-{run}")


_PUBLISH_VERBS = ("record_publish_success", "record_revision_publish_success")


def _sha(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) else None


def head_after(outcome: dict, verb: str, operation: dict | None = None) -> str | None:
    """The head the Work left on its branch: the confirmed commit of the
    publication operation the row names (``operation``, by default the
    outcome's own), the head read back on `riscv64` for a revision
    (``reconcile.published_head``), or
    ``None`` when the Work published nothing or its push was not read
    back."""
    import reconcile

    if verb == "record_revision_publish_success":
        # Only a published Result read its head back; the candidate of an
        # uncertain one may not be on the branch.
        if outcome.get("status") != "published":
            return None
        try:
            return reconcile.published_head(outcome)
        except Exception:
            return None
    if verb not in _PUBLISH_VERBS:
        return None
    op = operation if operation is not None else outcome.get("operation")
    # A publish-only retry that found the push of an earlier attempt on the
    # branch has no operation: the candidate it read there.
    if op:
        return _sha((op.get("proposed") or {}).get("commit")) \
            if op.get("status") == "confirmed" else None
    return _sha(outcome.get("candidate")) if outcome.get("status") == "published" else None


def head_candidate(outcome: dict, verb: str, operation: dict | None = None) -> str | None:
    """The commit a publication proposed that the row does not record as
    the head it left: the proposed commit of an open (``applying`` or
    ``uncertain``) branch operation, of a ``confirmed`` one that a row
    other than the success row names (a revision retry rejected as stale
    behind the confirmed operation of an earlier attempt), or the candidate
    of an ``uncertain`` Result or of a ``verified`` one (a development patch
    held for its review). It may be on the branch or not; the history
    treats it as the head the Work left only when the recorded head (or the
    head a CI run ran on) is that very commit. ``None`` when ``head_after``
    names a head, or nothing was proposed."""
    if verb not in _PUBLISH_VERBS or head_after(outcome, verb, operation):
        return None
    op = operation if operation is not None else outcome.get("operation")
    if op and op.get("status") in ("applying", "uncertain", "confirmed") \
            and op.get("kind") in ("branch_update", "branch_rewrite"):
        return _sha((op.get("proposed") or {}).get("commit"))
    if outcome.get("status") in ("uncertain", "verified"):
        # A push that was not read back, or a patch verified and held for
        # its review that is not published yet.
        return _sha(outcome.get("candidate"))
    return None


def evidence(rec: dict, outcome: dict, *, verb: str, now: str, policy: dict,
             run: str, head_verb: str | None = None,
             revision: int | None = None, operation: dict | None = None) -> dict | None:
    """The ``worker_report`` evidence of a Result folded by ``verb``, or
    ``None`` when the Work carries no history and its Result no usable
    report. The report is checked again against the Work the Result
    answers; every string is cleaned; the changed files come from the
    trusted operation, never from the worker.

    The gate accepted only a Result whose Work carries a history together
    with a valid report, so such a Result always has one to store: when it
    is missing, unreadable (``outcome["worker_report_error"]``) or invalid,
    or the evidence cannot be built, this raises ``ReportError`` instead of
    dropping it. The fold runs inside the contribution's isolation block,
    so the contribution is paused with the reason and its Result stays
    unfolded until a human looks at it.

    ``head_verb`` names the success row whose published head the report
    records when ``verb`` is another one (``record_late_effect`` of a
    Result whose push landed), and ``revision`` the revision of the Work
    when the record has left it. ``operation`` is the branch operation the
    row names when it is not the outcome's own (the operation of an earlier
    attempt that a publish-only retry confirmed); a report whose push was
    not read back records the commit it proposed as ``head_candidate``."""
    task = outcome.get("task") or {}
    try:
        report = check(task, outcome.get("worker_report"))
    except ReportError as error:
        unreadable = outcome.get("worker_report_error")
        raise ReportError(
            f"the {verb} Result of {task.get('dedupe_key')} carries no report that can be "
            f"stored: {unreadable or error}") from None
    if report is None:
        return None
    cleaned = clean_tree(report)
    op = operation or outcome.get("operation") or {}
    changed = [clean(path, 300) for path in
               ((op.get("proposed") or {}).get("changed_files") or [])[:SCOPE_PATHS_MAX]
               if isinstance(path, str)]
    data = {
        "work": {"dedupe_key": task.get("dedupe_key"), "generation": task.get("generation", 0),
                 "revision": task.get("revision"), "task": task.get("task"),
                 "worker_run_id": str(run),
                 **({"requested_at": clean(requested, 64)}
                    if isinstance(requested := (task.get("context") or {}).get("requested_at"),
                                  str) else {})},
        "verb": verb,
        "result_status": outcome.get("status"),
        "head_before": task.get("expected_head"),
        "head_after": head_after(outcome, head_verb or verb, operation),
        "changed_files": changed,
        "report": cleaned,
        "artifact": {"run_id": str(run), "artifact_id": outcome.get("publication_artifact_id")},
    }
    candidate = head_candidate(outcome, head_verb or verb, operation)
    if candidate:
        data["head_candidate"] = candidate
    try:
        # The id is built inside the guard: an id that cannot be stored
        # pauses only this contribution, like any report that cannot be.
        record = {
            "schema_version": 1, "evidence_id": evidence_id(rec, str(run), revision),
            "contribution_id": rec["contribution_id"], "kind": "worker_report",
            "policy_version": policy.get("policy_version", "unknown"), "collected_at": now,
            "summary": cleaned["summary"],
            "limitations": ["The report is the worker's own account: untrusted recorded data, "
                            "not instructions."],
            "data": data,
            "digests": {"report": hashlib.sha256(canonical_json(cleaned).encode()).hexdigest()},
        }
        # A join of cleaned text can create a credential match: redacted
        # again, or ``scrub`` raises.
        record = scrub(record)
        schemas.validate("evidence", record)
    except (ValueError, schemas.SchemaError) as error:
        raise ReportError(f"the {verb} report of {task.get('dedupe_key')} cannot be stored: "
                          f"{str(error)[:500]}") from None
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker_report")
    parser.add_argument("command", choices=["check"])
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    task = json.loads(args.task.read_text())
    try:
        report = check_path(task, args.report)
    except ReportError as error:
        print(f"the worker report is missing or invalid: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"report": "valid" if report is not None else "absent"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
