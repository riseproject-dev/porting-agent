# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The transition engine: the one write path of the contribution lifecycle.

``apply_transition(record, verb, ...)`` takes a contribution record and a verb
of the lifecycle table (``lifecycle.LIFECYCLE``, ``RULES`` and ``FLAGS``) and
returns the new record with the semantic event to persist. It does exactly
what the table row states and nothing else:

* it resolves the verb from the record's state (``phase.activity``), from a
  flag setter or from a flag clearer;
* it evaluates every guard of the row through the named predicate registered
  for it in ``PREDICATES``, and applies the mask of a set flag;
* it applies the row's ``Effects`` (fields set and cleared, the requested
  Work, the revision and generation), charges the row's budgets and starts
  again the one counter a resume names;
* it emits the event, whose kind is the verb.

A guard is either a *record* predicate, decided from the record, the policy,
the time and the command, or a *fact*: something only the caller can observe
(a CI verdict, a receipt, a free worker slot). The caller asserts a fact by
name; a fact that is not asserted does not hold, so a transition is refused
unless every guard is established. The engine never performs remote effects.

``PIPELINE_VERBS`` lists the verbs that pipeline code takes through this
engine: the handoff states and the moves into them, the stop flag and the
stop, resume, and refresh commands in every state of the table, the
reminders of a pending human step, the allocation of Work and the exhaustion
of its budgets, the dispatch and Work recovery, the Results of patch Work and
their publication, the observation of the development head, late Results,
and the rows of the fork provisioning and handoff operations (the operation
table itself has its engine in ``recovery``). Every other verb of the table is in
``UNWIRED_VERBS``: the engine applies it, but no pipeline code takes it yet
and the corresponding behavior still runs through ``compat``. The two sets
partition the table, so the share of the table the pipeline obeys is
measurable.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import ci_evidence
import lifecycle as lc
import schemas
from commands import Command
from common import UPSTREAM_REF

# The fields only this module (and, until its call sites are converted, the
# ``compat`` shim) may assign.
FENCED_FIELDS = ("phase", "activity", "paused", "revision", "generation")

TransitionError = lc.TransitionError


