#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Bounded discovery and exact readback for one GitHub Pages artifact."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path


class PagesArtifactError(RuntimeError):
    pass


def _matches(artifact: dict, run_id: int) -> bool:
    workflow_run = artifact.get("workflow_run") or {}
    return (
        artifact.get("name") == "github-pages"
        and artifact.get("expired") is False
        and workflow_run.get("id") == run_id
    )


def discover(
    run_id: int,
    fetch: Callable[[], dict],
    *,
    attempts: int,
    sleep: Callable[[float], None] = time.sleep,
    delay_seconds: int = 5,
) -> int:
    """Return one exact artifact ID after bounded eventual-consistency polling."""
    for attempt in range(1, attempts + 1):
        try:
            payload = fetch()
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            payload = {"artifacts": []}
        artifacts = payload.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise PagesArtifactError("artifact inventory is malformed")
        candidates = [
            item for item in artifacts if isinstance(item, dict) and _matches(item, run_id)
        ]
        if len(candidates) > 1:
            raise PagesArtifactError("multiple github-pages artifacts for this run")
        if len(candidates) == 1:
            artifact_id = candidates[0].get("id")
            if not isinstance(artifact_id, int):
                raise PagesArtifactError("github-pages artifact has no integer ID")
            return artifact_id
        if attempt < attempts:
            sleep(attempt * delay_seconds)
    raise PagesArtifactError("github-pages artifact unavailable after bounded discovery")


def verify(
    artifact_id: int,
    run_id: int,
    fetch: Callable[[], dict],
    *,
    attempts: int,
    sleep: Callable[[float], None] = time.sleep,
    delay_seconds: int = 10,
) -> int:
    """Require exact ID/name/run/non-expired readback before deployment retry."""
    for attempt in range(1, attempts + 1):
        try:
            artifact = fetch()
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            artifact = {}
        if artifact.get("id") == artifact_id and _matches(artifact, run_id):
            return artifact_id
        if attempt < attempts:
            sleep(attempt * delay_seconds)
    raise PagesArtifactError("exact github-pages artifact readback unavailable for retry")


def _gh_json(path: str) -> dict:
    output = subprocess.run(
        ["gh", "api", path], check=True, capture_output=True, text=True
    ).stdout
    value = json.loads(output)
    if not isinstance(value, dict):
        raise PagesArtifactError("GitHub artifact response is not an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("discover", "verify"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--artifact-id", type=int)
    parser.add_argument("--attempts", type=int, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.attempts <= 10:
        parser.error("attempts must be in 1..10")
    try:
        if args.mode == "discover":
            artifact_id = discover(
                args.run_id,
                lambda: _gh_json(
                    f"/repos/{args.repo}/actions/runs/{args.run_id}/artifacts?per_page=100"
                ),
                attempts=args.attempts,
            )
            if args.github_output is None:
                parser.error("discover requires --github-output")
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(f"artifact-id={artifact_id}\n")
        else:
            if args.artifact_id is None:
                parser.error("verify requires --artifact-id")
            artifact_id = verify(
                args.artifact_id,
                args.run_id,
                lambda: _gh_json(
                    f"/repos/{args.repo}/actions/artifacts/{args.artifact_id}"
                ),
                attempts=args.attempts,
            )
    except PagesArtifactError as error:
        print(f"::error::{error}")
        return 1
    print(artifact_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
