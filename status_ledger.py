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
import compat
import engine
import isolation
import lifecycle
import schemas
from common import CONTROL_REPO


class LedgerError(ValueError):
    pass


MESSAGES = {
    "intake:accepted": "Accepted the labeled request.",
    "intake:label_removed": "Cancelled intake because the request label was removed before Work admission.",
    "withdraw_request": ("Withdrew the request: the request label was removed before any Work, "
                         "dossier, or fork. The contribution is closed."),
    "abandon": "Abandoned the parked contribution. It is closed; its evidence is kept for audit.",
    compat.TERMINAL_MIGRATED: ("Recorded this closed contribution in the lifecycle's one terminal "
                               "phase for work that is not needed, `closed`, with its reason in "
                               "`terminal_reason`. Nothing else changed."),
    "command:force-develop": ("Overrode the dossier park: automatic development starts with the "
                              "admission class and paths of the command."),
    "dossier_develop": "Verified the investigation dossier and admitted automatic development.",
    "dossier_parked": "Verified the investigation dossier and parked the contribution because no bounded patch is actionable.",
    "fork_provisioned": "Provisioned and verified the contribution fork and exact `riscv64-dev` base.",
    "development_pr_created": "Created and verified the same-fork development PR.",
    "development_review_feedback": "Recorded authorized development-PR change feedback and requested repair.",
    "development_review_inquiry": "Recorded an authorized explanation-only development-PR comment. No repair was requested; lifecycle state is unchanged.",
    "return_for_repair": ("Left the submission gate: a blocking CI failure on the candidate "
                          "head needs a repair. The evidence of the failure is recorded."),
    "record_actionable_review": ("Recorded authorized change feedback on the development PR. "
                                 "A repair is due; the feedback stays pending until a patch "
                                 "that carries it is published."),
    "record_explanation": ("Recorded an authorized explanation-only comment on the development "
                           "PR. No Work was requested and nothing is answered automatically; "
                           "the revision, the candidate head, and the CI evidence are unchanged."),
    "record_head_change": "Recorded a new fork head. CI evidence and the candidate head are cleared.",
    "record_late_effect": ("Recorded a late worker Result for audit. It does not match the "
                           "requested Work, so it was not folded and the state is unchanged."),
    "pause": "Paused automatic work. The lifecycle state is retained until resume.",
    "pause_on_identity_change": ("Paused automatic work: the upstream repository identity differs "
                                 "from the recorded one."),
    "pause_on_processing_error": (
        "Paused automatic work: the controller hit an unexpected error while it processed this "
        "contribution. Everything that run had prepared for the contribution was discarded: "
        "its state is as before the run, apart from this pause, and no Work or remote effect "
        "was launched for it. The requested Work and anything held for publication are "
        "cleared. Other contributions were processed normally."),
    "resume_pause": "Resumed: the pause is cleared.",
    "resume_stale": ("Resumed after a stop longer than `refresh.stale_after_days` days: the "
                     "dossier is refreshed, a complete investigation runs again while the fork "
                     "and development PR are kept."),
    "restart_generation": ("Resumed from parked: a new generation starts with a complete "
                           "investigation."),
    "retract_submission": ("Retracted the submission package: reminders stop and branch work is "
                           "unlocked. Development Work is due again; no upstream resource was "
                           "changed, and `riscv64` is left as it is."),
    "submit_upstream": ("Accepted the upstream submission request. The handoff preparation runs "
                        "now: a review shapes the commits of `riscv64-dev` and the pull request "
                        "text after the project's guidelines, `riscv64` is built from the "
                        "checked series, CI is checked on its head, and the submission package "
                        "is posted on this issue. `riscv64-dev` and the development PR stay as "
                        "they are."),
    "request_submission_rewrite": "Requested the submission rewrite Work.",
    "accept_submission_rewrite": ("The submission rewrite passed the trusted check: every "
                                  "commit is valid upstream, the series has the tree of the "
                                  "approved series without its debug commits, and it follows "
                                  "the dossier's measurable rules. Its review is next."),
    "reject_submission_rewrite": ("The submission rewrite did not pass the trusted check, or "
                                  "the series cannot be shaped without a code change. Nothing "
                                  "was posted and `riscv64` was not written. The reasons are "
                                  "recorded as rewrite evidence and are the feedback of the "
                                  "next development Work."),
    "expire_submission_rewrite": ("The rewritten series expired before `riscv64` was built; a "
                                  "fresh rewrite and review are due."),
    "request_submission_review": "Requested the review of the rewritten series and text.",
    "accept_submission_review": ("The review of the rewritten series and pull request text "
                                 "passed. `riscv64` is built from the series next."),
    "reject_submission_review": ("The review of the rewritten series and pull request text did "
                                 "not pass. Nothing was posted and `riscv64` was not written. "
                                 "The reasons are recorded as review evidence and are the "
                                 "feedback of the next development Work."),
    "confirm_clean_head": "Built `riscv64` from the reviewed series and read it back.",
    "require_clean_head_ci": ("CI on the clean head of `riscv64` has not run or not completed: "
                              "its workflow cannot be dispatched, or the bounded wait passed. "
                              "Run the project's required CI on that head, then resume."),
    "resume_clean_head_ci": "Resumed: CI on the clean head of `riscv64` is observed again.",
    "retry_upstream_branch": ("Writing `riscv64` failed with an infrastructure error; it is "
                              "attempted again."),
    "exhaust_upstream_branch_retries": (
        "Writing `riscv64` failed repeatedly (`upstream_branch` is spent). The contribution "
        "waits for a human: correct the cause, then `/upstreaming resume` at the current "
        "revision attempts it again."),
    "require_branch_intervention": (
        "An upstream pull request is open from `riscv64`, so `riscv64` was not replaced. The "
        "contribution waits for a human: close that pull request or decide otherwise, then "
        "`/upstreaming resume` at the current revision builds `riscv64` again."),
    "accept_clean_head_ci": "Required CI passed on the clean head.",
    "retry_submission_comment": "Posting the submission package failed; it is attempted again.",
    "exhaust_comment_retries": (
        "Posting the submission package failed repeatedly (`submission_comment` is spent). The "
        "contribution waits for a human: correct the cause, then `/upstreaming resume` at the "
        "current revision attempts it again."),
    "resume_submission_review": "Resumed: the submission rewrite Work is due again.",
    "resume_upstream_branch": "Resumed: building `riscv64` is attempted again.",
    "resume_submission_preparation": "Resumed: posting the submission package is attempted again.",
    "record_audit_observation": ("Recorded an observation of the upstream pull request wait for "
                                 "audit. It does not verify the requested pull request; the "
                                 "state is unchanged."),
    "resume_investigation": "Resumed: the investigation continues.",
    "resume_provisioning": "Resumed: provisioning continues.",
    "resume_ci": "Resumed: CI on the head is observed again.",
    "resume_development": "Resumed: development Work is due again.",
    "resume_upstream_review": "Resumed: the upstream review is observed again.",
    "resume_upstream_revision": "Resumed: upstream revision Work is due again.",
    "resume_release_observation": "Resumed: the release is observed again.",
    "record_reminder": "Reminder: this contribution still waits for a human.",
    compat.FLAG_MIGRATED: ("Recorded the stop of this contribution in the lifecycle's one stop "
                           "flag, `paused`, with its reason and who set it. The retired "
                           "`block` is a pause now; `resume` at the current revision ends it."),
    "park_contribution": "Parked the contribution; its evidence is kept for audit.",
    "refresh_investigation": ("Started a refresh: a complete investigation runs again while the "
                              "fork and development PR are kept."),
    "worker_no_change": "Verified a no-change worker Result for the exact head. No new repair Work was requested.",
    "worker_proposal_rejected": "Rejected the worker proposal in trusted validation.",
    "begin_investigation": "Began the investigation of the request.",
    "dispatch_investigation": "Requested investigation Work.",
    "request_patch": "Requested patch Work.",
    "request_repair": "Requested repair Work for the failing CI job of the head.",
    "request_upstream_revision": "Requested revision Work for the failing CI job of the upstream PR head.",
    "request_review_revision": "Requested revision Work for the review feedback on the upstream PR.",
    "exhaust_work_total": ("The Work budget of this generation (`work_total`) is spent. The "
                           "contribution waits for a human: `/upstreaming resume` at the current "
                           "revision starts `work_total` again, `/upstreaming park` stops it."),
    "refresh_on_exhaustion": ("The repair budgets of this generation are exhausted: the one "
                              "automatic refresh runs a complete investigation again while the "
                              "fork and development PR are kept."),
    "exhaust_repair_attempts": "The repair budgets are exhausted.",
    "allocate_work_retry": ("The requested Work ended in the queue without a usable Result: fresh "
                            "Work is requested at a new revision."),
    "exhaust_work_recovery": ("The requested Work ended in the queue without a usable Result and "
                              "its automatic recovery is exhausted. The contribution waits for a "
                              "human: `/upstreaming resume` at the current revision requests "
                              "fresh Work."),
    "reject_dossier": ("Rejected the investigation Result: it has no usable dossier. A new "
                       "investigation is due."),
    "admit_development": ("Verified the investigation dossier and admitted development: the "
                          "fork is provisioned automatically, then patch Work is due."),
    "readmit_development": ("Verified the refreshed investigation dossier: development "
                            "continues on the recorded fork and development PR."),
    "park_investigation": ("Verified the investigation dossier and parked the contribution: the "
                           "dossier decided that no bounded patch is actionable."),
    "close_investigation": ("Verified the investigation dossier and closed the contribution: "
                            "the dossier decided that the work is not needed."),
    compat.PROVISIONING_MIGRATED: (
        "Recorded this contribution in the state of the lifecycle table its provisioning shape "
        "stands for. Provisioning is planned and launched from `provisioning.idle`, and patch "
        "Work is requested only on the provisioned fork."),
    "exhaust_investigation_recovery": (
        "Rejected the investigation Result: it has no usable dossier, and the investigation "
        "attempts are exhausted. The contribution waits for a human: `/upstreaming resume` at "
        "the current revision starts a new investigation."),
    "record_publish_success": ("Published the patch to the development branch. CI on the "
                               "published head is observed next."),
    "reject_patch_proposal": ("Rejected the worker's proposal in trusted validation. "
                              "Development Work is due again at a new revision."),
    "replan_development": ("The proposal no longer applies to the fork head. Development Work "
                           "is due again at a new revision."),
    "schedule_publish_retry": ("Publishing the verified patch failed with an infrastructure "
                               "error. The same patch is published again later; no new Work is "
                               "requested."),
    "exhaust_publish_recovery": (
        "Publishing the verified patch failed with an infrastructure error, and the publish "
        "retries (`publish_recovery`) are spent. The contribution waits for a human: correct "
        "the cause, then `/upstreaming resume` at the current revision requests fresh Work."),
    "retry_dispatch": ("A dispatcher run ended without the requested Work being claimed. The "
                       "dispatcher is launched again for the same Work."),
    "exhaust_dispatch_recovery": (
        "Dispatcher runs ended without the requested Work being claimed, and the dispatch "
        "retries (`dispatch_recovery`) are spent. The contribution waits for a human: correct "
        "the cause, then `/upstreaming resume` at the current revision requests fresh Work."),
    "request_ci_rerun": (
        "The repair worker reports that the CI failure is not caused by the patch. No CI rerun "
        "is launched automatically: the contribution waits for CI of another run or attempt "
        "on the same head. Rerun the failed jobs or push to the development branch; "
        "`/upstreaming refresh` or `/upstreaming park` also leave the wait."),
    "record_ci_failure": ("A blocking CI job failed on the published head. Repair Work is due "
                          "for the failing job."),
    "record_pr_mismatch": (
        "The development PR no longer matches the recorded fork, branch, or PR. The "
        "contribution waits for a human: correct the development PR, then `/upstreaming "
        "resume` at the current revision continues development."),
    compat.DEVELOPMENT_MIGRATED: ("Moved this contribution to the lifecycle state of the table "
                                  "its development state stands for. Its fork, head, revision, "
                                  "and evidence are unchanged."),
    "start_operation": "Launched the trusted fork provisioning workflow.",
    "require_provisioning_intervention": (
        "Provisioning cannot continue safely: the fork provisioning operation failed, or its "
        "launches (`operation_launch`) are spent without a receipt. The contribution waits for "
        "a human: correct the cause, then `/upstreaming resume` at the current revision starts "
        "provisioning again."),
    "confirm_provisioning": ("Provisioned and verified the contribution fork and exact `riscv64-dev` "
                             "base. Development Work is due."),
    "repair_attempts_exhausted": ("Exhausted the bounded repair-attempt budget and parked the contribution. "
                                  "Correct the recorded cause, then `resume` at the current revision starts a "
                                  "clean generation at `queued:idle`."),
}


