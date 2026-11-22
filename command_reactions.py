# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Project durable command results to idempotent terminal reactions."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from common import CONTROL_REPO
from status_ledger import Api, LedgerError


def _retry(call, attempts: int = 4):
    delay = 1
    for attempt in range(attempts):
        try:
            return call()
        except (LedgerError, OSError):
            if attempt + 1 == attempts:
                raise
            time.sleep(delay)
            delay *= 2


def publish(root: Path, token: str) -> list[dict]:
    api = Api(token)
    results = []
    paths = sorted((root / ".state/commands").glob("*.json"))[-512:]
    terminal = {"accepted": "+1", "rejected": "-1"}
    bot = "riseproject-dev[bot]"
    for path in paths:
        receipt = json.loads(path.read_text())
        target = terminal.get(receipt.get("result"))
        if target is None:
            continue
        opposite = "-1" if target == "+1" else "+1"
        comment_id = int(receipt["comment_id"])
        endpoint = f"/repos/{CONTROL_REPO}/issues/comments/{comment_id}/reactions"
        reactions = _retry(lambda: api.request("GET", endpoint + "?per_page=100"))
        removed_opposite = False
        for item in reactions:
            if item.get("content") == opposite and (item.get("user") or {}).get("login") == bot:
                reaction_id = item.get("id")
                if not isinstance(reaction_id, int):
                    raise LedgerError("opposite command reaction has no stable ID")
                _retry(lambda reaction_id=reaction_id: api.request(
                    "DELETE", f"{endpoint}/{reaction_id}"))
                removed_opposite = True
        exists = any(
            item.get("content") == target and (item.get("user") or {}).get("login") == bot
            for item in reactions
        )
        if not exists:
            created = _retry(lambda: api.request("POST", endpoint, {"content": target}))
            if created.get("content") != target or (created.get("user") or {}).get("login") != bot:
                raise LedgerError("command completion reaction readback or App author mismatch")
            created_target = True
        else:
            created_target = False
        observed = _retry(lambda: api.request("GET", endpoint + "?per_page=100"))
        if not any(
            item.get("content") == target and (item.get("user") or {}).get("login") == bot
            for item in observed
        ) or any(
            item.get("content") == opposite and (item.get("user") or {}).get("login") == bot
            for item in observed
        ):
            raise LedgerError("terminal command reaction readback mismatch")
        status = "updated" if removed_opposite else "created" if created_target else "existing"
        results.append({"command_id": receipt["command_id"], "comment_id": comment_id, "status": status})
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = publish(args.root, os.environ.get("GH_TOKEN", ""))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
