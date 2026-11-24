# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The review of a held development patch, and how trusted code reads it.

Every development patch is reviewed before it is published. The worker's
trusted publication job verifies the patch without a token
(``fork_publish verify``) and publishes nothing; the reconciler holds it
(``hold_patch_for_review``), pinned in ``held_patch``: the worker run, its
publication artifact, the Work, the SHA-256 of the series, and the commit
it rebuilds to. A separate uncredentialed review Work
(``development_review``, the ``reviewer`` workflow and its model) reads and
diffs exactly that patch, with the worker's report, the verified plan, the
reason evidence of the worker's Work, and the history; it runs no
reproduction and writes nothing. Its Result
(``schemas/patch_review.schema.json``) is untrusted.

* ``check`` is the trusted check of the Result in the reviewer's publish job,
  without a token: the exact Work, the pin (digest and commit), findings
  only on files the patch changes, every text bounded and redacted
  (``worker_report.clean``). The verdict is derived from the findings: a
  blocker or a major finding fails the review whatever the reviewer
  declared; minor findings and nits pass and are recorded. A declared fail
  without a blocker or major finding, a wrong pin, or a malformed Result is
  not a usable review: the Work ended without a usable Result.
* ``evidence`` builds the ``patch_review`` evidence the reconciler stores for
  every review, passed or failed; the history of every later Work carries
  it (``work_history``).
* ``comment`` posts, on the development PR, the latest review and why it
  failed, once a patch published after successive failed reviews
  (``publish_after_failed_reviews``) is published. It is the one post about
  a review: nothing about reviews is posted on the tracking issue.

    python -m patch_review check --task T --result R --out O
    python -m patch_review comment --contribution C --evidence E --attempt N --out O
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import lifecycle
import schemas
import worker_report
from common import APP_COMMIT_NAME, STATE_ROOT, canonical_json, fitted_id, repository_root

TASK = "development_review"
STATUS = "patch_review"
PASS, FAIL = "pass", "fail"
# The severities that fail a review.
BLOCKING = ("blocker", "major")
SEVERITIES = ("blocker", "major", "minor", "nit")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
# The bounds of a review, the same in its schema
# (`schemas/patch_review.schema.json`), the reviewer's prompt, the stored
# evidence, and the full entry of the history: the findings and the answers
# to earlier findings, and the stored texts, in characters.
MAX_FINDINGS = 30
MAX_PREVIOUS_FINDINGS = 60
MAX_SUMMARY = 3000
MAX_DETAIL = 1500
MAX_SUGGESTION = 1000
MAX_TITLE = 300
# The marker of the comment on the development PR, by review evidence.
COMMENT_MARKER = "<!-- upstreaming-patch-review:{} -->"


def unusable(reason: str) -> dict:
    """The outcome of a Result that is not a usable review: the Work ended
    without a usable Result."""
    return {"status": "rejected", "failure_class": "validation", "stage": "patch_review_check",
            "reason": str(reason)[:2000]}


def pin(task: dict) -> dict | None:
    """The held patch the review Work names (``context.patch``), or ``None``
    when it names none that a review could answer."""
    patch = (task.get("context") or {}).get("patch")
    if not isinstance(patch, dict) or not _DIGEST.match(str(patch.get("patch_digest"))) \
            or not _SHA.match(str(patch.get("candidate"))) \
            or not isinstance(patch.get("changed_files"), list):
        return None
    return patch


def derived_verdict(findings: list[dict]) -> str:
    """Fail when a finding is a blocker or major, pass otherwise."""
    return FAIL if any(f.get("severity") in BLOCKING for f in findings) else PASS


def _finding(item: dict) -> dict:
    out = {"id": item["id"], "severity": item["severity"], "category": item["category"],
           "file": worker_report.clean(item["file"], 300) if item.get("file") else None,
           "line": item.get("line"), "end_line": item.get("end_line"),
           "title": worker_report.clean(item["title"], MAX_TITLE),
           "detail": worker_report.clean(item["detail"], MAX_DETAIL)}
    if item.get("suggestion"):
        out["suggestion"] = worker_report.clean(item["suggestion"], MAX_SUGGESTION)
    return out


