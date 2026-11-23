# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Compatibility shim: lifecycle writes that do not yet go through the table.

``engine.apply_transition`` is the write path of the contribution lifecycle.
The code paths listed in ``LEGACY_SITES`` predate the lifecycle table and
still decide a state change themselves; until each is converted to a table
verb, it writes the fenced fields (``phase``, ``activity``, ``paused``,
``revision``, ``generation``) only through ``legacy_update`` or the legacy
transition functions of this module. No other module assigns those fields.

A legacy write can leave a record in a state of the table or in one of
``LEGACY_STATES``, the states the table removed that these paths still
produce. It can never enter the ``handoff`` phase or change a record that is
in it: every handoff state is written by the engine.

For a record in a state of ``LEGACY_STATES`` the verbs that set ``paused``
are written by ``legacy_pause``, and ``pause``, ``resume``, and ``park`` by
``apply_command``; in a state of the table the engine takes them. Every
other command of the table applies in states of the table only.
``apply_command`` also applies the intake command ``authorize-followup``.
The reviewed record migrations (``migrate_handoff_wait``, ``migrate_flags``,
``migrate_terminal``, ``migrate_provisioning``, ``migrate_development``) bring
records written before the table to the shapes it describes.

``LEGACY_SITES`` and ``LEGACY_STATES`` shrink as call sites are converted;
when both are empty this module is deleted.
"""

from __future__ import annotations

import copy
from typing import Any

import engine
import lifecycle
import schemas
from commands import URGENT, Command
from common import INTAKE_LABEL
from lifecycle import (
    ACTIVITIES,
    FLAGS,
    LIFECYCLE,
    REMINDER_KINDS,
    TERMINAL,
    Outcome,
    TransitionError,
    _event,
)

# States that are not in the lifecycle table and that legacy paths still
# write. Each is removed together with the paths that write it. Every state
# of the investigation, provisioning, and development phases is a state of
# the table.
_LEGACY_PHASES = ("upstream_review",)
_LEGACY_ACTIVITIES = ("idle", "agent_running", "waiting_ci", "fix_needed", "waiting_capacity",
                      "waiting_human")
LEGACY_STATES = frozenset(
    # The legacy paths combine a working phase with any of the activities
    # they know: `idle` where Work is due or CI passed, `waiting_capacity`
    # where the table keeps the state whose guard does not hold yet.
    {f"{phase}.{activity}" for phase in _LEGACY_PHASES for activity in _LEGACY_ACTIVITIES}
) - frozenset(LIFECYCLE)

# The states of the table that wait for a human (kinds `human_wait`, `gate`,
# and `locked`). Only the rows the table lists for one leave it, so a legacy
# write never changes the phase or activity of a record in one
# (``waits_for_human``).
HUMAN_WAITS = frozenset(key for key, spec in LIFECYCLE.items() if spec.kind in REMINDER_KINDS)


def waits_for_human(record: dict) -> bool:
    """Whether ``record`` is in a wait of the table for a human: a state of
    kind ``gate`` or ``locked``, or of kind ``human_wait`` with a recorded
    ``pending_human_action``. Only the rows the table lists for the state
    leave it. A ``waiting_human`` activity without a pending action is a
    shape the paths before the table wrote (the legacy provisioning wait,
    a block recorded in place); the legacy paths keep handling it."""
    state = engine.state_key(record)
    if state not in HUMAN_WAITS:
        return False
    return LIFECYCLE[state].kind != "human_wait" or bool(record.get("pending_human_action"))

# The call sites that still write lifecycle state outside the table, by the
# identifier each passes to ``legacy_update``.
LEGACY_SITES = frozenset({
    "canary.repair_mismatch",
    # The Result of upstream revision Work, and its publish-only retries.
    "reconcile.publication_outcome",
    "reconcile.publish_retry_exhausted",
})


def state_problem(record: dict) -> str | None:
    """Why a record's state is not admitted, legacy states included."""
    return engine.state_problem(record, LEGACY_STATES)


# --------------------------------------------------------------------------
# Record migration
#
# Before the lifecycle table, passing CI on the development head moved a
# contribution to ``handoff:waiting_human`` with the pending action
# ``approve_handoff``. The table has no such wait: the same situation is
# ``handoff.awaiting_submission`` with the pending action ``submit_upstream``.
# ``migrate_handoff_wait`` rewrites exactly that shape, by taking the table's
# ``nominate_for_handoff`` on the record as it was before the old code moved
# it. Any other record that is in the handoff phase without being in a table
# state has no reviewed migration, and the controller refuses to run on it.

HANDOFF_WAIT_MIGRATION = "handoff-awaiting-submission"
HANDOFF_WAIT_MIGRATED = "handoff_wait_migrated"

# Fields the table adds to a record; a migrated record carries all of them.
_ADDED_FIELDS = {"paused_at": None, "paused_reason": None, "paused_by": None,
                 "admission_override": None, "automatic_refresh_used": False}


def needs_migration(record: dict) -> bool:
    """Whether a record is in the handoff phase but not in a table state."""
    return record.get("phase") == "handoff" and engine.state_problem(record) is not None


def handoff_wait_shape_problem(record: dict) -> str | None:
    """Why a record is not exactly the old approval wait, or ``None``."""
    fork = record.get("fork") or {}
    head = fork.get("head_sha")
    request = record.get("handoff_request") or {}
    expected = {
        "phase": "handoff", "activity": "waiting_human", "paused": False,
        "blocked_reason": None, "active_work": None, "active_operation_id": None,
        "pending_publication": None, "handoff_approval": None, "upstream_pr": None,
        "merge": None, "refresh": None, "debug_commits": [],
    }
    for name, value in expected.items():
        if record.get(name) != value:
            return f"{name} is {record.get(name)!r}, not {value!r}"
    if (record.get("pending_human_action") or {}).get("kind") != "approve_handoff":
        return "the pending human action is not approve_handoff"
    if not head or not fork.get("pr_number") or not fork.get("repository_id"):
        return "no fork, development PR, and head are recorded"
    if request.get("head") != head or record.get("clean_head_sha") != head:
        return "the candidate head is not the recorded fork head"
    if request.get("revision") != record.get("revision"):
        return "the handoff request is not for the current revision"
    if not record.get("last_ci_evidence_id"):
        return "no CI evidence is recorded"
    if record.get("pending_feedback_evidence_ids"):
        return "actionable feedback is pending"
    return None


