# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The transition engine: the one write path of the contribution lifecycle.

``try_apply_transition(record, verb, ...)`` takes a contribution record and a
verb of the lifecycle table (``lifecycle.LIFECYCLE``, ``RULES`` and ``FLAGS``)
and returns either ``Applied`` (the new record with the semantic event to
persist) or ``Refused``, one of a closed set of refusals (``Refusal``), with
the record untouched and nothing emitted. It is the only public way to take a
row: it evaluates the row's preconditions with the pure ``preconditions``
and, only when they hold, applies the row (the private ``_apply_transition``).
A row whose preconditions always hold (the guard ``always``) always applies.
Every caller handles every kind of refusal explicitly: ``HANDLING`` declares
what each call site does with each kind, ``dispatch`` returns it, and a test
checks the table and the call sites. The transition does exactly what the
table row states and nothing else:

* it resolves the verb from the record's state (``phase.activity``), from a
  flag setter or from a flag clearer;
* it evaluates every guard of the row through the named predicate registered
  for it in ``PREDICATES``, and applies the mask of a set flag;
* it applies the row's ``Effects`` (fields set and cleared, the requested
  task, the revision and generation), charges the row's budgets and starts
  again the one counter a resume names;
* it emits the event, whose kind is the verb.

A guard is either a *record* predicate, decided from the record, the policy,
the time and the command, or a *fact*: something only the caller can observe
(a CI verdict, a receipt, a free worker slot). The caller asserts a fact by
name; a fact that is not asserted does not hold, so a transition is refused
unless every guard is established. The engine never performs remote effects.

``PIPELINE_VERBS`` lists the verbs that pipeline code takes through this
engine, with the module that takes each. Every other verb of the table is in
``UNWIRED_VERBS``: the engine applies it, but no pipeline code takes it,
because an open decision blocks it (``OPEN_DECISIONS`` names the entry of the
known-gaps page for each). The two sets partition the table, so the share of
the table the pipeline obeys is measurable. No other module assigns a field
that an effect of the table names (``FENCED_FIELDS``); what pipeline code
records beside a row goes through ``record_beside``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable

import ci_evidence
import intake
import lifecycle as lc
import schemas
from commands import Command
from common import UPSTREAM_REF

# The fields only this module assigns: the state, the flag, the revision and
# the generation, the requested task, and every field an effect of the table
# names (by its top-level name). Pipeline code changes them through
# ``try_apply_transition``, or beside a row through ``record_beside``.
STATE_FIELDS = ("phase", "activity", "paused", "revision", "generation")


def _effect_fields() -> tuple[str, ...]:
    names: list[str] = []
    transitions = [e.transition for e in lc.table_edges()]
    for flag in lc.FLAGS.values():
        transitions += [t for _verb, t in flag.transitions()]
    for t in transitions:
        if t.effects is not None:
            for entry in (*t.effects.sets, *t.effects.clears):
                names.append(lc._field_name(entry).split(".")[0])
    record_fields = schemas._validator("contribution").schema["properties"]
    return tuple(dict.fromkeys(n for n in names if n in record_fields))


FENCED_FIELDS = tuple(dict.fromkeys((*STATE_FIELDS, "active_work", *_effect_fields())))

TransitionError = lc.TransitionError


class Refusal(StrEnum):
    """Why a row is not taken: a closed set. Every precondition of a row
    refuses with exactly one of these, whatever its wording."""

    NOT_APPLICABLE = "not_applicable"      # no row for this verb in this state
    PAUSED = "paused"                      # the mask of the paused flag, or "not paused"
    COMMAND_MISMATCH = "command_mismatch"  # wrong revision, head or operation, no reason
    FENCED = "fenced"                      # an open operation, a pending publication, a task
    BUDGET_EXHAUSTED = "budget_exhausted"  # a "remains" guard is false
    FACT_MISSING = "fact_missing"          # a fact the row needs was not asserted
    GUARD_UNMET = "guard_unmet"            # another record predicate of the row is false
    SOURCE_INVALID = "source_invalid"      # the record violates the requirements of its state
    TARGET_INVALID = "target_invalid"      # the record after the row fails its schema or state


@dataclass(frozen=True)
class Ok:
    """The preconditions of ``verb`` hold: it leads to ``target``."""

    verb: str
    target: str


@dataclass(frozen=True)
class Refused:
    """``verb`` is not taken: the kind, the guard (or requirement) that does
    not hold, and why. The record is untouched and nothing is emitted."""

    kind: Refusal
    verb: str | None
    guard: str | None
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True)
class Refusals:
    """No candidate of ``try_apply_one_of`` is taken: one refusal each."""

    refused: tuple[Refused, ...]

    @property
    def detail(self) -> str:
        return "; ".join(dict.fromkeys(r.detail for r in self.refused)) or "no row applies"

    def kinds(self) -> frozenset[Refusal]:
        return frozenset(r.kind for r in self.refused)

    def __str__(self) -> str:
        return self.detail


@dataclass
class Applied:
    """A row taken: the new record and the event to persist."""

    record: dict[str, Any]
    event: dict[str, Any]
    verb: str
    # Budgets charged whose counter lives outside the record (an operation's
    # attempts, dispatcher runs); the caller persists them.
    charged: tuple[str, ...] = ()
    # The counter a resume started again: {"counter", "count"}.
    restarted: dict[str, Any] | None = None


class CommandRefused(lc.TransitionError):
    """A command whose value the table's row cannot use (an invalid target
    branch): the reply is a rejection. Any other ``TransitionError`` raised
    while a command is applied is a programming error."""


class TransitionRefused(lc.TransitionError):
    """A refusal whose disposition at its call site is ``Bug``: the caller
    raises it, and the tick pauses the contribution for a processing error."""

    def __init__(self, refused: Refused | Refusals, site: tuple[str, str] | None = None):
        self.refused = refused
        where = f" at {site[0]}.{site[1]}" if site else ""
        kind = refused.kind if isinstance(refused, Refused) else \
            ", ".join(sorted(refused.kinds()))
        verb = refused.verb if isinstance(refused, Refused) else \
            ", ".join(r.verb or "?" for r in refused.refused)
        super().__init__(f"{verb} refused ({kind}){where}: {refused.detail}")


@dataclass
class Context:
    """Everything a guard may read."""

    record: dict[str, Any]
    state: str
    policy: dict[str, Any]
    now: str
    command: Command | None = None
    facts: frozenset[str] = frozenset()
    counts: dict[str, int] = field(default_factory=dict)
    open_operations: tuple[str, ...] | None = None
    # The evidence record ``last_ci_evidence_id`` names, supplied by the
    # caller from the store: the failing CI job of the head is decided from it.
    ci_evidence: dict[str, Any] | None = None

    @property
    def cluster(self) -> str | None:
        """The failing CI job recorded for the head, or ``None``."""
        return recorded_failing_job(self.record, self.ci_evidence)[1]


def state_key(record: dict) -> str:
    """The table key of the state a record is in."""
    return f"{record['phase']}.{record['activity']}"


# --------------------------------------------------------------------------
# Guards


@dataclass(frozen=True)
class Predicate:
    """A named guard. ``check`` returns ``None`` when the guard holds and the
    reason it does not otherwise. A ``fact`` predicate holds when the caller
    asserts its name. ``refusal`` is the kind of refusal it gives when it
    does not hold."""

    name: str
    kind: str
    check: Callable[[Context], str | None]
    refusal: Refusal = Refusal.GUARD_UNMET


PREDICATES: dict[str, Predicate] = {}

# The refusal of each record predicate that is not ``GUARD_UNMET``, by its
# name; every fact predicate refuses with ``FACT_MISSING``, and every budget
# that "remains" with ``BUDGET_EXHAUSTED``.
PREDICATE_REFUSALS: dict[str, Refusal] = {
    "exact_revision": Refusal.COMMAND_MISMATCH,
    "any_revision": Refusal.COMMAND_MISMATCH,
    "reason_given": Refusal.COMMAND_MISMATCH,
    "exact_candidate_head": Refusal.COMMAND_MISMATCH,
    "exact_clean_head": Refusal.COMMAND_MISMATCH,
    "not_paused": Refusal.PAUSED,
    "pause_allowed": Refusal.PAUSED,
    "no_open_operation": Refusal.FENCED,
    "quiescent": Refusal.FENCED,
    "no_requested_work": Refusal.FENCED,
    "no_work_requested": Refusal.FENCED,
    "repair_budgets_remain": Refusal.BUDGET_EXHAUSTED,
    "revision_budgets_remain": Refusal.BUDGET_EXHAUSTED,
    "review_revision_budgets_remain": Refusal.BUDGET_EXHAUSTED,
    "work_recovery_and_work_total_remain": Refusal.BUDGET_EXHAUSTED,
}


def _refusal_of(name: str, kind: str) -> Refusal:
    if kind == "fact":
        return Refusal.FACT_MISSING
    if name in PREDICATE_REFUSALS:
        return PREDICATE_REFUSALS[name]
    if name.endswith("_remains"):
        return Refusal.BUDGET_EXHAUSTED
    return Refusal.GUARD_UNMET


def _register(guard: str, name: str, kind: str, check: Callable[[Context], str | None]) -> None:
    if guard in PREDICATES:
        raise ValueError(f"guard {guard!r} is registered twice")
    if any(p.name == name for p in PREDICATES.values()):
        raise ValueError(f"predicate name {name!r} is registered twice")
    PREDICATES[guard] = Predicate(name, kind, check, _refusal_of(name, kind))


def _record(guard: str, name: str):
    def decorate(fn: Callable[[Context], str | None]):
        _register(guard, name, "record", fn)
        return fn
    return decorate


def _asserted(ctx: Context, name: str) -> bool:
    return name in ctx.facts


def _fact(guard: str, name: str) -> None:
    _register(guard, name, "fact",
              lambda ctx: None if _asserted(ctx, name) else f"not established: {guard}")


def is_paused(record: dict) -> bool:
    """Whether the stop flag is set."""
    return bool(record.get("paused"))


def open_operations(ctx: Context) -> tuple[str, ...]:
    if ctx.open_operations is not None:
        return ctx.open_operations
    active = ctx.record.get("active_operation_id")
    return (active,) if active else ()


def _command_revision(ctx: Context) -> int | None:
    return ctx.command.revision if ctx.command is not None else None


def _command_arg(ctx: Context, name: str) -> str | None:
    return ctx.command.args.get(name) if ctx.command is not None else None


@_record(lc.ALWAYS, "always")
def _always(ctx: Context) -> str | None:
    return None


@_record(lc.EXACT_REVISION, "exact_revision")
def _exact_revision(ctx: Context) -> str | None:
    revision, current = _command_revision(ctx), ctx.record["revision"]
    if revision is None:
        return "the command must name the current revision"
    if revision > current:
        return "revision is not a known revision"
    if revision != current:
        return f"stale revision {revision}, current is {current}"
    return None


@_record(lc.ANY_REVISION, "any_revision")
def _any_revision(ctx: Context) -> str | None:
    revision = _command_revision(ctx)
    if revision is not None and revision > ctx.record["revision"]:
        return "revision is not a known revision"
    return None


@_record("a reason is given", "reason_given")
def _reason_given(ctx: Context) -> str | None:
    return None if _command_arg(ctx, "reason") else "a reason is required"


@_record(lc.NOT_PAUSED, "not_paused")
def _not_paused(ctx: Context) -> str | None:
    return "contribution is paused" if is_paused(ctx.record) else None


@_record(lc.UPSTREAM_BOUND, "upstream_bound")
def _upstream_bound(ctx: Context) -> str | None:
    upstream = ctx.record.get("upstream") or {}
    missing = [k for k in ("repository_id", "base_ref", "base_sha") if upstream.get(k) is None]
    if not missing:
        return None
    return "upstream identity and base not bound: " + ", ".join(f"upstream.{k}" for k in missing)


@_record("paused", "paused")
def _paused(ctx: Context) -> str | None:
    return None if ctx.record.get("paused") else "not paused"


# The prefixes of `paused_reason` of the controller's stop after an error:
# a processing error, and a loaded record that breaks an invariant of its
# state with no row to heal it.
ERROR_PAUSE_PREFIXES = ("processing_error:", "invariant_violation:")


def paused_for_error(record: dict) -> bool:
    """Whether the controller paused the record after a processing error."""
    return bool(record.get("paused")) and record.get("paused_by") == "controller" \
        and str(record.get("paused_reason") or "").startswith(ERROR_PAUSE_PREFIXES)


@_record(lc.PAUSED_FOR_ERROR, "paused_for_error")
def _paused_for_error(ctx: Context) -> str | None:
    return None if paused_for_error(ctx.record) else \
        "not paused by the controller after a processing error"


@_record(lc.PAUSE_ALLOWED, "pause_allowed")
def _pause_allowed(ctx: Context) -> str | None:
    if not ctx.record.get("paused") or ctx.record.get("paused_by") == "controller":
        return None
    return "contribution is paused"


@_record("not terminal", "not_terminal")
def _not_terminal(ctx: Context) -> str | None:
    state = lc.LIFECYCLE.get(ctx.state)
    return f"contribution is terminal ({ctx.state})" if state and state.kind == "terminal" else None


@_record("paused, or waiting for a human", "stopped")
def _stopped(ctx: Context) -> str | None:
    if ctx.record.get("paused") or lc.LIFECYCLE[ctx.state].kind == "human_wait":
        return None
    return "nothing to resume"


def _no_open_operation(ctx: Context) -> str | None:
    ops = open_operations(ctx)
    return f"operation {ops[0]} is open" if ops else None


_record("no open operation", "no_open_operation")(_no_open_operation)


def publication_pending(record: dict) -> bool:
    """Whether a Result is held for publication and may still be published.
    A held publication whose ``publish_recovery`` is exhausted
    (``pending_publication.exhausted``) launches nothing more: it is kept for
    the human wait it caused and is not pending."""
    pending = record.get("pending_publication")
    return bool(pending) and not pending.get("exhausted")


@_record(lc.QUIESCENT, "quiescent")
def _quiescent(ctx: Context) -> str | None:
    return _no_open_operation(ctx) or (
        "a publication is pending" if publication_pending(ctx.record) else None)


def _no_work(ctx: Context) -> str | None:
    return "A task is requested" if ctx.record.get("active_work") else None


_record(lc.NO_WORK, "no_requested_work")(_no_work)
_record("no task requested", "no_work_requested")(_no_work)


@_record("development PR and head recorded", "development_pr_recorded")
def _development_pr_recorded(ctx: Context) -> str | None:
    return lc.dev_pr_problem(ctx.record)


def stopped_since(record: dict) -> datetime | None:
    """When the current stop began: the older of ``paused_at`` and
    ``pending_human_action.requested_at``."""
    stamps = [record.get("paused_at"), (record.get("pending_human_action") or {}).get("requested_at")]
    times = [datetime.fromisoformat(s) for s in stamps if s]
    return min(times) if times else None


def is_stale(ctx: Context) -> bool:
    since = stopped_since(ctx.record)
    if since is None:
        return False
    days = int(lc.policy_value(ctx.policy, lc.STALE_KEY))
    return datetime.fromisoformat(ctx.now) - since > timedelta(days=days)


@_record(lc.STALE, "stale")
def _stale(ctx: Context) -> str | None:
    return None if is_stale(ctx) else "the stop is not longer than refresh.stale_after_days"