def _previous(item: dict) -> dict:
    """A finding of an earlier review, as this review answers it."""
    out = {"ref": worker_report.clean(item["ref"], 200), "status": item["status"]}
    if item.get("note"):
        out["note"] = worker_report.clean(item["note"], 1000)
    return out


def check(task: dict, review: Any) -> dict:
    """The trusted outcome of one review Result of the held patch the Work
    names. A usable review has the status ``patch_review`` and the derived
    verdict; anything else is ``unusable``."""
    patch = pin(task)
    if task.get("task") != TASK or patch is None \
            or not _SHA.match(str(patch.get("expected_head") or "")):
        return unusable("the Work is not a review of a held development patch")
    if not isinstance(review, dict):
        return unusable("the review is not a JSON object")
    try:
        schemas.validate("patch_review", review)
    except schemas.SchemaError as error:
        return unusable(f"the review is malformed: {str(error)[:1500]}")
    for result_key, task_key in (("contribution_id", "contribution_id"),
                                 ("expected_revision", "revision")):
        if review.get(result_key) != task.get(task_key):
            return unusable(f"the review's {result_key} does not answer the Work")
    # The held patch, on the head it was verified on: the fork may have
    # moved since the patch was held, and then its publication replans it.
    if review["patch_digest"] != patch["patch_digest"] or review["candidate"] != patch["candidate"] \
            or review.get("expected_head") != patch["expected_head"]:
        return unusable("the review names another patch than the held patch it was given")
    changed = set(patch["changed_files"])
    outside = sorted({f["file"] for f in review["findings"]
                      if f.get("file") is not None and f["file"] not in changed})
    if outside:
        return unusable("findings name files the patch does not change: "
                        + ", ".join(outside[:5]))
    ids = [f["id"] for f in review["findings"]]
    if len(ids) != len(set(ids)):
        return unusable("two findings share an id")
    findings = [_finding(f) for f in review["findings"]]
    verdict = derived_verdict(findings)
    if review["verdict"] == FAIL and verdict == PASS:
        return unusable("a failed review names no blocker or major finding")
    return {
        "status": STATUS, "stage": "patch_review_check", "verdict": verdict,
        "declared_verdict": review["verdict"],
        "summary": worker_report.clean(review["summary"], MAX_SUMMARY),
        "findings": findings,
        "previous_findings": [_previous(p) for p in review.get("previous_findings") or []],
        "checked": [worker_report.clean(c, 500) for c in review.get("checked") or []],
        "limitations": [worker_report.clean(c, 1000) for c in review.get("limitations") or []],
        "patch_digest": review["patch_digest"], "candidate": review["candidate"],
        "expected_head": review["expected_head"],
    }


# --------------------------------------------------------------------------
# The held patch


def _retention() -> tuple[timedelta, timedelta]:
    """The effective retention of an artifact
    (``handoff.artifact_retention_days``) and the margin before its expiry
    at which it is no longer relied on."""
    import handoff

    retention = timedelta(days=handoff.artifact_retention_days())
    return retention, min(timedelta(days=handoff.EXPIRY_MARGIN_DAYS), retention / 2)


