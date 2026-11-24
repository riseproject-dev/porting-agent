# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Durable lifecycle state under ``main:.state/`` (plan section 6).

Two parts:

* ``StateStore``: load/validate/stage records in a working tree. It writes
  files locally only; publication is the state publisher's job.
* ``validate_state_diff``: the independent final-tree check the state
  publisher runs before pushing. It compares the candidate commit's tree with
  the trusted base and rejects anything other than regular, schema-valid,
  size-bounded JSON files under ``.state/``, including deletions,
  renames out of the directory, mode changes, symlinks, submodules and
  case-folding path collisions.

GitHub tokens are repository-scoped, not path-scoped. This validator plus the
reviewed publisher code are what keep the state writer inside
``.state/``; the token itself could write anywhere.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import engine
import lifecycle
import recovery
import schemas
from common import (
    STATE_KINDS,
    STATE_ROOT,
    canonical_json,
    contribution_file_id,
    load_policy,
    repository_root,
    safe_id,
)

SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]

_ID_KEY = {
    "projects": "project_id",
    "contributions": "contribution_id",
    "operations": "operation_id",
    "commands": "command_id",
    "evidence": "evidence_id",
}

# State is rendered into the repository, whose text contract forbids this
# punctuation. Normalize every untrusted string at the single state staging
# boundary and reject it again during final-tree validation. The replacement is
# deliberately ASCII and meaning-preserving rather than a broad Unicode fold.
_FORBIDDEN_TEXT_REPLACEMENTS = {"\u2014": "--"}


@dataclass(frozen=True)
class LegacyTextMigration:
    path: str
    sha256: str
    contribution_id: str
    migration_id: str


# One already-committed worker summary predates the staging boundary. This
# reviewed, blob-bound migration is intentionally not a general repair tool.
LEGACY_TEXT_MIGRATIONS = (
    LegacyTextMigration(
        path=(
            ".state/evidence/"
            "rehearsal-closeio--ciso8601--1-r3-44982902579.json"
        ),
        sha256="34572c0ac0aa1214d2d12c9ff507634307ced173a2677e70b58f649373f1a149",
        contribution_id="closeio--ciso8601/1",
        migration_id="state-text-u2014-34572c0ac0aa",
    ),
)


class StateError(ValueError):
    pass


def find_secrets(text: str) -> list[str]:
    return [p.pattern for p in SECRET_PATTERNS if p.search(text)]


def normalize_state_text(value: Any) -> Any:
    """Return a deep copy whose strings satisfy repository text policy.

    Object keys are text too. A replacement that would collapse two keys is
    rejected rather than silently discarding either untrusted value.
    """
    if isinstance(value, str):
        for forbidden, replacement in _FORBIDDEN_TEXT_REPLACEMENTS.items():
            value = value.replace(forbidden, replacement)
        return value
    if isinstance(value, list):
        return [normalize_state_text(item) for item in value]
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            clean_key = normalize_state_text(key) if isinstance(key, str) else key
            if clean_key in normalized:
                raise StateError(f"state text normalization collides at key {clean_key!r}")
            normalized[clean_key] = normalize_state_text(item)
        return normalized
    return copy.deepcopy(value)


def _forbidden_state_text(value: Any) -> str | None:
    if isinstance(value, str):
        return next((char for char in _FORBIDDEN_TEXT_REPLACEMENTS if char in value), None)
    if isinstance(value, list):
        return next((found for item in value if (found := _forbidden_state_text(item))), None)
    if isinstance(value, dict):
        for key, item in value.items():
            found = _forbidden_state_text(key) or _forbidden_state_text(item)
            if found:
                return found
    return None


def record_path(kind: str, record: dict) -> str:
    """Relative path of a record. Events live in a per-contribution folder."""
    if kind == "events":
        return (f"{STATE_ROOT}/events/{contribution_file_id(record['contribution_id'])}/"
                f"{record['sequence']:08d}.json")
    rid = record[_ID_KEY[kind]]
    name = contribution_file_id(rid) if "/" in rid else safe_id(rid)
    return f"{STATE_ROOT}/{kind}/{name}.json"