def submit_command(revision: int, head: str) -> str:
    return f"/upstreaming submit-upstream revision={revision} head={head}"


def _submission_request(event: dict, record: dict) -> tuple[str, str] | None:
    """The message and the command of an entry that asks for upstream
    submission, bound to the revision and head the event recorded."""
    details = event.get("details") or {}
    bound = details.get("bound_to") or details
    head, revision = bound.get("head"), bound.get("revision")
    if not head or revision is None:
        return None
    if event["kind"] == "nominate_for_handoff":
        lead = f"Required CI passed on the exact head `{head}` of the development PR."
    elif event["kind"] == "record_reminder" \
            and details.get("from_state") == "handoff.awaiting_submission":
        lead = (f"Reminder: required CI passed on the exact head `{head}` of the development PR, "
                "and upstream submission still waits for an authorized human.")
    elif event["kind"] == compat.HANDOFF_WAIT_MIGRATED:
        lead = ("Moved this contribution to the lifecycle state `handoff.awaiting_submission`. "
                f"Its fork, development PR, head `{head}`, revision, and CI evidence are "
                "unchanged. The earlier request to run `approve-handoff` no longer applies: "
                "that command is retired.")
    else:
        return None
    message = (f"{lead} To request upstream submission, an authorized human posts the command "
               "below on this issue.")
    return message, submit_command(revision, head)


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


