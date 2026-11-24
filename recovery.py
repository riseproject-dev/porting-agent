# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Remote operations: the operation table and its engine.

There is no transaction spanning control Git, the native queue, fork pushes,
PR mutations, run launches or notifications. Each remote effect is an
operation: an intent persisted before the effect, then a receipt confirmed by
discovering the actual resource. A lost receipt is recovered by discovery,
never by repeating the effect. Elapsed time alone never confirms an effect,
declares nonlaunch, or frees capacity.

The states and transitions of an operation are the table
``lifecycle.OPERATIONS``; this module is its engine, the one write path of an
operation's ``status``, as ``engine.try_apply_transition`` is the one write path
of a contribution's state:

* ``plan`` takes the initial verb ``record_intent`` (``planned``);
* ``apply_operation`` takes one row of the table: it evaluates every guard of
  the row through the predicate registered for it in ``PREDICATES`` (a
  *fact* the caller establishes, or a *record* predicate decided from the
  operation and the command), applies the row's effects (``external_ids``,
  ``discovery``, ``resolution``) and moves the operation to the row's target;
  the evidence of a ``resolve_*`` row is the ``resolution`` record the
  controller writes from the authorized ``/upstreaming resolve-operation``;
* ``mark_applying``, ``mark_result``, ``record_discovery``, ``fold_receipt``
  and ``resolve`` name the rows a caller's observation takes;
* ``fire`` takes, through ``engine.try_apply_one_of``, the lifecycle row with
  the ``operation`` trigger that an operation of a kind takes when it closes
  (``FIRES``), and releases the contribution's ``active_operation_id``.

The launches of a planned operation are counted on the operation
(``external_ids.launch_attempts``): the counter of the budget
``operation_launch``. No status changes without a row of the table: the
store admits the table's transitions only.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

import engine
import lifecycle as lc
import schemas
from commands import Command

STATUSES = tuple(lc.OPERATIONS)
OPEN = frozenset(key for key, state in lc.OPERATIONS.items() if state.open)
ALERT_AFTER = timedelta(hours=1)
OWNER_REVIEW_AFTER = timedelta(hours=24)

# The counter of the budget ``operation_launch``: the launches of one
# planned operation.
LAUNCH_COUNTER = "launch_attempts"


class RecoveryError(ValueError):
    pass


def operation_id(contribution_id: str, revision: int, kind: str, *reason_ids: str) -> str:
    """Deterministic: resubmitting the same intent yields the same ID, so a
    retried controller tick finds the existing operation instead of creating
    a second effect."""
    h = hashlib.sha256("\0".join([contribution_id, str(revision), kind, *reason_ids]).encode())
    return f"op-{h.hexdigest()[:24]}"


def is_open(op: dict | None) -> bool:
    return bool(op) and op["status"] in OPEN


def open_operations(contribution_id: str, operations: dict,
                    active: str | None = None) -> tuple[str, ...]:
    """The open operations of a contribution: those of the store, and the
    one the record names as active when the store does not hold it."""
    ops = {op["operation_id"] for op in operations.values()
           if op["contribution_id"] == contribution_id and op["status"] in OPEN}
    if active and (active not in operations or operations[active]["status"] in OPEN):
        ops.add(active)
    return tuple(sorted(ops))


# --------------------------------------------------------------------------
# Guards


@dataclass
class Context:
    """Everything a guard of the operation table may read."""

    operation: dict[str, Any]
    command: Command | None = None
    evidence: dict[str, Any] | None = None
    facts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Predicate:
    name: str
    kind: str
    check: Callable[[Context], str | None]


PREDICATES: dict[str, Predicate] = {}


def _register(guard: str, name: str, kind: str, check: Callable[[Context], str | None]) -> None:
    if guard in PREDICATES or any(p.name == name for p in PREDICATES.values()):
        raise ValueError(f"guard {guard!r} or predicate {name!r} is registered twice")
    PREDICATES[guard] = Predicate(name, kind, check)