def is_handoff_wait(record: dict) -> bool:
    return needs_migration(record) and handoff_wait_shape_problem(record) is None


def migrate_handoff_wait(record: dict, *, evidence: dict | None, policy: dict,
                         now: str) -> tuple[dict, dict]:
    """The old approval wait as ``handoff.awaiting_submission``, with its event.

    ``evidence`` is the record's ``last_ci_evidence_id`` evidence; it must
    state that CI passed on the exact candidate head. Fork, development PR,
    head, revision, dossier, CI evidence, and budgets are kept. Raises
    ``TransitionError`` for any other shape.
    """
    problem = handoff_wait_shape_problem(record)
    if problem:
        raise TransitionError(f"no reviewed migration for this handoff record: {problem}")
    head = record["fork"]["head_sha"]
    if not evidence or evidence.get("evidence_id") != record["last_ci_evidence_id"] \
            or evidence.get("kind") != "ci" or (evidence.get("ci") or {}).get("head_sha") != head \
            or not str(evidence.get("summary") or "").endswith(": passed"):
        raise TransitionError("no reviewed migration for this handoff record: its CI evidence "
                              "does not state that CI passed on the candidate head")
    # The record as it was when CI passed, before the old code moved it.
    before = dict(record, phase="development", activity="waiting_ci", handoff_request=None,
                  pending_human_action=None, clean_head_sha=None)
    for name, value in _ADDED_FIELDS.items():
        before.setdefault(name, value)
    before["budget"] = dict(before["budget"])
    before["budget"].setdefault("investigation_attempts", 0)
    out = engine.apply_transition(
        before, "nominate_for_handoff", policy=policy, now=now,
        cause_id=record["last_ci_evidence_id"],
        facts={"ci_passed_on_head"},
        values={"last_ci_evidence_id": record["last_ci_evidence_id"],
                "handoff_request": dict(record["handoff_request"])})
    if not out.accepted:
        raise TransitionError(f"no reviewed migration for this handoff record: {out.reason}")
    new = out.record
    kept = ("contribution_id", "project_id", "revision", "generation", "fork", "upstream",
            "dossier_evidence_id", "last_ci_evidence_id", "handoff_request", "scope_paths",
            "proposal_artifact", "publication_operation_id", "processed_publications",
            "budget", "tracking_issue", "admission_class")
    for name in kept:
        if name == "budget":
            same = {k: v for k, v in new["budget"].items() if k != "investigation_attempts"} \
                == {k: v for k, v in record["budget"].items() if k != "investigation_attempts"}
        else:
            same = new.get(name) == record.get(name)
        if not same:
            raise TransitionError(f"handoff record migration would change {name}")
    event = {
        "schema_version": 1, "contribution_id": new["contribution_id"], "sequence": 0,
        "at": now, "kind": HANDOFF_WAIT_MIGRATED,
        "from": {"activity": record["activity"]}, "to": {"activity": new["activity"]},
        "cause": {"type": "migration", "id": HANDOFF_WAIT_MIGRATION},
        "revision_bumped": False,
        "details": {"verb": "nominate_for_handoff", "from_state": engine.state_key(record),
                    "to_state": engine.state_key(new), "head": head,
                    "revision": new["revision"],
                    "pending_human_action": {"from": "approve_handoff",
                                             "to": new["pending_human_action"]["kind"]}},
    }
    schemas.validate("event", event)
    return new, event


# The stop flag before ``blocked`` was merged into ``paused``: a record with a
# ``blocked_reason``, a paused record without ``paused_at``, and a stopped
# record that still holds what the table's stops clear. ``migrate_flags``
# rewrites exactly these shapes, as the table's stop would have left them.

FLAG_MIGRATION = "paused-flag-merge"
FLAG_MIGRATED = "paused_flag_migrated"
# The ``blocked_reason`` the controller wrote when the upstream identity
# changed; the table's ``pause_on_identity_change`` records this reason.
IDENTITY_BLOCK = "upstream repository identity changed"
IDENTITY_REASON = "upstream_identity_changed"


