# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The handoff operations: building the upstream branch and the submission
package.

`/upstreaming submit-upstream` (``submit_upstream``) authorizes the review of
the exact candidate head and a deterministic preparation. Development
happens on ``riscv64-dev``; nothing here changes it, and the development PR
is never closed. After the submission review Work and its trusted check
(``submission``, ``accept_submission_review``), two states of the lifecycle
table run one remote operation each, through the trusted workflow
``handoff.yml``:

* ``handoff.building_upstream_branch``, operation kind ``upstream_branch``:
  the reviewed series is rebuilt on the frozen base and checked again, and
  ``riscv64`` is created at its head, or replaced under an exact-head lease,
  never while an upstream PR is open from it, and read back.
* ``handoff.preparing_submission``, kind ``submission_comment``: one
  App-authored tracking-issue comment carries the submission package (the
  reviewed title and description, and a prefilled compare link from
  ``riscv64``) and is read back.

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
from common import (
    CONTROL_REPO,
    DEBUG_PREFIX,
    DEV_REF,
    FORK_ORG,
    UPSTREAM_REF,
    contribution_file_id,
    safe_id,
)

# The operation kind each handoff work state runs, and the state of a kind.
KINDS = {
    "handoff.building_upstream_branch": "upstream_branch",
    "handoff.preparing_submission": "submission_comment",
}
STATE_OF = {kind: state for state, kind in KINDS.items()}
# The kinds that write the destination fork.
FORK_KINDS = frozenset({"upstream_branch"})
# The fact a definite failure of each kind establishes for its retry row.
FAILURE_FACTS = {
    "upstream_branch": "cleanup_infrastructure_error",
    "submission_comment": "submission_comment_failed",
}
# The fact the verified receipt and its readback establish.
CONFIRM_FACTS = {
    "upstream_branch": "clean_head_read_back",
    "submission_comment": "submission_package_read_back",
}
# A reviewed series that does not rebuild to the reviewed head, or a lease on
# `riscv64` that conflicts, returns to development (`return_for_repair`); an
# upstream PR open from `riscv64` waits for a human
# (`require_branch_intervention`).
INVALID_FACT = "upstream_branch_invalid"
BLOCKED_FACT = "upstream_branch_blocked"
# Failure classes a receipt may report.
INFRASTRUCTURE = "infrastructure"
INVALID = "invalid"
BLOCKED = "blocked"

APP_LOGIN = "riseproject-dev[bot]"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_OP_ID = re.compile(r"^op-[0-9a-f]{24}$")

# Bounds of the submission package. The title and the description are posted
# in full in the comment, so a link that would be longer than ``MAX_URL``
# carries the title only and the comment says to paste the description.
MAX_TITLE = 120
MAX_BODY = 12000
MAX_URL = 8000
MAX_COMMENT = 60000
MAX_COMMITS = 50


class HandoffError(RuntimeError):
    """A definite failure of an attempt: retried within its budget."""


class HandoffInvalid(HandoffError):
    """The reviewed series does not rebuild to the reviewed head, or the lease
    on `riscv64` conflicts: the contribution returns to development."""


class HandoffBlocked(HandoffError):
    """`riscv64` may not be written: a pull request is open from it (upstream
    or in the fork), `riscv64-dev` is missing, or the record does not develop
    on `riscv64-dev`. It is not written, and a human decides."""


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


def review_evidence(record: dict, evidence: dict) -> dict:
    """The accepted submission review the record names, checked: the review
    of the approved head on the frozen base that passed the trusted check."""
    eid = record.get("submission_review_id")
    review = evidence.get(eid or "")
    data = (review or {}).get("data") or {}
    approval = record.get("handoff_approval") or {}
    if not review or review.get("kind") != "submission_review" \
            or review.get("contribution_id") != record["contribution_id"] \
            or data.get("verdict") != "pass" or data.get("approved_head") != approval.get("head") \
            or data.get("base_sha") != record["upstream"].get("base_sha") \
            or not _SHA.match(str(data.get("head") or "")) \
            or not _SHA.match(str(data.get("clean_tree") or "")) \
            or not re.fullmatch(r"[0-9a-f]{64}", str(data.get("series_digest") or "")) \
            or not isinstance(data.get("commits"), list) or not data["commits"] \
            or len(data["commits"]) > MAX_COMMITS \
            or not re.fullmatch(r"[1-9][0-9]{0,19}", str((data.get("artifact") or {}).get("run_id"))) \
            or not isinstance((data.get("artifact") or {}).get("artifact_id"), int) \
            or data["artifact"]["artifact_id"] <= 0:
        raise lifecycle.TransitionError(f"no accepted submission review {eid} of the approved head")
    return review


# The worker's publication artifact, which holds the rewritten series, is
# kept this long unless the repository variable
# GH_AW_DEFAULT_ARTIFACT_RETENTION_DAYS, which the generated worker lock
# uses instead, sets another number of days; the controller's workflow hands
# that variable to the tick as RETENTION_ENV. A rewrite is treated as
# expired a day before (half the retention, for a retention of one day).
ARTIFACT_RETENTION_DAYS = 30
EXPIRY_MARGIN_DAYS = 1
RETENTION_ENV = "UPSTREAMING_ARTIFACT_RETENTION_DAYS"


def artifact_retention_days() -> int:
    """The days the publication artifact is kept: the value the workflow
    hands over from the repository variable when it is a number of days
    GitHub accepts (1 to 400), else the default of 30 days (an unset
    variable, or 0, which keeps an artifact for the repository's default)."""
    value = str(os.environ.get(RETENTION_ENV) or "").strip()
    if re.fullmatch(r"[0-9]{1,3}", value) and 1 <= int(value) <= 400:
        return int(value)
    return ARTIFACT_RETENTION_DAYS


def rewrite_expired(record: dict, evidence: dict, now, *, review: bool = False) -> bool:
    """Whether the publication artifact of the record's accepted rewrite has
    expired or expires within a day (``expire_submission_rewrite``), for the
    effective retention (``artifact_retention_days``). With
    ``review``, the rewrite is the one the accepted review passed."""
    from datetime import datetime, timedelta

    if review:
        accepted = evidence.get(record.get("submission_review_id") or "") or {}
        eid = ((accepted.get("data") or {}).get("rewrite_evidence_id")
               or record.get("submission_rewrite_id"))
        rewrite = evidence.get(eid or "") or accepted
    else:
        rewrite = evidence.get(record.get("submission_rewrite_id") or "") or {}
    collected = rewrite.get("collected_at")
    if not collected:
        return False
    now = now if isinstance(now, datetime) else datetime.fromisoformat(str(now))
    age = now - datetime.fromisoformat(collected)
    retention = timedelta(days=artifact_retention_days())
    return age >= retention - min(timedelta(days=EXPIRY_MARGIN_DAYS), retention / 2)


