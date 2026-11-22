#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Fail unless a normalized read-only repository settings snapshot matches policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

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


def audit(snapshot: Path, expected_path: Path = DEFAULT_EXPECTED) -> list[str]:
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    actual = json.loads(snapshot.read_text(encoding="utf-8"))
    return differences(expected, actual)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED)
    args = parser.parse_args(argv)
    issues = audit(args.snapshot, args.expected)
    for issue in issues:
        print(issue)
    if issues:
        return 1
    print(f"repository settings match {args.expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