def _instant(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def held_patch_expires_at(outcome: dict, *, run: str, requested_at: str | None,
                          now: str) -> str:
    """When the artifacts the held patch depends on expire, at the earliest:
    the artifact that carries its series, by the expiry GitHub reported for
    it (else the retention after the Work was requested, before any of its
    runs began), and, when another run owns that artifact (a publish-only
    retry that verified the patch again), the worker run's own artifact,
    which its publication reads, by the retention after the Work was
    requested."""
    retention, _margin = _retention()
    since = _instant(requested_at) or _instant(now)
    bound = since + retention
    reported = _instant(outcome.get("publication_artifact_expires_at"))
    series = reported if reported is not None else bound
    if str(outcome.get("publication_run_id") or "") != str(run):
        series = min(series, bound)
    return series.isoformat()


def held_patch_expired(record: dict, now: Any) -> bool:
    """Whether an artifact the held patch depends on expired or expires
    within a day (``held_patch.expires_at``, which every pin records)."""
    held = record.get("held_patch")
    if not held:
        return False
    _, margin = _retention()
    expires = datetime.fromisoformat(str(held["expires_at"]).replace("Z", "+00:00"))
    now = now if isinstance(now, datetime) else datetime.fromisoformat(str(now))
    return now >= expires - margin


def held_patch(record: dict, outcome: dict, *, run: str, now: str,
               report_evidence_id: str | None) -> dict:
    """The pin of a verified patch (``outcome``, the Result of the worker's
    trusted verification) of the Work the record holds."""
    task = outcome.get("task") or {}
    work = record.get("active_work") or {}
    artifact = outcome.get("publication_artifact_id")
    values = {
        # The run that owns the artifact: the worker run, or the publish-only
        # retry of it that verified the patch again and carries the series.
        "worker_run_id": str(run), "artifact_run_id": str(outcome.get("publication_run_id") or ""),
        "artifact_id": artifact,
        # Reached only after the Result matched the Work identity, which
        # names both.
        "work": task["dedupe_key"], "generation": task["generation"],
        "revision": task.get("revision"), "patch_digest": outcome.get("patch_digest"),
        "candidate": outcome.get("candidate"), "tree": outcome.get("tree"),
        "expected_head": task.get("expected_head"), "operation": outcome.get("operation_kind"),
        "changed_files": [str(path)[:300] for path in (outcome.get("changed_files") or [])][:200],
        "reason_ids": list(work.get("reason_ids") or [])[:16],
        "feedback_evidence_ids": list(work.get("feedback_evidence_ids") or [])[:64],
        "report_evidence_id": report_evidence_id, "held_at": now,
        "expires_at": held_patch_expires_at(outcome, run=str(run),
                                            requested_at=work.get("requested_at"), now=now),
    }
    problems = []
    if not isinstance(artifact, int) or isinstance(artifact, bool) or artifact <= 0:
        problems.append("no publication artifact")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", values["worker_run_id"]):
        problems.append("no worker run")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", values["artifact_run_id"]):
        problems.append("no run that owns the publication artifact")
    elif outcome.get("publication_series_digest") != values["patch_digest"]:
        problems.append("the publication artifact does not carry the verified series")
    for key in ("candidate", "tree", "expected_head"):
        if not _SHA.match(str(values[key] or "")):
            problems.append(f"no {key}")
    if not _DIGEST.match(str(values["patch_digest"] or "")):
        problems.append("no patch digest")
    if values["operation"] not in ("append", "rewrite"):
        problems.append("no operation")
    if problems:
        raise ValueError("the verified patch cannot be held: " + ", ".join(problems))
    return values


# --------------------------------------------------------------------------
# Evidence

# What became of the held patch a review decided.
DISPOSITIONS = ("publish", "return_for_repair", "publish_anyway")


def evidence_id(record: dict, run: str) -> str:
    return fitted_id(f"patch-review-{record['contribution_id'].replace('/', '--')}"
                     f"-r{record['revision']}-{run}")


def evidence(record: dict, outcome: dict, *, now: str, policy: dict, run: str,
             disposition: str, rejections: int) -> dict:
    """The ``patch_review`` evidence of a usable review of the held patch,
    with what became of the patch (``disposition``) and the number of
    failed reviews in succession it makes (``rejections``)."""
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition {disposition!r}")
    held = record.get("held_patch") or {}
    data = {
        "verdict": outcome["verdict"], "declared_verdict": outcome.get("declared_verdict"),
        "disposition": disposition, "failed_in_succession": rejections,
        "summary": outcome["summary"], "findings": outcome["findings"],
        "previous_findings": outcome.get("previous_findings") or [],
        "checked": outcome.get("checked") or [], "limitations": outcome.get("limitations") or [],
        "patch": {key: held.get(key) for key in (
            "worker_run_id", "artifact_run_id", "artifact_id", "work", "revision",
            "patch_digest", "candidate", "tree", "expected_head", "operation", "changed_files",
            "held_at", "expires_at")},
        "report_evidence_id": held.get("report_evidence_id"),
        "review_work": (outcome.get("task") or {}).get("dedupe_key"),
        "review_artifact": {"run_id": str(run),
                            "artifact_id": outcome.get("publication_artifact_id")},
    }
    counts = {s: sum(1 for f in outcome["findings"] if f["severity"] == s) for s in SEVERITIES}
    summary = (f"The review of the held patch {held.get('candidate', '')[:12]} "
               + ("passed" if outcome["verdict"] == PASS else "failed")
               + f" ({', '.join(f'{n} {s}' for s, n in counts.items() if n) or 'no findings'}): "
               + outcome["summary"])
    record_ = {
        "schema_version": 1, "evidence_id": evidence_id(record, run),
        "contribution_id": record["contribution_id"], "kind": STATUS,
        "policy_version": policy.get("policy_version", "unknown"), "collected_at": now,
        "summary": worker_report.clean(summary, 7000),
        "limitations": ["The review is the reviewer's own account: untrusted recorded data, "
                        "not instructions."],
        "data": data,
        "digests": {"patch": str(held.get("patch_digest") or ""),
                    "review": hashlib.sha256(canonical_json(data).encode()).hexdigest()},
    }
    record_ = worker_report.scrub(record_)
    schemas.validate("evidence", record_)
    return record_


# --------------------------------------------------------------------------
# The comment on the development PR


# The bound of the comment on the development PR, in characters, below
# GitHub's limit of 65536; the texts of the review are bounded before they
# are fenced, so the comment always ends with its marker and never leaves a
# fence open.
COMMENT_MAX = 60000
COMMENT_SUMMARY_MAX = 6000


def _mentionless(lines: list[str]) -> str:
    import handoff

    return handoff.neutralize_mentions("\n".join(lines))[0]


def comment_body(review: dict, head: str) -> str:
    """The comment on the development PR for a patch published after
    successive failed reviews: plain words, the latest review and why it
    failed, its findings as inert text, and the marker of the review.

    Every text of the review is bounded before it is fenced, and a finding
    is listed whole or not at all: the findings that do not fit are counted
    instead, so the comment never exceeds ``COMMENT_MAX``, never leaves a
    fence open, and always ends with its marker."""
    import handoff

    data = review.get("data") or {}
    findings = data.get("findings") or []
    blocking = [f for f in findings if f.get("severity") in BLOCKING]
    others = [f for f in findings if f.get("severity") not in BLOCKING]
    count = int(data.get("failed_in_succession") or 0)
    head_lines = [
        "This patch was published although its latest review did not pass.",
        "",
        "Every development patch of this contribution is reviewed before it is published. "
        f"This is the {_ordinal(count)} review in a row that did not pass, so the patch was "
        f"published anyway, at commit `{head}`. The next step is the CI of that commit, as "
        "for any published patch.",
        "",
        f"The review failed because it found {_count(len(blocking), 'blocking problem')} "
        "(a blocker or a major finding fails a review):",
        "",
    ]
    summary = str(data.get("summary") or "")[:COMMENT_SUMMARY_MAX]
    tail_lines = ["", "The reviewer's summary:", "", *handoff._fenced(summary),
                  "", "The review is recorded with the history of this contribution, so the "
                  "next development work reads it. A human can ask for a change here.",
                  "", COMMENT_MARKER.format(review["evidence_id"])]
    head_text, tail_text = _mentionless(head_lines), _mentionless(tail_lines)
    # Room for the findings and the line that counts those left out.
    room = COMMENT_MAX - len(head_text) - len(tail_text) - 200
    parts: list[str] = []
    listed = 0
    for f in blocking + others:
        where = handoff._code(f"{f['file']}:{f['line']}" if f.get("file") and f.get("line")
                              else f.get("file") or "the whole patch", 300)
        lines = [f"- **{f['severity']}**, {str(f.get('category') or '').replace('_', ' ')}, "
                 f"{where}: " + handoff._code(f.get("title"), 300)]
        lines.extend("  " + line
                     for line in handoff._fenced(str(f.get("detail") or "")[:MAX_DETAIL]))
        if f.get("suggestion"):
            lines.append("  Suggested change:")
            lines.extend("  " + line for line in handoff._fenced(
                str(f["suggestion"])[:MAX_SUGGESTION]))
        text = _mentionless(lines)
        if len(text) + 1 > room:
            break
        parts.append(text)
        room -= len(text) + 1
        listed += 1
    left = len(findings) - listed
    if left:
        parts.append(f"- {_count(left, 'further finding')} of the review "
                     f"{'is' if left == 1 else 'are'} recorded with the history of this "
                     "contribution and not listed here.")
    return "\n".join([head_text, *parts, tail_text])


def _ordinal(n: int) -> str:
    words = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}
    return words.get(n, f"{n}th")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