def _fact(guard: str, name: str) -> None:
    _register(guard, name, "fact",
              lambda ctx: None if name in ctx.facts else f"not established: {guard}")


# Facts: what only the caller can observe. ``contribution_not_paused`` is the
# flag as the final check before the effect read it.
for _guard, _name in (
    ("capacity available", "capacity_available"),
    ("the contribution is not paused", "contribution_not_paused"),
    ("the final check before the effect passes", "final_check_passed"),
    ("the remote call succeeded, or its response was lost or ambiguous", "response_not_failed"),
    ("the remote call returned a definite failure", "response_failed"),
    ("discovery finds the exact resource the operation proposed", "resource_found"),
    ("discovery finds the resource absent or an unrelated one", "resource_not_found"),
):
    _fact(_guard, _name)

FACT_NAMES = frozenset(p.name for p in PREDICATES.values() if p.kind == "fact")
CONTRADICTORY_FACTS = (("response_not_failed", "response_failed"),
                       ("resource_found", "resource_not_found"))


def _command_arg(ctx: Context, name: str) -> str | None:
    return ctx.command.args.get(name) if ctx.command is not None else None


def _exact_operation(ctx: Context) -> str | None:
    if ctx.command is None or ctx.command.verb != "resolve-operation":
        return "no resolve-operation command"
    if _command_arg(ctx, "operation") != ctx.operation["operation_id"]:
        return "command does not target this operation"
    return None


def _resolution_evidence(ctx: Context) -> str | None:
    """The resolution evidence record the controller wrote from the command:
    of kind ``resolution``, naming this operation and the disposition the
    command states, with the actor who stated it."""
    evidence = ctx.evidence
    if not isinstance(evidence, dict):
        return "resolution evidence record not found"
    if evidence.get("kind") != "resolution":
        return "evidence is not a resolution record"
    oid = ctx.operation["operation_id"]
    resolution = evidence.get("resolution") or {}
    if not str(evidence.get("evidence_id") or "").startswith(f"resolution-{oid}-") \
            or resolution.get("operation_id") != oid:
        return "resolution evidence names another operation"
    if resolution.get("disposition") != _command_arg(ctx, "disposition"):
        return "resolution evidence states another disposition"
    if not (resolution.get("actor") or {}).get("id"):
        return "resolution evidence names no actor"
    return None


def _disposition(value: str) -> Callable[[Context], str | None]:
    def check(ctx: Context) -> str | None:
        given = _command_arg(ctx, "disposition")
        return None if given == value else f"disposition is {given}, not {value}"
    return check


_register(lc.EXACT_OPERATION, "exact_operation", "record", _exact_operation)
_register("a resolution evidence record states the disposition", "resolution_evidence", "record",
          _resolution_evidence)
for _value in ("confirmed", "failed", "not-launched"):
    _register(f"disposition {_value}", f"disposition_{_value.replace('-', '_')}", "record",
              _disposition(_value))


def table_guards() -> set[str]:
    return {g for e in lc.table_edges(lc.OPERATIONS) for g in e.transition.guard}


def table_verbs() -> set[str]:
    return {e.verb for e in lc.table_edges(lc.OPERATIONS)} | {lc.OPERATION_INITIAL_VERB}


# --------------------------------------------------------------------------
# The engine


@dataclass
class Outcome:
    """The result of taking, or refusing, one row of the operation table."""

    operation: dict[str, Any]
    accepted: bool
    reason: str | None
    verb: str | None = None


def _edge(status: str, verb: str) -> lc.Edge:
    if status not in lc.OPERATIONS:
        raise RecoveryError(f"{status} is not a state of the operation table")
    try:
        return lc.find_edge(status, verb, lc.OPERATIONS)
    except KeyError:
        raise RecoveryError(f"{verb} does not leave an operation in status {status}") from None


