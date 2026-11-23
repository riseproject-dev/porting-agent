# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Compatibility shim: lifecycle writes that do not yet go through the table.

``engine.apply_transition`` is the write path of the contribution lifecycle.
The code paths listed in ``LEGACY_SITES`` predate the lifecycle table and
still decide a state change themselves; until each is converted to a table
verb, it writes the fenced fields (``phase``, ``activity``, ``paused``,
``revision``, ``generation``) only through ``legacy_update`` or the legacy
transition functions of this module. No other module assigns those fields.

A legacy write must leave a record in a state of the table
(``LEGACY_STATES`` is empty). It can never enter the ``handoff`` phase or
change a record that is in it: every handoff state is written by the engine.

Every command of the table is taken by the engine; ``apply_command``
applies the intake command ``authorize-followup`` only.

``LEGACY_SITES`` and ``LEGACY_STATES`` shrink as call sites are converted;
when both are empty this module is deleted.
"""

from __future__ import annotations

from typing import Any

import engine
import schemas
from commands import URGENT, Command
from lifecycle import (
    LIFECYCLE,
    REMINDER_KINDS,
    TERMINAL,
    Outcome,
    TransitionError,
    _event,
)

# States that are not in the lifecycle table and that legacy paths still
# write. Every state the pipeline writes is a state of the table; the set
# stays until this module is deleted.
LEGACY_STATES: frozenset[str] = frozenset()

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
})


def state_problem(record: dict) -> str | None:
    """Why a record's state is not admitted, legacy states included."""
    return engine.state_problem(record, LEGACY_STATES)


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


# --------------------------------------------------------------------------
# Commands


def _reject(record: dict, reason: str) -> Outcome:
    return Outcome(record, False, reason, None)


# The command ``apply_command`` applies itself: the intake command
# ``authorize-followup``. Every command of the table is taken by the engine.
LEGACY_COMMANDS = frozenset({"authorize-followup"})


def apply_command(record: dict, cmd: Command, *, command_id: str, actor: dict,
                  now: str) -> Outcome:
    """Apply an already authenticated, not-yet-consumed command that the
    engine does not take (``engine.takes_command``): ``authorize-followup``
    on a merged contribution. Any other command is rejected with the states
    it applies in.

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
    rev = cmd.revision if cmd.revision is not None else record["revision"]
    if rev > record["revision"]:
        return _reject(record, "revision is not a known revision")
    if rev != record["revision"]:
        return _reject(record, f"stale revision {rev}, current is {record['revision']}")
    if record["phase"] not in ("release_wait", "released"):
        return _reject(record, "follow-ups need a merged contribution")
    # The completed record is left intact; intake.new_followup creates the
    # new identity. Here only the event is recorded.
    rec = dict(record, updated_at=now)
    schemas.validate("contribution", rec)
    return Outcome(rec, True, None, _event(record, rec, "command:authorize-followup", "command",
                                           command_id, now, False))


def command_priority(cmd: Command) -> int:
    """Urgent stop commands sort first, then other controls."""
    return 0 if cmd.verb in URGENT else 1