# The posts of the comment of one review: the attempts, and the minutes
# before an attempt that failed is made again (times the attempts so far).
COMMENT_ATTEMPTS = 5
COMMENT_RETRY_MINUTES = 30
# Once those attempts are spent the comment is exhausted: it is alerted on in
# every tick and posted again once every this many hours, until it is posted
# or found, so a human who restores what the post needs ends the alert.
COMMENT_EXHAUSTED_RETRY_HOURS = 24
# The last attempt of a comment: the `attempt` input of `review-comment.yml`
# admits 1 to 999 (`^[1-9][0-9]{0,2}$`), the schema of `review_comments`
# bounds `attempts` the same, and the command line refuses any other. An
# exhausted comment at this attempt is no longer launched; it stays due and
# alerted on.
COMMENT_LAST_ATTEMPT = 999
# A launched post whose receipt is not folded within this many minutes is
# a failed attempt.
COMMENT_RECEIPT_WAIT_MINUTES = 60
# A due comment whose post is held (by the pause of a contribution that is
# not terminal, or because no development PR is recorded yet) for this many
# hours is alerted on in every tick until it is launched; the held comment of
# a terminal contribution is alerted on at once, since nothing resumes it.
COMMENT_HELD_ALERT_HOURS = 24
# The outcomes of a post that end its comment: posted now, or found by its
# marker (an earlier attempt posted it).
COMMENT_DONE = ("posted", "found")
# The comments a record keeps due. Beyond this bound the oldest exhausted
# comments are dropped by the tick, each with an alert; the schema allows
# twice as many (``RECORDED_DUE_COMMENTS``), for the comments that become due
# between two ticks.
MAX_DUE_COMMENTS = 16
# The comments the record can hold (`maxItems` of `review_comments`). A
# comment that becomes due beyond it drops the oldest exhausted comment, and
# only when none is left the oldest other one, never silently: every comment
# dropped is returned, and the tick alerts on it.
RECORDED_DUE_COMMENTS = 2 * MAX_DUE_COMMENTS
# The author of the comment: the App whose token posts it. Only a comment of
# that author carries the marker of a review.
COMMENT_AUTHOR = APP_COMMIT_NAME