def _limit(kind: str, policy: dict) -> int:
    s = policy["storage"]
    return s["max_event_bytes"] if kind in ("events", "commands") else s["max_record_bytes"]


def check_blob(path: str, data: bytes, policy: dict) -> list[str]:
    """Validate one file's content as committed state."""
    problems: list[str] = []
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != STATE_ROOT or parts[1] not in STATE_KINDS:
        return [f"{path}: not under an allowed {STATE_ROOT}/<kind>/ directory"]
    kind = parts[1]
    expected_depth = 4 if kind == "events" else 3
    if len(parts) != expected_depth or not parts[-1].endswith(".json"):
        problems.append(f"{path}: unexpected path shape")
    if len(data) > _limit(kind, policy):
        problems.append(f"{path}: {len(data)} bytes exceeds the {kind} limit")
    try:
        text = data.decode("utf-8")
        record = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return problems + [f"{path}: not UTF-8 JSON ({e})"]
    if find_secrets(text):
        problems.append(f"{path}: content matches a credential pattern")
    if _forbidden_state_text(record):
        problems.append(f"{path}: content contains forbidden repository text U+2014")
    # A record written before the strict record schema or the plan rename is
    # read as the record migrations rewrite it (``migrated_view``); one they
    # would refuse is invalid.
    try:
        view = migrated_view(kind, record)
    except (StateError, KeyError, TypeError) as error:
        problems.append(f"{path}: {error}")
        return problems
    for e in schemas.errors(STATE_KINDS[kind], view):
        problems.append(f"{path}: {e}")
    if kind == "contributions" and not problems:
        # Every record is in a state of the lifecycle table.
        state_problem = engine.state_problem(view)
        if state_problem:
            problems.append(f"{path}: {state_problem}")
    if not problems:
        try:
            if record_path(kind, record) != path:
                problems.append(f"{path}: record identity does not match its path")
        except (KeyError, ValueError) as e:
            problems.append(f"{path}: {e}")
    return problems


# --------------------------------------------------------------------------
# Final-tree validation against the trusted base (git plumbing only).


def _git(repo: Path, *args: str, input: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, input=input).stdout


@dataclass
class DiffReport:
    problems: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


