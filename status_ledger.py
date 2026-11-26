# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Append concise App-authored lifecycle entries to tracking issues, for the
events that need a human."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import commands
import engine
import isolation
import lifecycle
import schemas
import state
from common import CONTROL_REPO

# The pending action of a provisioning operation that failed (`controller`).
PROVISIONING_FAILED = "provisioning_operation_failed"
# The pending action of the provisioning wait after exhausted launches.
LAUNCH_EXHAUSTED = "provisioning_launch_exhausted"
_OPERATION_ID = r"op-[0-9a-f]{24}"


class LedgerError(ValueError):
    pass


# --------------------------------------------------------------------------
# Wording
#
# The text of an entry is generated from the lifecycle table: the row the
# event took (its verb, its trigger, the guards that state why, and its
# description), the state it leads to (its description), and the pending
# human action the row records. What the event observed (a head, a link the
# pipeline recorded, an error, a quoted comment) is added as bounded,
# quoted data; untrusted text never renders as Markdown.

# Guards every command or stop row states that say nothing about why the row
# was taken.
GENERIC_GUARDS = frozenset({
    lifecycle.ALWAYS, lifecycle.EXACT_REVISION, lifecycle.ANY_REVISION, lifecycle.NOT_PAUSED,
    lifecycle.QUIESCENT, lifecycle.NOT_STALE, "a reason is given", "no open operation",
    "not terminal", "paused",
})
TRIGGERS = {
    lifecycle.Trigger.COMMAND: "by the command {command}",
    lifecycle.Trigger.OBSERVATION: "by observation",
    lifecycle.Trigger.RESULT: "by the Result of a requested task",
    lifecycle.Trigger.OPERATION: "by a remote operation",
    lifecycle.Trigger.TICK: "by the controller",
}
# Events the pipeline records outside the table, and earlier kinds that stay
# in the event logs.
RECORDED_KINDS = {
    "intake:accepted": "Accepted the labeled request.",
    "intake:label_removed": ("Cancelled intake because the request label was removed before "
                             "task admission."),
    "command:authorize-followup": "Recorded the authorized follow-up of this merged contribution.",
    "development_pr_created": "Created and verified the same-fork development PR.",
    "ci_rerun_recorded": ("Read back the new attempt of the CI runs rerun on the exact head for the "
                          "interrupted jobs."),
    "record_fields_migrated": ("Recorded this contribution in the strict record schema: missing "
                               "fields are added with the values the pipeline read for them, and "
                               "the retired `blocked_reason`, `next_poll_at`, and the fence of the "
                               "CI run a rerun answered are removed. Its state, revision, and "
                               "evidence are unchanged."),
    "record_rejections_refunded": ("Returned the `no_progress` charges of proposals that the "
                                   "worker gate and the publisher rejected for contradicting "
                                   "checks of the changed files, and, when only those charges "
                                   "had parked this contribution, returned it to repair; the automatic "
                                   "refresh those rejections used is returned too."),
    "record_names_migrated": ("Recorded this contribution under the current names (the "
                              "planning phase and its plan, or the requested task). Its state, "
                              "revision, and evidence are unchanged."),
}


def _transition(verb: str, source: str) -> lifecycle.Transition | None:
    """The row ``verb`` takes from ``source``, or ``None``."""
    try:
        return engine.resolve(source, verb)[0]
    except lifecycle.TransitionError:
        return None


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", ":")) else text + "."


def _pending(verb: str, t: lifecycle.Transition, target: str, record: dict) -> str | None:
    """The pending human action the row records: the value the table names,
    else the one the record holds in the state the row led to."""
    for entry in t.effects.sets:
        name, literal, value = entry.partition("=")
        if name == "pending_human_action" and literal:
            return value
    if engine.state_key(record) == target and target in engine.HUMAN_WAITS:
        return (record.get("pending_human_action") or {}).get("kind")
    return None


def table_message(event: dict, record: dict) -> str | None:
    """The wording of an event of the table, generated from the row it took
    and the state it led to; ``None`` for an event outside the table."""
    details = event.get("details") or {}
    # An event written before the plan rename names the historic verb and
    # states (``lifecycle.HISTORIC_NAMES``).
    verb = lifecycle.current_name(details.get("verb") or event["kind"])
    source = lifecycle.current_name(details.get("from_state"))
    target = lifecycle.current_name(details.get("to_state"))
    if source not in lifecycle.LIFECYCLE or target not in lifecycle.LIFECYCLE:
        return None
    t = _transition(verb, source)
    if t is None:
        return None
    reasons = [g for g in t.guard if g not in GENERIC_GUARDS]
    how = TRIGGERS[t.trigger].format(command=f"`{t.command}`" if t.command else "")
    text = f"`{verb}` {how}" + (f": {'; '.join(reasons)}." if reasons else ".")
    if t.description:
        text += " " + _sentence(t.description)
    state = lifecycle.LIFECYCLE[target]
    if target != source:
        text += f" Now `{target}`: {_sentence(state.description)}"
    else:
        text += f" The contribution stays in `{target}`: {_sentence(state.description)}"
    pending = _pending(verb, t, target, record)
    if pending and target in engine.HUMAN_WAITS:
        text += f" Pending human action: `{_quotable(pending, 80)}`."
    if t.charges:
        text += " Charged: " + ", ".join(f"`{c}`" for c in t.charges) + "."
    return text