def _flags_migrated(record: dict) -> tuple[dict, list[str]]:
    """The record as the table's stop leaves it, and what was changed.
    Raises ``TransitionError`` for a shape the migration does not cover."""
    rec = copy.deepcopy(record)
    changed: list[str] = []
    blocked = record.get("blocked_reason")
    if blocked is not None:
        if not isinstance(blocked, str):
            raise TransitionError("blocked_reason is not text")
        if record["phase"] == "handoff":
            raise TransitionError("a handoff record is never blocked")
        identity = blocked == IDENTITY_BLOCK
        if not record.get("paused"):
            rec.update(paused=True, paused_by="controller" if identity else "human",
                       paused_reason=IDENTITY_REASON if identity else (blocked[:2000] or None),
                       paused_at=None)
        elif not rec.get("paused_reason"):
            rec["paused_reason"] = blocked[:2000] or None
        rec["blocked_reason"] = None
        changed.append("blocked_reason")
        if record["activity"] == "waiting_human" and not record.get("pending_human_action"):
            # A block recorded a human wait in place; a resume ended it. In
            # development the wait ends in the state of the table that
            # ``development_activity`` names.
            rec["activity"] = (development_activity(rec) if record["phase"] == "development"
                               else "idle")
        elif record["activity"] == "waiting_human" \
                and f"{record['phase']}.waiting_human" not in LIFECYCLE:
            raise TransitionError(f"{record['phase']}.waiting_human with a pending human action "
                                  "has no reviewed migration")
    if rec.get("paused") and not rec.get("paused_at"):
        rec["paused_at"] = record["updated_at"]
        changed.append("paused_at")
    if rec.get("paused") and not rec.get("paused_by"):
        rec["paused_by"] = "human"
        changed.append("paused_by")
    if stopped(rec):
        if rec.get("active_work"):
            rec["active_work"] = None
            changed.append("active_work")
            if rec["activity"] == "agent_running":
                moves = FLAGS["paused"].set_moves.get(engine.state_key(rec))
                rec["activity"] = moves.split(".", 1)[1] if moves else "idle"
        if rec.get("pending_publication") is not None:
            rec["pending_publication"] = None
            changed.append("pending_publication")
        if holds_unpublished_proposal(rec):
            rec["proposal_artifact"] = None
            changed.append("proposal_artifact")
    if changed and engine.state_key(rec) in DEVELOPMENT_LEGACY_STATES \
            and not holds_unpublished_proposal(rec) and rec.get("fork"):
        # A development state the table removed, without Work or a held
        # publication once stopped: the state of the table it stands for.
        rec["activity"] = development_activity(rec)
    return rec, changed


def needs_flag_migration(record: dict) -> bool:
    """Whether a record is in one of the shapes ``migrate_flags`` rewrites."""
    try:
        return bool(_flags_migrated(record)[1])
    except TransitionError:
        return True


def schema_view(record: dict) -> dict:
    """The record as the schema reads it while it awaits a record migration:
    a text ``blocked_reason`` (flag migration) and a terminal phase the
    table replaced by ``closed`` (terminal migration) are the values the
    current schema no longer admits."""
    view = record
    blocked = record.get("blocked_reason")
    if isinstance(blocked, str) and len(blocked) <= 2000:
        view = dict(view, blocked_reason=None)
    if needs_terminal_migration(record):
        view = dict(view, phase="closed")
    return view


def awaits_flag_migration(record: dict) -> bool:
    """Whether the flag migration rewrites this record without refusing it."""
    try:
        if not needs_flag_migration(record):
            return False
        migrate_flags(record, now=record["updated_at"])
    except (TransitionError, schemas.SchemaError, KeyError, TypeError, AttributeError):
        return False
    return True


def migrate_flags(record: dict, *, now: str) -> tuple[dict, dict]:
    """The record with ``blocked`` merged into ``paused``, with its event.

    ``blocked_reason`` becomes ``paused`` with ``paused_by`` (``controller``
    for the identity block, ``human`` otherwise) and ``paused_reason``; a
    block's human wait without a pending action becomes ``idle``, as the
    legacy resume left it. A paused record without ``paused_at`` gets its
    ``updated_at``. A stopped record loses the requested Work, the pending
    publication, and a held proposal, as the table's stops clear them.
    Nothing else changes. Raises ``TransitionError`` for any other shape.
    """
    new, changed = _flags_migrated(record)
    if not changed:
        raise TransitionError("the record needs no flag migration")
    new["updated_at"] = now
    problem = state_problem(new)
    if problem and not needs_provisioning_migration(new):
        # A development record without a fork is rewritten by the
        # provisioning migration in a later run.
        raise TransitionError(f"no reviewed flag migration for this record: {problem}")
    schemas.validate("contribution", new)
    event = _event(record, new, FLAG_MIGRATED, "migration", FLAG_MIGRATION, now, False)
    event["details"] = {"from_state": engine.state_key(record), "to_state": engine.state_key(new),
                        "changed": changed, "paused_by": new.get("paused_by"),
                        "paused_reason": new.get("paused_reason")}
    schemas.validate("event", event)
    return new, event


# The terminal phases before the lifecycle table: ``abandoned`` (written by
# ``/upstreaming abandon`` in any phase, and by the removal of the intake
# label), and ``no_work``, ``deferred``, and ``already_released``, which the
# schema admitted. The table has one terminal phase for work that is not
# needed, ``closed.idle``, whose reason is ``terminal_reason``.
# ``migrate_terminal`` rewrites exactly these records.

TERMINAL_MIGRATION = "closed-terminal-phase"
TERMINAL_MIGRATED = "terminal_phase_migrated"
LEGACY_TERMINAL_PHASES = ("abandoned", "no_work", "deferred", "already_released")
# The ``terminal_reason`` the removal of the intake label recorded; the
# table's ``withdraw_request`` records ``withdrawn``.
LABEL_REMOVED_REASON = f"{INTAKE_LABEL} label removed before Work admission"


def needs_terminal_migration(record: dict) -> bool:
    """Whether a record is in a terminal phase the table replaced."""
    return isinstance(record, dict) and record.get("phase") in LEGACY_TERMINAL_PHASES


def terminal_reason(record: dict) -> str:
    """The ``terminal_reason`` of ``closed.idle`` for a legacy terminal record:
    ``withdrawn`` for a removed intake label, ``abandoned`` for any other
    abandon, and the name of the phase for the others."""
    if record["phase"] == "abandoned":
        return "withdrawn" if record.get("terminal_reason") == LABEL_REMOVED_REASON \
            else "abandoned"
    return record["phase"]


def terminal_shape_problem(record: dict) -> str | None:
    """Why a legacy terminal record is not the shape the migration rewrites."""
    if not needs_terminal_migration(record):
        return f"phase {record.get('phase')!r} is not a legacy terminal phase"
    expected = {"activity": "idle", "blocked_reason": None, "active_work": None,
                "active_operation_id": None, "pending_publication": None}
    for name, value in expected.items():
        if record.get(name) != value:
            return f"{name} is {record.get(name)!r}, not {value!r}"
    if holds_unpublished_proposal(record):
        return "a proposal is held without having been published"
    return None