def published_anyway(record: dict, outcome: dict, evidence: dict[str, dict]) -> list[tuple[str, str]]:
    """The reviews whose comment a ``published`` Result makes due, as
    ``(evidence id, published head)``: every ``patch_review`` evidence of
    the contribution that published its held patch anyway
    (``publish_anyway``) and whose pin is the patch this Result published:
    the same worker run, Work, and commit. It holds whatever state the
    record is in when the Result is collected, and however late: the patch
    is on the fork either way, and its development PR must say why."""
    if outcome.get("status") != "published":
        return []
    run = str(outcome.get("worker_run_id") or "")
    work = (outcome.get("task") or {}).get("dedupe_key")
    op = outcome.get("operation") or {}
    heads = {h for h in (outcome.get("candidate"),
                         (op.get("proposed") or {}).get("commit")
                         if op.get("status") == "confirmed" else None)
             if isinstance(h, str) and _SHA.match(h)}
    found = []
    for eid, item in sorted(evidence.items()):
        data = item.get("data") or {}
        patch = data.get("patch") or {}
        if item.get("kind") == STATUS \
                and item.get("contribution_id") == record["contribution_id"] \
                and data.get("disposition") == "publish_anyway" \
                and run and str(patch.get("worker_run_id")) == run \
                and work is not None and patch.get("work") == work \
                and patch.get("candidate") in heads:
            found.append((eid, patch["candidate"]))
    return found


