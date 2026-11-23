# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Contribution lifecycle: the lifecycle table and the record it describes.

The lifecycle table at the end of this module (``LIFECYCLE``, ``RULES``,
``FLAGS``, ``BUDGETS`` and ``OPERATIONS``) is the definition of the contribution
state machine. Documentation and tests are derived from it, and
``engine.apply_transition`` is the write path that applies it to a record.

Nothing here performs remote effects or changes a record's state. The trusted
state publisher persists the engine's outcome; remote writers call
``publication_check`` immediately before an effect.

Revision rule: ``revision`` increments only when requested work changes (head
or base, approved scope/contract, or a human decision affecting work). Polls,
heartbeats and routine CI observations never increment it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NamedTuple

import schemas
from common import DEV_REF, UPSTREAM_REF

TERMINAL = frozenset({"released", "closed"})

# Phases in which automated contribution writes to the fork may happen.
WRITE_PHASES = frozenset({"provisioning", "development", "handoff", "upstream_review"})


class TransitionError(ValueError):
    pass


@dataclass
class Outcome:
    record: dict[str, Any]
    accepted: bool
    reason: str | None
    event: dict[str, Any] | None


def _event(before: dict, after: dict, kind: str, cause_type: str, cause_id: str | None,
           now: str, bumped: bool) -> dict:
    keys = ("phase", "activity", "paused", "revision", "generation", "parked_reason")
    return {
        "schema_version": 1,
        "contribution_id": after["contribution_id"],
        "sequence": 0,  # assigned by the state store
        "at": now,
        "kind": kind,
        "from": {k: before.get(k) for k in keys if before.get(k) != after.get(k)},
        "to": {k: after.get(k) for k in keys if before.get(k) != after.get(k)},
        "cause": {"type": cause_type, "id": cause_id},
        "revision_bumped": bumped,
    }


# --------------------------------------------------------------------------
# Publication guard


def work_ref(record: dict) -> str:
    """The fork branch an automated writer of the record's phase writes: the
    development branch ``riscv64-dev`` before the submission, and
    ``riscv64``, the branch of the upstream pull request, from it on. In the
    handoff phase only ``handoff.building_upstream_branch`` writes it."""
    return UPSTREAM_REF if record.get("phase") in ("handoff", "upstream_review") else DEV_REF


def recorded_head(record: dict) -> str | None:
    """The recorded head of the branch the record's phase works on
    (``work_ref``): in upstream review the head of the upstream PR from
    `riscv64` (``upstream_pr.head_sha``, which ``verify_upstream_pr``
    records and ``record_head_change`` there updates), elsewhere the head of
    the development branch (``fork.head_sha``)."""
    upstream = record.get("upstream_pr") or {}
    if record.get("phase") == "upstream_review" and upstream.get("head_sha"):
        return upstream["head_sha"]
    return (record.get("fork") or {}).get("head_sha")


def publication_check(record: dict, *, expected_revision: int, expected_head: str | None,
                      observed_head: str | None, ref: str, repository: str,
                      expected_generation: int = 0, work_key: str | None = None) -> str | None:
    """Re-read immediately before a fork write. Returns None when the write
    may proceed, otherwise the reason it must not. An effect that already
    passed this check may still finish after a later pause (best-effort
    pause, plan section 2); it is reconciled as a normal effect.

    The Result must match the Work identity: the generation and revision of
    the record, and the Work id of the requested Work (``work_key``), or a
    pending publication of a held Result. A payload written before the Work
    identity carried the Work id passes ``work_key=None`` and generation 0."""
    if record["phase"] not in WRITE_PHASES:
        return f"phase {record['phase']} does not permit fork writes"
    state = LIFECYCLE.get(f"{record['phase']}.{record['activity']}")
    if record["phase"] == "handoff" and not (state and state.writes):
        # Every handoff state is a table state; the table says which of them
        # an automated writer may write the fork in.
        return f"state {record['phase']}.{record['activity']} does not permit fork writes"
    if record.get("paused"):
        return "paused"
    expected_ref = work_ref(record)
    if ref != expected_ref:
        return f"ref {ref!r} is not {expected_ref!r}"
    fork = record.get("fork") or {}
    if expected_ref == DEV_REF and fork.get("head_ref") != DEV_REF:
        return f"the recorded development branch is not {DEV_REF!r}"
    if repository != fork.get("name"):
        return "repository is not the recorded contribution fork"
    if int(record.get("generation", 0)) != expected_generation:
        return "work generation changed"
    if record["revision"] != expected_revision:
        return "work revision changed"
    if work_key is not None:
        work = record.get("active_work")
        if work is not None and work.get("dedupe_key") != work_key:
            return "the requested Work is other Work"
        if work is None and not record.get("pending_publication"):
            return "no requested Work or pending publication"
    if observed_head != expected_head:
        return "fork head changed since the proposal was made"
    if record.get("active_operation_id"):
        return "another writer operation is active on this fork"
    return None


def new_contribution(*, contribution_id: str, project_id: str, issue: dict, upstream: str,
                     distributions: list[str], base_ref: str | None, policy_id: str,
                     now: str, follows: str | None = None) -> dict:
    rec = {
        "schema_version": 1,
        "contribution_id": contribution_id,
        "project_id": project_id,
        "revision": 0,
        "generation": 0,
        "refresh": None,
        "phase": "queued",
        "activity": "idle",
        "paused": False,
        "paused_at": None,
        "paused_reason": None,
        "paused_by": None,
        "parked_reason": None,
        "terminal_reason": None,
        "previous_phase": None,
        "tracking_issue": dict(issue),
        "owners": {"primary": None, "backup": None, "submitter": None, "handoff_reviewer": None},
        "admission_class": None,
        "admission_override": None,
        "automatic_refresh_used": False,
        "distributions": distributions,
        "upstream": {"repository_id": None, "full_name": upstream, "base_ref": base_ref,
                     "base_sha": None},
        "fork": None,
        "upstream_pr": None,
        "target_contract_id": None,
        "prerequisite_ids": [],
        "active_operation_id": None,
        "queue_work_id": None,
        "queue_claim_handle": None,
        "queue_result_id": None,
        "proposal_artifact": None,
        "publication_operation_id": None,
        "pending_publication": None,
        "processed_publications": [],
        "processed_review_ids": [],
        "processed_review_comment_ids": [],
        "processed_pr_conversation_comment_ids": [],
        "active_work": None,
        "plan_evidence_id": None,
        "pending_feedback_evidence_ids": [],
        "scope_coverage_complete": False,
        "scope_paths": None,
        "debug_commits": [],
        "clean_head_sha": None,
        "clean_head_ci_since": None,
        "submission_rewrite_id": None,
        "submission_review_id": None,
        "handoff_request": None,
        "handoff_approval": None,
        "last_ci_evidence_id": None,
        "merge": None,
        "retirement": None,
        "release_acceptance": None,
        "budget": {"policy_id": policy_id, "attempts_used": 0, "no_progress_streak": 0,
                   "clusters": {}, "planning_attempts": 0,
                   "reruns": {"head_sha": None, "flaky": 0, "interrupted": 0},
                   "spent_estimate_usd_cents": 0},
        "pending_human_action": None,
        "follows": follows,
        "next_poll_at": None,
        "created_at": now,
        "updated_at": now,
    }
    schemas.validate("contribution", rec)
    return rec


def write_admission(record: dict) -> str | None:
    """Gate before any destination write-capable work is submitted."""
    if not record.get("admission_class"):
        return "admission class not approved"
    if not record.get("fork") or not record["fork"].get("repository_id"):
        return "fork not provisioned/verified"
    if record["upstream"].get("base_sha") is None:
        return "no immutable upstream base SHA in the plan"
    return None


# --------------------------------------------------------------------------
# Names before the plan rename
#
# The planning phase and its plan were once named `investigation` and
# `dossier`. Committed events keep the names they were written with; the
# record migration of `state.py` (`state.migrate_record_names`) rewrites the
# stored records, projects, and evidence to the current names, and readers of
# the event history translate a historic name with `current_name`. Each
# entry maps a name that is no longer written to the name that replaced it.

HISTORIC_NAMES: dict[str, str] = {
    # The phase, its states, and the Work task.
    "investigation": "planning",
    "investigation.idle": "planning.idle",
    "investigation.agent_running": "planning.agent_running",
    "investigation.waiting_human": "planning.waiting_human",
    # The verbs of its rows.
    "begin_investigation": "begin_planning",
    "dispatch_investigation": "dispatch_planning",
    "reject_dossier": "reject_plan",
    "exhaust_investigation_recovery": "exhaust_planning_recovery",
    "park_investigation": "park_planning",
    "close_investigation": "close_planning",
    "resume_investigation": "resume_planning",
    "refresh_investigation": "refresh_planning",
    # Its budget, the pending human action, the evidence kind, and the field
    # that names the verified plan.
    "investigation_attempts": "planning_attempts",
    "investigation_failed": "planning_failed",
    "dossier": "plan",
    "dossier_evidence_id": "plan_evidence_id",
}


def current_name(name):
    """The current name of a lifecycle name a committed event may carry: the
    name that replaced a historic one, else ``name`` itself."""
    return HISTORIC_NAMES.get(name, name) if isinstance(name, str) else name


# --------------------------------------------------------------------------
# Lifecycle table
#
# This is the definition of the contribution state machine. ``LIFECYCLE``
# lists every state a contribution can be in, keyed ``phase.activity``, with
# its properties and, in order, the transitions that leave it. Each
# transition is keyed by its verb and the state it leads to, and records its
# trigger, guard, effects on the record and the retry budgets it charges.
# ``RULES`` are transitions that apply in many states; each is written once
# and expanded into ``LIFECYCLE`` when this module is imported, so the table
# holds every row. ``FLAGS`` are the orthogonal overrides (``paused``) that
# do not change phase or activity. ``BUDGETS`` names every retry budget: what
# one charge is, where the count lives, the ``policy.json`` key that sets its
# limit, and the exhaustion verbs that leave it. ``OPERATIONS`` is the table
# of one remote operation, held to the same checks.
#
# ``DIAGRAM_SECTIONS`` is presentation metadata used to render the table as
# diagrams; it selects and orders table entries but adds no transitions.
# ``check_table`` (``python -m lifecycle check``) verifies that the tables
# are consistent.


class Trigger(StrEnum):
    """What causes a transition to be taken."""

    COMMAND = "command"
    OBSERVATION = "observation"
    RESULT = "result"
    OPERATION = "operation"
    TICK = "tick"


TRIGGER_DESCRIPTIONS = {
    Trigger.COMMAND: "An authorized `/upstreaming` command. Commands that activate or approve work "
                     "name the exact current revision; stop commands may name a stale one.",
    Trigger.OBSERVATION: "A read-only observation by the collector: fork branch, CI runs, pull "
                         "requests, review comments, or releases.",
    Trigger.RESULT: "A verified native-queue Result folded by reconciliation. A Result is folded "
                    "only when it matches the current generation, revision, and requested Work; "
                    "any other Result takes `record_late_effect`. Each Result records its token "
                    "usage in the events.",
    Trigger.OPERATION: "A trusted remote operation receipt and its exact readback.",
    Trigger.TICK: "A controller decision without new input: Work allocation, a budget charge "
                  "or exhaustion, a deadline, or the stop after an unexpected error while one "
                  "contribution was processed.",
}

KIND_DESCRIPTIONS = {
    "auto_wait": "Waits for an automatic trigger; the controller acts without a human. A guard "
                 "that does not hold yet (a worker slot, capacity) keeps the contribution here; "
                 "there is no separate capacity wait.",
    "work": "Requested Work or a trusted operation is in progress.",
    "human_wait": "A recorded `pending_human_action` names what a human must do; "
                  "`/upstreaming resume` continues.",
    "gate": "Waits for a human command that starts the next step.",
    "locked": "Write-fenced wait: only the listed exits leave it, and no Work, patch, "
              "or branch write happens while here.",
    "generation_end": "The current generation has ended; the record is kept for audit and "
                      "late-effect reconciliation until a clean restart with "
                      "`/upstreaming refresh`.",
    "terminal": "No transition leaves this state.",
}

# The identity of requested Work. A Result names all three; a Result that
# differs in any of them is never folded and never published.
WORK_IDENTITY = ("generation", "revision", "Work id")


@dataclass(frozen=True)
class Effects:
    """Record effects of a transition.

    ``revision`` and ``generation`` are ``keep`` or ``bump``; a generation
    bump resets every mutable lifecycle field and keeps identity and replay
    fences. ``work`` is what happens to the requested Work (``active_work``):
    ``keep``, ``clear``, ``allocate`` (new Work for this state) or
    ``reallocate`` (a fresh Work graph for the same task). ``sets`` and
    ``clears`` name record fields; ``field=value`` names the value set. A
    clear in ``QUALIFIED_CLEARS`` clears only the part of a field it names.
    ``restarts`` names the budgets whose counter the transition may start
    again: it starts exactly the one named in ``pending_human_action.counter``,
    and none when no counter is named.
    """

    revision: str = "keep"
    generation: str = "keep"
    work: str = "keep"
    sets: tuple[str, ...] = ()
    clears: tuple[str, ...] = ()
    restarts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.revision not in ("keep", "bump") or self.generation not in ("keep", "bump"):
            raise ValueError("revision and generation are keep or bump")
        if self.work not in ("keep", "clear", "allocate", "reallocate"):
            raise ValueError(f"unknown work effect {self.work!r}")


@dataclass(frozen=True)
class Transition:
    """One transition, stored under the ``(verb, target)`` key of its state.

    ``command`` is the `/upstreaming` command that triggers it. ``guard``
    lists every condition that must hold; an unconditional transition states
    ``(ALWAYS,)``. ``charges`` names the ``BUDGETS`` entries one taking of
    this transition charges.
    """

    trigger: Trigger
    command: str | None = None
    guard: tuple[str, ...] = ()
    effects: Effects = field(default_factory=Effects)
    charges: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class State:
    """One state and the transitions that leave it.

    ``launches``: the controller may allocate Work or launch remote effects
    from this state (subject to the flags). ``writes``: automated writers may
    write the contribution fork while the contribution is in this state.
    ``transitions`` maps ``(verb, target)`` to the transition, in order;
    ``target`` is a state key, or ``SELF`` for a transition that keeps the
    state. A verb leads from a state to one target only, so the machine is
    deterministic; several verbs may lead to the same target.
    """

    kind: str
    launches: bool
    writes: bool
    description: str
    transitions: dict[tuple[str, str], Transition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KIND_DESCRIPTIONS:
            raise ValueError(f"unknown state kind {self.kind!r}")


@dataclass(frozen=True)
class Rule:
    """A transition that applies in many states, written once.

    The rule's verb leads to ``target`` (a state key, or ``SELF``) from every
    state whose kind is in ``kinds`` (every kind when empty) and whose key
    matches a pattern in ``states`` (every state when empty), except the
    states matching ``exclude`` that do not also match ``include``.
    ``applies`` says the same in words. ``expand_rules`` adds one row per
    matching state to ``LIFECYCLE``.
    """

    target: str
    transition: Transition
    applies: str
    kinds: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    include: tuple[str, ...] = ()


@dataclass(frozen=True)
class Flag:
    """An orthogonal override stored in ``field``.

    ``set_by`` and ``cleared_by`` map a verb to its transition; they apply
    from every non-terminal state and keep phase and activity. The one
    exception is ``set_moves``: setting the flag clears the requested Work,
    so a state that holds requested Work is left for the state in which that
    Work is due again. A setter that is not a command records its reason. ``mask`` says what each trigger may still do while
    the flag is set.
    """

    field: str
    description: str
    set_by: dict[str, Transition]
    cleared_by: dict[str, Transition]
    set_moves: dict[str, str]
    mask: dict[Trigger, str]


@dataclass(frozen=True)
class Budget:
    """A retry budget.

    The limit is the value at ``policy_key`` in ``policy.json`` plus
    ``offset`` (``offset=1`` for budgets whose first try is free). ``unit``
    is what one charge is, ``counter`` where the count lives. When the count
    reaches the limit, one of ``exhaustion_verbs`` is taken instead of the
    charging verb, from the charging state or the state the charging verb
    leads to; its guard names the budget. ``infrastructure`` marks a budget
    spent on infrastructure failures: its exhaustion always leads to a human
    wait of the same phase and never ends the generation. Every exhaustion
    that leads to a human wait names its counter in
    ``pending_human_action.counter``, and the resume from that wait starts
    exactly that counter again.
    """

    policy_key: str
    offset: int
    unit: str
    counter: str
    exhaustion_verbs: tuple[str, ...]
    infrastructure: bool = False


@dataclass(frozen=True)
class OperationState:
    """One state of a remote operation and the transitions that leave it.

    ``open``: the effect may still happen or is not yet known, so the
    operation fences its contribution (no park, refresh, or second writer).
    A state that is not open is an end state.
    """

    open: bool
    description: str
    transitions: dict[tuple[str, str], Transition] = field(default_factory=dict)


SELF = "@self"
ALWAYS = "always"  # the guard of an unconditional transition
_C, _O, _R, _P, _K = (Trigger.COMMAND, Trigger.OBSERVATION, Trigger.RESULT,
                 Trigger.OPERATION, Trigger.TICK)
_E = Effects

PARK = "/upstreaming park"
RESUME = "/upstreaming resume"
REFRESH = "/upstreaming refresh"
RESOLVE = "/upstreaming resolve-operation"

INITIAL_STATE = "queued.idle"
INITIAL_VERB = "accept_request"

# Guard conditions that the consistency check reads.
EXACT_REVISION = "exact revision"
ANY_REVISION = "any revision, current or stale"
NOT_PAUSED = "not paused"
QUIESCENT = "quiescent: no open operation and no pending publication"
NO_WORK = "no requested Work"
# Work names the verified upstream repository identity and base, bound from
# trusted API data (`upstream.repository_id`, `upstream.base_ref`,
# `upstream.base_sha`). Every row that allocates Work requires them; a
# restart clears them, and the tick that restarts binds them again.
UPSTREAM_BOUND = "upstream identity and base bound"
# The upstream repository and base branch, read by the collector in the
# states that need them (`BASE_BRANCH_WAITS`). A definitive answer about the
# branch (it does not exist, or GitHub redirects it to the branch it was
# renamed to) leads to the human wait at once; any other failed read is
# charged to `source_reads`, and its exhaustion leads to the same wait, with
# the pending action of the cause: the base branch, or the repository itself
# (renamed, transferred, or no longer readable with the token).
BASE_BRANCH_UNREAD = ("the upstream repository or its base branch could not be read, and the "
                      "upstream did not say that the branch is missing")
BASE_BRANCH_GONE = "the upstream says that the base branch does not exist or was renamed"
BASE_BRANCH_UNREADABLE = ("source_reads exhausted: reads of the upstream kept failing for the "
                          "whole retry window, most of them for the base branch")
UPSTREAM_GONE = ("GitHub now gives the upstream repository another name (renamed or "
                 "transferred), or source_reads exhausted with most failed reads for the "
                 "repository itself")
# The upstream PR the locked wait verifies targets the frozen base, or the
# name GitHub redirects it to after a rename, when that branch still
# contains the recorded base commit.
UPSTREAM_PR_ON_BASE = ("an open PR against the upstream repository and the frozen base, or the "
                       "name the upstream renamed it to when that branch contains the recorded "
                       "base commit")
# The automatic end of a wait that failed reads caused: reads succeed again.
UPSTREAM_READ_AGAIN = "the upstream repository and its base branch are read again"
READS_CAUSED_WAIT = ("the wait was entered because reads kept failing, not because the upstream "
                     "said that the branch is missing")
BASE_BRANCH_UNAVAILABLE = "base_branch_unavailable"
# `source_reads` is exhausted only once its count is reached and this many
# minutes have passed since the first failed read of the run: the controller
# ticks on issue, comment, and push events too, so a count of ticks alone can
# be spent in minutes.
SOURCE_READS_MINUTES_KEY = "retries.source_unavailable_minutes"
UPSTREAM_UNAVAILABLE = "upstream_unavailable"
# The upstream renamed the base branch while the upstream PR is open: GitHub
# retargets the PR to the new name. The record follows the rename once the
# verified PR targets the new name and the history of the new name contains
# the recorded base commit.
BASE_RENAMED_IN_PR = ("the upstream renamed the base branch, the open upstream PR now targets "
                      "the new name, and the new name's history contains the recorded base commit")
# A stop is stale when it lasted longer than ``refresh.stale_after_days``,
# counted from the older of ``paused_at`` and
# ``pending_human_action.requested_at``. ``resume_stale`` requires it; every
# other resume from the same states requires the complement, so exactly one
# of them applies.
STALE = "stopped for longer than `refresh.stale_after_days` days"
NOT_STALE = "stopped for at most `refresh.stale_after_days` days"
FLAG_NOT_STALE = ("stopped for at most `refresh.stale_after_days` days, or the phase is not "
                  "provisioning or development")
STALE_PHASES = ("provisioning", "development")
STALE_KEY = "refresh.stale_after_days"
REMINDER_KEY = "human_sla_business_days.reminder"
REMINDER_KINDS = ("gate", "human_wait", "locked")
INFRASTRUCTURE_KEY = "retries.infrastructure_per_head_failure"
EXACT_OPERATION = "the command names this operation"

# Guards on the recorded ``pending_human_action``, as (negated, kinds). The
# resumes of a human wait that are guarded by it are mutually exclusive, and
# together they cover every pending action with which the wait is entered.
PENDING_GUARDS: dict[str, tuple[bool, tuple[str, ...]]] = {}
# The field in which an exhaustion that leads to a human wait names the
# counter that the resume from that wait starts again.
PENDING_COUNTER = "pending_human_action.counter"


def _one_of(kinds: tuple[str, ...]) -> str:
    if len(kinds) < 3:
        return " or ".join(kinds)
    return ", ".join(kinds[:-1]) + f", or {kinds[-1]}"


def pending_is(*kinds: str) -> str:
    """The guard that the pending human action is one of ``kinds``."""
    guard = f"pending_human_action is {_one_of(kinds)}"
    PENDING_GUARDS[guard] = (False, kinds)
    return guard


def pending_is_not(*kinds: str) -> str:
    """The guard that the pending human action is none of ``kinds``."""
    guard = f"pending_human_action is not {_one_of(kinds)}"
    PENDING_GUARDS[guard] = (True, kinds)
    return guard


_REFRESH_EFFECTS = _E(
    revision="bump", sets=("refresh",),
    clears=("plan_evidence_id", "scope_coverage_complete", "scope_paths", "proposal_artifact",
            "last_ci_evidence_id", "clean_head_sha", "handoff_request", "handoff_approval",
            "pending_human_action", "publication_operation_id"))
_FLAG_FIELDS = ("paused", "paused_at", "paused_reason", "paused_by")
# The guard of the controller's stop after a processing error, and the stops
# set by the controller whose reason the caller states: the engine refuses
# such a stop without one. Every other controller stop names its reason in
# the table.
PROCESSING_ERROR = ("an unexpected error while the controller processed this contribution in "
                    "the tick")
CALLER_REASONS = ("pause_on_processing_error",)
# A stop clears the pending publication together with the requested Work: the
# Result held for publication is invalidated, and a branch its worker run
# already pushed is recorded by head observation. A stop is a transition that
# sets a flag or ends the generation.
HELD_RESULT = "pending_publication"
STOP_KINDS = ("generation_end", "terminal")
# Clears that take only a part of a field, as (field, the part cleared).
# A published Result clears exactly the feedback its Work carried.
CARRIED_FEEDBACK = "carried pending_feedback_evidence_ids"
QUALIFIED_CLEARS: dict[str, tuple[str, str]] = {
    CARRIED_FEEDBACK: (
        "pending_feedback_evidence_ids",
        "exactly the ids in `active_work.feedback_evidence_ids`, the pending feedback present "
        "when the Work was allocated; feedback recorded later stays pending"),
}
# What every stop clears beside the requested Work. `proposal_artifact` only
# ever holds a published Result, which a stop keeps.
_HELD = (HELD_RESULT,)
# The pending feedback a Work request carries: the ids present at allocation.
# A fresh Work graph for the same task and a publish retry of its Result carry
# the same ids.
WORK_FEEDBACK = "active_work.feedback_evidence_ids"
# Rerun counts of the current head. They start again whenever the recorded
# head changes: on every row of ``RERUN_RESTART_VERBS``.
RERUNS = "budget.reruns"
RERUN_RESTART_VERBS = ("record_publish_success", "record_head_change", "confirm_clean_head")

# Transitions shared by several states. The key under each state names the
# verb and the target.
_REFRESH = Transition(
    _C, REFRESH,
    (EXACT_REVISION, QUIESCENT, NO_WORK, "development PR and head recorded"),
    _REFRESH_EFFECTS,
    description="Runs complete planning again while keeping the fork and development PR. "
                "Accepted while paused.")
_RETRY_DISPATCH = Transition(
    _K, None,
    ("a dispatcher run ended with the requested Work absent or unclaimed", "dispatch_recovery remains"),
    charges=("dispatch_recovery",),
    description="Launches the dispatcher again for the same committed Work request.")
_EXHAUST_DISPATCH_RECOVERY = Transition(
    _K, None, ("dispatch_recovery exhausted",),
    _E(work="clear", sets=("pending_human_action=dispatch_failed",
                           f"{PENDING_COUNTER}=dispatch_recovery")))
# A dispatch refused because the installed work-queue Policy no longer
# matches the compiled one is not a failure of the Work: once the dispatcher
# reinstalls the compiled policy, the controller requests the Work again.
QUEUE_POLICY_REINSTALLED = ("the work-queue Policy was installed after the dispatch failed, and "
                            "the compiled policy preserves it")
_REARM_DISPATCH = Transition(
    _K, None,
    (pending_is("dispatch_failed"), QUEUE_POLICY_REINSTALLED, NOT_PAUSED, QUIESCENT),
    _E(revision="bump", clears=("pending_human_action",), restarts=("dispatch_recovery",)),
    description="Re-arms a dispatch that failed while the installed work-queue Policy refused "
                "every dispatch, once the dispatcher has reinstalled the compiled policy: the "
                "same transition as the resume a human would post, taken by the controller.")
# Guards that decide which transition applies when Work is due. EXCLUSIVE_GUARDS
# lists the combinations that cannot hold together.
WORK_TOTAL_REMAINS = "work_total remains"
WORK_TOTAL_EXHAUSTED = "work_total exhausted"
# ``repair_attempts_cluster`` counts repairs per failing CI job, so it is
# charged, and its exhaustion applies, only while a failing CI job is recorded
# for the head. Work that is due without one (the first patch, a rejected
# proposal, a repair driven only by review feedback) charges ``work_total``
# only.
FAILING_JOB = "a failing CI job is recorded for the head"
NO_FAILING_JOB = "no failing CI job is recorded for the head"
NO_PROGRESS_REMAINS = "no_progress remains"
REPAIR_BUDGETS_REMAIN = "repair_attempts_cluster and no_progress remain"
REPAIR_BUDGET_EXHAUSTED = ("no_progress is exhausted, or repair_attempts_cluster is exhausted "
                           "and a failing CI job is recorded for the head")
REFRESH_UNUSED = "the automatic refresh is unused in this generation"
REFRESH_USED = "the automatic refresh is used in this generation"
REVISION_BUDGETS_REMAIN = "repair_attempts_cluster, no_progress, and work_total remain"
REVIEW_REVISION_BUDGETS_REMAIN = "no_progress and work_total remain"
REVISION_BUDGET_EXHAUSTED = ("no_progress or work_total is exhausted, or repair_attempts_cluster "
                             "is exhausted and a failing CI job is recorded for the head")
# What leaves the submission gate for repair: a CI failure on the candidate
# head takes ``return_for_repair``; an actionable review takes
# ``record_actionable_review`` only when no such failure is observed. A review
# observed with the failure is recorded in the same observation by
# ``record_actionable_review`` in ``development.fix_needed``.
CANDIDATE_FAILED = "an executed blocking failure on the candidate head"
CANDIDATE_NOT_FAILED = "no executed blocking failure on the candidate head"
# The outcomes of the submission rewrite Work and of the submission review
# Work, each in a fresh context: the trusted deterministic check of the
# rewrite, then the model review of the rewritten series and text.
REWRITE_PASSED = ("the rewritten series has the tree of the approved series without its debug "
                  "commits, every commit passes the upstream-facing validation and the "
                  "attribution check, and the series and description satisfy the plan's "
                  "measurable rules")
REWRITE_FAILED = ("the rewrite reports the series cannot be shaped without a code change, the "
                  "rewritten series changes the tree, a commit fails the upstream-facing "
                  "validation or the attribution check, or a measurable rule is broken")
REVIEW_PASSED = ("the review of exactly the rewritten series and text passes, with every "
                 "expectation it lists met")
REVIEW_FAILED = "the review of exactly the rewritten series and text fails"
# The rewritten series is held in the worker run's publication artifact,
# which GitHub keeps 30 days.
REWRITE_EXPIRED = "the publication artifact of the rewritten series expired or expires within a day"
REWRITE_CURRENT = "the publication artifact of the rewritten series is current"
# CI on the clean head of `riscv64` is waited for this long before a human
# is asked: `riscv64` has no pull request, so the project's CI runs on it
# only when the trusted build dispatched its workflow or a push triggers it.
CLEAN_HEAD_CI_WAIT_HOURS = 12
CLEAN_HEAD_CI_MISSING = ("CI on the clean head is not complete, and either no run exists and its "
                         "workflow cannot be dispatched, or "
                         f"{CLEAN_HEAD_CI_WAIT_HOURS} hours have passed since the wait began")
CLEAN_HEAD_CI_PASSED = ("every required CI job executed and passed on the clean head, or on the "
                        "candidate head when the clean head has its tree")
# Work that carries feedback is never answered by a CI rerun.
WORK_WITHOUT_FEEDBACK = "the Work carries no feedback"
# Passing CI nominates a head only without pending feedback; with pending
# feedback it makes Work due again, so the feedback is never stranded.
NO_PENDING_FEEDBACK = "no pending actionable feedback"
PENDING_FEEDBACK = "pending actionable feedback"
EXCLUSIVE_GUARDS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    ((WORK_TOTAL_REMAINS,), (WORK_TOTAL_EXHAUSTED,)),
    ((NO_FAILING_JOB,), (FAILING_JOB,)),
    ((NO_FAILING_JOB, NO_PROGRESS_REMAINS), (REPAIR_BUDGET_EXHAUSTED,)),
    ((REPAIR_BUDGETS_REMAIN,), (REPAIR_BUDGET_EXHAUSTED,)),
    ((REFRESH_UNUSED,), (REFRESH_USED,)),
    ((REVISION_BUDGETS_REMAIN,), (REVISION_BUDGET_EXHAUSTED,)),
    ((NO_FAILING_JOB, REVIEW_REVISION_BUDGETS_REMAIN), (REVISION_BUDGET_EXHAUSTED,)),
    ((CANDIDATE_FAILED,), (CANDIDATE_NOT_FAILED,)),
    ((REWRITE_CURRENT,), (REWRITE_EXPIRED,)),
    ((NO_PENDING_FEEDBACK,), (PENDING_FEEDBACK,)),
)
# States in which two observations could apply to the same input; their
# guards are exclusive, so the order of observation never decides.
EXCLUSIVE_OBSERVATIONS = {
    "handoff.awaiting_submission": ("return_for_repair", "record_actionable_review"),
    "development.waiting_ci": ("nominate_for_handoff", "return_for_feedback"),
}

