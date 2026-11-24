# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Append actionable App-authored replies for rejected issue commands."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import commands
import engine
from common import CONTROL_REPO
from status_ledger import Api, LedgerError, issue_comments

TEMPLATES = {
    "refresh-override": commands.OVERRIDE_FORM,
    "submit-upstream": "/upstreaming submit-upstream revision=N head=FULL_SHA",
    "retract-pr": "/upstreaming retract-pr revision=N head=FULL_SHA",
    "pause": '/upstreaming pause revision=N reason="..."',
    "park": '/upstreaming park revision=N reason="..."',
    "abandon": '/upstreaming abandon revision=N reason="..."',
    "refresh": '/upstreaming refresh revision=N reason="..."',
    "resume": '/upstreaming resume revision=N reason="..."',
    "resolve-operation": commands.RESOLVE_FORM,
}
# A retired command is answered with the form of the command that replaced it.
REPLACEMENTS = {
    "request-handoff": "submit-upstream",
    "approve-handoff": "submit-upstream",
    "register-upstream-pr": "submit-upstream",
    "block": "pause",
    "approve-scope": "refresh",
    "provision": "resume",
    "restart": "refresh",
    "clarify": "resume",
    "revise-scope": "refresh",
    "force-develop": "refresh-override",
}
# A rejection that moves the command to another form, by reason: a resume in
# `parked.idle` is answered with the refresh that starts a new generation, and
# a resolution that names `evidence` with the form that states a `reason`.
MOVED = {
    commands.PARKED_RESUME: "refresh",
    **{reason: "refresh-override" for (verb, _name), reason in commands.MOVED_FIELDS.items()
       if verb == "resume"},
    commands.MOVED_FIELDS[("resolve-operation", "evidence")]: "resolve-operation",
}


def _verb(body: str, receipt: dict) -> str:
    if receipt.get("verb"):
        return receipt["verb"]
    match = re.match(r"\s*/upstreaming\s+([a-z][a-z-]*)", body)
    return match.group(1) if match else ""


def render(receipt: dict, original: dict) -> str:
    login = (receipt.get("actor") or {}).get("login") or (original.get("user") or {}).get("login")
    if not login or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login):
        raise LedgerError("rejected command has no valid author login")
    body = (original.get("body") or "").strip()[:8000]
    quoted = "\n".join("> " + line for line in body.splitlines())
    reason = (receipt.get("reason") or receipt.get("result") or "command rejected").strip()
    text = f"@{login}, this command was rejected:\n\n{quoted}\n\n---\n\n**Reason:** {reason}"
    verb = _verb(body, receipt)
    if reason == "already paused":
        revision = receipt.get("revision")
        suffix = f" revision={revision}" if revision is not None else " revision=N"
        text += ("\n\n**No action is needed:** the contribution remains paused. "
                 "To continue it, use:\n```text\n"
                 f'/upstreaming resume{suffix} reason="..."\n```')
        return text + f"\n\n<!-- upstreaming-command-feedback:{receipt['comment_id']} -->"
    if reason == "not paused" and verb == "resume":
        text += "\n\n**No action is needed:** the contribution is already active."
        return text + f"\n\n<!-- upstreaming-command-feedback:{receipt['comment_id']} -->"
    if reason == commands.ENDED_RESUME:
        text += "\n\n**Nothing was changed.**"
        return text + f"\n\n<!-- upstreaming-command-feedback:{receipt['comment_id']} -->"
    if reason in engine.HELD_COMMANDS.values():
        text += ("\n\n**Nothing was changed.** The command was well-formed; no other form of it "
                 "is accepted.")
        return text + f"\n\n<!-- upstreaming-command-feedback:{receipt['comment_id']} -->"
    template = TEMPLATES.get(MOVED.get(reason) or REPLACEMENTS.get(verb, verb))
    if template:
        head = re.search(r"(?:^|\s)head=([0-9a-f]{40})(?:\s|$)", body)
        if head:
            template = template.replace("head=FULL_SHA", f"head={head.group(1)}")
        revision = receipt.get("revision")
        if revision is None:
            match = re.search(r"(?:^|\s)revision=(\d+)(?:\s|$)", body)
            revision = int(match.group(1)) if match else None
        if revision is not None:
            template = template.replace("revision=N", f"revision={revision}")
        text += f"\n\n**Expected form:**\n```text\n{template}\n```"
    return text + f"\n\n<!-- upstreaming-command-feedback:{receipt['comment_id']} -->"


def publish(root: Path, token: str) -> list[dict]:
    api = Api(token)
    receipts = []
    cache: dict[int, list[dict]] = {}
    paths = sorted((root / ".state/commands").glob("*.json"))
    for path in paths[-512:]:
        receipt = json.loads(path.read_text())
        if receipt.get("result") != "rejected":
            continue
        issue = receipt["issue_number"]
        marker = f"<!-- upstreaming-command-feedback:{receipt['comment_id']} -->"
        comments = cache.setdefault(issue, issue_comments(api, issue))
        existing = next((item for item in comments if marker in (item.get("body") or "")), None)
        original = api.request("GET", f"/repos/{CONTROL_REPO}/issues/comments/{receipt['comment_id']}")
        body = render(receipt, original)
        status = "created"
        if existing:
            if (existing.get("user") or {}).get("login") != "riseproject-dev[bot]":
                raise LedgerError("command feedback marker is not App-authored")
            if existing.get("body") == body:
                receipts.append({"command_id": receipt["command_id"], "comment_id": existing["id"], "status": "existing"})
                continue
            comment = api.request(
                "PATCH",
                f"/repos/{CONTROL_REPO}/issues/comments/{existing['id']}",
                {"body": body},
            )
            status = "updated"
        else:
            comment = api.request("POST", f"/repos/{CONTROL_REPO}/issues/{issue}/comments", {"body": body})
        observed = api.request("GET", f"/repos/{CONTROL_REPO}/issues/comments/{comment['id']}")
        if observed.get("body") != body or (observed.get("user") or {}).get("login") != "riseproject-dev[bot]":
            raise LedgerError("command feedback readback or App author mismatch")
        if not existing:
            comments.append(observed)
        receipts.append({"command_id": receipt["command_id"], "comment_id": observed["id"], "status": status})
    return receipts


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = publish(args.root, os.environ.get("GH_TOKEN", ""))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
