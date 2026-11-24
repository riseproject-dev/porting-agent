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
    python -m patch_review comment --record R --outcome O --out O2
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

import schemas
import worker_report
from common import STATE_ROOT, canonical_json, fitted_id, repository_root

TASK = "development_review"
STATUS = "patch_review"
PASS, FAIL = "pass", "fail"
# The severities that fail a review.
BLOCKING = ("blocker", "major")
SEVERITIES = ("blocker", "major", "minor", "nit")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
# The bounds of the stored texts, in characters.
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
            or not _SHA.match(str(task.get("expected_head") or "")):
        return unusable("the Work is not a review of a held development patch")
    if not isinstance(review, dict):
        return unusable("the review is not a JSON object")
    try:
        schemas.validate("patch_review", review)
    except schemas.SchemaError as error:
        return unusable(f"the review is malformed: {str(error)[:1500]}")
    for result_key, task_key in (("contribution_id", "contribution_id"),
                                 ("expected_revision", "revision"),
                                 ("expected_head", "expected_head")):
        if review.get(result_key) != task.get(task_key):
            return unusable(f"the review's {result_key} does not answer the Work")
    if review["patch_digest"] != patch["patch_digest"] or review["candidate"] != patch["candidate"]:
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


def held_patch_expired(record: dict, now: Any) -> bool:
    """Whether the publication artifact of the held patch expired or
    expires within a day, for the effective retention
    (``handoff.artifact_retention_days``)."""
    import handoff

    held = record.get("held_patch") or {}
    at = held.get("held_at")
    if not at:
        return False
    now = now if isinstance(now, datetime) else datetime.fromisoformat(str(now))
    retention = timedelta(days=handoff.artifact_retention_days())
    margin = min(timedelta(days=handoff.EXPIRY_MARGIN_DAYS), retention / 2)
    return now - datetime.fromisoformat(at) >= retention - margin


def held_patch(record: dict, outcome: dict, *, run: str, now: str,
               report_evidence_id: str | None) -> dict:
    """The pin of a verified patch (``outcome``, the Result of the worker's
    trusted verification) of the Work the record holds."""
    task = outcome.get("task") or {}
    work = record.get("active_work") or {}
    artifact = outcome.get("publication_artifact_id")
    values = {
        "worker_run_id": str(run), "artifact_id": artifact,
        "work": task.get("dedupe_key"), "generation": int(task.get("generation", 0)),
        "revision": task.get("revision"), "patch_digest": outcome.get("patch_digest"),
        "candidate": outcome.get("candidate"), "tree": outcome.get("tree"),
        "expected_head": task.get("expected_head"), "operation": outcome.get("operation_kind"),
        "changed_files": [str(path)[:300] for path in (outcome.get("changed_files") or [])][:200],
        "reason_ids": list(work.get("reason_ids") or [])[:16],
        "feedback_evidence_ids": list(work.get("feedback_evidence_ids") or [])[:64],
        "report_evidence_id": report_evidence_id, "held_at": now,
    }
    problems = []
    if not isinstance(artifact, int) or isinstance(artifact, bool) or artifact <= 0:
        problems.append("no publication artifact")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", values["worker_run_id"]):
        problems.append("no worker run")
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
            "worker_run_id", "artifact_id", "work", "revision", "patch_digest", "candidate",
            "expected_head", "changed_files")},
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


def comment_body(review: dict, head: str) -> str:
    """The comment on the development PR for a patch published after
    successive failed reviews: plain words, the latest review and why it
    failed, its findings as inert text, and the marker of the review."""
    import handoff

    data = review.get("data") or {}
    findings = data.get("findings") or []
    blocking = [f for f in findings if f.get("severity") in BLOCKING]
    others = [f for f in findings if f.get("severity") not in BLOCKING]
    count = int(data.get("failed_in_succession") or 0)
    lines = [
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
    for f in blocking + others:
        where = handoff._code(f"{f['file']}:{f['line']}" if f.get("file") and f.get("line")
                              else f.get("file") or "the whole patch", 300)
        lines.append(f"- **{f['severity']}**, {f['category'].replace('_', ' ')}, {where}: "
                     + handoff._code(f.get("title"), 300))
        lines.extend("  " + line for line in handoff._fenced(str(f.get("detail") or "")))
        if f.get("suggestion"):
            lines.append("  Suggested change:")
            lines.extend("  " + line for line in handoff._fenced(str(f["suggestion"])))
    lines += ["", "The reviewer's summary:", "", *handoff._fenced(str(data.get("summary") or "")),
              "", "The review is recorded with the history of this contribution, so the next "
              "development work reads it. A human can ask for a change here.",
              "", COMMENT_MARKER.format(review["evidence_id"])]
    text, _ = handoff.neutralize_mentions("\n".join(lines))
    return text[:60000]


def _ordinal(n: int) -> str:
    words = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}
    return words.get(n, f"{n}th")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def comment_due(record: dict, outcome: dict) -> str | None:
    """The review evidence id whose comment is due on the development PR, or
    ``None``: only a published patch whose pending publication records a
    failed review it was published after."""
    review = (record.get("pending_publication") or {}).get("patch_review") or {}
    if outcome.get("status") != "published" or not review.get("published_anyway"):
        return None
    return review.get("evidence_id")


def comment(api, record: dict, outcome: dict, *, root: Path | None = None) -> dict:
    """Post the comment of a patch published after successive failed
    reviews on the development PR, once: a comment that carries the marker
    of the review is found instead of posted again."""
    eid = comment_due(record, outcome)
    if eid is None:
        return {"status": "not_due"}
    fork = record.get("fork") or {}
    repository, number = fork.get("name"), fork.get("pr_number")
    if not repository or not number:
        return {"status": "skipped", "reason": "no development PR is recorded"}
    path = (root or repository_root()) / STATE_ROOT / "evidence" / f"{eid}.json"
    review = json.loads(path.read_text(encoding="utf-8"))
    if review.get("kind") != STATUS or review.get("evidence_id") != eid:
        raise ValueError(f"{eid} is not a patch review")
    head = outcome.get("candidate") or ((outcome.get("operation") or {}).get("proposed")
                                        or {}).get("commit")
    if not _SHA.match(str(head or "")):
        raise ValueError("the published head is unknown")
    marker = COMMENT_MARKER.format(eid)
    existing = api.call("GET", f"/repos/{repository}/issues/{int(number)}/comments?per_page=100")
    for item in existing or []:
        if marker in (item.get("body") or ""):
            return {"status": "found", "comment_id": item.get("id"), "evidence_id": eid}
    posted = api.call("POST", f"/repos/{repository}/issues/{int(number)}/comments",
                      {"body": comment_body(review, head)})
    return {"status": "posted", "comment_id": (posted or {}).get("id"), "evidence_id": eid}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m patch_review")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="check a review Result without a token")
    c.add_argument("--task", type=Path, required=True)
    c.add_argument("--result", type=Path, required=True, help="the review proposal.json")
    c.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("comment", help="post the comment of a patch published anyway")
    p.add_argument("--record", type=Path, required=True)
    p.add_argument("--outcome", type=Path, required=True)
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

    record = json.loads(args.record.read_text())
    outcome = json.loads(args.outcome.read_text())
    token = os.environ.pop("UPSTREAMING_FORK_TOKEN", None)
    if comment_due(record, outcome) is None:
        result = {"status": "not_due"}
    elif not token:
        raise SystemExit("UPSTREAMING_FORK_TOKEN is missing")
    else:
        result = comment(review_response.Api(token), record, outcome)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
