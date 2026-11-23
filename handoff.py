# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The handoff operations: debug cleanup, closing the development PR, and the
submission package.

`/upstreaming submit-upstream` (``submit_upstream``) authorizes a
deterministic preparation of the exact candidate head. Three states of the
lifecycle table run one remote operation each, through the trusted workflow
``handoff.yml``:

* ``handoff.cleaning_debug``, operation kind ``debug_cleanup``: every
  ``{debug}`` commit is removed from the admitted series, the clean head is
  published under an exact-head lease and read back. With no debug commit the
  lease write is a verified no-op: the branch is read back at the same head.
* ``handoff.closing_internal_pr``, kind ``internal_pr_closure``: the exact
  recorded, marked development PR is closed and read back.
* ``handoff.preparing_submission``, kind ``submission_comment``: one
  App-authored tracking-issue comment carries the submission package (the
  title, the complete description, and a prefilled compare link) and is
  read back.

Each attempt is its own operation of the same intent (the approval), with
its attempt index and its count in the budget of the state in ``reason_ids``
(``attempt:<n>``, ``count:<k>``). The receipt of a failed attempt fires the
retry row within the budget and the exhaustion row beyond it; the resume of
that exhaustion starts the count again at one.

The upstream PR itself is opened by a human under their own identity from
the package; ``matching_upstream_prs`` is the read-only discovery filter that
verifies it. No credential that can write upstream is used anywhere.

Nothing in this module decides a lifecycle transition: the controller plans
and launches the operations, the reconciler folds the verified receipts
through ``recovery.fire``, and the CI and PR observation takes the
observation rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import collect
import isolation
import lifecycle
import recovery
import schemas
from common import CONTROL_REPO, DEBUG_PREFIX, DEV_REF, FORK_ORG, contribution_file_id, safe_id

# The operation kind each handoff work state runs, and the state of a kind.
KINDS = {
    "handoff.cleaning_debug": "debug_cleanup",
    "handoff.closing_internal_pr": "internal_pr_closure",
    "handoff.preparing_submission": "submission_comment",
}
STATE_OF = {kind: state for state, kind in KINDS.items()}
# The kinds that write the destination fork.
FORK_KINDS = frozenset({"debug_cleanup", "internal_pr_closure"})
# The fact a definite failure of each kind establishes for its retry row.
FAILURE_FACTS = {
    "debug_cleanup": "cleanup_infrastructure_error",
    "internal_pr_closure": "internal_pr_closure_failed",
    "submission_comment": "submission_comment_failed",
}
# The fact the verified receipt and its readback establish.
CONFIRM_FACTS = {
    "debug_cleanup": "clean_head_read_back",
    "internal_pr_closure": "internal_pr_closed",
    "submission_comment": "submission_package_read_back",
}
# A cleanup that cannot produce an admissible clean head, or whose lease
# conflicts, returns to development (`return_for_repair`).
INVALID_FACT = "cleanup_invalid"
# Failure classes a receipt may report.
INFRASTRUCTURE = "infrastructure"
INVALID = "invalid"

APP_LOGIN = "riseproject-dev[bot]"
DEVELOPMENT_MARKER = "<!-- upstreaming-development:"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_OP_ID = re.compile(r"^op-[0-9a-f]{24}$")

# Bounds of the submission package. The title and the description are posted
# in full in the comment, so a link that would be longer than ``MAX_URL``
# carries the title only and the comment says to paste the description.
MAX_TITLE = 120
MAX_COMMIT_BODY = 4000
MAX_BODY = 12000
MAX_URL = 8000
MAX_COMMENT = 60000
MAX_COMMITS = 50


class HandoffError(RuntimeError):
    """A definite failure of an attempt: retried within its budget."""


class HandoffInvalid(HandoffError):
    """The cleanup cannot produce an admissible clean head, or the exact-head
    lease conflicts: the contribution returns to development."""


class HandoffRefused(RuntimeError):
    """The final check before the effect refused the operation: nothing was
    attempted and no attempt is charged."""


# --------------------------------------------------------------------------
# Intents and counts


def reason_value(op: dict, name: str) -> str | None:
    for reason in op.get("reason_ids") or []:
        key, _, value = reason.partition(":")
        if key == name:
            return value
    return None


def approval_reason(record: dict) -> str:
    return f"approval:{record['handoff_approval']['command_id']}"


def attempts(record: dict, operations: dict, kind: str) -> list[dict]:
    """The attempts of ``kind`` for the record's current approval, oldest first."""
    approval = record.get("handoff_approval")
    if not approval:
        return []
    reason = approval_reason(record)
    found = [op for op in operations.values()
             if op.get("contribution_id") == record["contribution_id"] and op.get("kind") == kind
             and reason in (op.get("reason_ids") or [])]
    return sorted(found, key=lambda op: int(reason_value(op, "attempt") or 0))


def attempt_count(op: dict) -> int:
    """The count of the budget of the operation's state at this attempt."""
    return int(reason_value(op, "count") or 0)