@dataclass
class Outcome:
    """The result of taking, or refusing, one transition."""

    record: dict[str, Any]
    accepted: bool
    reason: str | None
    event: dict[str, Any] | None
    verb: str | None = None
    # Budgets charged whose counter lives outside the record (an operation's
    # attempts, dispatcher runs); the caller persists them.
    charged: tuple[str, ...] = ()
    # The counter a resume started again: {"counter", "count"}.
    restarted: dict[str, Any] | None = None


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
    asserts its name."""

    name: str
    kind: str
    check: Callable[[Context], str | None]


PREDICATES: dict[str, Predicate] = {}


def _register(guard: str, name: str, kind: str, check: Callable[[Context], str | None]) -> None:
    if guard in PREDICATES:
        raise ValueError(f"guard {guard!r} is registered twice")
    if any(p.name == name for p in PREDICATES.values()):
        raise ValueError(f"predicate name {name!r} is registered twice")
    PREDICATES[guard] = Predicate(name, kind, check)


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


@_record("paused", "paused")
def _paused(ctx: Context) -> str | None:
    return None if ctx.record.get("paused") else "not paused"


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
    return "Work is requested" if ctx.record.get("active_work") else None


_record(lc.NO_WORK, "no_requested_work")(_no_work)
_record("no Work requested", "no_work_requested")(_no_work)


@_record("development PR and head recorded", "development_pr_recorded")
def _development_pr_recorded(ctx: Context) -> str | None:
    fork = ctx.record.get("fork") or {}
    if fork.get("pr_number") and fork.get("head_sha"):
        return None
    return "no development PR and head are recorded"


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


def business_days_between(start: datetime, end: datetime) -> int:
    """Whole business days (Monday to Friday) elapsed from ``start`` to ``end``."""
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
    """The pending feedback the requested Work carries: the ids present when
    it was allocated. A Result held for publication carries those of its Work."""
    for holder in (record.get("active_work"), record.get("pending_publication")):
        if holder and holder.get("feedback_evidence_ids"):
            return list(holder["feedback_evidence_ids"])
    return []


@_record("no pending actionable feedback", "no_pending_actionable_feedback")
def _no_pending_feedback(ctx: Context) -> str | None:
    pending = ctx.record.get("pending_feedback_evidence_ids") or []
    return f"actionable feedback is pending: {', '.join(pending[:4])}" if pending else None


@_record(lc.WORK_WITHOUT_FEEDBACK, "work_without_feedback")
def _work_without_feedback(ctx: Context) -> str | None:
    return "the Work carries feedback" if carried_feedback(ctx.record) else None


@_record(lc.REFRESH_UNUSED, "automatic_refresh_unused")
def _refresh_unused(ctx: Context) -> str | None:
    return "the automatic refresh is used" if ctx.record.get("automatic_refresh_used") else None


@_record(lc.REFRESH_USED, "automatic_refresh_used")
def _refresh_used(ctx: Context) -> str | None:
    return None if ctx.record.get("automatic_refresh_used") else "the automatic refresh is unused"


@_record("no dossier", "no_dossier")
def _no_dossier(ctx: Context) -> str | None:
    return "a dossier is recorded" if ctx.record.get("dossier_evidence_id") else None


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
    if name == "investigation_attempts":
        return int(budget.get("investigation_attempts", 0))
    if name == "repair_attempts_cluster":
        clusters = budget.get("clusters") or {}
        if ctx.cluster is not None:
            return int(clusters.get(ctx.cluster, 0))
        return max((int(v) for v in clusters.values()), default=0)
    if name == "work_recovery":
        return int((record.get("active_work") or {}).get("attempt", 1))
    if name == "publish_recovery":
        return int((record.get("pending_publication") or {}).get("attempts", 0))
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
RECORD_COUNTERS = ("work_total", "no_progress", "investigation_attempts",
                   "repair_attempts_cluster", "work_recovery", "publish_recovery",
                   *RERUN_COUNTERS)


def remains(name: str, ctx: Context) -> bool | None:
    count = budget_count(name, ctx)
    if count is None:
        return None
    return count < lc.budget_limit(name, ctx.policy)


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
_register("work_recovery or work_total exhausted", "work_recovery_or_work_total_exhausted",
          "record", _any_exhausted("work_recovery", "work_total"))
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


@_record("a prerequisite cannot be satisfied, an uncertain operation has no discovery, or "
         "operation_launch is exhausted", "provisioning_needs_intervention")
def _provisioning_needs_intervention(ctx: Context) -> str | None:
    if _asserted(ctx, "provisioning_blocked") or remains("operation_launch", ctx) is False:
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
    ("intake label removed", "intake_label_removed"),
    ("fork_provision operation planned", "fork_provision_planned"),
    ("the receipt matches the operation", "receipt_matches_operation"),
    ("exact fork and ref readback", "fork_and_ref_read_back"),
    ("the prerequisite is corrected", "prerequisite_corrected"),
    ("a dispatcher run ended with the requested Work absent or unclaimed", "dispatch_lost"),
    ("the Work graph is terminal without a usable Result", "work_terminal_without_result"),
    ("the Result has no dossier, the dossier is invalid, or its decision is not develop, park, "
     "or close", "dossier_rejected"),
    ("the Result has no usable dossier", "no_usable_dossier"),
    (lc._ADMISSION, "development_admitted"),
    (lc.PATCH_SCOPE_COMPLETE, "patch_scope_complete"),
    ("no refresh recorded, or the recorded fork or development PR no longer matches",
     "fresh_fork_required"),
    ("the recorded fork and development PR still match", "recorded_fork_matches"),
    ("verified dossier decision park", "dossier_decision_park"),
    ("verified dossier decision close", "dossier_decision_close"),
    ("the Result is published, or uncertain and later confirmed", "result_published"),
    ("the proposal no longer applies to the fork head", "proposal_stale"),
    ("the worker reports a failure not caused by the patch", "failure_not_caused_by_patch"),
    ("publication failed with an infrastructure error", "publication_infrastructure_error"),
    ("an executed blocking failure on the exact head", "ci_failed_on_head"),
    (lc._INTERRUPTED, "ci_interrupted_on_head"),
    ("authorized actionable review feedback", "actionable_review"),
    ("authorized explanation-only inquiry", "explanation_inquiry"),
    ("the observed fork head differs from the recorded head", "fork_head_changed"),
    ("the observed development PR does not match the recorded fork, ref, or PR",
     "development_pr_mismatch"),
    ("every required CI job executed and passed on the exact head", "ci_passed_on_head"),
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
    (lc.REWRITE_CURRENT, "submission_rewrite_current"),
    (lc.REVIEW_PASSED, "submission_review_passed"),
    (lc.REVIEW_FAILED, "submission_review_failed"),
    (lc.CLEAN_HEAD_CI_PASSED, "ci_passed_on_clean_head"),
    (lc.CLEAN_HEAD_CI_MISSING, "clean_head_ci_missing"),
    ("a blocking CI failure on the clean head, or the observed `riscv64` head differs from the "
     "clean head", "clean_head_needs_repair"),
    ("posting the package failed", "submission_comment_failed"),
    ("the package comment is read back", "submission_package_read_back"),
    ("an observation that does not satisfy verify_upstream_pr", "unverified_upstream_observation"),
    ("an open PR against the upstream repository and the frozen base", "upstream_pr_open_on_base"),
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
    ("the Result does not match the current generation, revision, and requested Work",
     "result_mismatch"),
    ("the observed upstream repository identity differs from the recorded one",
     "upstream_identity_changed"),
    (lc.PROCESSING_ERROR, "processing_error"),
):
    _fact(_guard, _name)

# ``upstream_pr_reopened`` is required only when the pending action is
# ``pr_closed_unmerged``; the guard text states the condition.
_register("for pr_closed_unmerged, the upstream PR is observed open again",
          "upstream_pr_reopened", "fact",
          lambda ctx: None
          if (ctx.record.get("pending_human_action") or {}).get("kind") != "pr_closed_unmerged"
          or _asserted(ctx, "upstream_pr_reopened")
          else "the upstream PR is not observed open again")

# Facts read by a record predicate in addition to the fact predicates above.
EXTRA_FACTS = frozenset({"upstream_pr_not_open", "provisioning_blocked"})
FACT_NAMES = frozenset(p.name for p in PREDICATES.values() if p.kind == "fact") | EXTRA_FACTS
# Facts that state the opposite of each other; a caller that asserts both has
# established neither.
CONTRADICTORY_FACTS = (("candidate_head_failed", "candidate_head_not_failed"),
                       ("submission_rewrite_passed", "submission_rewrite_failed"),
                       ("submission_rewrite_current", "submission_rewrite_expired"),
                       ("submission_review_passed", "submission_review_failed"))


def table_guards() -> set[str]:
    """Every guard text of the lifecycle table and its flags."""
    guards = {g for e in lc.table_edges() for g in e.transition.guard}
    for flag in lc.FLAGS.values():
        for t in (*flag.set_by.values(), *flag.cleared_by.values()):
            guards.update(t.guard)
    return guards


# --------------------------------------------------------------------------
# Verbs

# Commands the mask of the ``paused`` flag still admits.
PAUSED_COMMANDS = frozenset(f"/upstreaming {name}" for name in (
    "resume", "park", "refresh", "retract-pr", "abandon", "resolve-operation"))

# Verbs that pipeline code takes through ``apply_transition``, with the module
# that takes them. ``return_for_repair`` is taken by observation at the gate
# and in ``handoff.waiting_ci``, and fired by a build of `riscv64` whose
# reviewed series does not rebuild or whose lease conflicts.
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
    "record_pr_mismatch": "observe_ci",
    # The Result of patch Work: published, rejected, no longer applicable to
    # the fork head, or held for a publish-only retry within
    # `publish_recovery`, and the wait once that budget is exhausted.
    "record_publish_success": "reconcile",
    "reject_patch_proposal": "reconcile",
    "replan_development": "reconcile",
    "schedule_publish_retry": "reconcile",
    "exhaust_publish_recovery": "reconcile",
    # A dispatcher run that ended with the requested Work absent or
    # unclaimed, within `dispatch_recovery`.
    "retry_dispatch": "reconcile",
    "exhaust_dispatch_recovery": "reconcile",
    # A worker Result that matches no request of the record, in any state.
    "record_late_effect": "compat",
    # Work allocation, by the table's rows, and the exhaustion of its budgets
    # where Work is due.
    "begin_investigation": "controller",
    "dispatch_investigation": "controller",
    "request_patch": "controller",
    "request_repair": "controller",
    "request_upstream_revision": "controller",
    "request_review_revision": "controller",
    "exhaust_work_total": "controller",
    "refresh_on_exhaustion": "controller",
    "exhaust_repair_attempts": "controller",
    # A terminal Work graph without a usable Result, and an investigation
    # Result without a usable dossier.
    "allocate_work_retry": "reconcile",
    "exhaust_work_recovery": "reconcile",
    "reject_dossier": "reconcile",
    "exhaust_investigation_recovery": "reconcile",
    # The verified dossier decides: development on a new fork or on the
    # recorded one, a park, or a close.
    "admit_development": "reconcile",
    "readmit_development": "reconcile",
    "park_investigation": "reconcile",
    "close_investigation": "reconcile",
    # The submission command, the stop and resume commands, and refresh, on a
    # record in a state of the table, and the controller's own stops.
    "submit_upstream": "controller",
    "retract_submission": "controller",
    "abandon": "controller",
    "pause": "controller",
    "resume_pause": "controller",
    "resume_stale": "controller",
    "park_contribution": "controller",
    "refresh_investigation": "controller",
    "restart_generation": "controller",
    "pause_on_identity_change": "controller",
    # The resumes of the human waits of the table.
    "resume_investigation": "controller",
    "resume_provisioning": "controller",
    "resume_ci": "controller",
    "resume_development": "controller",
    "resume_submission_review": "controller",
    "resume_upstream_branch": "controller",
    "resume_clean_head_ci": "controller",
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
    # The fork provisioning operation: its launches within `operation_launch`,
    # the wait once that budget is exhausted or the operation failed, and its
    # confirmation by the receipt and readback (rows the operation fires
    # through ``recovery.fire``).
    "start_operation": "controller",
    "require_provisioning_intervention": "controller",
    "confirm_provisioning": "recovery",
    # The submission rewrite and review Work: their allocation and the
    # exhaustion of `work_total` where they are due, their Results, which the
    # trusted checks decide, and a rewrite whose artifact expired.
    "request_submission_rewrite": "controller",
    "accept_submission_rewrite": "reconcile",
    "reject_submission_rewrite": "reconcile",
    "expire_submission_rewrite": "controller",
    "request_submission_review": "controller",
    "accept_submission_review": "reconcile",
    "reject_submission_review": "reconcile",
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


# Verbs of the table that no pipeline code takes yet; their behavior still
# runs through ``compat``.
UNWIRED_VERBS: frozenset[str] = frozenset(table_verbs() - set(PIPELINE_VERBS))

# Commands the grammar and the engine accept but the controller holds: the
# controller checks such a command against the table, then refuses it with
# this reason and changes nothing. No command is held: every command of the
# table is applied as its row states.
HELD_COMMANDS: dict[str, str] = {}


def resolve(state: str, verb: str) -> tuple[lc.Transition, str]:
    """The transition ``verb`` from ``state`` and the state it leads to."""
    if state not in lc.LIFECYCLE:
        raise TransitionError(f"{state} is not a state of the lifecycle table")
    for flag in lc.FLAGS.values():
        if verb in flag.set_by:
            return flag.set_by[verb], flag.set_moves.get(state, state)
        if verb in flag.cleared_by:
            return flag.cleared_by[verb], state
    try:
        edge = lc.find_edge(state, verb)
    except KeyError:
        raise TransitionError(f"{verb} does not leave {state}") from None
    return edge.transition, edge.target


def _mask(ctx: Context, verb: str, t: lc.Transition) -> str | None:
    """What the ``paused`` flag forbids, by trigger."""
    if not ctx.record.get("paused"):
        return None
    if t.trigger == lc.Trigger.COMMAND and t.command not in PAUSED_COMMANDS:
        return "contribution is paused"
    if t.trigger == lc.Trigger.TICK:
        return "contribution is paused"
    if t.trigger == lc.Trigger.RESULT and verb != "record_late_effect":
        return "contribution is paused; the Result no longer matches"
    return None


def refusal(record: dict, verb: str, *, policy: dict, now: str, command: Command | None = None,
            facts=(), counts: dict[str, int] | None = None,
            open_operations: tuple[str, ...] | None = None,
            ci_evidence: dict | None = None) -> str | None:
    """Why ``verb`` cannot be taken now, or ``None`` when every guard holds."""
    state = state_key(record)
    t, _target = resolve(state, verb)
    ctx = Context(record, state, policy, now, command, frozenset(facts), dict(counts or {}),
                  open_operations, ci_evidence)
    unknown = set(facts) - FACT_NAMES
    if unknown:
        raise TransitionError(f"unknown facts: {', '.join(sorted(unknown))}")
    for pair in CONTRADICTORY_FACTS:
        if set(pair) <= set(facts):
            raise TransitionError(f"contradictory facts: {' and '.join(pair)}")
    masked = _mask(ctx, verb, t)
    if masked:
        return masked
    if not t.guard:
        raise TransitionError(f"{state} --{verb}-->: the row states no guard")
    for guard in t.guard:
        predicate = PREDICATES.get(guard)
        if predicate is None:
            raise TransitionError(f"guard {guard!r} has no predicate")
        why = predicate.check(ctx)
        if why:
            return why
    return None


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
    return False, None


# ``sets`` entries the engine always derives; the caller gives no value.
ENGINE_VALUES = (lc.WORK_FEEDBACK, lc.RERUNS)
# ``sets`` entries whose value the caller may omit.
OPTIONAL_VALUES = frozenset({"admission_override", "paused_reason", lc.PENDING_COUNTER})
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
    evidence, and replay fences are kept."""
    rec = copy.deepcopy(record)
    rec.update({
        "generation": int(record.get("generation", 0)) + 1, "refresh": None,
        "paused": False, "paused_at": None, "paused_reason": None, "paused_by": None,
        "blocked_reason": None, "parked_reason": None, "terminal_reason": None,
        "previous_phase": None,
        "owners": {"primary": None, "backup": None, "submitter": None,
                   "handoff_reviewer": None},
        "admission_class": None, "admission_override": None,
        "automatic_refresh_used": False,
        "upstream": {"repository_id": None, "full_name": record["upstream"]["full_name"],
                     "base_ref": None, "base_sha": None},
        "fork": None, "upstream_pr": None, "target_contract_id": None,
        "prerequisite_ids": [], "active_operation_id": None,
        "queue_work_id": None, "queue_claim_handle": None, "queue_result_id": None,
        "proposal_artifact": None, "publication_operation_id": None,
        "pending_publication": None,
        # processed_publications is kept as a replay fence against old run artifacts.
        "processed_review_ids": [], "processed_review_comment_ids": [],
        "processed_pr_conversation_comment_ids": [],
        "pending_feedback_evidence_ids": [], "active_work": None,
        "dossier_evidence_id": None, "scope_coverage_complete": False,
        "scope_paths": None, "debug_commits": [], "clean_head_sha": None,
        "clean_head_ci_since": None,
        "submission_rewrite_id": None,
        "submission_review_id": None,
        "handoff_request": None, "handoff_approval": None,
        "last_ci_evidence_id": None, "merge": None, "retirement": None,
        "release_acceptance": None, "pending_human_action": None,
        "next_poll_at": None,
    })
    rec["budget"] = {"policy_id": record["budget"]["policy_id"], "attempts_used": 0,
                     "no_progress_streak": 0, "clusters": {}, "investigation_attempts": 0,
                     "reruns": {"head_sha": None, "flaky": 0, "interrupted": 0},
                     "spent_estimate_usd_cents": 0}
    return rec