def refusal(op: dict, verb: str, *, facts=(), command: Command | None = None,
            evidence: dict | None = None) -> str | None:
    """Why ``verb`` cannot be taken now, or ``None`` when every guard holds."""
    edge = _edge(op["status"], verb)
    unknown = set(facts) - FACT_NAMES
    if unknown:
        raise RecoveryError(f"unknown facts: {', '.join(sorted(unknown))}")
    for pair in CONTRADICTORY_FACTS:
        if set(pair) <= set(facts):
            raise RecoveryError(f"contradictory facts: {' and '.join(pair)}")
    ctx = Context(op, command, evidence, frozenset(facts))
    for guard in edge.transition.guard:
        predicate = PREDICATES.get(guard)
        if predicate is None:
            raise RecoveryError(f"guard {guard!r} has no predicate")
        why = predicate.check(ctx)
        if why:
            return why
    return None


# The discovery results each discovery row records.
_DISCOVERY_RESULTS = {"confirm_by_discovery": ("confirmed",),
                      "record_discovery": ("absent", "unrelated")}


def apply_operation(op: dict, verb: str, *, now: str, facts=(), values: dict | None = None,
                    command: Command | None = None, evidence: dict | None = None,
                    actor: dict | None = None, command_id: str | None = None) -> Outcome:
    """Take the row ``verb`` of the operation table from the operation's
    status. ``values`` gives what the row sets: ``external_ids`` (merged into
    the recorded ones) and ``discovery`` (``{"result", "detail"}``, appended).
    ``resolution`` is derived from the command, its actor and the evidence.
    A guard that does not hold refuses the row and leaves the operation
    unchanged; a call the table does not allow raises ``RecoveryError``."""
    edge = _edge(op["status"], verb)
    why = refusal(op, verb, facts=facts, command=command, evidence=evidence)
    if why:
        return Outcome(op, False, why, verb)
    values = dict(values or {})
    sets = set(edge.transition.effects.sets)
    extra = set(values) - (sets - {"resolution"})
    if extra:
        raise RecoveryError(f"{verb} does not set {', '.join(sorted(extra))}")
    new = copy.deepcopy(op)
    if "external_ids" in sets:
        new["external_ids"] = dict(op.get("external_ids") or {}, **(values.get("external_ids") or {}))
    if "discovery" in sets:
        found = values.get("discovery")
        if not isinstance(found, dict) or found.get("result") not in _DISCOVERY_RESULTS[verb]:
            raise RecoveryError(f"{verb} records a discovery result in {_DISCOVERY_RESULTS[verb]}")
        new["discovery"] = list(op.get("discovery") or []) + [
            {"at": now, "result": found["result"], "detail": str(found.get("detail") or "")[:2000]}]
    if "resolution" in sets:
        if actor is None or command_id is None:
            raise RecoveryError(f"{verb} records the actor and the command")
        new["resolution"] = {"actor": {"id": actor["id"], "login": actor["login"]},
                             "disposition": command.args["disposition"],
                             "evidence_id": evidence["evidence_id"], "command_id": command_id}
    new["status"] = edge.target
    new["updated_at"] = now
    schemas.validate("operation", new)
    return Outcome(new, True, None, verb)


def _take(op: dict, verb: str, **kwargs: Any) -> dict:
    out = apply_operation(op, verb, **kwargs)
    if not out.accepted:
        raise RecoveryError(f"{verb} refused: {out.reason}")
    return out.operation


def plan(*, contribution_id: str, revision: int, kind: str, policy_version: str,
         reason_ids: list[str], target: dict, proposed: dict | None = None,
         capacity: dict | None = None, now: str, external_ids: dict | None = None) -> dict:
    """``record_intent``: the intent of a remote effect, before the effect."""
    op = {
        "schema_version": 1,
        "operation_id": operation_id(contribution_id, revision, kind, *reason_ids),
        "contribution_id": contribution_id,
        "revision": revision,
        "kind": kind,
        "policy_version": policy_version,
        "reason_ids": reason_ids,
        "target": target,
        "proposed": proposed or {},
        "capacity_reservation": capacity,
        "status": lc.OPERATION_INITIAL_STATE,
        "external_ids": dict(external_ids or {}),
        "discovery": [],
        "resolution": None,
        "created_at": now,
        "updated_at": now,
    }
    schemas.validate("operation", op)
    return op