@_record(lc.NOT_STALE, "not_stale")
def _not_stale(ctx: Context) -> str | None:
    return "the stop is longer than refresh.stale_after_days" if is_stale(ctx) else None


@_record(lc.FLAG_NOT_STALE, "flag_not_stale")
def _flag_not_stale(ctx: Context) -> str | None:
    if lc.state_phase(ctx.state) not in lc.STALE_PHASES:
        return None
    return _not_stale(ctx)


def _utc(t: datetime) -> datetime:
    """``t`` in UTC; a naive value is read as UTC."""
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def business_days_between(start: datetime, end: datetime) -> int:
    """Whole business days (Monday to Friday) elapsed from ``start`` to
    ``end``: a day counts when it ends on a weekday (UTC). Both are read in
    UTC, whatever offset they carry."""
    start, end = _utc(start), _utc(end)
    days, t = 0, start
    while t + timedelta(days=1) <= end:
        t += timedelta(days=1)
        if t.weekday() < 5:
            days += 1
    return days


@_record("at least `human_sla_business_days.reminder` business days since the request or the "
         "last reminder", "reminder_due")
def _reminder_due(ctx: Context) -> str | None:
    pending = ctx.record.get("pending_human_action")
    if not pending:
        return "no pending human action"
    since = datetime.fromisoformat(pending.get("reminded_at") or pending["requested_at"])
    limit = int(lc.policy_value(ctx.policy, lc.REMINDER_KEY))
    if business_days_between(since, datetime.fromisoformat(ctx.now)) >= limit:
        return None
    return "the reminder is not due"


UNKNOWN_EVIDENCE = "the CI evidence named by last_ci_evidence_id is not supplied"


def evidence_head(record: dict) -> str | None:
    """The head the recorded CI evidence must be for: the upstream PR head in
    upstream review, the fork head otherwise."""
    if record.get("upstream_pr"):
        return record["upstream_pr"].get("head_sha")
    return (record.get("fork") or {}).get("head_sha")


def recorded_failing_job(record: dict, evidence: dict | None) -> tuple[bool, str | None]:
    """Whether the failing CI job of the head is known, and its name.

    It is decided from the evidence at ``last_ci_evidence_id`` alone
    (``ci_evidence.failing_job``): none without recorded evidence; unknown
    when the caller does not supply that evidence, so a guard that reads it
    fails closed."""
    evidence_id = record.get("last_ci_evidence_id")
    if not evidence_id:
        return True, None
    if not evidence or evidence.get("evidence_id") != evidence_id:
        return False, None
    return True, ci_evidence.failing_job(evidence, evidence_head(record))


@_record(lc.FAILING_JOB, "failing_job_recorded")
def _failing_job(ctx: Context) -> str | None:
    known, job = recorded_failing_job(ctx.record, ctx.ci_evidence)
    if not known:
        return UNKNOWN_EVIDENCE
    return None if job else "no failing CI job is recorded for the head"


@_record(lc.NO_FAILING_JOB, "no_failing_job_recorded")
def _no_failing_job(ctx: Context) -> str | None:
    known, job = recorded_failing_job(ctx.record, ctx.ci_evidence)
    if not known:
        return UNKNOWN_EVIDENCE
    return "a failing CI job is recorded for the head" if job else None


def carried_feedback(record: dict) -> list[str]:
    """The pending feedback the requested task carries: the ids present when
    it was allocated. A Result held for publication carries those of its task."""
    for holder in (record.get("active_work"), record.get("pending_publication")):
        if holder and holder.get("feedback_evidence_ids"):
            return list(holder["feedback_evidence_ids"])
    return []


@_record(lc.NO_PENDING_FEEDBACK, "no_pending_actionable_feedback")
def _no_pending_feedback(ctx: Context) -> str | None:
    pending = ctx.record.get("pending_feedback_evidence_ids") or []
    return f"actionable feedback is pending: {', '.join(pending[:4])}" if pending else None


@_record(lc.FEEDBACK_OR_UNMET, "pending_feedback_or_unmet_series_requirements")
def _feedback_or_unmet(ctx: Context) -> str | None:
    if ctx.record.get("pending_feedback_evidence_ids") or _asserted(ctx, "series_requirements_unmet"):
        return None
    return "no actionable feedback is pending and every series requirement is met"


@_record(lc.WORK_WITHOUT_FEEDBACK, "work_without_feedback")
def _work_without_feedback(ctx: Context) -> str | None:
    return "the task carries feedback" if carried_feedback(ctx.record) else None


@_record(lc.REFRESH_UNUSED, "automatic_refresh_unused")
def _refresh_unused(ctx: Context) -> str | None:
    return "the automatic refresh is used" if ctx.record.get("automatic_refresh_used") else None


@_record(lc.REFRESH_USED, "automatic_refresh_used")
def _refresh_used(ctx: Context) -> str | None:
    return None if ctx.record.get("automatic_refresh_used") else "the automatic refresh is unused"


@_record("no plan", "no_plan")
def _no_plan(ctx: Context) -> str | None:
    return "a plan is recorded" if ctx.record.get("plan_evidence_id") else None


@_record("no fork", "no_fork")
def _no_fork(ctx: Context) -> str | None:
    return "a fork is recorded" if ctx.record.get("fork") else None


@_record("refresh recorded", "refresh_recorded")
def _refresh_recorded(ctx: Context) -> str | None:
    return None if ctx.record.get("refresh") else "no refresh is recorded"


@_record("no admission_override recorded", "no_admission_override")
def _no_admission_override(ctx: Context) -> str | None:
    return "an admission_override is recorded" if ctx.record.get("admission_override") else None


@_record("exact candidate head", "exact_candidate_head")
def _exact_candidate_head(ctx: Context) -> str | None:
    head = _command_arg(ctx, "head")
    candidate = (ctx.record.get("handoff_request") or {}).get("head")
    recorded = (ctx.record.get("fork") or {}).get("head_sha")
    if not candidate or candidate != recorded:
        return "no candidate head is recorded for the current fork head"
    return None if head == candidate else "head is not the candidate head"


@_record("exact clean head", "exact_clean_head")
def _exact_clean_head(ctx: Context) -> str | None:
    head = _command_arg(ctx, "head")
    clean = ctx.record.get("clean_head_sha")
    if not clean:
        return "no clean head of riscv64 is recorded"
    return None if head == clean else "head is not the clean head"


@_record("write admission", "write_admission")
def _write_admission(ctx: Context) -> str | None:
    return lc.write_admission(ctx.record)


@_record("the upstream PR head is the controlled fork ref", "upstream_pr_on_controlled_ref")
def _upstream_pr_on_controlled_ref(ctx: Context) -> str | None:
    pr, fork = ctx.record.get("upstream_pr") or {}, ctx.record.get("fork") or {}
    if pr and pr.get("head_ref") == UPSTREAM_REF and fork.get("repository_id") is not None \
            and pr.get("head_repository_id") == fork.get("repository_id"):
        return None
    return "the upstream PR head is not the controlled fork ref"


@_record("no open upstream PR", "no_open_upstream_pr")
def _no_open_upstream_pr(ctx: Context) -> str | None:
    if ctx.record.get("upstream_pr") is None or _asserted(ctx, "upstream_pr_not_open"):
        return None
    return "an upstream PR is recorded and not observed closed"


def _pending_guard(guard: str, negated: bool, kinds: tuple[str, ...]) -> None:
    def check(ctx: Context) -> str | None:
        kind = (ctx.record.get("pending_human_action") or {}).get("kind")
        if (kind in kinds) != negated:
            return None
        return f"pending_human_action is {kind}"
    name = "pending_" + ("not_" if negated else "") + "_or_".join(kinds)
    _register(guard, name, "record", check)


for _guard, (_negated, _kinds) in lc.PENDING_GUARDS.items():
    _pending_guard(_guard, _negated, _kinds)


# Budgets. A budget remains while its count is below its limit.

def budget_count(name: str, ctx: Context) -> int | None:
    """The count of a budget, or ``None`` when it lives outside the record
    and the caller did not supply it in ``counts``."""
    if name in ctx.counts:
        return ctx.counts[name]
    record = ctx.record
    budget = record.get("budget") or {}
    if name == "work_total":
        return int(budget.get("attempts_used", 0))
    if name == "no_progress":
        return int(budget.get("no_progress_streak", 0))
    if name == "planning_attempts":
        return int(budget.get("planning_attempts", 0))
    if name == "repair_attempts_cluster":
        clusters = budget.get("clusters") or {}
        if ctx.cluster is not None:
            return int(clusters.get(ctx.cluster, 0))
        return max((int(v) for v in clusters.values()), default=0)
    if name == "work_recovery":
        return int((record.get("active_work") or {}).get("attempt", 1))
    if name == "publish_recovery":
        return int((record.get("pending_publication") or {}).get("attempts", 0))
    if name == "source_reads":
        return int(budget.get("source_unavailable", 0))
    if name in RERUN_COUNTERS:
        return int(head_reruns(record)[RERUN_COUNTERS[name]])
    return None


# The rerun budgets and their count in ``budget.reruns``.
RERUN_COUNTERS = {"flaky_reruns": "flaky", "interrupted_reruns": "interrupted"}


def fresh_reruns(record: dict) -> dict:
    """Rerun counts that start again for the head the record names."""
    return {"head_sha": (record.get("fork") or {}).get("head_sha"), "flaky": 0, "interrupted": 0}


def head_reruns(record: dict) -> dict:
    """The rerun counts of the current head. Counts recorded for another
    head, or none at all, are counts of zero for this one."""
    reruns = (record.get("budget") or {}).get("reruns")
    fresh = fresh_reruns(record)
    if not reruns or reruns.get("head_sha") != fresh["head_sha"]:
        return fresh
    return dict(reruns)


# Budgets whose counter is a field of the contribution record.
RECORD_COUNTERS = ("work_total", "no_progress", "planning_attempts",
                   "repair_attempts_cluster", "work_recovery", "publish_recovery",
                   "source_reads", *RERUN_COUNTERS)


def remains(name: str, ctx: Context) -> bool | None:
    count = budget_count(name, ctx)
    if count is None:
        return None
    if name == "source_reads" and name not in ctx.counts:
        return not source_reads_spent(ctx.record, ctx.policy, ctx.now)
    return count < lc.budget_limit(name, ctx.policy)


def work_total_spent(record: dict, policy: dict) -> bool:
    """Whether ``work_total`` is exhausted for the record."""
    ctx = Context(record, state_key(record), policy, "")
    return remains("work_total", ctx) is False


# The fields beside `budget.source_unavailable` that a run of failed reads
# records, cleared with it.
SOURCE_READ_FIELDS = ("source_unavailable_since", "source_unavailable_at",
                      "source_unavailable_repository")


# Ticks do not come on time: a scheduled run is delayed or skipped, and most
# ticks come from issue, comment, and push events. The time since the first
# failed read of a run (`budget.source_unavailable_since`) is cut into slots
# of `retries.source_unavailable_minutes` divided by
# `retries.source_unavailable_reads`, and a failed read is counted when it
# falls in a later slot than the last counted one: a burst of ticks counts
# once, a tick a little early or late still counts, and a regular tick
# counts at most once per slot whatever its jitter.


def source_read_slot_length(policy: dict) -> timedelta:
    """The length of a spacing slot: ``retries.source_unavailable_minutes``
    divided by the count of ``source_reads``."""
    minutes = int(lc.policy_value(policy, lc.SOURCE_READS_MINUTES_KEY))
    return timedelta(minutes=minutes) / max(1, lc.budget_limit("source_reads", policy))