def submit_command(revision: int, head: str) -> str:
    return f"/upstreaming submit-upstream revision={revision} head={head}"


# The events whose entry asks for upstream submission, bound to the revision
# and head the event recorded: the nomination, a reminder at the gate, and
# the earlier migration into the gate.
SUBMISSION_REQUESTS = ("nominate_for_handoff", "record_reminder", "handoff_wait_migrated")


def _submission_request(event: dict) -> str | None:
    """The `submit-upstream` command of an entry that asks for upstream
    submission, bound to the revision and head the event recorded."""
    details = event.get("details") or {}
    if event["kind"] not in SUBMISSION_REQUESTS:
        return None
    if event["kind"] == "record_reminder" \
            and details.get("from_state") != "handoff.awaiting_submission":
        return None
    bound = details.get("bound_to") or details
    head, revision = bound.get("head"), bound.get("revision")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head) \
            or not isinstance(revision, int):
        return None
    return submit_command(revision, head)


def _quotable(text, limit: int = 300) -> str:
    """Untrusted text as one bounded line that cannot close a quote, open an
    HTML comment, or carry a credential."""
    body = isolation.clean_text(text or "", limit)
    return body.replace("<!--", "").replace("-->", "").replace("`", "'")


def _link(url) -> str | None:
    """A GitHub URL the pipeline recorded, or ``None``."""
    url = str(url or "")
    return url if url.startswith("https://github.com/") and not any(c in url for c in " ()<>[]") \
        else None


def _head(value) -> str:
    return f"`{_quotable(value, 40)}`"


def _development_pr_guidance(details: dict) -> str:
    """What a human does once the launches of a development PR operation are
    spent without a verified receipt. Only trusted values are named: the
    operation id, its kind, and the launch count; no PR body or workflow
    log text."""
    operation = details.get("operation")
    named = isinstance(operation, str) and re.fullmatch(r"op-[0-9a-f]{24}", operation)
    runs = (f"the `Development PR {operation}` runs" if named
            else "the `Development PR` runs of the operation")
    launches, replans = details.get("launches"), details.get("replans")
    spent = (f" after {launches} launches" if isinstance(launches, int)
             and not isinstance(launches, bool) else
             f" after {replans} re-plans at this revision" if isinstance(replans, int)
             and not isinstance(replans, bool) else "")
    return (f"The development PR operation got no verified receipt{spent}. Read {runs} of the "
            "control repository, correct the cause (for example, close a pull request open "
            "from `riscv64-dev` that is not this contribution's), then post "
            "`/upstreaming resume`.")


def _resolve_template(operation: str, disposition: str) -> str:
    return (f"`/upstreaming resolve-operation operation={operation} disposition={disposition} "
            'reason="..."`')


def _provisioning_launch_guidance(details: dict) -> str:
    """What a human does once the launches of the planned provisioning
    operation are spent without a receipt: the operation stays open, and it
    fences `park` and the stale resume until it is resolved. Once no open
    operation is named (a resolution or a receipt closed it, or the re-plans
    of the intent are spent), `park` applies. Only trusted values are named:
    the operation id, the launch or re-plan count, and the stale limit; no
    workflow log text."""
    operation = details.get("operation")
    named = isinstance(operation, str) and re.fullmatch(_OPERATION_ID, operation)
    runs = (f"the `Fork Provisioning {operation}` runs" if named
            else "the `Fork Provisioning` runs of the operation")
    launches, replans = details.get("launches"), details.get("replans")
    spent = (f" after {launches} launches" if isinstance(launches, int)
             and not isinstance(launches, bool) else
             f" after {replans} re-plans at this revision" if isinstance(replans, int)
             and not isinstance(replans, bool) else "")
    days = details.get("stale_after_days")
    limit = (f"{days} days" if isinstance(days, int) and not isinstance(days, bool)
             else "`refresh.stale_after_days` days")
    text = (f"The provisioning operation got no receipt{spent}. Read {runs} of the control "
            "repository and correct the cause, then post `/upstreaming resume`.")
    if named:
        text += (f" To park, or to resume after more than {limit}, first close the operation: "
                 f"{_resolve_template(operation, 'not-launched')} when no run did anything, "
                 "`disposition=failed` when a run failed.")
    else:
        # No open operation fences the wait: `park` applies, and `resume`
        # also once the wait is stale.
        text += " To stop instead, post `/upstreaming park`."
    return text


# What a human corrects for each closed failure code a provisioning receipt
# reports: fixed sentences, never text from the receipt.
PROVISIONING_FAILURE_GUIDANCE = {
    "source_changed": "The upstream repository or its base commit no longer has the identity "
                      "the plan approved.",
    "fork_mismatch": "The fork under `riseproject-upstreaming` is not the approved upstream's fork, "
                     "changed identity, or disappeared after it was recorded.",
    "branch_conflict": "The fork's `riscv64-dev` branch holds work the pipeline did not record, or "
                       "is gone; the pipeline never resets it.",
    "settings": "The fork's Actions settings could not be set to read-only CI, or its workflows "
                "stay unregistered.",
    "workflows": "The fork's inherited workflows could not be inventoried or read within the "
                 "bounded setup scan.",
    "write_refused": "GitHub refused a write of the provisioning App to the fork.",
    "other": "The provisioning workflow failed definitely for another reason.",
}