def mark_applying(op: dict, now: str, *, facts=()) -> dict:
    """``begin_effect``: the caller's final check passed, with the facts it
    established (``capacity_available``, ``contribution_not_paused``,
    ``final_check_passed``)."""
    return _take(op, "begin_effect", now=now, facts=facts)


def mark_result(op: dict, *, response_ok: bool | None, external_ids: dict | None, now: str) -> dict:
    """After the remote call. ``response_ok=None`` means the response was lost
    or ambiguous: the effect may or may not exist, so the operation becomes
    ``uncertain`` (``record_response``) and must be discovered, not repeated.
    Even an OK response is only provisional: discovery confirms the resource.
    A definite failure takes ``record_failure``."""
    if response_ok is False:
        return _take(op, "record_failure", now=now, facts={"response_failed"},
                     values={"external_ids": external_ids or {}})
    return _take(op, "record_response", now=now, facts={"response_not_failed"},
                 values={"external_ids": external_ids or {}})


def record_discovery(op: dict, result: str, detail: str, now: str) -> dict:
    """Fold one discovery attempt into an ``uncertain`` operation.

    * confirmed -> ``confirm_by_discovery``: confirmed
    * absent, unrelated -> ``record_discovery``: stays uncertain (absence never
      proves nonlaunch; an unrelated resource holds affected work)
    """
    if op["status"] not in OPEN:
        raise RecoveryError("discovery on a closed operation")
    if result == "confirmed":
        return _take(op, "confirm_by_discovery", now=now, facts={"resource_found"},
                     values={"discovery": {"result": result, "detail": detail}})
    if result in ("absent", "unrelated"):
        return _take(op, "record_discovery", now=now, facts={"resource_not_found"},
                     values={"discovery": {"result": result, "detail": detail}})
    raise RecoveryError(f"no row records the discovery result {result!r}")


# The facts a verified receipt of a trusted workflow establishes: the
# workflow's final check (the operation planned and current, the
# contribution not paused, capacity admitted when the controller launched
# it) passed, and the remote call it made returned.
RECEIPT_FACTS = frozenset({"capacity_available", "contribution_not_paused", "final_check_passed"})


def fold_receipt(op: dict, *, external_ids: dict | None, detail: str, now: str) -> dict:
    """A verified receipt of a trusted workflow, with the exact readback of
    the resource it reports: ``begin_effect`` (when the operation is still
    ``planned``), ``record_response``, then ``confirm_by_discovery``."""
    if op["status"] == "planned":
        op = mark_applying(op, now, facts=RECEIPT_FACTS)
    if op["status"] == "applying":
        op = mark_result(op, response_ok=True, external_ids=external_ids, now=now)
    elif external_ids:
        raise RecoveryError(f"a receipt for an operation in status {op['status']}")
    return record_discovery(op, "confirmed", detail, now)


def fold_failure_receipt(op: dict, *, external_ids: dict | None, now: str) -> dict:
    """A verified receipt of a trusted workflow that reports a definite
    failure of the effect: ``begin_effect`` (when the operation is still
    ``planned``), then ``record_failure``."""
    if op["status"] == "planned":
        op = mark_applying(op, now, facts=RECEIPT_FACTS)
    if op["status"] != "applying":
        raise RecoveryError(f"a failure receipt for an operation in status {op['status']}")
    return mark_result(op, response_ok=False, external_ids=external_ids, now=now)


def resolution_verbs(op: dict) -> list[str]:
    return [e.verb for e in lc.state_edges(op["status"], lc.OPERATIONS)
            if e.transition.command == lc.RESOLVE]