_ALLOCATE_WORK_RETRY = Transition(
    _K, None,
    ("the Work graph is terminal without a usable Result", "work_recovery and work_total remain",
     UPSTREAM_BOUND),
    _E(revision="bump", work="reallocate"), ("work_recovery", "work_total"),
    description="A terminal Work graph is never submitted again; a fresh graph is allocated. It "
                "carries the same feedback ids.")
_EXHAUST_WORK_RECOVERY = Transition(
    _K, None,
    ("the Work graph is terminal without a usable Result", "work_recovery or work_total exhausted"),
    _E(work="clear", sets=("pending_human_action=work_failed", PENDING_COUNTER)),
    description="`pending_human_action.counter` names `work_total` when that budget is "
                "exhausted, and `work_recovery` otherwise.")
_SCHEDULE_PUBLISH_RETRY = Transition(
    _R, None,
    ("publication failed with an infrastructure error", "publish_recovery remains"),
    _E(work="clear", sets=("pending_publication",)), ("publish_recovery",),
    "The same verified Result is published again; no new Work is requested. The pending "
    "publication carries the feedback ids of its Work. A publication whose push was not read "
    "back (`uncertain`) is such a failure; the retry reads the branch first and never pushes "
    "over a push that landed.")
_EXHAUST_PUBLISH_RECOVERY = Transition(
    _R, None,
    ("publish_recovery exhausted",),
    _E(sets=("pending_human_action=publication_infrastructure_failure",
            f"{PENDING_COUNTER}=publish_recovery", "pending_publication.exhausted")))
_RECORD_EXPLANATION = Transition(
    _O, None, ("authorized explanation-only inquiry",),
    _E(sets=("processed_review_ids", "processed_review_comment_ids",
            "processed_pr_conversation_comment_ids")),
    description="Answered on the development PR or recorded without requesting a change; "
                "revision and CI evidence are kept.")
_INTERRUPTED = "a required CI job on the exact head was cancelled or lost its runner"
_RERUN_INTERRUPTED_CI = Transition(
    _O, None, (_INTERRUPTED, "interrupted_reruns remains"),
    charges=("interrupted_reruns",),
    description="The collector reruns the interrupted jobs on the exact head. It reruns for no "
                "other conclusion. The collector records a `ci_rerun` operation that the "
                "trusted workflow executes; a rerun that fails is requested again by this row "
                "within the budget.")
_EXHAUST_INTERRUPTED_RERUNS = Transition(
    _O, None, (_INTERRUPTED, "interrupted_reruns exhausted"),
    _E(sets=("pending_human_action=ci_interrupted", f"{PENDING_COUNTER}=interrupted_reruns")))
_RESUME_CI = _E(revision="bump", clears=("pending_human_action",), restarts=("interrupted_reruns",))
_RESUME_CI_NOTE = ("Starts `interrupted_reruns` (`budget.reruns.interrupted`) again for the head; "
                   "the first observation after it reruns the interrupted jobs on the exact head "
                   "through `rerun_interrupted_ci`, charging the restarted counter.")
_LEAVE_HANDOFF = ("clean_head_sha", "clean_head_ci_since", "submission_rewrite_id",
                  "submission_review_id", "handoff_request", "pending_human_action")
# The effect of a return to development with no CI verdict: the recorded CI
# evidence is kept. A return for a CI failure also records its evidence.
_RETURN_FOR_REPAIR = _E(revision="bump", clears=_LEAVE_HANDOFF)
_PROCESSED_IDS = ("processed_review_ids", "processed_review_comment_ids",
                  "processed_pr_conversation_comment_ids")
_FEEDBACK_NOTE = (" The Work request carries the pending feedback ids present at allocation "
                  "(`active_work.feedback_evidence_ids`).")
_RESUME_EFFECTS = _E(revision="bump", clears=("pending_human_action",))
_RESTART_NOTE = ("When an exhausted budget caused the wait, the resume starts again the one "
                 "counter named in `pending_human_action.counter`, and no other. The event "
                 "records the actor, the counter, and its count at that point.")


def _resume_restarting(*budgets: str) -> Effects:
    """The effects of the resume of a human wait that one exhausted budget caused."""
    return _E(revision="bump", clears=("pending_human_action",), restarts=budgets)
DEVELOPMENT_PROPOSAL_REJECTED = (
    "the proposal is rejected for the current revision, the worker reports it is blocked, or the "
    "worker reports a failure not caused by the patch and either flaky_reruns is exhausted or "
    "the Work carries feedback")
_ADMISSION = ("verified plan decision develop, or verified plan decision park and a "
              "recorded admission_override")
PATCH_SCOPE_COMPLETE = ("the verified plan names `scope_paths`, and `scope_coverage_complete` "
                        "holds")
# The trusted confirmation of a decision close: the reconciler reads PyPI
# itself, since `closed.idle` is final and the plan is worker output. PyPI is
# the one registry it reads, so a project not released there is never closed
# by its plan.
CLOSE_CONFIRMED = ("PyPI serves riscv64 for the latest release of every distribution, and each "
                   "PyPI project names the upstream repository: a riscv64 wheel, or pure-Python "
                   "wheels only")
PLAN_REJECTED = ("the Result has no plan, the plan is invalid, its decision is not "
                    "develop, park, or close, or PyPI does not confirm a decision close")