def awaits_terminal_migration(record: dict) -> bool:
    """Whether the terminal migration rewrites this record without refusing it."""
    if not needs_terminal_migration(record):
        return False
    try:
        migrate_terminal(record, now=record["updated_at"])
    except (TransitionError, schemas.SchemaError, KeyError, TypeError, AttributeError):
        return False
    return True


def migrate_terminal(record: dict, *, now: str) -> tuple[dict, dict]:
    """The legacy terminal record as ``closed.idle``, with its event.

    ``terminal_reason`` becomes the table's reason (see ``terminal_reason``);
    the text it held before is kept in the event. Nothing else changes: the
    revision, the generation, the flag, the evidence, and the replay fences
    are kept. Raises ``TransitionError`` for any other shape: open Work, an
    open operation, a pending publication, a held proposal, a text
    ``blocked_reason``, or an activity other than ``idle``.
    """
    problem = terminal_shape_problem(record)
    if problem:
        raise TransitionError(f"no reviewed terminal migration for this record: {problem}")
    new = copy.deepcopy(record)
    new.update(phase="closed", activity="idle", terminal_reason=terminal_reason(record),
               updated_at=now)
    problem = state_problem(new)
    if problem:
        raise TransitionError(f"no reviewed terminal migration for this record: {problem}")
    schemas.validate("contribution", new)
    event = _event(record, new, TERMINAL_MIGRATED, "migration", TERMINAL_MIGRATION, now, False)
    event["details"] = {"from_state": engine.state_key(record), "to_state": engine.state_key(new),
                        "terminal_reason": {"from": record.get("terminal_reason"),
                                            "to": new["terminal_reason"]}}
    schemas.validate("event", event)
    return new, event


# The shapes of the investigation and provisioning phases, and of the path
# that proposed a patch before the fork existed, that the table does not
# have. Provisioning is planned and launched from `provisioning.idle` only,
# and patch Work is requested only on a provisioned fork. ``migrate_provisioning``
# rewrites exactly these shapes:
#
# * `provisioning.waiting_capacity` (a planned or launched provisioning
#   intent) becomes `provisioning.idle`; the planned operation is kept and
#   launched by `start_operation`;
# * `provisioning.waiting_human` without a pending action, or with the
#   retired `provision_fork`, becomes `provisioning.idle`;
# * `development.idle` or `development.waiting_capacity` without a
#   provisioned fork (a deferred fork intent, or a patch proposed before the
#   fork) becomes `provisioning.idle` at a new revision;
# * `development.idle` or `development.waiting_capacity` holding a proposal
#   that was never published on the provisioned fork, and
#   `development.waiting_human` without a pending action holding one, become
#   the development state of the table that ``development_activity`` names,
#   at a new revision, so no Work or publication of the earlier revision is
#   reused.
#
# A proposal held without having been published is cleared, together with
# the publication pending for it. Any other record in an investigation or
# provisioning state outside the table, or holding such a proposal, has no
# reviewed migration and fails closed.

PROVISIONING_MIGRATION = "provisioning-table-states"
PROVISIONING_MIGRATED = "provisioning_state_migrated"
# The pending action of the provisioning wait before the table: a human was
# asked to provision the fork with the retired `/upstreaming provision`.
RETIRED_PROVISIONING_WAIT = "provision_fork"
_PRE_FORK_STATES = ("development.idle", "development.waiting_capacity")
# The wait without a pending action that the code before the table entered
# from `development.waiting_capacity` while it held such a proposal for a
# publish-only retry.
HELD_PROPOSAL_WAIT = "development.waiting_human"


def needs_provisioning_migration(record: dict) -> bool:
    """Whether a record is in a shape ``migrate_provisioning`` rewrites or
    refuses: a state of the investigation or provisioning phase that the
    table does not have, the provisioning wait without a pending action the
    table writes, development without a provisioned fork, or a proposal held
    without having been published."""
    if not isinstance(record, dict) or not isinstance(record.get("phase"), str):
        return False
    state = engine.state_key(record)
    if record["phase"] in ("investigation", "provisioning") and state not in LIFECYCLE:
        return True
    pending = (record.get("pending_human_action") or {}).get("kind")
    if state == "provisioning.waiting_human" and pending in (None, RETIRED_PROVISIONING_WAIT):
        return True
    if state in _PRE_FORK_STATES and not (record.get("fork") or {}).get("repository_id"):
        return True
    return holds_unpublished_proposal(record)


def provisioning_shape_problem(record: dict, operation: dict | None, *,
                               check_operation: bool = True) -> str | None:
    """Why a record that ``needs_provisioning_migration`` is not exactly a
    shape the migration rewrites. ``operation`` is the store's record of the
    operation the record names as active; ``check_operation=False`` judges
    the record alone."""
    state = engine.state_key(record)
    if record.get("active_work") is not None:
        return "Work is requested"
    held = holds_unpublished_proposal(record)
    pending_publication = record.get("pending_publication")
    if pending_publication is not None and not (
            held and str(pending_publication.get("worker_run_id"))
            == str(record["proposal_artifact"].get("run_id"))):
        return "a publication is pending"
    if state == "provisioning.waiting_capacity":
        op_id = record.get("active_operation_id")
        if op_id is not None and check_operation and (
                operation is None or operation.get("operation_id") != op_id
                                  or operation.get("kind") != "fork_provision"
                                  or operation.get("status") != "planned"):
            return "the active operation is not a planned fork provisioning operation"
        return None
    if record.get("active_operation_id") is not None:
        return "an operation is active"
    if state == "provisioning.waiting_human":
        if (record.get("pending_human_action") or {}).get("kind") not in (
                None, RETIRED_PROVISIONING_WAIT):
            return "the provisioning wait holds a pending action of the table"
        return None
    if state in _PRE_FORK_STATES:
        if waits_for_human(record):
            return "the record waits for a human"
        return None
    if state == HELD_PROPOSAL_WAIT and held:
        if record.get("pending_human_action") is not None:
            return f"{state} holds the pending human action"
        if not (record.get("fork") or {}).get("repository_id"):
            return "no fork is recorded"
        return None
    if held:
        return f"a proposal is held without having been published in {state}"
    return f"{state} is not a state of the table"