def _provisioning_failure_guidance(details: dict) -> str:
    """What a human does once the provisioning workflow reported a definite
    failure. Only trusted values are named: the operation id and the closed
    failure code; never the receipt's reason or workflow log text."""
    operation = details.get("operation")
    named = isinstance(operation, str) and re.fullmatch(r"op-[0-9a-f]{24}", operation)
    run = (f"the `Fork Provisioning {operation}` run" if named
           else "the `Fork Provisioning` run of the operation")
    failure = details.get("failure")
    cause = PROVISIONING_FAILURE_GUIDANCE.get(failure if isinstance(failure, str) else "",
                                              PROVISIONING_FAILURE_GUIDANCE["other"])
    return (f"{cause} Read {run} of the control repository, correct the cause, then post "
            "`/upstreaming resume`, or `/upstreaming park`.")


def _observed(event: dict) -> str:
    """What the event observed, as bounded data: heads, links the pipeline
    recorded, the upstream pull request, a mismatch, release data."""
    details = event.get("details") or {}
    kind = lifecycle.current_name(event["kind"])
    state = lifecycle.current_name(details.get("from_state"))
    parts = []
    if kind == "confirm_clean_head":
        how = ("it already resolved to that head; nothing was written"
               if not details.get("written") else
               f"replaced {_head(details.get('replaced'))} under the exact-head lease"
               if details.get("replaced") else "created")
        parts.append(f"Clean head of `riscv64`: {_head(details.get('head'))} ({how}).")
    if kind == "accept_clean_head_ci":
        clean = details.get("clean_head") or details.get("head")
        parts.append(f"Clean head: {_head(clean)}; CI of {_head(details.get('head'))}.")
    if kind == "require_head_ci":
        problems = "; ".join(f"`{_quotable(p, 200)}`" for p in (details.get("problems") or [])[:5]
                             if isinstance(p, str))
        parts.append(f"CI on {_head(details.get('head'))} is complete and establishes no "
                     f"verdict: {problems or 'no problem recorded'}. Correct the cause (the "
                     "recorded contract, the workflow, or the CI run), then post "
                     "`/upstreaming resume`, or `/upstreaming park`.")
    if kind == "confirm_submission_package":
        link = _link(details.get("package_url"))
        package = f"[submission package]({link})" if link else "submission package"
        parts.append(f"Posted the {package} for the clean head {_head(details.get('head'))}.")
    if kind == "verify_upstream_pr":
        link = _link(details.get("url"))
        number = _quotable(details.get("pr_number"), 12)
        pr = f"[#{number}]({link})" if link else f"#{number}"
        parts.append(f"Upstream pull request {pr} by {_login(details.get('author'))} at "
                     f"{_head(details.get('head'))}.")
    if kind == "record_pr_mismatch" and details.get("invariant") == "dev_pr":
        parts.append("CI is awaited only on a development PR, and this contribution waited for "
                     "CI without one. Post `/upstreaming resume` to open the development PR "
                     "of the recorded head; CI is then awaited on it.")
    if kind == "resume_pause" and details.get("automatic"):
        parts.append("The controller resumed its own stop after a processing error, since the "
                     "pipeline code changed. A new error pauses the contribution again until "
                     "the next change; `/upstreaming pause` keeps it stopped.")
    if kind == "confirm_development_pr" and details.get("pr_number") is not None:
        parts.append(f"Development PR #{_quotable(details.get('pr_number'), 12)}.")
    if kind == "exhaust_development_pr_launches" or (
            kind == "record_reminder"
            and details.get("pending_human_action") in (lifecycle.DEVELOPMENT_PR_LAUNCH_EXHAUSTED,
                                                        lifecycle.DEVELOPMENT_PR_NOT_OPENED)):
        parts.append(_development_pr_guidance(details))
    if (kind == "require_provisioning_intervention"
            and ("launches" in details or "replans" in details)
            and not details.get("failure")) or (
            kind == "record_reminder" and details.get("pending_human_action") == LAUNCH_EXHAUSTED):
        parts.append(_provisioning_launch_guidance(details))
    if (kind == "require_provisioning_intervention" and details.get("failure")) or (
            kind == "record_reminder"
            and details.get("pending_human_action") == PROVISIONING_FAILED):
        parts.append(_provisioning_failure_guidance(details))
    if kind == "record_reminder" and state == "handoff.waiting_upstream_pr":
        bound = details.get("bound_to") or {}
        link = _link(details.get("package_url"))
        package = f"[submission package]({link})" if link else "submission package"
        who = f" Submitter: {_login(details['submitter'])}." if details.get("submitter") else ""
        parts.append(f"The {package} names the clean head {_head(bound.get('head'))}.{who}")
    if kind == "record_reminder" and details.get("requested_at"):
        parts.append(f"Requested at {_quotable(details['requested_at'], 40)}.")
    if kind in SUBMISSION_REQUESTS and _submission_request(event):
        bound = details.get("bound_to") or details
        parts.append(f"Candidate head: {_head(bound.get('head'))}.")
    if details.get("history") == "never recorded":
        parts.append("The history or the task file of this task was never recorded, and the "
                     "dispatcher gives no worker task without them, so no worker ever "
                     "received it.")
    for key in ("mismatch", "incomplete", "why"):
        if details.get(key):
            parts.append(f"Observed: `{_quotable(details[key], 600)}`.")
    if kind == "verify_release" and isinstance(details.get("releases"), dict):
        parts.append("Releases: " + ", ".join(
            f"`{_quotable(n, 80)} {_quotable(v, 40)}`"
            for n, v in sorted(details["releases"].items())) + ".")
    if kind == "close_planning" and isinstance(details.get("pypi"), dict):
        parts.append("PyPI: " + "; ".join(
            _close_finding(name, entry) for name, entry in sorted(details["pypi"].items())
            if isinstance(entry, dict)) + ".")
    if kind == "return_for_feedback" \
            and isinstance(details.get("series_requirements_unmet"), list):
        # Ids only: they match a fixed pattern; the descriptions are planner
        # text and are not rendered.
        ids = [i for i in details["series_requirements_unmet"][:6]
               if isinstance(i, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", i)]
        if ids:
            parts.append(f"Unmet series requirements of the plan at {_head(details.get('head'))}: "
                         + ", ".join(f"`{i}`" for i in ids) + ".")
    if kind == "nominate_for_handoff" and details.get("series_requirements"):
        parts.append(f"Series requirements: {_quotable(details['series_requirements'], 100)}.")
    if kind == "record_merge" and details.get("merge_sha"):
        parts.append(f"Merge commit: {_head(details['merge_sha'])}.")
    override = details.get("admission_override")
    if override:
        parts.append(f"Admission override: {_override_text(override)}.")
    if details.get("admission_override_not_applied"):
        parts.append("The recorded admission override was not applied.")
    return "".join(" " + p for p in parts)


# The rows of an upstream that could not be read, of a base branch the
# record followed after the upstream renamed it (while the upstream PR is
# open, or before it was opened, when `verify_upstream_pr` names the rename),
# and of a wait for the upstream ending once it is read again. Their
# entries, and the reminders of their waits, are written in plain words
# (`plain_message`): they name no verb, budget, field, state, or pending
# action of the table.
UNAVAILABLE_KINDS = frozenset({"require_base_branch", "exhaust_source_reads",
                               "require_upstream_access", "require_renamed_upstream"})
REPOSITORY_KINDS = frozenset({"require_upstream_access", "require_renamed_upstream"})
RENAMED_BASE_KINDS = frozenset({"follow_renamed_base", "resume_renamed_base"})
READ_AGAIN_KINDS = frozenset({"resume_upstream_read"})
UNAVAILABLE_PENDING = frozenset({lifecycle.BASE_BRANCH_UNAVAILABLE, lifecycle.UPSTREAM_UNAVAILABLE})


def _branch(details: dict) -> str:
    base = details.get("base_ref")
    return f"`{_quotable(base, 255)}`" if base else "the default branch"


def _renamed(details: dict, when: str) -> str:
    number = _quotable(details.get("pr_number"), 12)
    new = f"`{_quotable(details.get('renamed_to'), 255)}`"
    return (f"The upstream renamed the base branch {_branch(details)} to {new}{when}. The "
            f"upstream pull request #{number} targets {new}, so the contribution now follows "
            f"it. The base commit {_head(details.get('base_sha'))} is unchanged.")


def _unavailable_cause(kind: str, details: dict) -> str:
    """Which cause applied, in plain words: the branch does not exist, was
    renamed, or could not be read; the repository could not be read or moved;
    or the record followed a renamed branch."""
    if kind in RENAMED_BASE_KINDS:
        return _renamed(details, "")
    if details.get("repository_back") is True and kind not in REPOSITORY_KINDS:
        return ("The upstream repository is back under its recorded name, but its base branch "
                f"is not: {_unavailable_cause(kind, {k: v for k, v in details.items() if k != 'repository_back'})}")
    failed = ""
    if isinstance(details.get("count"), int):
        since = _quotable(details.get("since") or "", 40)
        failed = (" Reads have failed" + (f" since {since}" if since else "")
                  + f": {details['count']} counted failures")
        if isinstance(details.get("repository_failures"), int):
            failed += f", {details['repository_failures']} of them for the repository itself"
        failed += "."
    if kind in REPOSITORY_KINDS:
        repository = f"`{_quotable(details.get('repository'), 200)}`"
        if details.get("renamed_repository"):
            return (f"Cause: GitHub now names the upstream repository {repository} "
                    f"`{_quotable(details['renamed_repository'], 200)}`: it was renamed or "
                    "transferred." + failed)
        return (f"Cause: the upstream repository {repository} could not be read (it may have "
                "been deleted, made private, or the token lost access)." + failed)
    if details.get("renamed_to"):
        return (f"Cause: the upstream renamed the base branch {_branch(details)} to "
                f"`{_quotable(details['renamed_to'], 255)}`.")
    if isinstance(details.get("count"), int):
        return f"Cause: the upstream base branch {_branch(details)} could not be read." + failed
    return f"Cause: the upstream says the base branch {_branch(details)} does not exist."


def _unavailable_guidance(record: dict, event: dict) -> str:
    """What a human can do while the upstream is unavailable. A wait that
    failed reads caused ends on its own once the upstream is read again, and
    a resume reads it again at once; the wait a renamed repository caused
    ends on its own once GitHub answers for it under its recorded name
    again, and a resume before then waits again at once. A restart on
    another branch is a refresh of the parked contribution: it is named
    only after a park, and never while the upstream PR is open, which
    refuses it."""
    pending = record.get("pending_human_action") or {}
    if engine.state_key(record) not in engine.HUMAN_WAITS or record.get("paused") \
            or pending.get("kind") not in UNAVAILABLE_PENDING:
        return ""
    revision = record["revision"]
    by_reads = pending.get("counter") == "source_reads"
    moved = (" A repository that was renamed or transferred is a new request: park, then "
             "abandon, and file a request for its new name.")
    if pending["kind"] == lifecycle.UPSTREAM_UNAVAILABLE and by_reads:
        return (" The pipeline keeps reading it and continues on its own once it can be read "
                "again; a resume reads it again at once." + moved)
    if pending["kind"] == lifecycle.UPSTREAM_UNAVAILABLE \
            and record["upstream"].get("repository_id") is None:
        return (" GitHub did not say which repository it is, so the pipeline does not continue "
                "on its own: once the repository is back under its recorded name, a resume "
                "continues; while GitHub names it otherwise, a resume only waits again." + moved)
    if pending["kind"] == lifecycle.UPSTREAM_UNAVAILABLE:
        return (" The pipeline keeps reading it and continues on its own if GitHub answers for "
                "the same repository under its recorded name again; while GitHub names it "
                "otherwise, a resume only waits again." + moved)
    if by_reads:
        text = (" The pipeline keeps reading the branch and continues on its own once it can be "
                "read again; a resume reads it again at once.")
    else:
        text = " Once the branch can be read again, resume."
    if record["phase"] == "upstream_review" or record.get("upstream_pr"):
        return text + (" The upstream pull request is open, so a restart on another branch is "
                       "refused; a rename the pull request follows is applied without a "
                       "command.")
    renamed = ((event.get("details") or {}).get("renamed_to"))
    branch = renamed if isinstance(renamed, str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", renamed) else "NAME"
    return text + (" To continue on another branch, park first; then, once it is parked, post "
                   f'`/upstreaming refresh revision={revision} branch={branch} reason="..."`.')


def _waits_for_upstream(event: dict, record: dict) -> bool:
    """Whether ``event`` is a reminder of a wait for an unavailable
    upstream."""
    details = event.get("details") or {}
    pending = record.get("pending_human_action") or {}
    return lifecycle.current_name(event["kind"]) in REMINDER_VERBS \
        and engine.state_key(record) == lifecycle.current_name(details.get("to_state")) \
        and pending.get("kind") in UNAVAILABLE_PENDING


def plain_message(event: dict, record: dict) -> str | None:
    """The entry of an event about an unavailable upstream, in plain words,
    or ``None`` for any other event: what happened and why, what was
    observed, and what a human can do. It names no verb, budget, field, or
    pending action of the table; the commands below the entry are the only
    names a human types."""
    kind = lifecycle.current_name(event["kind"])
    details = event.get("details") or {}
    # The error of a counted read; a definitive answer is the cause itself.
    observed = (f" Last error: `{_quotable(details['why'], 600)}`."
                if details.get("why") and isinstance(details.get("count"), int) else "")
    if kind in UNAVAILABLE_KINDS:
        what = ("the upstream repository is unavailable" if kind in REPOSITORY_KINDS
                else "the upstream base branch is unavailable")
        return (f"The contribution waits for a human: {what}. "
                + _unavailable_cause(kind, details) + observed
                + _unavailable_guidance(record, event))
    if kind == "follow_renamed_base":
        return _renamed(details, " while the upstream pull request is open") \
            + " The contribution continues without a command."
    if kind == "resume_renamed_base":
        return (_renamed(details, " while the upstream pull request is open")
                + " The wait the rename caused ends without a command, and the revision that "
                  "was due is prepared again.")
    if kind in READ_AGAIN_KINDS:
        what = ("repository" if details.get("pending_human_action") == lifecycle.UPSTREAM_UNAVAILABLE
                else f"base branch {_branch(details)}")
        # The wait a renamed repository caused (an event written before
        # the merge of the two resumes names its own verb).
        renamed = details.get("cause") == "renamed" or event["kind"] == "resume_renamed_upstream"
        how = (" under its recorded name" if renamed else "")
        return (f"The upstream {what} can be read again{how}, so the wait for it ends without "
                "a command, and the contribution continues with the step that was due.")
    if kind == "verify_upstream_pr" and details.get("renamed_to"):
        link = _link(details.get("url"))
        number = _quotable(details.get("pr_number"), 12)
        pr = f"[#{number}]({link})" if link else f"#{number}"
        return (_renamed(details, " before the pull request was opened")
                + f" Pull request {pr} by {_login(details.get('author'))} at "
                  f"{_head(details.get('head'))} is recorded, and the contribution now waits "
                  "for its upstream CI and review.")
    if _waits_for_upstream(event, record):
        pending = record["pending_human_action"]
        what = ("the upstream repository is unavailable"
                if pending["kind"] == lifecycle.UPSTREAM_UNAVAILABLE
                else "the upstream base branch is unavailable")
        requested = (f" Requested at {_quotable(details['requested_at'], 40)}."
                     if details.get("requested_at") else "")
        return (f"Reminder: the contribution still waits for a human because {what}."
                + requested + _unavailable_guidance(record, event))
    return None


def _close_finding(name, entry: dict) -> str:
    """How PyPI serves one distribution, as the reconciler confirmed a
    decision close: its latest release and one wheel, quoted."""
    release = f"`{_quotable(name, 100)} {_quotable(entry.get('version'), 40)}`"
    wheel = f"`{_quotable(entry.get('wheel'), 200)}`"
    if entry.get("served_by") == "riscv64":
        return f"{release} (riscv64 wheel {wheel})"
    if entry.get("served_by") == "pure":
        return f"{release} (pure-Python wheels only)"
    return release


def _processing_error(event: dict) -> str:
    """The error a ``pause_on_processing_error`` event recorded: its class,
    its bounded message, the step, and the run. Never a traceback."""
    details = event.get("details") or {}
    error = _quotable(f"{details.get('error_class') or 'Error'}: {details.get('message') or ''}")
    step = _quotable(details.get("step") or "unknown", 60)
    run = _quotable(details.get("run_id") or "unknown", 40)
    return f"\n\n> {error}\n\nStep: {step}. Run: {run}.\n\n"


def _budget_note(event: dict) -> str:
    """The spent count or the exhausted counters an exhaustion recorded."""
    details = event.get("details") or {}
    if isinstance(details.get("count"), int) and details.get("limit") is not None:
        return f" Spent: {details['count']} of {_quotable(details['limit'], 8)}."
    if details.get("exhausted"):
        spent = ", ".join(f"`{_quotable(name, 40)}`" for name in details["exhausted"])
        return f" Exhausted: {spent}."
    return ""


def _override_text(override: dict) -> str:
    """The class and paths of an admission override, quoted for the issue.
    The controller records an override only with both."""
    return (f"class `{_quotable(override.get('class') or '', 60)}`, paths "
            f"`{_quotable(', '.join(override.get('paths') or []), 300)}`")


def _quoted_comments(event: dict) -> str:
    """The comments an explanation event recorded, for the tracking issue,
    each as a code span, so nothing in it renders: no mention, no link, no
    image."""
    details = event.get("details") or {}
    lines = []
    for item in details.get("comments") or []:
        url = _link(item.get("url"))
        lines.append(f"\n> `{_quotable(item.get('body'))}`" + (f" ([comment]({url}))" if url else ""))
    return ("\n" + "".join(lines) + "\n\n") if lines else ""


def _login(value) -> str:
    """A GitHub login as a mention, or a code span when it is not one."""
    text = str(value or "")
    return f"@{text}" if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", text) \
        else f"`{_quotable(text, 39)}`"


_TEMPLATES = {
    "/upstreaming pause": '/upstreaming pause revision={revision} reason="..."',
    "/upstreaming resume": '/upstreaming resume revision={revision} reason="..."',
    "/upstreaming park": '/upstreaming park revision={revision} reason="..."',
    "/upstreaming refresh": '/upstreaming refresh revision={revision} reason="..."',
    "/upstreaming abandon": '/upstreaming abandon revision={revision} reason="..."',
    "/upstreaming submit-upstream": "/upstreaming submit-upstream revision={revision} head={head}",
    "/upstreaming retract-pr": "/upstreaming retract-pr revision={revision} head={head}",
}
# The commands whose row in a state checks the exact head, by state.
_HEAD_TEMPLATES = {
    ("handoff.waiting_upstream_pr", "/upstreaming refresh"):
        '/upstreaming refresh revision={revision} head={head} reason="..."',
}
# The head a command names in a state of the table.
_HEADS = {"handoff.awaiting_submission": lambda r: (r.get("handoff_request") or {}).get("head"),
          "handoff.waiting_upstream_pr": lambda r: r.get("clean_head_sha")}
# The refresh from `parked.idle` that records an admission override, offered
# beside the plain refresh: a plan that decides park then admits
# development with the override's class and paths.
REFRESH_OVERRIDE = ('/upstreaming refresh revision={revision} class={admission} '
                    'paths="{paths}" reason="..."')


def _table_command_lines(record: dict) -> list[str]:
    """The commands the lifecycle table offers in the record's state."""
    state = engine.state_key(record)
    paused = bool(record.get("paused"))
    offered = []
    if lifecycle.LIFECYCLE[state].kind != "terminal" or paused:
        offered = [t.command for flag in lifecycle.FLAGS.values()
                   for t in (flag.cleared_by if paused else flag.set_by).values() if t.command]
    for edge in lifecycle.state_edges(state):
        command = edge.transition.command
        if not command or command in offered:
            continue
        if paused and command not in engine.PAUSED_COMMANDS:
            continue
        if command == lifecycle.RESUME and edge.verb == "resume_stale":
            continue
        offered.append(command)
    head = _HEADS.get(state, lambda r: None)(record)
    lines = [_HEAD_TEMPLATES.get((state, c), _TEMPLATES[c]).format(
                 revision=record["revision"], head=head)
             for c in offered if c in _TEMPLATES and c.split()[-1] in commands.GRAMMAR]
    operation = record.get("active_operation_id")
    if isinstance(operation, str) and re.fullmatch(_OPERATION_ID, operation) \
            and (paused or lifecycle.LIFECYCLE[state].kind == "human_wait"):
        # `active_operation_id` names an open operation only: in a wait or a
        # pause, a human who verified what it did closes it.
        lines.append(f"/upstreaming resolve-operation operation={operation} "
                     'disposition=failed|not-launched reason="..."')
    if state == "parked.idle":
        lines.append(REFRESH_OVERRIDE.format(
            revision=record["revision"],
            admission=record.get("admission_class") or "configuration_integration",
            paths=",".join(record.get("scope_paths") or [".github/workflows/*"])))
    return lines


def _command_lines(record: dict) -> list[str]:
    """The commands an authorized human may post next, from the table."""
    return _table_command_lines(record)


def render(event: dict, record: dict, run_url: str) -> str:
    schemas.validate("event", event); schemas.validate("contribution", record)
    revision = event.get("to", {}).get("revision", record["revision"])
    phase = event.get("to", {}).get("phase", record["phase"])
    activity = event.get("to", {}).get("activity", record["activity"])
    if record.get("paused"):
        activity = "paused"
    kind = event["kind"]
    plain = plain_message(event, record)
    if plain is not None:
        message = plain
    else:
        message = (table_message(event, record) or RECORDED_KINDS.get(kind)
                   or f"Recorded `{_quotable(kind, 64)}`.")
        if kind == "record_explanation":
            message += _quoted_comments(event)
        message += _observed(event)
        if kind == "pause_on_processing_error":
            message += _processing_error(event)
        message += _budget_note(event)
        message += _unavailable_guidance(record, event)
    request = _submission_request(event)
    if request:
        message += " An authorized human requests upstream submission with the command below."
    visible = f"**Upstreaming r{revision} · {phase}/{activity}** -- {message} [Workflow run]({run_url})"
    if request:
        visible += f"\n\n```text\n{request}\n```"
    lines = _command_lines(record)
    details = ""
    if lines:
        details = "\n\n<details>\n<summary>Possible next commands</summary>\n\n```text\n" + "\n".join(lines) + "\n```\n"
        details += "</details>"
    marker = f"<!-- upstreaming-ledger:{record['contribution_id']}:{event['sequence']} -->"
    return visible + details + "\n\n" + marker


# --------------------------------------------------------------------------
# Which events reach the tracking issue
#
# The tracking issue receives an entry only when a human is needed: when an
# event leaves the contribution waiting on a human, and when it ends the
# contribution. Everything else is shown in the event log of the status page
# only. The lifecycle table decides it:
#
# - a destination state of a kind that waits for a human step
#   (`lifecycle.REMINDER_KINDS`: `gate`, `human_wait`, `locked`), entered by
#   the event or kept at a new revision (the commands a human posts name the
#   revision, as after the first `resume` of a paused human wait);
# - the stop flag set by the event (`paused` in the event's `to`), whichever
#   verb of `lifecycle.FLAGS` set it, `pause_on_processing_error` included;
# - a destination state of kind `generation_end` (park) or `terminal`
#   (closed, abandoned, released), entered by the event;
# - a reminder of a pending human step: a rule that applies in exactly the
#   states of those kinds and keeps the state (`record_reminder`).
#
# - a status entry for a change the pipeline made without a human that the
#   human must know about (`NOTICE_VERBS`): the record followed a base
#   branch the upstream renamed (while the upstream PR is open, or before
#   it was opened, when `verify_upstream_pr` names the rename), or a wait
#   that failed reads caused ended once the upstream was read again;
# - a wait for an unavailable upstream (`UNAVAILABLE_KINDS`), also when it
#   replaces another wait of the same state.
#
# Replies to commands are posted by `command_feedback` and `command_reactions`.

HUMAN_KINDS = frozenset(lifecycle.REMINDER_KINDS)
END_KINDS = frozenset({"generation_end", "terminal"})
NOTICE_VERBS = RENAMED_BASE_KINDS | READ_AGAIN_KINDS
REMINDER_VERBS = frozenset(
    verb for verb, rule in lifecycle.RULES.items()
    if rule.target == lifecycle.SELF and frozenset(rule.kinds) == HUMAN_KINDS)


def state_kind(state: str | None) -> str | None:
    """The table kind of ``state``. A state the table does not have (a legacy
    activity) takes the kind of its phase when every state of that phase has
    the same kind, as `parked`, `closed`, and `released` do."""
    if not state:
        return None
    if state in lifecycle.LIFECYCLE:
        return lifecycle.LIFECYCLE[state].kind
    phase = lifecycle.state_phase(state)
    kinds = {s.kind for key, s in lifecycle.LIFECYCLE.items() if lifecycle.state_phase(key) == phase}
    return kinds.pop() if len(kinds) == 1 else None


def event_states(events: list[dict]) -> list[tuple[str | None, str | None]]:
    """The source and destination state of each event of one contribution,
    in sequence order. An event of the table names both in its `details`; an
    older event records only the fields it changed (`from`, `to`), which are
    folded over the events before it."""
    phase, activity = None, "idle"
    states = []
    for event in sorted(events, key=lambda e: e["sequence"]):
        details = event.get("details") or {}
        # An event written before the plan rename names the historic phase
        # and states.
        before, to = ({k: lifecycle.current_name(v) if k == "phase" else v
                       for k, v in (event.get(side) or {}).items()} for side in ("from", "to"))
        source = details.get("from_state")
        if not source and (before.get("phase") or phase):
            source = f"{before.get('phase') or phase}.{before.get('activity') or activity}"
        target = details.get("to_state")
        if not target:
            new_phase, new_activity = to.get("phase") or phase, to.get("activity") or activity
            target = f"{new_phase}.{new_activity}" if new_phase else None
        source, target = lifecycle.current_name(source), lifecycle.current_name(target)
        if target:
            phase, activity = target.split(".", 1)
        states.append((source, target))
    return states


def notifies(event: dict, source: str | None, target: str | None) -> bool:
    """Whether ``event`` is posted on the tracking issue (see above)."""
    if lifecycle.current_name(event["kind"]) in NOTICE_VERBS:
        return True
    # A wait for an unavailable upstream is announced even when it replaces
    # another wait of the same state (a renamed repository that is back
    # under its recorded name, its base branch missing).
    if lifecycle.current_name(event["kind"]) in UNAVAILABLE_KINDS:
        return True
    if lifecycle.current_name(event["kind"]) == "verify_upstream_pr" \
            and (event.get("details") or {}).get("renamed_to"):
        return True
    if lifecycle.current_name(event["kind"]) in REMINDER_VERBS \
            or lifecycle.current_name((event.get("details") or {}).get("verb")) in REMINDER_VERBS:
        return True
    if (event.get("to") or {}).get("paused") is True:
        return True
    kind = state_kind(target)
    if kind in HUMAN_KINDS:
        return target != source or event.get("revision_bumped") is True
    return kind in END_KINDS and target != source


class Api:
    def __init__(self, token: str):
        if not token:
            raise LedgerError("App issue token required")
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None):
        if method not in ("GET", "POST", "PATCH", "DELETE") or not path.startswith(f"/repos/{CONTROL_REPO}/issues/") or ".." in path:
            raise LedgerError("invalid ledger API request")
        req = urllib.request.Request("https://api.github.com" + path,
            data=None if body is None else json.dumps(body).encode(), method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise LedgerError(f"ledger API HTTP {exc.code}") from None
        if len(raw) > 2 * 1024 * 1024:
            raise LedgerError("oversized ledger response")
        return json.loads(raw) if raw else None


def issue_comments(api: Api, number: int, max_pages: int = 20) -> list[dict]:
    comments = []
    for page in range(1, max_pages + 1):
        suffix = "?per_page=100" if page == 1 else f"?per_page=100&page={page}"
        batch = api.request("GET", f"/repos/{CONTROL_REPO}/issues/{number}/comments{suffix}")
        if not isinstance(batch, list):
            raise LedgerError("issue comment inventory is malformed")
        comments.extend(batch)
        if len(batch) < 100:
            return comments
    raise LedgerError("issue comment inventory exceeded pagination bound")


def publish(root: Path, report: dict, token: str, run_url: str) -> list[dict]:
    api = Api(token)
    reported = {
        Path(path)
        for path in report.get("written", [])
        if re.fullmatch(r"\.state/events/[A-Za-z0-9_.-]+/[0-9]{8}\.json", path)
    }
    # Projection is recoverable after a failure following the state commit:
    # every tick revisits durable events and hidden markers prevent duplicates.
    durable = {
        path.relative_to(root)
        for path in (root / ".state/events").glob("*/*.json")
        if path.is_file() and not path.is_symlink()
    }
    paths = sorted(reported | durable)
    events = {path: json.loads((root / path).read_text()) for path in paths}
    # Whether an event is posted depends on the events before it, so each
    # contribution's events are read in sequence order.
    posted: dict[Path, bool] = {}
    for cid_file in sorted({path.parent.name for path in paths}):
        own = sorted((p for p in paths if p.parent.name == cid_file),
                     key=lambda p: events[p]["sequence"])
        for path, (source, target) in zip(own, event_states([events[p] for p in own])):
            posted[path] = notifies(events[path], source, target)
    receipts = []
    comment_cache: dict[int, list[dict]] = {}
    for path in paths:
        if not posted[path]:
            # Shown in the event log of the status page only. Not a failure,
            # and never posted later.
            receipts.append({"event": str(path), "status": "status_page_only"})
            continue
        event = events[path]
        cid_file = path.parent.name
        record = state.migrate_names("contributions", json.loads(
            (root / ".state/contributions" / f"{cid_file}.json").read_text()))[0]
        number = record["tracking_issue"]["number"]
        marker = f"<!-- upstreaming-ledger:{record['contribution_id']}:{event['sequence']} -->"
        if number not in comment_cache:
            comment_cache[number] = issue_comments(api, number)
        comments = comment_cache[number]
        existing = next((comment for comment in comments if marker in (comment.get("body") or "")), None)
        if existing:
            if (existing.get("user") or {}).get("login") != "riseproject-dev[bot]":
                raise LedgerError("lifecycle ledger marker is not App-authored")
            receipts.append({"event": str(path), "comment_id": existing["id"], "status": "existing"})
            continue
        body = render(event, record, run_url)
        comment = api.request("POST", f"/repos/{CONTROL_REPO}/issues/{number}/comments", {"body": body})
        observed = api.request("GET", f"/repos/{CONTROL_REPO}/issues/comments/{comment['id']}")
        if observed.get("body") != body or (observed.get("user") or {}).get("login") != "riseproject-dev[bot]":
            raise LedgerError("ledger comment readback or App author mismatch")
        comments.append(observed)
        receipts.append({"event": str(path), "comment_id": observed["id"], "status": "created"})
    return receipts

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = publish(args.root, json.loads(args.report.read_text()), os.environ.get("GH_TOKEN", ""), args.run_url)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
