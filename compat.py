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

``LEGACY_SITES`` and ``LEGACY_STATES`` shrink as call sites are converted;
when both are empty this module is deleted.
"""

from __future__ import annotations

import copy
from typing import Any

import engine
import schemas
from commands import URGENT, Command
from common import DEV_REF, validate_scope_paths
from lifecycle import ACTIVITIES, LIFECYCLE, TERMINAL, Outcome, TransitionError, _event

# States that are not in the lifecycle table and that legacy paths still
# write. Each is removed together with the paths that write it.
_LEGACY_PHASES = ("investigation", "provisioning", "development", "upstream_review")
_LEGACY_ACTIVITIES = ("idle", "agent_running", "waiting_ci", "fix_needed", "waiting_capacity",
                      "waiting_human")
LEGACY_STATES = frozenset(
    # The legacy paths combine a working phase with any of the activities
    # they know: `idle` where Work is due or CI passed, `waiting_capacity`
    # where the table keeps the state whose guard does not hold yet.
    {f"{phase}.{activity}" for phase in _LEGACY_PHASES for activity in _LEGACY_ACTIVITIES}
    # `block` records a human wait in place, also before and after the work.
    | {"queued.waiting_human", "parked.waiting_human"}
    # `abandon` and a removed intake label; `closed.idle` replaces it.
    | {"abandoned.idle"}) - frozenset(LIFECYCLE)

# The call sites that still write lifecycle state outside the table, by the
# identifier each passes to ``legacy_update``.
LEGACY_SITES = frozenset({
    "canary.repair_mismatch",
    "controller.select_work",
    "controller.defer_destination_operation",
    "controller.cancel_unlabeled_request",
    "controller.block_identity_change",
    "controller.rearm_provisioning",
    "controller.plan_provisioning",
    "controller.plan_development_pr",
    "controller.plan_ci_bootstrap",
    "controller.publish_pre_fork_proposal",
    "observe_ci.review_feedback",
    "observe_ci.ci_passed",
    "observe_ci.withdraw_obsolete_repair",
    "reconcile.confirm_development_pr",
    "reconcile.confirm_provisioning",
    "reconcile.publication_outcome",
    "reconcile.work_timeout",
    "reconcile.terminal_work_retry",
    "reconcile.park_terminal_work",
    "reconcile.dispatch_exhausted",
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
        facts={"ci_passed_on_head", "no_pending_actionable_feedback"},
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
    before = {k: record.get(k) for k in engine.FENCED_FIELDS}
    record.update(fields)
    _check_legacy_write(before, record, site)


def _check_legacy_write(before: dict, after: dict, site: str) -> None:
    if all(before.get(k) == after.get(k) for k in engine.FENCED_FIELDS):
        return
    if "handoff" in (before["phase"], after["phase"]):
        raise TransitionError(f"{site}: handoff states are written by the transition engine only")
    problem = state_problem(after)
    if problem:
        raise TransitionError(f"{site}: {problem}")


def _bump(rec: dict) -> None:
    rec["revision"] += 1


def observe_head(record: dict, new_head: str, *, now: str, cause_id: str | None = None) -> Outcome:
    """Record an observed fork head. A changed head is changed requested work:
    bump the revision and invalidate head-bound readiness and approval."""
    fork = record.get("fork") or {}
    if fork.get("head_sha") == new_head:
        return Outcome(record, True, "unchanged", None)
    if record["phase"] == "handoff":
        raise TransitionError("handoff states are written by the transition engine only")
    rec = copy.deepcopy(record)
    rec["fork"]["head_sha"] = new_head
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


def _restart_generation(record: dict, now: str) -> dict:
    """Reset mutable lifecycle state while retaining identity and replay fences."""
    rec = engine.reset_generation(record)
    rec.update({"phase": "queued", "activity": "idle", "updated_at": now})
    _bump(rec)
    return rec


def park(rec: dict, reason: str) -> dict:
    """End the current generation in place.

    The completed generation is retained for audit and late-effect
    reconciliation; only exact-revision ``resume`` restarts it. Callers own
    revision bumps and the event."""
    if rec["phase"] == "handoff":
        raise TransitionError("handoff states are written by the transition engine only")
    rec["previous_phase"] = rec["phase"]
    rec["phase"] = "parked"
    rec["parked_reason"] = reason[:2000]
    rec["activity"] = "idle"
    return rec


def apply_command(record: dict, cmd: Command, *, command_id: str, actor: dict, now: str,
                  provisioning: dict | None = None,
                  backup_user: dict | None = None) -> Outcome:
    """Apply an already authenticated, not-yet-consumed command outside the
    handoff phase.

    Actor authority, replay and digest checks happen in ``intake.accept_command``
    before this is called.
    """
    if cmd.verb == "resolve-operation":
        raise TransitionError("resolve-operation applies to operations, see recovery.py")
    if record["phase"] == "handoff":
        raise TransitionError("commands in the handoff phase are applied by the transition engine")
    if cmd.contribution is not None and cmd.contribution != record["contribution_id"]:
        return _reject(record, "command names another contribution than this issue's")
    rev = cmd.revision
    cur = record["revision"]
    phase = record["phase"]
    if rev is None:
        # Only stop commands may omit it (enforced by the grammar).
        rev = cur
    if rev > cur:
        return _reject(record, "revision is not a known revision")
    # Stopping is always safe, so a stale pause/park/block/abandon is honoured.
    # Everything that (re)activates or approves work must name the current revision.
    if cmd.verb not in ("pause", "block", "park", "abandon") and rev != cur:
        return _reject(record, f"stale revision {rev}, current is {cur}")
    if phase in TERMINAL and cmd.verb not in ("authorize-followup", "restart"):
        return _reject(record, f"contribution is terminal ({phase})")

    rec = copy.deepcopy(record)
    rec["updated_at"] = now
    bumped = False
    v = cmd.verb
    a = cmd.args

    if v == "pause":
        if rec["paused"]:
            return _reject(record, "already paused")
        rec.update(paused=True, paused_at=now, paused_by="human",
                   paused_reason=a.get("reason"))
    elif v == "block":
        rec["blocked_reason"] = a.get("reason") or "blocked"
        rec["activity"] = "waiting_human"
    elif v == "resume":
        if phase == "parked":
            # Resume from parked needs
            # quiescence and starts a clean generation at queued:idle, so the
            # next generation cannot bypass a complete verified dossier.
            if record.get("active_work"):
                return _reject(record, "cannot resume from parked while Work is active")
            if record.get("active_operation_id") or record.get("pending_publication"):
                return _reject(record, "cannot resume from parked while a writer operation is active")
            rec = _restart_generation(record, now)
            bumped = True
        elif rec["paused"] or rec.get("blocked_reason"):
            rec.update(paused=False, paused_at=None, paused_by=None, paused_reason=None)
            rec["blocked_reason"] = None
            if rec["activity"] == "waiting_human" and rec.get("pending_human_action") is None:
                rec["activity"] = "idle"
        else:
            return _reject(record, "nothing to resume")
        if not bumped:
            _bump(rec)
            bumped = True
    elif v == "park":
        if phase == "parked":
            return _reject(record, "already parked")
        park(rec, a["reason"])
    elif v == "abandon":
        rec["phase"] = "abandoned"
        rec["terminal_reason"] = a["reason"]
        rec["activity"] = "idle"
    elif v == "restart":
        if phase != "abandoned":
            return _reject(record, f"restart needs phase abandoned, not {phase}")
        if record.get("active_work"):
            return _reject(record, "cannot restart while Work is active")
        if record.get("active_operation_id") or record.get("pending_publication"):
            return _reject(record, "cannot restart while a writer operation is active")
        rec = _restart_generation(record, now)
        bumped = True
    elif v == "refresh":
        if phase != "development":
            return _reject(record, f"refresh needs an existing development PR, not phase {phase}")
        fork = record.get("fork") or {}
        if not fork.get("pr_number") or not fork.get("head_sha"):
            return _reject(record, "refresh needs a verified development PR and head")
        if record.get("active_work"):
            return _reject(record, "cannot refresh while Work is active")
        if record.get("active_operation_id") or record.get("pending_publication"):
            return _reject(record, "cannot refresh while a writer operation is active")
        rec["refresh"] = {"command_id": command_id, "reason": a["reason"],
                          "from_phase": phase, "head_sha": fork["head_sha"],
                          "pr_number": fork["pr_number"]}
        rec["phase"] = "queued"
        rec["activity"] = "idle"
        rec["dossier_evidence_id"] = None
        rec["scope_coverage_complete"] = False
        rec["scope_paths"] = None
        rec["proposal_artifact"] = None
        rec["last_ci_evidence_id"] = None
        rec["clean_head_sha"] = None
        rec["handoff_request"] = None
        rec["handoff_approval"] = None
        rec["pending_human_action"] = None
        rec["publication_operation_id"] = None
        _bump(rec)
        bumped = True
    elif v == "clarify":
        if rec["activity"] != "waiting_human" and not rec.get("blocked_reason"):
            return _reject(record, "no pending question")
        rec["activity"] = "idle"
        rec["pending_human_action"] = None
        _bump(rec)
        bumped = True
    elif v == "revise-scope":
        if rec.get("upstream_pr") is None:
            rec["handoff_approval"] = None
        rec["target_contract_id"] = a["proposal"]
        rec["activity"] = "idle"
        _bump(rec)
        bumped = True
    elif v == "force-develop":
        if phase != "parked":
            return _reject(record, f"force-develop needs phase parked, not {phase}")
        paths = [g for g in a["paths"].split(",") if g]
        try:
            validate_scope_paths(paths)
        except ValueError as exc:
            return _reject(record, str(exc))
        rec["phase"] = "provisioning"
        rec["activity"] = "idle"
        rec["parked_reason"] = None
        rec["admission_class"] = a["class"]
        rec["scope_paths"] = paths
        rec["pending_human_action"] = None
        _bump(rec)
        bumped = True
    elif v == "approve-scope":
        if phase != "investigation":
            return _reject(record, f"approve-scope needs phase investigation, not {phase}")
        if rec.get("active_work"):
            return _reject(record, "the investigation is still running")
        if not rec.get("dossier_evidence_id"):
            return _reject(record, "no investigation dossier recorded yet")
        if backup_user is None:
            return _reject(record, f"backup owner {a['backup']!r} is not a known GitHub user")
        if backup_user["id"] == actor["id"]:
            return _reject(record, "the backup owner must be someone else")
        rec["owners"] = dict(rec["owners"], primary=dict(actor), backup=dict(backup_user))
        rec["admission_class"] = a["class"]
        rec["scope_paths"] = [g for g in (a.get("paths") or "").split(",") if g] or None
        rec["phase"] = "provisioning"
        rec["activity"] = "waiting_human"
        rec["pending_human_action"] = {"kind": "provision_fork", "requested_at": now,
                                       "reminded_at": None, "escalated_at": None}
        _bump(rec)
        bumped = True
    elif v == "provision":
        if phase != "provisioning":
            return _reject(record, f"provision needs phase provisioning, not {phase}")
        if not provisioning or provisioning.get("error"):
            return _reject(record, (provisioning or {}).get("error") or "fork not verified")
        if provisioning.get("workflow_verified") is not True:
            return _reject(record, "fork provisioning must complete through the trusted upstreaming workflow; a bare existing fork is not sufficient")
        rec["fork"] = {"repository_id": provisioning["fork_id"], "name": provisioning["fork"],
                       "head_ref": DEV_REF, "head_sha": provisioning["head_sha"], "pr_number": None}
        rec["upstream"] = dict(rec["upstream"], repository_id=provisioning["upstream_id"],
                               base_ref=provisioning["base_ref"], base_sha=provisioning["base_sha"])
        rec["phase"] = "development"
        rec["activity"] = "idle"
        rec["pending_human_action"] = None
        rec["last_ci_evidence_id"] = None
        _bump(rec)
        bumped = True
    elif v == "authorize-followup":
        if phase not in ("release_wait", "released"):
            return _reject(record, "follow-ups need a merged contribution")
        # The completed record is left intact; intake.new_followup creates
        # the new identity. Here we only record the event.
    else:  # pragma: no cover - grammar and this table must agree
        raise TransitionError(f"unhandled verb {v}")

    # Stopping a contribution or moving it to a new revision ends any
    # requested task: a late worker outcome is then ignored as stale, and
    # dispatch recovery must not launch it again.
    if rec.get("active_work") and (rec["paused"] or rec["phase"] in TERMINAL | {"parked"}
                                   or rec["revision"] != record["revision"] or rec.get("blocked_reason")):
        rec["active_work"] = None
        if rec["activity"] == "agent_running":
            rec["activity"] = "idle"

    _check_legacy_write(record, rec, "apply_command")
    if rec != record or v == "authorize-followup":
        schemas.validate("contribution", rec)
    kind = f"command:{v}"
    return Outcome(rec, True, None, _event(record, rec, kind, "command", command_id, now, bumped))


def command_priority(cmd: Command) -> int:
    """Urgent stop commands sort first, then other controls."""
    return 0 if cmd.verb in URGENT else 1