def _provisioning_target(record: dict) -> tuple[str, bool]:
    """The state a migrated record is in, and whether the revision moves."""
    state = engine.state_key(record)
    if state in _PRE_FORK_STATES or state == HELD_PROPOSAL_WAIT:
        if not (record.get("fork") or {}).get("repository_id"):
            return "provisioning.idle", True
        # The held proposal is discarded: development continues in the state
        # of the table the development migration names for the record
        # without it.
        return f"development.{development_activity(record)}", True
    return "provisioning.idle", False


def awaits_provisioning_migration(record: dict) -> bool:
    """Whether the provisioning migration rewrites this record without
    refusing it, judged from the record alone (the operation it names is
    checked when the migration runs)."""
    if not needs_provisioning_migration(record):
        return False
    try:
        migrate_provisioning(record, operation=None, now=record["updated_at"],
                             check_operation=False)
    except (TransitionError, schemas.SchemaError, KeyError, TypeError, AttributeError):
        return False
    return True


def migrate_provisioning(record: dict, *, operation: dict | None, now: str,
                         check_operation: bool = True) -> tuple[dict, dict]:
    """The record in the state of the table its shape stands for, with its
    event (see the shapes above). The planned operation, the fork, the
    dossier, the scope, the flag, the budgets, and the replay fences are
    kept; a pending human action of the retired provisioning wait is
    cleared, and a proposal held without having been published is cleared
    with the publication pending for it. The revision moves when development
    without a fork returns to provisioning and when a held proposal is
    discarded, so no Work or operation of the earlier revision is reused.
    Raises ``TransitionError`` for any other shape."""
    if not needs_provisioning_migration(record):
        raise TransitionError("the record needs no provisioning migration")
    problem = provisioning_shape_problem(record, operation, check_operation=check_operation)
    if problem:
        raise TransitionError(f"no reviewed provisioning migration for this record: {problem}")
    target, bump = _provisioning_target(record)
    new = copy.deepcopy(record)
    changed: list[str] = []
    if new.get("pending_human_action") is not None:
        new["pending_human_action"] = None
        changed.append("pending_human_action")
    if holds_unpublished_proposal(new):
        new["proposal_artifact"] = None
        changed.append("proposal_artifact")
        bump = True
    if new.get("pending_publication") is not None:
        new["pending_publication"] = None
        changed.append("pending_publication")
    new["phase"], new["activity"] = target.split(".", 1)
    if bump:
        new["revision"] = record["revision"] + 1
    new["updated_at"] = now
    problem = state_problem(new)
    if problem:
        raise TransitionError(f"no reviewed provisioning migration for this record: {problem}")
    schemas.validate("contribution", new)
    event = _event(record, new, PROVISIONING_MIGRATED, "migration", PROVISIONING_MIGRATION, now,
                   bump)
    event["details"] = {"from_state": engine.state_key(record), "to_state": target,
                        "changed": changed,
                        "pending_human_action": (record.get("pending_human_action") or {}).get(
                            "kind"),
                        "active_operation_id": new.get("active_operation_id")}
    schemas.validate("event", event)
    return new, event


# The development states the table does not have, and the pending action
# the removed Work deadline wrote. ``migrate_development`` rewrites exactly
# these shapes:
#
# * `development.idle` (Work due, CI passed, a `no_change` or stale Result),
#   `development.waiting_capacity` without a held publication (a planned
#   development PR operation), and `development.waiting_human` without a
#   pending action (the CI access wait) become `development.waiting_ci` when
#   CI evidence is recorded for the head or the head is a published one (it
#   differs from the provisioned base), and `development.fix_needed`
#   otherwise (``development_activity``);
# * `development.waiting_capacity` holding a Result for a publish-only retry
#   becomes `development.agent_running`, where `schedule_publish_retry` keeps
#   it;
# * a human wait (`investigation`, `development`, or `upstream_review`) with
#   the pending action `task_without_outcome` keeps its state with the
#   pending action `work_failed`, which `exhaust_work_recovery` writes and the
#   resume of the wait admits.
#
# The revision, the fork, the evidence, the budgets, the flag, the operation,
# and the replay fences are kept. Any other record in one of these states
# (requested Work, a held publication that is exhausted or of a stopped
# record, a pending action in the removed states) fails closed.

DEVELOPMENT_MIGRATION = "development-table-states"
DEVELOPMENT_MIGRATED = "development_state_migrated"
DEVELOPMENT_LEGACY_STATES = ("development.idle", "development.waiting_capacity")
# The pending action of the removed Work deadline, and the one of the table.
TASK_WITHOUT_OUTCOME = "task_without_outcome"
WORK_FAILED = "work_failed"
_DEADLINE_WAITS = ("investigation.waiting_human", "development.waiting_human",
                   "upstream_review.waiting_human")


def development_activity(record: dict) -> str:
    """The development activity of the table for a record that holds no Work
    and no publication: ``waiting_ci`` when CI evidence is recorded for the
    head or the head differs from the provisioned base (a published head),
    ``fix_needed`` otherwise (development Work is due)."""
    head = (record.get("fork") or {}).get("head_sha")
    base = (record.get("upstream") or {}).get("base_sha")
    if record.get("last_ci_evidence_id") or (head and head != base):
        return "waiting_ci"
    return "fix_needed"