def _restart_counter(rec: dict, name: str) -> None:
    budget = rec["budget"] = dict(rec["budget"])
    if name == "work_total":
        budget["attempts_used"] = 0
    elif name == "no_progress":
        budget["no_progress_streak"] = 0
    elif name == "investigation_attempts":
        budget["investigation_attempts"] = 0
    elif name == "repair_attempts_cluster":
        budget["clusters"] = {}
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
    elif name == "investigation_attempts":
        budget["investigation_attempts"] = int(budget.get("investigation_attempts", 0)) + 1
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
            raise TransitionError(f"{verb} allocates Work; active_work is required")
        rec["active_work"] = copy.deepcopy(values["active_work"])
        rec["active_work"].pop("feedback_evidence_ids", None)
        if e.work == "reallocate" and carried:
            # A fresh graph for the same task carries the same feedback.
            rec["active_work"]["feedback_evidence_ids"] = carried

    pending_kind = None
    counter: Any = None
    counter_declared = False
    for entry in e.sets:
        name, literal, value = entry.partition("=")
        if name == "pending_human_action":
            pending_kind = value if literal else values.get(name)
            if not isinstance(pending_kind, str) or not pending_kind:
                raise TransitionError(f"{verb} sets pending_human_action; its kind is required")
            continue
        if name == lc.PENDING_COUNTER:
            counter_declared = True
            counter = value if literal else values.get(name)
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
        elif name in OPTIONAL_VALUES and verb not in lc.CALLER_REASONS:
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
    elif counter_declared:
        raise TransitionError(f"{verb} names a counter without a pending human action")
    if lc.RERUNS in e.sets:
        rec["budget"] = dict(rec["budget"], reruns=fresh_reruns(rec))
    if "pending_publication" in values and carried and rec.get("pending_publication"):
        # A Result held for publication carries the feedback of its Work.
        rec["pending_publication"] = dict(rec["pending_publication"],
                                          feedback_evidence_ids=carried)

    charged_elsewhere = tuple(name for name in t.charges if not _charge(rec, name, ctx))
    rec["phase"], rec["activity"] = target.split(".", 1)
    return rec, charged_elsewhere, restarted