def validate_state_diff(repo: Path, base: str, candidate: str, policy: dict) -> DiffReport:
    """Check that ``candidate`` differs from ``base`` only by valid state.

    Uses ``git diff-tree --no-renames`` so a rename is seen as a delete plus
    an add, and both sides are checked. Deletions are rejected: lifecycle
    history is append/update only, retention is a reviewed migration.
    """
    rep = DiffReport()
    parents = _git(repo, "rev-list", "--parents", "-n1", candidate).decode().split()[1:]
    base_sha = _git(repo, "rev-parse", base).decode().strip()
    if parents != [base_sha]:
        rep.problems.append("candidate must be a single commit on top of the trusted base")
    raw = _git(repo, "diff-tree", "-r", "-z", "--no-renames", "--no-commit-id",
               base, candidate)
    fields = raw.split(b"\0")
    i = 0
    while i < len(fields) - 1:
        meta = fields[i].decode()
        path = fields[i + 1].decode("utf-8", "surrogateescape")
        i += 2
        old_mode, new_mode, old_sha, new_sha, status = meta.lstrip(":").split()
        rep.changed.append(path)
        if not path.startswith(STATE_ROOT + "/"):
            rep.problems.append(f"{path}: outside {STATE_ROOT}/")
            continue
        if status == "D":
            rep.problems.append(f"{path}: deletion is not permitted")
            continue
        if new_mode != "100644":
            rep.problems.append(f"{path}: mode {new_mode} is not a regular non-executable file")
            continue
        if status == "M" and old_mode != new_mode:
            rep.problems.append(f"{path}: mode change")
        if status == "M" and path.startswith(f"{STATE_ROOT}/events/"):
            rep.problems.append(f"{path}: committed events are append-only")
        data = _git(repo, "cat-file", "blob", new_sha)
        rep.total_bytes += len(data)
        blob_problems = check_blob(path, data, policy)
        rep.problems.extend(blob_problems)
        if not blob_problems and _awaits_migration(path, json.loads(data)):
            rep.problems.append(f"{path}: written in a shape that awaits the record migration")
        if not blob_problems and path.startswith(f"{STATE_ROOT}/operations/"):
            # An operation's status changes only by rows of the operation
            # table: a closed operation never changes status.
            before = json.loads(_git(repo, "cat-file", "blob", old_sha)) if status == "M" else None
            why = recovery.transition_problem(before, json.loads(data))
            if why:
                rep.problems.append(f"{path}: {why}")
    if rep.total_bytes > policy["storage"]["max_commit_bytes"]:
        rep.problems.append(f"state commit is {rep.total_bytes} bytes, over the limit")
    # Case-insensitive collisions anywhere in the resulting state tree.
    names = _git(repo, "ls-tree", "-r", "-z", "--name-only", candidate, "--", STATE_ROOT)
    seen: dict[str, str] = {}
    for n in filter(None, names.decode("utf-8", "surrogateescape").split("\0")):
        k = n.casefold()
        if k in seen and seen[k] != n:
            rep.problems.append(f"{n}: collides with {seen[k]} on case-insensitive filesystems")
        seen[k] = n
    return rep


# --------------------------------------------------------------------------
# Working-tree store


def run_from_env(environ=None) -> dict | None:
    """The workflow run named by `GITHUB_RUN_ID` and `GITHUB_RUN_ATTEMPT`, as
    `{"id", "attempt"}`, or None when either is missing or not a positive
    integer (a local run)."""
    import os

    environ = os.environ if environ is None else environ
    try:
        run = {"id": int(environ.get("GITHUB_RUN_ID") or ""),
               "attempt": int(environ.get("GITHUB_RUN_ATTEMPT") or "")}
    except ValueError:
        return None
    return run if run["id"] > 0 and run["attempt"] > 0 else None


