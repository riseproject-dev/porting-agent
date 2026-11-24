#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Fail unless a normalized read-only repository settings snapshot matches policy.

``--live OWNER/REPO`` instead reads the names of the Actions secrets the
repository can use on GitHub (its own, the organization's visible to it, and
its environments') with the read-only ``gh api`` and fails when any is a
``GH_AW_*`` secret.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPECTED = ROOT / ".github" / "repository-settings.json"


def differences(expected: Any, actual: Any, path: str = "$") -> list[str]:
    """Return stable, human-readable differences between normalized documents."""
    if type(expected) is not type(actual):
        return [f"{path}: expected {type(expected).__name__}, got {type(actual).__name__}"]
    if isinstance(expected, dict):
        issues: list[str] = []
        for key in sorted(expected.keys() - actual.keys()):
            issues.append(f"{path}.{key}: missing")
        for key in sorted(actual.keys() - expected.keys()):
            issues.append(f"{path}.{key}: unexpected")
        for key in sorted(expected.keys() & actual.keys()):
            issues.extend(differences(expected[key], actual[key], f"{path}.{key}"))
        return issues
    if isinstance(expected, list):
        if expected == actual:
            return []
        return [f"{path}: expected {expected!r}, got {actual!r}"]
    if expected != actual:
        return [f"{path}: expected {expected!r}, got {actual!r}"]
    return []


# The planner and worker agents run outside the sandbox and can read every
# credential their agent job holds. gh-aw hands `GH_AW_GITHUB_TOKEN` and
# `GH_AW_GITHUB_MCP_SERVER_TOKEN` to that job when they are set, so no
# `GH_AW_*` secret may reach the repository: gh-aw then falls back to the
# job's read-only `GITHUB_TOKEN`. See docs/state-machine/known-gaps.md, "The
# planner and worker agents run on the runner, outside the sandbox".
FORBIDDEN_SECRET_PREFIX = "GH_AW_"


def forbidden_secrets(settings: dict) -> list[str]:
    """Every ``GH_AW_*`` secret a settings document lists: repository,
    selected organization and environment secrets."""
    places: list[tuple[str, Any]] = [
        ("$.repositorySecrets", settings.get("repositorySecrets")),
        ("$.selectedOrganizationSecrets", settings.get("selectedOrganizationSecrets")),
    ]
    for name, environment in sorted((settings.get("environments") or {}).items()):
        if isinstance(environment, dict):
            places.append((f"$.environments.{name}.secrets", environment.get("secrets")))
    return [f"{path}: {secret} is a {FORBIDDEN_SECRET_PREFIX}* secret, which the unsandboxed "
            "planner and worker agents could read"
            for path, names in places for secret in (names or [])
            if isinstance(secret, str) and secret.startswith(FORBIDDEN_SECRET_PREFIX)]


def audit(snapshot: Path, expected_path: Path = DEFAULT_EXPECTED) -> list[str]:
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    actual = json.loads(snapshot.read_text(encoding="utf-8"))
    return forbidden_secrets(expected) + forbidden_secrets(actual) + differences(expected, actual)


def _gh_api(path: str) -> list[dict]:
    """Every page of a read-only GitHub REST listing, through ``gh api``."""
    out = subprocess.run(["gh", "api", "--paginate", "--slurp", path],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def live_settings(repository: str, api: Callable[[str], list[dict]] = _gh_api) -> dict:
    """The names of the Actions secrets ``repository`` can use, in the shape
    of the settings document: its own, the organization's visible to it, and
    those of each of its environments."""
    def names(pages: list[dict], key: str) -> list[str]:
        return sorted(item["name"] for page in pages for item in page.get(key) or [])

    environments = names(api(f"repos/{repository}/environments"), "environments")
    return {
        "repositorySecrets": names(api(f"repos/{repository}/actions/secrets"), "secrets"),
        "selectedOrganizationSecrets": names(
            api(f"repos/{repository}/actions/organization-secrets"), "secrets"),
        "environments": {env: {"secrets": names(
            api(f"repos/{repository}/environments/{quote(env, safe='')}/secrets"), "secrets")}
            for env in environments},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot", type=Path)
    mode.add_argument("--live", metavar="OWNER/REPO",
                      help="check the secrets the repository can use on GitHub for GH_AW_* names")
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED)
    args = parser.parse_args(argv)
    if args.live:
        issues = forbidden_secrets(live_settings(args.live))
        for issue in issues:
            print(issue)
        if issues:
            return 1
        print(f"no {FORBIDDEN_SECRET_PREFIX}* secret reaches {args.live}")
        return 0
    issues = audit(args.snapshot, args.expected)
    for issue in issues:
        print(issue)
    if issues:
        return 1
    print(f"repository settings match {args.expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
