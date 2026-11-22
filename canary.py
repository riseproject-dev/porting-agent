# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Exact, one-time recovery contract for canonical ciso8601 issue #1."""

from __future__ import annotations

import copy
import hashlib

import recovery
import schemas
from common import CONTROL_REPO

ISSUE_ID = 6536262483
ISSUE_NUMBER = 1
ISSUE_TITLE = "[upstreaming] ciso8601 riscv64 wheel support"
CONTRIBUTION_ID = "closeio--ciso8601/1"
PROJECT_ID = "closeio--ciso8601"
UPSTREAM = "closeio/ciso8601"
UPSTREAM_REPOSITORY_ID = 20633992
UPSTREAM_BASE_SHA = "cbe05770dc95966a9bf0bff32bbcfa8ba0a3cbf8"
TARGET_BRANCH = "master"
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
INCOMPLETE_BODY = """### Upstream repository

https://github.com/closeio/ciso8601

### PyPI distributions

ciso8601 (import: ciso8601)

### Upstream target branch

master

### Additional notes

"""
EXPECTED_DIGEST = "9f58bffa65d7e7df0206ae1130ce6eb5e218181b966839f2e609c05e8baa7ebb"
INCOMPLETE_DIGEST = "4a5a584a429838e79b79c512b2bdf9f5c49df33dc046969c5d958a254d6a20c8"
OPERATION_KIND = "issue_body_repair"
REASON_IDS = [INCOMPLETE_DIGEST, EXPECTED_DIGEST]

assert hashlib.sha256(EXPECTED_BODY.encode()).hexdigest() == EXPECTED_DIGEST
assert hashlib.sha256(INCOMPLETE_BODY.encode()).hexdigest() == INCOMPLETE_DIGEST


class CanaryError(ValueError):
    """The live resource or durable state does not match the one recovery contract."""


def body_digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def expected_notes() -> str:
    marker = "### Additional notes\n\n"
    return EXPECTED_BODY.split(marker, 1)[1].removesuffix("\n")


def validate_intake(issue: dict, body: str) -> None:
    """Prevent a fresh canonical canary from entering with edited content."""
    if issue.get("number") != ISSUE_NUMBER:
        return
    if issue.get("id") != ISSUE_ID or issue.get("title") != ISSUE_TITLE:
        raise CanaryError("canonical canary issue identity does not match the reviewed fixture")
    if body_digest(body) != EXPECTED_DIGEST or body != EXPECTED_BODY:
        raise CanaryError("canonical canary body does not match the reviewed fixture")


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise CanaryError(detail)


def is_target(record: dict) -> bool:
    return record.get("contribution_id") == CONTRIBUTION_ID


def has_expected_tracking(record: dict) -> bool:
    tracking = record.get("tracking_issue") or {}
    return (
        is_target(record)
        and tracking.get("id") == ISSUE_ID
        and tracking.get("number") == ISSUE_NUMBER
        and tracking.get("description_digest") == EXPECTED_DIGEST
        and tracking.get("description") == EXPECTED_BODY
        and tracking.get("notes") == expected_notes()
    )


def is_repaired(record: dict) -> bool:
    return (
        has_expected_tracking(record)
        and record.get("active_work") is None
        and record.get("active_operation_id") is None
        and record.get("phase") == "queued"
        and record.get("activity") == "idle"
    )


