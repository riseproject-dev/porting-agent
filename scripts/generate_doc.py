#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Generate the public porting status page from durable contribution state."""

from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

CONTROL_REPO = "riseproject-dev/porting-agent"
GITHUB = "https://github.com"

PHASE_LABELS = {
    "intake": "Requested",
    "investigation": "Investigating",
    "parked": "Parked",
    "closed": "Closed",
    "released": "Released",
    "provisioning": "Preparing fork",
    "development": "Development",
    "handoff": "Ready for upstream",
    "upstream_review": "Upstream review",
    "completed": "Completed",
}
ACTIVITY_LABELS = {
    "idle": "",
    "agent_running": "agent running",
    "waiting_ci": "waiting for CI",
    "waiting_human": "waiting for human",
    "awaiting_submission": "waiting for a human to request submission",
    "cleaning_debug": "removing debug commits",
    "closing_internal_pr": "closing the development PR",
    "preparing_submission": "posting the submission package",
    "waiting_upstream_pr": "waiting for a human to open the upstream PR",
}


# Why a contribution is closed (`terminal_reason` of `closed.idle`).
TERMINAL_REASON_LABELS = {
    "abandoned": "abandoned",
    "withdrawn": "request withdrawn",
    "no_work": "not applicable",
    "deferred": "deferred",
    "already_released": "already released",
}


def status_label(state: dict) -> str:
    phase = PHASE_LABELS.get(state["phase"], state["phase"].replace("_", " ").title())
    if state["phase"] == "closed" and state.get("terminal_reason"):
        reason = state["terminal_reason"]
        phase += f" ({TERMINAL_REASON_LABELS.get(reason, reason.replace('_', ' '))})"
        return phase
    activity = ACTIVITY_LABELS.get(
        state["activity"], state["activity"].replace("_", " ")
    )
    if state.get("paused"):
        pending = f"; {activity} pending" if activity else ""
        return f"Paused ({phase}{pending})"
    return f"{phase} ({activity})" if activity else phase


def escape_html(value: str) -> str:
    """Escape a value for HTML text or a quoted attribute. Braces are
    escaped too, so no value can start a Liquid tag when Jekyll renders the
    page."""
    return html.escape(str(value), quote=True).replace("{", "&#123;").replace("}", "&#125;")


def escape_cell(value: str) -> str:
    """Escape generated values for one table cell."""
    return escape_html(str(value).replace("\n", " "))


_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_RUN = re.compile(r"[1-9][0-9]{0,19}")
_URL = re.compile(r"https://github\.com/[A-Za-z0-9._~/#?=&%:+-]{1,500}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def html_link(label: str, url: str | None) -> str:
    """An HTML link to a GitHub URL the pipeline built or recorded, or ``-``."""
    if not url or not _URL.fullmatch(url):
        return "-"
    return f'<a href="{escape_html(url)}">{escape_cell(label)}</a>'


def repo_url(repo, *path) -> str | None:
    repo = str(repo or "")
    return "/".join((GITHUB, repo, *map(str, path))) if _REPO.fullmatch(repo) else None


def run_url(run_id, repo: str = CONTROL_REPO) -> str | None:
    run = str(run_id or "")
    return repo_url(repo, "actions/runs", run) if _RUN.fullmatch(run) else None


def commit_url(repo, sha) -> str | None:
    sha = str(sha or "")
    return repo_url(repo, "commit", sha) if _SHA.fullmatch(sha) else None


def state_file_url(kind: str, item_id: str) -> str:
    return f"{GITHUB}/{CONTROL_REPO}/blob/main/.state/{kind}/{item_id}.json"


# --------------------------------------------------------------------------
# Event log


class StateData:
    """The committed records an event log resolves its references against."""

    def __init__(self, root: Path | None):
        self.root = root

    def _read(self, kind: str, item_id: str) -> dict | None:
        if self.root is None or not _ID.fullmatch(str(item_id or "")):
            return None
        path = self.root / kind / f"{item_id}.json"
        if not path.is_file() or path.is_symlink():
            return None
        try:
            data = json.loads(path.read_text())
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def evidence(self, item_id):
        return self._read("evidence", item_id)

    def operation(self, item_id):
        return self._read("operations", item_id)

    def events(self, contribution_id: str) -> list[dict]:
        if self.root is None:
            return []
        directory = self.root / "events" / contribution_id.replace("/", "--")
        if not _ID.fullmatch(directory.name) or not directory.is_dir():
            return []
        events = []
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or not re.fullmatch(r"[0-9]{8}\.json", path.name):
                continue
            try:
                event = json.loads(path.read_text())
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("sequence"), int):
                events.append(event)
        return sorted(events, key=lambda event: event["sequence"])