def resolve(op: dict, cmd: Command, *, actor: dict, command_id: str, evidence: dict | None,
            now: str) -> dict:
    """Apply ``/upstreaming resolve-operation``: the one ``resolve_*`` row of
    the table whose guards hold (the command names this operation, the
    resolution evidence record states the disposition, the disposition).
    The evidence is the record the controller writes from the authorized
    command (``controller.resolution_evidence``). It never synthesizes a
    native queue Result."""
    if cmd.verb != "resolve-operation" or cmd.args.get("operation") != op["operation_id"]:
        raise RecoveryError("command does not target this operation")
    if op["status"] not in OPEN:
        raise RecoveryError(f"operation already {op['status']}")
    reasons = []
    for verb in resolution_verbs(op):
        out = apply_operation(op, verb, now=now, command=cmd, evidence=evidence, actor=actor,
                              command_id=command_id)
        if out.accepted:
            return out.operation
        reasons.append(out.reason)
    raise RecoveryError(next((r for r in reasons if not r.startswith("disposition ")),
                             reasons[0] if reasons else "no resolution applies"))


# --------------------------------------------------------------------------
# Launches: the counter of ``operation_launch``


def launch_attempts(op: dict) -> int:
    return int((op.get("external_ids") or {}).get(LAUNCH_COUNTER, 0))


def charge_launch(op: dict, now: str) -> dict:
    """Count one launch of a planned operation."""
    if op["status"] != "planned":
        raise RecoveryError(f"cannot launch an operation in status {op['status']}")
    new = copy.deepcopy(op)
    new["external_ids"] = dict(op.get("external_ids") or {}, **{LAUNCH_COUNTER: launch_attempts(op) + 1})
    new["updated_at"] = now
    schemas.validate("operation", new)
    return new


def restart_launches(op: dict, now: str) -> dict:
    """Start the launch count of a planned operation again (a resume that
    names ``operation_launch``)."""
    new = copy.deepcopy(op)
    new["external_ids"] = dict(op.get("external_ids") or {}, **{LAUNCH_COUNTER: 0})
    new["updated_at"] = now
    schemas.validate("operation", new)
    return new


def launch_count(op: dict | None) -> dict[str, int]:
    """The ``counts`` of ``operation_launch`` for the engine."""
    return {"operation_launch": launch_attempts(op)} if op else {}


# --------------------------------------------------------------------------
# Lifecycle rows with the ``operation`` trigger

# The lifecycle verbs an operation of a kind takes when it closes, by the
# state its contribution is in and the end state the operation reached. When
# several are listed, the one whose guards hold is taken (a retry within its
# budget, or the exhaustion of that budget).
FIRES: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {
    ("provisioning.idle", "fork_provision"): {
        "confirmed": ("confirm_provisioning",),
        "failed": ("require_provisioning_intervention",),
    },
    # The development PR operation: the PR of a published head, opened from
    # `development.fix_needed` or `development.agent_running` before CI is
    # awaited, and the CI bootstrap of a recorded PR in
    # `development.waiting_ci` (where a loaded record that waits without a PR
    # also opens it). It fires no row when it fails: the controller
    # takes the exhaustion row itself once its launches are spent, and closes
    # a stale or paused one without a row. A verified receipt in another
    # state closes it and keeps the state.
    ("development.fix_needed", "dev_pr_create"): {"confirmed": ("confirm_development_pr",)},
    ("development.agent_running", "dev_pr_create"): {"confirmed": ("confirm_development_pr",)},
    ("development.waiting_ci", "dev_pr_create"): {"confirmed": ("confirm_development_pr",)},
    ("development.waiting_ci", "dev_pr_update"): {"confirmed": ("confirm_development_pr",)},
    # The handoff operations. A failed attempt takes its retry row within the
    # budget of its state and the exhaustion row beyond it; the caller
    # supplies that budget's count (the attempt's count, ``handoff.counts``).
    # A failed build of `riscv64` whose reviewed series does not rebuild, or
    # whose lease conflicts, returns to development instead: its caller
    # establishes ``upstream_branch_invalid`` and supplies no count, so neither
    # budget row applies. One that finds an upstream PR open from `riscv64`
    # waits for a human: its caller establishes ``upstream_branch_blocked``.
    # A package attempt that finds `riscv64` off the clean head, read back the
    # same by the reconciler, returns to development: its caller establishes
    # ``clean_head_moved`` and supplies no count, so neither budget row applies.
    ("handoff.building_upstream_branch", "upstream_branch"): {
        "confirmed": ("confirm_clean_head",),
        "failed": ("retry_upstream_branch", "exhaust_upstream_branch_retries",
                   "return_for_repair", "require_branch_intervention"),
    },
    ("handoff.preparing_submission", "submission_comment"): {
        "confirmed": ("confirm_submission_package",),
        "failed": ("retry_submission_comment", "exhaust_comment_retries", "return_for_repair"),
    },
}