def counts(record: dict, operations: dict) -> dict[str, int]:
    """The counts of the handoff budgets for the engine: the count of the
    last attempt of each kind for the current approval."""
    out = {}
    for kind in KINDS.values():
        done = attempts(record, operations, kind)
        out[kind] = attempt_count(done[-1]) if done else 0
    return out


def next_count(previous: dict | None) -> int:
    """The count of the next attempt: one after an exhaustion (its resume
    starts the budget again) or as the first attempt, one more otherwise."""
    if previous is None or (previous.get("external_ids") or {}).get("exhausted"):
        return 1
    return attempt_count(previous) + 1


def submission_marker(record: dict) -> str:
    approval = record["handoff_approval"]
    return (f"<!-- upstreaming-submission:{record['contribution_id']}:r{approval['revision']}:"
            f"{record['clean_head_sha']} -->")


def plan_attempt(record: dict, operations: dict, *, policy: dict, now: str,
                 kind: str | None = None) -> dict:
    """``record_intent`` of the next attempt of the operation the record's
    handoff state runs (or of ``kind``, for the approval being recorded)."""
    state = f"{record['phase']}.{record['activity']}"
    kind = kind or KINDS[state]
    approval = record.get("handoff_approval")
    fork = record.get("fork") or {}
    if not approval or not fork.get("name"):
        raise lifecycle.TransitionError(f"{kind} needs the approval and the fork")
    done = attempts(record, operations, kind)
    if done and recovery.is_open(done[-1]):
        raise lifecycle.TransitionError(f"{done[-1]['operation_id']} is still open")
    attempt, count = len(done) + 1, next_count(done[-1] if done else None)
    reason_ids = [approval_reason(record), f"attempt:{attempt}", f"count:{count}"]
    base_sha = record["upstream"]["base_sha"]
    external_ids: dict[str, Any] = {}
    if kind == "debug_cleanup":
        target = {"repository": fork["name"], "repository_id": fork["repository_id"],
                  "ref": DEV_REF, "expected_head": approval["head"], "base_sha": base_sha}
        proposed = {"marker": f"debug-cleanup:{record['contribution_id']}:r{approval['revision']}"}
    elif kind == "internal_pr_closure":
        target = {"repository": fork["name"], "repository_id": fork["repository_id"],
                  "ref": DEV_REF, "expected_head": record["clean_head_sha"], "base_sha": base_sha}
        proposed = {"marker": DEVELOPMENT_MARKER}
        external_ids = {"pr_number": int(fork["pr_number"])}
    else:
        number = record["tracking_issue"]["number"]
        target = {"repository": CONTROL_REPO, "repository_id": None, "ref": f"issues/{number}",
                  "expected_head": record["clean_head_sha"], "base_sha": base_sha}
        proposed = {"marker": submission_marker(record)}
    return recovery.plan(contribution_id=record["contribution_id"], revision=record["revision"],
                         kind=kind, policy_version=policy["policy_version"],
                         reason_ids=reason_ids, target=target, proposed=proposed, now=now,
                         external_ids=external_ids)


def approval(record: dict, *, command_id: str, actor: dict, head: str) -> dict:
    """The ``handoff_approval`` ``submit_upstream`` records: the command, its
    actor, the exact revision and head, and the evidence the approval saw."""
    evidence = [i for i in (record.get("last_ci_evidence_id"), record.get("dossier_evidence_id")) if i]
    return {"command_id": command_id, "actor": {"id": actor["id"], "login": actor["login"]},
            "head": head, "revision": record["revision"], "evidence_ids": evidence}


# --------------------------------------------------------------------------
# The submission package (deterministic, from committed state and the exact
# clean commit series)


def _clean_block(text: Any, limit: int) -> str:
    """Untrusted multi-line text: credential patterns removed, no HTML
    comment that could fake a marker, bounded, Unix newlines."""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in value.split("\n"):
        cleaned = isolation.clean_text(line, 1000) if line.strip() else ""
        lines.append(cleaned.replace("<!--", "").replace("-->", ""))
    out = "\n".join(lines).strip("\n")
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out if len(out) <= limit else out[: limit - 3].rstrip() + "..."


def _line(text: Any, limit: int) -> str:
    return isolation.clean_text(text or "", limit).replace("<!--", "").replace("-->", "")


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _ci_jobs(ci: dict) -> tuple[list[str], list[str]]:
    """The distinct names of the executed, passed jobs of the evidence, and
    those of them that ran on riscv64."""
    names, riscv = [], []
    for job in (ci.get("ci") or {}).get("jobs") or []:
        if not job.get("executed") or job.get("conclusion") != "success":
            continue
        name = _line(job.get("name"), 200)
        if name and name not in names:
            names.append(name)
            labels = " ".join(job.get("runner_labels") or [])
            if "riscv" in labels or "riscv64" in name:
                riscv.append(name)
    return names, riscv