def event_states(events: list[dict]) -> list[tuple[str | None, str | None, int | None, int | None]]:
    """The source and destination state and revision of each event, in
    sequence order. An event of the lifecycle table names its states in
    `details`; an older event records only the fields it changed, which are
    folded over the events before it."""
    phase, activity, revision = None, "idle", 0
    out = []
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        before = event.get("from") if isinstance(event.get("from"), dict) else {}
        to = event.get("to") if isinstance(event.get("to"), dict) else {}
        source = details.get("from_state")
        if not source and (before.get("phase") or phase):
            source = f"{before.get('phase') or phase}.{before.get('activity') or activity}"
        target = details.get("to_state")
        if not target and (to.get("phase") or phase):
            target = f"{to.get('phase') or phase}.{to.get('activity') or activity}"
        if target and "." in str(target):
            phase, activity = str(target).split(".", 1)
        old_revision = before.get("revision", revision)
        if isinstance(to.get("revision"), int):
            revision = to["revision"]
        elif event.get("revision_bumped") is True and isinstance(revision, int):
            revision += 1
        elif isinstance(details.get("revision"), int):
            # An older event that names the revision it is bound to.
            revision = details["revision"]
        out.append((source, target, old_revision if isinstance(old_revision, int) else None,
                    revision))
    return out


def _strings(value, depth: int = 0):
    """Every string in a bounded walk of ``value``, in order."""
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from _strings(value[key], depth + 1)
    elif isinstance(value, list):
        for item in value[:50]:
            yield from _strings(item, depth + 1)


def _evidence_links(evidence_id: str, evidence: dict) -> list[tuple[str, str | None]]:
    links = [(f"evidence {evidence_id}", state_file_url("evidence", evidence_id))]
    ci = evidence.get("ci") if isinstance(evidence.get("ci"), dict) else None
    if ci:
        repo = ci.get("repository")
        links.append((f"CI run {ci.get('run_id')}", run_url(ci.get("run_id"), repo)))
        links.append((f"commit {str(ci.get('head_sha') or '')[:12]}", commit_url(repo, ci.get("head_sha"))))
        for job in ci.get("jobs") or []:
            if isinstance(job, dict) and job.get("executed") \
                    and job.get("conclusion") not in ("success", "skipped", "neutral", None) \
                    and _RUN.fullmatch(str(job.get("job_id") or "")) and run_url(ci.get("run_id"), repo):
                links.append((f"failed job {job['job_id']}",
                              f"{run_url(ci.get('run_id'), repo)}/job/{job['job_id']}"))
    return links


def _operation_links(operation_id: str, op: dict, record: dict) -> list[tuple[str, str | None]]:
    links = [(f"operation {operation_id}", state_file_url("operations", operation_id))]
    target = op.get("target") if isinstance(op.get("target"), dict) else {}
    external = op.get("external_ids") if isinstance(op.get("external_ids"), dict) else {}
    proposed = op.get("proposed") if isinstance(op.get("proposed"), dict) else {}
    for key in sorted(external):
        if key.endswith("_run_id"):
            links.append((f"workflow run {external[key]}", run_url(external[key])))
    repo = target.get("repository")
    if str(op.get("kind") or "").startswith("dev_pr") and external.get("pr_number"):
        links.append((f"development PR #{external['pr_number']}",
                      repo_url(repo, "pull", external["pr_number"])
                      if _RUN.fullmatch(str(external["pr_number"])) else None))
    if proposed.get("commit"):
        links.append((f"commit {str(proposed['commit'])[:12]}", commit_url(repo, proposed["commit"])))
    return links