def pending_published_anyway(record: dict) -> list[tuple[str, str]]:
    """The review the pending publication of the held patch was published
    anyway after, as ``(evidence id, reviewed commit)``, or nothing."""
    review = (record.get("pending_publication") or {}).get("patch_review") or {}
    if not review.get("published_anyway"):
        return []
    return [(review["evidence_id"], (record.get("held_patch") or {}).get("candidate"))]


def landed_anyway(record: dict, heads: set[str] | list[str] | dict[str, str],
                  evidence: dict[str, dict]) -> list[tuple[str, str]]:
    """The reviews whose comment trusted branch evidence makes due, as
    ``(evidence id, head)``: every ``patch_review`` evidence of the
    contribution that published its held patch anyway (``publish_anyway``)
    and whose pinned commit (``patch.candidate``) is one of ``heads``, the
    commits that evidence shows on the development branch: the proposed
    commit of a branch operation of the contribution once it is
    ``confirmed`` (by discovery, by the read-back of a publish-only retry,
    or by a human resolution), or a fork head that ``record_head_change``
    records, or the head observed when a command moves a record whose
    recorded head it is not. However the Result of that publication ends
    (uncertain, lost, or folded after a pause, a processing error or the
    exhaustion of ``publish_recovery``), the reviewed commit on the branch
    is the patch published anyway. ``heads`` may map each pinned commit to
    the commit the branch holds for it (``confirmed_heads``), which the
    comment names."""
    heads = dict(heads) if isinstance(heads, dict) else {h: h for h in heads}
    heads = {h: (landed if isinstance(landed, str) and _SHA.match(landed) else h)
             for h, landed in heads.items() if isinstance(h, str) and _SHA.match(h)}
    found = []
    for eid, item in sorted(evidence.items()):
        data = item.get("data") or {}
        candidate = (data.get("patch") or {}).get("candidate")
        if item.get("kind") == STATUS \
                and item.get("contribution_id") == record["contribution_id"] \
                and data.get("disposition") == "publish_anyway" and candidate in heads:
            found.append((eid, heads[candidate]))
    return found


def confirmed_heads(record: dict, operations, observed: str | None = None) -> dict[str, str]:
    """The commits that the confirmed branch operations among ``operations``
    (``branch_update`` or ``branch_rewrite`` of the contribution) put on its
    branch, by proposed commit: the proposed commit itself, or ``observed``,
    the head the discovery that confirmed them read, which differs from it
    when discovery confirmed the operation by its tree and parents (a signed
    replay changes the commit)."""
    return {str(prop.get("commit")): (observed or str(prop.get("commit")))
            for op in operations
            for prop in [op.get("proposed") or {}]
            if op.get("contribution_id") == record["contribution_id"]
            and op.get("kind") in ("branch_update", "branch_rewrite")
            and op.get("status") == "confirmed"}


