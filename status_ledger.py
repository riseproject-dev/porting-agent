# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Append concise App-authored lifecycle entries to tracking issues."""
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
    "dossier_develop": "Verified the investigation dossier and admitted automatic development.",
    "dossier_parked": "Verified the investigation dossier and parked the contribution because no bounded patch is actionable.",
    "fork_provisioned": "Provisioned and verified the contribution fork and exact `riscv64` base.",
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
    "resume_investigation": "Resumed: the investigation continues.",
    "resume_provisioning": "Resumed: provisioning continues.",
    "resume_ci": "Resumed: CI on the head is observed again.",
    "resume_development": "Resumed: development Work is due again.",
    "resume_upstream_review": "Resumed: the upstream review is observed again.",
    "resume_upstream_revision": "Resumed: upstream revision Work is due again.",
    "resume_release_observation": "Resumed: the release is observed again.",
    compat.FLAG_MIGRATED: ("Recorded the stop of this contribution in the lifecycle's one stop "
                           "flag, `paused`, with its reason and who set it. The retired "
                           "`block` is a pause now; `resume` at the current revision ends it."),
    "park_contribution": "Parked the contribution; its evidence is kept for audit.",
    "refresh_investigation": ("Started a refresh: a complete investigation runs again while the "
                              "fork and development PR are kept."),
    "worker_no_change": "Verified a no-change worker Result for the exact head. No new repair Work was requested.",
    "worker_proposal_rejected": "Rejected the worker proposal in trusted validation.",
    "repair_attempts_exhausted": ("Exhausted the bounded repair-attempt budget and parked the contribution. "
                                  "Correct the recorded cause, then `resume` at the current revision starts a "
                                  "clean generation at `queued:idle`."),
}


SUBMIT_HOLD = ("This controller does not run the submission preparation that the command "
               "authorizes (debug cleanup, CI on the clean head, closing the development PR, and "
               "the submission package), so for now it refuses the command and changes nothing: "
               "the contribution stays in `handoff.awaiting_submission` with its fork, "
               "development PR, head, and evidence.")


def submit_command(revision: int, head: str) -> str:
    return f"/upstreaming submit-upstream revision={revision} head={head}"


def _submission_request(event: dict, record: dict) -> tuple[str, str] | None:
    """The message and the command of an entry that asks for upstream
    submission, bound to the revision and head the event recorded."""
    details = event.get("details") or {}
    head, revision = details.get("head"), details.get("revision")
    if not head or revision is None:
        return None
    if event["kind"] == "nominate_for_handoff":
        lead = f"Required CI passed on the exact head `{head}` of the development PR."
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
}


def _table_command_lines(record: dict) -> list[str]:
    """The commands the lifecycle table offers in the record's state."""
    state = engine.state_key(record)
    paused = bool(record.get("paused"))
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
    head = (record.get("handoff_request") or {}).get("head") or record.get("clean_head_sha")
    return [_TEMPLATES[c].format(revision=record["revision"], head=head) for c in offered
            if c in _TEMPLATES and c.split()[-1] in commands.GRAMMAR]


def _command_lines(record: dict) -> list[str]:
    revision = record["revision"]
    lines = []
    phase = record["phase"]
    if phase == "handoff":
        return _table_command_lines(record)
    if phase not in ("released", "abandoned", "superseded", "no_work", "already_released"):
        if not record.get("paused"):
            lines.append(f'/upstreaming pause revision={revision} reason="..."')
        lines += [f'/upstreaming park revision={revision} reason="..."',
                  f'/upstreaming abandon revision={revision} reason="..."']
    state = lifecycle.LIFECYCLE.get(engine.state_key(record))
    if record.get("paused") or phase == "parked" or (state and state.kind == "human_wait"):
        lines.append(f'/upstreaming resume revision={revision} reason="..."')
    if phase == "development" and (record.get("fork") or {}).get("pr_number"):
        lines.append(f'/upstreaming refresh revision={revision} reason="..."')
    if phase == "abandoned":
        lines.append(f'/upstreaming restart revision={revision} reason="..."')
    if phase == "parked":
        paths = ",".join(record.get("scope_paths") or [".github/workflows/*"])
        admission = record.get("admission_class") or "configuration_integration"
        lines.append(f'/upstreaming force-develop revision={revision} class={admission} paths="{paths}" reason="..."')
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
    if kind == "resume_pause" and pending:
        message += (f" The contribution still waits for a human: pending action "
                    f"`{_quotable(pending, 80)}`. A second `resume` at the new revision "
                    "leaves the wait.")
    if kind == "pause_on_processing_error":
        message += _processing_error(event)
    request = _submission_request(event, record)
    if request:
        message = request[0]
    visible = f"**Upstreaming r{revision} · {phase}/{activity}** -- {message} [Workflow run]({run_url})"
    held = "submit-upstream" in engine.HELD_COMMANDS
    if request:
        visible += f"\n\n```text\n{request[1]}\n```"
        if held:
            visible += f"\n\n{SUBMIT_HOLD}"
    lines = _command_lines(record)
    details = ""
    if lines:
        details = "\n\n<details>\n<summary>Possible next commands</summary>\n\n```text\n" + "\n".join(lines) + "\n```\n"
        if held and any(line.startswith("/upstreaming submit-upstream ") for line in lines):
            details += "\n`submit-upstream` is refused for now and changes nothing.\n"
        details += "</details>"
    marker = f"<!-- upstreaming-ledger:{record['contribution_id']}:{event['sequence']} -->"
    return visible + details + "\n\n" + marker


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
    receipts = []
    comment_cache: dict[int, list[dict]] = {}
    for path in sorted(paths):
        event = json.loads((root / path).read_text())
        cid_file = path.parent.name
        record = json.loads((root / ".state/contributions" / f"{cid_file}.json").read_text())
        number = record["tracking_issue"]["number"]
        marker = f"<!-- upstreaming-ledger:{record['contribution_id']}:{event['sequence']} -->"
        comments = comment_cache.setdefault(number, issue_comments(api, number))
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
