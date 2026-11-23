# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The reviewed contract of the canonical ciso8601 request, issue #1: intake
refuses that issue unless its identity and body are exactly the reviewed ones."""

from __future__ import annotations

import hashlib

ISSUE_ID = 6536262483
ISSUE_NUMBER = 1
ISSUE_TITLE = "[upstreaming] ciso8601 riscv64 wheel support"
EXPECTED_BODY = """### Upstream repository

https://github.com/closeio/ciso8601

### PyPI distributions

ciso8601 (import: ciso8601)

### Upstream target branch

master

### Additional notes

Migration canary based on https://github.com/riseproject-dev/python-wheels/issues/631.
The downstream riscv64 wheel build was added in python-wheels#625.
The earlier investigation found that PyPI ciso8601 2.4.1 had no riscv64 wheel,
upstream issue closeio/ciso8601#181 requested support, and the project is a pure
C extension using setuptools and cibuildwheel with no third-party native library.
The earlier proposal selected configuration_integration and these bounded paths:
.github/workflows/build-wheels.yml, pyproject.toml, CHANGELOG.md, and tox.ini.
The expected topology is an ubuntu-24.04-riscv row with CIBW_ARCHS=riscv64,
following the project's existing native aarch64 row.
The previous implementation reached handoff/waiting_human at revision 9 and
reported clean head 9339ec54ae594458b0b9c6039bb55e9b2b13eff7.
Treat all previous dossier, CI, fork, PR, and head data as migration evidence.
Do not treat it as current authorization or verified state without the migration
and readback gates in this plan.
"""
EXPECTED_DIGEST = "9f58bffa65d7e7df0206ae1130ce6eb5e218181b966839f2e609c05e8baa7ebb"

assert hashlib.sha256(EXPECTED_BODY.encode()).hexdigest() == EXPECTED_DIGEST


class CanaryError(ValueError):
    """The canonical request does not match its reviewed contract."""


def body_digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def validate_intake(issue: dict, body: str) -> None:
    """Prevent a fresh canonical canary from entering with edited content."""
    if issue.get("number") != ISSUE_NUMBER:
        return
    if issue.get("id") != ISSUE_ID or issue.get("title") != ISSUE_TITLE:
        raise CanaryError("canonical canary issue identity does not match the reviewed fixture")
    if body_digest(body) != EXPECTED_DIGEST or body != EXPECTED_BODY:
        raise CanaryError("canonical canary body does not match the reviewed fixture")