def event_links(event: dict, record: dict, data: StateData) -> list[tuple[str, str]]:
    """Every reference of ``event`` that committed data resolves, as
    ``(label, url)`` pairs, in a fixed order and without duplicates."""
    fork = record.get("fork") or {}
    fork_repo = fork.get("name")
    issue = (record.get("tracking_issue") or {}).get("number")
    upstream_pr = record.get("upstream_pr") or {}
    cause = event.get("cause") if isinstance(event.get("cause"), dict) else {}
    cause_id = str(cause.get("id") or "")
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    links: list[tuple[str, str | None]] = []

    def head(sha, what="commit"):
        full = str(fork.get("head_sha") or "")
        sha = full if sha and full.startswith(str(sha)) else sha
        links.append((f"{what} {str(sha or '')[:12]}", commit_url(fork_repo, sha)))

    # The cause.
    if _RUN.fullmatch(cause_id):
        links.append((f"workflow run {cause_id}", run_url(cause_id)))
    elif m := re.fullmatch(r"dispatcher-([0-9]+)", cause_id):
        links.append((f"dispatcher run {m[1]}", run_url(m[1])))
    elif m := re.fullmatch(r"(?:branch:|clean-head-ci-)([0-9a-f]{7,40})", cause_id):
        head(m[1])
    elif m := re.fullmatch(r"comment-([0-9]+)", cause_id):
        if _RUN.fullmatch(str(issue or "")):
            links.append((f"command comment {m[1]}",
                          f"{GITHUB}/{CONTROL_REPO}/issues/{issue}#issuecomment-{m[1]}"))
    elif re.fullmatch(r"issue-[0-9]+", cause_id) and issue:
        links.append((f"tracking issue #{issue}", repo_url(CONTROL_REPO, "issues", issue)))
    # Evidence and operations the cause or the details name.
    for value in [cause_id, *_strings(details)]:
        evidence = data.evidence(value)
        if evidence is not None:
            links.extend(_evidence_links(value, evidence))
            continue
        op = data.operation(value)
        if op is not None:
            links.extend(_operation_links(value, op, record))
    # Heads, pull requests, and runs the details name.
    for key in ("head", "clean_head", "replaced"):
        if details.get(key):
            head(details[key], "clean head" if key == "clean_head" else "commit")
    bound = details.get("bound_to") if isinstance(details.get("bound_to"), dict) else {}
    if bound.get("head"):
        head(bound["head"])
    if details.get("run_id"):
        links.append((f"controller run {details['run_id']}", run_url(details["run_id"])))
    if m := re.fullmatch(r"pr-([1-9][0-9]*)", cause_id):
        links.append((f"development PR #{m[1]}", repo_url(fork_repo, "pull", m[1])))
    if re.fullmatch(r"upstream-pr-[0-9]+", cause_id) or details.get("verb") == "verify_upstream_pr":
        url = details.get("url") or upstream_pr.get("url")
        number = details.get("pr_number") or upstream_pr.get("number")
        links.append((f"upstream PR #{number}", url if _URL.fullmatch(str(url or "")) else None))
    if details.get("package_url"):
        url = str(details["package_url"])
        links.append(("submission package", url if _URL.fullmatch(url) else None))
    for item in details.get("comments") or []:
        url = str((item or {}).get("url") or "") if isinstance(item, dict) else ""
        if _URL.fullmatch(url):
            links.append(("comment", url))
    seen, out = set(), []
    for label, url in links:
        if url and _URL.fullmatch(url) and url not in seen:
            seen.add(url)
            out.append((label, url))
    return out


CHEVRON = ('<svg class="event-log__chevron" viewBox="0 0 16 16" width="16" height="16" '
           'aria-hidden="true" focusable="false"><path d="M 3.5 6 L 8 10.5 L 12.5 6" fill="none" '
           'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
           'stroke-linejoin="round"/></svg>')


def _time(value) -> tuple[str, str]:
    try:
        at = datetime.fromisoformat(str(value)).astimezone(timezone.utc)
    except ValueError:
        return "", "-"
    return at.isoformat(timespec="seconds"), at.strftime("%Y-%m-%d %H:%M:%S")