_CAUSE = {lc.Trigger.COMMAND: "command", lc.Trigger.OBSERVATION: "observation",
          lc.Trigger.RESULT: "result", lc.Trigger.OPERATION: "operation",
          lc.Trigger.TICK: "tick"}


def apply_transition(record: dict, verb: str, *, policy: dict, now: str,
                     cause_id: str | None = None, command: Command | None = None,
                     facts=(), values: dict[str, Any] | None = None,
                     counts: dict[str, int] | None = None,
                     open_operations: tuple[str, ...] | None = None,
                     ci_evidence: dict | None = None, actor: dict | None = None,
                     details: dict[str, Any] | None = None) -> Outcome:
    """Take the table transition ``verb`` from the state ``record`` is in.

    ``facts`` names the observations the caller established; ``values`` gives
    the value of each field the row sets that the engine cannot derive;
    ``counts`` supplies the counts of budgets that live outside the record;
    ``ci_evidence`` is the evidence record ``last_ci_evidence_id`` names, from
    which the failing CI job of the head is decided.
    A guard that does not hold refuses the transition and leaves the record
    unchanged. A call the table does not allow (an unknown verb, a value for
    a field the row does not set, a missing value) raises ``TransitionError``.
    """
    state = state_key(record)
    t, target = resolve(state, verb)
    why = refusal(record, verb, policy=policy, now=now, command=command, facts=facts,
                  counts=counts, open_operations=open_operations, ci_evidence=ci_evidence)
    if why:
        return Outcome(record, False, why, None, verb)
    ctx = Context(record, state, policy, now, command, frozenset(facts), dict(counts or {}),
                  open_operations, ci_evidence)
    rec, charged, restarted = _apply_effects(ctx, verb, t, target, dict(values or {}))
    if rec != record:
        rec["updated_at"] = now
    schemas.validate("contribution", rec)
    bumped = rec["revision"] != record["revision"]
    if bumped != (t.effects.revision == "bump"):
        raise TransitionError(f"{verb}: the revision rule is violated")
    event = lc._event(record, rec, verb, _CAUSE[t.trigger], cause_id, now, bumped)
    info: dict[str, Any] = {"verb": verb, "from_state": state, "to_state": target}
    if t.charges:
        info["charged"] = list(t.charges)
    if restarted:
        info["restarted"] = restarted
    if actor:
        info["actor"] = {"id": actor["id"], "login": actor["login"]}
    info.update(details or {})
    event["details"] = info
    return Outcome(rec, True, None, event, verb, charged, restarted)


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


