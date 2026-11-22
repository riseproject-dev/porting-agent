# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Apply and exactly read back the one reviewed canonical canary repair."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import canary
import state
from common import CONTROL_REPO, load_policy, repository_root


class RecoveryError(RuntimeError):
    pass


class IssueApi:
    """One-resource issue client; every other method or path is rejected."""

    path = f"/repos/{CONTROL_REPO}/issues/{canary.ISSUE_NUMBER}"

    def __init__(self, token: str):
        if not token:
            raise RecoveryError("control App issue token required")
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if method not in {"GET", "PATCH"} or path != self.path:
            raise RecoveryError("invalid canary issue API request")
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            raise RecoveryError(f"canary issue API HTTP {error.code}") from None
        if len(raw) > 2 * 1024 * 1024:
            raise RecoveryError("oversized canary issue response")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RecoveryError("malformed canary issue response")
        return value


def _record_by_id(records: dict[str, dict], key: str, value: str) -> dict:
    matches = [record for record in records.values() if record.get(key) == value]
    if len(matches) != 1:
        raise RecoveryError(f"expected exactly one durable {key}={value}")
    return matches[0]


def apply(
    root: Path,
    operation_id: str,
    api: IssueApi,
    *,
    now: str,
) -> dict:
    """Repair or discover the issue effect, then stage an exact durable receipt."""
    policy = load_policy()
    store = state.StateStore(root, policy)
    operation = _record_by_id(store.load_all("operations"), "operation_id", operation_id)
    record = _record_by_id(
        store.load_all("contributions"), "contribution_id", canary.CONTRIBUTION_ID
    )
    try:
        canary.validate_operation(operation)
    except canary.CanaryError as error:
        raise RecoveryError(str(error)) from error
    if operation["status"] == "confirmed":
        if not canary.is_repaired(record):
            raise RecoveryError("confirmed canary operation does not match repaired state")
        return {
            "operation_id": operation_id,
            "status": "already_confirmed",
            "effect": "none",
            "state_changed": False,
            "body_digest": canary.EXPECTED_DIGEST,
        }
    try:
        canary.validate_mismatch(record, operation_id=operation_id)
    except canary.CanaryError as error:
        raise RecoveryError(str(error)) from error

    issue = api.request("GET", IssueApi.path)
    try:
        observed = canary.validate_issue(
            issue, {canary.INCOMPLETE_DIGEST, canary.EXPECTED_DIGEST}
        )
    except canary.CanaryError as error:
        raise RecoveryError(str(error)) from error
    effect = "discovered"
    if observed == canary.INCOMPLETE_DIGEST:
        # The planned operation is already durable. This is the only mutation,
        # and the response is not trusted as its receipt.
        api.request("PATCH", IssueApi.path, {"body": canary.EXPECTED_BODY})
        effect = "applied"

    # GET readback is mandatory even when PATCH returned an apparently valid
    # resource, and also recovers a prior lost PATCH response without replay.
    readback = api.request("GET", IssueApi.path)
    try:
        digest = canary.validate_issue(readback, {canary.EXPECTED_DIGEST})
    except canary.CanaryError as error:
        raise RecoveryError(f"canary issue exact readback failed: {error}") from error

    repaired, confirmed, event = canary.finalize(record, operation, now)
    store.stage("operations", confirmed)
    store.stage("contributions", repaired)
    store.stage("events", event)
    written = store.write_staged()
    return {
        "operation_id": operation_id,
        "status": "confirmed",
        "effect": effect,
        "state_changed": True,
        "issue_id": canary.ISSUE_ID,
        "body_digest": digest,
        "written": written,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m canary_recovery")
    parser.add_argument("--root", type=Path, default=repository_root())
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = apply(
            args.root,
            args.operation_id,
            IssueApi(os.environ.get("GH_TOKEN", "")),
            now=datetime.now(timezone.utc).isoformat(),
        )
    except (RecoveryError, state.StateError) as error:
        print(f"::error::{error}")
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