def _related_issues(record: dict, dossier: dict) -> list[str]:
    """Open issues of the upstream repository that the verified dossier
    records in its history."""
    upstream = record["upstream"]["full_name"]
    pattern = re.compile(rf"^https://github\.com/{re.escape(upstream)}/issues/([0-9]+)$")
    found = []
    for item in ((dossier.get("data") or {}).get("history") or []):
        match = pattern.match(str(item.get("url") or ""))
        if match and item.get("outcome") == "open" and f"#{match.group(1)}" not in found:
            found.append(f"#{match.group(1)}")
    return found


def compare_url(record: dict, *, title: str | None = None, body: str | None = None) -> str:
    fork_owner = record["fork"]["name"].split("/", 1)[0]
    base = urllib.parse.quote(record["upstream"]["base_ref"], safe="/")
    url = (f"https://github.com/{record['upstream']['full_name']}/compare/{base}..."
           f"{fork_owner}:{urllib.parse.quote(DEV_REF, safe='/')}")
    if title is None:
        return url
    query = "quick_pull=1&title=" + urllib.parse.quote(title, safe="")
    if body is not None:
        query += "&body=" + urllib.parse.quote(body, safe="")
    return f"{url}?{query}"


def build_package(record: dict, *, dossier: dict, ci: dict, commits: list[dict],
                  files: list[str], retract_revision: int | None = None) -> dict:
    """The submission package for the record's clean head.

    ``commits`` are the commits of ``base..clean head`` (``sha``,
    ``message``), oldest first; ``files`` the changed paths. Everything
    is derived from them, the verified dossier, the CI evidence of the clean
    head, and the record, so the same inputs give the same bytes."""
    head = record["clean_head_sha"]
    if not commits or len(commits) > MAX_COMMITS:
        raise HandoffError(f"the clean series has {len(commits)} commits")
    subjects, bodies = [], []
    for commit in commits:
        subject, _, rest = str(commit.get("message") or "").partition("\n")
        if subject.startswith(DEBUG_PREFIX):
            raise HandoffError(f"the clean series has a debug commit {commit.get('sha')}")
        subjects.append(_line(subject, MAX_TITLE))
        bodies.append(_clean_block(rest, MAX_COMMIT_BODY))
    title = subjects[0] if len(commits) == 1 else "Build riscv64 wheels"
    title = title or "Build riscv64 wheels"
    if len(commits) == 1:
        description = bodies[0]
    else:
        description = "\n\n".join(
            f"### {subject}\n\n{body}".rstrip() for subject, body in zip(subjects, bodies))
    names, riscv = _ci_jobs(ci)
    fork = record["fork"]["name"]
    testing = (f"CI in the fork passed on the exact head `{head}`: {len(names)} "
               f"job{'s' if len(names) != 1 else ''} executed and passed")
    testing += (", including " + ", ".join(f"`{n}`" for n in riscv) + "." if riscv else ".")
    testing += f" Checks: https://github.com/{fork}/commit/{head}"
    sections = [description, "## Testing\n\n" + testing]
    related = _related_issues(record, dossier)
    if related:
        sections.append("Related: " + ", ".join(related))
    body = "\n\n".join(s for s in sections if s).strip()
    if len(body) > MAX_BODY:
        body = body[: MAX_BODY - 3].rstrip() + "..."
    link = compare_url(record, title=title, body=body)
    body_in_link = len(link) <= MAX_URL
    if not body_in_link:
        link = compare_url(record, title=title)
    approval = record["handoff_approval"]
    retract = (f"/upstreaming retract-pr revision={retract_revision if retract_revision is not None else record['revision']} "
               f"head={head}")
    upstream, base = record["upstream"]["full_name"], record["upstream"]["base_ref"]
    proposal = (dossier.get("data") or {}).get("proposal") or {}
    rules = _line(proposal.get("contribution_rules"), 600).rstrip(".")
    unchanged = [_line(p, 200) for p in proposal.get("allowed_paths") or []
                 if not any(ch in str(p) for ch in "*?[") and p not in files][:10]
    fence = _fence(body)
    lines = [
        f"**Submission package** for revision {approval['revision']} and the exact clean head "
        f"`{head}`, requested by @{approval['actor']['login']}.",
        "",
        "An authorized human opens the upstream pull request from this package under their own "
        "GitHub account. No App opens it and the pipeline holds no credential that can write "
        "upstream; it verifies the pull request by observation once it is open.",
        "",
        f"- Upstream repository: `{upstream}`, base `{base}`",
        f"- Source: `{fork}`, branch `{DEV_REF}`",
        f"- Exact head: `{head}`",
        f"- Changed files: {', '.join(f'`{_line(f, 200)}`' for f in files) or 'none'}",
        "",
        "> [!WARNING]",
        f"> The compare form resolves the branch `{DEV_REF}` when it opens. Before you submit, "
        f"check that it shows exactly the head `{head}`. If it shows another head, do not submit; "
        "withdraw this package instead:",
        ">",
        f"> `{retract}`",
        "",
        f"**[Open the prefilled pull request form]({link})**",
        "",
    ]
    if body_in_link:
        lines.append(f"If GitHub refuses the link, open [the compare view]({compare_url(record)}) "
                     "and paste the title and description below.")
    else:
        lines.append("The description is too long for a link: the form opens with the title "
                     f"only. Paste the description below. Compare view: {compare_url(record)}")
    lines += [
        "",
        "**Title**",
        "",
        "```text",
        title,
        "```",
        "",
        "<details>",
        "<summary>Description</summary>",
        "",
        f"{fence}markdown",
        body,
        fence,
        "",
        "</details>",
        "",
        "**Contribution policy** (from the verified dossier): " + (rules or "none recorded") + ".",
        *([f"The dossier admitted {', '.join(f'`{p}`' for p in unchanged)}, which the series "
           "does not change; check whether the project expects a change there (for example a "
           "changelog entry) before you submit."] if unchanged else []),
        "Before you submit, apply what the project asks of a contributor: a sign-off (DCO), a "
        "contributor agreement (CLA), or its pull request template. A prefilled description "
        "replaces the template; copy the template's required parts into it.",
        "",
        submission_marker(record),
    ]
    comment = "\n".join(lines)
    if len(comment) > MAX_COMMENT:
        raise HandoffError(f"the submission package has {len(comment)} characters")
    return {"title": title, "body": body, "link": link, "body_in_link": body_in_link,
            "comment": comment, "marker": submission_marker(record),
            "digest": hashlib.sha256(comment.encode()).hexdigest()}