def validate_mismatch(record: dict, *, operation_id: str | None) -> None:
    """Require the exact committed pre-dispatch revision-0 defect."""
    _require(is_target(record), "not the canonical canary contribution")
    _require(record.get("project_id") == PROJECT_ID, "canary project identity changed")
    _require(record.get("revision") == 0 and record.get("generation") == 0,
             "canary is no longer at revision 0 generation 0")
    _require(record.get("phase") == "investigation" and record.get("activity") == "agent_running",
             "canary lifecycle is no longer the pre-dispatch investigation")
    _require(record.get("distributions") == ["ciso8601"], "canary distributions changed")
    upstream = record.get("upstream") or {}
    _require(
        upstream == {
            "repository_id": UPSTREAM_REPOSITORY_ID,
            "full_name": UPSTREAM,
            "base_ref": TARGET_BRANCH,
            "base_sha": UPSTREAM_BASE_SHA,
        },
        "canary upstream identity or base changed",
    )
    tracking = record.get("tracking_issue") or {}
    _require(tracking.get("id") == ISSUE_ID and tracking.get("number") == ISSUE_NUMBER,
             "canary tracking issue identity changed")
    _require(tracking.get("url") == f"https://github.com/{CONTROL_REPO}/issues/{ISSUE_NUMBER}",
             "canary tracking issue URL changed")
    _require(tracking.get("description") == INCOMPLETE_BODY,
             "canary frozen body is not the exact known incomplete body")
    _require(tracking.get("description_digest") == INCOMPLETE_DIGEST and tracking.get("notes") == "",
             "canary frozen digest or notes changed")
    _require(not record.get("paused") and record.get("blocked_reason") is None
             and record.get("pending_human_action") is None,
             "canary is paused, blocked, or waiting for human action")
    _require(record.get("fork") is None and record.get("dossier_evidence_id") is None,
             "canary has progressed beyond pre-dispatch intake")
    _require((record.get("budget") or {}).get("attempts_used") == 0,
             "canary has consumed an attempt")
    _require(record.get("queue_work_id") is None and record.get("queue_claim_handle") is None
             and record.get("queue_result_id") is None,
             "canary has native queue evidence")
    _require(record.get("proposal_artifact") is None and record.get("processed_publications") == [],
             "canary has worker or publication evidence")
    work = record.get("active_work") or {}
    _require(
        work.get("task") == "investigation"
        and work.get("revision") == 0
        and work.get("attempt") == 1
        and work.get("dedupe_key") == f"{CONTRIBUTION_ID}@0:investigation"
        and work.get("dispatched_run_id") is None
        and work.get("reason_ids") == [],
        "canary Work request is not the exact undispatched investigation",
    )
    frozen = work.get("tracking_issue") or {}
    _require(
        frozen.get("id") == ISSUE_ID
        and frozen.get("number") == ISSUE_NUMBER
        and frozen.get("url") == f"https://github.com/{CONTROL_REPO}/issues/{ISSUE_NUMBER}"
        and frozen.get("description") == INCOMPLETE_BODY
        and frozen.get("description_digest") == INCOMPLETE_DIGEST,
        "canary Work request does not carry the known incomplete issue",
    )
    _require(record.get("active_operation_id") == operation_id,
             "canary active operation does not match recovery intent")


def validate_issue(issue: dict, allowed_digests: set[str]) -> str:
    _require(issue.get("id") == ISSUE_ID and issue.get("number") == ISSUE_NUMBER,
             "live canary issue identity changed")
    _require(issue.get("title") == ISSUE_TITLE, "live canary issue title changed")
    body = issue.get("body")
    _require(isinstance(body, str), "live canary issue body is unavailable")
    digest = body_digest(body)
    _require(digest in allowed_digests, "live canary issue body changed unexpectedly")
    if digest == EXPECTED_DIGEST:
        _require(body == EXPECTED_BODY, "live canary expected digest has unexpected bytes")
    if digest == INCOMPLETE_DIGEST:
        _require(body == INCOMPLETE_BODY, "live canary incomplete digest has unexpected bytes")
    return digest


def operation_for(record: dict, *, policy: dict, now: str) -> dict:
    validate_mismatch(record, operation_id=None)
    return recovery.plan(
        contribution_id=CONTRIBUTION_ID,
        revision=0,
        kind=OPERATION_KIND,
        policy_version=policy["policy_version"],
        reason_ids=REASON_IDS,
        target={
            "repository": CONTROL_REPO,
            "repository_id": None,
            "ref": f"issues/{ISSUE_NUMBER}",
            "expected_head": None,
            "base_sha": None,
        },
        proposed={
            "marker": f"canonical-canary:{ISSUE_ID}:{EXPECTED_DIGEST}",
            "content_digest": EXPECTED_DIGEST,
        },
        now=now,
    )