LIFECYCLE: dict[str, State] = {
    "queued.idle": State(
        "auto_wait", True, False,
        "An accepted request is recorded; planning has not started.",
        transitions={
            ("begin_planning", "planning.idle"): Transition(_K, None, (NOT_PAUSED,)),
            ("withdraw_request", "closed.idle"): Transition(
                _O, None,
                ("intake label removed", "no Work requested", "no plan", "no fork"),
                _E(sets=("terminal_reason=withdrawn",))),
        }),
    "planning.idle": State(
        "auto_wait", True, False,
        "Planning is due; the controller allocates planning Work when a worker slot is free.",
        transitions={
            ("dispatch_planning", "planning.agent_running"): Transition(
                _K, None,
                ("worker slot free", WORK_TOTAL_REMAINS, NOT_PAUSED, "no open operation",
                 UPSTREAM_BOUND),
                _E(work="allocate"), ("work_total",)),
            ("exhaust_work_total", "planning.waiting_human"): Transition(
                _K, None, (WORK_TOTAL_EXHAUSTED,),
                _E(sets=("pending_human_action=work_budget_exhausted",
                        f"{PENDING_COUNTER}=work_total"))),
        }),
    "planning.agent_running": State(
        "work", False, False,
        "Planning Work is requested or running and will return a plan.",
        transitions={
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "planning.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "planning.waiting_human"): _EXHAUST_WORK_RECOVERY,
            ("reject_plan", "planning.idle"): Transition(
                _R, None,
                (PLAN_REJECTED, "planning_attempts remains"),
                _E(revision="bump", work="clear"), ("planning_attempts",),
                "Trusted validation rejects the Result; a new planning run is due."),
            ("exhaust_planning_recovery", "planning.waiting_human"): Transition(
                _R, None,
                ("the Result has no usable plan", "planning_attempts exhausted"),
                _E(work="clear", sets=("pending_human_action=planning_failed",
                                       f"{PENDING_COUNTER}=planning_attempts"))),
            ("admit_development", "provisioning.idle"): Transition(
                _R, None,
                (_ADMISSION, PATCH_SCOPE_COMPLETE,
                 "no refresh recorded, or the recorded fork or development PR no longer matches"),
                _E(revision="bump", work="clear",
                  sets=("plan_evidence_id", "admission_class", "scope_paths"),
                  clears=("refresh", "admission_override")),
                description="The scope of the patch is checked here, before a fork exists: "
                            "no patch Work is requested before `confirm_provisioning`."),
            ("readmit_development", "development.fix_needed"): Transition(
                _R, None,
                (_ADMISSION,
                 "refresh recorded", "the recorded fork and development PR still match"),
                _E(revision="bump", work="clear",
                  sets=("plan_evidence_id", "admission_class", "scope_paths"),
                  clears=("refresh", "admission_override")),
                description="A refreshed planning returns to the existing fork for repair."),
            ("park_planning", "parked.idle"): Transition(
                _R, None,
                ("verified plan decision park", "no admission_override recorded"),
                _E(work="clear", sets=("parked_reason", "previous_phase"), clears=_HELD)),
            ("close_planning", "closed.idle"): Transition(
                _R, None,
                ("verified plan decision close", CLOSE_CONFIRMED),
                _E(work="clear", sets=("terminal_reason",), clears=_HELD),
                description="The reconciler reads PyPI itself; `terminal_reason` is `already_released` "
                "when a riscv64 wheel is published, else `no_work`. A project that is not released "
                "on PyPI is never closed by its plan: it is parked with the evidence. A recorded "
                "`admission_override` is not applied against a decision close; the reply states so."),
        }),
    "planning.waiting_human": State(
        "human_wait", False, False,
        "Planning recovery or a budget is exhausted; a human corrects the cause and resumes.",
        transitions={
            ("rearm_dispatch", "planning.idle"): _REARM_DISPATCH,
            ("resume_planning", "planning.idle"): Transition(
                _C, RESUME,
                (EXACT_REVISION, QUIESCENT),
                _resume_restarting("work_total", "dispatch_recovery", "work_recovery",
                                   "planning_attempts", "source_reads"),
                description=_RESTART_NOTE),
        }),
    "provisioning.idle": State(
        "work", True, True,
        "The fork provisioning operation is planned or running through the trusted workflow.",
        transitions={
            ("start_operation", SELF): Transition(
                _K, None,
                ("fork_provision operation planned", "capacity available", "operation_launch remains",
                 NOT_PAUSED),
                charges=("operation_launch",),
                description="Launches the planned operation; an uncertain launch is never repeated "
                            "without discovery."),
            ("require_provisioning_intervention", "provisioning.waiting_human"): Transition(
                _P, None,
                ("a prerequisite cannot be satisfied, an uncertain operation has no discovery, "
                 "or operation_launch is exhausted",),
                _E(sets=("pending_human_action", PENDING_COUNTER)),
                description="`pending_human_action.counter` names `operation_launch` when that "
                            "budget is exhausted, and no counter otherwise."),
            ("confirm_provisioning", "development.fix_needed"): Transition(
                _P, None,
                ("the receipt matches the operation", "exact fork and ref readback"),
                _E(sets=("fork", "upstream.repository_id", "upstream.base_ref",
                        "upstream.base_sha"))),
        }),
    "provisioning.waiting_human": State(
        "human_wait", False, False,
        "Provisioning cannot continue safely; a human corrects the prerequisite and resumes, "
        "or parks.",
        transitions={
            ("resume_provisioning", "provisioning.idle"): Transition(
                _C, RESUME,
                (EXACT_REVISION, "the prerequisite is corrected", NOT_STALE),
                _resume_restarting("operation_launch", "source_reads"),
                description=_RESTART_NOTE),
        }),
    "development.agent_running": State(
        "work", False, True,
        "Patch Work is requested or running, or its verified Result is being published.",
        transitions={
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "development.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "development.waiting_human"): _EXHAUST_WORK_RECOVERY,
            ("record_publish_success", "development.waiting_ci"): Transition(
                _R, None,
                ("the Result is published, or uncertain and later confirmed",),
                _E(work="clear", sets=("proposal_artifact", "publication_operation_id", RERUNS),
                  clears=(CARRIED_FEEDBACK,)),
                description="The published patch answers exactly the feedback its Work "
                            "carried; feedback recorded while the Work ran stays pending. A "
                            "publication whose push was not read back (`uncertain`) does not "
                            "take this row when its Result is folded: it is a failed "
                            "publication (`schedule_publish_retry`), and the row is taken once "
                            "the publication is confirmed, by the publish-only retry reading "
                            "back the head the lost push left (or publishing the same commit), "
                            "or by the confirmed branch operation of an earlier attempt."),
            ("reject_patch_proposal", "development.fix_needed"): Transition(
                _R, None,
                (DEVELOPMENT_PROPOSAL_REJECTED, "no_progress remains"),
                _E(revision="bump", work="clear", clears=("clean_head_sha",)), ("no_progress",),
                "The pending feedback is kept."),
            ("replan_development", "development.fix_needed"): Transition(
                _R, None,
                ("the proposal no longer applies to the fork head",),
                _E(revision="bump", work="clear"),
                description="The pending feedback is kept."),
            ("request_ci_rerun", "development.waiting_ci"): Transition(
                _R, None,
                ("the worker reports a failure not caused by the patch", WORK_WITHOUT_FEEDBACK,
                 "flaky_reruns remains"),
                _E(work="clear", clears=("last_ci_evidence_id",)), ("flaky_reruns",),
                "The repair worker classifies the failure; the failed jobs are rerun on the exact "
                "head, and the head and revision are kept. A rerun answers no feedback, so Work "
                "that carries feedback never takes this row: the same outcome counts as "
                "`reject_patch_proposal`. The rerun is the publication of this Result: the "
                "trusted publication job requests it on the exact head and reads back the new "
                "attempt before the row is taken. A rerun that cannot be requested or read back "
                "is a failed publication (`schedule_publish_retry`, `exhaust_publish_recovery`)."),
            ("schedule_publish_retry", SELF): _SCHEDULE_PUBLISH_RETRY,
            ("exhaust_publish_recovery", "development.waiting_human"): _EXHAUST_PUBLISH_RECOVERY,
        }),
    "development.waiting_ci": State(
        "auto_wait", False, True,
        "A patch is published to the development PR; CI and review evaluate the exact head. "
        "Passing CI nominates the head, or returns to `development.fix_needed` while actionable "
        "feedback is pending; passing CI behind an open operation keeps the contribution here.",
        transitions={
            ("record_ci_failure", "development.fix_needed"): Transition(
                _O, None,
                ("an executed blocking failure on the exact head",),
                _E(sets=("last_ci_evidence_id",))),
            ("rerun_interrupted_ci", SELF): _RERUN_INTERRUPTED_CI,
            ("exhaust_interrupted_reruns", "development.waiting_human"): _EXHAUST_INTERRUPTED_RERUNS,
            ("record_actionable_review", "development.fix_needed"): Transition(
                _O, None,
                ("authorized actionable review feedback",),
                _E(revision="bump", sets=("pending_feedback_evidence_ids",))),
            ("record_explanation", SELF): _RECORD_EXPLANATION,
            ("record_head_change", SELF): Transition(
                _O, None,
                ("the observed fork head differs from the recorded head",),
                _E(revision="bump", sets=("fork.head_sha", RERUNS),
                  clears=("last_ci_evidence_id",))),
            ("record_pr_mismatch", "development.waiting_human"): Transition(
                _O, None,
                ("the observed development PR does not match the recorded fork, ref, or PR",),
                _E(sets=("pending_human_action=development_pr_mismatch",))),
            ("nominate_for_handoff", "handoff.awaiting_submission"): Transition(
                _O, None,
                ("every required CI job executed and passed on the exact head",
                 NO_PENDING_FEEDBACK, "no requested Work"),
                _E(sets=("last_ci_evidence_id", "handoff_request",
                        "pending_human_action=submit_upstream"))),
            ("return_for_feedback", "development.fix_needed"): Transition(
                _O, None,
                ("every required CI job executed and passed on the exact head", PENDING_FEEDBACK,
                 "no requested Work"),
                _E(sets=("last_ci_evidence_id",)),
                description="CI passed on the exact head while actionable feedback is still "
                            "pending (a push while Work was due for a review, or feedback the "
                            "published Work did not carry): the passing evidence is recorded "
                            "and Work is due again, so `request_patch` requests a repair that "
                            "carries the feedback. The pending feedback is kept; only a "
                            "published Result clears it."),
            ("refresh_planning", "queued.idle"): _REFRESH,
        }),
    "development.fix_needed": State(
        "auto_wait", True, True,
        "Development Work is due: the fork is ready for a first patch, or a CI failure, "
        "actionable review, passing CI with feedback still pending, rejected proposal, "
        "refreshed plan, or a resume needs a bounded repair. It is the only state in which patch Work is requested, so none is requested "
        "before `confirm_provisioning`.",
        transitions={
            ("request_patch", "development.agent_running"): Transition(
                _K, None,
                (NO_FAILING_JOB, "write admission", "worker slot free",
                 "capacity available", NO_PROGRESS_REMAINS, WORK_TOTAL_REMAINS, NOT_PAUSED,
                 UPSTREAM_BOUND),
                _E(work="allocate", sets=(WORK_FEEDBACK,)), ("work_total",),
                "Covers the first patch, the patch after a rejected proposal, and a repair "
                "driven only by review feedback." + _FEEDBACK_NOTE),
            ("request_repair", "development.agent_running"): Transition(
                _K, None,
                (FAILING_JOB, "write admission", "worker slot free", "capacity available",
                 REPAIR_BUDGETS_REMAIN, WORK_TOTAL_REMAINS, NOT_PAUSED, UPSTREAM_BOUND),
                _E(work="allocate", sets=(WORK_FEEDBACK,)),
                ("repair_attempts_cluster", "work_total"),
                "Charges `repair_attempts_cluster` for the failing CI job." + _FEEDBACK_NOTE),
            ("refresh_on_exhaustion", "queued.idle"): Transition(
                _K, None,
                (REPAIR_BUDGET_EXHAUSTED, WORK_TOTAL_REMAINS, REFRESH_UNUSED, NOT_PAUSED),
                _E(revision="bump", sets=("refresh", "automatic_refresh_used"),
                  clears=(*_REFRESH_EFFECTS.clears, "budget.clusters", "budget.no_progress_streak")),
                description="One automatic refresh per generation: complete planning runs "
                            "again on the existing fork. The per-job and no-progress counters "
                            "start again; `work_total` does not."),
            ("exhaust_repair_attempts", "parked.idle"): Transition(
                _K, None,
                (REPAIR_BUDGET_EXHAUSTED, WORK_TOTAL_REMAINS, REFRESH_USED),
                _E(work="clear", sets=("parked_reason", "previous_phase"), clears=_HELD),
                description="Parks for `repair_attempts_cluster` and `no_progress` only, after "
                            "`refresh_on_exhaustion` is used. Exhaustion of `work_total` takes "
                            "`exhaust_work_total` first."),
            ("exhaust_work_total", "development.waiting_human"): Transition(
                _K, None, (WORK_TOTAL_EXHAUSTED,),
                _E(sets=("pending_human_action=work_budget_exhausted",
                        f"{PENDING_COUNTER}=work_total")),
                description="The reply states the number of Work allocations spent in this "
                            "generation and names both `/upstreaming resume`, which starts "
                            "`work_total` again, and `/upstreaming park`."),
            ("record_head_change", "development.waiting_ci"): Transition(
                _O, None,
                ("the observed fork head differs from the recorded head",),
                _E(revision="bump", sets=("fork.head_sha", RERUNS),
                  clears=("last_ci_evidence_id",)),
                description="A push to the development branch while Work is due (a human "
                            "push): the new head is recorded and CI is awaited for it, so no "
                            "Work is requested for the head the record names any more. The "
                            "pending feedback is kept; when CI passes on the new head "
                            "`return_for_feedback` makes Work due again for it."),
            ("record_actionable_review", SELF): Transition(
                _O, None,
                ("authorized actionable review feedback",),
                _E(revision="bump", sets=("pending_feedback_evidence_ids",)),
                description="A review on the recorded head while Work is due, including one "
                            "observed together with the CI failure that left the submission "
                            "gate: it is recorded as pending feedback, so the Work requested "
                            "next carries it. The CI evidence is kept."),
            ("refresh_planning", "queued.idle"): _REFRESH,
        }),
    "development.waiting_human": State(
        "human_wait", False, False,
        "Development recovery or `work_total` is exhausted, the development PR does not match, "
        "or CI on the head was interrupted repeatedly; a human corrects the cause and resumes.",
        transitions={
            ("resume_ci", "development.waiting_ci"): Transition(
                _C, RESUME,
                (EXACT_REVISION, pending_is("ci_interrupted"), QUIESCENT, NOT_STALE),
                _RESUME_CI, description=_RESUME_CI_NOTE),
            ("rearm_dispatch", "development.fix_needed"): _REARM_DISPATCH,
            ("resume_development", "development.fix_needed"): Transition(
                _C, RESUME,
                (EXACT_REVISION, pending_is_not("ci_interrupted"), QUIESCENT, NOT_STALE),
                _E(revision="bump", clears=("pending_human_action", *_HELD),
                  restarts=("dispatch_recovery", "work_recovery", "work_total", "publish_recovery",
                            "source_reads")),
                description="Fresh Work is requested; a Result held for publication is not "
                            "reused. The pending feedback is kept. " + _RESTART_NOTE),
            ("refresh_planning", "queued.idle"): _REFRESH,
        }),
    "handoff.awaiting_submission": State(
        "gate", False, False,
        "Required CI passed on the exact head; a human may request upstream submission.",
        transitions={
            ("submit_upstream", "handoff.review_due"): Transition(
                _C, "/upstreaming submit-upstream",
                (EXACT_REVISION, "exact candidate head", NOT_PAUSED),
                _E(sets=("handoff_approval",), clears=("pending_human_action",)),
                description="The only submission command; it authorizes the submission review "
                            "and the whole deterministic handoff preparation. The development "
                            "branch `riscv64-dev` and the development PR are never changed for "
                            "the submission."),
            ("return_for_repair", "development.fix_needed"): Transition(
                _O, None,
                (CANDIDATE_FAILED,),
                _E(revision="bump", sets=("last_ci_evidence_id",), clears=_LEAVE_HANDOFF),
                description="Only a CI failure takes this row; the evidence of the failure "
                            "is recorded. An actionable review without a failure takes "
                            "`record_actionable_review` at the gate; one observed with the "
                            "failure is recorded by `record_actionable_review` in "
                            "`development.fix_needed`."),
            ("record_actionable_review", "development.fix_needed"): Transition(
                _O, None,
                ("authorized actionable review feedback", CANDIDATE_NOT_FAILED),
                _E(revision="bump", sets=("pending_feedback_evidence_ids",),
                  clears=_LEAVE_HANDOFF),
                description="The review is recorded as pending feedback, and the CI evidence "
                            "of the candidate head is kept. When CI also failed on the candidate "
                            "head, `return_for_repair` applies instead, and the review is "
                            "recorded in the same observation by `record_actionable_review` in "
                            "`development.fix_needed`."),
            ("record_explanation", SELF): Transition(
                _O, None, ("authorized explanation-only inquiry",),
                _E(sets=_PROCESSED_IDS),
                description="The comment is recorded and linked from the event log of the "
                            "website. No Work is requested and nothing is answered automatically; the "
                            "revision, the candidate head, and the CI evidence are kept."),
            ("record_head_change", "development.waiting_ci"): Transition(
                _O, None,
                ("the observed fork head differs from the candidate head",),
                _E(revision="bump", sets=("fork.head_sha", RERUNS),
                  clears=("last_ci_evidence_id", "handoff_request", "pending_human_action"))),
            ("refresh_planning", "queued.idle"): _REFRESH,
        }),
    "handoff.review_due": State(
        "auto_wait", True, False,
        "The submission rewrite is due; the controller allocates rewrite Work when a worker slot "
        "is free.",
        transitions={
            ("request_submission_rewrite", "handoff.agent_running"): Transition(
                _K, None,
                ("worker slot free", WORK_TOTAL_REMAINS, NOT_PAUSED, "no open operation",
                 UPSTREAM_BOUND),
                _E(work="allocate"), ("work_total",),
                "The uncredentialed rewrite Work reads the plan's contribution guidelines and "
                "the non-debug commits of the approved `riscv64-dev` series. It reshapes the "
                "commit messages (squashing or splitting only where the project's contributing "
                "instructions or reviewers require it, with the same final tree and no content "
                "the approved series does not have) and writes the upstream PR title and "
                "description after the project's pull request template and contributing "
                "instructions."),
            ("exhaust_work_total", "handoff.waiting_human"): Transition(
                _K, None, (WORK_TOTAL_EXHAUSTED,),
                _E(sets=("pending_human_action=work_budget_exhausted",
                        f"{PENDING_COUNTER}=work_total"))),
        }),
    "handoff.agent_running": State(
        "work", False, False,
        "Submission rewrite Work is requested or running; its Result is the rewritten series, "
        "title, and description, or the reasons the series cannot be shaped without a code "
        "change.",
        transitions={
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "handoff.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "handoff.waiting_human"): _EXHAUST_WORK_RECOVERY,
            ("accept_submission_rewrite", "handoff.review_pending"): Transition(
                _R, None,
                (REWRITE_PASSED,),
                _E(revision="bump", work="clear", sets=("submission_rewrite_id",)),
                description="The trusted deterministic check before the Result is folded "
                            "rebuilds the rewritten series on the frozen base and requires the "
                            "tree of the approved series without its debug commits, the "
                            "upstream-facing validation of every commit, attribution trailers "
                            "of the App or of the approved commits, the plan's measurable "
                            "commit rules, and every required section of the pull request "
                            "template in the description. Nothing is posted. The revision "
                            "is bumped: the review Work is new Work of the next revision."),
            ("reject_submission_rewrite", "development.fix_needed"): Transition(
                _R, None,
                (REWRITE_FAILED,),
                _E(revision="bump", work="clear", sets=("pending_feedback_evidence_ids",),
                  clears=_LEAVE_HANDOFF),
                description="The reasons are recorded as rewrite evidence and are the pending "
                            "feedback of the next development Work. Nothing is posted and "
                            "`riscv64` is not written."),
        }),
    "handoff.review_pending": State(
        "auto_wait", True, False,
        "The rewritten series passed the trusted check; its review is due, and the controller "
        "allocates review Work when a worker slot is free.",
        transitions={
            ("request_submission_review", "handoff.reviewing"): Transition(
                _K, None,
                ("worker slot free", WORK_TOTAL_REMAINS, NOT_PAUSED, "no open operation",
                 REWRITE_CURRENT, UPSTREAM_BOUND),
                _E(work="allocate"), ("work_total",),
                "The uncredentialed review Work starts in a fresh context and reads exactly the "
                "rewritten series and text (the trusted pre-agent step downloads them from the "
                "rewrite's publication artifact and checks their digests), the plan's "
                "contribution guidelines, the pull request template, the contributing "
                "instructions, and the commit conventions. It reviews them against the "
                "reviewers' expectations and returns pass or fail with reasons; it can change "
                "nothing."),
            ("exhaust_work_total", "handoff.waiting_human"): Transition(
                _K, None, (WORK_TOTAL_EXHAUSTED,),
                _E(sets=("pending_human_action=work_budget_exhausted",
                        f"{PENDING_COUNTER}=work_total"))),
            ("expire_submission_rewrite", "handoff.review_due"): Transition(
                _K, None, (REWRITE_EXPIRED, WORK_TOTAL_REMAINS),
                _E(clears=("submission_rewrite_id",)),
                description="A fresh rewrite is due; the expired rewrite is never reviewed."),
        }),
    "handoff.reviewing": State(
        "work", False, False,
        "Submission review Work is requested or running; its Result is pass or fail with "
        "reasons for exactly the rewritten series and text.",
        transitions={
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "handoff.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "handoff.waiting_human"): _EXHAUST_WORK_RECOVERY,
            ("accept_submission_review", "handoff.building_upstream_branch"): Transition(
                _R, None,
                (REVIEW_PASSED,),
                _E(work="clear", sets=("submission_review_id",)),
                description="The trusted check of the Result requires a review of exactly the "
                            "rewritten series and text (their digests); the accepted review "
                            "records the rewrite it passed. Nothing is posted."),
            ("reject_submission_review", "development.fix_needed"): Transition(
                _R, None,
                (REVIEW_FAILED,),
                _E(revision="bump", work="clear", sets=("pending_feedback_evidence_ids",),
                  clears=_LEAVE_HANDOFF),
                description="The reasons are recorded as review evidence and are the pending "
                            "feedback of the next development Work. Nothing is posted and "
                            "`riscv64` is not written. A Result that is not a usable review "
                            "takes `allocate_work_retry` or `exhaust_work_recovery` instead."),
        }),
    "handoff.building_upstream_branch": State(
        "work", False, True,
        "The upstream branch `riscv64` is created, or replaced under an exact-head lease, from "
        "the reviewed series, and read back. It is never written while an upstream PR is open "
        "from it, and `riscv64-dev` is not changed.",
        transitions={
            ("retry_upstream_branch", SELF): Transition(
                _P, None,
                ("the lease write or its readback failed with an infrastructure error",
                 "upstream_branch remains"),
                charges=("upstream_branch",)),
            ("confirm_clean_head", "handoff.waiting_ci"): Transition(
                _P, None,
                ("the lease write and ref readback match the reviewed series",),
                _E(sets=("clean_head_sha", "clean_head_ci_since", RERUNS)),
                description="When the clean head has another tree than the candidate head, the "
                            "same trusted attempt dispatches the project's required CI workflow "
                            "on `riscv64` if it can be dispatched, and reads its run back by "
                            "head; the outcome is recorded on the operation."),
            ("return_for_repair", "development.fix_needed"): Transition(
                _P, None,
                ("the reviewed series does not rebuild to the reviewed head, or the lease on "
                 "`riscv64` conflicts",),
                _RETURN_FOR_REPAIR),
            ("require_branch_intervention", "handoff.waiting_human"): Transition(
                _P, None,
                (("a pull request is open from `riscv64` (upstream or in the fork), `riscv64-dev` is "
                 "missing, or the record's development branch is not `riscv64-dev`"),),
                _E(sets=("pending_human_action=upstream_branch_blocked",)),
                description="`riscv64` is left as it is and the reason is recorded: a human "
                            "closes the pull request, restores `riscv64-dev`, or decides "
                            "otherwise, then resumes."),
            ("expire_submission_rewrite", "handoff.review_due"): Transition(
                _K, None, (REWRITE_EXPIRED, "no open operation"),
                _E(clears=("submission_rewrite_id", "submission_review_id")),
                description="The series `riscv64` would be built from is no longer available: a "
                            "fresh rewrite and review are due, also after a long human wait "
                            "that resumed to this step."),
            ("exhaust_upstream_branch_retries", "handoff.waiting_human"): Transition(
                _P, None,
                ("upstream_branch exhausted",),
                _E(sets=("pending_human_action=upstream_branch_failed",
                        f"{PENDING_COUNTER}=upstream_branch"))),
        }),
    "handoff.waiting_ci": State(
        "auto_wait", False, False,
        "Required CI runs on the clean head of `riscv64`.",
        transitions={
            ("accept_clean_head_ci", "handoff.preparing_submission"): Transition(
                _O, None,
                (CLEAN_HEAD_CI_PASSED,),
                _E(sets=("last_ci_evidence_id",)),
                description="CI is a function of the tree: when the reviewed series has the "
                            "tree of the candidate head (the approved series had no debug "
                            "commit), the CI evidence of the candidate head is CI on the clean "
                            "head."),
            ("rerun_interrupted_ci", SELF): Transition(
                _RERUN_INTERRUPTED_CI.trigger, _RERUN_INTERRUPTED_CI.command,
                _RERUN_INTERRUPTED_CI.guard, _E(sets=("clean_head_ci_since",)),
                charges=_RERUN_INTERRUPTED_CI.charges,
                description=_RERUN_INTERRUPTED_CI.description + " The bounded wait for CI on "
                            "the clean head starts again, since CI was started again."),
            ("exhaust_interrupted_reruns", "handoff.waiting_human"): _EXHAUST_INTERRUPTED_RERUNS,
            ("return_for_repair", "development.fix_needed"): Transition(
                _O, None,
                ("a blocking CI failure on the clean head, or the observed `riscv64` head "
                 "differs from the clean head",),
                _E(revision="bump", sets=("last_ci_evidence_id",), clears=_LEAVE_HANDOFF),
                description="A blocking failure records the failing evidence of the clean head "
                            "as `last_ci_evidence_id`, so the repair requested in "
                            "`development.fix_needed` names the failing job and charges its "
                            "cluster. A `riscv64` head that is not the clean head has no CI "
                            "verdict: the recorded evidence of the candidate head is kept."),
            ("require_clean_head_ci", "handoff.waiting_human"): Transition(
                _O, None,
                (CLEAN_HEAD_CI_MISSING,),
                _E(sets=("pending_human_action=clean_head_ci_missing",)),
                description="The pending action names the missing CI on the clean head; a "
                            "human runs it (or makes it runnable) and resumes, which observes "
                            "CI on the clean head again."),
        }),
    "handoff.preparing_submission": State(
        "work", False, False,
        "The submission package (title, body, prefilled compare link) is posted to the tracking issue.",
        transitions={
            ("retry_submission_comment", SELF): Transition(
                _P, None,
                ("posting the package failed", "submission_comment remains"),
                charges=("submission_comment",)),
            ("confirm_submission_package", "handoff.waiting_upstream_pr"): Transition(
                _P, None,
                ("the package comment is read back",),
                _E(sets=("pending_human_action=open_upstream_pr", "handoff_request"))),
            ("exhaust_comment_retries", "handoff.waiting_human"): Transition(
                _P, None,
                ("submission_comment exhausted",),
                _E(sets=("pending_human_action=submission_comment_failed",
                        f"{PENDING_COUNTER}=submission_comment"))),
        }),
    "handoff.waiting_human": State(
        "human_wait", False, False,
        "A handoff step failed repeatedly or needs a human: the submission rewrite or review Work, "
        "writing `riscv64` (also while an upstream PR is open from it), CI on the clean head, "
        "or posting the submission package. A human corrects it and resumes; the resume returns "
        "to the step named by `pending_human_action` and starts again the counter named in "
        "`pending_human_action.counter`.",
        transitions={
            ("rearm_dispatch", "handoff.review_due"): _REARM_DISPATCH,
            ("resume_submission_review", "handoff.review_due"): Transition(
                _C, RESUME,
                (EXACT_REVISION,
                 pending_is("work_budget_exhausted", "dispatch_failed", "work_failed",
                            BASE_BRANCH_UNAVAILABLE, UPSTREAM_UNAVAILABLE)),
                _resume_restarting("work_total", "dispatch_recovery", "work_recovery",
                                   "source_reads"),
                description="Fresh rewrite Work is requested, and its review after it. "
                            + _RESTART_NOTE),
            ("resume_upstream_branch", "handoff.building_upstream_branch"): Transition(
                _C, RESUME,
                (EXACT_REVISION, pending_is("upstream_branch_failed", "upstream_branch_blocked")),
                _resume_restarting("upstream_branch")),
            ("resume_ci", "handoff.waiting_ci"): Transition(
                _C, RESUME, (EXACT_REVISION, pending_is("ci_interrupted")),
                _E(revision="bump", sets=("clean_head_ci_since",),
                   clears=("pending_human_action",), restarts=("interrupted_reruns",)),
                description=_RESUME_CI_NOTE[:-1] + ", and the bounded wait for CI on the clean "
                            "head starts again."),
            ("resume_clean_head_ci", "handoff.waiting_ci"): Transition(
                _C, RESUME, (EXACT_REVISION, pending_is("clean_head_ci_missing")),
                _E(revision="bump", sets=("clean_head_ci_since",),
                   clears=("pending_human_action",)),
                description="CI on the clean head is observed again, and its bounded wait "
                            "starts again."),
            ("resume_submission_preparation", "handoff.preparing_submission"): Transition(
                _C, RESUME, (EXACT_REVISION, pending_is("submission_comment_failed")),
                _resume_restarting("submission_comment")),
        }),
    "handoff.waiting_upstream_pr": State(
        "locked", False, False,
        "Waiting for a human to open the upstream PR from the package. Exactly three exits leave "
        "it: `verify_upstream_pr`, `retract_submission`, and `refresh_planning`.",
        transitions={
            ("record_audit_observation", SELF): Transition(
                _O, None,
                ("an observation that does not satisfy verify_upstream_pr",),
                description="Recorded for audit only; it never changes phase or activity."),
            ("verify_upstream_pr", "upstream_review.waiting_ci"): Transition(
                _O, None,
                (UPSTREAM_PR_ON_BASE,
                 "its head is `riscv64` of the contribution fork at the exact clean head",
                 "a compatible human author"),
                _E(sets=("upstream_pr", "upstream.base_ref"), clears=("pending_human_action",)),
                description="`upstream.base_ref` is kept, unless the upstream renamed the frozen "
                            "base before the PR was opened: GitHub redirects the recorded name "
                            "to the PR's base and that branch still contains "
                            "`upstream.base_sha`, and then the record follows the new name, as "
                            "`follow_renamed_base` does."),
            ("retract_submission", "development.fix_needed"): Transition(
                _C, "/upstreaming retract-pr",
                (EXACT_REVISION, "exact clean head", QUIESCENT),
                _E(revision="bump", clears=("pending_human_action", "handoff_request", "handoff_approval")),
                description="Stops reminders and unlocks branch work. `riscv64` is left as it "
                            "is; the next `submit_upstream` replaces it."),
            ("refresh_planning", "queued.idle"): Transition(
                _C, REFRESH,
                (EXACT_REVISION, "exact clean head", QUIESCENT),
                _REFRESH_EFFECTS,
                description="Returns to complete planning while keeping the fork. "
                            "Accepted while paused."),
        }),
    "upstream_review.waiting_ci": State(
        "auto_wait", False, False,
        "The upstream PR is open; upstream CI, review, and merge are observed. Upstream CI is "
        "never rerun by the pipeline.",
        transitions={
            ("record_upstream_ci_failure", "upstream_review.fix_needed"): Transition(
                _O, None,
                ("an executed blocking failure on the upstream PR head",
                 "not yet recorded for this head and CI run"),
                _E(sets=("last_ci_evidence_id",)),
                description="Fires once per head and CI run, so a failure that was already recorded does not "
                "request a revision again after a resume."),
            ("record_actionable_review", "upstream_review.fix_needed"): Transition(
                _O, None,
                ("authorized actionable review feedback",),
                _E(revision="bump", sets=("pending_feedback_evidence_ids",))),
            ("record_explanation", SELF): Transition(
                _O, None, ("authorized explanation-only inquiry",),
                _E(sets=("processed_review_ids", "processed_review_comment_ids",
                        "processed_pr_conversation_comment_ids")),
                description="Recorded and linked from the event log of the website; "
                            "nothing is posted on the upstream PR. Revision and CI evidence are kept."),
            ("record_head_change", "upstream_review.waiting_human"): Transition(
                _O, None,
                ("the upstream PR head changed and the pipeline did not publish the change",),
                _E(revision="bump",
                  sets=("upstream_pr.head_sha", "pending_human_action=upstream_head_changed",
                        RERUNS),
                  clears=("last_ci_evidence_id",))),
            ("record_pr_closed_unmerged", "upstream_review.waiting_human"): Transition(
                _O, None,
                ("the upstream PR is closed without merge",),
                _E(sets=("pending_human_action=pr_closed_unmerged",))),
            ("record_merge", "release_wait.idle"): Transition(
                _O, None,
                ("the upstream PR is merged",),
                _E(sets=("merge",), clears=("active_operation_id",))),
        }),
    "upstream_review.fix_needed": State(
        "auto_wait", True, True,
        "Upstream CI or actionable review needs a revision of the upstream PR head.",
        transitions={
            ("request_upstream_revision", "upstream_review.agent_running"): Transition(
                _K, None,
                ("the upstream PR head is the controlled fork ref", FAILING_JOB,
                 "worker slot free", REVISION_BUDGETS_REMAIN, NOT_PAUSED, UPSTREAM_BOUND),
                _E(work="allocate", sets=(WORK_FEEDBACK,)),
                ("repair_attempts_cluster", "work_total"),
                "Charges `repair_attempts_cluster` for the failing CI job." + _FEEDBACK_NOTE),
            ("request_review_revision", "upstream_review.agent_running"): Transition(
                _K, None,
                ("the upstream PR head is the controlled fork ref", NO_FAILING_JOB,
                 "worker slot free", REVIEW_REVISION_BUDGETS_REMAIN, NOT_PAUSED,
                 UPSTREAM_BOUND),
                _E(work="allocate", sets=(WORK_FEEDBACK,)), ("work_total",),
                "A revision driven only by review feedback; it charges no "
                "`repair_attempts_cluster`." + _FEEDBACK_NOTE),
            ("exhaust_repair_attempts", "upstream_review.waiting_human"): Transition(
                _K, None,
                (REVISION_BUDGET_EXHAUSTED,),
                _E(sets=("pending_human_action=repair_budget_exhausted", PENDING_COUNTER)),
                description="`pending_human_action.counter` names the counter that is exhausted."),
        }),
    "upstream_review.agent_running": State(
        "work", False, True,
        "Revision Work for the upstream PR head is requested or running, or its verified Result "
        "is being published.",
        transitions={
            ("record_revision_publish_success", "upstream_review.waiting_ci"): Transition(
                _R, None,
                ("the revision is published to the upstream PR head",),
                _E(revision="bump", work="clear",
                  sets=("proposal_artifact", "publication_operation_id", "upstream_pr.head_sha"),
                  clears=(CARRIED_FEEDBACK,)),
                description="The published revision answers exactly the feedback its Work "
                            "carried. `upstream_pr.head_sha` records the head the publisher read "
                            "back on `riscv64` after its push. A publication whose push was not "
                            "read back (`uncertain`) does not take this row: it is a failed "
                            "publication (`schedule_publish_retry`), and the publish-only retry "
                            "reads back the head a lost push left without pushing again."),
            ("reject_patch_proposal", "upstream_review.fix_needed"): Transition(
                _R, None,
                ("the proposal is rejected for the current revision, or the worker reports it is "
                 "blocked",
                 "no_progress remains"),
                _E(revision="bump", work="clear"), ("no_progress",),
                "The pending feedback is kept."),
            ("require_reviewer_answer", "upstream_review.waiting_human"): Transition(
                _R, None,
                ("the worker reports that no change is needed",),
                _E(work="clear", sets=("pending_human_action=answer_reviewer",)),
                description="A human answers the reviewer; the pipeline posts nothing on the "
                            "upstream PR."),
            ("schedule_publish_retry", SELF): _SCHEDULE_PUBLISH_RETRY,
            ("exhaust_publish_recovery", "upstream_review.waiting_human"): _EXHAUST_PUBLISH_RECOVERY,
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "upstream_review.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "upstream_review.waiting_human"): _EXHAUST_WORK_RECOVERY,
        }),
    "upstream_review.waiting_human": State(
        "human_wait", False, False,
        "The upstream PR was closed unmerged, its head changed, a reviewer needs an answer, or "
        "recovery or a budget is exhausted; a human triages.",
        transitions={
            ("resume_upstream_review", "upstream_review.waiting_ci"): Transition(
                _C, RESUME,
                (EXACT_REVISION,
                 pending_is("pr_closed_unmerged", "answer_reviewer", "upstream_head_changed"),
                 "for pr_closed_unmerged, the upstream PR is observed open again"),
                _E(revision="bump", clears=("pending_human_action", "pending_feedback_evidence_ids")),
                description="No revision Work is requested. The pending feedback is cleared when "
                            "the pending action is `answer_reviewer`, because a human answered "
                            "it; the other two pending actions hold none."),
            ("rearm_dispatch", "upstream_review.fix_needed"): _REARM_DISPATCH,
            ("resume_upstream_revision", "upstream_review.fix_needed"): Transition(
                _C, RESUME,
                (EXACT_REVISION,
                 pending_is("dispatch_failed", "work_failed", "publication_infrastructure_failure",
                            "repair_budget_exhausted", BASE_BRANCH_UNAVAILABLE,
                            UPSTREAM_UNAVAILABLE)),
                _E(revision="bump", clears=("pending_human_action", *_HELD),
                  restarts=("repair_attempts_cluster", "no_progress", "work_total",
                            "dispatch_recovery", "work_recovery", "publish_recovery",
                            "source_reads")),
                description="Only when revision Work failed: dispatch, Work, publication, or a "
                            "budget. The pending feedback is kept. " + _RESTART_NOTE),
        }),
    "release_wait.idle": State(
        "auto_wait", False, False,
        "The upstream PR is merged; official release coverage is observed.",
        transitions={
            ("record_release_incomplete", SELF): Transition(
                _O, None,
                ("release evidence does not yet satisfy the acceptance contract",)),
            ("require_release_intervention", "release_wait.waiting_human"): Transition(
                _O, None,
                ("release evidence needs operator action",), _E(sets=("pending_human_action",))),
            ("verify_release", "released.idle"): Transition(
                _O, None,
                ("verified release coverage satisfies the acceptance contract",),
                _E(sets=("release_acceptance",))),
        }),
    "release_wait.waiting_human": State(
        "human_wait", False, False,
        "Release evidence needs operator action.",
        transitions={
            ("resume_release_observation", "release_wait.idle"): Transition(
                _C, RESUME, (EXACT_REVISION,), _RESUME_EFFECTS),
        }),
    "released.idle": State(
        "terminal", False, False,
        "Verified official release coverage satisfies the acceptance contract."),
    "parked.idle": State(
        "generation_end", False, False,
        "The generation ended without success; a clean restart is possible.",
        transitions={
            ("restart_generation", "queued.idle"): Transition(
                _C, REFRESH,
                (EXACT_REVISION, QUIESCENT, "no open upstream PR"),
                _E(revision="bump", generation="bump", work="clear",
                   sets=("admission_override", "upstream.base_ref")),
                description="Starts a clean generation that runs complete planning; "
                            "identity, events, evidence, and replay fences are kept. An optional "
                            "class, paths, and reason are stored as `admission_override`. "
                            "`upstream.base_ref` is the optional `branch` of the command, else "
                            "the target branch the request asked for at intake, else none (the "
                            "upstream default branch is bound). Accepted while paused."),
            ("abandon", "closed.idle"): Transition(
                _C, "/upstreaming abandon",
                ("a reason is given", ANY_REVISION),
                _E(sets=("terminal_reason=abandoned",), clears=_HELD)),
        }),
    "closed.idle": State(
        "terminal", False, False,
        "No further work: withdrawn, not applicable, already satisfied, or abandoned. The reason "
        "is `terminal_reason`."),
}