# --------------------------------------------------------------------------
# Upstream PR discovery (read-only)


def normalize_pull(pr: dict) -> dict:
    """The fields of a GitHub pull request the discovery filter reads."""
    head, base, user = pr.get("head") or {}, pr.get("base") or {}, pr.get("user") or {}
    return {"id": pr.get("id"), "number": pr.get("number"), "url": pr.get("html_url"),
            "state": pr.get("state"), "merged": bool(pr.get("merged_at") or pr.get("merged")),
            "base_repository_id": (base.get("repo") or {}).get("id"), "base_ref": base.get("ref"),
            "head_repository_id": (head.get("repo") or {}).get("id"),
            "head_ref": head.get("ref"), "head_sha": head.get("sha"),
            "user": {"id": user.get("id"), "login": user.get("login"), "type": user.get("type")}}


def pull_mismatch(record: dict, pr: dict, authority) -> str | None:
    """Why ``pr`` is not the upstream PR the package asked for, or ``None``.

    It must be open, against the recorded upstream repository and the frozen
    base, from the controlled fork ref at the exact clean head, and authored
    by an authorized human (a ``User`` account whose id is in the command
    authority)."""
    upstream, fork = record["upstream"], record.get("fork") or {}
    if pr.get("state") != "open" or pr.get("merged"):
        return f"#{pr.get('number')} is not open"
    if pr.get("base_repository_id") != upstream.get("repository_id"):
        return f"#{pr.get('number')} is not against the upstream repository"
    if pr.get("base_ref") != upstream.get("base_ref"):
        return f"#{pr.get('number')} targets the base {pr.get('base_ref')!r}, not {upstream.get('base_ref')!r}"
    if pr.get("head_repository_id") != fork.get("repository_id"):
        return f"#{pr.get('number')} is not from the contribution fork"
    if pr.get("head_ref") != DEV_REF:
        return f"#{pr.get('number')} is from the branch {pr.get('head_ref')!r}, not {DEV_REF!r}"
    if pr.get("head_sha") != record.get("clean_head_sha"):
        return f"#{pr.get('number')} is at the head {pr.get('head_sha')}, not the clean head"
    user = pr.get("user") or {}
    if user.get("type") != "User" or user.get("id") not in set(authority):
        return f"#{pr.get('number')} is not authored by an authorized human"
    return None


def matching_upstream_prs(record: dict, prs: list[dict], authority) -> tuple[list[dict], list[str]]:
    """The candidates that satisfy every filter, and why each other one does not."""
    matching, rejected = [], []
    for pr in prs:
        why = pull_mismatch(record, pr, authority)
        (rejected.append(why) if why else matching.append(pr))
    return matching, rejected


def upstream_pr_value(pr: dict) -> dict:
    """The ``upstream_pr`` that ``verify_upstream_pr`` records."""
    return {"id": int(pr["id"]), "number": int(pr["number"]), "url": str(pr["url"]),
            "head_repository_id": int(pr["head_repository_id"]), "head_ref": pr["head_ref"],
            "head_sha": pr["head_sha"], "base_ref": pr["base_ref"],
            "registered_by": {"id": int(pr["user"]["id"]), "login": str(pr["user"]["login"])}}


def observation_key(observed_head: str | None, record: dict, rejected: list[str],
                    ambiguous: int) -> str | None:
    """A key of what the locked wait observed that does not satisfy
    ``verify_upstream_pr``, or ``None`` when there is nothing to record."""
    parts = []
    if observed_head != record.get("clean_head_sha"):
        parts.append(f"head:{observed_head}")
    parts += [f"pr:{why}" for why in sorted(rejected)]
    if ambiguous > 1:
        parts.append(f"ambiguous:{ambiguous}")
    if not parts:
        return None
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:24]


