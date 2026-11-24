# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Constants, policy loading and small shared helpers."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = REPOSITORY_ROOT / "schemas"
POLICY_PATH = REPOSITORY_ROOT / "policy.json"

CONTROL_REPO = "riseproject-dev/porting-agent"
FORK_ORG = "riseproject-upstreaming"
# Development happens on DEV_REF: the worker's patches, the development PR,
# and its CI. The upstream pull request comes from UPSTREAM_REF, which only
# the trusted handoff creates or replaces, from the reviewed non-debug series
# of DEV_REF.
DEV_REF = "riscv64-dev"
UPSTREAM_REF = "riscv64"
NATIVE_RUNNER_LABEL = "ubuntu-24.04-riscv"
STATE_ROOT = ".state"
INTAKE_LABEL = "upstreaming-request"
DEBUG_PREFIX = "{debug}"
# The commit identity of the destination App: the trusted publisher replays
# every commit it writes under it, and the worker's checkout is configured
# with it.
APP_COMMIT_NAME = "riseproject-upstreaming[bot]"
APP_COMMIT_EMAIL = "359135081+riseproject-upstreaming[bot]@users.noreply.github.com"

# Directory under STATE_ROOT -> schema file name.
STATE_KINDS = {
    "projects": "project",
    "contributions": "contribution",
    "operations": "operation",
    "commands": "command",
    "events": "event",
    "evidence": "evidence",
}

# Record identifiers become path components: keep them boring.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PolicyError(ValueError):
    """Raised when reviewed policy or an input violates a hard rule."""


def repository_root() -> Path:
    """Return the checkout root containing the flattened control modules."""
    return REPOSITORY_ROOT


def load_policy(path: Path | None = None) -> dict[str, Any]:
    with open(path or POLICY_PATH, encoding="utf-8") as f:
        policy = json.load(f)
    import schemas  # local import to avoid a cycle

    schemas.validate("policy", policy)
    return policy


def safe_id(value: str) -> str:
    """Return ``value`` if it is a safe single path component, else raise."""
    if not isinstance(value, str) or not _ID_RE.match(value) or ".." in value:
        raise PolicyError(f"unsafe record identifier: {value!r}")
    return value


def fitted_id(value: str) -> str:
    """``value`` as a safe record identifier of at most 128 characters: a
    longer one keeps its first 111 characters and the first 16 hex digits of
    its SHA-256, so it stays deterministic and distinct. Raises like
    ``safe_id`` for a value with other unsafe characters."""
    if isinstance(value, str) and len(value) > 128:
        value = f"{value[:111]}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"
    return safe_id(value)


def contribution_file_id(contribution_id: str) -> str:
    """Map a contribution ID such as ``R_kgDO/1`` to a file-safe name."""
    return safe_id(contribution_id.replace("/", "--"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


# The bounds of a patch scope (``scope_paths`` of a contribution record, the
# paths of an admission override, and the scope a review extension widens):
# ``schemas/contribution.schema.json`` states the same numbers, and a test
# keeps the two equal. The plan's own ``allowed_paths`` stay at 32
# (``schemas/plan.schema.json``), so a scope has room for the exact paths
# that published patches and review extensions add to it. The files one
# patch changes are bounded alike (``changed_files`` of a proposal and of a
# push operation), since they become scope paths when
# the patch is published; the worker gate checks them with
# ``validate_scope_paths``.
SCOPE_PATHS_MAX = 64
SCOPE_PATH_MAX_LENGTH = 200


def validate_scope_paths(paths: list[str]) -> list[str]:
    """Validate exact relative paths or one final-component glob."""
    if not paths or len(paths) > SCOPE_PATHS_MAX:
        raise ValueError(f"scope needs 1..{SCOPE_PATHS_MAX} paths")
    for allowed in paths:
        path = Path(allowed)
        if len(allowed) > SCOPE_PATH_MAX_LENGTH or path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] == ".git":
            raise ValueError(f"unsafe scope path: {allowed}")
        wildcard_parts = [part for part in path.parts if any(ch in part for ch in "*?[]")]
        if wildcard_parts and (len(path.parts) < 2 or wildcard_parts != [path.parts[-1]] or "**" in path.parts[-1]):
            raise ValueError(f"scope wildcard must be one final component: {allowed}")
    return paths


def scope_covers(path: str, scope: list[str]) -> bool:
    """Whether a scope path matches ``path``: the one matching rule of every
    scope check (``fnmatch.fnmatchcase``, so ``dir/*`` matches below
    ``dir``)."""
    return any(fnmatch.fnmatchcase(path, allowed) for allowed in scope)


def widen_scope(scope: list[str], changed: list[str]) -> list[str]:
    """``scope`` with the changed files that no scope path matches appended
    as exact paths, in order and without duplicates. A file the scope
    already matches (``Makefile``, or ``pkg/qemu/qemu.go`` under
    ``pkg/qemu/*``) adds nothing. Raises ``ValueError`` when the widened
    scope is not a valid scope (``validate_scope_paths``)."""
    added = [path for path in dict.fromkeys(changed) if not scope_covers(path, scope)]
    widened = list(scope) + added
    if added:
        validate_scope_paths(widened)
    return widened


def normalize_distribution(name: str) -> str:
    """PEP 503 name normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()