# Transitions that apply in many states, keyed by verb.
RULES: dict[str, Rule] = {
    "park_contribution": Rule(
        "parked.idle",
        Transition(
            _C, PARK,
            ("a reason is given", ANY_REVISION, "no open operation"),
            _E(work="clear", sets=("parked_reason", "previous_phase"), clears=_HELD),
            description="Ends the current generation; evidence is kept for audit. A Result held "
                        "for publication is not published. Accepted while paused."),
        "every non-terminal state except `parked.idle`, `handoff.waiting_upstream_pr`, "
        "`release_wait.*`, and `upstream_review.*` other than `upstream_review.waiting_human`",
        kinds=("auto_wait", "work", "human_wait", "gate", "locked"),
        exclude=("handoff.waiting_upstream_pr", "release_wait.*", "upstream_review.*"),
        include=("upstream_review.waiting_human",)),
    "resume_stale": Rule(
        "queued.idle",
        Transition(
            _C, RESUME,
            (EXACT_REVISION, "paused, or waiting for a human", STALE, QUIESCENT, NO_WORK),
            _E(revision="bump", sets=_REFRESH_EFFECTS.sets,
              clears=(*_REFRESH_EFFECTS.clears, *_FLAG_FIELDS),
              restarts=("operation_launch", "interrupted_reruns", "dispatch_recovery",
                        "work_recovery", "work_total", "publish_recovery", "source_reads")),
            description="After a long stop the plan is refreshed: complete planning "
                        "runs again while the fork and development PR are kept. The stop is "
                        "counted from the older of `paused_at` and "
                        "`pending_human_action.requested_at`. While an operation is open no "
                        "resume applies, and the rejection names the open operation. From a "
                        "human wait that an exhausted budget caused, it starts again the "
                        "counter named in `pending_human_action.counter`, like every resume."),
        "every `provisioning.*` and `development.*` state",
        states=tuple(f"{phase}.*" for phase in STALE_PHASES)),
    "record_reminder": Rule(
        SELF,
        Transition(
            _K, None,
            ("at least `human_sla_business_days.reminder` business days since the request or the "
             "last reminder", NOT_PAUSED),
            _E(sets=("pending_human_action.reminded_at",)),
            description="Posts an append-only reminder of the pending human step. In "
                        "`handoff.waiting_upstream_pr` the reminder text carries the package "
                        "link and the exact head, and states when an observed PR does not match."),
        "every state of kind `gate`, `human_wait`, or `locked`",
        kinds=REMINDER_KINDS),
    "record_source_unavailable": Rule(
        SELF,
        Transition(
            _O, None, (BASE_BRANCH_UNREAD, "source_reads remains", NOT_PAUSED, QUIESCENT),
            charges=("source_reads",),
            description="A network error, a server error, a rate limit, or a repository that "
                        "cannot be read: the contribution stays, and a later tick reads the "
                        "upstream again. A failed read is charged only when it is the first of "
                        "a run, or once the spacing (`retries.source_unavailable_minutes` "
                        "divided by `retries.source_unavailable_ticks`) has passed since the "
                        "last charged read; a failed read in between writes nothing, so a "
                        "burst of ticks adds no events. A tick that reads the upstream starts "
                        "`source_reads` again."),
        "every state in which the controller needs the upstream base branch: "
        "`queued.idle`, `planning.idle`, `provisioning.idle`, `development.fix_needed`, "
        "`handoff.review_due`, `handoff.review_pending`, and `upstream_review.fix_needed`",
        states=("queued.idle", "planning.idle", "provisioning.idle", "development.fix_needed",
                "handoff.review_due", "handoff.review_pending", "upstream_review.fix_needed")),
    "record_late_effect": Rule(
        SELF,
        Transition(
            _R, None,
            ("the Result does not match the current generation, revision, and requested Work",),
            description="Recorded in the events for audit. The Result is never folded, the "
                        "publisher refuses it, and the state never changes."),
        "every state"),
}


def rule_states(verb: str) -> tuple[str, ...]:
    """The states a rule applies in, in table order."""
    from fnmatch import fnmatchcase

    rule = RULES[verb]

    def matches(key: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatchcase(key, p) for p in patterns)

    return tuple(
        key for key, state in LIFECYCLE.items()
        if (not rule.kinds or state.kind in rule.kinds)
        and (not rule.states or matches(key, rule.states))
        and (not matches(key, rule.exclude) or matches(key, rule.include)))


def expand_rules() -> None:
    """Add one row per matching state for every rule, after the state's own rows."""
    for verb, rule in RULES.items():
        for key in rule_states(verb):
            transitions = LIFECYCLE[key].transitions
            if any(v == verb for v, _to in transitions):
                raise ValueError(f"{key} already has a transition {verb}")
            transitions[(verb, SELF if rule.target == key else rule.target)] = rule.transition


# The states in which the controller needs the upstream base branch (Work or
# the fork provisioning is due), each with the human wait it leads to when
# the base branch is unavailable: the wait of its phase, and for a request
# in `queued.idle`, which has none, the wait of planning, the phase it is
# about to begin.
BASE_BRANCH_WAITS = {
    "queued.idle": "planning.waiting_human",
    "planning.idle": "planning.waiting_human",
    "provisioning.idle": "provisioning.waiting_human",
    "development.fix_needed": "development.waiting_human",
    "handoff.review_due": "handoff.waiting_human",
    "handoff.review_pending": "handoff.waiting_human",
    "upstream_review.fix_needed": "upstream_review.waiting_human",
}
_UNAVAILABLE_NOTE = ("While the contribution waits, the upstream is read again only once an "
                     "authorized resume is posted.")
_REQUIRE_BASE_BRANCH = Transition(
    _O, None, (BASE_BRANCH_GONE, NOT_PAUSED, QUIESCENT),
    _E(sets=(f"pending_human_action={BASE_BRANCH_UNAVAILABLE}",)),
    description="Records whether the branch does not exist or was renamed, and to which name. "
                + _UNAVAILABLE_NOTE + " Once the branch exists again, a resume continues; to "
                "continue on another branch, the contribution is parked first and then restarted "
                "on that branch.")
_EXHAUST_SOURCE_READS = Transition(
    _O, None, (BASE_BRANCH_UNREADABLE, NOT_PAUSED, QUIESCENT),
    _E(sets=(f"pending_human_action={BASE_BRANCH_UNAVAILABLE}",
             f"{PENDING_COUNTER}=source_reads")),
    description="Records how many failed reads were counted, since when, how many of them "
                "could not read the repository itself, and the last error. The upstream is "
                "still read on every tick, and once it can be read again the wait ends without "
                "a human (`resume_upstream_read`); a resume does the same at once.")
_REQUIRE_UPSTREAM_ACCESS = Transition(
    _O, None, (UPSTREAM_GONE, NOT_PAUSED, QUIESCENT),
    _E(sets=(f"pending_human_action={UPSTREAM_UNAVAILABLE}",
             f"{PENDING_COUNTER}=source_reads")),
    description="Taken at once when GitHub answers for the recorded repository id under "
                "another name (renamed or transferred), and otherwise once failed reads of the "
                "repository itself are the majority of the retry window. Records the "
                "repository, the name GitHub now gives it, and the counted reads. The upstream "
                "is still read on every tick, and once it can be read under its recorded name "
                "again the wait ends without a human; a repository that moved is a new request: "
                "the contribution is parked, then abandoned.")
for _state, _wait in BASE_BRANCH_WAITS.items():
    LIFECYCLE[_state].transitions[("require_base_branch", _wait)] = _REQUIRE_BASE_BRANCH
    LIFECYCLE[_state].transitions[("exhaust_source_reads", _wait)] = _EXHAUST_SOURCE_READS
    LIFECYCLE[_state].transitions[("require_upstream_access", _wait)] = _REQUIRE_UPSTREAM_ACCESS

# The wait that failed reads caused ends once the upstream is read again: the
# same transition as the resume a human would post, taken by the controller.
# Each wait returns to the state its posted resume returns to.
UPSTREAM_READ_RESUMES = {
    "planning.waiting_human": "planning.idle",
    "provisioning.waiting_human": "provisioning.idle",
    "development.waiting_human": "development.fix_needed",
    "handoff.waiting_human": "handoff.review_due",
    "upstream_review.waiting_human": "upstream_review.fix_needed",
}
_RESUME_UPSTREAM_READ_DESCRIPTION = (
    "Ends a wait that failed reads caused once a tick reads the upstream again: the step that "
    "was due runs again, and the tracking issue states that the contribution continues. A wait "
    "the upstream's own answer caused (a missing or renamed branch) still needs a resume.")


def _resume_upstream_read(*restarts: str) -> Transition:
    # A tick row, like `rearm_dispatch`: the wait has no observation row, so
    # the head and CI are not observed while it lasts.
    return Transition(
        _K, None,
        (pending_is(BASE_BRANCH_UNAVAILABLE, UPSTREAM_UNAVAILABLE), READS_CAUSED_WAIT,
         UPSTREAM_READ_AGAIN, NOT_PAUSED, QUIESCENT),
        _E(revision="bump", clears=("pending_human_action",), restarts=restarts),
        description=_RESUME_UPSTREAM_READ_DESCRIPTION)


for _wait, _to in UPSTREAM_READ_RESUMES.items():
    # The wait of provisioning is also entered by the exhaustion of
    # `operation_launch`; the row restarts only the counter the pending action
    # names, which is `source_reads` whenever its guards hold.
    LIFECYCLE[_wait].transitions[("resume_upstream_read", _to)] = _resume_upstream_read(
        *(("operation_launch",) if _wait == "provisioning.waiting_human" else ()), "source_reads")

# A renamed base branch of an open upstream PR: the record follows the
# rename, in the states of upstream review that read the base branch, and
# ends the wait the rename caused. A paused record follows it once resumed.
_FOLLOW_RENAMED_BASE = Transition(
    _O, None, (BASE_RENAMED_IN_PR, NOT_PAUSED, QUIESCENT),
    _E(sets=("upstream.base_ref", "upstream_pr.base_ref")),
    description="`upstream.base_ref` and the recorded base of the upstream PR become the new "
                "name; `upstream.base_sha` is kept, since the renamed branch still contains it. "
                "The tracking issue states the rename.")
for _state in ("upstream_review.waiting_ci", "upstream_review.fix_needed"):
    LIFECYCLE[_state].transitions[("follow_renamed_base", SELF)] = _FOLLOW_RENAMED_BASE
LIFECYCLE["upstream_review.waiting_human"].transitions[
    ("resume_renamed_base", "upstream_review.fix_needed")] = Transition(
    _O, None, (pending_is(BASE_BRANCH_UNAVAILABLE), BASE_RENAMED_IN_PR, NOT_PAUSED, QUIESCENT),
    _E(revision="bump", sets=("upstream.base_ref", "upstream_pr.base_ref"),
       clears=("pending_human_action",), restarts=("source_reads",)),
    description="The wait a renamed base branch caused ends without a human: the record follows "
                "the rename as `follow_renamed_base` does, and the revision Work that was due "
                "is due again. The tracking issue states the rename.")

expand_rules()


FLAGS: dict[str, Flag] = {
    "paused": Flag(
        "paused",
        "A stop of new Work and writer admission, with its reason and who set it (a human or "
        "the controller) in `paused_reason` and `paused_by`. `pause` accepts an optional reason; "
        "a pause set by the controller always records one: `pause_on_identity_change` names "
        "it in the table, and `pause_on_processing_error` records the error. Setting it clears "
        "the requested "
        "Work and the pending publication, and "
        "records `paused_at`; nothing held is published after the stop, and no publish retry "
        "is launched while paused. Phase, "
        "activity, evidence, and pending feedback are kept. "
        "The one exception: setting it in an `agent_running` state leads to the state in which the "
        "Work is due again, `planning.idle`, `development.fix_needed`, "
        "`handoff.review_due` (also from `handoff.reviewing`), or `upstream_review.fix_needed`. "
        "In `handoff.awaiting_submission` and `handoff.waiting_upstream_pr`, `resume_pause` "
        "keeps the plan, candidate head, clean head, `handoff_request`, and the posted "
        "package. `verify_upstream_pr` binds to the exact head only, so the revision bump "
        "does not invalidate the posted package; `submit_upstream` and `retract_submission` "
        "also name the exact revision, so after the bump they name the new revision with the "
        "same head, as the tracking-issue entries offer them.",
        {
            "pause": Transition(
                _C, "/upstreaming pause",
                (NOT_PAUSED, "not terminal", ANY_REVISION),
                _E(work="clear", sets=(*_FLAG_FIELDS[:3], "paused_by=human"),
                  clears=_HELD),
                description="A reason is optional; when given it is recorded in `paused_reason`."),
            "pause_on_identity_change": Transition(
                _O, None,
                (NOT_PAUSED, "not terminal",
                 "the observed upstream repository identity differs from the recorded one"),
                _E(work="clear", sets=("paused", "paused_at",
                                       "paused_reason=upstream_identity_changed",
                                       "paused_by=controller"),
                  clears=_HELD)),
            "pause_on_processing_error": Transition(
                _K, None,
                (NOT_PAUSED, "not terminal", PROCESSING_ERROR),
                _E(work="clear", sets=(*_FLAG_FIELDS[:3], "paused_by=controller"),
                  clears=_HELD),
                description="Everything the tick staged for the contribution and every effect "
                            "it requested for it are discarded first, so the stop is taken on "
                            "the record as it was committed before the tick. `paused_reason` "
                            "states the error class, the bounded and sanitized message, and the "
                            "run id. The tick continues with the other contributions. A "
                            "contribution that is already paused is not paused again."),
        },
        {
            "resume_pause": Transition(
                _C, RESUME, ("paused", EXACT_REVISION, FLAG_NOT_STALE),
                _E(revision="bump", clears=_FLAG_FIELDS),
                description="A Result that landed during the pause no longer matches and takes "
                            "`record_late_effect`. In a human wait the resume clears the flag "
                            "only: the reply states that the contribution still waits for a "
                            "human and names the pending action, and a second resume leaves "
                            "the wait."),
        },
        {
            "planning.agent_running": "planning.idle",
            "development.agent_running": "development.fix_needed",
            "handoff.agent_running": "handoff.review_due",
            "handoff.reviewing": "handoff.review_due",
            "upstream_review.agent_running": "upstream_review.fix_needed",
        },
        {
            _C: "`resume`, `park`, `refresh`, `retract-pr`, `abandon`, and `resolve-operation` "
                "apply; other commands are rejected. `resume` clears the flag first: it takes "
                "`resume_pause` or `resume_stale`. In a human wait a second `resume` then leaves "
                "the wait.",
            _O: "Folded normally, including CI on the exact head.",
            _R: "The requested Work and the pending publication were cleared, so a Result takes "
                "`record_late_effect`; nothing is published.",
            _P: "Folded normally. The publisher's final check reads `paused`; an effect that "
                "passed it before the pause may finish.",
            _K: "No launch, no Work allocation, no budget charge; reminders are suppressed.",
        },
    ),
}

BUDGETS: dict[str, Budget] = {
    "work_total": Budget(
        "retries.max_attempts_per_contribution", 0,
        "one Work allocation of any kind",
        "`budget.attempts_used`, counted per generation",
        ("exhaust_work_total", "exhaust_work_recovery", "exhaust_repair_attempts")),
    "work_recovery": Budget(
        "retries.work_recovery", 1,
        "one fresh Work graph after terminal Work without a usable Result",
        "`active_work.attempt`", ("exhaust_work_recovery",), infrastructure=True),
    "planning_attempts": Budget(
        "retries.planning_attempts", 0,
        "one planning Result without a usable plan",
        "`budget.planning_attempts`", ("exhaust_planning_recovery",)),
    "repair_attempts_cluster": Budget(
        "retries.patch_attempts_per_cluster", 0,
        "one repair or revision Work allocation for one failing CI job; Work that is due "
        "without a failing CI job charges none",
        "`budget.clusters[<job>]`, started again by `refresh_on_exhaustion`",
        ("refresh_on_exhaustion", "exhaust_repair_attempts")),
    "no_progress": Budget(
        "retries.no_progress_review_after", 0,
        "one successive rejected or blocked outcome without progress",
        "`budget.no_progress_streak`, started again by a published patch and by "
        "`refresh_on_exhaustion`",
        ("refresh_on_exhaustion", "exhaust_repair_attempts")),
    "flaky_reruns": Budget(
        "retries.flaky_reruns", 0,
        "one rerun of the failed CI jobs on the exact head, requested by the repair worker",
        "`budget.reruns.flaky`, counted for the head in `budget.reruns.head_sha` and started "
        "again whenever the recorded head changes", ("reject_patch_proposal",)),
    "interrupted_reruns": Budget(
        INFRASTRUCTURE_KEY, 0,
        "one rerun by the collector of CI jobs that were cancelled or lost their runner",
        "`budget.reruns.interrupted`, counted for the head in `budget.reruns.head_sha` and "
        "started again whenever the recorded head changes and by `resume_ci`",
        ("exhaust_interrupted_reruns",), infrastructure=True),
    "dispatch_recovery": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one dispatcher run that ended with the requested Work absent or unclaimed",
        "dispatcher runs since `active_work.requested_at`", ("exhaust_dispatch_recovery",),
        infrastructure=True),
    "publish_recovery": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one publish-only retry of the same verified Result",
        "`pending_publication.attempts`", ("exhaust_publish_recovery",), infrastructure=True),
    "source_reads": Budget(
        "retries.source_unavailable_ticks", 0,
        "one failed read of the upstream repository or base branch without a definitive "
        "answer, counted at most once per spacing (`retries.source_unavailable_minutes` "
        "divided by `retries.source_unavailable_ticks`)",
        "`budget.source_unavailable`, the counted failed reads, since "
        "`budget.source_unavailable_since`, the last at `budget.source_unavailable_at`, of which "
        "`budget.source_unavailable_repository` could not read the repository itself: a tick "
        "that reads the upstream starts it again. It is exhausted only once both the count is "
        "reached and `retries.source_unavailable_minutes` minutes have passed since the first "
        "failed read",
        ("exhaust_source_reads", "require_upstream_access")),
    "operation_launch": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one launch of a planned fork provisioning operation", "the operation's launch attempts",
        ("require_provisioning_intervention",), infrastructure=True),
    "upstream_branch": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one lease write and readback of `riscv64`", "the handoff operation's attempts",
        ("exhaust_upstream_branch_retries",), infrastructure=True),
    "submission_comment": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one attempt to post the submission package", "the handoff operation's attempts",
        ("exhaust_comment_retries",), infrastructure=True),
}

# The table of one remote operation. An operation is an intent recorded
# before a remote effect and a receipt confirmed by discovering the actual
# resource. A lifecycle transition with the ``operation`` trigger is taken
# when its operation reaches ``confirmed`` or ``failed``.
OPERATION_INITIAL_STATE = "planned"
OPERATION_INITIAL_VERB = "record_intent"

_RESOLVE_GUARD = (EXACT_OPERATION, "a resolution evidence record states the disposition")
_RESOLVE = {
    ("resolve_confirmed", "confirmed"): Transition(
        _C, RESOLVE, (*_RESOLVE_GUARD, "disposition confirmed"), _E(sets=("resolution",))),
    ("resolve_failed", "failed"): Transition(
        _C, RESOLVE, (*_RESOLVE_GUARD, "disposition failed"), _E(sets=("resolution",))),
    ("resolve_not_launched", "not_launched"): Transition(
        _C, RESOLVE, (*_RESOLVE_GUARD, "disposition not-launched"), _E(sets=("resolution",))),
}

OPERATIONS: dict[str, OperationState] = {
    "planned": OperationState(
        True, "The intent is recorded; the remote effect has not been attempted.",
        transitions={
            ("begin_effect", "applying"): Transition(
                _K, None,
                ("capacity available", "the contribution is not paused",
                 "the final check before the effect passes")),
            **_RESOLVE,
        }),
    "applying": OperationState(
        True, "The remote call is in flight.",
        transitions={
            ("record_response", "uncertain"): Transition(
                _P, None,
                ("the remote call succeeded, or its response was lost or ambiguous",),
                _E(sets=("external_ids",)),
                description="Even a successful response is provisional until discovery confirms "
                            "the resource."),
            ("record_failure", "failed"): Transition(
                _P, None, ("the remote call returned a definite failure",),
                _E(sets=("external_ids",))),
            **_RESOLVE,
        }),
    "uncertain": OperationState(
        True, "The effect may or may not exist; it is discovered, never repeated.",
        transitions={
            ("confirm_by_discovery", "confirmed"): Transition(
                _P, None, ("discovery finds the exact resource the operation proposed",),
                _E(sets=("discovery",))),
            ("record_discovery", SELF): Transition(
                _P, None, ("discovery finds the resource absent or an unrelated one",),
                _E(sets=("discovery",)),
                description="Absence never proves that the effect did not launch, and elapsed "
                            "time never closes an operation."),
            **_RESOLVE,
        }),
    "confirmed": OperationState(False, "The effect exists and was read back."),
    "failed": OperationState(False, "The effect definitely did not succeed."),
    "not_launched": OperationState(
        False, "A human established with evidence that the effect was never launched."),
}

TERMINAL_STATES = frozenset(k for k, s in LIFECYCLE.items() if s.kind == "terminal")
# The phases and activities of the table's states, in table order: the record
# schema's `phase` and `activity` enums are generated from them.
PHASES = tuple(dict.fromkeys(k.split(".", 1)[0] for k in LIFECYCLE))
ACTIVITIES = tuple(dict.fromkeys(k.split(".", 1)[1] for k in LIFECYCLE))


class Edge(NamedTuple):
    """One table transition with its source and resolved target state."""

    source: str
    verb: str
    target: str
    transition: Transition


def state_phase(state: str) -> str:
    return state.split(".", 1)[0]


def resolve_target(state: str, to: str) -> str:
    return state if to == SELF else to