def _source_read_slot(since: str, at: str, policy: dict) -> int:
    elapsed = datetime.fromisoformat(at) - datetime.fromisoformat(since)
    return max(0, elapsed // source_read_slot_length(policy))


def source_read_counted(record: dict, policy: dict, now: str) -> bool:
    """Whether a failed read at ``now`` is counted: the first of a run, or
    one in a later spacing slot since ``budget.source_unavailable_since``
    than the last counted read (``budget.source_unavailable_at``). A read in
    the same slot writes nothing, so a burst of ticks adds no events."""
    budget = record.get("budget") or {}
    since, last = budget.get("source_unavailable_since"), budget.get("source_unavailable_at")
    if not int(budget.get("source_unavailable", 0)) or not since or not last:
        return True
    return _source_read_slot(since, now, policy) > _source_read_slot(since, last, policy)


def source_reads_spent(record: dict, policy: dict, now: str) -> bool:
    """Whether a failed read at ``now`` exhausts ``source_reads``: at least
    ``retries.source_unavailable_minutes`` minutes passed since the first
    failed read of the run (``budget.source_unavailable_since``), and the
    counted failed reads (``budget.source_unavailable``), this one included
    when it is counted (``source_read_counted``), reached
    ``retries.source_unavailable_reads``. A read that meets both is not held
    back by the spacing, and a burst of ticks (issue, comment, and push
    events tick too) never exhausts it alone. A count recorded without its
    start time is not exhausted: the next charge records the start."""
    budget = record.get("budget") or {}
    since = budget.get("source_unavailable_since")
    count = int(budget.get("source_unavailable", 0))
    if not count or not since:
        return False
    minutes = int(lc.policy_value(policy, lc.SOURCE_READS_MINUTES_KEY))
    if datetime.fromisoformat(now) - datetime.fromisoformat(since) < timedelta(minutes=minutes):
        return False
    if source_read_counted(record, policy, now):
        count += 1
    return count >= lc.budget_limit("source_reads", policy)


def exhausted(record: dict, names: tuple[str, ...], *, policy: dict, now: str,
              ci_evidence: dict | None = None) -> list[str]:
    """The budgets among ``names`` whose count has reached its limit, in that
    order. ``repair_attempts_cluster`` counts only for the failing CI job
    recorded for the head."""
    ctx = Context(record, state_key(record), policy, now, ci_evidence=ci_evidence)
    return [name for name in names
            if (name != "repair_attempts_cluster" or ctx.cluster)
            and remains(name, ctx) is False]


def _remain(*names: str) -> Callable[[Context], str | None]:
    def check(ctx: Context) -> str | None:
        for name in names:
            left = remains(name, ctx)
            if left is None:
                return f"the count of {name} is not known"
            if not left:
                return f"{name} is exhausted"
        return None
    return check


def _any_exhausted(*names: str) -> Callable[[Context], str | None]:
    def check(ctx: Context) -> str | None:
        unknown = None
        for name in names:
            left = remains(name, ctx)
            if left is False:
                return None
            if left is None:
                unknown = name
        if unknown:
            return f"the count of {unknown} is not known"
        return f"{' and '.join(names)} remain"
    return check


for _name in lc.BUDGETS:
    _register(f"{_name} remains", f"{_name}_remains", "record", _remain(_name))
    _register(f"{_name} exhausted", f"{_name}_exhausted", "record", _any_exhausted(_name))

_register("work_recovery and work_total remain", "work_recovery_and_work_total_remain", "record",
          _remain("work_recovery", "work_total"))
_register(lc.REPAIR_BUDGETS_REMAIN, "repair_budgets_remain", "record",
          _remain("repair_attempts_cluster", "no_progress"))
_register(lc.REVISION_BUDGETS_REMAIN, "revision_budgets_remain", "record",
          _remain("repair_attempts_cluster", "no_progress", "work_total"))
_register(lc.REVIEW_REVISION_BUDGETS_REMAIN, "review_revision_budgets_remain", "record",
          _remain("no_progress", "work_total"))


def _cluster_exhausted(ctx: Context) -> bool | None:
    """``repair_attempts_cluster`` counts only for a recorded failing CI job;
    ``None`` when that job is not known."""
    known, job = recorded_failing_job(ctx.record, ctx.ci_evidence)
    if not known:
        return None
    return bool(job) and remains("repair_attempts_cluster", ctx) is False


@_record(lc.REVISION_BUDGET_EXHAUSTED, "revision_budget_exhausted")
def _revision_budget_exhausted(ctx: Context) -> str | None:
    if remains("no_progress", ctx) is False or remains("work_total", ctx) is False:
        return None
    cluster = _cluster_exhausted(ctx)
    if cluster is None:
        return UNKNOWN_EVIDENCE
    return None if cluster else "no_progress, work_total, and repair_attempts_cluster remain"


@_record(lc.REPAIR_BUDGET_EXHAUSTED, "repair_budget_exhausted")
def _repair_budget_exhausted(ctx: Context) -> str | None:
    if remains("no_progress", ctx) is False:
        return None
    cluster = _cluster_exhausted(ctx)
    if cluster is None:
        return UNKNOWN_EVIDENCE
    return None if cluster else "no_progress and repair_attempts_cluster remain"


@_record(lc.BASE_BRANCH_UNREADABLE, "base_branch_unreadable")
def _base_branch_unreadable(ctx: Context) -> str | None:
    if _asserted(ctx, "upstream_repository_unread"):
        return "the upstream repository itself could not be read"
    if not _asserted(ctx, "base_branch_unread"):
        return "the base branch was not read without an answer"
    if remains("source_reads", ctx) is not False:
        return "source_reads remains"
    return None


@_record(lc.UPSTREAM_UNREADABLE, "upstream_unreadable")
def _upstream_unreadable(ctx: Context) -> str | None:
    if not _asserted(ctx, "upstream_repository_unread") or not _asserted(ctx, "base_branch_unread"):
        return "the upstream repository was read"
    # A renamed repository is a definitive answer (`require_renamed_upstream`),
    # never the exhaustion of `source_reads`.
    if _asserted(ctx, "upstream_repository_renamed"):
        return "GitHub answered for the repository under another name"
    if remains("source_reads", ctx) is not False:
        return "source_reads remains"
    return None


@_record(lc.UPSTREAM_READ_WAIT, "upstream_read_wait")
def _upstream_read_wait(ctx: Context) -> str | None:
    # Failed reads name the counter `source_reads`; `require_renamed_upstream`
    # is the only row that records `upstream_unavailable` without a counter.
    pending = ctx.record.get("pending_human_action") or {}
    if pending.get("counter") == "source_reads" or (
            pending.get("kind") == lc.UPSTREAM_UNAVAILABLE and pending.get("counter") is None):
        return None
    return ("the wait was entered neither because reads kept failing nor because GitHub named "
            "the repository otherwise")


@_record(lc.RENAME_CAUSED_WAIT, "rename_caused_wait")
def _rename_caused_wait(ctx: Context) -> str | None:
    # `require_renamed_upstream` is the only row that records
    # `upstream_unavailable` without a counter.
    pending = ctx.record.get("pending_human_action") or {}
    if pending.get("kind") != lc.UPSTREAM_UNAVAILABLE or pending.get("counter") is not None:
        return "the wait was not entered because GitHub named the repository otherwise"
    return None


def recovery_exhaustion(ctx: Context) -> tuple[str, str] | str:
    """The pending action and counter of ``exhaust_recovery`` for the failure
    the caller asserts, or why the row does not apply: exactly one failure is
    asserted, the task or the publication it concerns is held, and one of
    the budgets that recover it is exhausted (``work_total`` is named before
    ``work_recovery``, so the resume starts the budget of the generation)."""
    failures = [fact for fact in lc.RECOVERY_FAILURES if _asserted(ctx, fact)]
    if len(failures) != 1:
        return "the failure whose recovery is exhausted is not established"
    budgets, kind = lc.RECOVERY_FAILURES[failures[0]]
    if failures[0] == "publication_infrastructure_error":
        if not publication_pending(ctx.record):
            return "no publication is pending"
    elif not ctx.record.get("active_work"):
        return "no task is requested"
    unknown = None
    for name in budgets:
        left = remains(name, ctx)
        if left is False:
            return kind, name
        if left is None:
            unknown = name
    if unknown:
        return f"the count of {unknown} is not known"
    return f"{' and '.join(budgets)} remain"


@_record(lc.RECOVERY_EXHAUSTED, "recovery_exhausted")
def _recovery_exhausted(ctx: Context) -> str | None:
    found = recovery_exhaustion(ctx)
    return found if isinstance(found, str) else None


@_record(lc.NOT_PIPELINE_FAILURE, "not_pipeline_failure")
def _not_pipeline_failure(ctx: Context) -> str | None:
    if _asserted(ctx, "pipeline_failure"):
        return "the last Claim of the task failed before its agent for a failure of the pipeline"
    return None


@_record(lc.PROVISIONING_NEEDS_INTERVENTION, "provisioning_needs_intervention")
def _provisioning_needs_intervention(ctx: Context) -> str | None:
    if _asserted(ctx, "provisioning_blocked"):
        return None
    if remains("operation_launch", ctx) is False:
        if ctx.record.get("paused"):
            return "operation_launch is exhausted but the contribution is paused"
        return None
    return "provisioning is not blocked and operation_launch remains"


@_record(lc.DEVELOPMENT_PROPOSAL_REJECTED, "development_proposal_rejected")
def _development_proposal_rejected(ctx: Context) -> str | None:
    if _asserted(ctx, "proposal_rejected_or_blocked"):
        return None
    if _asserted(ctx, "failure_not_caused_by_patch") and (
            remains("flaky_reruns", ctx) is False or carried_feedback(ctx.record)):
        return None
    return "the proposal is not rejected and the worker is not blocked"


# Facts: guards only the caller can observe. The name is what the caller
# asserts in ``facts``.
for _guard, _name in (
    ("worker slot free", "worker_slot_free"),
    ("capacity available", "capacity_available"),
    (lc.PIPELINE_CHANGED, "pipeline_changed_since_pause"),
    ("intake label removed", "intake_label_removed"),
    ("fork_provision operation planned", "fork_provision_planned"),
    (lc.DEVELOPMENT_PR_PLANNED, "development_pr_operation_planned"),
    ("exact development PR readback", "development_pr_read_back"),
    ("the receipt matches the operation", "receipt_matches_operation"),
    ("exact fork and ref readback", "fork_and_ref_read_back"),
    ("the prerequisite is corrected", "prerequisite_corrected"),
    ("a dispatcher run ended with the requested task absent or unclaimed", "dispatch_lost"),
    (lc.DISPATCH_CAUSE_CHANGED, "dispatch_cause_changed"),
    ("the task graph is terminal without a usable Result", "work_terminal_without_result"),
    (lc.PIPELINE_FAILURE, "pipeline_failure"),
    (lc.PIPELINE_FAILURE_CLEARED, "pipeline_failure_cleared"),
    (lc.PLAN_REJECTED, "plan_rejected"),
    ("the Result has no usable plan", "no_usable_plan"),
    (lc._ADMISSION, "development_admitted"),
    (lc.PATCH_SCOPE_COMPLETE, "patch_scope_complete"),
    ("no refresh recorded, or the recorded fork or development PR no longer matches",
     "fresh_fork_required"),
    ("the recorded fork and development PR still match", "recorded_fork_matches"),
    ("verified plan decision park", "plan_decision_park"),
    ("verified plan decision close", "plan_decision_close"),
    (lc.CLOSE_CONFIRMED, "close_confirmed_by_pypi"),
    ("the Result is published, or uncertain and later confirmed", "result_published"),
    ("the proposal no longer applies to the fork head", "proposal_stale"),
    ("the worker reports a failure not caused by the patch", "failure_not_caused_by_patch"),
    ("publication failed with an infrastructure error", "publication_infrastructure_error"),
    (lc.HEAD_FAILED, "ci_failed_on_head"),
    (lc.HEAD_NOT_FAILED, "head_not_failed"),
    (lc._INTERRUPTED, "ci_interrupted_on_head"),
    ("authorized actionable review feedback", "actionable_review"),
    ("authorized explanation-only inquiry", "explanation_inquiry"),
    ("the observed fork head differs from the recorded head", "fork_head_changed"),
    ("the observed development PR does not match the recorded fork, ref, or PR, or is closed",
     "development_pr_mismatch"),
    ("every required CI job executed and passed on the exact head", "ci_passed_on_head"),
    (lc.SERIES_REQUIREMENTS_MET, "series_requirements_met"),
    (lc.CANDIDATE_FAILED, "candidate_head_failed"),
    (lc.CANDIDATE_NOT_FAILED, "candidate_head_not_failed"),
    ("the observed fork head differs from the candidate head", "candidate_head_changed"),
    ("the lease write or its readback failed with an infrastructure error",
     "cleanup_infrastructure_error"),
    ("the lease write and ref readback match the reviewed series", "clean_head_read_back"),
    ("the reviewed series does not rebuild to the reviewed head, or the lease on `riscv64` "
     "conflicts", "upstream_branch_invalid"),
    (("a pull request is open from `riscv64` (upstream or in the fork), `riscv64-dev` is "
                 "missing, or the record's development branch is not `riscv64-dev`"), "upstream_branch_blocked"),
    (lc.REWRITE_PASSED, "submission_rewrite_passed"),
    (lc.REWRITE_FAILED, "submission_rewrite_failed"),
    (lc.REWRITE_EXPIRED, "submission_rewrite_expired"),
    (lc.CLEAN_HEAD_CI_PASSED, "ci_passed_on_clean_head"),
    (lc.CLEAN_HEAD_CI_MISSING, "clean_head_ci_missing"),
    ("a blocking CI failure on the clean head, or the observed `riscv64` head differs from the "
     "clean head", "clean_head_needs_repair"),
    ("posting the package failed", "submission_comment_failed"),
    (lc.CLEAN_HEAD_MOVED, "clean_head_moved"),
    ("the package comment is read back", "submission_package_read_back"),
    ("an observation that does not satisfy verify_upstream_pr", "unverified_upstream_observation"),
    (lc.UPSTREAM_PR_ON_BASE, "upstream_pr_open_on_base"),
    ("its head is `riscv64` of the contribution fork at the exact clean head",
     "upstream_pr_at_clean_head"),
    ("a compatible human author", "upstream_pr_human_author"),
    ("an executed blocking failure on the upstream PR head", "upstream_ci_failed"),
    ("not yet recorded for this head and CI run", "upstream_failure_not_recorded"),
    ("the upstream PR head changed and the pipeline did not publish the change",
     "upstream_head_changed_externally"),
    ("the upstream PR is closed without merge", "upstream_pr_closed_unmerged"),
    ("the upstream PR is merged", "upstream_pr_merged"),
    ("the revision is published to the upstream PR head", "revision_published"),
    ("the proposal is rejected for the current revision, or the worker reports it is blocked",
     "proposal_rejected_or_blocked"),
    ("the worker reports that no change is needed", "no_change_needed"),
    ("release evidence does not yet satisfy the acceptance contract", "release_incomplete"),
    ("release evidence needs operator action", "release_needs_operator"),
    ("verified release coverage satisfies the acceptance contract", "release_verified"),
    ("the Result does not match the current generation, revision, and a requested task",
     "result_mismatch"),
    ("the observed upstream repository identity differs from the recorded one",
     "upstream_identity_changed"),
    (lc.PROCESSING_ERROR, "processing_error"),
    (lc.BASE_BRANCH_UNREAD, "base_branch_unread"),
    (lc.BASE_BRANCH_GONE, "base_branch_missing"),
    (lc.BASE_RENAMED_IN_PR, "base_renamed_in_pr"),
    (lc.UPSTREAM_READ_AGAIN, "upstream_read_again"),
    (lc.UPSTREAM_RENAMED, "upstream_repository_renamed"),
):
    _fact(_guard, _name)

# ``upstream_pr_open_or_merged`` is required only when the pending action is
# ``pr_closed_unmerged``; the guard text states the condition.
_register("for pr_closed_unmerged, the upstream PR is observed open again or merged",
          "upstream_pr_open_or_merged", "fact",
          lambda ctx: None
          if (ctx.record.get("pending_human_action") or {}).get("kind") != "pr_closed_unmerged"
          or _asserted(ctx, "upstream_pr_open_or_merged")
          else "the upstream PR is not observed open again or merged")

# Facts read by a record predicate in addition to the fact predicates above.
EXTRA_FACTS = frozenset({"upstream_pr_not_open", "provisioning_blocked",
                         "upstream_repository_unread", "series_requirements_unmet"})
FACT_NAMES = frozenset(p.name for p in PREDICATES.values() if p.kind == "fact") | EXTRA_FACTS
# Facts that state the opposite of each other; a caller that asserts both has
# established neither.
CONTRADICTORY_FACTS = (("candidate_head_failed", "candidate_head_not_failed"),
                       ("ci_failed_on_head", "head_not_failed"),
                       ("submission_rewrite_passed", "submission_rewrite_failed"),
                       ("base_branch_unread", "base_branch_missing"),
                       ("upstream_repository_unread", "base_branch_missing"),
                       ("upstream_repository_renamed", "base_branch_missing"),
                       ("upstream_read_again", "base_branch_unread"),
                       ("series_requirements_met", "series_requirements_unmet"))


def table_guards() -> set[str]:
    """Every guard text of the lifecycle table and its flags."""
    guards = {g for e in lc.table_edges() for g in e.transition.guard}
    for flag in lc.FLAGS.values():
        for _verb, t in flag.transitions():
            guards.update(t.guard)
    return guards


# --------------------------------------------------------------------------
# Verbs

# Commands the mask of the ``paused`` flag still admits.
PAUSED_COMMANDS = lc.PAUSED_COMMANDS

# Verbs that pipeline code takes through ``try_apply_transition``, with the module
# that takes them. ``return_for_repair`` is taken by observation at the gate
# and in ``handoff.waiting_ci``, and fired by a build of `riscv64` whose
# reviewed series does not rebuild or whose lease conflicts, and by a package
# attempt that finds `riscv64` off the clean head.
PIPELINE_VERBS: dict[str, str] = {
    # Entering and leaving the submission gate, by observation, and the
    # review comments observed at the gate.
    "nominate_for_handoff": "observe_ci",
    "return_for_repair": "observe_ci",
    "record_actionable_review": "observe_ci",
    "record_explanation": "observe_ci",
    "record_head_change": "observe_ci",
    # Observation of the published development head: a CI failure, and a
    # development PR that does not match the record.
    "record_ci_failure": "observe_ci",
    "return_for_feedback": "observe_ci",
    "record_pr_mismatch": "observe_ci",
    # A required job that was cancelled or lost its runner: a `ci_rerun`
    # operation within `interrupted_reruns`, and the wait once it is spent.
    "rerun_interrupted_ci": "observe_ci",
    "exhaust_interrupted_reruns": "observe_ci",
    # The Result of a patch task: published, rejected, no longer applicable to
    # the fork head, or held for a publish-only retry within
    # `publish_recovery`, and the wait once that budget is exhausted.
    "record_publish_success": "reconcile",
    "reject_patch_proposal": "reconcile",
    "replan_development": "reconcile",
    "schedule_publish_retry": "reconcile",
    # A failure the repair worker reports not caused by the patch, once the
    # trusted publication job reran the failed jobs on the exact head and
    # read the new attempt back (or found nothing to rerun).
    "request_ci_rerun": "reconcile",
    # A dispatcher run that ended with the requested task absent or
    # unclaimed, within `dispatch_recovery`.
    "retry_dispatch": "reconcile",
    # The exhaustion of the recovery of a lost dispatch, of a terminal task
    # graph (also taken by `controller.terminal_work`), or of a failed
    # publication: the wait for a human.
    "exhaust_recovery": "reconcile",
    # A dispatch failed while the installed work-queue Policy refused every
    # dispatch, once the dispatcher has reinstalled the compiled policy.
    "rearm_dispatch": "reconcile",
    # A worker Result that matches no request of the record, in any state.
    "record_late_effect": "reconcile",
    # Task allocation, by the table's rows, and the exhaustion of its budgets
    # where a task is due.
    "begin_planning": "controller",
    "dispatch_planning": "controller",
    "request_patch": "controller",
    "request_repair": "controller",
    "request_upstream_revision": "controller",
    "request_review_revision": "controller",
    "exhaust_work_total": "controller",
    "refresh_on_exhaustion": "controller",
    "exhaust_repair_attempts": "controller",
    # A terminal task graph without a usable Result (taken by
    # `controller.terminal_work` for the reconciler and for an allocation
    # that names a spent graph), and a planning run Result without a
    # usable plan.
    "allocate_work_retry": "controller",
    # A fresh graph for a task whose last Claim failed before its agent for a
    # failure of the pipeline, once the pipeline code changed (or a human
    # launched the controller): nothing is charged.
    "reallocate_after_pipeline_failure": "controller",
    "reject_plan": "reconcile",
    "exhaust_planning_recovery": "reconcile",
    # The verified plan decides: development on a new fork or on the
    # recorded one, a park, or a close.
    "admit_development": "reconcile",
    "readmit_development": "reconcile",
    "park_planning": "reconcile",
    "close_planning": "reconcile",
    # The submission command, the stop and resume commands, and refresh, on a
    # record in a state of the table, and the controller's own stops.
    "submit_upstream": "controller",
    "retract_submission": "controller",
    "abandon": "controller",
    "pause": "controller",
    "resume_pause": "controller",
    "resume_stale": "controller",
    "park_contribution": "controller",
    "refresh_planning": "controller",
    "restart_generation": "controller",
    "pause_on_identity_change": "controller",
    # The resumes of the human waits of the table.
    "resume_planning": "controller",
    "resume_provisioning": "controller",
    "resume_ci": "controller",
    "resume_development": "controller",
    "resume_submission_review": "controller",
    "resume_upstream_branch": "controller",
    "resume_submission_preparation": "controller",
    "resume_upstream_review": "controller",
    "resume_upstream_revision": "controller",
    "resume_release_observation": "controller",
    # The reminder of a pending human step.
    "record_reminder": "controller",
    # The stop after an unexpected error while one contribution was processed.
    "pause_on_processing_error": "reconcile",
    # The intake label removed from a request that has not started.
    "withdraw_request": "controller",
    # The upstream base branch, read where a task or provisioning is due: a
    # failed read within `source_reads`, and the human wait once the
    # upstream answers that the branch is missing or the budget is spent.
    "record_source_unavailable": "controller",
    "require_base_branch": "controller",
    "exhaust_source_reads": "controller",
    "require_upstream_access": "controller",
    "require_renamed_upstream": "controller",
    # A base branch the upstream renamed while the upstream PR is open: the
    # record follows the rename the verified PR shows.
    "follow_renamed_base": "controller",
    "resume_renamed_base": "controller",
    # The wait that failed reads caused ends once the upstream is read again,
    # and the wait a renamed repository caused once GitHub answers for it
    # under its recorded name again.
    "resume_upstream_read": "controller",
    # The fork provisioning operation: its launches within `operation_launch`,
    # the wait once that budget is exhausted or the operation failed, and its
    # confirmation by the receipt and readback (rows the operation fires
    # through ``recovery.fire``).
    "start_operation": "controller",
    "require_provisioning_intervention": "controller",
    "confirm_provisioning": "recovery",
    # The development PR operation of `development.waiting_ci` (the PR and
    # the CI bootstrap): its launches within `operation_launch`, the wait once
    # that budget is exhausted, and its confirmation by the receipt and
    # readback (a row the operation fires through ``recovery.fire``).
    "launch_development_pr": "controller",
    "exhaust_development_pr_launches": "controller",
    "confirm_development_pr": "recovery",
    # The submission rewrite task: its allocation and the exhaustion of
    # `work_total` where it is due, its Results, which the trusted checks
    # decide, and a rewrite whose artifact expired.
    "request_submission_rewrite": "controller",
    "accept_submission_rewrite": "reconcile",
    "reject_submission_rewrite": "reconcile",
    "expire_submission_rewrite": "controller",
    # The handoff operations (building `riscv64`, the submission package):
    # the rows each attempt fires through ``recovery.fire`` when its verified
    # receipt is folded, or when the controller closes an attempt whose
    # launches are spent without one.
    "confirm_clean_head": "recovery",
    "retry_upstream_branch": "recovery",
    "exhaust_upstream_branch_retries": "recovery",
    "require_branch_intervention": "recovery",
    "confirm_submission_package": "recovery",
    "retry_submission_comment": "recovery",
    "exhaust_comment_retries": "recovery",
    # CI on the clean head, and the locked wait for the upstream PR, by
    # observation.
    "accept_clean_head_ci": "observe_ci",
    "require_clean_head_ci": "observe_ci",
    "verify_upstream_pr": "observe_ci",
    "record_audit_observation": "observe_ci",
    # Upstream review, by observation of the upstream PR: a CI failure on its
    # head once per head and run, its merge, and its closure without merge
    # (its head change and review comments are the verbs above); the Result
    # of a revision task.
    "record_upstream_ci_failure": "observe_ci",
    "record_merge": "observe_ci",
    "record_pr_closed_unmerged": "observe_ci",
    "record_revision_publish_success": "reconcile",
    "require_reviewer_answer": "reconcile",
    # The official release, by observation (`release.decide` decides the row).
    "record_release_incomplete": "observe_ci",
    "require_release_intervention": "observe_ci",
    "verify_release": "observe_ci",
}

def table_commands() -> frozenset[str]:
    """The commands of the lifecycle table, by their grammar verb."""
    texts = {e.transition.command for e in lc.table_edges()}
    for flag in lc.FLAGS.values():
        texts.update(t.command for t in (*flag.set_by.values(), *flag.cleared_by.values()))
    return frozenset(text.split()[-1] for text in texts if text)


# Commands the controller applies through this engine to a record in a state
# of the table: every command of the table. In the handoff phase every
# command is the table's.
TABLE_COMMANDS = table_commands()


def takes_command(record: dict, verb: str) -> bool:
    """Whether the command ``verb`` on ``record`` is applied by this engine."""
    if record.get("phase") == "handoff":
        return True
    return verb in TABLE_COMMANDS and state_key(record) in lc.LIFECYCLE


def table_verbs() -> set[str]:
    verbs = {e.verb for e in lc.table_edges()}
    for flag in lc.FLAGS.values():
        verbs.update(flag.set_by)
        verbs.update(flag.cleared_by)
    return verbs


# Verbs of the table that no pipeline code takes.
UNWIRED_VERBS: frozenset[str] = frozenset(table_verbs() - set(PIPELINE_VERBS))

# Every verb of ``UNWIRED_VERBS``, with the entry of the known-gaps page that
# records the open decision it waits on. A test requires the two to be equal.
# No decision is open: every verb of the table is taken by the pipeline.
OPEN_DECISIONS: dict[str, str] = {}

# Commands the grammar and the engine accept but the controller holds: the
# controller checks such a command against the table, then refuses it with
# this reason and changes nothing. No command is held: every command of the
# table is applied as its row states.
HELD_COMMANDS: dict[str, str] = {}


def resolve(state: str, verb: str, *, command: bool = True) -> tuple[lc.Transition, str]:
    """The transition ``verb`` from ``state`` and the state it leads to.
    Without a command (``command=False``) a clear of a flag that has a
    variant under another trigger is that variant (``Flag.variants``)."""
    if state not in lc.LIFECYCLE:
        raise TransitionError(f"{state} is not a state of the lifecycle table")
    for flag in lc.FLAGS.values():
        if verb in flag.set_by:
            return flag.set_by[verb], flag.set_moves.get(state, state)
        if not command and verb in flag.variants:
            return flag.variants[verb], state
        if verb in flag.cleared_by:
            return flag.cleared_by[verb], state
    try:
        edge = lc.find_edge(state, verb)
    except KeyError:
        raise TransitionError(f"{verb} does not leave {state}") from None
    return edge.transition, edge.target


def _mask(ctx: Context, verb: str, t: lc.Transition, target: str) -> str | None:
    """What the ``paused`` flag forbids (``lifecycle.masked_while_paused``)."""
    if not ctx.record.get("paused") or not lc.masked_while_paused(verb, t, ctx.state, target):
        return None
    if t.trigger == lc.Trigger.RESULT:
        return "contribution is paused; the Result no longer matches"
    if t.trigger == lc.Trigger.OBSERVATION:
        return "contribution is paused; the observation is read again after the resume"
    return "contribution is paused"


@dataclass
class _Evaluation:
    """The preconditions of a row and, when they hold, the record after it."""

    result: Ok | Refused
    ctx: Context | None = None
    transition: lc.Transition | None = None
    record: dict[str, Any] | None = None
    charged: tuple[str, ...] = ()
    restarted: dict[str, Any] | None = None


_TABLE_VERBS: set[str] | None = None


def _check_facts(facts) -> None:
    unknown = set(facts) - FACT_NAMES
    if unknown:
        raise TransitionError(f"unknown facts: {', '.join(sorted(unknown))}")
    for pair in CONTRADICTORY_FACTS:
        if set(pair) <= set(facts):
            raise TransitionError(f"contradictory facts: {' and '.join(pair)}")


def invariant_problems(record: dict) -> list[tuple[str, str]]:
    """The invariants the state of ``record`` requires that it violates
    (``lifecycle.REQUIREMENTS``), with why: empty for a record of a state
    outside the table."""
    state = state_key(record)
    if state not in lc.LIFECYCLE:
        return []
    return lc.requirement_problems(state, record)


# The rows a record that violates a requirement of its state may still take:
# the stops (the flag, `park`, `abandon`, `refresh`, a resume, a resolution),
# the audit of a late Result, and the row that heals the requirement.
def exempt_from_requirements(verb: str, t: lc.Transition, name: str) -> bool:
    """Whether ``verb`` is taken from a record that violates the invariant
    ``name`` of its state."""
    if verb == "record_late_effect" or verb in lc.INVARIANTS[name].heal:
        return True
    if any(verb in flag.set_by or verb in flag.cleared_by for flag in lc.FLAGS.values()):
        return True
    return t.trigger == lc.Trigger.COMMAND and t.command in lc.PAUSED_COMMANDS


def _guards(record: dict, verb: str, *, policy: dict, now: str, command: Command | None,
            facts, counts, open_operations, ci_evidence
            ) -> tuple[Refused | None, Context | None, lc.Transition | None, str | None]:
    """The guards of the row of ``verb``: the first refusal, or ``None`` and
    the context, transition and target."""
    global _TABLE_VERBS
    if _TABLE_VERBS is None:
        _TABLE_VERBS = table_verbs()
    if verb not in _TABLE_VERBS:
        raise TransitionError(f"{verb} is not a verb of the lifecycle table")
    _check_facts(facts)
    state = state_key(record)
    try:
        t, target = resolve(state, verb, command=command is not None)
    except TransitionError as error:
        if state not in lc.LIFECYCLE:
            raise
        return Refused(Refusal.NOT_APPLICABLE, verb, None, str(error)), None, None, None
    ctx = Context(record, state, policy, now, command, frozenset(facts), dict(counts or {}),
                  open_operations, ci_evidence)
    masked = _mask(ctx, verb, t, target)
    if masked:
        return Refused(Refusal.PAUSED, verb, None, masked), ctx, t, target
    broken = [(name, why) for name, why in invariant_problems(record)
              if not exempt_from_requirements(verb, t, name)]
    if broken:
        name, why = broken[0]
        return (Refused(Refusal.SOURCE_INVALID, verb, None, f"{state} requires {name}: {why}"),
                ctx, t, target)
    if not t.guard:
        raise TransitionError(f"{state} --{verb}-->: the row states no guard")
    for guard in t.guard:
        predicate = PREDICATES.get(guard)
        if predicate is None:
            raise TransitionError(f"guard {guard!r} has no predicate")
        why = predicate.check(ctx)
        if why:
            return Refused(predicate.refusal, verb, guard, why), ctx, t, target
    return None, ctx, t, target


def _evaluate(record: dict, verb: str, *, policy: dict, now: str, command: Command | None = None,
              facts=(), values: dict[str, Any] | None = None,
              counts: dict[str, int] | None = None,
              open_operations: tuple[str, ...] | None = None,
              ci_evidence: dict | None = None) -> _Evaluation:
    refused, ctx, t, target = _guards(record, verb, policy=policy, now=now, command=command,
                                      facts=facts, counts=counts,
                                      open_operations=open_operations, ci_evidence=ci_evidence)
    if refused is not None:
        return _Evaluation(refused)
    # The effects are simulated on a copy: a record the schema refuses is
    # a refusal, known before anything is applied.
    rec, charged, restarted = _apply_effects(ctx, verb, t, target, dict(values or {}))
    if rec != record:
        rec["updated_at"] = now
    try:
        schemas.validate("contribution", rec)
    except schemas.SchemaError as error:
        return _Evaluation(Refused(Refusal.TARGET_INVALID, verb, None, str(error)[:2000]))
    # The record after the row satisfies the requirements of its target. A
    # row that may leave a broken record (a stop, a heal) adds no breach of
    # its own: the ones it carries from the source are kept.
    carried = {name for name, _why in invariant_problems(record)}
    for name, why in lc.requirement_problems(target, rec):
        if name in carried and exempt_from_requirements(verb, t, name):
            continue
        return _Evaluation(Refused(Refusal.TARGET_INVALID, verb, None,
                                   f"{target} requires {name}: {why}"))
    bumped = rec["revision"] != record["revision"]
    if bumped != (t.effects.revision == "bump"):
        raise TransitionError(f"{verb}: the revision rule is violated")
    return _Evaluation(Ok(verb, target), ctx, t, rec, charged, restarted)


def preconditions(record: dict, verb: str, *, policy: dict, now: str,
                  command: Command | None = None, facts=(),
                  values: dict[str, Any] | None = None, counts: dict[str, int] | None = None,
                  open_operations: tuple[str, ...] | None = None,
                  ci_evidence: dict | None = None) -> Ok | Refused:
    """Whether ``verb`` can be taken now: ``Ok`` with the state it leads to,
    or ``Refused`` with its kind. Pure: the record is never changed. The
    effects are simulated on a copy, so a record the schema would refuse is
    found here (``TARGET_INVALID``). Only ``try_apply_transition``, the tests
    and ``lifecycle check`` call it; a caller that wants a row takes it with
    ``try_apply_transition``. Raises ``TransitionError`` only for a call
    the table does not allow: a verb that is not in the table, an unknown or
    contradictory fact, a value for a field the row does not set, or a
    missing value."""
    return _evaluate(record, verb, policy=policy, now=now, command=command, facts=facts,
                     values=values, counts=counts, open_operations=open_operations,
                     ci_evidence=ci_evidence).result


# The call sites outside the engine that may ask, without taking it, whether
# a row would be admitted (``row_admitted``): the trusted CI rerun workflow,
# which cannot take the row the reconciler takes later, and the fold of a
# patch Result, which chooses its row by it.
ROW_QUERY_SITES = frozenset({("ci_rerun", "preflight"), ("reconcile", "fold_patch_result")})


def row_admitted(record: dict, verb: str, *, policy: dict, now: str, facts=(),
                 command: Command | None = None, counts: dict[str, int] | None = None,
                 open_operations: tuple[str, ...] | None = None,
                 ci_evidence: dict | None = None) -> Refused | None:
    """The read-only query of ``ROW_QUERY_SITES``: ``None`` when the guards
    of ``verb`` hold, else its refusal. It evaluates the guards only (the
    effects need the values the row would set)."""
    refused, *_rest = _guards(record, verb, policy=policy, now=now, command=command, facts=facts,
                              counts=counts, open_operations=open_operations,
                              ci_evidence=ci_evidence)
    return refused


# --------------------------------------------------------------------------
# Effects

# Values of ``sets`` entries the engine derives itself.
def _derived(name: str, ctx: Context) -> tuple[bool, Any]:
    if name == "paused":
        return True, True
    if name in ("paused_at", "pending_human_action.reminded_at", "clean_head_ci_since"):
        return True, ctx.now
    if name == "previous_phase":
        return True, ctx.record["phase"]
    if name in ("pending_publication.exhausted", "automatic_refresh_used"):
        return True, True
    if name == "upstream.base_ref" and ctx is not None and ctx.command is not None \
            and ctx.command.verb == "refresh":
        # The target branch of a new generation: the command's `branch`, else
        # the one the request asked for at intake (`reset_generation`).
        branch = _command_arg(ctx, "branch")
        if not branch:
            return True, intake.requested_target_branch(ctx.record["tracking_issue"])
        try:
            return True, intake.canonical_branch(branch)
        except intake.IntakeError as error:
            raise CommandRefused(str(error)) from None
    return False, None


# ``sets`` entries the engine always derives; the caller gives no value.
ENGINE_VALUES = (lc.WORK_FEEDBACK, lc.RERUNS, lc.PENDING_CREDIT)
# ``sets`` entries whose value the caller may omit.
OPTIONAL_VALUES = frozenset({"admission_override", "paused_reason", "paused_at_commit",
                             lc.PENDING_COUNTER})
# ``sets`` entries filled from the command's reason when no value is given.
REASON_VALUES = frozenset({"parked_reason", "paused_reason"})

_EMPTY: dict[str, Any] = {
    "pending_feedback_evidence_ids": [], "scope_coverage_complete": False, "paused": False,
    "budget.clusters": {}, "budget.no_progress_streak": 0,
}


def _put(record: dict, name: str, value: Any) -> None:
    *parents, leaf = name.split(".")
    node = record
    for part in parents:
        if not isinstance(node.get(part), dict):
            raise TransitionError(f"cannot set {name}: {part} is not recorded")
        node[part] = dict(node[part])
        node = node[part]
    node[leaf] = value


def reset_generation(record: dict) -> dict:
    """A record with every mutable lifecycle field reset; identity, events,
    evidence, and replay fences are kept. The `paused` flag and its fields
    are kept too: only the rows of the flag clear it, so a restart accepted
    while paused leaves the new generation paused until a resume. The
    upstream identity and base are cleared, except the target branch the
    request asked for at intake: the new generation is bound to that branch
    again, and to the upstream default branch only when none was asked for."""
    rec = copy.deepcopy(record)
    rec.update({
        "generation": int(record.get("generation", 0)) + 1, "refresh": None,
        "parked_reason": None, "terminal_reason": None,
        "previous_phase": None,
        "owners": {"primary": None, "backup": None, "submitter": None,
                   "handoff_reviewer": None},
        "admission_class": None, "admission_override": None,
        "automatic_refresh_used": False,
        "upstream": {"repository_id": None, "full_name": record["upstream"]["full_name"],
                     "base_ref": intake.requested_target_branch(record["tracking_issue"]),
                     "base_sha": None},
        "fork": None, "upstream_pr": None, "target_contract_id": None,
        "prerequisite_ids": [], "active_operation_id": None,
        "queue_work_id": None, "queue_claim_handle": None, "queue_result_id": None,
        "proposal_artifact": None, "publication_operation_id": None,
        "pending_publication": None,
        # processed_publications is kept as a replay fence against old run artifacts.
        "processed_review_ids": [], "processed_review_comment_ids": [],
        "processed_pr_conversation_comment_ids": [],
        "pending_feedback_evidence_ids": [], "active_work": None,
        "plan_evidence_id": None, "scope_coverage_complete": False,
        "scope_paths": None, "debug_commits": [], "clean_head_sha": None,
        "clean_head_ci_since": None,
        "submission_rewrite_id": None,
        "handoff_request": None, "handoff_approval": None,
        "last_ci_evidence_id": None, "merge": None, "retirement": None,
        "release_acceptance": None, "pending_human_action": None,
    })
    rec["budget"] = {"policy_id": record["budget"]["policy_id"], "attempts_used": 0,
                     "no_progress_streak": 0, "clusters": {}, "planning_attempts": 0,
                     "reruns": {"head_sha": None, "flaky": 0, "interrupted": 0},
                     "spent_estimate_usd_cents": 0}
    return rec


def _restart_counter(rec: dict, name: str) -> None:
    budget = rec["budget"] = dict(rec["budget"])
    if name == "work_total":
        budget["attempts_used"] = 0
    elif name == "no_progress":
        budget["no_progress_streak"] = 0
    elif name == "planning_attempts":
        budget["planning_attempts"] = 0
    elif name == "repair_attempts_cluster":
        budget["clusters"] = {}
    elif name == "source_reads":
        budget["source_unavailable"] = 0
        for key in SOURCE_READ_FIELDS:
            budget.pop(key, None)
    elif name in RERUN_COUNTERS:
        budget["reruns"] = dict(head_reruns(rec), **{RERUN_COUNTERS[name]: 0})


def _charge(rec: dict, name: str, ctx: Context) -> bool:
    """Charge a budget whose counter is in the record. Returns ``False`` for
    a counter that lives elsewhere."""
    budget = rec["budget"] = dict(rec["budget"])
    if name == "work_total":
        budget["attempts_used"] = int(budget.get("attempts_used", 0)) + 1
    elif name == "no_progress":
        budget["no_progress_streak"] = int(budget.get("no_progress_streak", 0)) + 1
    elif name == "planning_attempts":
        budget["planning_attempts"] = int(budget.get("planning_attempts", 0)) + 1
    elif name == "repair_attempts_cluster":
        if not ctx.cluster:
            raise TransitionError("charging repair_attempts_cluster needs the failing CI job")
        clusters = budget["clusters"] = dict(budget.get("clusters") or {})
        clusters[ctx.cluster] = int(clusters.get(ctx.cluster, 0)) + 1
    elif name == "work_recovery":
        before = int((ctx.record.get("active_work") or {}).get("attempt", 1))
        rec["active_work"] = dict(rec["active_work"], attempt=before + 1)
    elif name == "publish_recovery":
        before = int((ctx.record.get("pending_publication") or {}).get("attempts", 0))
        rec["pending_publication"] = dict(rec["pending_publication"], attempts=before + 1)
    elif name == "source_reads":
        # The first failed read of a run starts its clock. The time of the
        # last counted read spaces the next one, and the reads that could
        # not read the repository itself are counted apart, so that the
        # exhaustion names the cause of most of the run.
        if not int(budget.get("source_unavailable", 0)) or not budget.get("source_unavailable_since"):
            budget["source_unavailable_since"] = ctx.now
            budget.pop("source_unavailable_repository", None)
        budget["source_unavailable"] = int(budget.get("source_unavailable", 0)) + 1
        budget["source_unavailable_at"] = ctx.now
        if _asserted(ctx, "upstream_repository_unread"):
            budget["source_unavailable_repository"] = \
                int(budget.get("source_unavailable_repository", 0)) + 1
    elif name in RERUN_COUNTERS:
        # Counted for the current head: counts of another head start again.
        reruns, key = head_reruns(rec), RERUN_COUNTERS[name]
        budget["reruns"] = dict(reruns, **{key: int(reruns[key]) + 1})
    else:
        return False
    return True


def _clear(rec: dict, before: dict, name: str) -> None:
    """Clear a field, or the part of it a qualified clear names."""
    if name == lc.CARRIED_FEEDBACK:
        carried = set(carried_feedback(before))
        rec["pending_feedback_evidence_ids"] = [
            i for i in before.get("pending_feedback_evidence_ids") or [] if i not in carried]
    elif name in lc.QUALIFIED_CLEARS:
        raise TransitionError(f"the qualified clear {name!r} is not implemented")
    else:
        _put(rec, name, copy.deepcopy(_EMPTY.get(name)))


def _charged_entries(ctx: Context, t: lc.Transition) -> list[str]:
    """The budgets the allocation row ``t`` charges that a lost dispatch gives
    back (``lc.CREDITABLE``), as ``active_work.charged`` records them: the
    repair counter with the failing CI job it was charged for
    (``repair_attempts_cluster:<job>``)."""
    entries = []
    for name in t.charges:
        if name == "repair_attempts_cluster" and ctx.cluster:
            entries.append(f"{name}:{ctx.cluster}")
        elif name in lc.CREDITABLE and name != "repair_attempts_cluster":
            entries.append(name)
    return entries


def credit_entries(record: dict, t: lc.Transition) -> list[str]:
    """The budgets the row ``t`` gives back to ``record``: the ones its
    ``credits`` name among those the lost allocation charged
    (``pending_human_action.credit``), ``work_total`` alone for a wait that
    records none (an allocation before the field existed charged it, as
    every allocation does)."""
    if not t.effects.credits:
        return []
    recorded = (record.get("pending_human_action") or {}).get("credit")
    entries = ["work_total"] if recorded is None else list(recorded)
    return [e for e in entries if e.partition(":")[0] in t.effects.credits]


def _credit(rec: dict, entry: str) -> None:
    """Give back one budget entry of ``credit_entries``."""
    name, _, detail = entry.partition(":")
    budget = rec["budget"] = dict(rec["budget"])
    if name == "work_total":
        budget["attempts_used"] = max(0, int(budget.get("attempts_used", 0)) - 1)
    elif name == "repair_attempts_cluster" and detail:
        clusters = budget["clusters"] = dict(budget.get("clusters") or {})
        if int(clusters.get(detail, 0)) > 1:
            clusters[detail] = int(clusters[detail]) - 1
        else:
            clusters.pop(detail, None)
    else:
        raise TransitionError(f"the budget {entry!r} cannot be credited")


def _apply_effects(ctx: Context, verb: str, t: lc.Transition, target: str,
                   values: dict[str, Any]) -> tuple[dict, tuple[str, ...], dict | None]:
    e = t.effects
    before = ctx.record
    declared = {lc._field_name(s) for s in e.sets} - set(ENGINE_VALUES)
    if e.work in ("allocate", "reallocate"):
        declared.add("active_work")
    extra = set(values) - declared
    if extra:
        raise TransitionError(
            f"{verb} does not set {', '.join(sorted(extra))}; the table row sets "
            f"{', '.join(sorted(declared)) or 'nothing'}")

    restarted = None
    named = (before.get("pending_human_action") or {}).get("counter")
    if e.restarts and named in e.restarts:
        count = budget_count(named, ctx)
        restarted = {"counter": named, "count": count}

    rec = reset_generation(before) if e.generation == "bump" else copy.deepcopy(before)
    if e.revision == "bump":
        rec["revision"] = before["revision"] + 1
    if restarted:
        _restart_counter(rec, restarted["counter"])

    carried = carried_feedback(before)
    for name in e.clears:
        _clear(rec, before, name)
    if e.work == "clear":
        rec["active_work"] = None
    elif e.work in ("allocate", "reallocate"):
        if not isinstance(values.get("active_work"), dict):
            raise TransitionError(f"{verb} allocates a task; active_work is required")
        rec["active_work"] = copy.deepcopy(values["active_work"])
        # A task carries the feedback the row records for it (``WORK_FEEDBACK``)
        # and none otherwise; a fresh graph for the same task carries the
        # same feedback.
        rec["active_work"]["feedback_evidence_ids"] = carried if e.work == "reallocate" else []

    pending_kind = None
    pipeline_version = None
    counter: Any = None
    counter_declared = False
    exhaustion = recovery_exhaustion(ctx) if lc.RECOVERY_EXHAUSTED in t.guard else None
    for entry in e.sets:
        name, literal, value = entry.partition("=")
        if name == "pending_human_action":
            if literal and "|" in value:
                # One of the listed kinds, derived from the failure.
                if not isinstance(exhaustion, tuple) or exhaustion[0] not in value.split("|"):
                    raise TransitionError(f"{verb}: the pending action of the failure is not "
                                          f"one of {value}")
                pending_kind = exhaustion[0]
            else:
                pending_kind = value if literal else values.get(name)
            if not isinstance(pending_kind, str) or not pending_kind:
                raise TransitionError(f"{verb} sets pending_human_action; its kind is required")
            continue
        if name == lc.PENDING_PIPELINE:
            pipeline_version = values.get(name)
            continue
        if name == lc.PENDING_CREDIT:
            continue      # derived below, from what the lost allocation charged
        if name == lc.PENDING_COUNTER:
            counter_declared = True
            if not literal and isinstance(exhaustion, tuple):
                counter = exhaustion[1]
            else:
                counter = value if literal else values.get(name)
            continue
        if name in lc.QUALIFIED_SETS and not rec.get(name.split(".")[0]):
            # A qualified set applies only where its condition holds: the
            # part of a field that is not recorded is not set.
            continue
        if literal:
            _put(rec, name, value)
            continue
        if name == lc.WORK_FEEDBACK:
            _put(rec, name, list(before.get("pending_feedback_evidence_ids") or []))
            continue
        if name == lc.RERUNS:
            continue  # derived below, from the head the row records
        derived, derived_value = _derived(name, ctx)
        if name in REASON_VALUES and verb in lc.CALLER_REASONS \
                and not (isinstance(values.get(name), str) and values[name].strip()):
            raise TransitionError(f"{verb} sets {name}; the caller states the reason")
        if name in values:
            _put(rec, name, copy.deepcopy(values[name]))
        elif derived:
            _put(rec, name, derived_value)
        elif name in REASON_VALUES and _command_arg(ctx, "reason"):
            _put(rec, name, _command_arg(ctx, "reason")[:2000])
        elif name in OPTIONAL_VALUES and (verb not in lc.CALLER_REASONS
                                          or name not in REASON_VALUES):
            _put(rec, name, None)
        else:
            raise TransitionError(f"{verb} sets {name}; a value is required")
    if pending_kind is not None:
        if counter is not None:
            allowed = lc.counters_named(lc.Edge(ctx.state, verb, target, t))
            if counter not in allowed:
                raise TransitionError(f"{verb} cannot name the counter {counter!r}")
        rec["pending_human_action"] = {"kind": pending_kind, "requested_at": ctx.now,
                                       "reminded_at": None, "escalated_at": None,
                                       "counter": counter}
        if pending_kind == "dispatch_failed":
            # What the re-arm needs: the version of the pipeline code the
            # dispatch failed at, and what the lost allocation charged (a task
            # allocated before it was recorded gives back `work_total` alone).
            if isinstance(pipeline_version, str) and pipeline_version:
                rec["pending_human_action"]["pipeline_version"] = pipeline_version
            charged = (before.get("active_work") or {}).get("charged")
            if charged is not None:
                rec["pending_human_action"]["credit"] = list(charged)
    elif counter_declared:
        raise TransitionError(f"{verb} names a counter without a pending human action")
    if lc.RERUNS in e.sets:
        rec["budget"] = dict(rec["budget"], reruns=fresh_reruns(rec))
    if "pending_publication" in values and carried and rec.get("pending_publication"):
        # A Result held for publication carries the feedback of its task.
        rec["pending_publication"] = dict(rec["pending_publication"],
                                          feedback_evidence_ids=carried)

    charged_elsewhere = tuple(name for name in t.charges if not _charge(rec, name, ctx))
    if e.work in ("allocate", "reallocate") and rec.get("active_work"):
        # What this allocation charged, which a lost dispatch gives back.
        # A graph that replaces one that failed for the pipeline charges
        # nothing, so the allocation of the first graph is what it stands for;
        # any other fresh graph charged its own.
        prior = (list(rec["active_work"].get("charged") or [])
                 if verb == "reallocate_after_pipeline_failure" else [])
        rec["active_work"] = dict(rec["active_work"], charged=prior + _charged_entries(ctx, t))
    for entry in credit_entries(before, t):
        _credit(rec, entry)
    rec["phase"], rec["activity"] = target.split(".", 1)
    return rec, charged_elsewhere, restarted


_CAUSE = {lc.Trigger.COMMAND: "command", lc.Trigger.OBSERVATION: "observation",
          lc.Trigger.RESULT: "result", lc.Trigger.OPERATION: "operation",
          lc.Trigger.TICK: "tick"}


def _apply_transition(evaluation: _Evaluation, record: dict, *, now: str,
                      cause_id: str | None, actor: dict | None,
                      details: dict[str, Any] | None) -> Applied:
    """Apply a row whose preconditions hold: the record after it and its
    event. Private: ``try_apply_transition`` is the one way to take a row."""
    ctx, t, rec = evaluation.ctx, evaluation.transition, evaluation.record
    verb, target = evaluation.result.verb, evaluation.result.target
    bumped = rec["revision"] != record["revision"]
    event = lc._event(record, rec, verb, _CAUSE[t.trigger], cause_id, now, bumped)
    info: dict[str, Any] = {"verb": verb, "from_state": ctx.state, "to_state": target}
    if t.charges:
        info["charged"] = list(t.charges)
    if credited := credit_entries(record, t):
        info["credited"] = credited
    if evaluation.restarted:
        info["restarted"] = evaluation.restarted
    if actor:
        info["actor"] = {"id": actor["id"], "login": actor["login"]}
    info.update(details or {})
    event["details"] = info
    return Applied(rec, event, verb, evaluation.charged, evaluation.restarted)


def try_apply_transition(record: dict, verb: str, *, policy: dict, now: str,
                         cause_id: str | None = None, command: Command | None = None,
                         facts=(), values: dict[str, Any] | None = None,
                         counts: dict[str, int] | None = None,
                         open_operations: tuple[str, ...] | None = None,
                         ci_evidence: dict | None = None, actor: dict | None = None,
                         details: dict[str, Any] | None = None) -> Applied | Refused:
    """Take the table transition ``verb`` from the state ``record`` is in when
    its preconditions hold (``preconditions``), else return the refusal.

    ``facts`` names the observations the caller established; ``values`` gives
    the value of each field the row sets that the engine cannot derive;
    ``counts`` supplies the counts of budgets that live outside the record;
    ``ci_evidence`` is the evidence record ``last_ci_evidence_id`` names, from
    which the failing CI job of the head is decided. A refusal leaves the
    record unchanged and emits nothing; the caller handles its kind as
    ``HANDLING`` declares for the call site. A call the table does not
    allow (an unknown verb or fact, a value for a field the row does not
    set, a missing value) raises ``TransitionError``."""
    evaluation = _evaluate(record, verb, policy=policy, now=now, command=command, facts=facts,
                           values=values, counts=counts, open_operations=open_operations,
                           ci_evidence=ci_evidence)
    if isinstance(evaluation.result, Refused):
        return evaluation.result
    return _apply_transition(evaluation, record, now=now, cause_id=cause_id, actor=actor,
                             details=details)


def try_apply_one_of(record: dict, verbs, *, policy: dict, now: str,
                     prepare: Callable[[str], dict[str, Any]] | None = None,
                     **kwargs: Any) -> Applied | Refusals:
    """Take the one row among ``verbs`` whose guards hold, or return a
    refusal for each. ``prepare(verb)`` gives the arguments that depend on
    the row taken (``values``, ``details``, ``cause_id``); the others are
    common. More than one admitted row is a table error
    (``TransitionError``)."""
    common = {k: v for k, v in kwargs.items()
              if k in ("command", "facts", "counts", "open_operations", "ci_evidence")}
    refused: list[Refused] = []
    admitted = []
    for verb in dict.fromkeys(verbs):
        why, *_rest = _guards(record, verb, policy=policy, now=now,
                              command=common.get("command"), facts=common.get("facts", ()),
                              counts=common.get("counts"),
                              open_operations=common.get("open_operations"),
                              ci_evidence=common.get("ci_evidence"))
        (refused if why else admitted).append(why or verb)
    if len(admitted) > 1:
        raise TransitionError(f"{state_key(record)}: {', '.join(admitted)} apply together")
    if not admitted:
        return Refusals(tuple(refused))
    verb = admitted[0]
    out = try_apply_transition(record, verb, policy=policy, now=now,
                               **(kwargs | (prepare(verb) if prepare else {})))
    return out if isinstance(out, Applied) else Refusals((out,))


# --------------------------------------------------------------------------
# Commands


def command_verbs(record: dict, command: Command) -> list[str]:
    """The verbs ``command`` may take from the state the record is in."""
    text = f"/upstreaming {command.verb}"
    state = state_key(record)
    if state not in lc.LIFECYCLE:
        raise TransitionError(f"{state} is not a state of the lifecycle table")
    flag_verbs = [verb for flag in lc.FLAGS.values()
                  for verb, t in (*flag.set_by.items(), *flag.cleared_by.items())
                  if t.command == text]
    rows = [e.verb for e in lc.state_edges(state) if e.transition.command == text]
    if record.get("paused") and text == lc.RESUME:
        # A resume clears the flag first: resume_pause, or resume_stale.
        rows = [verb for verb in rows if verb == "resume_stale"]
    return flag_verbs + rows


def command_states(verb: str) -> list[str]:
    """The states of the table whose rows take the command ``verb``."""
    text = f"/upstreaming {verb}"
    return sorted({e.source for e in lc.table_edges() if e.transition.command == text})


def not_applicable(verb: str, state: str) -> str:
    """The reply to a command no row of ``state`` takes: where it applies."""
    states = command_states(verb)
    where = f"; it applies in {', '.join(f'`{s}`' for s in states)}" if states else ""
    return f"`/upstreaming {verb}` does not apply in `{state}`{where}"


def try_apply_command(record: dict, command: Command, *, policy: dict, now: str,
                      prepare: Callable[[str], dict[str, Any]] | None = None,
                      **kwargs: Any) -> Applied | Refusals:
    """Take the one row an authenticated command takes now, as the table
    defines it (``try_apply_one_of`` over ``command_verbs``), or the
    refusals: ``NOT_APPLICABLE`` naming where the command applies when no
    row of the state takes it."""
    verbs = command_verbs(record, command)
    if not verbs:
        return Refusals((Refused(Refusal.NOT_APPLICABLE, None, None,
                                 not_applicable(command.verb, state_key(record))),))
    return try_apply_one_of(record, verbs, policy=policy, now=now, command=command,
                            prepare=prepare, **kwargs)


# --------------------------------------------------------------------------
# Record states


def state_problem(record: dict) -> str | None:
    """Why a record's state is not one the lifecycle admits, or ``None``.

    A record is in a state of the table; a state of kind ``human_wait``
    names its pending action, and a human wait of the handoff phase carries a
    pending action the table enters it with.
    """
    key = state_key(record)
    state = lc.LIFECYCLE.get(key)
    if state is None:
        return f"{key} is not a lifecycle state"
    if key.endswith(".fix_needed") and record.get("pending_publication"):
        # A task is requested again from here, and a held publication is read
        # nowhere in it: the record would wait forever.
        return f"{key} holds the publication of a Result"
    if state.kind == "human_wait" and not (record.get("pending_human_action") or {}).get("kind"):
        # Every row into a human wait sets the pending action, and no row in
        # it clears the action without setting it (`lifecycle check`).
        return f"{key} is a human wait without a pending action"
    if record["phase"] == "handoff" and state.kind in ("gate", "locked", "human_wait"):
        # Every handoff wait is entered through the table, which names its
        # pending action.
        kind = (record.get("pending_human_action") or {}).get("kind")
        if kind not in lc.pending_kinds(key):
            return f"{key} is never entered with pending_human_action {kind}"
    return None


# The states of the table that wait for a human (kinds `human_wait`, `gate`,
# and `locked`).
HUMAN_WAITS = frozenset(key for key, spec in lc.LIFECYCLE.items()
                        if spec.kind in lc.REMINDER_KINDS)


def waits_for_human(record: dict) -> bool:
    """Whether ``record`` is in a wait of the table for a human: a state of
    kind ``gate``, ``locked``, or ``human_wait`` (which always names its
    ``pending_human_action``, see `state_problem`). Only the rows the table
    lists for the state leave it."""
    return state_key(record) in HUMAN_WAITS


def stopped(record: dict) -> bool:
    """Whether a record is stopped: paused, parked, or terminal."""
    return bool(record.get("paused") or record["phase"] in lc.TERMINAL | {"parked"})


def record_late_effect(record: dict, *, policy: dict, now: str, cause_id: str | None,
                       details: dict[str, Any]) -> dict:
    """The audit event of the table's ``record_late_effect`` for ``record``:
    the rule applies in every state, under the stop flag too, and never
    changes the record."""
    outcome = try_apply_transition(record, "record_late_effect", policy=policy, now=now,
                                   cause_id=cause_id, facts={"result_mismatch"}, details=details)
    match dispatch(outcome, ("engine", "record_late_effect")):
        case Applied() as applied if applied.record == record:
            return applied.event
        case Applied():
            raise TransitionError(f"record_late_effect in {state_key(record)}: the record changed")
        case Bug(refused):
            raise TransitionRefused(refused, ("engine", "record_late_effect"))


# --------------------------------------------------------------------------
# Fields of records written before the strict record schema

# The value of each record field that a record may lack, as the pipeline
# reads an absent one (``record.get(name)``, an empty list, or false).
RECORD_DEFAULTS: dict[str, Any] = {
    "generation": 0, "refresh": None, "paused_at": None, "paused_reason": None,
    "paused_by": None, "parked_reason": None, "terminal_reason": None, "previous_phase": None,
    "admission_class": None, "admission_override": None, "automatic_refresh_used": False,
    "distributions": [], "fork": None, "upstream_pr": None, "target_contract_id": None,
    "prerequisite_ids": [], "active_operation_id": None, "queue_work_id": None,
    "queue_claim_handle": None, "queue_result_id": None, "proposal_artifact": None,
    "publication_operation_id": None, "pending_publication": None,
    "processed_publications": [], "processed_review_ids": [],
    "processed_review_comment_ids": [], "processed_pr_conversation_comment_ids": [],
    "pending_feedback_evidence_ids": [], "active_work": None, "plan_evidence_id": None,
    "scope_coverage_complete": False, "scope_paths": None, "debug_commits": [],
    "clean_head_sha": None, "clean_head_ci_since": None, "submission_rewrite_id": None,
    "handoff_request": None, "handoff_approval": None,
    "last_ci_evidence_id": None, "merge": None, "retirement": None,
    "release_acceptance": None, "pending_human_action": None, "follows": None,
}
# The fields of the record the strict schema does not require: an absent
# one reads as unknown, so no record is migrated for it. `paused_at_commit`
# is the version of the pipeline code a controller stop after a processing
# error ran (the controller resumes the stop once that code changed).
OPTIONAL_RECORD_FIELDS = ("paused_at_commit",)
# The counts of `budget` a record may lack; `reruns` starts for the recorded
# head (``fresh_reruns``), as the engine reads absent rerun counts.
BUDGET_DEFAULTS: dict[str, Any] = {
    "no_progress_streak": 0, "clusters": {}, "planning_attempts": 0,
    "spent_estimate_usd_cents": 0,
}
# The fields of a requested task a record may lack: a task recorded without a
# generation is carried as generation 0 by the field migration, as its first
# attempt, undispatched, and with no reason or feedback.
WORK_DEFAULTS: dict[str, Any] = {
    "generation": 0, "attempt": 1, "dispatched_run_id": None, "reason_ids": [],
    "feedback_evidence_ids": [],
}
RETIRED_FIELDS = ("blocked_reason",)
# Fields removed whatever their value: `next_poll_at` was written by CI
# observation (nothing read it for CI) and by release observation (which now
# reads PyPI on every run); `held_patch` was the unpublished patch of the
# review task that no longer exists, `review_comments` the comments due on
# development PRs for the same review, and `submission_review_id` the accepted
# review of a submission rewrite, which the rewrite's own review loop replaced.
RETIRED_ANY_FIELDS = ("next_poll_at", "held_patch", "review_comments", "submission_review_id")
# The states of the separate reviews, which the review loop of the worker
# retired, and the state each record in one goes to: the unpublished patch
# (or the accepted rewrite) is dropped, and the task is requested again.
RETIRED_STATES = {
    "development.review_pending": "development.fix_needed",
    "development.reviewing": "development.fix_needed",
    "development.publishing": "development.fix_needed",
    "handoff.review_pending": "handoff.review_due",
    "handoff.reviewing": "handoff.review_due",
}
# The pending action of a review of a held patch that could not run, which
# now is a failed task.
RETIRED_PENDING_KINDS = {"patch_review_unavailable": "work_failed"}
# The count of failed reviews in succession, a field of `budget` removed
# whatever its value.
RETIRED_BUDGET_FIELD = "review_rejections"
# A field of `budget.reruns` that is removed whatever its value.
RETIRED_RERUN_FIELD = "answered_ci_evidence_id"


def with_defaults(record: dict) -> tuple[dict, list[str], list[str]]:
    """``record`` with every field the strict schema requires and it lacks
    added at the value the pipeline reads for an absent one, the null
    retired fields, `next_poll_at`, and `budget.reruns.answered_ci_evidence_id` removed, with
    the names added and removed. A record in a retired state of the separate
    reviews goes to the state that requests the task again, and a pending
    action of a retired kind to its replacement; both are listed as
    ``state <name>`` and ``pending_human_action.kind <name>``. Raises
    ``TransitionError`` for a retired field that is set."""
    new = copy.deepcopy(record)
    added: list[str] = []
    removed: list[str] = []
    moved = RETIRED_STATES.get(state_key(new))
    if moved:
        removed.append(f"state {state_key(new)}")
        new["phase"], new["activity"] = moved.split(".")
        added.append(f"state {moved}")
        new["active_work"] = None
        # The held Result of a publication is the held patch's: nothing in
        # `development.fix_needed` reads it, so it would strand the record.
        if new.get("pending_publication") is not None:
            removed.append("pending_publication")
        new["pending_publication"] = None
        if moved == "handoff.review_due":
            # The accepted rewrite is made again with the task.
            new["submission_rewrite_id"] = None
    pending = new.get("pending_human_action")
    if isinstance(pending, dict) and pending.get("kind") in RETIRED_PENDING_KINDS:
        replacement = RETIRED_PENDING_KINDS[pending["kind"]]
        removed.append(f"pending_human_action.kind {pending['kind']}")
        new["pending_human_action"] = dict(pending, kind=replacement)
        added.append(f"pending_human_action.kind {replacement}")
    for name in RETIRED_FIELDS:
        if name in new:
            if new[name] is not None:
                raise TransitionError(f"{name} is {new[name]!r}: only a null {name} is removed")
            del new[name]
            removed.append(name)
    for name in RETIRED_ANY_FIELDS:
        if name in new:
            del new[name]
            removed.append(name)
    for name, value in RECORD_DEFAULTS.items():
        if name not in new:
            new[name] = copy.deepcopy(value)
            added.append(name)
    budget = new.get("budget")
    if isinstance(budget, dict):
        budget = new["budget"] = dict(budget)
        if RETIRED_BUDGET_FIELD in budget:
            del budget[RETIRED_BUDGET_FIELD]
            removed.append(f"budget.{RETIRED_BUDGET_FIELD}")
        for name, value in BUDGET_DEFAULTS.items():
            if name not in budget:
                budget[name] = copy.deepcopy(value)
                added.append(f"budget.{name}")
        if "reruns" not in budget:
            budget["reruns"] = fresh_reruns(new)
            added.append("budget.reruns")
        elif isinstance(budget["reruns"], dict) and RETIRED_RERUN_FIELD in budget["reruns"]:
            # The fence of the CI run a `request_ci_rerun` answered, from
            # before the trusted rerun: a confirmed rerun operation replaces
            # it (``ci_rerun.awaited``).
            budget["reruns"] = {k: v for k, v in budget["reruns"].items()
                                if k != RETIRED_RERUN_FIELD}
            removed.append(f"budget.reruns.{RETIRED_RERUN_FIELD}")
    pending = new.get("pending_human_action")
    if isinstance(pending, dict) and "counter" not in pending:
        new["pending_human_action"] = dict(pending, counter=None)
        added.append("pending_human_action.counter")
    work = new.get("active_work")
    if isinstance(work, dict):
        work = new["active_work"] = dict(work)
        for name, value in WORK_DEFAULTS.items():
            if name not in work:
                work[name] = copy.deepcopy(value)
                added.append(f"active_work.{name}")
    return new, added, removed


def with_current_names(record: dict) -> tuple[dict, list[str]]:
    """``record`` with the names of the planning phase and its plan that it
    carries from before the rename (``lifecycle.HISTORIC_NAMES``) replaced,
    and the places it replaced: the fields `dossier_evidence_id` and
    `budget.investigation_attempts`, and the historic values of `phase`,
    `previous_phase`, `pending_human_action.kind`, and
    `pending_human_action.counter`. Nothing else changes. Raises
    ``TransitionError`` for a requested task of the historic task, whose queued
    payload names a worker profile that no longer exists, and for a record
    that carries a field under both names."""
    historic = lc.HISTORIC_NAMES
    new = copy.deepcopy(record)
    renamed: list[str] = []

    def rename_key(container: dict, old: str, path: str) -> None:
        if old in container:
            if historic[old] in container:
                raise TransitionError(f"both {path}{old} and {path}{historic[old]} are present")
            container[historic[old]] = container.pop(old)
            renamed.append(f"{path}{old}")

    def rename_value(container: dict, name: str, old: str, path: str) -> None:
        if container.get(name) == old:
            container[name] = historic[old]
            renamed.append(f"{path}{name}={old}")

    rename_key(new, "dossier_evidence_id", "")
    if isinstance(new.get("budget"), dict):
        new["budget"] = dict(new["budget"])
        rename_key(new["budget"], "investigation_attempts", "budget.")
    for name in ("phase", "previous_phase"):
        rename_value(new, name, "investigation", "")
    if isinstance(new.get("pending_human_action"), dict):
        new["pending_human_action"] = dict(new["pending_human_action"])
        rename_value(new["pending_human_action"], "kind", "investigation_failed",
                     "pending_human_action.")
        rename_value(new["pending_human_action"], "counter", "investigation_attempts",
                     "pending_human_action.")
    work = new.get("active_work")
    if isinstance(work, dict) and (work.get("task") == "investigation"
                                   or str(work.get("dedupe_key")).endswith(":investigation")):
        raise TransitionError("A requested task of the historic task investigation has no "
                              "reviewed name migration")
    return new, renamed


# The `changed_files` of a patch proposal was checked against two different
# sets: the worker gate compared it with the paths of the whole series, the
# fork publisher with the net delta from `expected_head`, so no rewrite whose
# series touches a file already on the branch could pass both. Each side
# rejected such a proposal with one of these reasons, as `reject_patch_proposal`,
# which charged `no_progress`. One contract (the net delta) replaced both, and
# the record migration refunds the charges of these rejections.
CONTRADICTED_REJECTIONS = ("mbox paths do not match proposal changed_files",
                           "proposal claims files ")
# The publisher still rejects a claim that differs from the delta with the
# second reason, and under the one contract that is a genuine mistake of the
# worker, which `no_progress` must limit. Only a rejection recorded before this
# time is the contradiction's: the time is a few hours after the change that
# ended it ships, so that a worker run that began under the earlier code and is
# folded after the deploy is still covered. Never move it.
CONTRADICTION_CUTOFF = "2026-11-25T14:00:00+00:00"
REPAIR_PARK_PREFIX = "repair budgets exhausted"
# The budgets whose exhaustion parks a development record after the refresh.
REPAIR_PARK_BUDGETS = ("no_progress", "work_total", "repair_attempts_cluster")


def before_cutoff(at: Any) -> bool:
    """Whether an ISO time is before ``CONTRADICTION_CUTOFF``."""
    try:
        return datetime.fromisoformat(str(at)) < datetime.fromisoformat(CONTRADICTION_CUTOFF)
    except ValueError:
        return False


def contradicted_rejection(event: dict) -> bool:
    """Whether a committed event is a `reject_patch_proposal` recorded before
    ``CONTRADICTION_CUTOFF`` that charged `no_progress` for one of
    ``CONTRADICTED_REJECTIONS``."""
    details = event.get("details") if isinstance(event, dict) else None
    if not isinstance(details, dict) or event.get("kind") != "reject_patch_proposal":
        return False
    reason = details.get("reason")
    return isinstance(reason, str) and reason.startswith(CONTRADICTED_REJECTIONS) \
        and "no_progress" in (details.get("charged") or []) and before_cutoff(event.get("at"))


def _charged_no_progress(event: dict) -> bool:
    return "no_progress" in ((event.get("details") or {}).get("charged") or [])


def contradicted_refresh(events: list[dict], refunded_before: set[int], *, policy: dict
                         ) -> int | None:
    """The sequence of the latest `refresh_on_exhaustion` that the
    contradiction spent, or ``None``: it exhausted `no_progress` alone, no
    refund returned it before, and every one of the `no_progress` charges
    before it that filled the budget was a contradicted rejection."""
    refreshes = [e for e in events if e.get("kind") == "refresh_on_exhaustion"]
    if not refreshes:
        return None
    refresh = refreshes[-1]
    if refresh["sequence"] in refunded_before \
            or (refresh.get("details") or {}).get("exhausted") != ["no_progress"]:
        return None
    limit = lc.budget_limit("no_progress", policy)
    before = [e for e in events if e["sequence"] < refresh["sequence"] and _charged_no_progress(e)]
    filled = before[len(before) - limit:]
    if len(filled) < limit or not all(contradicted_rejection(e) for e in filled):
        return None
    return refresh["sequence"]


def refund_contradicted(record: dict, events: list[dict], refunded_before: set[int], *,
                        policy: dict, now: str) -> tuple[dict, dict] | None:
    """``record`` with the charges of the contradicted rejections among
    ``events`` (the events of the contribution, oldest first, and the
    sequences already refunded) returned, and what changed, or ``None`` when
    nothing is returned.

    A charge is returned only while it still counts: the count of
    `no_progress` is the number of its charges since it last started again,
    so only the contradicted rejections among the newest
    `budget.no_progress_streak` charges that were not refunded are refunded,
    and no count goes below zero. When the `no_progress` streak that the
    latest `refresh_on_exhaustion` started again was made of contradicted
    rejections alone, the automatic refresh it used is returned too
    (`automatic_refresh_used` is cleared, once: the sequence of that refresh
    is in ``refunded_before`` afterwards). A record parked
    for exhausted repair budgets after a development patch whose budgets all
    remain after the refund returns to `development.fix_needed` with no task
    (the next tick requests it), and keeps everything else; the invariants of
    that state are checked, and a record that would violate them stays
    parked."""
    budget = record.get("budget")
    if not isinstance(budget, dict):
        return None
    streak = int(budget.get("no_progress_streak", 0))
    charging = [e for e in events if e["sequence"] not in refunded_before
                and _charged_no_progress(e)]
    counted = {e["sequence"] for e in charging[len(charging) - streak:]} if streak else set()
    refunded = [e["sequence"] for e in events
                if e["sequence"] in counted and contradicted_rejection(e)]
    new = copy.deepcopy(record)
    returned = {}
    if refunded:
        new["budget"] = dict(new["budget"])
        before = int(new["budget"].get("no_progress_streak", 0))
        new["budget"]["no_progress_streak"] = max(0, before - len(refunded))
        returned["no_progress"] = before - new["budget"]["no_progress_streak"]
    refresh = contradicted_refresh(events, refunded_before, policy=policy) \
        if new.get("automatic_refresh_used") else None
    if refresh is not None:
        new["automatic_refresh_used"] = False
    if not returned.get("no_progress") and refresh is None:
        return None
    unparked = False
    reason = new.get("parked_reason")
    if (new.get("phase") == "parked" and new.get("previous_phase") == "development"
            and isinstance(reason, str) and reason.startswith(REPAIR_PARK_PREFIX)
            and not new.get("pending_publication") and not new.get("active_work")):
        ctx = Context(new, "development.fix_needed", policy, now)
        if all(remains(name, ctx) for name in REPAIR_PARK_BUDGETS):
            moved = dict(new, phase="development", activity="fix_needed",
                         parked_reason=None, previous_phase=None)
            if not lc.requirement_problems("development.fix_needed", moved) \
                    and state_problem(moved) is None:
                new, unparked = moved, True
    facts = {"returned": returned, "refunded": refunded, "unparked": unparked}
    if refresh is not None:
        facts["refresh_returned"] = True
        facts["refresh_event"] = refresh
    return new, facts


# --------------------------------------------------------------------------
# Writes beside a row
#
# A few fields are recorded beside the row that a tick takes, or without a
# row where the table has none (each listed on the engine page). Each such
# write is named here with the fields it may change, a dotted name admitting
# only that part of a field; it never changes the state, the flag, the
# revision, or the generation. The rows themselves record the decided
# dispatcher run (`retry_dispatch`) and end a held publication (every row
# that folds its Result).

BESIDE_ROW: dict[str, tuple[str, ...]] = {
    # The controller records the intent of a planned operation in the same
    # tick that launches it; the launch itself is a row of the table (or, for
    # a handoff attempt or a CI rerun, is counted on the operation). A closed
    # operation is released.
    "operation_planned": ("active_operation_id",),
    "operation_released": ("active_operation_id",),
    # The upstream identity and base, bound from trusted API data once.
    "upstream_identity_bound": ("upstream.repository_id", "upstream.base_ref",
                                "upstream.base_sha"),
    # A tick that read the upstream starts `source_reads` again: it counts
    # consecutive ticks only.
    "upstream_read": ("budget.source_unavailable", "budget.source_unavailable_since",
                      "budget.source_unavailable_at", "budget.source_unavailable_repository"),
    # A task held in a handoff state that requests none is dropped.
    "handoff_work_dropped": ("active_work",),
    # A published patch: its files join the admitted scope.
    "patch_published": ("scope_paths",),
    # A verified receipt of a `ci_rerun` operation the collector planned
    # releases it; no row is fired by it.
    "ci_rerun_recorded": ("active_operation_id",),
    # The publish-only retry of the held Result is launched.
    "publish_retry_launched": ("pending_publication",),
    # The automatic resume of a stop the controller set failed at this
    # version of the pipeline code: it is attempted once per version.
    "restop_attempted": ("paused_at_commit",),
}


def record_beside(record: dict, write: str, *, now: str, **fields: Any) -> dict:
    """A copy of ``record`` with ``fields`` set by the write ``write`` of
    ``BESIDE_ROW``. Raises ``TransitionError`` for a field, or a part of a
    field, that the write does not name."""
    allowed = BESIDE_ROW.get(write)
    if allowed is None:
        raise TransitionError(f"{write} is not a write beside a row")
    new = copy.deepcopy(record)
    for name, value in fields.items():
        whole = name in allowed
        parts = {a.split(".", 1)[1] for a in allowed if a.startswith(name + ".")}
        if not whole and not parts:
            raise TransitionError(f"{write} does not write {name}")
        if not whole:
            before, after = record.get(name) or {}, value or {}
            changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
            if not isinstance(value, dict) or changed - parts:
                raise TransitionError(f"{write} writes only {', '.join(sorted(parts))} of {name}")
        new[name] = copy.deepcopy(value)
    if any(new.get(k) != record.get(k) for k in STATE_FIELDS):
        raise TransitionError(f"{write} cannot change the state, flag, revision, or generation")
    if new != record:
        new["updated_at"] = now
    schemas.validate("contribution", new)
    return new


# --------------------------------------------------------------------------
# Handling of refusals
#
# Every call site outside the engine that takes a row declares here what it
# does with each kind of refusal, and acts on it in a ``match`` over
# ``dispatch``: ``Skip`` (the row is not taken now; the site reports why),
# ``Take`` (the site takes the named row of the same state instead),
# ``LateEffect`` (a Result that matches nothing: ``record_late_effect``),
# ``Reply`` (a command: the reply names the refusal) or ``Bug`` (the caller
# raises ``TransitionRefused``, a processing error of the contribution).
# ``tests/test_handling.py`` checks that every entry names all the kinds,
# that every ``Take`` names a row of the same state and, for a budget that
# is exhausted, its exhaustion row, and that every call site has its entry
# and matches every disposition its entry uses. A site is ``(module,
# function)``; an entry is keyed by the site and the verb (several verbs
# that ``try_apply_one_of`` takes together are joined by ``|``).


@dataclass(frozen=True)
class Skip:
    refused: Refused | Refusals


@dataclass(frozen=True)
class Take:
    verb: str
    refused: Refused | Refusals | None = None


@dataclass(frozen=True)
class LateEffect:
    refused: Refused | Refusals


@dataclass(frozen=True)
class Reply:
    refused: Refused | Refusals


@dataclass(frozen=True)
class Bug:
    refused: Refused | Refusals


Disposition = Skip | Take | LateEffect | Reply | Bug
# A declaration of ``HANDLING``: a disposition class, or ``Take`` of a verb.
SKIP, LATE, REPLY, BUG = Skip, LateEffect, Reply, Bug


def TAKE(verb: str) -> Take:  # noqa: N802 - a declaration, like the classes
    return Take(verb)


# The kind of a group of refusals (``Refusals``): the one they share, else
# the first of this order. The rows ``try_apply_one_of`` chooses among are
# exclusive by their guards, so all but the one that is due fail on the
# guard that tells them apart (``GUARD_UNMET``, ``FACT_MISSING``,
# ``NOT_APPLICABLE``); the refusal of the row that is due comes first.
GROUP_ORDER = (Refusal.PAUSED, Refusal.COMMAND_MISMATCH, Refusal.FENCED, Refusal.SOURCE_INVALID,
               Refusal.TARGET_INVALID, Refusal.BUDGET_EXHAUSTED, Refusal.FACT_MISSING,
               Refusal.GUARD_UNMET, Refusal.NOT_APPLICABLE)


def refusal_kind(refused: Refused | Refusals) -> Refusal:
    """The kind of a refusal, or of a group of refusals (``GROUP_ORDER``)."""
    if isinstance(refused, Refused):
        return refused.kind
    kinds = refused.kinds()
    return next(kind for kind in GROUP_ORDER if kind in kinds)


def _verb_key(refused: Refused | Refusals) -> str:
    if isinstance(refused, Refused):
        return refused.verb or ""
    return "|".join(r.verb or "" for r in refused.refused)


def dispatch(outcome: Applied | Refused | Refusals, site: tuple[str, str],
             verb: str | None = None) -> Applied | Disposition:
    """``outcome`` itself when it is ``Applied``, else the disposition that
    ``HANDLING`` declares for the site, the verb (``verb`` names a group of
    ``try_apply_one_of``, or a command's verbs) and the kind of refusal."""
    if isinstance(outcome, Applied):
        return outcome
    key = (site[0], site[1], verb or _verb_key(outcome))
    entry = HANDLING.get(key)
    if entry is None:
        raise TransitionError(f"no handling is declared for {key}")
    declared = entry[refusal_kind(outcome)]
    if isinstance(declared, Take):
        return Take(declared.verb, outcome)
    return declared(outcome)


def applied(outcome: Applied | Refused | Refusals, site: tuple[str, str],
            verb: str | None = None) -> Applied:
    """The applied row at a site whose every refusal is a ``Bug``
    (``tests/test_handling.py`` checks that only such sites call it)."""
    match dispatch(outcome, site, verb):
        case Applied() as done:
            return done
        case Bug(refused):
            raise TransitionRefused(refused, site)
        case other:
            raise TransitionError(f"{site}: {type(other).__name__} is not handled here")


# The declarations by the family of the trigger (the defaults of each site;
# a site states what differs).
_ALL_BUG = dict.fromkeys(Refusal, BUG)
D_TICK = {**_ALL_BUG, Refusal.PAUSED: SKIP, Refusal.FENCED: SKIP, Refusal.FACT_MISSING: SKIP,
          Refusal.GUARD_UNMET: SKIP}
D_OBS = {**_ALL_BUG, Refusal.NOT_APPLICABLE: SKIP, Refusal.PAUSED: SKIP, Refusal.FENCED: SKIP,
         Refusal.GUARD_UNMET: SKIP}
D_RESULT = {**_ALL_BUG, Refusal.NOT_APPLICABLE: LATE, Refusal.PAUSED: LATE,
            Refusal.COMMAND_MISMATCH: LATE}
D_OP = {**_ALL_BUG, Refusal.PAUSED: SKIP, Refusal.FENCED: SKIP}
D_COMMAND = dict.fromkeys(Refusal, REPLY)


def _handling(base: dict, **overrides) -> dict:
    entry = dict(base)
    for name, declared in overrides.items():
        entry[Refusal[name]] = declared
    return entry


HANDLING: dict[tuple[str, str, str], dict[Refusal, Any]] = {}


def handles(module: str, function: str, verbs, base: dict, **overrides) -> None:
    """Declare the handling of ``verbs`` (each, or a group joined by ``|``)
    at the site ``(module, function)``."""
    for verb in ([verbs] if isinstance(verbs, str) else verbs):
        key = (module, function, verb)
        if key in HANDLING:
            raise ValueError(f"handling of {key} is declared twice")
        HANDLING[key] = _handling(base, **overrides)


# The handling of every call site, by module. ``Bug`` is the default for a
# kind a site never expects (the observation or the fold established what
# the row needs).
_OBS_STRICT = _handling(D_OBS, GUARD_UNMET=BUG)

# engine
handles("engine", "record_late_effect", "record_late_effect", _ALL_BUG)

# observe_ci: observations. A row an observation does not take now is read
# again on the next tick (``Skip``).
handles("observe_ci", "_observe_clean_head_ci", "return_for_repair", _OBS_STRICT)
handles("observe_ci", "_observe_clean_head_ci", ("require_clean_head_ci", "accept_clean_head_ci"),
        D_OBS)
handles("observe_ci", "_observe_upstream_pr", ("verify_upstream_pr", "record_audit_observation"),
        _OBS_STRICT)
handles("observe_ci", "_record_explanation", "record_explanation", _ALL_BUG)
handles("observe_ci", "_record_review", "record_actionable_review", D_OBS, FACT_MISSING=SKIP)
# A head that moved in `development.fix_needed` without a development PR is
# not recorded: the controller opens its PR from there first, and the
# observation reads it again.
handles("observe_ci", "_observe_development", "record_head_change", D_OBS)
handles("observe_ci", "_observe_development", ("record_pr_mismatch", "record_ci_failure"),
        _OBS_STRICT)
handles("observe_ci", "_observe_development", ("return_for_feedback", "nominate_for_handoff"),
        D_OBS)
handles("observe_ci", "_rerun_interrupted", "rerun_interrupted_ci", D_OBS,
        BUDGET_EXHAUSTED=TAKE("exhaust_interrupted_reruns"))
handles("observe_ci", "_rerun_interrupted", "exhaust_interrupted_reruns", _ALL_BUG)
handles("observe_ci", "_observe_gate", ("record_head_change", "return_for_repair"), _OBS_STRICT)
handles("observe_ci", "_observe_upstream_review",
        ("record_merge", "record_pr_closed_unmerged", "record_head_change",
         "record_upstream_ci_failure"), _OBS_STRICT)
handles("observe_ci", "_observe_release", "record_release_incomplete", _ALL_BUG)
handles("observe_ci", "_observe_release", ("require_release_intervention", "verify_release"),
        _ALL_BUG, PAUSED=SKIP)

# reconcile: Results, receipts and the recovery of a task.
handles("reconcile", "dispatch_failure_recovery", "rearm_dispatch", D_TICK)
handles("reconcile", "_lost_dispatch", "retry_dispatch", D_TICK, FACT_MISSING=BUG,
        BUDGET_EXHAUSTED=TAKE("exhaust_recovery"))
handles("reconcile", "_lost_dispatch", "exhaust_recovery", _ALL_BUG)
handles("reconcile", "_reject_plan", "reject_plan", D_RESULT,
        BUDGET_EXHAUSTED=TAKE("exhaust_planning_recovery"))
handles("reconcile", "_reject_plan", "exhaust_planning_recovery", _ALL_BUG)
handles("reconcile", "fold_plan",
        ("close_planning", "park_planning", "readmit_development", "admit_development"), D_RESULT)
handles("reconcile", "fold_submission_rewrite",
        ("accept_submission_rewrite", "reject_submission_rewrite"), D_RESULT)
handles("reconcile", "_fail_publication", "schedule_publish_retry", _ALL_BUG,
        BUDGET_EXHAUSTED=TAKE("exhaust_recovery"))
handles("reconcile", "_fail_publication", "exhaust_recovery", _ALL_BUG)
# The first published patch is folded once its development PR is open
# (`development.waiting_ci` requires it): a Result refused for want of the
# PR is deferred in `development.agent_running`, and a bug elsewhere.
handles("reconcile", "fold_patch_result", "record_publish_success", D_RESULT,
        GUARD_UNMET=SKIP)
handles("reconcile", "fold_patch_result",
        ("reject_patch_proposal",
         "replan_development", "request_ci_rerun", "record_revision_publish_success",
         "require_reviewer_answer"), D_RESULT)
# The stop after a processing error is the last resort: a refusal (already
# paused or terminal) is reported, never raised.
handles("reconcile", "tick", "pause_on_processing_error", dict.fromkeys(Refusal, SKIP))
# The controller's resume of its own stop waits while the code is unchanged
# (`fact_missing`), an operation is open (`fenced`), or the stop is stale or
# not its own (`guard_unmet`).
handles("reconcile", "_resume_after_error", "resume_pause", D_TICK)
# A heal is not taken from a paused record (the first tick after the resume
# heals it), nor the stop from one that is already stopped or terminal.
handles("reconcile", "_heal_invariant", "record_pr_mismatch", D_OBS)
handles("reconcile", "_heal_invariant", "pause_on_processing_error", _ALL_BUG,
        PAUSED=SKIP, GUARD_UNMET=SKIP)
handles("reconcile", "fold_handoff_receipts", ("confirm_clean_head", "confirm_submission_package"),
        _ALL_BUG)
handles("reconcile", "fold_provisioning_receipts",
        ("confirm_provisioning", "require_provisioning_intervention"), _ALL_BUG)
handles("reconcile", "_tick", "confirm_development_pr", _ALL_BUG)

# handoff: a failed attempt fires the one row of its state.
handles("handoff", "fail_attempt",
        ("retry_upstream_branch|exhaust_upstream_branch_retries|return_for_repair"
         "|require_branch_intervention",
         "retry_submission_comment|exhaust_comment_retries|return_for_repair"), _ALL_BUG)

# controller: the tick, the commands, and the rows operations fire.
handles("controller", "_bind_source", "pause_on_identity_change", D_OBS)
handles("controller", "_follow_renamed_base", ("follow_renamed_base", "resume_renamed_base"),
        D_OBS, FACT_MISSING=SKIP)
handles("controller", "_resume_upstream_read", "resume_upstream_read", D_TICK)
handles("controller", "_replace_rename_wait", "require_base_branch", D_TICK)
handles("controller", "_source_unavailable",
        ("record_source_unavailable", "require_base_branch", "exhaust_source_reads",
         "require_upstream_access", "require_renamed_upstream"), D_TICK)
handles("controller", "tick", "withdraw_request", D_OBS, FACT_MISSING=SKIP)
handles("controller", "tick", "record_reminder", D_TICK)
handles("controller", "tick",
        ("exhaust_work_total", "exhaust_work_total|refresh_on_exhaustion|exhaust_repair_attempts",
         "exhaust_repair_attempts"), D_TICK, BUDGET_EXHAUSTED=SKIP)
handles("controller", "_due_allocation",
        ("dispatch_planning", "request_repair|request_patch",
         "request_submission_rewrite",
         "request_upstream_revision|request_review_revision"), D_TICK, BUDGET_EXHAUSTED=SKIP)
# A paused request does not begin planning.
handles("controller", "tick", "begin_planning", D_TICK)
handles("controller", "tick",
        ("dispatch_planning", "request_repair", "request_patch",
         "request_submission_rewrite",
         "request_upstream_revision", "request_review_revision"), _ALL_BUG)
# A record that breaks its state is left to the heal of its tick (nothing is
# decided for its task).
handles("controller", "terminal_work", "allocate_work_retry", D_TICK,
        BUDGET_EXHAUSTED=TAKE("exhaust_recovery"), SOURCE_INVALID=SKIP)
handles("controller", "terminal_work", "reallocate_after_pipeline_failure", D_TICK,
        SOURCE_INVALID=SKIP)
handles("controller", "terminal_work", "exhaust_recovery", _ALL_BUG)
handles("controller", "_launch_handoff", "expire_submission_rewrite", _ALL_BUG,
        PAUSED=SKIP, FENCED=SKIP)
handles("controller", "_launch_development_pr", "launch_development_pr", D_TICK,
        BUDGET_EXHAUSTED=TAKE("exhaust_development_pr_launches"))
handles("controller", "_launch_development_pr",
        ("exhaust_development_pr_launches", "record_pr_mismatch"), _ALL_BUG)
handles("controller", "_exhaust_development_pr_replans",
        ("exhaust_development_pr_launches", "record_pr_mismatch"), _ALL_BUG)
handles("controller", "_launch_provisioning", "start_operation", D_TICK,
        BUDGET_EXHAUSTED=TAKE("require_provisioning_intervention"))
handles("controller", "_launch_provisioning", "require_provisioning_intervention", _ALL_BUG)
handles("controller", "_exhaust_provisioning_replans", "require_provisioning_intervention",
        _ALL_BUG)
handles("controller", "_apply_table_command", "command", D_COMMAND)
# A resolution that does not establish the guards of the row its operation
# fires releases the operation without a row.
handles("controller", "_resolve_operation",
        ("confirm_provisioning", "require_provisioning_intervention", "confirm_development_pr",
         "confirm_clean_head",
         "retry_upstream_branch|exhaust_upstream_branch_retries|return_for_repair"
         "|require_branch_intervention",
         "confirm_submission_package",
         "retry_submission_comment|exhaust_comment_retries|return_for_repair"),
        D_OP, NOT_APPLICABLE=SKIP, FACT_MISSING=SKIP, GUARD_UNMET=SKIP)


# --------------------------------------------------------------------------
# Coverage of the table by the pipeline: ``python -m engine``.


def coverage() -> dict[str, Any]:
    """How much of the table the pipeline takes through the engine."""
    import recovery

    kinds = [p.kind for g, p in PREDICATES.items() if g in table_guards()]
    return {
        "table_rows": sum(1 for _ in lc.table_edges()),
        "table_verbs": len(table_verbs()),
        "pipeline_verbs": sorted(PIPELINE_VERBS),
        "unwired_verbs": sorted(UNWIRED_VERBS),
        "open_decisions": dict(sorted(OPEN_DECISIONS.items())),
        "fenced_fields": list(FENCED_FIELDS),
        "writes_beside_a_row": sorted(BESIDE_ROW),
        "guards": {"record": kinds.count("record"), "fact": kinds.count("fact")},
        "held_commands": sorted(HELD_COMMANDS),
        # The operation table: every verb is taken through ``recovery``.
        "operation_rows": sum(1 for _ in lc.table_edges(lc.OPERATIONS)),
        "operation_verbs": sorted(recovery.table_verbs()),
        "operation_guards": {kind: sum(1 for g, p in recovery.PREDICATES.items()
                                       if p.kind == kind and g in recovery.table_guards())
                             for kind in ("record", "fact")},
        "fired_lifecycle_verbs": sorted({v for fires in recovery.FIRES.values()
                                         for verbs in fires.values() for v in verbs}),
    }


def main() -> int:
    import json

    report = coverage()
    print(json.dumps(report, indent=2))
    print(f"{len(report['pipeline_verbs'])} of {report['table_verbs']} verbs are taken by the "
          f"pipeline; {len(report['unwired_verbs'])} wait on an open decision "
          f"({', '.join(report['unwired_verbs']) or 'none'})")
    print(f"operation table: {report['operation_rows']} rows, "
          f"{len(report['operation_verbs'])} verbs taken through recovery; every operation "
          "status change is a row of the table")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
