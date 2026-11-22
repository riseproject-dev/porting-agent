# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Recover a trusted investigation publication interrupted after Result resolution."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from common import canonical_json
from dossier_gate import validate
from queue_result import PROPOSAL_ARTIFACT_SUFFIX

MAX_FILE_BYTES = 512 * 1024


class InvestigationRecoveryError(ValueError):
    pass


def _load(directory: Path, name: str) -> dict:
    path = directory / name
    if path.is_symlink() or not path.is_file():
        raise InvestigationRecoveryError(f"{name}: expected one regular file")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise InvestigationRecoveryError(f"{name}: oversized")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvestigationRecoveryError(f"{name}: invalid JSON") from error
    if not isinstance(value, dict):
        raise InvestigationRecoveryError(f"{name}: expected object")
    return value


def recover(directory: Path, run_id: str) -> dict:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
        raise InvestigationRecoveryError("invalid investigator run ID")
    task = _load(directory, "task.json")
    result = _load(directory, "result.json")
    proposal = _load(directory, "proposal.json")
    dossier = _load(directory, "dossier.json")
    if task.get("task") != "investigation":
        raise InvestigationRecoveryError("recovery accepts only investigation tasks")
    if str(result.get("run_id")) != str(run_id) or result.get("run_attempt") != 1:
        raise InvestigationRecoveryError("verified Result does not match investigator run attempt")
    for key in ("work_id", "claim_id", "artifact_id", "name", "digest", "result_commit"):
        if not result.get(key):
            raise InvestigationRecoveryError(f"verified Result missing {key}")
    if not re.fullmatch(r"[0-9a-f]{64}", str(result["work_id"])):
        raise InvestigationRecoveryError("verified Result Work ID is malformed")
    if not re.fullmatch(r"c_[0-9a-f]{64}_[1-9][0-9]*", str(result["claim_id"])):
        raise InvestigationRecoveryError("verified Result Claim ID is malformed")
    if not re.fullmatch(r"q_[0-9a-f]{64}", str(result["result_commit"])):
        raise InvestigationRecoveryError("verified Result commit ID is malformed")
    if not str(result["name"]).endswith("-" + PROPOSAL_ARTIFACT_SUFFIX):
        raise InvestigationRecoveryError("verified Result artifact has wrong suffix")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(result["digest"])):
        raise InvestigationRecoveryError("verified Result artifact digest is malformed")
    validate(dossier, proposal, task)
    outcome = {
        "status": "investigation",
        "failure_class": None,
        "reason": None,
        "stage": "result",
    }
    (directory / "outcome.json").write_text(canonical_json(outcome), encoding="utf-8")
    (directory / "recovered-investigation").write_text(
        f"run_id={run_id}\nresult_commit={result['result_commit']}\n",
        encoding="utf-8",
    )
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m investigation_recovery")
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        outcome = recover(args.directory, args.run_id)
    except InvestigationRecoveryError as error:
        print(f"::error::{error}")
        return 1
    print(json.dumps(outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