def validate_operation(operation: dict) -> None:
    _require(operation.get("kind") == OPERATION_KIND, "wrong canary recovery operation kind")
    _require(operation.get("contribution_id") == CONTRIBUTION_ID and operation.get("revision") == 0,
             "wrong canary recovery contribution or revision")
    _require(operation.get("reason_ids") == REASON_IDS, "wrong canary recovery reason binding")
    _require(operation.get("target") == {
        "repository": CONTROL_REPO,
        "repository_id": None,
        "ref": f"issues/{ISSUE_NUMBER}",
        "expected_head": None,
        "base_sha": None,
    }, "wrong canary recovery target")
    _require((operation.get("proposed") or {}).get("content_digest") == EXPECTED_DIGEST,
             "wrong canary recovery content digest")
    _require(operation.get("status") in {"planned", "confirmed"},
             "canary recovery operation is not recoverable")


def planned_event(record: dict, operation: dict, now: str) -> dict:
    return {
        "schema_version": 1,
        "contribution_id": CONTRIBUTION_ID,
        "sequence": 0,
        "at": now,
        "kind": "canary:recovery_planned",
        "from": {"phase": record["phase"], "revision": record["revision"]},
        "to": {"phase": record["phase"], "revision": record["revision"]},
        "cause": {"type": "migration", "id": operation["operation_id"]},
        "revision_bumped": False,
    }


def bind_operation(record: dict, operation: dict, now: str) -> dict:
    changed = copy.deepcopy(record)
    changed["active_operation_id"] = operation["operation_id"]
    changed["updated_at"] = now
    schemas.validate("contribution", changed)
    return changed


def finalize(record: dict, operation: dict, now: str) -> tuple[dict, dict, dict]:
    validate_operation(operation)
    validate_mismatch(record, operation_id=operation["operation_id"])
    repaired = copy.deepcopy(record)
    repaired["tracking_issue"] = {
        "id": ISSUE_ID,
        "number": ISSUE_NUMBER,
        "url": f"https://github.com/{CONTROL_REPO}/issues/{ISSUE_NUMBER}",
        "description": EXPECTED_BODY,
        "description_digest": EXPECTED_DIGEST,
        "notes": expected_notes(),
    }
    repaired["active_work"] = None
    repaired["active_operation_id"] = None
    repaired["phase"] = "queued"
    repaired["activity"] = "idle"
    repaired["updated_at"] = now
    schemas.validate("contribution", repaired)

    confirmed = copy.deepcopy(operation)
    confirmed["status"] = "confirmed"
    confirmed["external_ids"] = {
        "issue_id": ISSUE_ID,
        "issue_number": ISSUE_NUMBER,
        "body_digest": EXPECTED_DIGEST,
    }
    confirmed["discovery"] = list(confirmed.get("discovery") or []) + [{
        "at": now,
        "result": "confirmed",
        "detail": "canonical issue body matched exact reviewed bytes after readback",
    }]
    confirmed["updated_at"] = now
    schemas.validate("operation", confirmed)

    event = {
        "schema_version": 1,
        "contribution_id": CONTRIBUTION_ID,
        "sequence": 0,
        "at": now,
        "kind": "canary:intake_repaired",
        "from": {"phase": record["phase"], "revision": 0,
                 "description_digest": INCOMPLETE_DIGEST},
        "to": {"phase": "queued", "revision": 0,
               "description_digest": EXPECTED_DIGEST},
        "cause": {"type": "migration", "id": operation["operation_id"]},
        "revision_bumped": False,
    }
    schemas.validate("event", event)
    return repaired, confirmed, event