def mismatch_text(observed_head: str | None, record: dict, rejected: list[str],
                  ambiguous: int) -> str | None:
    """What the reminder states when the observation does not match."""
    notes = []
    if observed_head != record.get("clean_head_sha"):
        notes.append(f"the branch `{DEV_REF}` resolves to `{observed_head}`, not the requested "
                     "head")
    notes += rejected[:5]
    if ambiguous > 1:
        notes.append(f"{ambiguous} pull requests match; none is verified")
    return "; ".join(notes) or None


# --------------------------------------------------------------------------
# Verified receipts: the reconciler's independent readback


def receipt_problem(record: dict, op: dict, receipt: dict) -> str | None:
    """Why a receipt does not belong to the operation the record runs now."""
    state = f"{record['phase']}.{record['activity']}"
    if receipt.get("operation_id") != op["operation_id"] or receipt.get("kind") != op["kind"] \
            or receipt.get("contribution_id") != record["contribution_id"]:
        return "the receipt names another operation"
    if op["kind"] not in STATE_OF or not recovery.is_open(op):
        return "the operation is not an open handoff operation"
    if record.get("active_operation_id") != op["operation_id"] or STATE_OF[op["kind"]] != state:
        return "the record does not run this operation"
    return None


def readback(api: collect.Api, record: dict, op: dict, receipt: dict) -> tuple[dict, dict, str] | str:
    """Read back what a confirmed receipt reports. Returns the external ids,
    the values of the fields the confirm row sets, and a detail, or the
    reason the readback does not match."""
    kind, target = op["kind"], op["target"]
    run = {"handoff_run_id": str(receipt.get("run_id") or "")}
    if kind == "debug_cleanup":
        clean = receipt.get("clean_head")
        if not isinstance(clean, str) or not _SHA.match(clean):
            return "the receipt names no clean head"
        if not receipt.get("rewritten") and clean != target["expected_head"]:
            return "a cleanup without a rewrite must keep the head"
        head = collect.branch_head(api, target["repository"], DEV_REF)
        if not head or head["head"] != clean:
            return "the branch does not resolve to the clean head"
        cmp = collect.compare_commits(api, target["repository"], target["base_sha"], clean)
        series = cmp.get("commits") or []
        if cmp.get("behind_by") != 0 or not series or len(series) > MAX_COMMITS \
                or any(str((c.get("commit") or {}).get("message") or "").startswith(DEBUG_PREFIX)
                       for c in series):
            return "the clean series is not the admitted series without debug commits"
        return (dict(run, clean_head=clean), {"clean_head_sha": clean, "fork.head_sha": clean},
                f"{DEV_REF} at {clean}")
    if kind == "internal_pr_closure":
        number = int((op.get("external_ids") or {}).get("pr_number") or 0)
        pr = api.get(f"/repos/{target['repository']}/pulls/{number}")
        head = pr.get("head") or {}
        if receipt.get("pr_number") != number or pr.get("state") != "closed" or pr.get("merged") \
                or head.get("sha") != target["expected_head"] or head.get("ref") != DEV_REF \
                or (head.get("repo") or {}).get("id") != target["repository_id"] \
                or (pr.get("base") or {}).get("ref") != record["upstream"]["base_ref"] \
                or DEVELOPMENT_MARKER not in (pr.get("body") or ""):
            return "the development PR is not read back closed at the clean head"
        return dict(run, pr_id=int(pr["id"])), {}, f"development PR {number} closed"
    comment_id = receipt.get("comment_id")
    if not isinstance(comment_id, int):
        return "the receipt names no comment"
    comment = api.get(f"/repos/{CONTROL_REPO}/issues/comments/{comment_id}")
    body = comment.get("body") or ""
    if (comment.get("user") or {}).get("login") != APP_LOGIN \
            or op["proposed"]["marker"] not in body \
            or hashlib.sha256(body.encode()).hexdigest() != receipt.get("body_sha256") \
            or not str(comment.get("issue_url") or "").endswith(
                f"/repos/{CONTROL_REPO}/{target['ref']}"):
        return "the package comment is not read back"
    url = str(comment.get("html_url") or "")
    return (dict(run, comment_id=comment_id, comment_url=url[:500]), {},
            f"submission package comment {comment_id}")


# --------------------------------------------------------------------------
# The trusted workflow: validation before any effect, the effects, receipts