def render_event(event: dict, states, record: dict, data: StateData) -> str:
    source, target, old_revision, revision = states
    stamp, shown = _time(event.get("at"))
    to = event.get("to") if isinstance(event.get("to"), dict) else {}
    cause = event.get("cause") if isinstance(event.get("cause"), dict) else {}
    flags = ""
    if to.get("paused") is True:
        flags = ' <span class="event-log__flag">paused</span>'
    elif to.get("paused") is False:
        flags = ' <span class="event-log__flag">resumed</span>'
    if old_revision is not None and old_revision != revision:
        revision_text = f"r{old_revision} &rarr; r{revision}"
    else:
        revision_text = f"r{revision}" if revision is not None else "-"
    change = (f'<code>{escape_cell(source or "-")}</code> &rarr; '
              f'<code>{escape_cell(target or "-")}</code>')
    links = " &middot; ".join(html_link(label, url) for label, url in event_links(event, record, data))
    time_html = f'<time datetime="{escape_html(stamp)}">{escape_cell(shown)}</time>' if stamp else "-"
    return (
        '<li class="event-log__event">'
        f'<div class="event-log__head"><span class="event-log__seq">#{int(event["sequence"])}</span> '
        f'{time_html} <code class="event-log__kind">{escape_cell(event.get("kind") or "-")}</code> '
        f'<span class="event-log__cause">{escape_cell(cause.get("type") or "")}</span></div>'
        f'<div class="event-log__change">{change} <span class="event-log__revision">'
        f'{revision_text}</span>{flags}</div>'
        + (f'<div class="event-log__links">{links}</div>' if links else "")
        + "</li>")


def render_event_log(record: dict, data: StateData) -> str:
    """The collapsed event log of one contribution, newest event first."""
    events = data.events(str(record.get("contribution_id") or ""))
    count = len(events)
    noun = "event" if count == 1 else "events"
    summary = (f'<summary>{CHEVRON}<span>Event log</span> '
               f'<span class="event-log__count">({count} {noun})</span></summary>')
    if not events:
        return f'<details class="event-log">{summary}<p>No events are recorded.</p></details>'
    items = [render_event(event, states, record, data)
             for event, states in zip(events, event_states(events))]
    return (f'<details class="event-log">{summary}<ol class="event-log__list">'
            + "".join(reversed(items)) + "</ol></details>")


HEADERS = ("Project", "Tracking issue", "Status", "Development PR", "Upstream PR", "Updated (UTC)")


def render(states: list[dict], state_root: Path | None = None) -> str:
    """The status page. ``state_root`` is the committed `.state` directory
    from which each contribution's event log is read."""
    data = StateData(state_root)
    states = sorted(states, key=lambda state: (state["upstream"]["full_name"].casefold(),
                                               str(state.get("contribution_id") or "")))
    lines = [
        "---",
        "title: Status",
        "nav_order: 2",
        "permalink: /status/",
        "---",
        "",
        "# Porting status",
        "",
        "This table tracks work to contribute RISC-V support back to upstream projects. "
        "It is generated from the pipeline's durable state whenever the website is built. "
        "Open the event log under a project to see every recorded event, newest first, with "
        "links to the workflow runs, commits, pull requests, CI runs, and evidence it refers to.",
        "",
        '<table class="status-table">',
        "<thead><tr>" + "".join(f'<th scope="col">{escape_cell(h)}</th>' for h in HEADERS) + "</tr></thead>",
    ]
    for state in states:
        full_name = state["upstream"]["full_name"]
        issue = state["tracking_issue"]["number"]
        fork = state.get("fork") or {}
        internal_url = None
        if fork.get("name") and fork.get("pr_number"):
            internal_url = repo_url(fork["name"], "pull", fork["pr_number"])
        upstream_pr = state.get("upstream_pr") or {}
        updated = datetime.fromisoformat(state["updated_at"]).strftime("%Y-%m-%d %H:%M")
        cells = [
            html_link(full_name, repo_url(full_name)) if repo_url(full_name)
            else escape_cell(full_name),
            html_link(f"#{issue}", repo_url(CONTROL_REPO, "issues", issue)),
            escape_cell(status_label(state)),
            html_link(f"#{fork.get('pr_number')}", internal_url) if internal_url else "-",
            html_link(f"#{upstream_pr.get('number')}", upstream_pr.get("url")) if upstream_pr else "-",
            escape_cell(updated),
        ]
        lines.append("<tbody>")
        lines.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
        lines.append(f'<tr class="event-log-row"><td colspan="{len(HEADERS)}">'
                     + render_event_log(state, data) + "</td></tr>")
        lines.append("</tbody>")
    lines.extend(
        [
            "</table>",
            "",
            "Status changes are recorded by the automated porting pipeline. "
            "Use the tracking issue for the available actions.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=Path(".state/contributions"))
    parser.add_argument("--output", type=Path, default=Path("docs/status/index.md"))
    args = parser.parse_args()
    states = [json.loads(path.read_text()) for path in sorted(args.state_dir.glob("*.json"))]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(states, args.state_dir.parent))


if __name__ == "__main__":
    main()
