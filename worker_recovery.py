# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Normalize worker outcomes that an earlier trusted gate misclassified.

* A structurally valid infrastructure blocker becomes ``worker_blocked`` for
  bounded fresh retry.
* A valid exact-task ``no_change`` proposal that an earlier worker gate
  rejected as a validation failure becomes ``no_change`` (plan 8.6). The
  current gate and trusted preflight are re-run on the stored artifact files;
  nothing is synthesized and no destination token is involved.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import fork_publish
import schemas
import worker_gate
from common import canonical_json


class WorkerRecoveryError(ValueError):
    pass


def _load(directory: Path, name: str) -> dict:
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
        raise WorkerRecoveryError(f"{name}: invalid trusted publication file")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise WorkerRecoveryError(f"{name}: expected object")
    return value


def normalize(directory: Path, run_id: str) -> dict:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id):
        raise WorkerRecoveryError("invalid worker run ID")
    task = _load(directory, "task.json")
    result = _load(directory, "result.json")
    proposal = _load(directory, "proposal.json")
    outcome = _load(directory, "outcome.json")
    if task.get("task") not in {"development_patch", "development_repair", "upstream_revision"}:
        return outcome
    if str(result.get("run_id")) != run_id or result.get("run_attempt") != 1:
        raise WorkerRecoveryError("worker Result does not match run attempt")
    schemas.validate("proposal", proposal)
    if _misclassified_no_change(outcome, proposal):
        return _recover_no_change(directory, run_id, task, proposal, outcome)
    if not (
        outcome.get("status") == "proposal"
        and proposal.get("outcome") == "blocked"
        and proposal.get("operation") == "none"
        and str(proposal.get("failure_cluster") or "").startswith(
            "reproduction-image-infrastructure"
        )
    ):
        return outcome
    report = worker_gate.validate(task, proposal, directory / "patches.mbox")
    if report.get("status") != "infrastructure_blocked":
        raise WorkerRecoveryError("blocked proposal did not pass infrastructure classification")
    normalized = {
        "status": "worker_blocked",
        "failure_class": "infrastructure",
        "reason": proposal.get("diagnosis"),
        "stage": "worker_gate",
    }
    (directory / "outcome.json").write_text(canonical_json(normalized))
    (directory / "recovered-worker-blocker").write_text(
        f"run_id={run_id}\nfailure_cluster={proposal['failure_cluster']}\n"
    )
    return normalized


def _misclassified_no_change(outcome: dict, proposal: dict) -> bool:
    return (
        outcome.get("status") == "rejected"
        and outcome.get("failure_class") == "validation"
        and outcome.get("stage") == "worker_gate"
        and proposal.get("outcome") == "no_change"
        and proposal.get("operation") == "none"
    )


def _recover_no_change(directory: Path, run_id: str, task: dict, proposal: dict,
                       outcome: dict) -> dict:
    """Re-run the current gate and preflight; keep the rejection otherwise."""
    if not (directory / "record.json").is_file():
        return outcome
    record = _load(directory, "record.json")
    try:
        report = worker_gate.validate(task, proposal, directory / "patches.mbox")
    except (worker_gate.WorkerGateError, schemas.SchemaError, ValueError):
        return outcome
    if report.get("status") != "accepted":
        return outcome

    def no_fork_lookup(_name: str) -> dict:
        raise WorkerRecoveryError("no-change recovery never reads the fork")

    try:
        preflight = fork_publish.preflight(record=record, task=task, proposal=proposal,
                                           fork_lookup=no_fork_lookup)
    except WorkerRecoveryError:
        return outcome
    if preflight.get("status") != "no_change" or preflight.get("publish") is not False:
        return outcome
    normalized = {
        "status": "no_change",
        "failure_class": None,
        "reason": preflight.get("reason"),
        "stage": "preflight",
    }
    (directory / "outcome.json").write_text(canonical_json(normalized))
    (directory / "recovered-worker-no-change").write_text(f"run_id={run_id}\n")
    return normalized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker_recovery")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        outcome = normalize(args.directory, args.run_id)
    except (OSError, json.JSONDecodeError, schemas.SchemaError, worker_gate.WorkerGateError,
            WorkerRecoveryError, ValueError) as error:
        print(f"::error::{error}")
        return 1
    print(json.dumps(outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