def load_request(root: Path, operation_id: str, policy: dict) -> tuple[dict, dict]:
    """The operation and the record, checked against each other and against
    the committed state before any effect. Raises ``HandoffRefused``."""
    if not _OP_ID.fullmatch(operation_id):
        raise HandoffRefused("invalid operation ID")
    try:
        op = json.loads((root / ".state/operations" / f"{operation_id}.json").read_text())
        schemas.validate("operation", op)
        record = json.loads((root / ".state/contributions"
                             / f"{contribution_file_id(op['contribution_id'])}.json").read_text())
        schemas.validate("contribution", record)
    except (OSError, ValueError, schemas.SchemaError) as error:
        raise HandoffRefused(f"the operation or its record cannot be read: {error}") from None
    kind, target = op["kind"], op["target"]
    fork = record.get("fork") or {}
    approval = record.get("handoff_approval") or {}
    state = f"{record['phase']}.{record['activity']}"
    if kind not in STATE_OF or op["status"] != "planned" or STATE_OF[kind] != state \
            or record.get("active_operation_id") != operation_id or record.get("paused"):
        raise HandoffRefused("the operation is not the current authorized handoff operation")
    if policy.get("read_only", True):
        raise HandoffRefused("the policy does not permit this write")
    if not approval.get("head") or not approval.get("command_id"):
        raise HandoffRefused("the submission is not approved")
    if not fork.get("name", "").startswith(FORK_ORG + "/") or not fork.get("repository_id"):
        raise HandoffRefused("no verified fork is recorded")
    if target.get("base_sha") != record["upstream"].get("base_sha"):
        raise HandoffRefused("the base does not match the record")
    if kind == "debug_cleanup":
        expected = approval.get("head")
        if target.get("expected_head") != expected or fork.get("head_sha") != expected:
            raise HandoffRefused("the cleanup does not start from the approved head")
    else:
        expected = record.get("clean_head_sha")
        if not expected or target.get("expected_head") != expected or fork.get("head_sha") != expected:
            raise HandoffRefused("the operation is not bound to the recorded clean head")
    if kind in FORK_KINDS:
        if target.get("repository") != fork["name"] or target.get("repository_id") != fork["repository_id"] \
                or target.get("ref") != DEV_REF:
            raise HandoffRefused("the operation does not target the recorded fork branch")
    else:
        if target.get("repository") != CONTROL_REPO \
                or target.get("ref") != f"issues/{record['tracking_issue']['number']}" \
                or op["proposed"].get("marker") != submission_marker(record):
            raise HandoffRefused("the operation does not target the tracking issue")
    if kind == "internal_pr_closure" and (op.get("external_ids") or {}).get("pr_number") != fork.get("pr_number"):
        raise HandoffRefused("the operation does not name the recorded development PR")
    return record, op