def _handoff_message(event: dict, record: dict) -> str | None:
    """The entry of a handoff step that names its head, its package, or the
    upstream pull request."""
    details = event.get("details") or {}
    kind = event["kind"]
    head = _quotable(details.get("head"), 40)
    if kind == "confirm_clean_head":
        replaced = _quotable(details.get("replaced"), 40)
        how = ("it already resolved to that head, so nothing was written"
               if not details.get("written") else
               f"replaced `{replaced}` under the exact-head lease" if details.get("replaced")
               else "created")
        return (f"Built `riscv64` at the clean head `{head}` from the reviewed series ({how}) and "
                "read it back. `riscv64-dev` and the development PR are unchanged. CI on the "
                "clean head is checked next.")
    if kind == "accept_clean_head_ci":
        return (f"Required CI passed on the clean head `{_quotable(details.get('clean_head'), 40)}`"
                f" (CI of `{head}`). The submission package is posted next."
                if details.get("clean_head") and details.get("clean_head") != details.get("head")
                else f"Required CI passed on the clean head `{head}`. The submission package is "
                     "posted next.")
    if kind == "confirm_submission_package":
        link = _link(details.get("package_url"))
        package = f"[submission package]({link})" if link else "submission package"
        return (f"Posted the {package} for the exact clean head `{head}`. An authorized human "
                "opens the upstream pull request from it under their own account; the pipeline "
                "verifies it by observation and never opens it.")
    if kind == "verify_upstream_pr":
        link = _link(details.get("url"))
        number = _quotable(details.get("pr_number"), 12)
        pr = f"[#{number}]({link})" if link else f"#{number}"
        return (f"Verified the upstream pull request {pr} by @{_quotable(details.get('author'), 39)} "
                f"at the exact clean head `{head}`. Upstream review is observed now.")
    if kind == "return_for_repair" and details.get("from_state") in (
            "handoff.building_upstream_branch", "handoff.waiting_ci"):
        why = ("the reviewed series does not rebuild to the reviewed head, or the lease on "
               "`riscv64` conflicts" if details.get("from_state") == "handoff.building_upstream_branch"
               else "a blocking CI failure on the clean head, or `riscv64` is not at the clean "
                    "head")
        return f"Left the handoff preparation: {why}. A repair is due."
    if kind == "record_audit_observation" and details.get("mismatch"):
        return (MESSAGES[kind] + f" Observed: {_quotable(details['mismatch'], 600)}.")
    if kind == "record_reminder" and details.get("from_state") == "handoff.waiting_upstream_pr":
        bound = details.get("bound_to") or {}
        link = _link(details.get("package_url"))
        package = f"[submission package]({link})" if link else "submission package"
        who = f" @{_quotable(details['submitter'], 39)}" if details.get("submitter") else ""
        text = (f"Reminder{who}: the {package} for the exact clean head "
                f"`{_quotable(bound.get('head'), 40)}` still waits for the upstream pull request.")
        if details.get("mismatch"):
            text += (f" The observation does not match: {_quotable(details['mismatch'], 600)}. "
                     "Do not submit from the package; to withdraw it, post the `retract-pr` "
                     "command below.")
        else:
            text += (" The branch still resolves to that head: open the pull request from the "
                     "package's link under your own account, or withdraw it with `retract-pr`.")
        return text
    return None