def state_edges(state: str, table: dict | None = None):
    """Yield an ``Edge`` for every transition leaving ``state``, in table order."""
    for (verb, to), t in (table or LIFECYCLE)[state].transitions.items():
        yield Edge(state, verb, resolve_target(state, to), t)


def table_edges(table: dict | None = None):
    """Yield an ``Edge`` for every table transition, in table order."""
    for state in table or LIFECYCLE:
        yield from state_edges(state, table)


def find_edge(state: str, verb: str, table: dict | None = None) -> Edge:
    """The transition ``verb`` from ``state``; the verb must lead to one target."""
    edges = [e for e in state_edges(state, table) if e.verb == verb]
    if len(edges) != 1:
        raise KeyError(f"{state} has {len(edges)} transitions {verb}")
    return edges[0]


def is_rule_row(edge: Edge) -> bool:
    """Whether a table row is the expansion of a rule."""
    rule = RULES.get(edge.verb)
    return rule is not None and edge.transition is rule.transition


def _names_exhaustion(t: Transition, budget: str) -> bool:
    """Whether a guard of ``t`` states that ``budget`` is exhausted. A guard
    that requires the budget to remain does not."""
    return any(budget in g and "exhausted" in g for g in t.guard)


def exhaustion_edges(budget: str):
    """The transitions taken when ``budget`` is exhausted: one of its
    exhaustion verbs with a guard that names the budget."""
    verbs = BUDGETS[budget].exhaustion_verbs
    for e in table_edges():
        if e.verb in verbs and _names_exhaustion(e.transition, budget):
            yield e


def resolve_node(node: str, verb: str) -> tuple[Transition, str]:
    """The transition ``verb`` from a diagram node and the node it leads to.

    ``node`` may carry a flag (``state+flag``). Setting a flag keeps the
    state, or follows ``set_moves``; clearing it keeps the state. A table
    transition keeps the flag unless it clears the flag's field or starts a
    new generation."""
    state, _, flag = node.partition("+")
    if flag and verb in FLAGS[flag].cleared_by:
        return FLAGS[flag].cleared_by[verb], state
    for name, f in FLAGS.items():
        if verb in f.set_by:
            return f.set_by[verb], f"{f.set_moves.get(state, state)}+{name}"
    edge = find_edge(state, verb)
    effects = edge.transition.effects
    keeps = flag and FLAGS[flag].field not in effects.clears and effects.generation == "keep"
    return edge.transition, f"{edge.target}+{flag}" if keeps else edge.target


def policy_value(policy: dict, key: str) -> Any:
    value: Any = policy
    for part in key.split("."):
        value = value[part]
    return value


def budget_limit(name: str, policy: dict) -> int:
    budget = BUDGETS[name]
    return int(policy_value(policy, budget.policy_key)) + budget.offset


# --------------------------------------------------------------------------
# Diagram presentation metadata. Each diagram selects table states (``nodes``,
# in drawing order; phases are grouped in order of first appearance) and
# table transitions (``edges`` as ``(from_node, verb)``, in drawing order). A
# node written ``state+flag`` is ``state`` with that flag set. A diagram with
# ``rule`` draws that rule once, from a single node that stands for every
# state the rule applies in; rule rows are not repeated on every state.


@dataclass(frozen=True)
class Diagram:
    title: str | None
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    start: str | None = None
    start_verb: str | None = None
    ends: tuple[str, ...] = ()
    rule: str | None = None
    text: str = ""


@dataclass(frozen=True)
class DiagramSection:
    title: str
    text: str
    diagrams: tuple[Diagram, ...]


_SIMPLIFIED_NODES = (
    "queued.idle",
    "planning.idle", "planning.agent_running", "planning.waiting_human",
    "provisioning.idle", "provisioning.waiting_human",
    "development.fix_needed", "development.agent_running", "development.waiting_ci",
    "development.waiting_human",
    "handoff.awaiting_submission", "handoff.review_due", "handoff.agent_running",
    "handoff.review_pending", "handoff.reviewing",
    "handoff.building_upstream_branch", "handoff.waiting_ci", "handoff.preparing_submission",
    "handoff.waiting_upstream_pr",
    "upstream_review.waiting_ci", "upstream_review.fix_needed", "upstream_review.agent_running",
    "upstream_review.waiting_human",
    "release_wait.idle", "release_wait.waiting_human",
    "released.idle", "parked.idle", "closed.idle",
)

DIAGRAM_SECTIONS: tuple[DiagramSection, ...] = (
    DiagramSection(
        "Simplified state diagram",
        "This is the expected path when planning finds useful work, provisioning succeeds, "
        "development CI becomes green, upstream accepts the contribution, and an official release "
        "publishes the required artifacts.",
        (
            Diagram(None, _SIMPLIFIED_NODES, (
                ("queued.idle", "begin_planning"),
                ("queued.idle", "withdraw_request"),
                ("planning.idle", "dispatch_planning"),
                ("planning.agent_running", "reject_plan"),
                ("planning.agent_running", "exhaust_planning_recovery"),
                ("planning.waiting_human", "resume_planning"),
                ("planning.agent_running", "admit_development"),
                ("planning.agent_running", "park_planning"),
                ("planning.agent_running", "close_planning"),
                ("provisioning.idle", "require_provisioning_intervention"),
                ("provisioning.waiting_human", "resume_provisioning"),
                ("provisioning.idle", "confirm_provisioning"),
                ("development.fix_needed", "request_patch"),
                ("development.fix_needed", "request_repair"),
                ("development.agent_running", "record_publish_success"),
                ("development.agent_running", "reject_patch_proposal"),
                ("development.agent_running", "request_ci_rerun"),
                ("development.waiting_ci", "record_ci_failure"),
                ("development.waiting_ci", "record_actionable_review"),
                ("development.fix_needed", "record_actionable_review"),
                ("development.waiting_ci", "record_explanation"),
                ("development.waiting_human", "resume_development"),
                ("development.waiting_ci", "nominate_for_handoff"),
                ("development.waiting_ci", "return_for_feedback"),
                ("handoff.awaiting_submission", "submit_upstream"),
                ("handoff.review_due", "request_submission_rewrite"),
                ("handoff.agent_running", "accept_submission_rewrite"),
                ("handoff.review_pending", "request_submission_review"),
                ("handoff.reviewing", "accept_submission_review"),
                ("handoff.building_upstream_branch", "confirm_clean_head"),
                ("handoff.waiting_ci", "accept_clean_head_ci"),
                ("handoff.preparing_submission", "confirm_submission_package"),
                ("handoff.waiting_upstream_pr", "verify_upstream_pr"),
                ("handoff.awaiting_submission", "record_actionable_review"),
                ("handoff.awaiting_submission", "record_explanation"),
                ("handoff.agent_running", "reject_submission_rewrite"),
                ("handoff.reviewing", "reject_submission_review"),
                ("handoff.building_upstream_branch", "return_for_repair"),
                ("handoff.waiting_ci", "return_for_repair"),
                ("handoff.waiting_upstream_pr", "retract_submission"),
                ("upstream_review.waiting_ci", "record_upstream_ci_failure"),
                ("upstream_review.waiting_ci", "record_actionable_review"),
                ("upstream_review.waiting_ci", "record_explanation"),
                ("upstream_review.fix_needed", "request_upstream_revision"),
                ("upstream_review.fix_needed", "request_review_revision"),
                ("upstream_review.agent_running", "record_revision_publish_success"),
                ("upstream_review.waiting_ci", "record_pr_closed_unmerged"),
                ("upstream_review.waiting_human", "resume_upstream_review"),
                ("upstream_review.waiting_human", "resume_upstream_revision"),
                ("upstream_review.waiting_ci", "record_merge"),
                ("release_wait.idle", "require_release_intervention"),
                ("release_wait.waiting_human", "resume_release_observation"),
                ("release_wait.idle", "verify_release"),
                ("parked.idle", "restart_generation"),
                ("parked.idle", "abandon"),
            ), start=INITIAL_STATE, start_verb=INITIAL_VERB, ends=("released.idle", "closed.idle"),
                text="On both the development PR and the upstream PR, a CI failure keeps the "
                     "contribution in its phase while a bounded repair is prepared and tested, and "
                     "an actionable review comment is included in the next repair. An "
                     "explanation-only comment is answered on the development PR; at the "
                     "submission gate and on the upstream PR it is recorded and linked from the "
                     "event log of the website, and nothing is answered automatically. Transitions that apply in many states (`park_contribution`, "
                     "`resume_stale`, `record_reminder`, `record_late_effect`) are drawn once, "
                     "under the rules below."),
        ),
    ),
    DiagramSection(
        "Planning and provisioning failure cases",
        "Planning and provisioning fail in different ways, and each case has its own recovery "
        "path. An infrastructure failure that exhausts its retries waits for a human in the same "
        "phase; it never ends the generation.",
        (
            Diagram(
                "Failure case 1: planning finds no development path",
                ("planning.idle", "planning.agent_running", "provisioning.idle",
                 "parked.idle", "queued.idle", "closed.idle"),
                (("planning.idle", "dispatch_planning"),
                 ("planning.agent_running", "admit_development"),
                 ("planning.agent_running", "park_planning"),
                 ("planning.agent_running", "close_planning"),
                 ("parked.idle", "restart_generation"),
                 ("parked.idle", "abandon")),
                start="planning.idle", ends=("closed.idle",),
                text="The verified plan decides between development, park, and close; a close "
                     "is confirmed from PyPI by the reconciler, so a project that is not released on PyPI "
                     "is parked instead. "
                     "`closed` is the single terminal phase for work that is not needed; the reason "
                     "is `terminal_reason`. A restart may record an `admission_override`; the new "
                     "generation still runs complete planning."),
            Diagram(
                "Failure case 2: the Result has no usable plan",
                ("planning.idle", "planning.agent_running", "planning.waiting_human",
                 "provisioning.idle"),
                (("planning.idle", "dispatch_planning"),
                 ("planning.agent_running", "reject_plan"),
                 ("planning.agent_running", "exhaust_planning_recovery"),
                 ("planning.waiting_human", "resume_planning"),
                 ("planning.agent_running", "admit_development")),
                start="planning.idle",
                text="`reject_plan` retries automatically while `planning_attempts` remains; "
                     "a Result without a plan counts like an invalid one. An incomplete artifact "
                     "is never adopted by hand."),
            Diagram(
                "Failure case 3: the planner or dispatcher has an infrastructure failure",
                ("planning.idle", "planning.agent_running", "planning.waiting_human",
                 "parked.idle"),
                (("planning.idle", "dispatch_planning"),
                 ("planning.agent_running", "retry_dispatch"),
                 ("planning.agent_running", "allocate_work_retry"),
                 ("planning.agent_running", "exhaust_dispatch_recovery"),
                 ("planning.agent_running", "exhaust_work_recovery"),
                 ("planning.idle", "exhaust_work_total"),
                 ("planning.waiting_human", "resume_planning"),
                 ("planning.waiting_human", "rearm_dispatch"),
                 ("planning.waiting_human", "park_contribution")),
                start="planning.idle",
                text="Dispatch and terminal-Work recovery are automatic within `dispatch_recovery` "
                     "and `work_recovery`. A terminal Work graph is never submitted again under the "
                     "same identity. Every Work allocation also charges `work_total`. While the "
                     "installed work-queue Policy refuses every dispatch, a dispatch is not "
                     "decided until the queue is quiescent, where the dispatcher reinstalls the "
                     "compiled policy; a dispatch that failed before the reinstall is re-armed by "
                     "`rearm_dispatch`."),
            Diagram(
                "Failure case 4: fork provisioning fails or its outcome is uncertain",
                ("provisioning.idle", "provisioning.waiting_human", "development.fix_needed",
                 "parked.idle"),
                (("provisioning.idle", "start_operation"),
                 ("provisioning.idle", "require_provisioning_intervention"),
                 ("provisioning.waiting_human", "resume_provisioning"),
                 ("provisioning.idle", "confirm_provisioning"),
                 ("provisioning.waiting_human", "park_contribution")),
                start="provisioning.idle",
                text="`confirm_provisioning` follows fork creation, configuration, receipt "
                     "validation, and exact readback. `start_operation` relaunches within "
                     "`operation_launch`. A lost receipt is not proof that an operation did not "
                     "launch; an uncertain effect is resolved by discovery or "
                     "`/upstreaming resolve-operation`, never repeated blindly. A human corrects "
                     "the prerequisite and resumes, or parks."),
        ),
    ),
    DiagramSection(
        "Development and repair cases",
        "Development is one bounded loop around the same development PR. A worker proposes a patch, "
        "trusted code validates and publishes it, and CI plus authorized review evaluate the exact "
        "head. Only `nominate_for_handoff` leaves development forward.",
        (
            Diagram(
                "Success case: patch, publish, CI, and handoff",
                ("development.fix_needed", "development.agent_running", "development.waiting_ci",
                 "handoff.awaiting_submission"),
                (("development.fix_needed", "request_patch"),
                 ("development.agent_running", "record_publish_success"),
                 ("development.waiting_ci", "nominate_for_handoff")),
                start="development.fix_needed",
                text="`development.fix_needed` is the one state in which development Work is due, "
                     "and it is entered only after `confirm_provisioning`: no patch is proposed "
                     "before the fork exists. `request_repair` applies while a failing CI job is "
                     "recorded for the head and charges `repair_attempts_cluster` for that job; "
                     "`request_patch` applies otherwise (the first patch, a rejected proposal, a "
                     "repair driven only by review feedback) and charges `work_total` only. Both "
                     "need write admission. Every applicable build and test job must execute and "
                     "pass on the exact head."),
            Diagram(
                "Failure case 1: patch proposal, dispatch, or Work failure",
                ("development.fix_needed", "development.agent_running",
                 "development.waiting_human"),
                (("development.fix_needed", "request_patch"),
                 ("development.agent_running", "reject_patch_proposal"),
                 ("development.agent_running", "replan_development"),
                 ("development.fix_needed", "request_repair"),
                 ("development.agent_running", "retry_dispatch"),
                 ("development.agent_running", "allocate_work_retry"),
                 ("development.agent_running", "exhaust_dispatch_recovery"),
                 ("development.agent_running", "exhaust_work_recovery"),
                 ("development.waiting_human", "resume_development"),
                 ("development.waiting_human", "rearm_dispatch")),
                start="development.fix_needed",
                text="Dispatch recovery and bounded repair are automatic. Dispatch and Work "
                     "exhaustion wait for a human in development. A rejected proposal, a replan, "
                     "and a resume keep the pending feedback. A dispatch that failed while the "
                     "installed work-queue Policy refused every dispatch is re-armed by "
                     "`rearm_dispatch` once the dispatcher has reinstalled the compiled policy."),
            Diagram(
                "Failure case 2: trusted publication failure",
                ("development.agent_running", "development.fix_needed", "development.waiting_human",
                 "development.waiting_ci"),
                (("development.agent_running", "record_publish_success"),
                 ("development.agent_running", "schedule_publish_retry"),
                 ("development.agent_running", "exhaust_publish_recovery"),
                 ("development.waiting_human", "resume_development"),
                 ("development.fix_needed", "request_repair")),
                start="development.agent_running",
                text="An infrastructure failure publishes the same verified Result again within "
                     "`publish_recovery`. After exhaustion and a resume, fresh Work is requested; "
                     "the held Result is not reused."),
            Diagram(
                "Failure case 3: internal PR CI or review failure",
                ("development.waiting_ci", "development.fix_needed", "development.agent_running",
                 "development.waiting_human", "handoff.awaiting_submission"),
                (("development.waiting_ci", "record_ci_failure"),
                 ("development.waiting_ci", "record_actionable_review"),
                 ("development.fix_needed", "record_actionable_review"),
                 ("development.waiting_ci", "record_explanation"),
                 ("development.fix_needed", "request_repair"),
                 ("development.agent_running", "record_publish_success"),
                 ("development.agent_running", "request_ci_rerun"),
                 ("development.waiting_ci", "nominate_for_handoff"),
                 ("development.waiting_ci", "rerun_interrupted_ci"),
                 ("development.waiting_ci", "exhaust_interrupted_reruns"),
                 ("development.waiting_human", "resume_ci"),
                 ("development.waiting_ci", "record_head_change"),
                 ("development.fix_needed", "record_head_change"),
                 ("development.waiting_ci", "return_for_feedback"),
                 ("development.waiting_ci", "record_pr_mismatch")),
                start="development.waiting_ci",
                text="Build and test failures and actionable comments request repair automatically. "
                     "The repair worker classifies a failure: one not caused by the patch takes "
                     "`request_ci_rerun` within `flaky_reruns`. The collector reruns on its own only "
                     "jobs that were cancelled or lost their runner, within `interrupted_reruns` "
                     "per head; after that a human resumes with `resume_ci`, after which the "
                     "collector reruns the jobs again. Both rerun counts are kept for the current head in `budget.reruns` "
                     "and start again whenever the recorded head changes. The Work request "
                     "carries the pending feedback present at allocation, and "
                     "`record_publish_success` clears exactly that feedback. Work that carries "
                     "feedback never takes `request_ci_rerun`. Explanation-only comments do not "
                     "bump the revision, invalidate CI, or request repair. A push to the "
                     "development branch while CI is awaited or Work is due records the new head "
                     "and waits for its CI. Passing CI while feedback is pending returns to "
                     "`development.fix_needed` (`return_for_feedback`), so a push while Work is "
                     "due for a review never strands the review."),
            Diagram(
                "Failure case 4: a repair budget is exhausted",
                ("development.fix_needed", "development.agent_running", "queued.idle",
                 "planning.idle", "planning.agent_running", "parked.idle"),
                (("development.fix_needed", "request_repair"),
                 ("development.fix_needed", "refresh_on_exhaustion"),
                 ("queued.idle", "begin_planning"),
                 ("planning.idle", "dispatch_planning"),
                 ("planning.agent_running", "readmit_development"),
                 ("development.fix_needed", "exhaust_repair_attempts")),
                start="development.fix_needed",
                text="Exhaustion is decided at `development.fix_needed`. The first time "
                     "`repair_attempts_cluster` or `no_progress` is exhausted in a generation, "
                     "`refresh_on_exhaustion` runs complete planning again on the existing "
                     "fork. After that, `exhaust_repair_attempts` parks. Both apply only while "
                     "`work_total` remains. `repair_attempts_cluster` counts only while a failing "
                     "CI job is recorded for the head."),
            Diagram(
                "Failure case 5: the Work budget is exhausted",
                ("development.fix_needed", "development.agent_running",
                 "development.waiting_human", "parked.idle"),
                (("development.fix_needed", "request_repair"),
                 ("development.fix_needed", "exhaust_work_total"),
                 ("development.waiting_human", "resume_development"),
                 ("development.waiting_human", "park_contribution")),
                start="development.fix_needed",
                text="When `work_total` is exhausted at `development.fix_needed`, "
                     "`exhaust_work_total` waits for a human and names `work_total` in "
                     "`pending_human_action.counter`, whatever the repair budgets hold. The reply states the "
                     "number of Work allocations spent in this generation and names both "
                     "`/upstreaming resume` and `/upstreaming park`. One resume starts "
                     "`work_total` again and returns to `development.fix_needed`; the repair "
                     "budgets are not started again."),
            Diagram(
                "Failure case 6: development must be re-planned",
                ("development.waiting_ci", "development.fix_needed",
                 "development.waiting_human", "queued.idle", "planning.agent_running"),
                (("development.waiting_ci", "refresh_planning"),
                 ("development.fix_needed", "refresh_planning"),
                 ("development.waiting_human", "refresh_planning"),
                 ("planning.agent_running", "readmit_development")),
                text="`refresh` runs complete planning without deleting the fork or "
                     "development PR. A refreshed plan that decides to develop returns to "
                     "`development.fix_needed` on the existing fork."),
        ),
    ),
    DiagramSection(
        "Pause, waiting-human, and park cases",
        "Pause keeps the lifecycle phase and activity. `waiting_human` is entered automatically "
        "when a specific failure or external prerequisite needs operator action. Park means the "
        "approach is not working or a human said stop; it ends the current generation, and refresh "
        "from parked performs a clean reset. A Result is reused only while the pipeline ran "
        "continuously: after a pause, a human wait, or a park, the next step is fresh Work. A "
        "stop therefore clears the requested Work and the pending publication, so nothing is "
        "published after it.",
        (
            Diagram(
                "Success case 1: temporary pause and resume",
                ("development.waiting_ci", "development.waiting_ci+paused"),
                (("development.waiting_ci", "pause"),
                 ("development.waiting_ci", "pause_on_identity_change"),
                 ("development.waiting_ci", "pause_on_processing_error"),
                 ("development.waiting_ci+paused", "resume_pause")),
                start="development.waiting_ci",
                text="There is one flag, `paused`, with its reason and who set it. A human sets it "
                     "with `pause`, with an optional reason; the controller sets it when the "
                     "upstream identity changes, and after an unexpected error while it "
                     "processed the contribution, and always records the reason. `resume_pause` "
                     "bumps the revision. An effect that passed its final "
                     "publication check before the pause may finish and is reconciled normally."),
            Diagram(
                "Success case 2: pause while Work is requested",
                ("development.agent_running", "development.fix_needed+paused",
                 "development.fix_needed"),
                (("development.agent_running", "pause"),
                 ("development.fix_needed+paused", "resume_pause")),
                start="development.agent_running",
                text="Pause clears the requested Work, so the state in which that Work is due "
                     "again is entered. A Result that lands during the pause takes "
                     "`record_late_effect`; after the resume, fresh Work is requested."),
            Diagram(
                "Success case 3: resume after a long stop refreshes the plan",
                ("development.waiting_ci", "development.waiting_ci+paused",
                 "development.waiting_human", "development.fix_needed", "queued.idle"),
                (("development.waiting_ci", "pause"),
                 ("development.waiting_ci+paused", "resume_pause"),
                 ("development.waiting_ci+paused", "resume_stale"),
                 ("development.waiting_human", "resume_development"),
                 ("development.waiting_human", "resume_stale")),
                start="development.waiting_ci",
                text="In provisioning and development, a pause or human wait longer than "
                     "`refresh.stale_after_days` days resumes through `resume_stale`, which runs "
                     "the planning again. The normal resume verbs carry the complementary "
                     "guard, so exactly one applies. Handoff, upstream review, and release waits "
                     "are never refreshed automatically."),
            Diagram(
                "Failure case 1: waiting for operator action or an external prerequisite",
                ("development.agent_running", "development.waiting_human", "development.fix_needed",
                 "parked.idle"),
                (("development.agent_running", "exhaust_publish_recovery"),
                 ("development.waiting_human", "resume_development"),
                 ("development.waiting_human", "park_contribution")),
                start="development.agent_running",
                text="The controller records the concrete reason in `pending_human_action`. Correct "
                     "the stated problem first, then resume with the exact revision. A "
                     "contribution that is also paused needs two resumes: the first clears the "
                     "flag and its reply names the pending action, the second leaves the wait. "
                     "When an exhausted budget caused the wait, the resume starts again the one "
                     "counter named in `pending_human_action.counter`."),
            Diagram(
                "Failure case 2: the upstream base branch is missing or cannot be read",
                ("queued.idle", "planning.idle", "planning.waiting_human", "provisioning.idle",
                 "provisioning.waiting_human", "development.fix_needed",
                 "development.waiting_human", "handoff.review_due", "handoff.review_pending",
                 "handoff.waiting_human", "upstream_review.fix_needed",
                 "upstream_review.waiting_human"),
                (("queued.idle", "require_base_branch"),
                 ("queued.idle", "exhaust_source_reads"),
                 ("planning.idle", "require_base_branch"),
                 ("planning.idle", "exhaust_source_reads"),
                 ("planning.waiting_human", "resume_planning"),
                 ("provisioning.idle", "require_base_branch"),
                 ("provisioning.idle", "exhaust_source_reads"),
                 ("provisioning.waiting_human", "resume_provisioning"),
                 ("development.fix_needed", "require_base_branch"),
                 ("development.fix_needed", "exhaust_source_reads"),
                 ("development.waiting_human", "resume_development"),
                 ("handoff.review_due", "require_base_branch"),
                 ("handoff.review_due", "exhaust_source_reads"),
                 ("handoff.review_pending", "require_base_branch"),
                 ("handoff.review_pending", "exhaust_source_reads"),
                 ("handoff.waiting_human", "resume_submission_review"),
                 ("upstream_review.fix_needed", "require_base_branch"),
                 ("upstream_review.fix_needed", "exhaust_source_reads"),
                 ("upstream_review.waiting_human", "resume_upstream_revision"),
                 ("planning.waiting_human", "resume_upstream_read"),
                 ("provisioning.waiting_human", "resume_upstream_read"),
                 ("development.waiting_human", "resume_upstream_read"),
                 ("handoff.waiting_human", "resume_upstream_read"),
                 ("upstream_review.waiting_human", "resume_upstream_read")),
                text="Where Work or the fork provisioning is due, the collector reads the "
                     "upstream base branch. When GitHub answers that it does not exist, or "
                     "redirects it to the branch it was renamed to, the contribution waits for a "
                     "human in its phase at once; a read that fails otherwise (network, server "
                     "error, rate limit) is retried within `source_reads`, which counts at most "
                     "one failed read per spacing and is exhausted only once both its count and "
                     "its minimum time have passed, and its exhaustion leads to the same wait. "
                     "A wait that failed reads caused heals itself: the upstream is still read, "
                     "and `resume_upstream_read` ends the wait once it can be read again. A wait "
                     "the upstream's own answer caused is not read again until an authorized "
                     "resume is posted. A resume once "
                     "the branch exists again continues; to target another branch, the "
                     "contribution is parked first and then restarted with "
                     "`/upstreaming refresh branch=NAME`. In upstream review, a branch renamed "
                     "while the upstream PR is open is followed without a human (failure case 6 "
                     "of upstream review)."),
            Diagram(
                "Failure case 3: the upstream repository cannot be read",
                ("queued.idle", "planning.idle", "planning.waiting_human", "provisioning.idle",
                 "provisioning.waiting_human", "development.fix_needed",
                 "development.waiting_human", "handoff.review_due", "handoff.review_pending",
                 "handoff.waiting_human", "upstream_review.fix_needed",
                 "upstream_review.waiting_human"),
                (("queued.idle", "require_upstream_access"),
                 ("planning.idle", "require_upstream_access"),
                 ("planning.waiting_human", "resume_planning"),
                 ("provisioning.idle", "require_upstream_access"),
                 ("provisioning.waiting_human", "resume_provisioning"),
                 ("development.fix_needed", "require_upstream_access"),
                 ("development.waiting_human", "resume_development"),
                 ("handoff.review_due", "require_upstream_access"),
                 ("handoff.review_pending", "require_upstream_access"),
                 ("handoff.waiting_human", "resume_submission_review"),
                 ("upstream_review.fix_needed", "require_upstream_access"),
                 ("upstream_review.waiting_human", "resume_upstream_revision")),
                text="When GitHub answers for the recorded repository under another name "
                     "(renamed or transferred), the contribution waits at once with the pending "
                     "action `upstream_unavailable`; when most failed reads of the retry window "
                     "could not read the repository itself (the token lost access), the "
                     "exhaustion of `source_reads` leads to the same waits. Once the repository "
                     "can be read again, `resume_upstream_read` (drawn in failure case 2) or a "
                     "resume continues; a repository that moved is parked and abandoned, and a "
                     "new request names its new name."),
            Diagram(
                "Failure case 4: park, then restart as a clean generation or abandon",
                ("development.fix_needed", "parked.idle", "queued.idle", "closed.idle"),
                (("development.fix_needed", "park_contribution"),
                 ("parked.idle", "record_late_effect"),
                 ("parked.idle", "restart_generation"),
                 ("parked.idle", "abandon")),
                start="development.fix_needed", ends=("closed.idle",),
                text="`restart_generation` is accepted only when no operation is open, no "
                     "publication is pending, and no upstream PR is open. It bumps the generation, "
                     "clears mutable lifecycle state, and starts at `queued.idle`. Requested Work "
                     "carries its generation, so a Result of the ended generation is never folded "
                     "or published. Nothing here closes PRs, deletes branches, or deletes "
                     "repositories."),
        ),
    ),
    DiagramSection(
        "Handoff, upstream review, and release cases",
        "The submission request is bound to one exact revision and candidate head. Development "
        "happens on `riscv64-dev`, which the handoff never changes, and the development PR stays "
        "open. An uncredentialed review shapes the series and the pull request text after the "
        "project's guidelines; trusted code checks it, builds `riscv64` from it, and prepares "
        "the submission package. The Apps do not open the upstream PR.",
        (
            Diagram(
                "Success case 1: one command reviews the series, builds `riscv64`, and publishes "
                "a prefilled PR package that a human submits",
                ("development.waiting_ci", "handoff.awaiting_submission", "handoff.review_due",
                 "handoff.agent_running", "handoff.review_pending", "handoff.reviewing",
                 "handoff.building_upstream_branch",
                 "handoff.waiting_ci", "handoff.preparing_submission",
                 "handoff.waiting_upstream_pr", "upstream_review.waiting_ci"),
                (("development.waiting_ci", "nominate_for_handoff"),
                 ("handoff.awaiting_submission", "record_reminder"),
                 ("handoff.awaiting_submission", "submit_upstream"),
                 ("handoff.review_due", "request_submission_rewrite"),
                 ("handoff.agent_running", "accept_submission_rewrite"),
                 ("handoff.review_pending", "request_submission_review"),
                 ("handoff.reviewing", "accept_submission_review"),
                 ("handoff.building_upstream_branch", "confirm_clean_head"),
                 ("handoff.waiting_ci", "accept_clean_head_ci"),
                 ("handoff.preparing_submission", "confirm_submission_package"),
                 ("handoff.waiting_upstream_pr", "record_reminder"),
                 ("handoff.waiting_upstream_pr", "verify_upstream_pr")),
                text="`submit-upstream` is the only submission command. The rewrite Work reads "
                     "the plan's contribution guidelines and the non-debug commits of the "
                     "approved `riscv64-dev` series; the project's contributing instructions "
                     "take precedence over the pipeline's for content and style. It returns the "
                     "reshaped series and the upstream PR title and description written after "
                     "the pull request template. Trusted code checks that every commit of the "
                     "series is valid upstream, that its tree is the tree of the approved series "
                     "without its debug commits, the attribution trailers, the plan's "
                     "measurable commit rules, and the template's required sections. A separate "
                     "review Work, in a fresh context, reviews exactly that series and text "
                     "against the reviewers' expectations and blocks on failure. Trusted code "
                     "then creates `riscv64` from the series, or "
                     "replaces it under a lease, never while an upstream PR is open from it. The "
                     "human follows the prefilled link and opens the PR under their own "
                     "identity; observation verifies it before `upstream_review`. Both human "
                     "steps are reminded."),
            Diagram(
                "Failure case 1: pre-submission changes versus the locked upstream-PR wait",
                ("handoff.awaiting_submission", "handoff.review_due", "handoff.agent_running",
                 "handoff.review_pending", "handoff.reviewing",
                 "handoff.building_upstream_branch", "handoff.waiting_ci",
                 "handoff.waiting_upstream_pr", "development.fix_needed", "development.waiting_ci",
                 "queued.idle"),
                (("handoff.awaiting_submission", "submit_upstream"),
                 ("handoff.review_due", "request_submission_rewrite"),
                 ("handoff.agent_running", "accept_submission_rewrite"),
                 ("handoff.review_pending", "request_submission_review"),
                 ("handoff.reviewing", "accept_submission_review"),
                 ("handoff.building_upstream_branch", "confirm_clean_head"),
                 ("handoff.waiting_ci", "rerun_interrupted_ci"),
                 ("handoff.awaiting_submission", "return_for_repair"),
                 ("handoff.awaiting_submission", "record_actionable_review"),
                 ("development.fix_needed", "record_actionable_review"),
                 ("handoff.awaiting_submission", "record_explanation"),
                 ("handoff.awaiting_submission", "record_head_change"),
                 ("handoff.agent_running", "reject_submission_rewrite"),
                 ("handoff.reviewing", "reject_submission_review"),
                 ("handoff.building_upstream_branch", "return_for_repair"),
                 ("handoff.waiting_ci", "return_for_repair"),
                 ("handoff.awaiting_submission", "refresh_planning"),
                 ("handoff.waiting_upstream_pr", "record_audit_observation"),
                 ("handoff.waiting_upstream_pr", "retract_submission"),
                 ("handoff.waiting_upstream_pr", "refresh_planning")),
                start="handoff.awaiting_submission",
                text="Before the package is posted, a changed candidate head, a rewrite that "
                     "fails the trusted check, a failed review, a reviewed series that does not rebuild, a "
                     "lease conflict on `riscv64`, or blocking CI on the clean head returns to "
                     "development; blocking CI on the clean head records its failing evidence, "
                     "which names the candidate head it was built from, so the repair names the "
                     "failing job; a failed review records its reasons as the pending feedback "
                     "of the next development Work, and nothing is posted. At the gate, a CI "
                     "failure on the candidate head takes `return_for_repair` and records the "
                     "failing evidence; an actionable review takes `record_actionable_review` "
                     "and records the feedback; the guards are exclusive, and the failure applies "
                     "first; a review observed with the failure is then recorded in "
                     "`development.fix_needed`. An explanation-only comment takes `record_explanation` and keeps the "
                     "gate. `handoff.waiting_upstream_pr` is locked: only "
                     "`verify_upstream_pr`, `retract_submission`, and `refresh_planning` "
                     "leave it, and none of them changes `riscv64`."),
            Diagram(
                "Failure case 2: a handoff step fails repeatedly or needs a human",
                ("handoff.review_due", "handoff.agent_running", "handoff.review_pending",
                 "handoff.reviewing", "handoff.building_upstream_branch",
                 "handoff.waiting_ci", "handoff.preparing_submission", "handoff.waiting_human",
                 "handoff.waiting_upstream_pr"),
                (("handoff.review_due", "exhaust_work_total"),
                 ("handoff.agent_running", "retry_dispatch"),
                 ("handoff.agent_running", "exhaust_dispatch_recovery"),
                 ("handoff.agent_running", "allocate_work_retry"),
                 ("handoff.agent_running", "exhaust_work_recovery"),
                 ("handoff.waiting_human", "resume_submission_review"),
                 ("handoff.waiting_human", "rearm_dispatch"),
                 ("handoff.review_due", "request_submission_rewrite"),
                 ("handoff.agent_running", "accept_submission_rewrite"),
                 ("handoff.review_pending", "expire_submission_rewrite"),
                 ("handoff.review_pending", "exhaust_work_total"),
                 ("handoff.review_pending", "request_submission_review"),
                 ("handoff.reviewing", "retry_dispatch"),
                 ("handoff.reviewing", "exhaust_dispatch_recovery"),
                 ("handoff.reviewing", "allocate_work_retry"),
                 ("handoff.reviewing", "exhaust_work_recovery"),
                 ("handoff.reviewing", "accept_submission_review"),
                 ("handoff.building_upstream_branch", "expire_submission_rewrite"),
                 ("handoff.building_upstream_branch", "retry_upstream_branch"),
                 ("handoff.building_upstream_branch", "exhaust_upstream_branch_retries"),
                 ("handoff.building_upstream_branch", "require_branch_intervention"),
                 ("handoff.waiting_human", "resume_upstream_branch"),
                 ("handoff.building_upstream_branch", "confirm_clean_head"),
                 ("handoff.waiting_ci", "exhaust_interrupted_reruns"),
                 ("handoff.waiting_human", "resume_ci"),
                 ("handoff.waiting_ci", "require_clean_head_ci"),
                 ("handoff.waiting_human", "resume_clean_head_ci"),
                 ("handoff.waiting_ci", "accept_clean_head_ci"),
                 ("handoff.preparing_submission", "retry_submission_comment"),
                 ("handoff.preparing_submission", "exhaust_comment_retries"),
                 ("handoff.waiting_human", "resume_submission_preparation"),
                 ("handoff.preparing_submission", "confirm_submission_package")),
                start="handoff.review_due",
                text="The rewrite and review Work, the lease write of `riscv64`, the reruns of "
                     "interrupted CI, and the package each retry within their budget and then "
                     "wait for a human in the one `handoff.waiting_human`; a pull request that "
                     "is open from `riscv64`, or a missing `riscv64-dev`, waits there at once. "
                     "A rewritten series whose artifact expires before `riscv64` is built "
                     "leads back to a fresh rewrite, never to an endless wait. `pending_human_action` names the step, and "
                     "exactly one of the five resume verbs applies: it returns to that step. CI that "
                     "never runs on the clean head of `riscv64` waits there too, after a bounded "
                     "wait. "
                     "Recovery does not need another `submit-upstream`."),
            Diagram(
                "Success case 2: upstream review, merge, and release",
                ("upstream_review.waiting_ci", "release_wait.idle", "released.idle"),
                (("upstream_review.waiting_ci", "record_merge"),
                 ("release_wait.idle", "verify_release")),
                start="upstream_review.waiting_ci", ends=("released.idle",),
                text="Merge enters `release_wait`; it is not terminal success. Only verified "
                     "release coverage enters `released`."),
            Diagram(
                "Failure case 3: upstream CI or actionable review needs a revision",
                ("upstream_review.waiting_ci", "upstream_review.fix_needed",
                 "upstream_review.agent_running", "upstream_review.waiting_human"),
                (("upstream_review.waiting_ci", "record_upstream_ci_failure"),
                 ("upstream_review.waiting_ci", "record_actionable_review"),
                 ("upstream_review.waiting_ci", "record_explanation"),
                 ("upstream_review.fix_needed", "request_upstream_revision"),
                 ("upstream_review.fix_needed", "request_review_revision"),
                 ("upstream_review.agent_running", "record_revision_publish_success"),
                 ("upstream_review.agent_running", "reject_patch_proposal"),
                 ("upstream_review.agent_running", "require_reviewer_answer"),
                 ("upstream_review.fix_needed", "exhaust_repair_attempts"),
                 ("upstream_review.waiting_human", "resume_upstream_review"),
                 ("upstream_review.waiting_human", "resume_upstream_revision")),
                start="upstream_review.waiting_ci",
                text="Automated upstream revisions require that the upstream PR head is the "
                     "controlled fork ref. `request_upstream_revision` applies while a failing "
                     "CI job is recorded for the head and charges `repair_attempts_cluster`; "
                     "`request_review_revision` applies to a revision driven only by review "
                     "feedback and charges `work_total` only. A published revision clears "
                     "exactly the feedback its Work carried. A rejected proposal is repaired within `no_progress`. "
                     "When the worker reports that no change is needed, a human answers the "
                     "reviewer; `resume_upstream_review` then clears the pending feedback and "
                     "returns to `upstream_review.waiting_ci` without requesting Work. After a "
                     "budget is exhausted, `resume_upstream_revision` starts again the one "
                     "counter named in `pending_human_action.counter`. A CI failure is recorded "
                     "once per head and CI run. Upstream CI is never rerun by the pipeline."),
            Diagram(
                "Failure case 4: revision Work or its publication fails",
                ("upstream_review.agent_running", "upstream_review.waiting_human",
                 "upstream_review.fix_needed"),
                (("upstream_review.agent_running", "retry_dispatch"),
                 ("upstream_review.agent_running", "allocate_work_retry"),
                 ("upstream_review.agent_running", "schedule_publish_retry"),
                 ("upstream_review.agent_running", "exhaust_dispatch_recovery"),
                 ("upstream_review.agent_running", "exhaust_work_recovery"),
                 ("upstream_review.agent_running", "exhaust_publish_recovery"),
                 ("upstream_review.waiting_human", "resume_upstream_revision"),
                 ("upstream_review.waiting_human", "rearm_dispatch")),
                start="upstream_review.agent_running",
                text="Every exhaustion in upstream review waits for a human in "
                     "`upstream_review.waiting_human`; nothing parks from upstream review "
                     "automatically."),
            Diagram(
                "Failure case 5: the upstream PR is closed or its head changes",
                ("upstream_review.waiting_ci", "upstream_review.waiting_human", "parked.idle"),
                (("upstream_review.waiting_ci", "record_pr_closed_unmerged"),
                 ("upstream_review.waiting_ci", "record_head_change"),
                 ("upstream_review.waiting_human", "resume_upstream_review"),
                 ("upstream_review.waiting_human", "park_contribution")),
                start="upstream_review.waiting_ci",
                text="The pipeline does not reopen the PR or create a replacement. "
                     "`resume_upstream_review` returns to `upstream_review.waiting_ci` after a "
                     "head change, and after a closed PR once it is observed open again. "
                     "`resume_upstream_revision` applies only when revision Work failed. Human "
                     "triage either resumes or parks."),
            Diagram(
                "Failure case 6: the base branch is renamed while the upstream PR is open",
                ("upstream_review.waiting_ci", "upstream_review.fix_needed",
                 "upstream_review.waiting_human"),
                (("upstream_review.waiting_ci", "follow_renamed_base"),
                 ("upstream_review.fix_needed", "follow_renamed_base"),
                 ("upstream_review.waiting_human", "resume_renamed_base")),
                start="upstream_review.waiting_ci",
                text="GitHub retargets an open PR when its base branch is renamed. Once the "
                     "verified upstream PR targets the new name and that branch still contains "
                     "the recorded base commit, the record follows the rename and the tracking "
                     "issue states it. A contribution that already waits because of the rename "
                     "leaves the wait the same way, without a resume."),
            Diagram(
                "Failure case 7: release is missing, incomplete, or waiting for operator action",
                ("release_wait.idle", "release_wait.waiting_human", "released.idle"),
                (("release_wait.idle", "record_release_incomplete"),
                 ("release_wait.idle", "require_release_intervention"),
                 ("release_wait.waiting_human", "resume_release_observation"),
                 ("release_wait.idle", "verify_release")),
                start="release_wait.idle",
                text="Release observation waits without invoking a worker until the evidence "
                     "satisfies the frozen acceptance contract."),
        ),
    ),
    DiagramSection(
        "Rules that apply in many states",
        "Five transitions apply in many states. Each is written once as a rule and expanded into "
        "the table; each is drawn once here, from a single node that stands for every state the "
        "rule applies in.",
        (
            Diagram(
                "Rule: park by command", ("parked.idle",), (), rule="park_contribution",
                text="Nothing parks from `upstream_review` automatically, and "
                     "`handoff.waiting_upstream_pr` and `release_wait` cannot be parked."),
            Diagram(
                "Rule: resume after a long stop", ("queued.idle",), (), rule="resume_stale"),
            Diagram(
                "Rule: reminders of a pending human step", (), (), rule="record_reminder",
                text="The rule covers the `gate`, `human_wait`, and `locked` kinds. In "
                     "`handoff.waiting_upstream_pr` the reminder text carries the package link."),
            Diagram(
                "Rule: late Results", (), (), rule="record_late_effect"),
            Diagram(
                "Rule: the upstream base branch cannot be read", (), (),
                rule="record_source_unavailable",
                text="A read that fails without a definitive answer charges `source_reads` and "
                     "keeps the state, at most once per spacing; its exhaustion takes "
                     "`exhaust_source_reads`, or `require_upstream_access` when most failed "
                     "reads could not read the repository itself, drawn with the human waits."),
        ),
    ),
)