def fired_verbs(record: dict, op: dict, status: str | None = None) -> tuple[str, ...]:
    """The verbs ``op`` fires in the record's state on reaching ``status``
    (its own status by default)."""
    return FIRES.get((engine.state_key(record), op["kind"]), {}).get(status or op["status"], ())


def release(record: dict, op: dict, now: str) -> dict:
    """The record without its reference to ``op`` once ``op`` is closed:
    ``active_operation_id`` names an open operation only."""
    if is_open(op) or record.get("active_operation_id") != op["operation_id"]:
        return record
    return engine.record_beside(record, "operation_released", now=now, active_operation_id=None)


def fire(record: dict, op: dict, *, policy: dict, now: str, facts=(), values=None,
         cause_id: str | None = None, details: dict | None = None,
         open_operations: tuple[str, ...] | None = None,
         counts: dict[str, int] | None = None) -> engine.Applied | engine.Refusals | None:
    """Take the one lifecycle row an operation fires on reaching
    ``confirmed`` or ``failed`` (``engine.try_apply_one_of`` over the rows
    bound to it in the record's state), and release the record's reference
    to the closed operation. ``None`` when no row is bound to the operation
    in the record's state; the refusals of every bound row when none is
    taken, with the record as it is: each caller handles them as
    ``engine.HANDLING`` declares (the group of verbs is the key).
    ``counts`` are the counts of budgets that live outside the record (the
    operation's launches by default)."""
    verbs = fired_verbs(record, op)
    if not verbs:
        return None
    counts = launch_count(op) if counts is None else counts
    out = engine.try_apply_one_of(
        record, verbs, policy=policy, now=now, cause_id=cause_id or op["operation_id"],
        facts=facts, values=values, counts=counts, open_operations=open_operations,
        details={"operation": op["operation_id"], **(details or {})})
    if isinstance(out, engine.Applied):
        out.record = release(out.record, op, now)
    return out


def fired_group(record: dict, op: dict, status: str | None = None) -> str:
    """The ``engine.HANDLING`` key of the rows ``op`` fires in the record's
    state: their verbs joined by ``|``."""
    return "|".join(fired_verbs(record, op, status))


def reachable(status: str) -> frozenset[str]:
    """The statuses rows of the table can lead to from ``status``."""
    seen, todo = {status}, [status]
    while todo:
        for e in lc.state_edges(todo.pop(), lc.OPERATIONS):
            if e.target not in seen:
                seen.add(e.target)
                todo.append(e.target)
    return frozenset(seen)


def transition_problem(before: dict | None, after: dict) -> str | None:
    """Why the store must not write ``after`` over ``before``: a status no
    sequence of rows leads to (a closed operation never changes status)."""
    old = before["status"] if before else lc.OPERATION_INITIAL_STATE
    new = after["status"]
    if new in reachable(old):
        return None
    return f"operation status {old} cannot become {new}"