class StateStore:
    """Load and stage lifecycle records in a checkout of the control repo."""

    def __init__(self, root: Path, policy: dict, run: dict | None = None):
        self.root = Path(root)
        self.policy = policy
        # The workflow run that stages the records, `{"id", "attempt"}`;
        # recorded on every event staged through this store.
        self.run = dict(run) if run else None
        self._staged: dict[str, dict] = {}

    def _ensure_no_symlink(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as error:
            raise StateError(f"{path}: outside repository root") from error
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise StateError(f"{current}: symlinked state path")

    def _dir(self, kind: str) -> Path:
        if kind not in STATE_KINDS:
            raise StateError(f"unknown kind {kind}")
        directory = self.root / STATE_ROOT / kind
        self._ensure_no_symlink(directory)
        return directory

    def load_all(self, kind: str) -> dict[str, dict]:
        """Load every record of a kind, validating each. Invalid state blocks
        writes: callers must stop affected work, never guess."""
        out: dict[str, dict] = {}
        d = self._dir(kind)
        if not d.exists():
            return out
        seen: dict[str, str] = {}
        for p in sorted(d.rglob("*.json")):
            self._ensure_no_symlink(p)
            if not p.is_file():
                raise StateError(f"{p}: not a regular file")
            rel = p.relative_to(self.root).as_posix()
            folded = rel.casefold()
            if folded in seen and seen[folded] != rel:
                raise StateError(f"{rel}: collides with {seen[folded]} on case-insensitive filesystems")
            seen[folded] = rel
            problems = check_blob(rel, p.read_bytes(), self.policy)
            if problems:
                raise StateError("; ".join(problems))
            rec = json.loads(p.read_text(encoding="utf-8"))
            out[rel] = rec
        return out

    def contributions(self) -> list[dict]:
        return list(self.load_all("contributions").values())

    def next_event_sequence(self, contribution_id: str) -> int:
        d = self._dir("events") / contribution_file_id(contribution_id)
        existing = [int(p.stem) for p in d.glob("*.json")] if d.exists() else []
        staged = [r["sequence"] for k, r in self._staged.items()
                  if k.startswith(f"{STATE_ROOT}/events/{contribution_file_id(contribution_id)}/")]
        return max(existing + staged, default=-1) + 1

    def stage(self, kind: str, record: dict) -> str:
        # Everything entering durable state may contain API, human, model, or
        # receipt text. Normalize it here instead of relying on every producer.
        record = normalize_state_text(record)
        if kind == "events":
            record = dict(record, sequence=self.next_event_sequence(record["contribution_id"]))
            if self.run:
                record["run"] = dict(self.run)
        schemas.validate(STATE_KINDS[kind], record)
        path = record_path(kind, record)
        data = canonical_json(record).encode("utf-8")
        problems = check_blob(path, data, self.policy)
        if problems:
            raise StateError("; ".join(problems))
        if kind == "evidence" and path not in self._staged and self._same_evidence(path, record):
            # Observing the same run again is not new evidence: the committed
            # record, with the time it was first collected, is kept.
            return path
        self._staged[path] = record
        return path

    def _same_evidence(self, rel: str, record: dict) -> bool:
        p = self.root / rel
        self._ensure_no_symlink(p)
        if not p.is_file():
            return False
        try:
            stored = json.loads(p.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return ({k: v for k, v in stored.items() if k != "collected_at"}
                == {k: v for k, v in record.items() if k != "collected_at"})

    def write_staged(self) -> list[str]:
        written = []
        for rel, rec in sorted(self._staged.items()):
            p = self.root / rel
            self._ensure_no_symlink(p.parent)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_no_symlink(p)
            p.write_text(canonical_json(rec), encoding="utf-8")
            written.append(rel)
        self._staged.clear()
        return written

    def total_bytes(self) -> int:
        base = self.root / STATE_ROOT
        return sum(p.stat().st_size for p in base.rglob("*") if p.is_file()) if base.exists() else 0


def _pending_text_migration_records(
    root: Path,
) -> list[tuple[LegacyTextMigration, dict]]:
    """Read and verify legacy blobs without changing the checkout."""
    root = Path(root)
    pending: list[tuple[LegacyTextMigration, dict]] = []
    for migration in LEGACY_TEXT_MIGRATIONS:
        path = root / migration.path
        if not path.exists():
            continue
        data = path.read_bytes()
        try:
            record = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StateError(
                f"{migration.path}: legacy text migration is not valid JSON"
            ) from error
        if not _forbidden_state_text(record):
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest != migration.sha256:
            raise StateError(
                f"{migration.path}: legacy text migration expected "
                f"{migration.sha256}, got {digest}"
            )
        normalized = normalize_state_text(record)
        expected_path = record_path("evidence", normalized)
        if (
            expected_path != migration.path
            or normalized.get("contribution_id") != migration.contribution_id
        ):
            raise StateError(f"{migration.path}: legacy text migration identity mismatch")
        pending.append((migration, normalized))
    if len(pending) > 1:
        raise StateError("multiple legacy text migrations cannot share one repair commit")
    return pending


def pending_text_migrations(root: Path) -> list[LegacyTextMigration]:
    """Detect only exact, reviewed legacy blobs that are awaiting migration."""
    return [migration for migration, _record in _pending_text_migration_records(root)]


def apply_pending_text_migration(root: Path, policy: dict, *, now: str) -> list[str]:
    """Normalize the one reviewed legacy blob and append its migration event.

    A matching path with any unexpected content fails closed. When a migration
    is pending, callers must publish only these returned state paths before
    doing ordinary reconciliation, so unrelated lifecycle mutation cannot ride
    in the repair commit.
    """
    root = Path(root)
    pending = _pending_text_migration_records(root)
    if not pending:
        return []
    migration, normalized = pending[0]
    store = StateStore(root, policy)
    evidence_path = store.stage("evidence", normalized)
    event_path = store.stage("events", {
        "schema_version": 1,
        "contribution_id": migration.contribution_id,
        "sequence": 0,
        "at": now,
        "kind": "state_text_normalized",
        "from": {},
        "to": {},
        "cause": {"type": "migration", "id": migration.migration_id},
        "revision_bumped": False,
    })
    written = store.write_staged()
    if sorted(written) != sorted((evidence_path, event_path)):
        raise StateError("legacy text migration attempted an unrelated state mutation")
    return written


# --------------------------------------------------------------------------
# Name migration
#
# The planning phase and its plan were once named `investigation` and
# `dossier` (``lifecycle.HISTORIC_NAMES``). A record written before the
# rename carries the historic names in exactly these places, and
# ``migrate_record_names`` rewrites them and nothing else:
#
# - a contribution: the field `dossier_evidence_id` (now `plan_evidence_id`)
#   and the count `budget.investigation_attempts` (now
#   `budget.planning_attempts`), with their values; the value
#   `investigation` of `phase` and `previous_phase`; and the values
#   `investigation_failed` and `investigation_attempts` of
#   `pending_human_action.kind` and `.counter`;
# - a project: the field `dossier_evidence_id`;
# - an evidence record: the kind `dossier` (now `plan`). Its id is kept: ids
#   are opaque, and operations and events name it.
#
# Requested Work under the historic names (an `active_work` of the task
# `investigation`) has no reviewed migration: its queued payload names a
# worker profile that no longer exists. Committed events are not rewritten:
# they are the history as it was written, the event schema accepts any kind,
# and their readers translate a historic name (``lifecycle.current_name``).

NAME_MIGRATION = "plan-rename"
NAMES_MIGRATED = "record_names_migrated"
_HISTORIC = lifecycle.HISTORIC_NAMES


def migrate_names(kind: str, record: Any) -> tuple[Any, list[str]]:
    """``record`` of the state kind ``kind`` with its historic names replaced
    by the current ones, and the places it changed (empty when it carries
    none). Raises ``StateError`` for a shape the migration does not know."""
    if not isinstance(record, dict):
        return record, []
    if kind == "contributions":
        try:
            return engine.with_current_names(record)
        except lifecycle.TransitionError as error:
            raise StateError(str(error)) from error
    if kind == "projects" and "dossier_evidence_id" in record:
        if "plan_evidence_id" in record:
            raise StateError("both dossier_evidence_id and plan_evidence_id are present")
        return ({_HISTORIC.get(key, key) if key == "dossier_evidence_id" else key:
                 copy.deepcopy(value) for key, value in record.items()},
                ["dossier_evidence_id"])
    if kind == "evidence" and record.get("kind") == "dossier":
        return dict(copy.deepcopy(record), kind=_HISTORIC["dossier"]), ["kind=dossier"]
    return record, []


def needs_name_migration(kind: str, record: Any) -> bool:
    """Whether a stored record still carries a historic name."""
    try:
        return bool(migrate_names(kind, record)[1])
    except StateError:
        return True


def migrate_record_names(record: dict, *, now: str, evidence: list[str] = (),
                         projects: list[str] = ()) -> tuple[dict, dict]:
    """The contribution record with its historic names replaced, and the
    event that records it, with the evidence and projects whose names the
    same migration rewrites. Nothing else changes, `updated_at` included.
    Raises ``StateError`` when nothing is renamed or for any other shape."""
    new, changes = migrate_names("contributions", record)
    if not (changes or evidence or projects):
        raise StateError("the record needs no name migration")
    view = engine.with_defaults(new)[0]
    errors = schemas.errors("contribution", view)
    if errors:
        raise StateError("no reviewed name migration for this record: " + "; ".join(errors[:5]))
    problem = engine.state_problem(view)
    if problem:
        raise StateError(f"no reviewed name migration for this record: {problem}")
    event = lifecycle._event(record, new, NAMES_MIGRATED, "migration", NAME_MIGRATION, now, False)
    event["details"] = {"from_state": engine.state_key(record), "to_state": engine.state_key(new),
                        "renamed": changes, "evidence": sorted(evidence),
                        "projects": sorted(projects)}
    schemas.validate("event", event)
    return new, event


def migrated_view(kind: str, record: Any) -> Any:
    """``record`` as the controller's record migrations rewrite it: its
    historic names replaced and, for a contribution, the fields of the
    strict record schema added. Raises ``StateError`` when a migration
    refuses it."""
    view = migrate_names(kind, record)[0]
    if kind == "contributions" and needs_field_migration(view):
        view = migrate_record_fields(view, now=view["updated_at"])[0]
    return view


# --------------------------------------------------------------------------
# Record migration
#
# The strict record schema requires every field of the record, the counts of
# `budget`, `pending_human_action.counter`, and the fields of the Work
# identity in `active_work`; the stop flag is `paused` alone. A record
# written before it lacks fields the table added later and may still carry
# `blocked_reason`, always null. ``migrate_record_fields`` adds each missing
# field with the value the pipeline reads for an absent one
# (``engine.with_defaults``), and removes the null `blocked_reason`; it
# changes nothing else. Any other difference from
# the strict schema, and a state that is not a state of the table, fail
# closed.

FIELD_MIGRATION = "strict-record-schema"
FIELD_MIGRATED = "record_fields_migrated"

def needs_field_migration(record: Any) -> bool:
    """Whether a contribution record lacks a field of the strict schema or
    still carries a retired one."""
    if not isinstance(record, dict):
        return False
    try:
        _new, added, removed = engine.with_defaults(record)
    except lifecycle.TransitionError:
        return True
    return bool(added or removed)


def migrate_record_fields(record: dict, *, now: str) -> tuple[dict, dict]:
    """The record with its missing fields added at their defaults, the null
    ``blocked_reason`` and ``budget.reruns.answered_ci_evidence_id`` (the
    fence of the CI run a ``request_ci_rerun`` answered before the trusted
    rerun) removed, and its event. Raises ``StateError`` for any other
    shape."""
    try:
        new, added, removed = engine.with_defaults(record)
    except lifecycle.TransitionError as error:
        raise StateError(f"no reviewed record migration for this record: {error}") from error
    if not (added or removed):
        raise StateError("the record needs no field migration")
    new["updated_at"] = now
    errors = schemas.errors("contribution", new)
    if errors:
        raise StateError("no reviewed record migration for this record: "
                         + "; ".join(errors[:5]))
    problem = engine.state_problem(new)
    if problem:
        raise StateError(f"no reviewed record migration for this record: {problem}")
    state = engine.state_key(new)
    event = lifecycle._event(record, new, FIELD_MIGRATED, "migration", FIELD_MIGRATION, now, False)
    event["details"] = {"from_state": state, "to_state": state, "added": added,
                        "removed": removed}
    schemas.validate("event", event)
    return new, event


def _awaits_migration(path: str, record: Any) -> bool:
    """Whether a committed state file is in a shape the record migrations
    rewrite."""
    parts = path.split("/")
    kind = parts[1] if len(parts) > 2 else None
    if needs_name_migration(kind, record):
        return True
    return kind == "contributions" and needs_field_migration(record)


def _read_kind(root: Path, kind: str) -> list[tuple[str, Any]]:
    directory = root / STATE_ROOT / kind
    out: list[tuple[str, Any]] = []
    if not directory.exists():
        return out
    for path in sorted(directory.glob("*.json")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.is_file():
            raise StateError(f"{rel}: not a regular file")
        try:
            out.append((rel, json.loads(path.read_text(encoding="utf-8"))))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StateError(f"{rel}: not UTF-8 JSON ({error})") from error
    return out


def _pending_record_migrations(root: Path, policy: dict, *, now: str | None = None
                               ) -> list[tuple[str, str, dict]]:
    """The state files that the record migrations rewrite, each with its
    kind and migrated content, and the events that record them, in the
    order they are staged. Reads the checkout without changing it.

    A contribution, project, or evidence record that carries a historic name
    is renamed (``migrate_names``), with one ``record_names_migrated`` event
    per contribution that names the renamed evidence of the contribution and
    the renamed projects of which it is the first contribution; then a
    contribution that lacks a field of the strict schema is migrated
    (``migrate_record_fields``). Every other record must be valid, and a
    record a migration refuses fails closed here, before any caller acts on
    the state."""
    root = Path(root)
    contributions = _read_kind(root, "contributions")
    renamed: dict[str, list[tuple[str, str, dict]]] = {}
    evidence_of: dict[str, list[str]] = {}
    projects_of: dict[str, list[str]] = {}
    first_of_project: dict[str, str] = {}
    for _rel, record in contributions:
        if isinstance(record, dict) and isinstance(record.get("project_id"), str):
            cid = record.get("contribution_id")
            if isinstance(cid, str):
                first = first_of_project.get(record["project_id"])
                first_of_project[record["project_id"]] = min(cid, first) if first else cid
    for kind, owner in (("evidence", "contribution_id"), ("projects", "project_id")):
        for rel, record in _read_kind(root, kind):
            try:
                migrated, changes = migrate_names(kind, record)
            except StateError as error:
                raise StateError(f"{rel}: {error}") from error
            if not changes:
                continue
            key = record.get(owner) if isinstance(record, dict) else None
            cid = key if kind == "evidence" else first_of_project.get(key)
            if not isinstance(cid, str):
                raise StateError(f"{rel}: no contribution records its name migration")
            errors = schemas.errors(STATE_KINDS[kind], migrated)
            if errors:
                raise StateError(f"{rel}: no reviewed name migration: {'; '.join(errors[:5])}")
            renamed.setdefault(cid, []).append((rel, kind, migrated))
            target = evidence_of if kind == "evidence" else projects_of
            target.setdefault(cid, []).append(record[_ID_KEY[kind]])
    pending: list[tuple[str, str, dict]] = []
    seen: set[str] = set()
    for rel, record in contributions:
        cid = record.get("contribution_id") if isinstance(record, dict) else None
        seen.add(cid)
        try:
            current = record
            stamp = now or (record["updated_at"] if isinstance(record, dict) else None)
            if needs_name_migration("contributions", record) or cid in renamed:
                current, event = migrate_record_names(
                    record, now=stamp, evidence=evidence_of.get(cid, []),
                    projects=projects_of.get(cid, []))
                pending.append((rel, "contributions", current))
                pending.extend(renamed.get(cid, []))
                pending.append(("", "events", event))
            if needs_field_migration(current):
                current, event = migrate_record_fields(current, now=stamp)
                # A record that also needed the name migration is staged
                # once, migrated in full: its renamed form may lack a field
                # of the strict schema.
                pending = [entry for entry in pending
                           if not (entry[0] == rel and entry[1] == "contributions")]
                pending.append((rel, "contributions", current))
                pending.append(("", "events", event))
            elif current is record:
                errors = schemas.errors("contribution", record)
                if errors:
                    raise StateError("; ".join(errors[:5]))
        except (StateError, KeyError, TypeError) as error:
            raise StateError(f"{rel}: {error}") from error
    missing = sorted(set(renamed) - seen)
    if missing:
        raise StateError(f"no contribution record for the name migration of {missing}")
    return pending


def pending_record_migrations(root: Path, policy: dict) -> list[str]:
    """Paths of the state files that await a record migration."""
    return sorted({rel for rel, _kind, _record in _pending_record_migrations(root, policy)
                   if rel})


def apply_pending_record_migrations(root: Path, policy: dict, *, now: str) -> list[str]:
    """Rewrite every record written before the plan rename
    (``migrate_record_names``) or the strict record schema
    (``migrate_record_fields``) and append one migration event per
    migration of each contribution.

    It is idempotent: a migrated record is not selected again. Callers
    publish only the returned paths, before any ordinary reconciliation, so
    no other lifecycle mutation rides in the commit.
    """
    pending = _pending_record_migrations(root, policy, now=now)
    if not pending:
        return []
    store = StateStore(Path(root), policy)
    expected = set()
    for rel, kind, record in pending:
        staged = store.stage(kind, record)
        if rel and staged != rel:
            raise StateError(f"{rel}: record migration identity mismatch")
        expected.add(staged)
    written = store.write_staged()
    if sorted(written) != sorted(expected):
        raise StateError("record migration attempted an unrelated state mutation")
    return written


def resolution_problems(operations: dict[str, dict], evidence: dict[str, dict]) -> list[str]:
    """Each resolved operation names the `resolution` evidence record of that
    operation, which the controller writes in the same commit."""
    by_id = {e.get("evidence_id"): e for e in evidence.values()}
    problems = []
    for op in operations.values():
        resolution = op.get("resolution")
        if not resolution:
            continue
        record = by_id.get(resolution.get("evidence_id"))
        if record is None:
            problems.append(f"operation {op['operation_id']}: its resolution evidence "
                            f"{resolution.get('evidence_id')} does not exist")
        elif record.get("kind") != "resolution":
            problems.append(f"operation {op['operation_id']}: evidence "
                            f"{record['evidence_id']} is not a resolution record")
        elif (record.get("resolution") or {}).get("operation_id") != op["operation_id"]:
            problems.append(f"operation {op['operation_id']}: resolution evidence "
                            f"{record['evidence_id']} names another operation")
    return problems


def cmd_validate(args: argparse.Namespace) -> int:
    policy = load_policy()
    store = StateStore(Path(args.root) if args.root else repository_root(), policy)
    bad = 0
    loaded: dict[str, dict[str, dict]] = {}
    for kind in ("projects", "contributions", "operations", "commands", "events", "evidence"):
        try:
            loaded[kind] = store.load_all(kind)
            print(f"{kind}: {len(loaded[kind])} valid")
        except StateError as error:
            print(f"{kind}: INVALID: {error}", file=sys.stderr)
            bad += 1
    if "operations" in loaded and "evidence" in loaded:
        for problem in resolution_problems(loaded["operations"], loaded["evidence"]):
            print(f"operations: INVALID: {problem}", file=sys.stderr)
            bad += 1
    try:
        awaiting = pending_record_migrations(store.root, policy)
    except StateError as error:
        print(f"record migration: INVALID: {error}", file=sys.stderr)
        bad += 1
    else:
        for path in awaiting:
            print(f"{path}: awaits a record migration by the controller")
    total = store.total_bytes()
    if total > policy["storage"]["warn_total_bytes"]:
        print(
            f"warning: {total} bytes of live state; pause expansion and review storage policy",
            file=sys.stderr,
        )
    return 1 if bad else 0


def cmd_check_diff(args: argparse.Namespace) -> int:
    repo = Path(args.repo) if args.repo else repository_root()
    report = validate_state_diff(repo, args.base, args.candidate, load_policy())
    for problem in report.problems:
        print(f"::error::{problem}")
    print(json.dumps({"ok": report.ok, "changed": len(report.changed), "bytes": report.total_bytes}))
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m state")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("root", nargs="?")
    validate.set_defaults(handler=cmd_validate)
    check_diff = commands.add_parser("check-diff")
    check_diff.add_argument("base")
    check_diff.add_argument("candidate")
    check_diff.add_argument("--repo")
    check_diff.set_defaults(handler=cmd_check_diff)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
