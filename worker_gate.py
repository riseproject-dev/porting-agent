# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Trusted structural gate for uncredentialed worker proposal artifacts."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
from pathlib import Path

import schemas
from common import validate_scope_paths
from work import WRITE_TASKS

MAX_MBOX_BYTES = 4 * 1024 * 1024
FORBIDDEN_PATCH_TEXT = (
    "pull_request_target",
    "${{ secrets.",
    "permissions: write-all",
)


class WorkerGateError(ValueError):
    pass


def _regular(path: Path, *, required: bool) -> bytes | None:
    if not path.exists() and not required:
        return None
    if path.is_symlink() or not path.is_file():
        raise WorkerGateError(f"{path.name}: expected regular file")
    data = path.read_bytes()
    if len(data) > MAX_MBOX_BYTES:
        raise WorkerGateError(f"{path.name}: exceeds artifact limit")
    return data


def _patch_paths(text: str) -> list[str]:
    paths = []
    for line in text.splitlines():
        if not line.startswith("diff --git "):
            continue
        match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
        if not match or match.group(1) != match.group(2):
            raise WorkerGateError("mbox contains a quoted path or rename")
        path = match.group(1)
        if path not in paths:
            paths.append(path)
    return paths


def validate(task: dict, proposal: dict, mbox_path: Path) -> dict:
    schemas.validate("proposal", proposal)
    if task.get("task") not in WRITE_TASKS:
        raise WorkerGateError("worker gate accepts only patch tasks")
    for proposal_key, task_key in (
        ("contribution_id", "contribution_id"),
        ("expected_revision", "revision"),
        ("expected_head", "expected_head"),
    ):
        if proposal.get(proposal_key) != task.get(task_key):
            raise WorkerGateError(f"proposal {proposal_key} does not match task")
    if proposal.get("ref") != task.get("ref"):
        raise WorkerGateError("proposal ref does not match task")
    operation = proposal.get("operation")
    if operation not in (task.get("allowed_operations") or []):
        if not (operation == "none" and proposal.get("outcome") != "patch"):
            raise WorkerGateError("proposal operation is not allowed")

    allowed = validate_scope_paths(list(task.get("allowed_paths") or []))
    changed = list(proposal.get("changed_files") or [])
    if len(changed) != len(set(changed)) or len({path.casefold() for path in changed}) != len(changed):
        raise WorkerGateError("proposal changed_files contains duplicates or case collisions")
    validate_scope_paths(changed) if changed else None
    outside = [path for path in changed if not any(fnmatch.fnmatchcase(path, item) for item in allowed)]
    if outside:
        raise WorkerGateError(f"proposal paths outside approved scope: {outside[:3]}")

    required_mbox = operation in {"append", "rewrite"}
    mbox = _regular(mbox_path, required=required_mbox)
    actual_paths = []
    if required_mbox:
        if not mbox or not mbox.startswith(b"From "):
            raise WorkerGateError("patch operation requires a format-patch mbox")
        digest = hashlib.sha256(mbox).hexdigest()
        if proposal.get("patch_digest") != digest:
            raise WorkerGateError("patch digest mismatch")
        text = mbox.decode("utf-8", "strict")
        actual_paths = _patch_paths(text)
        if sorted(actual_paths) != sorted(changed):
            raise WorkerGateError("mbox paths do not match proposal changed_files")
        if task.get("upstream_facing") and "{debug}" in text:
            raise WorkerGateError("debug commit is forbidden on an upstream-facing branch")
        additions = "\n".join(
            line for line in text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        if any(value in additions for value in FORBIDDEN_PATCH_TEXT):
            raise WorkerGateError("patch adds forbidden workflow content")
    elif mbox is not None or proposal.get("patch_digest") is not None or changed:
        raise WorkerGateError("no-change proposal must not carry patch bytes or changed files")

    reproduction = proposal.get("reproduction")
    if not isinstance(reproduction, dict):
        raise WorkerGateError("worker proposal lacks reproduction evidence")
    image = reproduction.get("image", "")
    if not re.fullmatch(
        r"ghcr\.io/riseproject-dev/porting-agent/ubuntu-24\.04-riscv@sha256:[0-9a-f]{64}",
        image,
    ):
        raise WorkerGateError("reproduction image is not exact-digest pinned")
    if reproduction.get("architecture") != "riscv64":
        raise WorkerGateError("reproduction architecture is not riscv64")
    commands = reproduction.get("commands") or []
    if not commands or not all(isinstance(item.get("exit_status"), int) for item in commands):
        raise WorkerGateError("reproduction command evidence is incomplete")
    workflows = reproduction.get("workflows") or []
    if not workflows or any(not item.get("jobs") or not item.get("runner_mappings") for item in workflows):
        raise WorkerGateError("workflow selection or runner mapping evidence is incomplete")
    return {
        "status": "accepted",
        "changed_files": actual_paths,
        "patch_digest": proposal.get("patch_digest"),
        "image": image,
        "commands": len(commands),
        "workflows": len(workflows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker_gate")
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--mbox", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        task = json.loads(args.task.read_text())
        proposal = json.loads(args.proposal.read_text())
        report = validate(task, proposal, args.mbox)
        code = 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, schemas.SchemaError,
            WorkerGateError, ValueError) as error:
        report = {"status": "rejected", "reason": str(error)[:2000]}
        code = 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