# --------------------------------------------------------------------------
# Discovery predicates: given the operation and what the API shows now,
# return "confirmed", "absent", or "unrelated".


def discover_branch(op: dict, observed: dict | None) -> str:
    """observed = {"head": sha, "tree": sha, "parents": [...]} for the ref."""
    if observed is None:
        return "absent"
    prop = op.get("proposed") or {}
    if prop.get("commit") and observed.get("head") == prop["commit"]:
        return "confirmed"
    if prop.get("tree") and observed.get("tree") == prop["tree"] and \
            observed.get("parents") == prop.get("parents"):
        # Signed replay can change the commit SHA; tree + parents identify it.
        return "confirmed"
    if observed.get("head") == op["target"].get("expected_head"):
        return "absent"
    return "unrelated"


def discover_pr(op: dict, prs: list[dict]) -> str:
    """prs = open PRs in the fork, each with head_repo/head_ref/base_ref/body."""
    marker = (op.get("proposed") or {}).get("marker")
    for pr in prs:
        if pr.get("head_ref") == op["target"].get("ref") and \
                (marker is None or marker in (pr.get("body") or "")):
            return "confirmed"
    return "absent"


def discover_dispatch(op: dict, runs: list[dict], window: tuple[str, str]) -> str:
    """A dispatch is confirmed only by an associated run inside the launch
    window. Absence is *not* definitive nonlaunch."""
    lo, hi = window
    for r in runs:
        if r.get("head_sha") == op["target"].get("expected_head") and lo <= r["created_at"] <= hi \
                and r.get("event") in ("workflow_dispatch", "push", "pull_request"):
            return "confirmed"
    return "absent"


def discover_notification(op: dict, comments: list[dict]) -> str:
    marker = (op.get("proposed") or {}).get("marker")
    for c in comments:
        if marker and marker in (c.get("body") or ""):
            return "confirmed"
    return "absent"


def discover_rerun(op: dict, runs: list[dict]) -> str:
    """A CI rerun is confirmed only by a higher attempt of every run it
    names on the expected head (``ci_rerun.discover``): a rerun keeps the
    run id and its creation time, so a launch window cannot confirm it."""
    import ci_rerun

    return ci_rerun.discover(op, runs)


DISCOVERERS: dict[str, Callable[..., str]] = {
    "branch_update": discover_branch,
    "branch_rewrite": discover_branch,
    "dev_pr_create": discover_pr,
    "dev_pr_update": discover_pr,
    "ci_dispatch": discover_dispatch,
    "ci_rerun": discover_rerun,
    "notification": discover_notification,
}


def next_discovery_at(op: dict, policy: dict) -> datetime | None:
    if op["status"] not in OPEN:
        return None
    start = datetime.fromisoformat(op["created_at"])
    sched = policy["recovery_minutes"]
    n = len(op["discovery"])
    if n < len(sched):
        return start + timedelta(minutes=sched[n])
    # Reduced frequency thereafter; never declares success.
    return datetime.fromisoformat(op["discovery"][-1]["at"]) + timedelta(hours=6)


def alerts(op: dict, now: datetime) -> list[str]:
    if op["status"] not in OPEN or op["status"] == "planned":
        return []
    age = now - datetime.fromisoformat(op["created_at"])
    out = []
    if age >= ALERT_AFTER:
        out.append("uncertain_over_1h")
    if age >= OWNER_REVIEW_AFTER:
        out.append("owner_review_required")
    return out


def releases_capacity(op: dict) -> bool:
    """Capacity reservations are freed only on verified termination or
    definitive nonlaunch, never on elapsed time."""
    return op["status"] in ("not_launched", "failed") or (
        op["status"] == "confirmed" and op["kind"] not in ("ci_dispatch", "ci_rerun",
                                                           "branch_update", "branch_rewrite"))