def comment_due(record: dict, reviews: list[tuple[str, str]], *,
                now: str) -> tuple[list[dict], list[dict]]:
    """The record's due comments (``review_comments``) once a patch
    published anyway is published, and the comments dropped to fit them:
    every comment still due, kept, and the comment of each review in
    ``reviews`` (``(evidence id, published head)``) that is not due
    already: the review, the published head, the development PR it is
    posted on when one is recorded (else the one recorded when it is
    launched), and no attempt yet, due now. Idempotent by evidence id.

    The record holds ``RECORDED_DUE_COMMENTS``: beyond it the oldest
    exhausted comment that is not launched is dropped first, then the
    oldest exhausted one, then the oldest that is not launched, then the
    oldest; each one dropped is returned for the alert of the tick."""
    due = [dict(item) for item in record.get("review_comments") or []]
    fork = record.get("fork") or {}
    for eid, head in reviews:
        if any(item["evidence_id"] == eid for item in due):
            continue
        number = fork.get("pr_number")
        due.append({"evidence_id": eid, "head": head,
                    "repository": fork.get("name") if number else None, "pr_number": number,
                    "attempts": 0, "next_attempt_at": now, "launched_at": None,
                    "last_reason": "", "exhausted": False})
    dropped = []
    while len(due) > RECORDED_DUE_COMMENTS:
        oldest = min(range(len(due)), key=lambda k: (not due[k].get("exhausted"),
                                                     bool(due[k].get("launched_at")), k))
        dropped.append(due.pop(oldest))
    return due, dropped


def held_by_pause(record: dict) -> bool:
    """Whether a pause holds the post of a due comment: the record is paused
    and not terminal. Posting the comment is no development Work, and
    nothing resumes a terminal record, so its pause holds nothing."""
    return bool(record.get("paused")) \
        and f"{record['phase']}.{record['activity']}" not in lifecycle.TERMINAL_STATES


def comment_target(record: dict, due: dict) -> tuple[str | None, int | None]:
    """The development PR a due comment is posted on: the one recorded when
    it became due, else the record's."""
    fork = record.get("fork") or {}
    if due.get("repository") and due.get("pr_number"):
        return due["repository"], int(due["pr_number"])
    if fork.get("name") and fork.get("pr_number"):
        return fork["name"], int(fork["pr_number"])
    return None, None


def due_comment(record: dict, evidence_id: str) -> dict | None:
    return next((item for item in record.get("review_comments") or []
                 if item.get("evidence_id") == evidence_id), None)


def comment_check(record: dict, evidence_id: str, attempt: int, policy: dict) -> str | None:
    """Why the comment named by ``evidence_id`` and ``attempt`` must not be
    posted now, or ``None``: it is a due comment of the record, at its
    current attempt, on a known development PR, while no pause holds it
    (``held_by_pause``) and the policy permits writes."""
    due = due_comment(record, evidence_id) or {}
    if not 1 <= attempt <= COMMENT_LAST_ATTEMPT or due.get("attempts") != attempt:
        return "the comment is not a due comment of the record at this attempt"
    if held_by_pause(record):
        return "the contribution is paused"
    if policy.get("read_only", True):
        return "the policy is read-only"
    repository, number = comment_target(record, due)
    if not repository or not number:
        return "no development PR is recorded"
    if not _SHA.match(str(due.get("head") or "")):
        return "the published head is unknown"
    return None


def _comments(api, repository: str, number: int):
    """Every comment of the PR, page by page."""
    page = 1
    while True:
        items = api.call("GET", f"/repos/{repository}/issues/{number}/comments"
                                f"?per_page=100&page={page}") or []
        yield from items
        if len(items) < 100:
            return
        page += 1