OPERATION_SECTION = (
    "Remote operations",
    "Every remote effect is an operation with its own small table, held to the same checks. The "
    "intent is recorded before the effect, and the receipt is confirmed by discovering the actual "
    "resource. While an operation is open (`planned`, `applying`, `uncertain`) it fences its "
    "contribution: no park, no refresh, and no second writer. `/upstreaming resolve-operation` "
    "closes an open operation with a resolution evidence record.",
)

# Short node ids used when drawing states (``phase_activity`` otherwise).
DIAGRAM_PHASE_IDS = {"upstream_review": "upstream"}


# --------------------------------------------------------------------------
# Consistency check of the tables, run in CI as ``python -m lifecycle check``.

# The revision rule. A transition bumps the revision when it is a resume,
# when it starts a generation or a fresh Work graph, when it changes a field
# that defines the requested or approved work, or when a Result is rejected
# and the same kind of Work is due again. No other transition bumps it.
# Setting a field in ``REVISION_SET_FIELDS`` or clearing one in
# ``REVISION_CLEAR_FIELDS`` changes the requested or approved work; so does
# a recorded head (``REVISION_OBSERVED_FIELDS`` set by an observation, or by
# a Result that read the head back).
REVISION_SET_FIELDS = ("scope_paths", "pending_feedback_evidence_ids", "refresh")
REVISION_CLEAR_FIELDS = ("scope_paths", "plan_evidence_id", "handoff_request",
                         "handoff_approval", "clean_head_sha")
REVISION_OBSERVED_FIELDS = ("fork.head_sha", "upstream_pr.head_sha")
_REVISION_GUARDS = (EXACT_REVISION, ANY_REVISION)

# The exits of the locked upstream-PR wait.
LOCKED_EXITS = {"handoff.waiting_upstream_pr": ("verify_upstream_pr", "retract_submission",
                                                "refresh_planning")}
# Phases from which only a command may end the generation.
NO_AUTOMATIC_PARK = ("upstream_review",)
# The steps every submission passes, in order, from the gate to the locked
# upstream-PR wait: the review Work is due and runs, `riscv64` is built from
# its reviewed series, CI runs on its head, and the package is posted. The
# one state that writes the fork in the handoff phase writes `riscv64`; the
# review's Result leaves for that state, or returns to development with the
# reasons as pending feedback.
SUBMISSION_GATE = "handoff.awaiting_submission"
SUBMISSION_STEPS = ("handoff.review_due", "handoff.agent_running", "handoff.review_pending",
                    "handoff.reviewing", "handoff.building_upstream_branch", "handoff.waiting_ci",
                    "handoff.preparing_submission", "handoff.waiting_upstream_pr")
# The rewrite Work and the review Work, and where a Result of each that
# passes leads.
SUBMISSION_REWRITE = "handoff.agent_running"
SUBMISSION_REVIEW = "handoff.reviewing"
SUBMISSION_WORK = {SUBMISSION_REWRITE: "handoff.review_pending",
                   SUBMISSION_REVIEW: "handoff.building_upstream_branch"}
UPSTREAM_BRANCH_WRITER = "handoff.building_upstream_branch"
# The one resume that clears pending feedback: a human answered it.
FEEDBACK_ANSWERED_BY = ("resume_upstream_review",)


def _field_name(entry: str) -> str:
    return entry.split("=", 1)[0]