def plan_attempt(record: dict, operations: dict, *, policy: dict, now: str,
                 kind: str | None = None, evidence: dict | None = None) -> dict:
    """``record_intent`` of the next attempt of the operation the record's
    handoff state runs (or of ``kind``). ``evidence`` holds the accepted
    submission review that the build of `riscv64` writes."""
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
    if kind == "upstream_branch":
        review = review_evidence(record, evidence or {})
        data = review["data"]
        # The candidate head the review read is the approved head of
        # `riscv64-dev`; the operation writes `riscv64` only.
        target = {"repository": fork["name"], "repository_id": fork["repository_id"],
                  "ref": UPSTREAM_REF, "expected_head": approval["head"], "base_sha": base_sha}
        proposed = {"commit": data["head"], "tree": data["clean_tree"],
                    "content_digest": data["series_digest"],
                    "marker": f"upstream-branch:{record['contribution_id']}:r{approval['revision']}"}
        reason_ids.append(f"review:{review['evidence_id']}")
        external_ids = {"publication_run_id": str((data.get("artifact") or {}).get("run_id") or ""),
                        "publication_artifact_id": int((data.get("artifact") or {})
                                                       .get("artifact_id") or 0)}
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
    evidence = [i for i in (record.get("last_ci_evidence_id"), record.get("plan_evidence_id")) if i]
    return {"command_id": command_id, "actor": {"id": actor["id"], "login": actor["login"]},
            "head": head, "revision": record["revision"], "evidence_ids": evidence}


# --------------------------------------------------------------------------
# The submission package (deterministic, from committed state and the exact
# clean commit series)


def _strip_comments(text: str) -> str:
    """``text`` without HTML comment openers and closers, removed until none
    is left, so no removal joins the rest into a new one (``<!<!---- ``)."""
    while True:
        out = text.replace("<!--", "").replace("-->", "").replace("--!>", "")
        if out == text:
            return out
        text = out


def _clean_block(text: Any, limit: int) -> str:
    """Untrusted multi-line text: credential patterns removed, no HTML
    comment that could fake a marker, bounded, Unix newlines."""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in value.split("\n"):
        cleaned = isolation.clean_text(line, 1000) if line.strip() else ""
        lines.append(_strip_comments(cleaned))
    out = "\n".join(lines).strip("\n")
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out if len(out) <= limit else out[: limit - 3].rstrip() + "..."


def _line(text: Any, limit: int) -> str:
    return _strip_comments(isolation.clean_text(text or "", limit))


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _code(text: Any, limit: int = 200) -> str:
    """Untrusted text as one inline code span. A backtick cannot close it, so
    nothing in it renders: no mention, no link, no image, no HTML."""
    value = _line(text, limit).replace("`", "'")
    return f"`{value}`" if value.strip() else "`(empty)`"


def _fenced(text: str, info: str = "text") -> list[str]:
    """Untrusted text as a fenced block, fenced by more backticks than it
    contains, so nothing in it renders."""
    fence = _fence(text)
    return [f"{fence}{info}", text, fence]


# A GitHub login: what the package may mention.
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
# The at signs a mention could start with, and the entities that produce one.
_AT_SIGNS = "@\uff20\ufe6b"
_AT_ENTITY = re.compile(r"&(?:#0*(64|65312|65131)(?![0-9])|#[xX]0*(40|[fF][fF]20|[fF][eE]6[bB])(?![0-9A-Fa-f])"
                        r"|(commat)(?![A-Za-z0-9]));?")
# An at sign followed by a login or team character and not preceded by an
# ASCII letter or digit (an e-mail address keeps its at sign).
_AT_MENTION = re.compile(rf"(?<![A-Za-z0-9])([{_AT_SIGNS}])(?=[^\s\u2060])")
# The invisible character that keeps an at sign from starting a mention.
WORD_JOINER = "\u2060"


def _at_entity(match: re.Match) -> str:
    if match.group(3):
        return "@"
    value = int(match.group(1)) if match.group(1) else int(match.group(2), 16)
    return chr(value)


def neutralize_mentions(text: str) -> tuple[str, int]:
    """``text`` in which no at sign can start a mention, whatever the
    Markdown around it, so the upstream pull request that a human opens from
    it notifies nobody; and the number of changes.

    An HTML entity of an at sign is decoded first; then every at sign that is
    followed by any character other than white space or the word joiner (a
    character reference such as ``&#117;`` can decode to a login character)
    and not preceded by an ASCII letter
    or digit gets a word joiner (U+2060) after it. Code spans, fences,
    emphasis, and escapes are not parsed, so none of them can hide one."""
    decoded, entities = _AT_ENTITY.subn(_at_entity, text)
    out, breaks = _AT_MENTION.subn(lambda m: m.group(1) + WORD_JOINER, decoded)
    return out, entities + breaks


def _ci_jobs(ci: dict) -> tuple[list[str], list[str]]:
    """The distinct names of the executed, passed jobs of the evidence, and
    those of them that ran on riscv64."""
    names, riscv = [], []
    for job in (ci.get("ci") or {}).get("jobs") or []:
        if not job.get("executed") or job.get("conclusion") != "success":
            continue
        name = _line(job.get("name"), 200).replace("`", "'")
        if name and name not in names:
            names.append(name)
            labels = " ".join(job.get("runner_labels") or [])
            if "riscv" in labels or "riscv64" in name:
                riscv.append(name)
    return names, riscv


def compare_url(record: dict, *, title: str | None = None, body: str | None = None) -> str:
    fork_owner = record["fork"]["name"].split("/", 1)[0]
    base = urllib.parse.quote(record["upstream"]["base_ref"], safe="/")
    url = (f"https://github.com/{record['upstream']['full_name']}/compare/{base}..."
           f"{fork_owner}:{urllib.parse.quote(UPSTREAM_REF, safe='/')}")
    if title is None:
        return url
    query = "quick_pull=1&title=" + urllib.parse.quote(title, safe="")
    if body is not None:
        query += "&body=" + urllib.parse.quote(body, safe="")
    return f"{url}?{query}"


_FILES_LINE = "\0changed files\0"


def _files_line(files: list[str], shown: int) -> str:
    listed = ", ".join(_code(f) for f in files[:shown])
    rest = len(files) - shown
    if rest:
        noun = "file" if rest == 1 else "files"
        listed = f"{listed}, and {rest} more {noun}" if shown else f"{rest} {noun}"
    return f"- Changed files: {listed or 'none'}"


def _fit_files(template: str, files: list[str]) -> str:
    """The comment with its changed-file list, cut with the count of the
    files it leaves out when the whole list would not fit ``MAX_COMMENT``."""
    room = MAX_COMMENT - (len(template) - len(_FILES_LINE))
    shown = len(files)
    if len(_files_line(files, shown)) > room:
        low, high = 0, shown - 1  # the longest list that fits is in [low, high]
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if len(_files_line(files, middle)) <= room else (low, middle - 1)
        shown = low
    comment = template.replace(_FILES_LINE, _files_line(files, shown), 1)
    if len(comment) > MAX_COMMENT:
        raise HandoffError(f"the submission package has {len(comment)} characters")
    return comment