def comment(api, record: dict, evidence_id: str, attempt: int, *, policy: dict,
            root: Path | None = None) -> dict:
    """Post the comment of a patch published after successive failed reviews
    on its development PR, once: a comment of the App (``COMMENT_AUTHOR``)
    that carries the marker of the review, on any page, is found instead of
    posted again; the marker in anyone else's comment is ignored. The
    receipt names the contribution, the review and the attempt."""
    receipt = {"contribution_id": record["contribution_id"], "evidence_id": evidence_id,
               "attempt": attempt}
    why = comment_check(record, evidence_id, attempt, policy)
    if why:
        return receipt | {"status": "skipped", "reason": why}
    due = due_comment(record, evidence_id) or {}
    repository, number = comment_target(record, due)
    path = (root or repository_root()) / STATE_ROOT / "evidence" / f"{evidence_id}.json"
    review = json.loads(path.read_text(encoding="utf-8"))
    if review.get("kind") != STATUS or review.get("evidence_id") != evidence_id:
        return receipt | {"status": "skipped", "reason": f"{evidence_id} is not a patch review"}
    marker = COMMENT_MARKER.format(evidence_id)
    for item in _comments(api, repository, number):
        if (item.get("user") or {}).get("login") == COMMENT_AUTHOR \
                and marker in (item.get("body") or ""):
            return receipt | {"status": "found", "comment_id": item.get("id")}
    posted = api.call("POST", f"/repos/{repository}/issues/{number}/comments",
                      {"body": comment_body(review, due["head"])})
    return receipt | {"status": "posted", "comment_id": (posted or {}).get("id")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m patch_review")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="check a review Result without a token")
    c.add_argument("--task", type=Path, required=True)
    c.add_argument("--result", type=Path, required=True, help="the review proposal.json")
    c.add_argument("--out", type=Path, required=True)
    for name, text in (("comment-due", "whether the comment of a patch published anyway is "
                                         "due, without a token"),
                       ("comment", "post the comment of a patch published anyway")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--contribution", required=True, help="the contribution's file id")
        p.add_argument("--evidence", required=True)
        p.add_argument("--attempt", type=int, required=True)
        p.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "check":
        task = json.loads(args.task.read_text())
        try:
            result = check(task, json.loads(args.result.read_text()))
        except (OSError, ValueError) as error:
            result = unusable(f"the Result cannot be read: {str(error)[:300]}")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2))
        return 0
    import review_response
    from common import load_policy

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,200}", args.contribution) \
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", args.evidence) \
            or ".." in args.evidence or "/" in args.evidence:
        raise SystemExit("bad contribution or evidence id")
    if not 1 <= args.attempt <= COMMENT_LAST_ATTEMPT:
        raise SystemExit("bad attempt")
    record = json.loads((repository_root() / STATE_ROOT / "contributions"
                         / f"{args.contribution}.json").read_text(encoding="utf-8"))
    schemas.validate("contribution", record)
    policy = load_policy()
    token = os.environ.pop("UPSTREAMING_FORK_TOKEN", None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "comment-due":
        why = comment_check(record, args.evidence, args.attempt, policy)
        target, _number = comment_target(record, due_comment(record, args.evidence) or {})
        result = {"due": why is None, "reason": why,
                  "fork_name": str(target or "").split("/")[-1]}
        if why:
            # The receipt of a post that is not due: the reconciler folds it.
            receipt = {"contribution_id": record["contribution_id"],
                       "evidence_id": args.evidence, "attempt": args.attempt,
                       "status": "skipped", "reason": why}
            (args.out.parent / "review-comment.json").write_text(json.dumps(receipt, indent=2))
    elif not token:
        result = {"contribution_id": record["contribution_id"], "evidence_id": args.evidence,
                  "attempt": args.attempt, "status": "failed",
                  "reason": "no token for the development PR"}
    else:
        try:
            result = comment(review_response.Api(token), record, args.evidence, args.attempt,
                             policy=policy)
        except Exception as error:  # noqa: BLE001 - every failure is a receipt
            result = {"contribution_id": record["contribution_id"],
                      "evidence_id": args.evidence, "attempt": args.attempt,
                      "status": "failed", "reason": worker_report.clean(str(error), 500)}
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