def _processing_error(event: dict) -> str:
    """The error a ``pause_on_processing_error`` event recorded: its class,
    its bounded message, the step, and the run. Never a traceback."""
    details = event.get("details") or {}
    error = _quotable(f"{details.get('error_class') or 'Error'}: {details.get('message') or ''}")
    step = _quotable(details.get("step") or "unknown", 60)
    run = _quotable(details.get("run_id") or "unknown", 40)
    return (f"\n\n> {error}\n\nStep: {step}. Run: {run}. Once the cause is corrected, "
            "`resume` at the current revision continues; the full error is in the log of that "
            "workflow run.\n\n")


def _budget_note(event: dict) -> str:
    """What an exhaustion states: the spent count, or the exhausted counters
    and the state it leads to."""
    details = event.get("details") or {}
    kind = event["kind"]
    if kind == "exhaust_work_total" and isinstance(details.get("count"), int):
        return f" {details['count']} of {details.get('limit')} Work allocations are spent."
    if kind in ("refresh_on_exhaustion", "exhaust_repair_attempts") and details.get("exhausted"):
        spent = ", ".join(f"`{_quotable(name, 40)}`" for name in details["exhausted"])
        note = f" Exhausted: {spent}."
        if kind == "exhaust_repair_attempts":
            note += (" The contribution waits for a human; `/upstreaming resume` at the current "
                     "revision starts the counter again."
                     if details.get("to_state", "").endswith("waiting_human") else
                     " The contribution is parked; `/upstreaming resume` at the current revision "
                     "starts a clean generation.")
        return note
    return ""


