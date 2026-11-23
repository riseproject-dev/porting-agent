# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Publish an exact verified planning Result as a readable issue comment."""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from common import CONTROL_REPO, canonical_json
from plan_gate import validate


class CommentError(ValueError):
    pass


def _text(value, limit=8000):
    return str(value).replace("\x00", "").replace("<!--", "&lt;!--").strip()[:limit]


def _names(values, limit: int) -> str:
    """A bounded list of short names, as one line."""
    values = values if isinstance(values, list) else []
    return ", ".join(_text(v, limit).replace("\n", " ") for v in values[:10]) or "none"


def _coverage(entry) -> str:
    """One path coverage entry as `area: paths (assessment)`."""
    if not isinstance(entry, dict):
        return _text(entry, 500)
    paths = entry.get("paths") if isinstance(entry.get("paths"), list) else []
    shown = ", ".join(f"`{_text(p, 200).replace('`', '')}`" for p in paths[:32]) or "none"
    return (f"{_text(entry.get('area', ''), 40)}: {shown} "
            f"({_text(entry.get('assessment', ''), 1000).replace(chr(10), ' ')})")


def marker(task: dict, result: dict) -> str:
    identity = {"contribution_id": task["contribution_id"], "revision": task["revision"],
                "work_id": result["work_id"], "claim_id": result["claim_id"],
                "artifact_id": result["artifact_id"], "digest": result["digest"]}
    return "<!-- upstreaming-planning-result:" + hashlib.sha256(canonical_json(identity).encode()).hexdigest() + " -->"


def render(plan: dict, task: dict, result: dict) -> str:
    lines = ["## Porting planning result", "", _text(plan["summary"]), ""]
    proposal = plan.get("proposal") or {}
    if proposal:
        lines += ["### Proposed direction", ""]
        for key, value in list(proposal.items())[:20]:
            label = _text(str(key).replace("_", " ").capitalize(), 100)
            if isinstance(value, (str, int, float, bool)) or value is None:
                lines.append(f"- **{label}:** {_text(value, 2000)}")
            elif key == "path_coverage" and isinstance(value, list):
                lines.append(f"- **{label}:**")
                lines += [f"  - {_coverage(entry)}" for entry in value[:20]]
            elif isinstance(value, list):
                lines.append(f"- **{label}:** " + "; ".join(_text(v, 500) for v in value[:20]))
        lines.append("")
    project = plan.get("project")
    if isinstance(project, dict):
        lines += ["### Project", ""]
        lines.append("- **Languages:** " + _names(project.get("languages"), 40))
        lines.append("- **Build systems:** " + _names(project.get("build_systems"), 100))
        lines.append(f"- **Build and test:** {_text(project.get('build_and_test', ''), 2000)}")
        lines.append(f"- **riscv64 toolchain:** {_text(project.get('riscv64_toolchain', ''), 2000)}")
        channels = project.get("release_channels")
        channels = channels if isinstance(channels, list) else []
        lines.append("- **Release channels:**" + ("" if channels else " none"))
        for channel in channels[:20]:
            if isinstance(channel, dict):
                lines.append(f"  - {_text(channel.get('kind', ''), 40)} "
                             f"`{_text(channel.get('name', ''), 200).replace('`', '')}`: "
                             f"riscv64 {_text(channel.get('riscv64', ''), 40)} "
                             f"({_text(channel.get('url', ''), 2000)})")
        lines.append("")
    lines += ["### Dependency and ecosystem evidence", "", _text(plan["dependency_graph"]["assessment"]), ""]
    report = plan["ecosystem_report"]
    if report["status"] == "available":
        lines += [f"Ecosystem report: {report['url']} (`{_text(report['revision'], 100)}`)", "",
                  _text(report["riscv64_status"]), ""]
    else:
        lines += ["No matching ecosystem report was available; the plan records the search evidence.", ""]
    lines += ["### Research coverage", ""]
    for category, record in plan["research"].items():
        lines.append(f"- **{category.replace('_', ' ').title()}**: {_text(record['findings'], 1000)}")
        links = [source["url"] for source in record["sources"][:5]]
        if links:
            lines.append("  Sources: " + ", ".join(links))
    if plan.get("history"):
        lines += ["", "### Relevant prior work", ""]
        for item in plan["history"][:30]:
            lines.append(f"- [{item['outcome']}]({item['url']}): {_text(item['decision'], 1000)}")
    lines += ["", f"Verified native Result: work `{result['work_id']}`, claim `{result['claim_id']}`, artifact `{result['artifact_id']}`.",
              "This is planning evidence, not an upstream approval or publication decision.", "", marker(task, result)]
    body = "\n".join(lines)
    if len(body.encode()) > 60_000:
        raise CommentError("rendered plan comment exceeds bounded size")
    return body


def request(method: str, path: str, token: str, body: dict | None = None):
    prefix = f"/repos/{CONTROL_REPO}/issues/"
    if method not in ("GET", "POST") or not token or not path.startswith(prefix) or ".." in path:
        raise CommentError("invalid comment API request")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("https://api.github.com" + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise CommentError(f"comment API HTTP {exc.code}") from None
    if len(raw) > 2 * 1024 * 1024:
        raise CommentError("oversized comment API response")
    return json.loads(raw) if raw else None


def trusted_runtime(env: dict[str, str]) -> bool:
    if env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != CONTROL_REPO:
        return False
    if env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("GITHUB_WORKFLOW") in (
        "Planner", "Publish Retry"
    ):
        return True
    return (
        env.get("GITHUB_WORKFLOW") == "Controller"
        and env.get("PORTING_PLAN_RECOVERY") == "1"
    )


def publish(plan: dict, proposal: dict, task: dict, result: dict, token: str, run_id: str,
            expected_author: str | None = None) -> dict:
    validate(plan, proposal, task)
    for key in ("work_id", "claim_id", "artifact_id", "digest"):
        if not result.get(key):
            raise CommentError(f"verified Result missing {key}")
    number = int(task["tracking_issue"])
    body = render(plan, task, result)
    identity = marker(task, result)
    comments = request("GET", f"/repos/{CONTROL_REPO}/issues/{number}/comments?per_page=100", token)
    matches = [comment for comment in comments if identity in (comment.get("body") or "")]
    if len(matches) > 1:
        raise CommentError("multiple comments have the exact Result marker")
    created = not matches
    comment = matches[0] if matches else request("POST", f"/repos/{CONTROL_REPO}/issues/{number}/comments", token, {"body": body})
    observed = request("GET", f"/repos/{CONTROL_REPO}/issues/comments/{comment['id']}", token)
    if observed.get("body") != body or created and expected_author and (observed.get("user") or {}).get("login") != expected_author:
        raise CommentError("comment readback does not match verified Result and App author")
    return {"schema_version": 1, "status": "published", "contribution_id": task["contribution_id"],
            "revision": task["revision"], "work_id": result["work_id"], "claim_id": result["claim_id"],
            "artifact_id": result["artifact_id"], "comment_id": observed["id"],
            "comment_url": observed["html_url"], "content_digest": hashlib.sha256(body.encode()).hexdigest(),
            "notification_run_id": int(run_id)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "proposal", "task", "result"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not trusted_runtime(os.environ):
        raise CommentError("plan comments require a trusted porting Result workflow")
    values = [json.loads(getattr(args, name).read_text()) for name in ("plan", "proposal", "task", "result")]
    receipt = publish(*values, os.environ.get("GH_TOKEN", ""), os.environ["GITHUB_RUN_ID"],
                      os.environ.get("PORTING_COMMENT_AUTHOR") or None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, indent=2))