def needs_development_migration(record: dict) -> bool:
    """Whether a record is in a shape ``migrate_development`` rewrites or
    refuses. A shape the provisioning migration rewrites (development
    without a fork, a held proposal) is left to it."""
    if not isinstance(record, dict) or not isinstance(record.get("phase"), str) \
            or needs_provisioning_migration(record):
        return False
    state = engine.state_key(record)
    pending = (record.get("pending_human_action") or {}).get("kind")
    if state in DEVELOPMENT_LEGACY_STATES:
        return True
    if state == "development.waiting_human" and pending is None:
        return True
    return state in _DEADLINE_WAITS and pending == TASK_WITHOUT_OUTCOME


def development_shape_problem(record: dict) -> str | None:
    """Why a record that ``needs_development_migration`` is not exactly a
    shape the migration rewrites."""
    state = engine.state_key(record)
    pending = record.get("pending_human_action")
    publication = record.get("pending_publication")
    if record.get("active_work") is not None:
        return "Work is requested"
    if state in _DEADLINE_WAITS and pending:
        return "a publication is pending" if publication is not None else None
    if pending is not None:
        return f"{state} holds the pending human action {pending.get('kind')}"
    if publication is not None:
        if state != "development.waiting_capacity":
            return f"a publication is pending in {state}"
        if publication.get("exhausted"):
            return "the held publication is exhausted outside a human wait"
        if stopped(record):
            return "a stopped record holds a publication"
    if not (record.get("fork") or {}).get("repository_id"):
        return "no fork is recorded"
    return None


def _development_target(record: dict) -> str:
    """The state of the table a development shape stands for."""
    state = engine.state_key(record)
    if (record.get("pending_human_action") or {}).get("kind") == TASK_WITHOUT_OUTCOME:
        return state
    if state == "development.waiting_capacity" and record.get("pending_publication"):
        return "development.agent_running"
    return f"development.{development_activity(record)}"


def awaits_development_migration(record: dict) -> bool:
    """Whether the development migration rewrites this record without
    refusing it."""
    if not needs_development_migration(record):
        return False
    try:
        migrate_development(record, now=record["updated_at"])
    except (TransitionError, schemas.SchemaError, KeyError, TypeError, AttributeError):
        return False
    return True


def migrate_development(record: dict, *, now: str) -> tuple[dict, dict]:
    """The record in the state of the table its shape stands for, with its
    event (see the shapes above). Nothing but the activity and, for the
    removed Work deadline's wait, the kind of the pending action changes.
    Raises ``TransitionError`` for any other shape."""
    if not needs_development_migration(record):
        raise TransitionError("the record needs no development migration")
    problem = development_shape_problem(record)
    if problem:
        raise TransitionError(f"no reviewed development migration for this record: {problem}")
    target = _development_target(record)
    new = copy.deepcopy(record)
    changed: list[str] = []
    pending = record.get("pending_human_action")
    if (pending or {}).get("kind") == TASK_WITHOUT_OUTCOME:
        new["pending_human_action"] = dict(pending, kind=WORK_FAILED, counter=None)
        changed.append("pending_human_action")
    new["phase"], new["activity"] = target.split(".", 1)
    if target != engine.state_key(record):
        changed.append("activity")
    new["updated_at"] = now
    problem = engine.state_problem(new)
    if problem:
        raise TransitionError(f"no reviewed development migration for this record: {problem}")
    schemas.validate("contribution", new)
    event = _event(record, new, DEVELOPMENT_MIGRATED, "migration", DEVELOPMENT_MIGRATION, now,
                   False)
    event["details"] = {"from_state": engine.state_key(record), "to_state": target,
                        "changed": changed,
                        "pending_human_action": {
                            "from": (pending or {}).get("kind"),
                            "to": (new.get("pending_human_action") or {}).get("kind")},
                        "active_operation_id": new.get("active_operation_id")}
    schemas.validate("event", event)
    return new, event


def legacy_update(record: dict, site: str, **fields: Any) -> dict:
    """A copy of ``record`` with ``fields`` set, for a call site that is not
    converted to a table verb yet.

    ``site`` must be registered in ``LEGACY_SITES``. The write is refused
    when it would touch the handoff phase or leave the record in a state
    that is neither in the table nor in ``LEGACY_STATES``.
    """
    if site not in LEGACY_SITES:
        raise TransitionError(f"{site} is not a registered legacy write site")
    new = dict(record, **fields)
    _check_legacy_write(record, new, site)
    return new


def legacy_assign(record: dict, site: str, **fields: Any) -> None:
    """``legacy_update`` in place, for code that builds its result on a copy."""
    if site not in LEGACY_SITES:
        raise TransitionError(f"{site} is not a registered legacy write site")
    before = {k: record.get(k) for k in (*engine.FENCED_FIELDS, "pending_human_action")}
    record.update(fields)
    _check_legacy_write(before, record, site)


def _check_legacy_write(before: dict, after: dict, site: str) -> None:
    if all(before.get(k) == after.get(k) for k in engine.FENCED_FIELDS):
        return
    if "handoff" in (before["phase"], after["phase"]):
        raise TransitionError(f"{site}: handoff states are written by the transition engine only")
    if waits_for_human(before) and engine.state_key(before) != engine.state_key(after):
        raise TransitionError(f"{site}: {engine.state_key(before)} waits for a human and is left "
                              "only by the rows the table lists for it")
    problem = state_problem(after)
    if problem:
        raise TransitionError(f"{site}: {problem}")


# The phases whose states can request Work outside the handoff phase.
WORK_PHASES = frozenset({"investigation", "provisioning", "development", "upstream_review"})