def _git(repo: Path, *args: str, check: bool = True, input: bytes | None = None,
         extra_config: list[str] | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    import fork_publish
    return fork_publish._git(repo, *args, check=check, input=input, extra_config=extra_config, env=env)


def _out(p: subprocess.CompletedProcess) -> str:
    return p.stdout.decode().strip()


def rebuild_without_debug(work: Path, *, base: str, head: str) -> tuple[str, list[str]]:
    """The admitted series ``base..head`` without its ``{debug}`` commits,
    replayed with the App identity and the original dates, so the same
    series always gives the same clean head. Returns the clean head and the
    removed commits. Raises ``HandoffInvalid`` for a conflict, a merge, or
    a series with nothing left."""
    if _git(work, "merge-base", "--is-ancestor", base, head, check=False).returncode:
        raise HandoffInvalid("the head does not descend from the base")
    if _out(_git(work, "rev-list", "--merges", f"{base}..{head}")):
        raise HandoffInvalid("the series has a merge commit")
    series = [s for s in _out(_git(work, "rev-list", "--reverse", f"{base}..{head}")).split() if s]
    if len(series) > MAX_COMMITS:
        raise HandoffInvalid(f"the series has {len(series)} commits")
    removed, parent = [], base
    for sha in series:
        subject = _out(_git(work, "log", "-1", "--format=%s", sha))
        if subject.startswith(DEBUG_PREFIX):
            removed.append(sha)
            continue
        first = _out(_git(work, "rev-parse", f"{sha}^"))
        if first == parent:
            tree = _out(_git(work, "rev-parse", f"{sha}^{{tree}}"))
        else:
            merged = _git(work, "merge-tree", "--write-tree", f"--merge-base={first}", parent, sha,
                          check=False)
            if merged.returncode:
                raise HandoffInvalid(f"{sha[:12]} does not apply without the debug commits")
            tree = merged.stdout.decode().split("\n", 1)[0].strip()
        message = _git(work, "show", "-s", "--format=%B", sha).stdout
        dates = {"GIT_AUTHOR_DATE": _out(_git(work, "show", "-s", "--format=%aI", sha)),
                 "GIT_COMMITTER_DATE": _out(_git(work, "show", "-s", "--format=%cI", sha))}
        parent = _out(_git(work, "commit-tree", tree, "-p", parent, input=message, env=dates))
    if not removed:
        return head, []
    if parent == base:
        raise HandoffInvalid("nothing is left once the debug commits are removed")
    return parent, removed


def cleanup(root: Path, operation_id: str, *, policy: dict, read_api: collect.Api,
            token: str | None, workdir: Path | None = None,
            push: Callable[[Path, list[str], list[str]], subprocess.CompletedProcess] | None = None) -> dict:
    """``debug_cleanup``: rebuild without debug commits, publish under the
    exact-head lease, read back. With no debug commit nothing is pushed and
    the branch is read back at the same head."""
    import fork_publish
    import publisher

    record, op = load_request(root, operation_id, policy)
    target = op["target"]
    fork, expected, base = target["repository"], target["expected_head"], target["base_sha"]
    tmp = tempfile.TemporaryDirectory(prefix="upstreaming-handoff-") if workdir is None else None
    work = Path(tmp.name) / "repo" if tmp else workdir
    try:
        publisher.fresh_workspace(work)
        fetched = _git(work, "fetch", "--quiet", "--no-tags", fork_publish._url(fork), expected,
                       check=False, extra_config=fork_publish._auth_config(token))
        if fetched.returncode:
            raise HandoffError(f"could not fetch {expected[:12]}: {fetched.stderr.decode()[-300:]}")
        clean, removed = rebuild_without_debug(work, base=base, head=expected)
        if removed:
            scope = publisher.Scope(allowed_globs=list(record.get("scope_paths") or []),
                                    upstream_facing=True, history="rewrite")
            report = publisher.validate_update(work, base=base, expected_head=expected,
                                               candidate=clean, scope=scope)
            if not report.ok:
                raise HandoffInvalid("; ".join(report.problems[:5]))
        # The final check before the effect: the branch is still the
        # approved head (or already the clean head a previous attempt wrote).
        observed = collect.branch_head(read_api, fork, DEV_REF)
        observed = observed and observed["head"]
        if observed not in (expected, clean):
            raise HandoffInvalid(f"the branch moved to {observed}; the exact-head lease conflicts")
        if removed and observed == expected:
            if not token:
                raise HandoffError("a rewrite needs the fork token")
            args = publisher.lease_refspec(clean, expected)
            runner = push or (lambda w, a, cfg: _git(w, "push", "--porcelain", fork_publish._url(fork), *a,
                                                     check=False, extra_config=cfg))
            pushed = runner(work, args, fork_publish._auth_config(token))
            if pushed.returncode:
                err = pushed.stderr.decode()[-300:] + (pushed.stdout.decode() if isinstance(pushed.stdout, bytes) else "")
                if "stale info" in err or "fetch first" in err or "non-fast-forward" in err:
                    raise HandoffInvalid(f"the exact-head lease conflicts: {err[-200:]}")
                raise HandoffError(f"the lease write failed: {err[-200:]}")
        after = collect.branch_head(read_api, fork, DEV_REF)
        if not after or after["head"] != clean:
            raise HandoffError("the ref readback does not show the clean head")
        return {"result": "applied", "clean_head": clean, "rewritten": bool(removed),
                "removed_commits": removed}
    finally:
        if tmp:
            tmp.cleanup()


def close_development_pr(root: Path, operation_id: str, *, policy: dict, api) -> dict:
    """``internal_pr_closure``: close the exact recorded, marked development
    PR and read it back. ``api`` is a ``dev_pr.Api`` with the one-fork token."""
    record, op = load_request(root, operation_id, policy)
    target, number = op["target"], int(op["external_ids"]["pr_number"])
    path = f"/repos/{target['repository']}/pulls/{number}"

    def problem(pr: dict) -> str | None:
        head = pr.get("head") or {}
        if pr.get("number") != number or head.get("ref") != DEV_REF \
                or (head.get("repo") or {}).get("id") != target["repository_id"] \
                or (pr.get("base") or {}).get("ref") != record["upstream"]["base_ref"] \
                or DEVELOPMENT_MARKER not in (pr.get("body") or ""):
            return "the PR is not the recorded, marked development PR"
        if head.get("sha") != target["expected_head"]:
            return "the PR head is not the clean head"
        if pr.get("merged"):
            return "the PR is merged"
        return None

    pr = api.get(path)
    why = problem(pr)
    if why:
        raise HandoffError(why)
    if pr.get("state") == "open":
        api.mutate("PATCH", path, {"state": "closed"})
    observed = api.get(path)
    if problem(observed) or observed.get("state") != "closed":
        raise HandoffError("the readback does not show the development PR closed")
    return {"result": "applied", "pr_number": number, "pr_id": int(observed["id"]),
            "head_sha": target["expected_head"]}


def package_inputs(root: Path, record: dict, read_api: collect.Api) -> dict:
    """The verified dossier, the CI evidence of the clean head, and the
    exact clean series, read from committed state and the fork."""
    def evidence(eid: str | None) -> dict:
        if not eid:
            raise HandoffError("the record names no evidence")
        value = json.loads((root / ".state/evidence" / f"{safe_id(eid)}.json").read_text())
        schemas.validate("evidence", value)
        return value

    dossier, ci = evidence(record.get("dossier_evidence_id")), evidence(record.get("last_ci_evidence_id"))
    head = record["clean_head_sha"]
    if (ci.get("ci") or {}).get("head_sha") != head:
        raise HandoffError("the CI evidence is not for the clean head")
    cmp = collect.compare_commits(read_api, record["fork"]["name"], record["upstream"]["base_sha"], head)
    if cmp.get("behind_by") != 0 or int(cmp.get("total_commits") or 0) > MAX_COMMITS:
        raise HandoffError("the clean series cannot be read in full")
    commits = [{"sha": c["sha"], "message": (c.get("commit") or {}).get("message") or ""}
               for c in cmp.get("commits") or []]
    files = [f["filename"] for f in cmp.get("files") or []]
    return {"dossier": dossier, "ci": ci, "commits": commits, "files": files}


def post_package(root: Path, operation_id: str, *, policy: dict, read_api: collect.Api,
                 issue_api) -> dict:
    """``submission_comment``: post, or discover by its marker, the one
    App-authored package comment, and read it back. ``issue_api`` is a
    ``status_ledger.Api`` with the control App's issue token."""
    import status_ledger

    record, op = load_request(root, operation_id, policy)
    package = build_package(record, **package_inputs(root, record, read_api))
    number = record["tracking_issue"]["number"]
    marker = op["proposed"]["marker"]
    comments = status_ledger.issue_comments(issue_api, number)
    existing = [c for c in comments if marker in (c.get("body") or "")]
    if any((c.get("user") or {}).get("login") != APP_LOGIN for c in existing):
        raise HandoffError("a comment with the package marker is not App-authored")
    if existing:
        comment = existing[0]
    else:
        comment = issue_api.request("POST", f"/repos/{CONTROL_REPO}/issues/{number}/comments",
                                    {"body": package["comment"]})
    observed = issue_api.request("GET", f"/repos/{CONTROL_REPO}/issues/comments/{comment['id']}")
    body = observed.get("body") or ""
    if (observed.get("user") or {}).get("login") != APP_LOGIN or marker not in body \
            or (not existing and body != package["comment"]):
        raise HandoffError("the package comment readback does not match")
    return {"result": "applied", "comment_id": int(observed["id"]),
            "comment_url": str(observed.get("html_url") or ""),
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "marker": marker,
            "existing": bool(existing)}


def receipt(op_meta: dict, result: dict | None = None, *, error: BaseException | None = None) -> dict:
    """The receipt the workflow uploads for one attempt."""
    out = dict(op_meta, schema_version=1)
    if error is None:
        out.update(result or {})
        return out
    if isinstance(error, HandoffRefused):
        out.update(result="refused", failure=None)
    else:
        out.update(result="failed",
                   failure=INVALID if isinstance(error, HandoffInvalid) else INFRASTRUCTURE)
    out["reason"] = isolation.clean_text(error, 300)
    return out


def _operation_meta(root: Path, operation_id: str) -> dict:
    meta = {"operation_id": operation_id, "run_id": os.environ.get("GITHUB_RUN_ID")}
    try:
        op = json.loads((root / ".state/operations" / f"{operation_id}.json").read_text())
        meta.update(kind=op.get("kind"), contribution_id=op.get("contribution_id"))
    except (OSError, ValueError):
        pass
    return meta


def main(argv: list[str] | None = None) -> int:
    import argparse

    from common import load_policy

    parser = argparse.ArgumentParser(prog="python -m handoff", description=__doc__)
    parser.add_argument("action", choices=("target", "cleanup", "close", "comment", "failed"))
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != CONTROL_REPO \
            or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise HandoffRefused("handoff operations require the trusted control workflow on main")
    root, policy = Path.cwd(), load_policy()
    if args.action == "target":
        # The kind and the fork name the jobs scope their tokens to.
        _record, op = load_request(root, args.operation_id, policy)
        fork = op["target"]["repository"] if op["kind"] in FORK_KINDS else None
        name = fork.split("/", 1)[1] if fork else ""
        if fork and not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise HandoffRefused("invalid fork name")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"kind={op['kind']}\nfork_name={name}\n")
        return 0
    meta = _operation_meta(root, args.operation_id)
    if args.action == "failed":
        # A step of the job failed before the attempt wrote its receipt.
        if args.out.exists():
            return 0
        result = receipt(meta, error=HandoffError("a step of the handoff job failed"))
    else:
        read_api = collect.Api(token=os.environ.get("GH_TOKEN") or None)
        try:
            if args.action == "cleanup":
                result = cleanup(root, args.operation_id, policy=policy, read_api=read_api,
                                 token=os.environ.get("UPSTREAMING_FORK_TOKEN") or None)
            elif args.action == "close":
                import dev_pr
                result = close_development_pr(root, args.operation_id, policy=policy,
                                              api=dev_pr.Api(token=os.environ["UPSTREAMING_FORK_TOKEN"]))
            else:
                import status_ledger
                result = post_package(root, args.operation_id, policy=policy, read_api=read_api,
                                      issue_api=status_ledger.Api(os.environ["UPSTREAMING_ISSUE_TOKEN"]))
            result = receipt(meta, result)
        except Exception as error:  # every failure is reported by a receipt
            result = receipt(meta, error=error)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result.get(k) for k in ("operation_id", "kind", "result", "failure", "reason")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