def resolve_command(record: dict, command: Command, *, policy: dict, now: str, facts=(),
                    counts: dict[str, int] | None = None,
                    open_operations: tuple[str, ...] | None = None) -> tuple[str | None, str | None]:
    """The one verb ``command`` takes now, or the reason it takes none."""
    verbs = command_verbs(record, command)
    state = state_key(record)
    if not verbs:
        return None, not_applicable(command.verb, state)
    reasons, admitted = [], []
    for verb in verbs:
        why = refusal(record, verb, policy=policy, now=now, command=command, facts=facts,
                      counts=counts, open_operations=open_operations)
        if why:
            reasons.append(why)
        else:
            admitted.append(verb)
    if len(admitted) > 1:
        raise TransitionError(f"{state}: {', '.join(admitted)} apply together")
    if admitted:
        return admitted[0], None
    return None, reasons[0] if len(set(reasons)) == 1 else "; ".join(dict.fromkeys(reasons))


def apply_command(record: dict, command: Command, *, policy: dict, now: str, command_id: str,
                  actor: dict | None = None, facts=(), values: dict[str, Any] | None = None,
                  counts: dict[str, int] | None = None,
                  open_operations: tuple[str, ...] | None = None) -> Outcome:
    """Take the transition an authenticated command names, per the table."""
    verb, why = resolve_command(record, command, policy=policy, now=now, facts=facts,
                                counts=counts, open_operations=open_operations)
    if verb is None:
        return Outcome(record, False, why, None)
    return apply_transition(record, verb, policy=policy, now=now, cause_id=command_id,
                            command=command, facts=facts, values=values, counts=counts,
                            open_operations=open_operations, actor=actor)