def record_late_effect(record: dict, *, policy: dict, now: str, cause_id: str | None,
                       details: dict[str, Any]) -> dict:
    """The audit event of the table's ``record_late_effect`` for ``record``.

    The rule applies in every state and never changes the record. A state of
    the table takes it through the engine. A record the engine cannot take
    it from (a state in ``LEGACY_STATES``, or one that fails the engine's
    own checks) gets the same event written here, so a Result that matches
    nothing can never stop a reconciliation.
    """
    try:
        outcome = engine.apply_transition(record, "record_late_effect", policy=policy, now=now,
                                          cause_id=cause_id, facts={"result_mismatch"},
                                          details=details)
        if outcome.accepted and outcome.record == record:
            return outcome.event
    except (TransitionError, schemas.SchemaError):
        pass
    event = _event(record, record, "record_late_effect", "result", cause_id, now, False)
    state = engine.state_key(record)
    event["details"] = {"verb": "record_late_effect", "from_state": state, "to_state": state,
                        **details}
    schemas.validate("event", event)
    return event


def stopped(record: dict) -> bool:
    """Whether a record is stopped: paused, parked, or terminal."""
    return bool(record.get("paused") or record["phase"] in TERMINAL | {"parked"})


def holds_unpublished_proposal(record: dict) -> bool:
    """Whether a record holds a worker proposal it never published (the
    `outcome` `proposal` of the removed path that proposed a patch before the
    fork existed). No path writes one any more; the record migrations clear
    or refuse it."""
    return (record.get("proposal_artifact") or {}).get("outcome") == "proposal"


def _bump(rec: dict) -> None:
    rec["revision"] += 1


def observe_head(record: dict, new_head: str, *, now: str, cause_id: str | None = None) -> Outcome:
    """Record an observed fork head. A changed head is changed requested work:
    bump the revision and invalidate head-bound readiness and approval."""
    if lifecycle.recorded_head(record) == new_head:
        return Outcome(record, True, "unchanged", None)
    if record["phase"] == "handoff":
        raise TransitionError("handoff states are written by the transition engine only")
    rec = copy.deepcopy(record)
    rec["fork"]["head_sha"] = new_head
    if record["phase"] == "upstream_review" and rec.get("upstream_pr"):
        # The head of the upstream PR from `riscv64` changed.
        rec["upstream_pr"]["head_sha"] = new_head
    rec["last_ci_evidence_id"] = None
    if rec.get("upstream_pr") is None:
        rec["handoff_approval"] = None
    rec["updated_at"] = now
    _bump(rec)
    schemas.validate("contribution", rec)
    return Outcome(rec, True, None, _event(record, rec, "head_changed", "observation",
                                           cause_id, now, True))


def observe_status(record: dict, *, activity: str | None = None, next_poll_at: str | None = None,
                   now: str) -> Outcome:
    """Routine observation; never bumps revision, never invalidates work."""
    rec = copy.deepcopy(record)
    if activity is not None:
        if activity not in ACTIVITIES:
            raise TransitionError(f"unknown activity {activity!r}")
        rec["activity"] = activity
        _check_legacy_write(record, rec, "observe_status")
    if next_poll_at is not None:
        rec["next_poll_at"] = next_poll_at
    semantic = rec.get("activity") != record.get("activity")
    if semantic:
        rec["updated_at"] = now
    schemas.validate("contribution", rec)
    ev = _event(record, rec, "activity", "observation", None, now, False) if semantic else None
    return Outcome(rec, True, None, ev)


# --------------------------------------------------------------------------
# Commands


def _reject(record: dict, reason: str) -> Outcome:
    return Outcome(record, False, reason, None)


def park(rec: dict, reason: str) -> dict:
    """End the current generation in place.

    The completed generation is retained for audit and late-effect
    reconciliation; only exact-revision ``resume`` restarts it. A pending
    publication ends with the generation. Callers own revision bumps and the
    event."""
    if rec["phase"] == "handoff":
        raise TransitionError("handoff states are written by the transition engine only")
    rec["previous_phase"] = rec["phase"]
    rec["phase"] = "parked"
    rec["parked_reason"] = reason[:2000]
    rec["activity"] = "idle"
    rec["pending_publication"] = None
    return rec


def _end_requested(record: dict, rec: dict) -> None:
    """What a stop, or a move to a new revision, ends in ``rec``, the changed
    copy of ``record``."""
    # Stopping a contribution or moving it to a new revision ends any
    # requested task: a late worker outcome is then ignored as stale, and
    # dispatch recovery must not launch it again.
    if rec.get("active_work") and (rec["paused"] or rec["phase"] in TERMINAL | {"parked"}
                                   or rec["revision"] != record["revision"]):
        rec["active_work"] = None
        if rec["activity"] == "agent_running":
            rec["activity"] = "idle"
    # A stop also clears the pending publication, as the table's stop rows
    # do: what is held is invalidated and its publication Result takes
    # ``record_late_effect``. A branch the worker run already pushed is
    # recorded by head observation. The command that ends a stop clears it
    # too, so nothing held is published after a stop that began before this
    # rule.
    if stopped(rec) or stopped(record):
        waited_on_held = bool(rec.get("pending_publication"))
        rec["pending_publication"] = None
        if waited_on_held and rec["activity"] in ("agent_running", "waiting_capacity"):
            rec["activity"] = "idle"


def is_legacy_state(record: dict) -> bool:
    """Whether a record is in a state that only the legacy paths write."""
    return engine.state_key(record) not in LIFECYCLE


# The verbs that set ``paused``. In a state of the table they are taken
# through the engine; ``legacy_pause`` writes them for a record in a state
# of ``LEGACY_STATES``.
PAUSE_VERBS = {"pause": ("human", "command"),
               "pause_on_identity_change": ("controller", "observation"),
               "pause_on_processing_error": ("controller", "tick")}


