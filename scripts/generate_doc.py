#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Generate the public porting status page from durable contribution state."""

from __future__ import annotations

import argparse
import html
import json
import urllib.parse
from datetime import datetime
from pathlib import Path

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


def escape_cell(value: str) -> str:
    """Escape generated values for one Markdown table cell."""
    return html.escape(value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " "))


def markdown_link(label: str, url: str | None) -> str:
    safe_url = urllib.parse.quote(url, safe=":/#?&=%@+;,") if url else None
    return f"[{escape_cell(label)}]({safe_url})" if safe_url else "-"


def render(states: list[dict]) -> str:
    states = sorted(states, key=lambda state: state["upstream"]["full_name"].casefold())
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
        "It is generated from the pipeline's durable state whenever the website is built.",
        "",
        "| Project | Tracking issue | Status | Development PR | Upstream PR | Updated (UTC) |",
        "|:--------|:---------------|:-------|:---------------|:------------|:--------------|",
    ]
    for state in states:
        full_name = state["upstream"]["full_name"]
        issue = state["tracking_issue"]["number"]
        fork = state.get("fork") or {}
        internal_url = None
        if fork.get("name") and fork.get("pr_number"):
            internal_url = f"https://github.com/{fork['name']}/pull/{fork['pr_number']}"
        upstream_pr = state.get("upstream_pr") or {}
        updated = datetime.fromisoformat(state["updated_at"]).strftime("%Y-%m-%d %H:%M")
        cells = [
            markdown_link(full_name, f"https://github.com/{full_name}"),
            markdown_link(f"#{issue}", f"https://github.com/riseproject-dev/porting-agent/issues/{issue}"),
            escape_cell(status_label(state)),
            markdown_link(f"#{fork['pr_number']}", internal_url) if internal_url else "-",
            markdown_link(f"#{upstream_pr['number']}", upstream_pr.get("url")) if upstream_pr else "-",
            updated,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.extend(
        [
            "",
            "Status changes are recorded by the automated porting pipeline. "
            "Use the tracking issue for the detailed history and available actions.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=Path(".state/contributions"))
    parser.add_argument("--output", type=Path, default=Path("docs/status/index.md"))
    args = parser.parse_args()
    states = [json.loads(path.read_text()) for path in args.state_dir.glob("*.json")]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(states))


if __name__ == "__main__":
    main()