def build_package(record: dict, *, plan: dict, ci: dict, review: dict, commits: list[dict],
                  files: list[str], operation_id: str, retract_revision: int | None = None) -> dict:
    """The submission package for the record's clean head of `riscv64`.

    ``review`` is the accepted submission review: its title and description
    were written after the project's pull request template and contributing
    instructions and passed the trusted check. ``commits`` are the commits
    of ``base..clean head`` (``sha``, ``message``), oldest first, which must be
    the reviewed series; ``files`` the changed paths; ``operation_id`` the
    submission package operation. Everything is derived from them, the
    verified plan, the CI evidence of the clean head, and the record, so
    the same inputs give the same bytes.

    Every untrusted value (the plan's rules and paths, file names, commit
    messages, CI job names, the reviewed title and description) is in a code
    span or a fenced block of the comment, so none of it renders as a
    mention, a link, an image, or HTML; the only mention is the human who
    requested the submission. In the proposed title and description no at
    sign can start a mention (``neutralize_mentions``), so the upstream pull
    request notifies nobody. A changed-file list too long for the comment is
    cut with the count of the files it leaves out."""
    head = record["clean_head_sha"]
    if not commits or len(commits) > MAX_COMMITS:
        raise HandoffError(f"the clean series has {len(commits)} commits")
    if not _OP_ID.fullmatch(str(operation_id)):
        raise HandoffError("the package names no operation")
    data = (review or {}).get("data") or {}
    reviewed = [str(c.get("message") or "").rstrip("\n") for c in data.get("commits") or []]
    for commit in commits:
        if str(commit.get("message") or "").startswith(DEBUG_PREFIX):
            raise HandoffError(f"the clean series has a debug commit {commit.get('sha')}")
    if [str(c.get("message") or "").rstrip("\n") for c in commits] != reviewed:
        raise HandoffError("the clean series is not the reviewed series")
    title = _line(data.get("title"), MAX_TITLE) or "Add riscv64 support"
    title, title_changes = neutralize_mentions(title)
    body = _clean_block(data.get("body"), MAX_BODY)
    body, body_changes = neutralize_mentions(body)
    link = compare_url(record, title=title, body=body)
    body_in_link = len(link) <= MAX_URL
    if not body_in_link:
        link = compare_url(record, title=title)
    names, riscv = _ci_jobs(ci)
    fork = record["fork"]["name"]
    ci_head = (ci.get("ci") or {}).get("head_sha") or head
    testing = (f"{len(names)} required job{'s' if len(names) != 1 else ''} executed and passed"
               + (", including " + ", ".join(f"`{n}`" for n in riscv) if riscv else "")
               + (f" on `{ci_head}`" + (", the candidate head with the same tree"
                                         if ci_head != head else "")))
    approval = record["handoff_approval"]
    revision = retract_revision if retract_revision is not None else record["revision"]
    retract = f"/upstreaming retract-pr revision={revision} head={head}"
    upstream, base = record["upstream"]["full_name"], record["upstream"]["base_ref"]
    proposal = (plan.get("data") or {}).get("proposal") or {}
    rules = _line(proposal.get("contribution_rules"), 600)
    unchanged = [p for p in proposal.get("allowed_paths") or []
                 if not any(ch in str(p) for ch in "*?[") and p not in files][:10]
    login = str(approval["actor"]["login"])
    requester = f"@{login}" if _LOGIN.fullmatch(login) else _code(login, 40)
    dev = record["fork"].get("pr_number")
    lines = [
        f"**Submission package** for revision {approval['revision']} and the exact clean head "
        f"`{head}`, requested by {requester}.",
        "",
        "An authorized human opens the upstream pull request from this package under their own "
        "GitHub account. No App opens it and the pipeline holds no credential that can write "
        "upstream; it verifies the pull request by observation once it is open.",
        "",
        f"- Upstream repository: {_code(upstream)} (id {_code(record['upstream'].get('repository_id'), 20)}), "
        f"base {_code(base)}",
        f"- Source: {_code(fork)} (id {_code(record['fork'].get('repository_id'), 20)}), "
        f"branch `{UPSTREAM_REF}`, built from the reviewed series of the approved head "
        f"`{approval['head']}` of `riscv64-dev`"
        + (f" (development PR #{int(dev)}, which stays open)" if isinstance(dev, int) else ""),
        f"- Exact head: `{head}`",
        _FILES_LINE,
        f"- CI: {testing}. Checks: https://github.com/{fork}/commit/{ci_head}",
        f"- Package operation: {_code(operation_id, 40)}; approval: "
        f"{_code(approval['command_id'], 120)}; plan evidence: "
        f"{_code(record.get('plan_evidence_id'), 120)}; review evidence: "
        f"{_code(record.get('submission_review_id'), 120)}",
        "",
        "> [!WARNING]",
        f"> The compare form resolves the branch `{UPSTREAM_REF}` when it opens. Before you "
        f"submit, check that it shows exactly the head `{head}`. If it shows another head, do not "
        "submit; withdraw this package instead:",
        ">",
        f"> `{retract}`",
        ">",
        f"> This command names revision {revision}, the revision when the package was posted. A "
        "pause and its resume bump the revision: use the revision of the latest entry on this "
        "issue, whose next commands and reminders carry the current one.",
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
    if title_changes or body_changes:
        lines += ["", "Every at sign that could mention someone in the proposed title and "
                      "description has an invisible word joiner (U+2060) after it, so opening the "
                      "pull request notifies nobody upstream. Edit them in the form only if a "
                      "mention is intended."]
    lines += [
        "",
        "**Title**",
        "",
        *_fenced(title),
        "",
        "<details>",
        "<summary>Description</summary>",
        "",
        *_fenced(body, "markdown"),
        "",
        "</details>",
        "",
        "**Contribution policy** (from the verified plan):",
        "",
        *(_fenced(rules) if rules else ["None recorded."]),
        "",
    ]
    lines.append(f"Open the pull request at exactly this head. Do not add commits to "
                 f"`{UPSTREAM_REF}` or edit files in the form. The title and description were "
                 "written after the project's pull request template and contributing "
                 "instructions and checked for the template's required sections; review them "
                 "before you submit. If the contribution needs a change to the series (for "
                 "example a CHANGELOG entry or a commit sign-off the project asks for), do not "
                 "submit: withdraw this package with the `retract-pr` command above, which "
                 "returns the contribution to development for a repair. Only steps outside the "
                 "series (such as signing a CLA) are yours to do before you submit.")
    if unchanged:
        lines += ["", f"The plan admitted {', '.join(_code(p) for p in unchanged)}, which the "
                      "series does not change: if the project expects a change there (for example "
                      "a changelog entry), withdraw this package rather than submit it."]
    lines += [
        "",
        f"If GitHub does not let your account open this pull request (for example the project "
        f"limits who may open one), another human whose account may post `/upstreaming` commands "
        f"here can open it from this package. Do not open it from another fork or branch: only "
        f"{_code(fork)} `{UPSTREAM_REF}` at the exact head is verified. If nobody can, withdraw "
        "the package with the command above.",
        "",
        submission_marker(record),
    ]
    comment = _fit_files("\n".join(lines), files)
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


def pull_mismatch(record: dict, pr: dict, authority, base_ref: str | None = None) -> str | None:
    """Why ``pr`` is not the upstream PR the package asked for, or ``None``.

    It must be open, against the recorded upstream repository and the frozen
    base (or ``base_ref``, the name the upstream renamed it to), from the
    controlled fork ref at the exact clean head, and authored by an
    authorized human (a ``User`` account whose id is in the command
    authority)."""
    upstream, fork = record["upstream"], record.get("fork") or {}
    base = base_ref or upstream.get("base_ref")
    if pr.get("state") != "open" or pr.get("merged"):
        return f"#{pr.get('number')} is not open"
    if pr.get("base_repository_id") != upstream.get("repository_id"):
        return f"#{pr.get('number')} is not against the upstream repository"
    if pr.get("base_ref") != base:
        return f"#{pr.get('number')} targets the base {pr.get('base_ref')!r}, not {base!r}"
    if pr.get("head_repository_id") != fork.get("repository_id"):
        return f"#{pr.get('number')} is not from the contribution fork"
    if pr.get("head_ref") != UPSTREAM_REF:
        return f"#{pr.get('number')} is from the branch {pr.get('head_ref')!r}, not {UPSTREAM_REF!r}"
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


def renamed_base_candidates(record: dict, prs: list[dict], authority) -> list[dict]:
    """The candidates that satisfy every filter but the frozen base: they
    target another branch of the upstream repository. One of them is the
    upstream PR only once GitHub redirects the frozen base to its branch
    (``collect.base_renamed_to``)."""
    base = record["upstream"].get("base_ref")
    return [pr for pr in prs
            if isinstance(pr.get("base_ref"), str) and pr["base_ref"] and pr["base_ref"] != base
            and pull_mismatch(record, pr, authority, base_ref=pr["base_ref"]) is None]


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
        notes.append(f"the branch `{UPSTREAM_REF}` resolves to `{observed_head}`, not the "
                     "requested head")
    notes += rejected[:5]
    if ambiguous > 1:
        notes.append(f"{ambiguous} pull requests match; none is verified")
    return "; ".join(notes) or None


# --------------------------------------------------------------------------
# Verified receipts: the reconciler's independent readback


def _message(commit: dict) -> str:
    return str((commit.get("commit") or {}).get("message") or "")


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


# What a failed attempt records when its launches are spent without a
# receipt: the controller closes it as a failed infrastructure attempt.
LAUNCHES_SPENT = "launches spent without a receipt"


def fail_attempt(record: dict, op: dict, *, policy: dict, now: str, failure: str, reason: str,
                 external_ids: dict | None = None):
    """Close a failed attempt (``begin_effect`` when it is still ``planned``,
    then ``record_failure``) and fire the one row of its state: the retry
    row within the budget, the exhaustion row at its limit, or, for a build of
    `riscv64` that is ``invalid``, ``return_for_repair``, and for one that is
    ``blocked``, ``require_branch_intervention``. Returns the closed operation
    and the outcome of the row. Raises ``lifecycle.TransitionError`` when no
    single row applies."""
    import engine

    kind = op["kind"]
    special = {INVALID: INVALID_FACT, BLOCKED: BLOCKED_FACT}.get(failure) \
        if kind == "upstream_branch" else None
    facts = {special} if special else {FAILURE_FACTS[kind]}
    counts = {} if special else {kind: attempt_count(op)}
    verbs = [verb for verb in recovery.fired_verbs(record, op, "failed")
             if engine.refusal(record, verb, policy=policy, now=now, facts=facts,
                               counts=counts) is None]
    if len(verbs) != 1:
        raise lifecycle.TransitionError(
            f"a failed {kind} attempt fires {verbs or 'no row'} in {engine.state_key(record)}")
    exhausted = verbs[0] in lifecycle.BUDGETS[kind].exhaustion_verbs
    new_op = recovery.fold_failure_receipt(
        op, external_ids=dict(external_ids or {}, failure=str(failure or "unknown")[:40],
                              exhausted=int(exhausted)), now=now)
    fired = recovery.fire(
        record, new_op, policy=policy, now=now, facts=facts, counts=counts,
        details={"attempt": int(reason_value(op, "attempt") or 0), "count": attempt_count(op),
                 "reason": isolation.clean_text(reason or "", 300)})
    if fired is None or not fired.accepted:
        raise lifecycle.TransitionError(
            f"a failed {kind} attempt fires no row in {engine.state_key(record)}: "
            f"{fired.reason if fired else 'none bound'}")
    return new_op, fired


def readback(api: collect.Api, record: dict, op: dict, receipt: dict,
             evidence: dict | None = None) -> tuple[dict, dict, str] | str:
    """Read back what a confirmed receipt reports. Returns the external ids,
    the values of the fields the confirm row sets, and a detail, or the
    reason the readback does not match. ``evidence`` holds the accepted
    submission review whose series `riscv64` must be."""
    evidence = evidence or {}
    kind, target = op["kind"], op["target"]
    run = {"handoff_run_id": str(receipt.get("run_id") or "")}
    if kind == "upstream_branch":
        clean = receipt.get("clean_head")
        proposed = op.get("proposed") or {}
        if not isinstance(clean, str) or not _SHA.match(clean) or clean != proposed.get("commit"):
            return "the receipt names another head than the reviewed one"
        head = collect.branch_head(api, target["repository"], UPSTREAM_REF)
        if not head or head["head"] != clean or head.get("tree") != proposed.get("tree"):
            return f"{UPSTREAM_REF} does not resolve to the reviewed head and tree"
        review = evidence.get(next((r.split(":", 1)[1] for r in op.get("reason_ids") or []
                                    if r.startswith("review:")), ""))
        messages = [str(c.get("message") or "").rstrip("\n")
                    for c in ((review or {}).get("data") or {}).get("commits") or []]
        cmp = collect.compare_commits(api, target["repository"], target["base_sha"], clean)
        series = cmp.get("commits") or []
        if cmp.get("behind_by") != 0 or not series or len(series) > MAX_COMMITS \
                or [_message(c).rstrip("\n") for c in series] != messages:
            return f"{UPSTREAM_REF} is not the reviewed series on the frozen base"
        ci = {"ci_dispatch": receipt.get("ci_dispatch") or NOT_NEEDED}
        if ci["ci_dispatch"] not in (NOT_NEEDED, DISPATCHED, EXISTS, UNSUPPORTED):
            return "the receipt names no outcome of the CI dispatch"
        if ci["ci_dispatch"] != NOT_NEEDED:
            path = str(receipt.get("ci_workflow") or "")
            if path and not _WORKFLOW_PATH.match(path):
                return "the receipt names an invalid CI workflow"
            ci["ci_workflow"] = path
        if ci["ci_dispatch"] in (DISPATCHED, EXISTS):
            # The run is read back by the head it runs on.
            runs = _runs_for(api, target["repository"], ci["ci_workflow"].rsplit("/", 1)[1], clean)
            if not runs:
                return "no CI run on the clean head is read back"
            ci["ci_run_id"] = int(max(r.get("id") or 0 for r in runs))
        return (dict(run, clean_head=clean, **ci), {"clean_head_sha": clean},
                f"{UPSTREAM_REF} at {clean}")
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
    the committed state before any effect. For the build of `riscv64` the
    accepted submission review the operation names is checked too. Raises
    ``HandoffRefused``."""
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
    if kind == "upstream_branch":
        if target.get("expected_head") != approval.get("head"):
            raise HandoffRefused("the build does not start from the approved head")
        try:
            review = json.loads((root / ".state/evidence"
                                 / f"{safe_id(record.get('submission_review_id') or '')}.json")
                                .read_text())
            schemas.validate("evidence", review)
            review_evidence(record, {review["evidence_id"]: review})
        except (OSError, ValueError, schemas.SchemaError, lifecycle.TransitionError) as error:
            raise HandoffRefused(f"no accepted submission review: {error}") from None
        data = review["data"]
        if f"review:{review['evidence_id']}" not in (op.get("reason_ids") or []) \
                or op["proposed"].get("commit") != data["head"] \
                or op["proposed"].get("tree") != data["clean_tree"] \
                or op["proposed"].get("content_digest") != data["series_digest"]:
            raise HandoffRefused("the operation is not bound to the accepted submission review")
    else:
        expected = record.get("clean_head_sha")
        if not expected or target.get("expected_head") != expected:
            raise HandoffRefused("the operation is not bound to the recorded clean head")
    if kind in FORK_KINDS:
        if target.get("repository") != fork["name"] or target.get("repository_id") != fork["repository_id"] \
                or target.get("ref") != UPSTREAM_REF:
            raise HandoffRefused("the operation does not target the upstream branch of the fork")
    else:
        if target.get("repository") != CONTROL_REPO \
                or target.get("ref") != f"issues/{record['tracking_issue']['number']}" \
                or op["proposed"].get("marker") != submission_marker(record):
            raise HandoffRefused("the operation does not target the tracking issue")
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


def upstream_branch_blocked(read_api: collect.Api, record: dict) -> str | None:
    """Why `riscv64` may not be written now (``require_branch_intervention``),
    or ``None``: the record's development branch is not `riscv64-dev`,
    `riscv64-dev` is missing on the fork, or a pull request of the fork is
    open from `riscv64`."""
    fork = record.get("fork") or {}
    if fork.get("head_ref") != DEV_REF:
        return (f"the record's development branch is {fork.get('head_ref')!r}, not {DEV_REF!r}; "
                f"{UPSTREAM_REF} is not written")
    if collect.branch_head(read_api, fork["name"], DEV_REF) is None:
        return f"{DEV_REF} is missing on {fork['name']}; {UPSTREAM_REF} is not written"
    open_prs = collect.fork_pull_requests(read_api, fork["name"], UPSTREAM_REF)
    if open_prs:
        return (f"a pull request of {fork['name']} is open from {UPSTREAM_REF}: "
                + ", ".join(f"#{p.get('number')}" for p in open_prs[:5])
                + f"; {UPSTREAM_REF} is not written while it heads one")
    return None


def check_upstream_series(root: Path, operation_id: str, *, policy: dict, series: bytes,
                          workdir: Path) -> dict:
    """Before any token is minted: rebuild the reviewed series in ``workdir``
    without any credential (``submission.reconstruct`` reads the public fork
    and upstream anonymously), run every check of the submission check
    again, and require the reviewed head, tree, and messages. Returns the
    head and whether the clean head needs its own CI (its tree is not the
    candidate head's), which is when a CI dispatch may be needed. Raises
    ``HandoffInvalid`` for a series that is not the reviewed one and
    ``HandoffError`` when the series cannot be fetched."""
    import fork_publish
    import submission

    record, op = load_request(root, operation_id, policy)
    review = json.loads((root / ".state/evidence" / f"{safe_id(record['submission_review_id'])}"
                                                    ".json").read_text())["data"]
    target, proposed = op["target"], op["proposed"]
    if hashlib.sha256(series).hexdigest() != proposed["content_digest"]:
        raise HandoffInvalid("the reviewed series does not match the accepted review")
    try:
        built = submission.reconstruct(
            workdir, fork=target["repository"], upstream=record["upstream"]["full_name"],
            base=target["base_sha"], approved=target["expected_head"], mbox=series,
            scope_paths=list(record.get("scope_paths") or []), read_token=None)
    except submission.CheckError as error:
        raise HandoffError(f"could not fetch the series: {error}") from None
    except fork_publish.PublishRejected as error:
        raise HandoffInvalid(f"the reviewed series does not rebuild: {error}") from None
    messages = [c["message"] for c in built["commits"]]
    if built["head"] != proposed["commit"] or built["tree"] != proposed["tree"] \
            or built["clean_tree"] != proposed["tree"] or built["problems"] \
            or messages != [str(c.get("message") or "").rstrip("\n")
                            for c in review.get("commits") or []]:
        raise HandoffInvalid("the reviewed series does not rebuild to the reviewed head: "
                             + "; ".join(built["problems"][:3]))
    return {"head": built["head"],
            "needs_ci": review.get("clean_tree") != review.get("candidate_tree")}


def verify_prepared(work: Path, op: dict, series: bytes) -> str:
    """After the token is minted: the series still has the reviewed digest,
    and the repository ``check_upstream_series`` prepared holds the reviewed
    head with the reviewed tree. Nothing is fetched. Returns the head."""
    proposed = op["proposed"]
    if hashlib.sha256(series).hexdigest() != proposed["content_digest"]:
        raise HandoffInvalid("the reviewed series does not match the accepted review")
    head = str(proposed.get("commit") or "")
    found = _git(work, "rev-parse", "--verify", "--quiet", f"{head}^{{commit}}", check=False) \
        if _SHA.match(head) and (work / "HEAD").is_file() and (work / "objects").is_dir() \
        else None
    if found is None or found.returncode or _out(found) != head:
        raise HandoffError("the checked series is not prepared for the push")
    if _out(_git(work, "rev-parse", f"{head}^{{tree}}")) != proposed.get("tree"):
        raise HandoffInvalid("the prepared head does not have the reviewed tree")
    return head


def build_upstream_branch(root: Path, operation_id: str, *, policy: dict, read_api: collect.Api,
                          token: str | None, series: bytes, workdir: Path | None = None,
                          push: Callable[[Path, list[str], list[str]], subprocess.CompletedProcess]
                          | None = None,
                          dispatch: Callable[[str, dict], Any] | None = None,
                          prepared: bool = False) -> dict:
    """``upstream_branch``: create `riscv64` at the head of the reviewed
    series or replace it under an exact-head lease, never while an upstream
    PR is open from it; read it back.

    ``series`` is the reviewed ``patches.mbox`` from the worker run's
    publication artifact; it must be the series the accepted review names.
    The series is rebuilt and checked without any credential
    (``check_upstream_series``): by the workflow's step before the token is
    minted (``prepared``, with that step's ``workdir``), or here first.
    Then only the digest, head, and tree are verified again, and the token
    only pushes: it never fetches. `riscv64-dev` is only read. A branch that
    already resolves to the reviewed head is read back without a write."""
    import fork_publish

    record, op = load_request(root, operation_id, policy)
    review = json.loads((root / ".state/evidence" / f"{safe_id(record['submission_review_id'])}"
                                                    ".json").read_text())["data"]
    fork = op["target"]["repository"]
    if prepared and workdir is None:
        raise HandoffError("the checked series is not prepared for the push")
    tmp = tempfile.TemporaryDirectory(prefix="upstreaming-handoff-") if workdir is None else None
    work = Path(tmp.name) / "repo" if tmp else workdir
    try:
        if not prepared:
            check_upstream_series(root, operation_id, policy=policy, series=series, workdir=work)
        built = {"head": verify_prepared(work, op, series)}
        # The final checks before the effect. `riscv64` is the upstream
        # branch only once development is on `riscv64-dev`: nothing is written
        # while the record still develops on another branch, while
        # `riscv64-dev` is missing, or while `riscv64` heads an open pull
        # request of the fork, whose head a rewrite would change.
        blocked = upstream_branch_blocked(read_api, record)
        if blocked:
            raise HandoffBlocked(blocked)
        # The head `riscv64` resolves to now is the lease; nothing is written
        # while an upstream PR is open from it.
        observed = collect.branch_head(read_api, fork, UPSTREAM_REF)
        observed = observed and observed["head"]
        if observed != built["head"]:
            open_prs = collect.upstream_pull_requests(read_api, record, UPSTREAM_REF)
            if open_prs:
                raise HandoffBlocked(f"an upstream PR is open from {UPSTREAM_REF}: "
                                     + ", ".join(f"#{p.get('number')}" for p in open_prs[:5]))
            if not token:
                raise HandoffError("writing the upstream branch needs the fork token")
            import publisher
            args = publisher.lease_refspec(built["head"], observed, UPSTREAM_REF)
            runner = push or (lambda w, a, auth: _git(w, "push", "--porcelain",
                                                      fork_publish._url(fork), *a, check=False,
                                                      env=auth))
            pushed = runner(work, args, fork_publish._auth_env(token))
            if pushed.returncode:
                err = pushed.stderr.decode()[-300:] + (pushed.stdout.decode()
                                                       if isinstance(pushed.stdout, bytes) else "")
                if "stale info" in err or "fetch first" in err or "non-fast-forward" in err \
                        or "already exists" in err:
                    raise HandoffInvalid(f"the lease on {UPSTREAM_REF} conflicts: {err[-200:]}")
                raise HandoffError(f"the lease write failed: {err[-200:]}")
        after = collect.branch_head(read_api, fork, UPSTREAM_REF)
        if not after or after["head"] != built["head"]:
            raise HandoffError("the ref readback does not show the reviewed head")
        ci = {"ci_dispatch": NOT_NEEDED}
        if review.get("clean_tree") != review.get("candidate_tree"):
            # The clean head has another tree than the candidate head: CI must
            # run on it, and `riscv64` has no pull request to trigger it.
            ci = dispatch_clean_head_ci(read_api, fork=fork, head=built["head"],
                                        workflow_path=_candidate_workflow(root, record),
                                        dispatch=dispatch)
        return {"result": "applied", "clean_head": built["head"], "replaced": observed,
                "written": observed != built["head"], **ci}
    finally:
        if tmp:
            tmp.cleanup()


# What the build of `riscv64` did for CI on its clean head
# (``external_ids.ci_dispatch`` of the confirmed operation): nothing was
# needed (the clean head has the candidate's tree), the project's required
# workflow was dispatched on `riscv64` and its run read back by head, a run of
# it already exists for the head, or the workflow cannot be dispatched.
NOT_NEEDED, DISPATCHED, EXISTS, UNSUPPORTED = "not_needed", "dispatched", "exists", "unsupported"
_WORKFLOW_PATH = re.compile(r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$")
MAX_WORKFLOW_BYTES = 512 * 1024


def _candidate_workflow(root: Path, record: dict) -> str | None:
    """The workflow of the required CI run on the candidate head, from its
    committed evidence."""
    try:
        ci = json.loads((root / ".state/evidence"
                         / f"{safe_id(record.get('last_ci_evidence_id') or '')}.json").read_text())
    except (OSError, ValueError):
        return None
    path = str((ci.get("ci") or {}).get("workflow_path") or "")
    return path if _WORKFLOW_PATH.match(path) else None


def workflow_dispatchable(text: str) -> bool:
    """Whether a workflow file, read as data, can be dispatched with no
    input: its triggers include ``workflow_dispatch`` and every input it
    declares has a default or is optional. Nothing in it is run."""
    import yaml

    if len(text.encode()) > MAX_WORKFLOW_BYTES:
        return False
    try:
        workflow = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    if not isinstance(workflow, dict):
        return False
    on = workflow.get("on", workflow.get(True))
    if on == "workflow_dispatch":
        return True
    if isinstance(on, list):
        return "workflow_dispatch" in on
    if not isinstance(on, dict) or "workflow_dispatch" not in on:
        return False
    inputs = ((on.get("workflow_dispatch") or {}).get("inputs") or {}) \
        if isinstance(on.get("workflow_dispatch") or {}, dict) else {}
    return isinstance(inputs, dict) and all(
        not (isinstance(i, dict) and i.get("required") and i.get("default") is None)
        for i in inputs.values())


def _runs_for(read_api: collect.Api, fork: str, workflow: str, head: str) -> list[dict]:
    query = urllib.parse.urlencode({"branch": UPSTREAM_REF, "head_sha": head, "per_page": 20})
    data = read_api.get(f"/repos/{fork}/actions/workflows/{workflow}/runs?{query}") or {}
    return [r for r in data.get("workflow_runs") or [] if isinstance(r, dict)
            and r.get("head_sha") == head]


def dispatch_clean_head_ci(read_api: collect.Api, *, fork: str, head: str,
                           workflow_path: str | None, dispatch: Callable[[str, dict], Any] | None,
                           wait: Callable[[float], None] | None = None,
                           readbacks: int = 6) -> dict:
    """Run the project's required CI workflow on the clean head of `riscv64`.

    The workflow of the required CI run on the candidate head is read at the
    clean head, as data. When it can be dispatched (``workflow_dispatch``
    with no required input), it is dispatched on ``refs/heads/riscv64`` with
    the CI token (``actions: write`` on the one fork, minted only for an
    attempt whose clean head needs its own CI) unless a run of it already
    exists for the head, and the run is read back by its head SHA. A
    workflow that cannot be dispatched is reported as such: the CI wait then
    waits for a human (``require_clean_head_ci``). A failed dispatch or a run
    that is not read back is an infrastructure failure of the attempt."""
    import base64
    import time

    if not workflow_path:
        return {"ci_dispatch": UNSUPPORTED, "ci_workflow": ""}
    name = workflow_path.rsplit("/", 1)[1]
    try:
        content = read_api.get(f"/repos/{fork}/contents/{urllib.parse.quote(workflow_path)}"
                               f"?ref={head}") or {}
        text = base64.b64decode(content.get("content") or "").decode("utf-8")
    except (collect.CollectError, ValueError, UnicodeDecodeError):
        return {"ci_dispatch": UNSUPPORTED, "ci_workflow": workflow_path}
    if not workflow_dispatchable(text):
        return {"ci_dispatch": UNSUPPORTED, "ci_workflow": workflow_path}
    runs = _runs_for(read_api, fork, name, head)
    if runs:
        return {"ci_dispatch": EXISTS, "ci_workflow": workflow_path,
                "ci_run_id": int(max(r.get("id") or 0 for r in runs))}
    if dispatch is None:
        raise HandoffError("dispatching CI on the clean head needs the CI token")
    try:
        dispatch(f"/repos/{fork}/actions/workflows/{name}/dispatches",
                 {"ref": f"refs/heads/{UPSTREAM_REF}"})
    except Exception as error:  # every failure of the write is a failed attempt
        raise HandoffError(f"the CI dispatch failed: {isolation.clean_text(error, 200)}") from None
    for attempt in range(readbacks):
        (wait or time.sleep)(5 * (attempt + 1))
        runs = _runs_for(read_api, fork, name, head)
        if runs:
            return {"ci_dispatch": DISPATCHED, "ci_workflow": workflow_path,
                    "ci_run_id": int(max(r.get("id") or 0 for r in runs))}
    raise HandoffError(f"no run of {workflow_path} is read back for the clean head")


def clean_head_ci_missing(record: dict, operations: dict, now: str, *, has_runs: bool) -> bool:
    """Whether CI on the clean head waited long enough (``require_clean_head_ci``):
    no run exists and the build reported that the workflow cannot be
    dispatched, or the bounded wait since the clean head was confirmed (or
    since the last resume) has passed."""
    from datetime import datetime, timedelta

    since = record.get("clean_head_ci_since") or record.get("updated_at")
    if datetime.fromisoformat(now) - datetime.fromisoformat(since) \
            >= timedelta(hours=lifecycle.CLEAN_HEAD_CI_WAIT_HOURS):
        return True
    confirmed = [op for op in attempts(record, operations, "upstream_branch")
                 if op.get("status") == "confirmed"]
    status = ((confirmed[-1].get("external_ids") or {}).get("ci_dispatch")
              if confirmed else None)
    return status == UNSUPPORTED and not has_runs


def ci_head(record: dict, review: dict) -> str:
    """The head whose CI is CI on the clean head: the clean head, or the
    approved candidate head when the reviewed series has its tree (the
    approved series had no debug commit), since CI is a function of the
    tree."""
    data = (review or {}).get("data") or {}
    if data.get("clean_tree") and data.get("clean_tree") == data.get("candidate_tree"):
        return data["approved_head"]
    return record["clean_head_sha"]


def package_inputs(root: Path, record: dict, read_api: collect.Api) -> dict:
    """The verified plan, the accepted submission review, the CI evidence
    of the clean head, and the exact clean series of `riscv64`, read from
    committed state and the fork."""
    def evidence(eid: str | None) -> dict:
        if not eid:
            raise HandoffError("the record names no evidence")
        value = json.loads((root / ".state/evidence" / f"{safe_id(eid)}.json").read_text())
        schemas.validate("evidence", value)
        return value

    plan, ci = evidence(record.get("plan_evidence_id")), evidence(record.get("last_ci_evidence_id"))
    review = evidence(record.get("submission_review_id"))
    try:
        review_evidence(record, {review["evidence_id"]: review})
    except lifecycle.TransitionError as error:
        raise HandoffError(str(error)) from None
    head = record["clean_head_sha"]
    if head != review["data"]["head"]:
        raise HandoffError("the clean head is not the reviewed head")
    if (ci.get("ci") or {}).get("head_sha") != ci_head(record, review):
        raise HandoffError("the CI evidence is not for the clean head")
    cmp = collect.compare_commits(read_api, record["fork"]["name"], record["upstream"]["base_sha"], head)
    if cmp.get("behind_by") != 0 or int(cmp.get("total_commits") or 0) > MAX_COMMITS:
        raise HandoffError("the clean series cannot be read in full")
    commits = [{"sha": c["sha"], "message": (c.get("commit") or {}).get("message") or ""}
               for c in cmp.get("commits") or []]
    files = [f["filename"] for f in cmp.get("files") or []]
    return {"plan": plan, "ci": ci, "review": review, "commits": commits, "files": files}


def is_package_of(body: str, record: dict, package: dict) -> bool:
    """Whether a comment found by its marker is a package of the record's
    current approval: the exact bytes of this package, or a package of
    another attempt of the same approval, which ends with the marker and
    names the approval's command. A marker anywhere else (quoted, or in
    another comment) is not a package."""
    if hashlib.sha256(body.encode()).hexdigest() == package["digest"]:
        return True
    lines = body.split("\n")
    approval = _code(record["handoff_approval"]["command_id"], 120)
    # A package posted before the plan rename names its plan the `dossier`.
    named = re.compile(rf"- Package operation: `op-[0-9a-f]{{24}}`; approval: {re.escape(approval)}; "
                       r"(?:plan|dossier) evidence: `[^`\n]*`; review evidence: `[^`\n]*`")
    return lines[-1] == package["marker"] and any(named.fullmatch(line) for line in lines)


def post_package(root: Path, operation_id: str, *, policy: dict, read_api: collect.Api,
                 issue_api) -> dict:
    """``submission_comment``: post, or discover by its marker, the one
    App-authored package comment, and read it back. ``issue_api`` is a
    ``status_ledger.Api`` with the control App's issue token."""
    import status_ledger

    record, op = load_request(root, operation_id, policy)
    package = build_package(record, **package_inputs(root, record, read_api),
                            operation_id=op["operation_id"])
    number = record["tracking_issue"]["number"]
    marker = op["proposed"]["marker"]
    comments = status_ledger.issue_comments(issue_api, number)
    # Only an App-authored package of this approval is discovered; a comment
    # of anyone else, or one that only carries the marker, is not the package.
    existing = [c for c in comments if (c.get("user") or {}).get("login") == APP_LOGIN
                and marker in (c.get("body") or "")
                and is_package_of(c.get("body") or "", record, package)]
    if existing:
        comment = existing[0]
    else:
        # The final check before the effect: the package is posted only while
        # the branch still resolves to the clean head it names. A moved branch
        # is a failed attempt (`retry_submission_comment`, then
        # `exhaust_comment_retries`): the state has no other failure row.
        observed = collect.branch_head(read_api, record["fork"]["name"], UPSTREAM_REF)
        observed = observed and observed["head"]
        if observed != record["clean_head_sha"]:
            raise HandoffError(f"the branch {UPSTREAM_REF} resolves to {observed}, not the clean "
                               "head; the package is not posted")
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


def reviewed_series(root: Path, operation_id: str, series: Path | None,
                    meta: Path | None) -> bytes:
    """The reviewed series the workflow downloaded, checked against the
    operation: it comes from the ``porting-publication`` artifact of the
    worker run the accepted review names, as the artifact's API record
    shows, and it is a bounded regular file."""
    op = json.loads((root / ".state/operations" / f"{operation_id}.json").read_text())
    ids = op.get("external_ids") or {}
    if series is None or meta is None or series.is_symlink() or not series.is_file() \
            or series.stat().st_size > 4 * 1024 * 1024:
        raise HandoffInvalid("the reviewed series is missing")
    record = json.loads(meta.read_text())
    if record.get("id") != ids.get("publication_artifact_id") \
            or record.get("name") != "porting-publication" \
            or str((record.get("workflow_run") or {}).get("id")) != str(ids.get("publication_run_id")):
        raise HandoffInvalid("the reviewed series is not from the accepted review's run")
    if record.get("expired"):
        # Not a verdict on the series: the controller leads an expired
        # rewrite back to a fresh one (`expire_submission_rewrite`).
        raise HandoffError("the artifact of the rewritten series expired")
    data = series.read_bytes()
    if hashlib.sha256(data).hexdigest() != (op.get("proposed") or {}).get("content_digest"):
        raise HandoffInvalid("the reviewed series does not match the accepted review")
    return data


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
                   failure=INVALID if isinstance(error, HandoffInvalid)
                   else BLOCKED if isinstance(error, HandoffBlocked) else INFRASTRUCTURE)
    out["reason"] = isolation.clean_text(error, 300)
    return out


def _dispatcher(token: str | None) -> Callable[[str, dict], Any] | None:
    """The one write of the CI dispatch, with the CI token (``actions:
    write`` on the one fork)."""
    if not token:
        return None
    import provisioning

    api = provisioning.WriteApi(token)

    def dispatch(path: str, body: dict) -> Any:
        if not re.fullmatch(r"/repos/riseproject-upstreaming/[A-Za-z0-9_.-]+/actions/workflows/"
                            r"[A-Za-z0-9_.-]+\.ya?ml/dispatches", path):
            raise HandoffError("unexpected dispatch path")
        return api.write("POST", path, body)
    return dispatch


def _operation_meta(root: Path, operation_id: str) -> dict:
    meta = {"operation_id": operation_id, "run_id": os.environ.get("GITHUB_RUN_ID")}
    try:
        op = json.loads((root / ".state/operations" / f"{operation_id}.json").read_text())
        meta.update(kind=op.get("kind"), contribution_id=op.get("contribution_id"))
    except (OSError, ValueError):
        pass
    return meta


def current_state(root: Path, api) -> str:
    """The commit of the checked-out state, which must be the head of ``main``
    now: a queued or re-run job never acts on state that ``main`` has left.
    Raises ``HandoffRefused``."""
    import publisher

    checked_out = subprocess.run(["git", *publisher.SAFE_GIT_CONFIG, "-C", str(root), "rev-parse",
                                  "HEAD"], capture_output=True, text=True,
                                 env=publisher.safe_env()).stdout.strip()
    try:
        head = ((api.get(f"/repos/{CONTROL_REPO}/git/ref/heads/main") or {}).get("object") or {}).get("sha")
    except Exception as error:  # a read failure refuses; nothing was attempted
        raise HandoffRefused(f"the head of main cannot be read: {isolation.clean_text(error, 200)}") from None
    if not _SHA.match(checked_out or "") or checked_out != head:
        raise HandoffRefused(f"the checked-out state {checked_out[:12] or 'unknown'} is not the head "
                             f"of main {str(head)[:12]}")
    return checked_out


def main(argv: list[str] | None = None) -> int:
    import argparse

    from common import load_policy

    parser = argparse.ArgumentParser(prog="python -m handoff", description=__doc__)
    parser.add_argument("action", choices=("target", "check", "series", "branch", "comment",
                                           "failed"))
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--kind")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--series", type=Path, help="the reviewed patches.mbox (branch)")
    parser.add_argument("--artifact-meta", type=Path,
                        help="the API record of the publication artifact it came from (branch)")
    parser.add_argument("--workdir", type=Path,
                        help="where the series step rebuilds the series and the branch step "
                             "pushes it from (series, branch)")
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != CONTROL_REPO \
            or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise HandoffRefused("handoff operations require the trusted control workflow on main")
    root, policy = Path.cwd(), load_policy()
    if args.action == "target":
        # The kind and the fork name the jobs scope their tokens to, from the
        # state at the head of main.
        current_state(root, collect.Api(token=os.environ.get("GH_TOKEN") or None))
        _record, op = load_request(root, args.operation_id, policy)
        fork = op["target"]["repository"] if op["kind"] in FORK_KINDS else None
        name = fork.split("/", 1)[1] if fork else ""
        if fork and not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise HandoffRefused("invalid fork name")
        artifact = int((op.get("external_ids") or {}).get("publication_artifact_id") or 0) \
            if op["kind"] == "upstream_branch" else 0
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"kind={op['kind']}\nfork_name={name}\nartifact_id={artifact or ''}\n")
        return 0
    meta = _operation_meta(root, args.operation_id)
    if args.action == "check":
        # Before a job mints its token: the state it checked out is the head
        # of main and still runs this operation. A refusal is its receipt,
        # and the job's later steps do not run.
        try:
            current_state(root, collect.Api(token=os.environ.get("GH_TOKEN") or None))
            _record, op = load_request(root, args.operation_id, policy)
            if op["kind"] != args.kind:
                raise HandoffRefused("the operation is not of this job's kind")
            return 0
        except HandoffRefused as error:
            result = receipt(meta, error=error)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, indent=2))
            print(json.dumps({k: result.get(k) for k in ("operation_id", "kind", "result", "reason")}))
            return 1
    if args.action == "series":
        # Before the job mints any token: the downloaded series is the
        # accepted rewrite's, from its run's publication artifact, and has
        # its digest, and it rebuilds anonymously in the work directory to
        # the reviewed head with every check passing. The step names whether
        # the clean head needs its own CI, the only case for the CI token. A
        # refusal is the attempt's receipt.
        try:
            if args.workdir is None:
                raise HandoffError("the series check needs a work directory")
            checked = check_upstream_series(
                root, args.operation_id, policy=policy, workdir=args.workdir,
                series=reviewed_series(root, args.operation_id, args.series, args.artifact_meta))
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
                out.write(f"needs_ci={'true' if checked['needs_ci'] else 'false'}\n")
            return 0
        except Exception as error:  # every refusal is reported by a receipt
            result = receipt(meta, error=error if isinstance(error, HandoffError)
                             else HandoffInvalid(f"the reviewed series cannot be read: {error}")
                             if isinstance(error, (OSError, ValueError))
                             else HandoffError(f"the series check failed: {error}"))
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, indent=2))
            print(json.dumps({k: result.get(k) for k in ("operation_id", "kind", "result",
                                                         "failure", "reason")}))
            return 1
    if args.action == "failed":
        # A step of the job failed before the attempt wrote its receipt.
        if args.out.exists():
            return 0
        result = receipt(meta, error=HandoffError("a step of the handoff job failed"))
    else:
        read_api = collect.Api(token=os.environ.get("GH_TOKEN") or None)
        try:
            if args.action == "branch":
                # The fork token only pushes; the CI token, minted only when
                # the clean head needs its own CI, only dispatches.
                result = build_upstream_branch(
                    root, args.operation_id, policy=policy, read_api=read_api,
                    token=os.environ.get("UPSTREAMING_FORK_TOKEN") or None,
                    dispatch=_dispatcher(os.environ.get("UPSTREAMING_CI_TOKEN") or None),
                    workdir=args.workdir, prepared=True,
                    series=reviewed_series(root, args.operation_id, args.series,
                                           args.artifact_meta))
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
