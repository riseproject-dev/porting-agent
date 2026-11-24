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
import sys
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


class LiveAuditError(Exception):
    """A GitHub listing the live audit needs could not be read in full."""


def _gh_api(path: str) -> list[dict]:
    """Every page of a read-only GitHub REST listing, through ``gh api``."""
    try:
        result = subprocess.run(["gh", "api", "--paginate", "--slurp", path],
                                capture_output=True, text=True)
    except OSError as error:
        raise LiveAuditError(f"gh api {path}: {error}") from error
    if result.returncode != 0:
        raise LiveAuditError(f"gh api {path} exited with {result.returncode}: "
                             f"{result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise LiveAuditError(f"gh api {path} returned no JSON: {error}") from error


def live_settings(repository: str, api: Callable[[str], list[dict]] = _gh_api) -> dict:
    """The names of the Actions secrets ``repository`` can use, in the shape
    of the settings document: its own, the organization's visible to it, and
    those of each of its environments. Fails closed: a listing that is not
    a list of pages each carrying ``key`` as a list of named items raises
    ``LiveAuditError`` rather than reading as no secrets."""
    def names(path: str, key: str) -> list[str]:
        pages = api(path)
        if not isinstance(pages, list) or not pages:
            raise LiveAuditError(f"{path}: expected a list of pages, got {pages!r}")
        found = []
        for page in pages:
            items = page.get(key) if isinstance(page, dict) else None
            if not isinstance(items, list):
                raise LiveAuditError(f"{path}: a page has no {key!r} list: {page!r}")
            for item in items:
                if not (isinstance(item, dict) and isinstance(item.get("name"), str)):
                    raise LiveAuditError(f"{path}: an item of {key!r} has no name: {item!r}")
                found.append(item["name"])
        return sorted(found)

    environments = names(f"repos/{repository}/environments", "environments")
    return {
        "repositorySecrets": names(f"repos/{repository}/actions/secrets", "secrets"),
        "selectedOrganizationSecrets": names(
            f"repos/{repository}/actions/organization-secrets", "secrets"),
        "environments": {env: {"secrets": names(
            f"repos/{repository}/environments/{quote(env, safe='')}/secrets", "secrets")}
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
        try:
            issues = forbidden_secrets(live_settings(args.live))
        except LiveAuditError as error:
            print(f"live audit of {args.live} failed: {error}", file=sys.stderr)
            return 2
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