def _reachable(start: str, step) -> set[str]:
    seen, todo = {start}, [start]
    while todo:
        for nxt in step(todo.pop()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


# Tick transitions that end a human wait the way its resume does, once the
# controller has observed that the cause is gone. The checks hold them to the
# rules of a resume.
AUTOMATIC_RESUMES = frozenset({"rearm_dispatch", "resume_renamed_base", "resume_upstream_read"})


def resumes_wait(edge: Edge) -> bool:
    """Whether ``edge`` is the resume of a human wait, posted or automatic."""
    return edge.transition.command == RESUME or edge.verb in AUTOMATIC_RESUMES


def must_bump(edge: Edge) -> bool:
    """Whether the revision rule requires ``edge`` to bump the revision."""
    t, e = edge.transition, edge.transition.effects
    sets = {_field_name(f) for f in e.sets}
    clears = {_field_name(f) for f in e.clears}
    if resumes_wait(edge) or e.generation == "bump" or e.work == "reallocate":
        return True
    if sets & set(REVISION_SET_FIELDS) or clears & set(REVISION_CLEAR_FIELDS):
        return True
    if t.trigger in (Trigger.OBSERVATION, Trigger.RESULT) and sets & set(REVISION_OBSERVED_FIELDS):
        return True
    target = LIFECYCLE.get(edge.target)
    return bool(t.trigger == Trigger.RESULT and e.work == "clear" and edge.target != edge.source
                and target is not None and target.kind == "auto_wait" and target.launches)


def _check_transition(where: str, t: Transition, problems: list[str], *,
                      command_prefix: str = "/upstreaming ",
                      command_guards: tuple[str, ...] = _REVISION_GUARDS) -> None:
    if not isinstance(t.trigger, Trigger):
        problems.append(f"{where}: trigger {t.trigger!r} is not a Trigger")
    if not t.guard:
        problems.append(f"{where}: empty guard; write ({ALWAYS!r},) for an unconditional transition")
    elif ALWAYS in t.guard and len(t.guard) > 1:
        problems.append(f"{where}: {ALWAYS!r} combined with other conditions")
    if any(not g.strip() for g in t.guard):
        problems.append(f"{where}: blank guard condition")
    if t.trigger == Trigger.COMMAND:
        if not (t.command or "").startswith(command_prefix):
            problems.append(f"{where}: command trigger without an `/upstreaming` command")
        if not any(g in command_guards for g in t.guard):
            problems.append(f"{where}: command guard names none of "
                            + ", ".join(repr(g) for g in command_guards))
    elif t.command:
        problems.append(f"{where}: command {t.command!r} on a {t.trigger} trigger")
    e = t.effects
    if e.generation == "bump" and (e.revision != "bump" or e.work != "clear"):
        problems.append(f"{where}: a generation bump must bump the revision and clear Work")
    if e.work == "allocate" and NOT_PAUSED not in t.guard:
        problems.append(f"{where}: allocates Work without the guard {NOT_PAUSED!r}")
    guard_text = "; ".join(t.guard)
    for name in t.charges:
        if name not in BUDGETS:
            problems.append(f"{where}: charges unknown budget {name!r}")
        elif name not in guard_text:
            problems.append(f"{where}: charges {name!r} but its guard does not mention it")


def _check_graph(name: str, table: dict, initial: str, is_end, problems: list[str]) -> bool:
    """Structure of one table: targets, end states, reachability, and one
    target per verb. Returns False when targets are missing."""
    label = f"{name} " if name else ""
    if initial not in table:
        problems.append(f"{label}initial state {initial!r} is not a table state")
        return False
    ends = {k for k in table if is_end(k)}
    if not ends:
        problems.append(f"{label}no state is marked as an end state")
    ok = True
    for key, state in table.items():
        targets: dict[str, list[str]] = {}
        for verb, to in state.transitions:
            if not verb:
                problems.append(f"{label}{key}: transition with an empty verb")
            if to != SELF and to not in table:
                problems.append(f"{label}{key} --{verb}--> {to}: target is not a table state")
                ok = False
            targets.setdefault(verb, []).append(resolve_target(key, to))
        for verb, to in targets.items():
            if len(to) > 1:
                problems.append(f"{label}{key} --{verb}-->: the verb leads to {len(to)} targets "
                                f"({', '.join(to)}); a verb from a state must lead to one target")
        leaving = [v for v, to in state.transitions if resolve_target(key, to) != key]
        if key in ends and leaving:
            problems.append(f"{label}{key}: end state has outgoing transitions")
        if key not in ends and not leaving:
            problems.append(f"{label}{key}: not an end state but no transition leaves it")
    if not ok:
        return False
    successors: dict[str, set[str]] = {k: set() for k in table}
    predecessors: dict[str, set[str]] = {k: set() for k in table}
    for e in table_edges(table):
        successors[e.source].add(e.target)
        predecessors[e.target].add(e.source)
    reached = _reachable(initial, successors.__getitem__)
    for key in table:
        if key not in reached:
            kind = "end state" if key in ends else "state"
            problems.append(f"{label}{key}: {kind} is not reachable from {initial}")
    can_end: set[str] = set()
    for end in ends:
        can_end |= _reachable(end, predecessors.__getitem__)
    for key in table:
        if key not in can_end:
            problems.append(f"{label}{key}: no end state is reachable from this state")
    return True


def _check_states(problems: list[str]) -> None:
    for key, state in LIFECYCLE.items():
        phase, _, activity = key.partition(".")
        if not phase or not activity or "." in activity:
            problems.append(f"{key}: state key is not phase.activity")
        if state.kind == "terminal" and (state.launches or state.writes):
            problems.append(f"{key}: end state launches or writes")
        if state.kind in ("locked", "human_wait", "generation_end") and (state.launches or state.writes):
            problems.append(f"{key}: {state.kind} state launches or writes")
        if state.kind == "human_wait" and not any(
                t.command == RESUME for t in state.transitions.values()):
            problems.append(f"{key}: {state.kind} state has no `{RESUME}` exit")
        if state.kind == "generation_end" and not any(
                t.command == REFRESH for t in state.transitions.values()):
            problems.append(f"{key}: {state.kind} state has no `{REFRESH}` exit")
        if state.kind == "generation_end" and any(
                t.command == RESUME for t in state.transitions.values()):
            problems.append(f"{key}: {state.kind} state has a `{RESUME}` row; a clean "
                            f"restart is requested with `{REFRESH}`")
        if state.kind == "gate" and not any(
                t.trigger == Trigger.COMMAND for t in state.transitions.values()):
            problems.append(f"{key}: gate state has no command exit")
        for e in state_edges(key):
            where = f"{key} --{e.verb}--> {e.target}"
            _check_transition(where, e.transition, problems)
            effects = e.transition.effects
            if effects.work == "allocate" and not state.launches:
                problems.append(f"{where}: allocates Work from a state that does not launch")
            if effects.work in ("allocate", "reallocate") and "work_total" not in e.transition.charges:
                problems.append(f"{where}: allocates Work without charging 'work_total'")
            if must_bump(e) and effects.revision != "bump":
                problems.append(f"{where}: the revision rule requires a revision bump")
            if not must_bump(e) and effects.revision == "bump":
                problems.append(f"{where}: bumps the revision although the revision rule "
                                "does not require it")
            target = LIFECYCLE[e.target]
            entering = e.target != key
            sets = {_field_name(f).split(".", 1)[0] for f in effects.sets}
            if entering and target.kind == "human_wait" and state.kind != "human_wait" \
                    and "pending_human_action" not in sets:
                problems.append(f"{where}: enters a human wait without setting pending_human_action")
            if entering and target.kind == "generation_end":
                if not {"parked_reason", "previous_phase"} <= sets:
                    problems.append(f"{where}: ends the generation without parked_reason and "
                                    "previous_phase")
                if state_phase(key) in NO_AUTOMATIC_PARK and e.transition.trigger != Trigger.COMMAND:
                    problems.append(f"{where}: ends the generation from {state_phase(key)} "
                                    "without a command")
    for key, verbs in LOCKED_EXITS.items():
        state = LIFECYCLE.get(key)
        if state is None or state.kind != "locked":
            problems.append(f"{key}: LOCKED_EXITS names a state that is not locked")
            continue
        exits = [e.verb for e in state_edges(key) if e.target != key]
        if sorted(exits) != sorted(verbs):
            problems.append(f"{key}: exits {sorted(exits)} differ from the {len(verbs)} "
                            f"allowed exits {sorted(verbs)}")
        guards = {e.transition.guard for e in state_edges(key)
                  if e.target != key and e.transition.trigger == Trigger.COMMAND}
        if len(guards) > 1:
            problems.append(f"{key}: the command exits of a locked state must share one guard")
    for key, state in LIFECYCLE.items():
        if state.kind == "locked" and key not in LOCKED_EXITS:
            problems.append(f"{key}: locked state without LOCKED_EXITS")


def _check_rules(problems: list[str]) -> None:
    for verb, rule in RULES.items():
        where = f"rule {verb}"
        if rule.target != SELF and rule.target not in LIFECYCLE:
            problems.append(f"{where}: target {rule.target!r} is not a table state")
            continue
        _check_transition(where, rule.transition, problems)
        if not rule.applies.strip():
            problems.append(f"{where}: no description of where it applies")
        expected = set(rule_states(verb))
        if not expected:
            problems.append(f"{where}: applies in no state")
        rows = {e.source for e in table_edges() if is_rule_row(e)
                and e.target == resolve_target(e.source, rule.target) and e.verb == verb}
        for key in sorted(expected - rows):
            problems.append(f"{where}: applies in {key} but the table has no such row")
        for key in sorted(rows - expected):
            problems.append(f"{where}: the table has a row in {key} where the rule does not apply")
    # Reminders: every state that waits for a human command is reminded.
    reminder = RULES.get("record_reminder")
    if reminder is None or set(reminder.kinds) != set(REMINDER_KINDS) or reminder.states \
            or reminder.exclude or reminder.target != SELF:
        problems.append("rule record_reminder: must apply in every gate, human_wait, and locked "
                        "state and keep the state")
    for key, state in LIFECYCLE.items():
        if state.kind in REMINDER_KINDS and not any(
                v == "record_reminder" for v, _to in state.transitions):
            problems.append(f"{key}: {state.kind} state without record_reminder")
    for e in table_edges():
        if e.verb != "record_reminder":
            continue
        if REMINDER_KEY not in "; ".join(e.transition.guard):
            problems.append(f"{e.source} --record_reminder-->: guard does not read {REMINDER_KEY}")
        if not is_rule_row(e):
            problems.append(f"{e.source} --record_reminder-->: a reminder row beside the rule")
    # Late Results: recorded in every state, never changing it.
    late = RULES.get("record_late_effect")
    if late is None or late.target != SELF or late.kinds or late.states or late.exclude \
            or late.transition.effects != Effects():
        problems.append("rule record_late_effect: must apply in every state, keep the state, "
                        "and have no effects")
    # Park: one command rule; nothing else is a command into the generation end.
    park = RULES.get("park_contribution")
    if park is None or park.transition.command != PARK or "no open operation" not in park.transition.guard:
        problems.append(f"rule park_contribution: must be the `{PARK}` command guarded by "
                        "'no open operation'")


def stop_transitions():
    """Yield ``(where, transition)`` for every stop: a transition that sets a
    flag, or that enters a state in which the generation has ended."""
    for name, flag in FLAGS.items():
        for verb, t in flag.set_by.items():
            yield f"flag {name} {verb}", t
    for e in table_edges():
        if e.source != e.target and LIFECYCLE[e.target].kind in STOP_KINDS:
            yield f"{e.source} --{e.verb}-->", e.transition


def _check_stops(problems: list[str]) -> None:
    """A stop invalidates what is held: every stop that clears the requested
    Work, and every stop by command, also clears the pending publication."""
    for where, t in stop_transitions():
        if t.effects.work != "clear" and t.trigger != Trigger.COMMAND:
            continue
        for held in _HELD:
            if held not in t.effects.clears:
                problems.append(f"{where}: a stop that clears the requested Work, or a stop by "
                                f"command, must also clear {held}")


def _is_patch_work(edge: Edge) -> bool:
    """Whether ``edge`` allocates Work that writes the fork."""
    return edge.transition.effects.work == "allocate" and LIFECYCLE[edge.source].writes


def _check_feedback(problems: list[str]) -> None:
    """Pending feedback: a Work request carries the ids present at
    allocation, a published Result clears exactly those, and nothing else
    clears pending feedback but the resume after a human answered it. Work
    that carries feedback is never answered by a CI rerun."""
    field = QUALIFIED_CLEARS[CARRIED_FEEDBACK][0]
    for e in table_edges():
        where = f"{e.source} --{e.verb}--> {e.target}"
        t, effects = e.transition, e.transition.effects
        carries = WORK_FEEDBACK in effects.sets
        if carries != _is_patch_work(e):
            problems.append(f"{where}: exactly the rows that allocate patch or revision Work "
                            f"set {WORK_FEEDBACK}")
        published = t.trigger == Trigger.RESULT and "proposal_artifact" in effects.sets
        if published != (CARRIED_FEEDBACK in effects.clears):
            problems.append(f"{where}: exactly the rows that record a published Result clear "
                            f"the {CARRIED_FEEDBACK}")
        if field in effects.clears and e.verb not in FEEDBACK_ANSWERED_BY:
            problems.append(f"{where}: clears {field}; only a published Result clears the "
                            f"feedback its Work carried, and {_one_of(FEEDBACK_ANSWERED_BY)} "
                            "clears feedback a human answered")
        if "flaky_reruns" in t.charges:
            if WORK_WITHOUT_FEEDBACK not in t.guard:
                problems.append(f"{where}: a CI rerun without the guard {WORK_WITHOUT_FEEDBACK!r}")
            if not any("the Work carries feedback" in g for x in state_edges(e.source)
                       if x.verb in BUDGETS["flaky_reruns"].exhaustion_verbs
                       for g in x.transition.guard):
                problems.append(f"{where}: no row of {e.source} takes the same outcome of Work "
                                "that carries feedback")
    for entry, (name, meaning) in QUALIFIED_CLEARS.items():
        if not entry.endswith(f" {name}") or not meaning.strip():
            problems.append(f"qualified clear {entry!r}: must end with its field and state "
                            "what it clears")
        if not any(entry in e.transition.effects.clears for e in table_edges()):
            problems.append(f"qualified clear {entry!r}: no transition uses it")
    # A nomination blocked by pending feedback always has a row that makes
    # Work carrying that feedback due, so passing CI never strands it.
    for state in sorted({e.source for e in table_edges()
                         if NO_PENDING_FEEDBACK in e.transition.guard}):
        if not any(PENDING_FEEDBACK in x.transition.guard
                   and x.transition.trigger == Trigger.OBSERVATION
                   and LIFECYCLE[x.target].launches
                   and any(_is_patch_work(w) and WORK_FEEDBACK in w.transition.effects.sets
                           for w in state_edges(x.target))
                   for x in state_edges(state)):
            problems.append(f"{state}: blocks a nomination on {NO_PENDING_FEEDBACK!r} without an "
                            f"observation row guarded by {PENDING_FEEDBACK!r} that makes Work "
                            "carrying the feedback due")


# The guards of `request_patch`: write admission and the guards every Work
# allocation of development shares; no proposal or scope admission.
REQUEST_PATCH_GUARDS = frozenset({NO_FAILING_JOB, "write admission", "worker slot free",
                                  "capacity available", NO_PROGRESS_REMAINS, WORK_TOTAL_REMAINS,
                                  NOT_PAUSED, UPSTREAM_BOUND})
CONTROLLED_REF = "the upstream PR head is the controlled fork ref"


def _check_settled_rows(problems: list[str]) -> None:
    """Rows whose shape a settled decision fixes beyond the general rules:
    review feedback is recorded as pending feedback and never as CI
    evidence; a CI failure at the gate or on the clean head records its
    evidence and no feedback and leaves the handoff;
    an explanation keeps the CI evidence; a feedback-only upstream revision
    charges no `no_progress` and keeps the controlled-ref guard; the first
    patch is admitted by write admission only; the resume of development
    does not reuse a held publication."""
    def row(state: str, verb: str) -> Transition | None:
        try:
            return find_edge(state, verb).transition
        except KeyError:
            problems.append(f"{state}: the settled row {verb} is missing")
            return None

    def touched(t: Transition) -> set[str]:
        return {_field_name(x) for x in t.effects.sets} | set(t.effects.clears)

    gate = "handoff.awaiting_submission"
    for state in (gate, "development.waiting_ci", "development.fix_needed"):
        t = row(state, "record_actionable_review")
        where = f"{state} --record_actionable_review-->"
        if t is None:
            continue
        if "pending_feedback_evidence_ids" not in {_field_name(x) for x in t.effects.sets}:
            problems.append(f"{where}: an actionable review is recorded as "
                            "pending_feedback_evidence_ids")
        if "last_ci_evidence_id" in touched(t):
            problems.append(f"{where}: review feedback is never CI evidence; the row keeps "
                            "last_ci_evidence_id")
        if state == gate and not set(_LEAVE_HANDOFF) <= set(t.effects.clears):
            problems.append(f"{where}: leaving the gate clears {', '.join(_LEAVE_HANDOFF)}")
    for state in (gate, "handoff.waiting_ci"):
        t = row(state, "return_for_repair")
        if t is None:
            continue
        where = f"{state} --return_for_repair-->"
        sets = {_field_name(x) for x in t.effects.sets}
        if "last_ci_evidence_id" not in sets:
            problems.append(f"{where}: a CI failure records its evidence in "
                            "last_ci_evidence_id")
        if "pending_feedback_evidence_ids" in sets:
            problems.append(f"{where}: a CI failure records no pending feedback")
        if not set(_LEAVE_HANDOFF) <= set(t.effects.clears):
            problems.append(f"{where}: leaving the handoff clears {', '.join(_LEAVE_HANDOFF)}")
    t = row(gate, "record_explanation")
    if t is not None and "last_ci_evidence_id" in touched(t):
        problems.append(f"{gate} --record_explanation-->: an explanation keeps "
                        "last_ci_evidence_id")
    t = row("upstream_review.fix_needed", "request_review_revision")
    if t is not None:
        where = "upstream_review.fix_needed --request_review_revision-->"
        if "no_progress" in t.charges:
            problems.append(f"{where}: a revision driven by review feedback charges no "
                            "no_progress")
        if CONTROLLED_REF not in t.guard:
            problems.append(f"{where}: requires {CONTROLLED_REF!r}")
    t = row("development.fix_needed", "request_patch")
    if t is not None and set(t.guard) - REQUEST_PATCH_GUARDS:
        extra = ", ".join(sorted(set(t.guard) - REQUEST_PATCH_GUARDS))
        problems.append(f"development.fix_needed --request_patch-->: patch Work is admitted by "
                        f"write admission only, not by {extra}")
    t = row("development.waiting_human", "resume_development")
    if t is not None and HELD_RESULT not in t.effects.clears:
        problems.append(f"development.waiting_human --resume_development-->: fresh Work is "
                        f"requested; the row clears {HELD_RESULT}")


def _check_reruns(problems: list[str]) -> None:
    """Rerun counts are kept for the current head: exactly the rows that
    record a new head start them again, and both rerun budgets count there."""
    for e in table_edges():
        if (RERUNS in e.transition.effects.sets) != (e.verb in RERUN_RESTART_VERBS):
            problems.append(f"{e.source} --{e.verb}--> {e.target}: exactly the rows of "
                            f"{_one_of(RERUN_RESTART_VERBS)} set {RERUNS}")
    verbs = {e.verb for e in table_edges()}
    for verb in RERUN_RESTART_VERBS:
        if verb not in verbs:
            problems.append(f"{RERUNS}: {verb} is not a table verb")
    for name in ("flaky_reruns", "interrupted_reruns"):
        budget = BUDGETS.get(name)
        if budget is None or f"`{RERUNS}." not in budget.counter:
            problems.append(f"budget {name}: its counter is not a field of {RERUNS}")


def _check_patch_work(problems: list[str]) -> None:
    """Patch and revision Work is requested only where Work is due on an
    existing fork, under write admission; ``repair_attempts_cluster`` is
    charged only for a failing CI job; every such request requires that
    ``no_progress`` remains. The patch scope is checked at admission."""
    for e in table_edges():
        if not _is_patch_work(e):
            if "repair_attempts_cluster" in e.transition.charges:
                problems.append(f"{e.source} --{e.verb}-->: charges repair_attempts_cluster "
                                "without allocating patch Work")
            continue
        where = f"{e.source} --{e.verb}--> {e.target}"
        guard = e.transition.guard
        source = LIFECYCLE[e.source]
        if not (source.kind == "auto_wait" and source.launches and e.source.endswith(".fix_needed")):
            problems.append(f"{where}: patch Work is requested only from a fix_needed state")
        if state_phase(e.source) == "development" and "write admission" not in guard:
            problems.append(f"{where}: patch Work without the guard 'write admission'")
        charged = "repair_attempts_cluster" in e.transition.charges
        if charged != (FAILING_JOB in guard) or charged == (NO_FAILING_JOB in guard):
            problems.append(f"{where}: repair_attempts_cluster is charged exactly when the "
                            f"guard is {FAILING_JOB!r}; the other requests state "
                            f"{NO_FAILING_JOB!r}")
        if not any("no_progress" in g and "remain" in g for g in guard):
            problems.append(f"{where}: requests Work without requiring that no_progress remains")
    for e in table_edges():
        if any("proposal" in g and "admission" in g for g in e.transition.guard):
            problems.append(f"{e.source} --{e.verb}-->: a proposal admission guard; no patch is "
                            "proposed before the fork exists")
        if e.verb == "admit_development" and PATCH_SCOPE_COMPLETE not in e.transition.guard:
            problems.append(f"{e.source} --{e.verb}-->: without the guard "
                            f"{PATCH_SCOPE_COMPLETE!r}")
    if not any(e.verb == "admit_development" for e in table_edges()):
        problems.append("admit_development: missing")
    # The first entry into development is the confirmed fork.
    first = [e for e in table_edges() if state_phase(e.source) == "provisioning"
             and state_phase(e.target) == "development"]
    if not first or any(e.transition.trigger != Trigger.OPERATION or "fork" not in
                        e.transition.effects.sets for e in first):
        problems.append("development is entered from provisioning only by a confirmed fork "
                        "operation that records the fork")


def _check_submission(problems: list[str]) -> None:
    """Every submission passes the review, the build of `riscv64`, CI on its
    head, and the package, in that order; a failed review returns to
    development with its reasons and posts nothing; and the one handoff state
    that writes the fork is the build of `riscv64`."""
    missing = [k for k in (SUBMISSION_GATE, *SUBMISSION_STEPS) if k not in LIFECYCLE]
    if missing:
        problems.append(f"submission: {missing} are not table states")
        return
    successors: dict[str, set[str]] = {k: set() for k in LIFECYCLE}
    for e in table_edges():
        successors[e.source].add(e.target)

    # Paths through a human wait leave it only by a resume that admits the
    # pending action it was entered with.
    def step_from(node: tuple[str, str | None], skip: str) -> set[tuple[str, str | None]]:
        key, pending = node
        out = set()
        for e in state_edges(key):
            if e.target == skip or e.target == key:
                continue
            if pending is not None and e.transition.command == RESUME \
                    and not _admits(e.transition, pending):
                continue
            kinds = [f.partition("=")[2] for f in e.transition.effects.sets
                     if f.startswith("pending_human_action=")]
            entering = LIFECYCLE[e.target].kind == "human_wait"
            out.add((e.target, (kinds[0] if kinds else "") if entering else None))
        return out

    final = SUBMISSION_STEPS[-1]
    for step in SUBMISSION_STEPS[:-1]:
        reached = _reachable((SUBMISSION_GATE, None), lambda n, step=step: step_from(n, step))
        if any(key == final for key, _pending in reached):
            problems.append(f"submission: {final} is reachable from {SUBMISSION_GATE} without "
                            f"{step}")
    for earlier, later in zip(SUBMISSION_STEPS, SUBMISSION_STEPS[1:]):
        if later not in successors[earlier]:
            problems.append(f"submission: no row leads from {earlier} to {later}")
    writers = [k for k, st in LIFECYCLE.items() if state_phase(k) == "handoff" and st.writes]
    if writers != [UPSTREAM_BRANCH_WRITER]:
        problems.append(f"submission: the handoff states that write the fork are {writers}, not "
                        f"only {UPSTREAM_BRANCH_WRITER}")
    for work_state, passed in SUBMISSION_WORK.items():
        for e in state_edges(work_state):
            if e.transition.trigger != Trigger.RESULT or e.target == work_state:
                continue
            sets = {_field_name(f) for f in e.transition.effects.sets}
            if e.target == passed:
                continue
            if e.target != "development.fix_needed" \
                    or "pending_feedback_evidence_ids" not in sets:
                problems.append(f"{e.source} --{e.verb}--> {e.target}: a submission Result "
                                "that does not pass returns to development.fix_needed with its "
                                "reasons as pending feedback")


def _check_exclusive_observations(problems: list[str]) -> None:
    for key, verbs in EXCLUSIVE_OBSERVATIONS.items():
        try:
            edges = [find_edge(key, verb) for verb in verbs]
        except KeyError as exc:
            problems.append(f"exclusive observations: {exc.args[0]}")
            continue
        for i, a in enumerate(edges):
            if a.transition.trigger != Trigger.OBSERVATION:
                problems.append(f"{key} --{a.verb}-->: is not an observation")
            for b in edges[i + 1:]:
                if not excluded(a.transition, b.transition):
                    problems.append(f"{key}: {a.verb} and {b.verb} can apply together; their "
                                    "guards are not exclusive")


def _check_stale_resume(policy: dict, problems: list[str]) -> None:
    """``resume_stale`` and the other resumes of the same states carry
    complementary guards, so exactly one applies."""
    rule = RULES.get("resume_stale")
    if rule is None:
        problems.append("rule resume_stale: missing")
        return
    stale_states = set(rule_states("resume_stale"))
    expected = {k for k in LIFECYCLE if state_phase(k) in STALE_PHASES}
    if stale_states != expected:
        problems.append(f"rule resume_stale: applies in {sorted(stale_states)}, not in every "
                        f"state of {', '.join(STALE_PHASES)}")
    t = rule.transition
    if t.command != RESUME or STALE not in t.guard or NOT_STALE in t.guard:
        problems.append(f"rule resume_stale: must be `{RESUME}` guarded by {STALE!r}")
    refresh = {e.transition.guard for e in table_edges() if e.verb == "refresh_planning"}
    for guard in (QUIESCENT, NO_WORK):
        if guard not in t.guard:
            problems.append(f"rule resume_stale: without the guard {guard!r}")
        if not any(guard in g for g in refresh):
            problems.append(f"refresh_planning: no row with the guard {guard!r}")
    if not set(_REFRESH_EFFECTS.sets) <= set(t.effects.sets) \
            or not set(_REFRESH_EFFECTS.clears) <= set(t.effects.clears):
        problems.append("rule resume_stale: does not have the effects of refresh_planning")
    for e in table_edges():
        if e.transition.command != RESUME or e.verb == "resume_stale":
            continue
        where = f"{e.source} --{e.verb}--> {e.target}"
        if STALE in e.transition.guard:
            problems.append(f"{where}: only resume_stale may be guarded by {STALE!r}")
        declared = NOT_STALE in e.transition.guard
        if e.source in stale_states and not declared:
            problems.append(f"{where}: a resume beside resume_stale without the guard {NOT_STALE!r}")
        if e.source not in stale_states and declared:
            problems.append(f"{where}: guard {NOT_STALE!r} in a state without resume_stale")
    for name, f in FLAGS.items():
        for verb, clear in f.cleared_by.items():
            if clear.command == RESUME and FLAG_NOT_STALE not in clear.guard:
                problems.append(f"flag {name} {verb}: a resume without the guard {FLAG_NOT_STALE!r}")
    for key in (STALE_KEY, REMINDER_KEY):
        try:
            if int(policy_value(policy, key)) <= 0:
                problems.append(f"policy.json: {key} is not positive")
        except (KeyError, TypeError, ValueError):
            problems.append(f"policy.json has no integer {key}")


def _check_flags(problems: list[str]) -> None:
    allocated = {e.target for e in table_edges() if e.transition.effects.work == "allocate"}
    table_verbs = {e.verb for e in table_edges()}
    for name, f in FLAGS.items():
        if not f.set_by or not f.cleared_by:
            problems.append(f"flag {name}: no set or no clear transition")
        for verb, t in (*f.set_by.items(), *f.cleared_by.items()):
            where = f"flag {name} {verb}"
            if verb in table_verbs:
                problems.append(f"{where}: is also a table verb")
            _check_transition(where, t, problems)
        for verb, t in f.set_by.items():
            where = f"flag {name} {verb}"
            if f.field not in t.effects.sets:
                problems.append(f"{where}: does not set {f.field}")
            if NOT_PAUSED not in t.guard and name == "paused":
                problems.append(f"{where}: sets the flag without the guard {NOT_PAUSED!r}")
            if t.effects.work != "clear":
                problems.append(f"{where}: setting the flag must clear the requested Work")
            if t.effects.revision == "bump":
                problems.append(f"{where}: a stop must not bump the revision")
            reason = [s for s in t.effects.sets if _field_name(s) == f"{f.field}_reason"]
            if not reason:
                problems.append(f"{where}: does not set {f.field}_reason")
            elif t.trigger != Trigger.COMMAND and "=" not in reason[0] \
                    and verb not in CALLER_REASONS:
                problems.append(f"{where}: a stop set by the controller must record its reason")
            by = f"{f.field}_by=" + ("human" if t.trigger == Trigger.COMMAND else "controller")
            if by not in t.effects.sets:
                problems.append(f"{where}: does not set {by}")
            if verb in CALLER_REASONS and (t.trigger == Trigger.COMMAND or (reason and "=" in reason[0])):
                problems.append(f"{where}: a stop whose reason the caller states is set by the "
                                "controller and names no reason in the table")
        for verb in CALLER_REASONS:
            if name == "paused" and verb not in f.set_by:
                problems.append(f"flag {name}: {verb} is not a set verb")
        for verb, t in f.cleared_by.items():
            where = f"flag {name} {verb}"
            if t.trigger != Trigger.COMMAND:
                problems.append(f"{where}: is not command-triggered")
            if f.field not in t.effects.clears:
                problems.append(f"{where}: does not clear {f.field}")
            if t.command == RESUME and t.effects.revision != "bump":
                problems.append(f"{where}: a resume must bump the revision")
            extra = [c for c in t.effects.clears if not c.startswith(f.field)]
            if extra or t.effects.sets:
                problems.append(f"{where}: changes more than the flag: {extra or t.effects.sets}")
        if set(f.set_moves) != allocated:
            problems.append(f"flag {name}: set_moves {sorted(f.set_moves)} differ from the states "
                            f"that hold requested Work {sorted(allocated)}")
        for source, target in f.set_moves.items():
            state = LIFECYCLE.get(target)
            if state is None or state_phase(source) != state_phase(target) or not state.launches \
                    or state.kind != "auto_wait":
                problems.append(f"flag {name}: {source} moves to {target}, which is not a state "
                                "of the same phase in which Work is due")
        if set(f.mask) != set(Trigger):
            problems.append(f"flag {name}: mask does not cover every trigger")


def pending_kinds(state: str) -> set[str]:
    """The pending human actions with which ``state`` is entered; ``""`` for
    an entry that does not name its action."""
    kinds = set()
    for e in table_edges():
        if e.target == state and e.source != state:
            for entry in e.transition.effects.sets:
                name, _, value = entry.partition("=")
                if name == "pending_human_action":
                    kinds.add(value)
    return kinds


def _admits(t: Transition, kind: str) -> bool:
    """Whether the pending-action guards of ``t`` allow the action ``kind``."""
    for guard in t.guard:
        if guard in PENDING_GUARDS:
            negated, kinds = PENDING_GUARDS[guard]
            if (kind in kinds) == negated:
                return False
    return True


def _check_human_waits(problems: list[str]) -> None:
    """The resumes of one human wait are mutually exclusive: when a wait has
    several, each is guarded by the pending action, no action satisfies two
    of them, and every action with which the wait is entered satisfies one."""
    for e in table_edges():
        if any(g in PENDING_GUARDS for g in e.transition.guard) and (
                LIFECYCLE[e.source].kind != "human_wait" or not resumes_wait(e)):
            problems.append(f"{e.source} --{e.verb}--> {e.target}: a pending-action guard on a "
                            "transition that is not the resume of a human wait")
    for key, state in LIFECYCLE.items():
        if state.kind != "human_wait":
            continue
        resumes = [e for e in state_edges(key) if e.transition.command == RESUME and not is_rule_row(e)]
        entering = pending_kinds(key)
        guarded: dict[str, tuple[bool, tuple[str, ...]]] = {}
        for e in resumes:
            guards = [PENDING_GUARDS[g] for g in e.transition.guard if g in PENDING_GUARDS]
            if len(guards) > 1 or (len(resumes) > 1 and not guards):
                problems.append(f"{key} --{e.verb}--> {e.target}: one of {len(resumes)} resumes of "
                                "the wait, so exactly one pending-action guard is required")
            elif guards:
                guarded[e.verb] = guards[0]
        if not guarded:
            continue
        positive = [k for negated, kinds in guarded.values() if not negated for k in kinds]
        for kind in sorted(set(positive)):
            if positive.count(kind) > 1:
                problems.append(f"{key}: several resumes apply when pending_human_action is {kind}")
        named = {k for _negated, kinds in guarded.values() for k in kinds}
        for kind in sorted(named - entering):
            problems.append(f"{key}: a resume is guarded by pending_human_action {kind}, with "
                            "which the wait is never entered")
        negative = [kinds for negated, kinds in guarded.values() if negated]
        if len(negative) > 1 or (negative and set(negative[0]) != set(positive)):
            problems.append(f"{key}: the resume for the other pending actions must exclude "
                            "exactly the actions the other resumes name")
        if "" in entering:
            problems.append(f"{key}: entered without naming the pending action although its "
                            "resumes are guarded by it")
        for kind in sorted(entering - {""}):
            if sum(1 for e in resumes if _admits(e.transition, kind)) != 1:
                problems.append(f"{key}: not exactly one resume applies when pending_human_action "
                                f"is {kind}")


def excluded(a: Transition, b: Transition) -> bool:
    """Whether the guards of ``a`` and ``b`` cannot hold together, by
    ``EXCLUSIVE_GUARDS``."""
    return any((set(x) <= set(a.guard) and set(y) <= set(b.guard))
               or (set(y) <= set(a.guard) and set(x) <= set(b.guard))
               for x, y in EXCLUSIVE_GUARDS)


def due_work_edges(state: str) -> list[Edge]:
    """The controller transitions of a state in which Work is due: the rows
    of the table, without the expansions of rules."""
    return [e for e in state_edges(state)
            if e.transition.trigger == Trigger.TICK and not is_rule_row(e)]


def _check_due_work(problems: list[str]) -> None:
    """In a state in which the controller allocates Work, at most one of its
    controller transitions applies: every two of them carry guards that
    ``EXCLUSIVE_GUARDS`` lists as exclusive. Exhaustion of ``work_total``
    is one transition, guarded by nothing else, so that it applies whatever
    the other budgets hold."""
    guards = {g for e in table_edges() for g in e.transition.guard}
    for x, y in EXCLUSIVE_GUARDS:
        for g in (*x, *y):
            if g not in guards:
                problems.append(f"exclusive guards: {g!r} is not the guard of any transition")
    for key, state in LIFECYCLE.items():
        edges = due_work_edges(key)
        if not any(e.transition.effects.work == "allocate" for e in edges):
            continue
        for i, a in enumerate(edges):
            for b in edges[i + 1:]:
                if not excluded(a.transition, b.transition):
                    problems.append(f"{key}: {a.verb} and {b.verb} can apply together; their "
                                    "guards are not exclusive")
        for e in edges:
            if WORK_TOTAL_EXHAUSTED in e.transition.guard and (
                    e.verb != "exhaust_work_total" or e.transition.guard != (WORK_TOTAL_EXHAUSTED,)
                    or LIFECYCLE[e.target].kind != "human_wait"):
                problems.append(f"{key} --{e.verb}--> {e.target}: exhaustion of work_total where "
                                "Work is due is exhaust_work_total, guarded by nothing else, and "
                                "leads to a human wait")


def _check_work_identity(problems: list[str]) -> None:
    """Work names the verified upstream identity and base: every row that
    allocates Work, or a fresh graph for it, requires them."""
    for e in table_edges():
        if e.transition.effects.work in ("allocate", "reallocate") \
                and UPSTREAM_BOUND not in e.transition.guard:
            problems.append(f"{e.source} --{e.verb}--> {e.target}: allocates Work without the "
                            f"guard {UPSTREAM_BOUND!r}")


def exhausted_budgets(edge: Edge) -> list[str]:
    """The budgets whose exhaustion ``edge`` is."""
    return [n for n, b in BUDGETS.items()
            if edge.verb in b.exhaustion_verbs and _names_exhaustion(edge.transition, n)]


def counters_named(edge: Edge) -> set[str]:
    """The counters an exhaustion may name in ``PENDING_COUNTER``: the one it
    states, or any budget it exhausts when it states none."""
    named = [s for s in edge.transition.effects.sets if _field_name(s) == PENDING_COUNTER]
    if not named:
        return set()
    value = named[0].partition("=")[2]
    return {value} if value else set(exhausted_budgets(edge))


def _check_counter_restarts(problems: list[str]) -> None:
    """A resume after a budget was exhausted starts that counter again, with
    no exception: every exhaustion that leads to a human wait names its
    counter in ``PENDING_COUNTER``, and every resume from that wait restarts
    exactly the counters that can be named when it applies. No other
    transition names or restarts a counter."""
    entries: dict[str, list[tuple[set[str], list[str]]]] = {}
    for e in table_edges():
        where = f"{e.source} --{e.verb}--> {e.target}"
        budgets = exhausted_budgets(e)
        named = [s for s in e.transition.effects.sets if _field_name(s) == PENDING_COUNTER]
        counted = bool(budgets) and e.target != e.source \
            and LIFECYCLE[e.target].kind == "human_wait"
        if not counted:
            if named:
                problems.append(f"{where}: sets {PENDING_COUNTER} but is not an exhaustion that "
                                "leads to a human wait")
            continue
        if len(named) != 1:
            problems.append(f"{where}: exhaustion that leads to a human wait without "
                            f"{PENDING_COUNTER}")
            continue
        value = named[0].partition("=")[2]
        if value and value not in budgets:
            problems.append(f"{where}: {PENDING_COUNTER} names {value!r}, which this transition "
                            "does not exhaust")
            continue
        if not value and PENDING_COUNTER not in e.transition.description:
            problems.append(f"{where}: {PENDING_COUNTER} names no counter, so the description "
                            "must state which one it names")
        kinds = [s.partition("=")[2] for s in e.transition.effects.sets
                 if s.startswith("pending_human_action=")]
        entries.setdefault(e.target, []).append((counters_named(e), kinds))
    rule_expected: dict[str, set[str]] = {}
    for e in table_edges():
        t = e.transition
        restarts = set(t.effects.restarts)
        where = f"{e.source} --{e.verb}--> {e.target}"
        resume = resumes_wait(e) and LIFECYCLE[e.source].kind == "human_wait"
        if e.verb in RULES:
            if resume:
                rule_expected.setdefault(e.verb, set()).update(
                    n for names, _kinds in entries.get(e.source, ()) for n in names)
            continue
        if not resume:
            if restarts:
                problems.append(f"{where}: only the resume of a human wait starts a counter again")
            continue
        expected = {n for names, kinds in entries.get(e.source, ())
                    if all(_admits(t, k) for k in kinds) for n in names}
        for name in sorted(expected - restarts):
            problems.append(f"{where}: does not start {name!r} again, although an exhaustion of "
                            f"it leads to {e.source} and this resume applies")
        for name in sorted(restarts - expected):
            problems.append(f"{where}: starts {name!r} again, but no exhaustion of it names "
                            f"its counter on the way into {e.source}")
    for verb, rule in RULES.items():
        restarts = set(rule.transition.effects.restarts)
        expected = rule_expected.get(verb, set())
        if restarts and rule.transition.command != RESUME:
            problems.append(f"rule {verb}: only a resume starts a counter again")
        for name in sorted(expected - restarts):
            problems.append(f"rule {verb}: does not start {name!r} again, although it resumes a "
                            "human wait that an exhaustion of it leads to")
        for name in sorted(restarts - expected):
            problems.append(f"rule {verb}: starts {name!r} again, but no exhaustion of it names "
                            "its counter on the way into a human wait the rule resumes")
    for f in FLAGS.values():
        for verb, t in (*f.set_by.items(), *f.cleared_by.items()):
            if t.effects.restarts:
                problems.append(f"flag {f.field} {verb}: a flag transition starts a counter again")


def _check_budgets(policy: dict, problems: list[str]) -> None:
    edges = list(table_edges())
    charged = {n for e in edges for n in e.transition.charges}
    for name, b in BUDGETS.items():
        try:
            limit = budget_limit(name, policy)
        except (KeyError, TypeError, ValueError):
            problems.append(f"budget {name}: policy.json has no integer {b.policy_key}")
            continue
        if limit <= 0:
            problems.append(f"budget {name}: limit {limit} is not positive")
        if name not in charged:
            problems.append(f"budget {name}: no transition charges it")
        for verb in b.exhaustion_verbs:
            if not any(e.verb == verb for e in exhaustion_edges(name)):
                problems.append(f"budget {name}: no transition {verb!r} has a guard that names it")
        if b.policy_key == INFRASTRUCTURE_KEY and not b.infrastructure:
            problems.append(f"budget {name}: uses {INFRASTRUCTURE_KEY} but is not marked "
                            "infrastructure")
        for x in exhaustion_edges(name):
            where = f"{x.source} --{x.verb}--> {x.target}"
            target = LIFECYCLE[x.target]
            same_phase_wait = (target.kind == "human_wait"
                               and state_phase(x.target) == state_phase(x.source))
            if b.infrastructure and not same_phase_wait:
                problems.append(f"{where}: exhaustion of infrastructure budget {name!r} must lead "
                                "to a human wait of the same phase")
            if state_phase(x.source) in NO_AUTOMATIC_PARK and not same_phase_wait:
                problems.append(f"{where}: exhaustion of {name!r} in {state_phase(x.source)} must "
                                "lead to its human wait")
            if name in x.transition.charges:
                problems.append(f"{where}: exhaustion charges {name!r}")
    try:
        if int(policy_value(policy, SOURCE_READS_MINUTES_KEY)) <= 0:
            problems.append(f"budget source_reads: {SOURCE_READS_MINUTES_KEY} is not positive")
    except (KeyError, TypeError, ValueError):
        problems.append(f"budget source_reads: policy.json has no integer {SOURCE_READS_MINUTES_KEY}")
    shared = sorted(n for n, b in BUDGETS.items() if b.policy_key == INFRASTRUCTURE_KEY)
    if len(shared) < 2:
        problems.append(f"budgets: {INFRASTRUCTURE_KEY} is shared by {shared}")
    for e in edges:
        for name in e.transition.charges:
            if name not in BUDGETS:
                continue
            if not [x for x in exhaustion_edges(name) if x.source in (e.source, e.target)]:
                problems.append(f"{e.source} --{e.verb}--> {e.target}: charges {name!r} but no "
                                f"exhaustion transition whose guard names {name!r} leaves "
                                f"{e.source} or {e.target}")


def _check_operations(problems: list[str]) -> None:
    name = "operation"
    if not _check_graph(name, OPERATIONS, OPERATION_INITIAL_STATE,
                        lambda k: not OPERATIONS[k].open, problems):
        return
    lifecycle_verbs = {e.verb for e in table_edges()} | {
        v for f in FLAGS.values() for v in (*f.set_by, *f.cleared_by)}
    for e in table_edges(OPERATIONS):
        where = f"operation {e.source} --{e.verb}--> {e.target}"
        _check_transition(where, e.transition, problems, command_guards=(EXACT_OPERATION,))
        effects = e.transition.effects
        if effects.revision != "keep" or effects.generation != "keep" or effects.work != "keep":
            problems.append(f"{where}: an operation transition changes the contribution")
        if e.transition.charges:
            problems.append(f"{where}: an operation transition charges a budget")
        if e.transition.trigger == Trigger.COMMAND and e.transition.command != RESOLVE:
            problems.append(f"{where}: operation commands are `{RESOLVE}`")
        if e.verb in lifecycle_verbs:
            problems.append(f"{where}: the verb is also a lifecycle verb")
    for key, state in OPERATIONS.items():
        resolves = {e.target for e in state_edges(key, OPERATIONS) if e.transition.command == RESOLVE}
        ends = {k for k, s in OPERATIONS.items() if not s.open}
        if state.open and resolves != ends:
            problems.append(f"operation {key}: `{RESOLVE}` leads to {sorted(resolves)}, "
                            f"not to every end state")
    if OPERATION_INITIAL_VERB in {e.verb for e in table_edges(OPERATIONS)}:
        problems.append(f"operation initial verb {OPERATION_INITIAL_VERB!r} is also a transition")


def _check_diagrams(problems: list[str]) -> None:
    from scripts.state_machine_decisions import (
        DECISIONS,
        FURTHER_CHANGES,
        OPEN_ITEMS,
        SETTLED_OPEN_ITEMS,
    )

    def drawn(title: str, source: str, verb: str) -> tuple[str, str, str] | None:
        try:
            _t, target = resolve_node(source, verb)
        except KeyError as exc:
            problems.append(f"{title}: {source} {verb}: {exc.args[0]}")
            return None
        return source.partition("+")[0], verb, target.partition("+")[0]

    def check_node(title: str, node: str, extra: set[str] = frozenset()) -> None:
        state, _, flag = node.partition("+")
        if state not in LIFECYCLE and node not in extra:
            problems.append(f"{title}: node {node!r} is not a table state")
        if flag and flag not in FLAGS:
            problems.append(f"{title}: unknown flag in {node!r}")

    drawn_states: set[str] = set()
    drawn_edges: set[tuple[str, str, str]] = set()
    drawn_verbs: set[str] = set()
    drawn_rules: list[str] = []
    for section in DIAGRAM_SECTIONS:
        for d in section.diagrams:
            title = f"diagram {d.title or section.title!r}"
            for node in d.nodes:
                check_node(title, node)
            for node in (n for n in (d.start, *d.ends) if n):
                if node not in d.nodes:
                    problems.append(f"{title}: start or end node {node!r} is not drawn")
            drawn_states.update(n.partition("+")[0] for n in d.nodes)
            if d.rule is not None:
                drawn_rules.append(d.rule)
                rule = RULES.get(d.rule)
                if rule is None:
                    problems.append(f"{title}: unknown rule {d.rule!r}")
                elif d.edges or d.nodes != (() if rule.target == SELF else (rule.target,)):
                    problems.append(f"{title}: a rule diagram draws only the rule and its target")
                continue
            if len(set(d.edges)) != len(d.edges):
                problems.append(f"{title}: an edge is drawn twice")
            for source, verb in d.edges:
                if source not in d.nodes:
                    problems.append(f"{title}: edge source {source!r} is not drawn")
                    continue
                edge = drawn(title, source, verb)
                if edge:
                    drawn_edges.add(edge)
                    drawn_verbs.add(verb)
                    _t, target = resolve_node(source, verb)
                    if target not in d.nodes:
                        problems.append(f"{title}: {source} {verb} leads to {target}, not drawn")
    for key in LIFECYCLE:
        if key not in drawn_states:
            problems.append(f"{key}: state appears in no diagram")
    for e in table_edges():
        if not is_rule_row(e) and (e.source, e.verb, e.target) not in drawn_edges:
            problems.append(f"{e.source} --{e.verb}--> {e.target}: transition appears in no diagram")
    for verb in RULES:
        if drawn_rules.count(verb) != 1:
            problems.append(f"rule {verb}: drawn in {drawn_rules.count(verb)} rule diagrams, not one")
    for name, f in FLAGS.items():
        for verb in (*f.set_by, *f.cleared_by):
            if verb not in drawn_verbs:
                problems.append(f"flag {name} {verb}: appears in no diagram")
        if f.set_moves and not any((source, verb, target) in drawn_edges
                                   for source, target in f.set_moves.items() for verb in f.set_by):
            problems.append(f"flag {name}: no move out of a state with requested Work is drawn")

    for d in (*DECISIONS, *FURTHER_CHANGES, *SETTLED_OPEN_ITEMS, *OPEN_ITEMS):
        title = f"decision {d.title!r}"
        if d.operations:
            if d.nodes or d.edges:
                problems.append(f"{title}: an operation slice draws the operation table only")
            continue
        candidate_nodes = {n for c in d.candidates for n in (c.source, c.target)}
        for node in d.nodes:
            check_node(title, node, candidate_nodes)
        if len(set(d.edges)) != len(d.edges):
            problems.append(f"{title}: an edge is drawn twice")
        for source, verb in d.edges:
            if source not in d.nodes:
                problems.append(f"{title}: edge source {source!r} is not drawn")
            elif source.partition("+")[0] in LIFECYCLE and drawn(title, source, verb):
                _t, target = resolve_node(source, verb)
                if target not in d.nodes:
                    problems.append(f"{title}: {source} {verb} leads to {target}, not drawn")
        for edge in d.highlighted:
            if edge not in d.edges:
                problems.append(f"{title}: highlighted edge {edge} is not drawn")
        letters = {letter for letter, _ in d.options}
        settled = not any(d is o for o in OPEN_ITEMS)
        if settled and (d.options or d.candidates):
            problems.append(f"{title}: a settled decision lists options")
        if not settled and len(d.options) < 2:
            problems.append(f"{title}: an open item needs at least two options")
        for c in d.candidates:
            if c.option not in letters:
                problems.append(f"{title}: candidate names unknown option {c.option!r}")
            for node in (c.source, c.target):
                if node not in d.nodes:
                    problems.append(f"{title}: candidate node {node!r} is not drawn")


def check_table(policy: dict | None = None) -> list[str]:
    """Every consistency problem of the lifecycle tables; empty when coherent."""
    import json
    from pathlib import Path

    if policy is None:
        policy = json.loads((Path(__file__).resolve().parent / "policy.json").read_text())
    problems: list[str] = []
    if not _check_graph("", LIFECYCLE, INITIAL_STATE,
                        lambda k: LIFECYCLE[k].kind == "terminal", problems):
        return problems
    _check_states(problems)
    _check_rules(problems)
    _check_stops(problems)
    _check_feedback(problems)
    _check_reruns(problems)
    _check_settled_rows(problems)
    _check_patch_work(problems)
    _check_exclusive_observations(problems)
    _check_submission(problems)
    _check_stale_resume(policy, problems)
    _check_flags(problems)
    _check_budgets(policy, problems)
    _check_due_work(problems)
    _check_work_identity(problems)
    _check_human_waits(problems)
    _check_counter_restarts(problems)
    _check_operations(problems)

    # Constants used elsewhere in this module.
    writes = {state_phase(k) for k, s in LIFECYCLE.items() if s.writes}
    if writes != set(WRITE_PHASES):
        problems.append(f"fork-writing phases {sorted(writes)} differ from WRITE_PHASES")
    phases = {state_phase(k) for k in LIFECYCLE}
    if INITIAL_VERB in {e.verb for e in table_edges()}:
        problems.append(f"initial verb {INITIAL_VERB!r} is also a table transition")
    for phase in (*DIAGRAM_PHASE_IDS, *STALE_PHASES, *NO_AUTOMATIC_PARK):
        if phase not in phases:
            problems.append(f"unknown phase {phase!r} in a table constant")
    _check_diagrams(problems)
    return problems


def table_summary() -> str:
    ends = sum(1 for s in LIFECYCLE.values() if s.kind == "terminal")
    transitions = sum(len(s.transitions) for s in LIFECYCLE.values())
    rule_rows = sum(1 for e in table_edges() if is_rule_row(e))
    verbs = len({e.verb for e in table_edges()})
    operation_rows = sum(len(s.transitions) for s in OPERATIONS.values())
    return (f"{len(LIFECYCLE)} states ({ends} end states), {transitions} transitions "
            f"({rule_rows} expanded from {len(RULES)} rules) with {verbs} verbs, "
            f"{len(FLAGS)} flag{'' if len(FLAGS) == 1 else 's'}, {len(BUDGETS)} budgets; operation table: "
            f"{len(OPERATIONS)} states, {operation_rows} transitions")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m lifecycle",
                                     description="Lifecycle table tools.")
    parser.add_argument("command", choices=["check"],
                        help="check: verify the consistency of the lifecycle tables")
    parser.parse_args(argv)
    problems = check_table()
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    print(f"lifecycle table: {table_summary()}; "
          + (f"{len(problems)} problems" if problems else "consistent"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