# --------------------------------------------------------------------------
# Record states


def state_problem(record: dict, legacy_states=frozenset()) -> str | None:
    """Why a record's state is not one the lifecycle admits, or ``None``.

    A record is in a state of the table, or in one of ``legacy_states``
    (states that ``compat`` paths still write).
    """
    key = state_key(record)
    state = lc.LIFECYCLE.get(key)
    if state is None:
        return None if key in legacy_states else f"{key} is not a lifecycle state"
    if record["phase"] == "handoff" and state.kind in ("gate", "locked", "human_wait"):
        # Every handoff wait is entered through the table, which names its
        # pending action.
        kind = (record.get("pending_human_action") or {}).get("kind")
        if kind not in lc.pending_kinds(key):
            return f"{key} is never entered with pending_human_action {kind}"
    return None


# --------------------------------------------------------------------------
# Coverage of the table by the pipeline: ``python -m engine``.


def coverage() -> dict[str, Any]:
    """How much of the table the pipeline takes through the engine."""
    import compat
    import recovery

    kinds = [p.kind for g, p in PREDICATES.items() if g in table_guards()]
    return {
        "table_rows": sum(1 for _ in lc.table_edges()),
        "table_verbs": len(table_verbs()),
        "pipeline_verbs": sorted(PIPELINE_VERBS),
        "unwired_verbs": sorted(UNWIRED_VERBS),
        "guards": {"record": kinds.count("record"), "fact": kinds.count("fact")},
        "held_commands": sorted(HELD_COMMANDS),
        "legacy_write_sites": sorted(compat.LEGACY_SITES),
        "legacy_states": sorted(compat.LEGACY_STATES),
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
          f"pipeline; {len(report['legacy_write_sites'])} legacy write sites remain")
    print(f"operation table: {report['operation_rows']} rows, "
          f"{len(report['operation_verbs'])} verbs taken through recovery; every operation "
          "status change is a row of the table")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