def _override_text(override: dict) -> str:
    """The class and paths of an admission override, quoted for the issue.
    The controller records an override only with both."""
    return (f"class `{_quotable(override.get('class') or '', 60)}`, paths "
            f"`{_quotable(', '.join(override.get('paths') or []), 300)}`")


def _quoted_comments(event: dict) -> str:
    """The comments an explanation event recorded, for the tracking issue."""
    lines = []
    for item in (event.get("details") or {}).get("comments") or []:
        body = _quotable(item.get("body"))
        url = item.get("url")
        lines.append(f"\n> {body}" + (f" ([comment]({url}))" if url else ""))
    return ("\n" + "".join(lines) + "\n\n") if lines else ""


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
# The resume from `parked.idle` that records an admission override, offered
# beside the plain resume: a dossier that decides park then admits
# development with the override's class and paths.
RESUME_OVERRIDE = ('/upstreaming resume revision={revision} class={admission} '
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
    if state == "parked.idle" and not paused:
        lines.append(RESUME_OVERRIDE.format(
            revision=record["revision"],
            admission=record.get("admission_class") or "configuration_integration",
            paths=",".join(record.get("scope_paths") or [".github/workflows/*"])))
    return lines


def _command_lines(record: dict) -> list[str]:
    """The commands an authorized human may post next. In a state of the
    table they come from the table; in a state that only the legacy paths
    write, `pause`, `park`, and `resume` apply."""
    if engine.state_key(record) in lifecycle.LIFECYCLE:
        return _table_command_lines(record)
    revision = record["revision"]
    lines = []
    if not record.get("paused"):
        lines.append(f'/upstreaming pause revision={revision} reason="..."')
    lines.append(f'/upstreaming park revision={revision} reason="..."')
    if record.get("paused"):
        lines.append(f'/upstreaming resume revision={revision} reason="..."')
    return lines


def render(event: dict, record: dict, run_url: str) -> str:
    schemas.validate("event", event); schemas.validate("contribution", record)
    revision = event.get("to", {}).get("revision", record["revision"])
    phase = event.get("to", {}).get("phase", record["phase"])
    activity = event.get("to", {}).get("activity", record["activity"])
    underlying_activity = activity
    if record.get("paused"):
        activity = "paused"
    kind = event["kind"]
    message = MESSAGES.get(kind, kind.replace("_", " ").replace(":", ": ").capitalize() + ".")
    if kind == "development_review_feedback" and record.get("paused"):
        message = ("Recorded authorized development-PR change feedback. Repair remains pending, "
                   "but no Work was requested because the contribution is paused.")
    elif kind == "command:pause":
        pending = underlying_activity.replace("_", " ")
        message = f"Paused automatic work. Pending lifecycle activity `{pending}` is retained until resume."
    if kind == "record_explanation":
        message += _quoted_comments(event)
    pending = (event.get("details") or {}).get("pending_human_action")
    if kind == "record_reminder" and pending:
        message = (f"Reminder: this contribution still waits for a human: pending action "
                   f"`{_quotable(pending, 80)}`, requested at "
                   f"{_quotable((event.get('details') or {}).get('requested_at'), 40)}.")
    elif kind == "resume_pause" and pending:
        message += (f" The contribution still waits for a human: pending action "
                    f"`{_quotable(pending, 80)}`. A second `resume` at the new revision "
                    "leaves the wait.")
    if kind == "pause_on_processing_error":
        message += _processing_error(event)
    if kind == "record_pr_mismatch" and (event.get("details") or {}).get("mismatch"):
        message += f" Observed: {_quotable(event['details']['mismatch'], 300)}."
    message += _budget_note(event)
    details = event.get("details") or {}
    override = details.get("admission_override")
    if kind == "restart_generation" and override:
        message += (f" The admission override is recorded ({_override_text(override)}). "
                    "The new investigation still runs; a dossier that decides park then admits "
                    "development with the override's class and paths, and a dossier that "
                    "decides develop or close is applied as it decides. An override names both "
                    "a class and paths; a resume that names only one of them is refused.")
    elif kind in ("admit_development", "readmit_development") and override:
        message += (f" The dossier decided park; the recorded admission override "
                    f"({_override_text(override)}) admitted development instead.")
    elif kind == "close_investigation" and details.get("admission_override_not_applied"):
        message += (" The recorded admission override was not applied: it applies against a "
                    "dossier decision park only.")
    handoff_message = _handoff_message(event, record)
    if handoff_message:
        message = handoff_message
    request = _submission_request(event, record)
    if request:
        message = request[0]
    visible = f"**Upstreaming r{revision} · {phase}/{activity}** -- {message} [Workflow run]({run_url})"
    if request:
        visible += f"\n\n```text\n{request[1]}\n```"
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
# Replies to commands are posted by `command_feedback` and `command_reactions`.

HUMAN_KINDS = frozenset(lifecycle.REMINDER_KINDS)
END_KINDS = frozenset({"generation_end", "terminal"})
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
        before, to = event.get("from") or {}, event.get("to") or {}
        source = details.get("from_state")
        if not source and (before.get("phase") or phase):
            source = f"{before.get('phase') or phase}.{before.get('activity') or activity}"
        target = details.get("to_state")
        if not target:
            new_phase, new_activity = to.get("phase") or phase, to.get("activity") or activity
            target = f"{new_phase}.{new_activity}" if new_phase else None
        if target:
            phase, activity = target.split(".", 1)
        states.append((source, target))
    return states


def notifies(event: dict, source: str | None, target: str | None) -> bool:
    """Whether ``event`` is posted on the tracking issue (see above)."""
    if event["kind"] in REMINDER_VERBS or (event.get("details") or {}).get("verb") in REMINDER_VERBS:
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
        record = json.loads((root / ".state/contributions" / f"{cid_file}.json").read_text())
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