def legacy_pause(record: dict, verb: str, *, reason: str | None, now: str,
                 cause_id: str | None, details: dict[str, Any] | None = None) -> Outcome:
    """Set ``paused`` on a record in a legacy state, as the table's ``verb``
    does in a state of the table.

    The flag records ``paused_at``, ``paused_by`` and ``paused_reason``; the
    requested Work and the pending publication are cleared. A legacy state has no row to move to, so the activity that held
    the Work or the publication (``agent_running``, ``waiting_capacity``)
    becomes ``idle``, as the legacy paths always did. The outcome is not
    accepted, and nothing changes, when the record is already paused or is
    terminal. A record in a state of the table raises: the engine takes it.
    """
    if verb not in PAUSE_VERBS:
        raise TransitionError(f"{verb} does not set paused")
    if not is_legacy_state(record):
        raise TransitionError(f"{engine.state_key(record)} is a state of the table; "
                              f"{verb} is taken by the transition engine")
    by, cause = PAUSE_VERBS[verb]
    if by == "controller" and not (reason or "").strip():
        raise TransitionError(f"{verb} sets paused_reason; the caller states it")
    if record.get("paused"):
        return Outcome(record, False, "contribution is paused", None)
    if record["phase"] in TERMINAL:
        return Outcome(record, False, f"contribution is terminal ({record['phase']})", None)
    rec = copy.deepcopy(record)
    rec.update(paused=True, paused_at=now, paused_by=by,
               paused_reason=reason[:2000] if reason else None, updated_at=now)
    _end_requested(record, rec)
    _check_legacy_write(record, rec, verb)
    schemas.validate("contribution", rec)
    event = _event(record, rec, verb, cause, cause_id, now, False)
    event["details"] = {"verb": verb, "from_state": engine.state_key(record),
                        "to_state": engine.state_key(rec), **(details or {})}
    schemas.validate("event", event)
    return Outcome(rec, True, None, event)


# The commands ``apply_command`` applies itself: the stop and resume commands
# on a record in a state of ``LEGACY_STATES``, and the intake command
# ``authorize-followup``. Every other command of the table applies in states
# of the table only.
LEGACY_COMMANDS = frozenset({"pause", "resume", "park", "authorize-followup"})


def apply_command(record: dict, cmd: Command, *, command_id: str, actor: dict,
                  now: str) -> Outcome:
    """Apply an already authenticated, not-yet-consumed command that the
    engine does not take (``engine.takes_command``): ``pause``, ``resume``,
    or ``park`` on a record in a legacy state, and ``authorize-followup``. Any other command of the
    table on a record in a legacy state is rejected with the states it
    applies in.

    Actor authority, replay and digest checks happen in ``intake.accept_command``
    before this is called.
    """
    if cmd.verb == "resolve-operation":
        raise TransitionError("resolve-operation applies to operations, see recovery.py")
    if engine.takes_command(record, cmd.verb):
        raise TransitionError(f"`{cmd.verb}` in `{engine.state_key(record)}` is applied by the "
                              "transition engine")
    if cmd.contribution is not None and cmd.contribution != record["contribution_id"]:
        return _reject(record, "command names another contribution than this issue's")
    if cmd.verb not in LEGACY_COMMANDS:
        return _reject(record, engine.not_applicable(cmd.verb, engine.state_key(record)))
    rev = cmd.revision
    cur = record["revision"]
    phase = record["phase"]
    if rev is None:
        # Only stop commands may omit it (enforced by the grammar).
        rev = cur
    if rev > cur:
        return _reject(record, "revision is not a known revision")
    # Stopping is always safe, so a stale pause/park is honoured.
    # Everything that (re)activates or approves work must name the current revision.
    if cmd.verb not in ("pause", "park") and rev != cur:
        return _reject(record, f"stale revision {rev}, current is {cur}")
    if phase in TERMINAL and cmd.verb != "authorize-followup":
        return _reject(record, f"contribution is terminal ({phase})")

    rec = copy.deepcopy(record)
    rec["updated_at"] = now
    bumped = False
    v = cmd.verb
    a = cmd.args

    if v == "pause":
        out = legacy_pause(record, "pause", reason=a.get("reason"), now=now, cause_id=command_id,
                           details={"actor": {"id": actor["id"], "login": actor["login"]}})
        if not out.accepted:
            return _reject(record, "already paused" if record.get("paused") else out.reason)
        return out
    elif v == "resume":
        if not rec["paused"]:
            return _reject(record, "nothing to resume")
        if "class" in a or "paths" in a:
            return _reject(record, "`class` and `paths` apply only to a resume from `parked.idle` "
                                   "that starts a new generation")
        rec.update(paused=False, paused_at=None, paused_by=None, paused_reason=None)
        _bump(rec)
        bumped = True
    elif v == "park":
        if phase == "parked":
            return _reject(record, "already parked")
        park(rec, a["reason"])
    elif v == "authorize-followup":
        if phase not in ("release_wait", "released"):
            return _reject(record, "follow-ups need a merged contribution")
        # The completed record is left intact; intake.new_followup creates
        # the new identity. Here we only record the event.
    else:  # pragma: no cover - LEGACY_COMMANDS and this function must agree
        raise TransitionError(f"unhandled verb {v}")

    _end_requested(record, rec)

    _check_legacy_write(record, rec, "apply_command")
    if rec != record or v == "authorize-followup":
        schemas.validate("contribution", rec)
    kind = f"command:{v}"
    return Outcome(rec, True, None, _event(record, rec, kind, "command", command_id, now, bumped))


def command_priority(cmd: Command) -> int:
    """Urgent stop commands sort first, then other controls."""
    return 0 if cmd.verb in URGENT else 1


