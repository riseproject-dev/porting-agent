# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Decisions about the lifecycle table in ``lifecycle.py``.

``RULES_BEHIND`` are the rules the decisions follow. ``DECISIONS`` are
settled: each records its topic, the decision, and the slice of the table that
now holds it. ``FURTHER_CHANGES`` are settled changes that are not one of the
numbered decisions. ``SETTLED_OPEN_ITEMS`` are the items that were open after
those and are settled now, in the order of their numbers. ``OPEN_ITEMS`` are
not settled: each states what the table does today and the options; they are
numbered after the settled ones.

Every item names the table states and verbs it concerns. ``edges`` are table
rows (``(from_node, verb)``) drawn solid, ``highlighted`` are the rows the item
is about (drawn thick), and ``candidates`` are edges an option of an open item
would add (drawn dashed). A node written ``state+flag`` is ``state`` with that
flag set. ``operations`` draws the operation table instead. The page is
rendered by ``scripts/generate_state_machine_doc.py``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    source: str
    label: str
    target: str
    option: str


@dataclass(frozen=True)
class Item:
    title: str
    question: str
    text: str
    nodes: tuple[str, ...] = ()
    edges: tuple[tuple[str, str], ...] = ()
    highlighted: tuple[tuple[str, str], ...] = ()
    options: tuple[tuple[str, str], ...] = ()
    candidates: tuple[Candidate, ...] = ()
    operations: bool = False


# The rules the settled decisions follow.
RULES_BEHIND: tuple[str, ...] = (
    "Infrastructure exhaustion goes to the phase's `waiting_human`. It never ends the generation.",
    "Park means \"the approach is not working\" or \"a human said stop\". Nothing parks from "
    "`upstream_review` automatically.",
    "A Result is reused only while the pipeline ran continuously. Any stop of unknown length "
    "(pause, human wait, park) invalidates it; the next step is fresh Work.",
    "The plan is refreshed after a park, and after a pause or human wait longer than 7 days "
    "in provisioning or development. It is never refreshed automatically from "
    "`handoff.awaiting_submission` or `handoff.waiting_upstream_pr`; there a human uses "
    "`/upstreaming refresh`.",
    "Every row with a `Work: allocate` or `Work: reallocate` effect charges one per-generation "
    "counter, `work_total`. `lifecycle check` verifies this.",
    "Prefer a guard or a derived status over a new state or flag.",
    "Every stop that clears the requested Work also clears `pending_publication`: the flag "
    "set verbs, `park_contribution`, `abandon`, and every other transition that ends the "
    "generation. `lifecycle check` verifies this.",
)

DECISIONS: tuple[Item, ...] = (
    Item(
        "Pause semantics",
        "What `pause` stops, and whether `resume_pause` changes the revision.",
        "`pause` clears the requested Work and the pending publication, and records `paused_at`. "
        "A Result held for publication is invalidated: it is not published, and no publish "
        "retry is launched while paused. `resume_pause` bumps the "
        "revision, so a Result that lands during the pause no longer matches and takes "
        "`record_late_effect`. Observations, including CI on the exact head, still fold. The "
        "publisher's final check reads `paused`.",
        ("development.agent_running", "development.fix_needed+paused", "development.fix_needed",
         "development.waiting_ci", "development.waiting_ci+paused"),
        edges=(("development.agent_running", "pause"),
               ("development.fix_needed+paused", "resume_pause"),
               ("development.waiting_ci", "pause"),
               ("development.waiting_ci+paused", "resume_pause")),
        highlighted=(("development.agent_running", "pause"),
                     ("development.fix_needed+paused", "resume_pause"),
                     ("development.waiting_ci+paused", "resume_pause")),
    ),
    Item(
        "Park quiescence and the generation fence",
        "How `restart_generation` knows the ended generation is quiescent, and how a late Result "
        "of that generation is kept out of the new one.",
        "The generation is part of the Work identity. A Result of another generation is never "
        "folded and the publisher refuses it; it takes `record_late_effect`. Work and the "
        "pending publication are cleared at park and at abandon, so a publication that was "
        "pending when the generation ended never keeps `restart_generation` from applying; a "
        "branch its worker run already pushed is recorded by head observation in the phases "
        "that observe the head. Quiescence for `restart_generation` is: no open operation and "
        "no pending publication.",
        ("development.agent_running", "parked.idle", "queued.idle"),
        edges=(("development.agent_running", "park_contribution"),
               ("parked.idle", "record_late_effect"),
               ("parked.idle", "restart_generation")),
        highlighted=(("parked.idle", "restart_generation"), ("parked.idle", "record_late_effect")),
    ),
    Item(
        "Resume targets of recorded human waits in upstream review",
        "Where a resume from `upstream_review.waiting_human` leads.",
        "Two verbs, each guarded by the pending action, so exactly one applies. "
        "`resume_upstream_review` leads to `upstream_review.waiting_ci` after a closed PR that "
        "is observed open again, after a reviewer was answered, and after a head change. "
        "`resume_upstream_revision` leads to `upstream_review.fix_needed` only when revision "
        "Work failed. Dispatch exhaustion does not park.",
        ("upstream_review.agent_running", "upstream_review.waiting_human",
         "upstream_review.waiting_ci", "upstream_review.fix_needed"),
        edges=(("upstream_review.agent_running", "exhaust_dispatch_recovery"),
               ("upstream_review.waiting_ci", "record_pr_closed_unmerged"),
               ("upstream_review.waiting_human", "resume_upstream_review"),
               ("upstream_review.waiting_human", "resume_upstream_revision")),
        highlighted=(("upstream_review.waiting_human", "resume_upstream_review"),
                     ("upstream_review.waiting_human", "resume_upstream_revision")),
    ),
    Item(
        "Handoff entry and the handoff commands",
        "Whether `handoff.awaiting_submission`, `submit_upstream`, and the observed "
        "`verify_upstream_pr` replace the `request-handoff`, `approve-handoff`, and "
        "`register-upstream-pr` commands.",
        "They do. Nomination enters `handoff.awaiting_submission`, one `submit_upstream` command "
        "drives the handoff, and `verify_upstream_pr` is an observation. The three commands are "
        "retired. The reminder in `handoff.waiting_upstream_pr` states when an observed PR does "
        "not match.",
        ("development.waiting_ci", "handoff.awaiting_submission", "handoff.review_due",
         "handoff.waiting_upstream_pr", "upstream_review.waiting_ci"),
        edges=(("development.waiting_ci", "nominate_for_handoff"),
               ("handoff.awaiting_submission", "submit_upstream"),
               ("handoff.waiting_upstream_pr", "record_reminder"),
               ("handoff.waiting_upstream_pr", "verify_upstream_pr")),
        highlighted=(("handoff.awaiting_submission", "submit_upstream"),
                     ("handoff.waiting_upstream_pr", "verify_upstream_pr")),
    ),
    Item(
        "One terminal phase for work that is not needed",
        "Whether `closed.idle` with `terminal_reason` replaces the separate terminal phases "
        "`no_work`, `deferred`, `abandoned`, and `already_released`.",
        "It does: one `closed.idle` with `terminal_reason`. Existing records are migrated.",
        ("queued.idle", "planning.agent_running", "parked.idle", "closed.idle"),
        edges=(("queued.idle", "withdraw_request"),
               ("planning.agent_running", "close_planning"),
               ("parked.idle", "abandon")),
        highlighted=(("queued.idle", "withdraw_request"),
                     ("planning.agent_running", "close_planning"),
                     ("parked.idle", "abandon")),
    ),
    Item(
        "Capacity waits",
        "Whether lack of capacity, a planned operation, or a pending publication retry is an "
        "activity.",
        "It is not. There is no `waiting_capacity` state or flag: the contribution stays in its "
        "state while a guard such as a free worker slot or available capacity does not hold, and "
        "the status page shows the unmet guard.",
        ("provisioning.idle", "development.agent_running"),
        edges=(("provisioning.idle", "start_operation"),
               ("development.agent_running", "schedule_publish_retry")),
        highlighted=(("provisioning.idle", "start_operation"),
                     ("development.agent_running", "schedule_publish_retry")),
    ),
    Item(
        "Repair-budget exhaustion",
        "Where repair-budget exhaustion is decided, and what it does.",
        "Exhaustion is decided at `development.fix_needed` only. The first time "
        "`repair_attempts_cluster` or `no_progress` is exhausted in a generation, the tick row "
        "`refresh_on_exhaustion` leads to `queued.idle` and starts both counters again, but not "
        "`work_total`. Once that automatic refresh is used, `exhaust_repair_attempts` parks.",
        ("development.agent_running", "development.fix_needed", "queued.idle", "parked.idle"),
        edges=(("development.agent_running", "reject_patch_proposal"),
               ("development.fix_needed", "request_repair"),
               ("development.fix_needed", "refresh_on_exhaustion"),
               ("development.fix_needed", "exhaust_repair_attempts")),
        highlighted=(("development.fix_needed", "refresh_on_exhaustion"),
                     ("development.fix_needed", "exhaust_repair_attempts")),
    ),
    Item(
        "Planning retry budget",
        "Which budget `reject_plan` charges.",
        "A dedicated `planning_attempts` budget with the key `retries.planning_attempts` "
        "(3) and its own counter. A Result without a plan charges it too instead of going "
        "straight to `planning.waiting_human`.",
        ("planning.idle", "planning.agent_running", "planning.waiting_human"),
        edges=(("planning.idle", "dispatch_planning"),
               ("planning.agent_running", "reject_plan"),
               ("planning.agent_running", "exhaust_planning_recovery"),
               ("planning.waiting_human", "resume_planning")),
        highlighted=(("planning.agent_running", "reject_plan"),
                     ("planning.agent_running", "exhaust_planning_recovery")),
    ),
    Item(
        "Publication recovery exhaustion and its resume",
        "From which states `exhaust_publish_recovery` is taken, and where `resume_development` "
        "leads.",
        "In development, `exhaust_publish_recovery` leaves only `development.agent_running`. "
        "`resume_development` leads to `development.fix_needed`, clears `pending_publication`, "
        "and requests fresh Work. Automatic publish retries still reuse the Result.",
        ("development.agent_running", "development.fix_needed", "development.waiting_human"),
        edges=(("development.agent_running", "schedule_publish_retry"),
               ("development.agent_running", "exhaust_publish_recovery"),
               ("development.waiting_human", "resume_development")),
        highlighted=(("development.agent_running", "exhaust_publish_recovery"),
                     ("development.waiting_human", "resume_development")),
    ),
    Item(
        "Dispatch and Work recovery exhaustion targets",
        "Whether dispatch exhaustion and Work exhaustion lead to the same place.",
        "Both wait for a human: `exhaust_work_recovery` leads to the phase's `waiting_human` in "
        "planning, development, and upstream review, like `exhaust_dispatch_recovery`. An "
        "observed development PR that does not match the record takes `record_pr_mismatch` to "
        "`development.waiting_human`.",
        ("development.agent_running", "development.waiting_ci", "development.waiting_human"),
        edges=(("development.agent_running", "exhaust_dispatch_recovery"),
               ("development.agent_running", "exhaust_work_recovery"),
               ("development.waiting_ci", "record_pr_mismatch")),
        highlighted=(("development.agent_running", "exhaust_work_recovery"),
                     ("development.waiting_ci", "record_pr_mismatch")),
    ),
    Item(
        "Where park and refresh apply",
        "From which states `park_contribution` and `refresh_planning` are accepted.",
        "`park_contribution` is one rule: every non-terminal state, guarded by no open operation, "
        "except `handoff.waiting_upstream_pr`, `release_wait.*`, and `upstream_review.*` other "
        "than `upstream_review.waiting_human`. `refresh_planning` stays explicit: "
        "`development.fix_needed`, `development.waiting_ci`, "
        "`development.waiting_human`, `handoff.awaiting_submission`, and "
        "`handoff.waiting_upstream_pr`. Both are accepted while paused.",
        ("development.agent_running", "development.waiting_ci", "handoff.awaiting_submission",
         "handoff.waiting_upstream_pr", "queued.idle", "parked.idle"),
        edges=(("development.agent_running", "park_contribution"),
               ("development.waiting_ci", "park_contribution"),
               ("development.waiting_ci", "refresh_planning"),
               ("handoff.awaiting_submission", "park_contribution"),
               ("handoff.awaiting_submission", "refresh_planning"),
               ("handoff.waiting_upstream_pr", "refresh_planning")),
        highlighted=(("development.agent_running", "park_contribution"),
                     ("development.waiting_ci", "refresh_planning"),
                     ("handoff.waiting_upstream_pr", "refresh_planning")),
    ),
    Item(
        "Return path after refresh",
        "Where a refreshed planning that decides to develop leads.",
        "`readmit_development` leads to `development.fix_needed` on the existing fork. The guard "
        "of `admit_development` is the complement: no refresh recorded, or the recorded fork or "
        "development PR no longer matches.",
        ("development.fix_needed", "queued.idle", "planning.agent_running",
         "provisioning.idle"),
        edges=(("development.fix_needed", "refresh_planning"),
               ("planning.agent_running", "admit_development"),
               ("planning.agent_running", "readmit_development")),
        highlighted=(("planning.agent_running", "admit_development"),
                     ("planning.agent_running", "readmit_development")),
    ),
    Item(
        "Park by plan decision and park by command",
        "Whether parking from planning is one verb or two.",
        "Two verbs. `park_planning` is the Result transition taken on a plan decision and "
        "no longer carries the `/upstreaming park` label; `park_contribution` is the command.",
        ("planning.idle", "planning.agent_running", "parked.idle"),
        edges=(("planning.agent_running", "park_planning"),
               ("planning.agent_running", "park_contribution"),
               ("planning.idle", "park_contribution")),
        highlighted=(("planning.agent_running", "park_planning"),),
    ),
    Item(
        "Beginning the planning",
        "Whether `begin_planning` and `dispatch_planning` are two writes.",
        "Two transitions, written in two commits.",
        ("queued.idle", "planning.idle", "planning.agent_running"),
        edges=(("queued.idle", "begin_planning"),
               ("planning.idle", "dispatch_planning")),
        highlighted=(("queued.idle", "begin_planning"),
                     ("planning.idle", "dispatch_planning")),
    ),
    Item(
        "Provisioning failures",
        "How provisioning failures are recovered, and whether the `approve-scope` and `provision` "
        "commands stay.",
        "`approve-scope` and `provision` are retired. The `provisioning_attempts` budget and "
        "`exhaust_provisioning_attempts` are removed; a human parks explicitly. "
        "`close_planning` no longer leaves `provisioning.idle`. Exhausting "
        "`operation_launch` waits for a human and does not park.",
        ("provisioning.idle", "provisioning.waiting_human", "development.fix_needed",
         "parked.idle"),
        edges=(("provisioning.idle", "start_operation"),
               ("provisioning.idle", "require_provisioning_intervention"),
               ("provisioning.waiting_human", "resume_provisioning"),
               ("provisioning.waiting_human", "park_contribution"),
               ("provisioning.idle", "confirm_provisioning")),
        highlighted=(("provisioning.waiting_human", "resume_provisioning"),
                     ("provisioning.waiting_human", "park_contribution")),
    ),
    Item(
        "Upstream revision outcomes",
        "What happens when revision Work in upstream review is rejected or reports no change.",
        "A rejected proposal takes `reject_patch_proposal` to `upstream_review.fix_needed` and "
        "charges `no_progress`. When no change is needed, `require_reviewer_answer` leads to "
        "`upstream_review.waiting_human` with a pending action to answer the reviewer. "
        "`upstream_review.agent_running` has `schedule_publish_retry` and "
        "`exhaust_publish_recovery`. Every exhaustion in upstream review leads to "
        "`upstream_review.waiting_human`.",
        ("upstream_review.waiting_ci", "upstream_review.fix_needed", "upstream_review.agent_running",
         "upstream_review.waiting_human"),
        edges=(("upstream_review.fix_needed", "request_upstream_revision"),
               ("upstream_review.agent_running", "record_revision_publish_success"),
               ("upstream_review.agent_running", "reject_patch_proposal"),
               ("upstream_review.agent_running", "require_reviewer_answer"),
               ("upstream_review.agent_running", "schedule_publish_retry"),
               ("upstream_review.agent_running", "exhaust_publish_recovery"),
               ("upstream_review.fix_needed", "exhaust_repair_attempts")),
        highlighted=(("upstream_review.agent_running", "reject_patch_proposal"),
                     ("upstream_review.agent_running", "require_reviewer_answer"),
                     ("upstream_review.agent_running", "schedule_publish_retry"),
                     ("upstream_review.agent_running", "exhaust_publish_recovery")),
    ),
    Item(
        "Commands without table rows",
        "Which commands become table rows and which are retired.",
        "Retired: `request-handoff`, `approve-handoff`, `register-upstream-pr`, `approve-scope`, "
        "`provision`, `restart`, `clarify`, `revise-scope`, and `force-develop`. `abandon` leads "
        "from `parked.idle` to `closed.idle` with `terminal_reason=abandoned`. "
        "`/upstreaming refresh` from `parked.idle` accepts an optional class, paths, and reason, "
        "stored as `admission_override`; `restart_generation` still runs the complete "
        "planning, and `admit_development` accepts a verified plan decision develop, or "
        "a decision park with a recorded override. `authorize-followup` is an intake command outside the table.",
        ("parked.idle", "closed.idle", "queued.idle", "planning.agent_running",
         "provisioning.idle"),
        edges=(("parked.idle", "abandon"),
               ("parked.idle", "restart_generation"),
               ("planning.agent_running", "admit_development")),
        highlighted=(("parked.idle", "abandon"), ("parked.idle", "restart_generation")),
    ),
    Item(
        "Observations and outcomes without verbs",
        "Whether CI passing without nomination, a CI access wait, and a withdrawn obsolete repair "
        "become table verbs.",
        "None does; all three leave the controller. Green CI without nomination stays in "
        "`development.waiting_ci`. A CI access failure is collector health, not contribution "
        "state. An obsolete repair is not withdrawn; its Result is judged stale on arrival and "
        "takes `record_late_effect`.",
        ("development.agent_running", "development.waiting_ci", "handoff.awaiting_submission"),
        edges=(("development.agent_running", "record_publish_success"),
               ("development.agent_running", "record_late_effect"),
               ("development.waiting_ci", "nominate_for_handoff")),
        highlighted=(("development.agent_running", "record_late_effect"),),
    ),
    Item(
        "Late Results during handoff",
        "What a late Result does in the handoff states.",
        "One rule for every state: a Result that does not match the current generation, "
        "revision, and requested Work is recorded by `record_late_effect` and never changes "
        "state. `record_head_change` leads from `handoff.awaiting_submission` to "
        "`development.waiting_ci`, bumps the revision, and clears the CI evidence. "
        "`return_for_repair` from `handoff.waiting_ci` also fires when the head differs from the "
        "clean head.",
        ("handoff.awaiting_submission", "handoff.waiting_ci", "development.waiting_ci",
         "development.fix_needed"),
        edges=(("handoff.awaiting_submission", "record_late_effect"),
               ("handoff.awaiting_submission", "record_head_change"),
               ("handoff.waiting_ci", "return_for_repair")),
        highlighted=(("handoff.awaiting_submission", "record_late_effect"),
                     ("handoff.awaiting_submission", "record_head_change"),
                     ("handoff.waiting_ci", "return_for_repair")),
    ),
    Item(
        "Budget keys",
        "Whether budgets get their own `policy.json` keys.",
        "Keys are split by what is spent. `work_total` reads "
        "`retries.max_attempts_per_contribution`, `work_recovery` reads `retries.work_recovery`, "
        "`planning_attempts` reads `retries.planning_attempts`, and the infrastructure "
        "budgets share `retries.infrastructure_per_head_failure`. The reminder interval reads "
        "`human_sla_business_days.reminder`. Each Result records its token usage in the events.",
        ("planning.idle", "planning.agent_running", "planning.waiting_human"),
        edges=(("planning.agent_running", "retry_dispatch"),
               ("planning.agent_running", "allocate_work_retry"),
               ("planning.agent_running", "reject_plan"),
               ("planning.agent_running", "exhaust_dispatch_recovery"),
               ("planning.waiting_human", "record_reminder")),
        highlighted=(("planning.agent_running", "allocate_work_retry"),
                     ("planning.agent_running", "reject_plan"),
                     ("planning.waiting_human", "record_reminder")),
    ),
    Item(
        "Flaky CI reruns",
        "How a CI failure is classified before a rerun.",
        "The repair worker classifies. A Result reporting a failure not caused by the patch takes "
        "`request_ci_rerun` from `development.agent_running` to `development.waiting_ci` and "
        "charges `flaky_reruns`; it replaces `record_no_change`. When `flaky_reruns` is "
        "exhausted, or the Work carries feedback, the outcome counts as "
        "`reject_patch_proposal`. The collector reruns on its "
        "own only jobs that were cancelled or lost their runner, in `development.waiting_ci` and "
        "`handoff.waiting_ci`, within `interrupted_reruns` per head. Upstream CI is never rerun.",
        ("development.agent_running", "development.waiting_ci", "development.fix_needed",
         "handoff.waiting_ci"),
        edges=(("development.agent_running", "request_ci_rerun"),
               ("development.agent_running", "reject_patch_proposal"),
               ("development.waiting_ci", "record_ci_failure"),
               ("development.waiting_ci", "rerun_interrupted_ci"),
               ("handoff.waiting_ci", "rerun_interrupted_ci")),
        highlighted=(("development.agent_running", "request_ci_rerun"),
                     ("development.waiting_ci", "rerun_interrupted_ci"),
                     ("handoff.waiting_ci", "rerun_interrupted_ci")),
    ),
    Item(
        "Revision bumps",
        "Which rows bump the revision.",
        "One rule: revision +1 on every transition that invalidates requested or approved work, "
        "and on every resume, flags included. No other row bumps; `confirm_provisioning` lost "
        "its bump. `python -m lifecycle check` enforces the rule on every row.",
        ("provisioning.idle", "development.fix_needed", "handoff.building_upstream_branch",
         "handoff.waiting_human", "release_wait.idle", "release_wait.waiting_human"),
        edges=(("provisioning.idle", "confirm_provisioning"),
               ("handoff.building_upstream_branch", "exhaust_upstream_branch_retries"),
               ("handoff.waiting_human", "resume_upstream_branch"),
               ("release_wait.idle", "require_release_intervention"),
               ("release_wait.waiting_human", "resume_release_observation")),
        highlighted=(("provisioning.idle", "confirm_provisioning"),
                     ("handoff.waiting_human", "resume_upstream_branch"),
                     ("release_wait.waiting_human", "resume_release_observation")),
    ),
    Item(
        "Blocked flag",
        "Whether a block changes the activity, and whether `blocked` is a flag of its own.",
        "A block does not change the activity. `blocked` is merged into `paused`: one flag with "
        "a reason and who set it, a human or the controller. The controller sets it when the "
        "upstream identity changes. The `block` command is retired; `pause` accepts an optional "
        "reason.",
        ("development.waiting_ci", "development.waiting_ci+paused"),
        edges=(("development.waiting_ci", "pause"),
               ("development.waiting_ci", "pause_on_identity_change"),
               ("development.waiting_ci+paused", "resume_pause")),
        highlighted=(("development.waiting_ci", "pause"),
                     ("development.waiting_ci", "pause_on_identity_change")),
    ),
    Item(
        "Remote operation states",
        "Whether remote operations get their own table.",
        "They do: an operation table with the states planned, applying, uncertain, confirmed, "
        "failed, and not launched, under the same checks. `/upstreaming resolve-operation` lives "
        "there. The human waits of the handoff operations are one `handoff.waiting_human`. "
        "Later: publication retries move into the operation table.",
        operations=True,
    ),
)

FURTHER_CHANGES: tuple[Item, ...] = (
    Item(
        "Plan refresh after a stop",
        "When a stop of unknown length makes the plan stale.",
        "The plan is refreshed after a park (`restart_generation`, `/upstreaming refresh`, "
        "always runs the complete planning) and after a pause or human wait longer than `refresh.stale_after_days` "
        "(7) in provisioning or development. There, `resume_stale` (`/upstreaming resume`) leads "
        "to `queued.idle` with the effects of `refresh_planning`; the clock is the older of "
        "`paused_at` and `pending_human_action.requested_at`, and the normal resume verbs carry "
        "the complementary guard, so exactly one applies. `resume_stale` also requires that no "
        "Work is requested, no operation is open, and no publication is pending. A pause in "
        "planning needs no "
        "rule because the Work was cleared. Handoff, upstream review, and release waits are "
        "never refreshed automatically; in `handoff.awaiting_submission` and "
        "`handoff.waiting_upstream_pr` a human uses `/upstreaming refresh`.",
        ("provisioning.waiting_human", "provisioning.idle", "development.waiting_ci+paused",
         "development.waiting_ci", "queued.idle"),
        edges=(("provisioning.waiting_human", "resume_provisioning"),
               ("provisioning.waiting_human", "resume_stale"),
               ("development.waiting_ci+paused", "resume_pause"),
               ("development.waiting_ci+paused", "resume_stale")),
        highlighted=(("provisioning.waiting_human", "resume_stale"),
                     ("development.waiting_ci+paused", "resume_stale")),
    ),
    Item(
        "Budgets",
        "What each budget counts and where its exhaustion leads.",
        "Every row that allocates or reallocates Work charges the per-generation `work_total`. "
        "`provisioning_attempts` and `repair_attempts_total` are removed. Exhaustion of an "
        "infrastructure budget leads to a human wait of the same phase and never ends the "
        "generation. The budgets table of the lifecycle table page lists every budget with its "
        "charge, counter, limit, and exhaustion.",
        ("planning.idle", "planning.agent_running", "planning.waiting_human"),
        edges=(("planning.idle", "dispatch_planning"),
               ("planning.idle", "exhaust_work_total"),
               ("planning.agent_running", "allocate_work_retry"),
               ("planning.agent_running", "exhaust_work_recovery")),
        highlighted=(("planning.idle", "dispatch_planning"),
                     ("planning.idle", "exhaust_work_total"),
                     ("planning.agent_running", "exhaust_work_recovery")),
    ),
    Item(
        "No automatic park from upstream review",
        "A generation restarted after an automatic park would write the fork ref that is the "
        "upstream PR head.",
        "`exhaust_work_recovery` and `exhaust_repair_attempts` no longer park from "
        "`upstream_review`; they wait for a human. `restart_generation` requires that no "
        "upstream PR is open.",
        ("upstream_review.agent_running", "upstream_review.fix_needed",
         "upstream_review.waiting_human", "parked.idle", "queued.idle"),
        edges=(("upstream_review.agent_running", "exhaust_work_recovery"),
               ("upstream_review.fix_needed", "exhaust_repair_attempts"),
               ("upstream_review.waiting_human", "park_contribution"),
               ("parked.idle", "restart_generation")),
        highlighted=(("upstream_review.agent_running", "exhaust_work_recovery"),
                     ("upstream_review.fix_needed", "exhaust_repair_attempts"),
                     ("parked.idle", "restart_generation")),
    ),
    Item(
        "Every Work allocation is charged",
        "`request_patch` and `record_no_change` charged nothing, so the first patch could "
        "loop without bound, and with CI evidence on the head it had no automatic exit.",
        "`request_patch` charges `work_total`, `request_ci_rerun` replaces `record_no_change`, "
        "and `replan_development` leads to `development.fix_needed`.",
        ("development.agent_running", "development.waiting_ci", "development.fix_needed"),
        edges=(("development.fix_needed", "request_patch"),
               ("development.agent_running", "replan_development"),
               ("development.agent_running", "request_ci_rerun"),
               ("development.fix_needed", "request_repair")),
        highlighted=(("development.fix_needed", "request_patch"),
                     ("development.agent_running", "replan_development")),
    ),
    Item(
        "Reminders for every human step",
        "`handoff.awaiting_submission` had no reminder although it is the only mandatory human "
        "step.",
        "`record_reminder` is a rule of the `gate`, `human_wait`, and `locked` kinds.",
        ("handoff.awaiting_submission", "handoff.review_due"),
        edges=(("handoff.awaiting_submission", "record_reminder"),
               ("handoff.awaiting_submission", "submit_upstream")),
        highlighted=(("handoff.awaiting_submission", "record_reminder"),),
    ),
    Item(
        "Head change in upstream review",
        "`upstream_review.waiting_ci` had no head-change row.",
        "A head change that the pipeline did not publish takes `record_head_change` to "
        "`upstream_review.waiting_human`. The head a revision of the pipeline published is "
        "recorded by `record_revision_publish_success`, at a new revision, from the head the "
        "publisher read back on `riscv64`; a publication whose push was not read back is a "
        "failed publication.",
        ("upstream_review.agent_running", "upstream_review.waiting_ci",
         "upstream_review.waiting_human"),
        edges=(("upstream_review.waiting_ci", "record_head_change"),
               ("upstream_review.agent_running", "record_revision_publish_success")),
        highlighted=(("upstream_review.waiting_ci", "record_head_change"),),
    ),
    Item(
        "Explanations in upstream review",
        "Replies to reviewers are only safe on the development PR.",
        "In `upstream_review`, `record_explanation` records the comment and links it from the "
        "event log of the website; nothing is posted on the upstream PR.",
        ("upstream_review.waiting_ci",),
        edges=(("upstream_review.waiting_ci", "record_explanation"),),
        highlighted=(("upstream_review.waiting_ci", "record_explanation"),),
    ),
    Item(
        "Infrastructure failure of the branch write",
        "The handoff state that writes the clean head had no row for an infrastructure failure "
        "of the lease write.",
        "The lease write retries within its budget and then leads to the human wait "
        "`handoff.waiting_human`, like the other handoff operation. The write is now the one of "
        "`riscv64` in `handoff.building_upstream_branch`: `retry_upstream_branch` within "
        "`upstream_branch`, then `exhaust_upstream_branch_retries`.",
        ("handoff.building_upstream_branch", "handoff.waiting_human", "handoff.waiting_ci"),
        edges=(("handoff.building_upstream_branch", "retry_upstream_branch"),
               ("handoff.building_upstream_branch", "exhaust_upstream_branch_retries"),
               ("handoff.waiting_human", "resume_upstream_branch"),
               ("handoff.building_upstream_branch", "confirm_clean_head")),
        highlighted=(("handoff.building_upstream_branch", "retry_upstream_branch"),
                     ("handoff.building_upstream_branch", "exhaust_upstream_branch_retries"),
                     ("handoff.waiting_human", "resume_upstream_branch")),
    ),
    Item(
        "Resumes of handoff and release waits bump the revision",
        "The resume of the development PR closure (retired since: the development PR is never "
        "closed), `resume_submission_preparation`, and `resume_release_observation` did not bump "
        "the revision.",
        "They bump it, like every resume.",
        ("handoff.preparing_submission", "handoff.waiting_human"),
        edges=(("handoff.preparing_submission", "exhaust_comment_retries"),
               ("handoff.waiting_human", "resume_submission_preparation")),
        highlighted=(("handoff.waiting_human", "resume_submission_preparation"),),
    ),
    Item(
        "Three exits from the locked upstream-PR wait",
        "`handoff.waiting_upstream_pr` was defined as having exactly two exits.",
        "It has three: `verify_upstream_pr`, `retract_submission`, and `refresh_planning`, "
        "the last with the guards of `retract_submission` (exact revision, exact clean head, "
        "quiescent). The check enforces the three exits.",
        ("handoff.waiting_upstream_pr", "upstream_review.waiting_ci", "development.fix_needed",
         "queued.idle"),
        edges=(("handoff.waiting_upstream_pr", "verify_upstream_pr"),
               ("handoff.waiting_upstream_pr", "retract_submission"),
               ("handoff.waiting_upstream_pr", "refresh_planning")),
        highlighted=(("handoff.waiting_upstream_pr", "refresh_planning"),),
    ),
    Item(
        "An error in one contribution's processing",
        "Whether an unexpected error while the controller processes one contribution stops the "
        "tick for every contribution.",
        "It does not. Staging is all or nothing per contribution: everything the tick staged "
        "for the contribution (record, events, operations, evidence) is discarded, none of its "
        "effects is requested, and the tick continues with the other contributions. The "
        "controller then takes `pause_on_processing_error` on the record as it was committed "
        "before the tick: a set verb of `paused` that applies in every non-terminal state and "
        "keeps the state. It records the error class, the message, and the run id in "
        "`paused_reason`, `paused_by=controller`, and `paused_at`, and follows the pause "
        "semantics: the requested Work and the pending publication are cleared, and `resume_pause` bumps the revision. The error is posted on the tracking "
        "issue and listed in the tick report under `alerts`. Errors in two or more "
        "contributions in one tick are a pipeline failure: the whole tick stops, nothing is "
        "committed, and no contribution is paused. An error outside any contribution (state "
        "load, policy, schema validation of the store, the commit) stops the whole tick as "
        "before. Expected rejections (a refused transition of a command, a rejected command, "
        "an intake error) are rejected with a reason and pause nothing. A collector failure "
        "for one contribution skips that contribution for the tick with no state change and "
        "no pause, and the report records it. Stop commands are still applied first; when "
        "applying one fails, the contribution is treated as errored and nothing is launched "
        "for it in that tick.",
        ("development.waiting_ci", "development.waiting_ci+paused", "development.agent_running",
         "development.fix_needed+paused", "development.fix_needed"),
        edges=(("development.waiting_ci", "pause_on_processing_error"),
               ("development.agent_running", "pause_on_processing_error"),
               ("development.waiting_ci+paused", "resume_pause"),
               ("development.fix_needed+paused", "resume_pause")),
        highlighted=(("development.waiting_ci", "pause_on_processing_error"),
                     ("development.agent_running", "pause_on_processing_error")),
    ),
    Item(
        "Development on `riscv64-dev`, upstreaming from a reviewed `riscv64`",
        "Which branches development and the upstream PR use, whether the development PR is "
        "closed at submission, and how the upstream PR follows the project's pull request "
        "template, contributing instructions, commit conventions, and reviewers.",
        "Development happens on `riscv64-dev`: fork provisioning creates it from the base, and "
        "worker Work, publication, the development PR, its CI, and head observation use it. It "
        "is never changed for upstreaming, and the development PR is never closed: the "
        "development PR closure, its operation, budget, state, and resume are removed. "
        "`submit_upstream` leads to `handoff.review_due`, where `request_submission_rewrite` "
        "allocates uncredentialed rewrite Work (`handoff.agent_running`). The rewrite reshapes "
        "the non-debug commits of the approved series after the plan's measured commit "
        "conventions (squashing or splitting only where the contributing instructions or "
        "reviewers require it) and writes the upstream PR title and description after the pull "
        "request template and contributing instructions, which take precedence over the "
        "pipeline's instructions for content and style. Trusted code checks every commit, the "
        "same final tree, the attribution trailers, the measurable rules, and the template's "
        "required sections: `accept_submission_rewrite` bumps the revision and leads to "
        "`handoff.review_pending`, where `request_submission_review` allocates separate "
        "uncredentialed review Work (`handoff.reviewing`) in a fresh context that reviews "
        "exactly the rewritten series and text against the template, the contributing "
        "instructions, the commit conventions, and the reviewers' expectations and can change "
        "nothing. `accept_submission_review` leads to `handoff.building_upstream_branch`, which "
        "creates `riscv64` from the rewritten series or replaces it under a lease, never while "
        "an upstream PR is open from it (`require_branch_intervention`). A failed rewrite check "
        "(`reject_submission_rewrite`) or a failed review (`reject_submission_review`) returns "
        "to development with the reasons as pending feedback and posts nothing; a Result that "
        "is not usable is a Work retry (`allocate_work_retry`, `exhaust_work_recovery`), not a "
        "verdict. A rewrite whose artifact expires before `riscv64` is built "
        "(`expire_submission_rewrite`) leads back to `handoff.review_due`. CI runs on the clean head of "
        "`riscv64` as before, the package names `riscv64` and the reviewed title and "
        "description, and verification, reminders, and the retraction refer to `riscv64`. A "
        "retraction leaves `riscv64` as it is.",
        ("handoff.awaiting_submission", "handoff.review_due", "handoff.agent_running",
         "handoff.review_pending", "handoff.reviewing", "development.fix_needed"),
        edges=(("handoff.awaiting_submission", "submit_upstream"),
               ("handoff.review_due", "request_submission_rewrite"),
               ("handoff.agent_running", "accept_submission_rewrite"),
               ("handoff.agent_running", "reject_submission_rewrite"),
               ("handoff.review_pending", "request_submission_review"),
               ("handoff.reviewing", "reject_submission_review")),
        highlighted=(("handoff.review_due", "request_submission_rewrite"),
                     ("handoff.agent_running", "accept_submission_rewrite"),
                     ("handoff.agent_running", "reject_submission_rewrite"),
                     ("handoff.reviewing", "reject_submission_review")),
    ),
    Item(
        "When `riscv64` may not be written",
        "Which states of the fork keep the build of `riscv64` from writing it.",
        "`require_branch_intervention` applies when a pull request is open from `riscv64`, "
        "upstream or in the fork, when `riscv64-dev` is missing on the fork, or when the "
        "record's development branch is not `riscv64-dev`: `riscv64` is not written, the "
        "reason is recorded, and a human resolves it and resumes (`resume_upstream_branch`).",
        ("handoff.building_upstream_branch", "handoff.waiting_human"),
        edges=(("handoff.building_upstream_branch", "require_branch_intervention"),
               ("handoff.waiting_human", "resume_upstream_branch")),
        highlighted=(("handoff.building_upstream_branch", "require_branch_intervention"),),
    ),
    Item(
        "CI on a clean head of `riscv64` that has no pull request",
        "How CI runs on a rebuilt `riscv64` head that has another tree than the candidate head, "
        "and what happens when it never runs.",
        "The build of `riscv64` (`confirm_clean_head`) dispatches the project's required CI "
        "workflow on `riscv64` when the workflow, read as data at the clean head, accepts "
        "`workflow_dispatch` with no required input, and reads its run back by head; the fork "
        "token gains `actions: write` on the one fork for it. `confirm_clean_head` records when "
        "the wait for CI began (`clean_head_ci_since`). `require_clean_head_ci` leads from "
        "`handoff.waiting_ci` to `handoff.waiting_human` with the pending action "
        "`clean_head_ci_missing` at once when no run exists and the workflow cannot be "
        "dispatched, and in any case when CI is not complete 12 hours after the wait began; "
        "reminders apply, and `resume_clean_head_ci` returns to `handoff.waiting_ci`, observes "
        "CI again, and starts the wait again.",
        ("handoff.building_upstream_branch", "handoff.waiting_ci", "handoff.waiting_human"),
        edges=(("handoff.building_upstream_branch", "confirm_clean_head"),
               ("handoff.waiting_ci", "require_clean_head_ci"),
               ("handoff.waiting_human", "resume_clean_head_ci")),
        highlighted=(("handoff.waiting_ci", "require_clean_head_ci"),
                     ("handoff.waiting_human", "resume_clean_head_ci")),
    ),
    Item(
        "Head change while development Work is due",
        "`development.fix_needed` had no row for a fork head that moved while Work was due, so "
        "no Work could be requested for the recorded head and nothing recorded the new one.",
        "A head change in `development.fix_needed` takes `record_head_change` to "
        "`development.waiting_ci`: the new head is recorded, the revision is bumped, the rerun "
        "counts start again, and CI is awaited for the new head. The pending feedback is kept, "
        "and `return_for_feedback` makes Work due again once CI passes on the new head.",
        ("development.fix_needed", "development.waiting_ci"),
        edges=(("development.fix_needed", "record_head_change"),),
        highlighted=(("development.fix_needed", "record_head_change"),),
    ),
    Item(
        "Projects of any language",
        "Whether planning and development serve projects other than Python wheels.",
        "They do: a project in any language (Go, Rust, C or C++, Java, JavaScript, Python, or "
        "another) is planned and developed for riscv64 on its own build, test, and release "
        "tooling, and Python wheels are one case among others. A plan of schema version 3 "
        "records the project (languages, build systems, build and test, release channels with "
        "their riscv64 status, and the riscv64 toolchain); stored plans of versions 1 and 2 "
        "stay readable. The path coverage areas and the admission classes keep their names "
        "with a language-neutral meaning. A decision close is still confirmed from PyPI only, "
        "and each PyPI project must name the upstream repository.",
        ("planning.agent_running", "provisioning.idle", "parked.idle", "closed.idle"),
        edges=(("planning.agent_running", "admit_development"),
               ("planning.agent_running", "park_planning"),
               ("planning.agent_running", "close_planning")),
        highlighted=(("planning.agent_running", "close_planning"),),
    ),
    Item(
        "A parked contribution restarts with refresh",
        "Which command starts a new generation from `parked.idle`.",
        "In `parked.idle` the clean restart (`restart_generation`) and its admission override "
        "are requested with `/upstreaming refresh`, also while paused, and the new generation "
        "then stays paused until a resume. `/upstreaming resume` there only ends a pause; on a "
        "contribution that is not paused it is rejected with a reply that names the refresh "
        "form, and `class` and `paths` on a resume are rejected with the refresh form of the "
        "override. A state of kind `generation_end` has a refresh exit and no resume row.",
        ("parked.idle", "queued.idle", "closed.idle"),
        edges=(("parked.idle", "restart_generation"),
               ("parked.idle", "abandon")),
        highlighted=(("parked.idle", "restart_generation"),),
    ),
)

SETTLED_OPEN_ITEMS: tuple[Item, ...] = (
    Item(
        "Merge `development.idle` and `development.fix_needed`",
        "Whether the two states in which development Work is due become one.",
        "One state, `development.fix_needed`: development Work is due. `confirm_provisioning` "
        "enters it. `request_repair` applies while a failing CI job is recorded for the head; "
        "`request_patch` applies otherwise. "
        "`development.idle` and its own exhaustion row are removed, also from the states that "
        "accept `refresh_planning`.",
        ("provisioning.idle", "development.fix_needed", "development.agent_running",
         "parked.idle"),
        edges=(("provisioning.idle", "confirm_provisioning"),
               ("development.fix_needed", "request_patch"),
               ("development.fix_needed", "request_repair"),
               ("development.fix_needed", "exhaust_repair_attempts")),
        highlighted=(("provisioning.idle", "confirm_provisioning"),
                     ("development.fix_needed", "request_patch"),
                     ("development.fix_needed", "request_repair")),
    ),
    Item(
        "State after a pause that clears requested Work",
        "Which state a contribution is in after `pause` cleared its requested Work.",
        "`pause` in an `agent_running` state leads to the state in which the Work is due again: "
        "`planning.idle`, `development.fix_needed`, or `upstream_review.fix_needed`. It is "
        "the one exception to a flag keeping phase and activity, and the description of the "
        "flag states it.",
        ("planning.agent_running", "planning.idle+paused", "development.agent_running",
         "development.fix_needed+paused", "upstream_review.agent_running",
         "upstream_review.fix_needed+paused"),
        edges=(("planning.agent_running", "pause"),
               ("development.agent_running", "pause"),
               ("upstream_review.agent_running", "pause")),
        highlighted=(("planning.agent_running", "pause"),
                     ("development.agent_running", "pause"),
                     ("upstream_review.agent_running", "pause")),
    ),
    Item(
        "The `block` command after the flag merge",
        "Whether `/upstreaming block` stays a command once `blocked` is merged into `paused`.",
        "It is retired. `pause` accepts an optional reason, and a pause set by the controller "
        "always records one.",
        ("development.waiting_ci", "development.waiting_ci+paused"),
        edges=(("development.waiting_ci", "pause"),
               ("development.waiting_ci", "pause_on_identity_change"),
               ("development.waiting_ci+paused", "resume_pause")),
        highlighted=(("development.waiting_ci", "pause"),
                     ("development.waiting_ci", "pause_on_identity_change")),
    ),
    Item(
        "`resume_stale` with an open operation",
        "Whether `resume_stale` needs quiescence, as `refresh_planning` does.",
        "It does: `resume_stale` requires that no Work is requested, no operation is open, and "
        "no publication is pending. Until then no resume applies, and the rejection names the "
        "open operation.",
        ("provisioning.idle", "provisioning.idle+paused", "queued.idle"),
        edges=(("provisioning.idle+paused", "resume_pause"),
               ("provisioning.idle+paused", "resume_stale")),
        highlighted=(("provisioning.idle+paused", "resume_stale"),),
    ),
    Item(
        "Resume while paused in a human wait",
        "Which transition one `/upstreaming resume` takes when a contribution is both paused "
        "and in a human wait.",
        "Two resumes. The first clears the flag; its reply states that the contribution still "
        "waits for a human and names the pending action. The second leaves the human wait.",
        ("development.waiting_human+paused", "development.waiting_human",
         "development.fix_needed"),
        edges=(("development.waiting_human+paused", "resume_pause"),
               ("development.waiting_human", "resume_development")),
        highlighted=(("development.waiting_human+paused", "resume_pause"),
                     ("development.waiting_human", "resume_development")),
    ),
    Item(
        "Resume after a budget is exhausted",
        "What a resume does to the counter whose exhaustion caused the human wait.",
        "The resume starts again the one counter named in `pending_human_action.counter`, and "
        "no other. The event records the actor, the counter, and its count at that point. It "
        "was settled for `exhaust_work_total`, `exhaust_planning_recovery`, and "
        "`exhaust_repair_attempts` in upstream review; O14 extends it to every exhaustion that "
        "leads to a human wait.",
        ("planning.idle", "planning.agent_running", "planning.waiting_human",
         "upstream_review.fix_needed", "upstream_review.waiting_human"),
        edges=(("planning.idle", "exhaust_work_total"),
               ("planning.agent_running", "exhaust_planning_recovery"),
               ("planning.waiting_human", "resume_planning"),
               ("upstream_review.fix_needed", "exhaust_repair_attempts"),
               ("upstream_review.waiting_human", "resume_upstream_revision")),
        highlighted=(("planning.waiting_human", "resume_planning"),
                     ("upstream_review.waiting_human", "resume_upstream_revision")),
    ),
    Item(
        "Budget for collector reruns",
        "Whether reruns of cancelled or runner-lost jobs are bounded.",
        "They are: `rerun_interrupted_ci` charges the budget `interrupted_reruns`, counted per "
        "head, which reads the shared `retries.infrastructure_per_head_failure`. When it is "
        "exhausted, `exhaust_interrupted_reruns` leads to the human wait of the phase with "
        "`pending_human_action=ci_interrupted`, and `resume_ci` returns to the CI wait, "
        "reruns the jobs, and starts `interrupted_reruns` again for the head (O14).",
        ("development.waiting_ci", "development.waiting_human", "handoff.waiting_ci",
         "handoff.waiting_human"),
        edges=(("development.waiting_ci", "rerun_interrupted_ci"),
               ("development.waiting_ci", "exhaust_interrupted_reruns"),
               ("development.waiting_human", "resume_ci"),
               ("handoff.waiting_ci", "rerun_interrupted_ci"),
               ("handoff.waiting_ci", "exhaust_interrupted_reruns"),
               ("handoff.waiting_human", "resume_ci")),
        highlighted=(("development.waiting_ci", "exhaust_interrupted_reruns"),
                     ("development.waiting_human", "resume_ci"),
                     ("handoff.waiting_ci", "exhaust_interrupted_reruns"),
                     ("handoff.waiting_human", "resume_ci")),
    ),
    Item(
        "Limits and names chosen without a decision",
        "Values and names the decisions left open.",
        "`retries.max_attempts_per_contribution` stays 24, `retries.work_recovery` stays 2, and "
        "the verb names stay. The human waits of handoff are one "
        "`handoff.waiting_human`: four resume verbs leave it, each guarded by its "
        "`pending_human_action`, so exactly one applies (the submission review, the branch "
        "write, CI on the clean head, and the package).",
        ("handoff.review_due", "handoff.agent_running", "handoff.building_upstream_branch",
         "handoff.waiting_ci", "handoff.preparing_submission", "handoff.waiting_human"),
        edges=(("handoff.review_due", "exhaust_work_total"),
               ("handoff.agent_running", "exhaust_work_recovery"),
               ("handoff.building_upstream_branch", "exhaust_upstream_branch_retries"),
               ("handoff.waiting_ci", "exhaust_interrupted_reruns"),
               ("handoff.preparing_submission", "exhaust_comment_retries"),
               ("handoff.waiting_human", "resume_submission_review"),
               ("handoff.waiting_human", "resume_upstream_branch"),
               ("handoff.waiting_human", "resume_ci"),
               ("handoff.waiting_human", "resume_submission_preparation")),
        highlighted=(("handoff.waiting_human", "resume_submission_review"),
                     ("handoff.waiting_human", "resume_upstream_branch"),
                     ("handoff.waiting_human", "resume_ci"),
                     ("handoff.waiting_human", "resume_submission_preparation")),
    ),
    Item(
        "Reminder of the locked upstream-PR wait",
        "Whether the reminder rule also covers the `locked` kind.",
        "It does: the rule covers the `gate`, `human_wait`, and `locked` kinds, and the "
        "separate row is removed. In `handoff.waiting_upstream_pr` the package link is part of "
        "the reminder text. The reminder keeps the state, so the locked wait still has exactly "
        "its three exits.",
        ("handoff.awaiting_submission", "handoff.waiting_upstream_pr", "handoff.waiting_human"),
        edges=(("handoff.awaiting_submission", "record_reminder"),
               ("handoff.waiting_upstream_pr", "record_reminder"),
               ("handoff.waiting_human", "record_reminder")),
        highlighted=(("handoff.waiting_upstream_pr", "record_reminder"),),
    ),
    Item(
        "Where a development PR mismatch is observed",
        "From which states `record_pr_mismatch` leads to `development.waiting_human`.",
        "From `development.waiting_ci` only, where the development PR is observed.",
        ("development.waiting_ci", "development.waiting_human"),
        edges=(("development.waiting_ci", "record_pr_mismatch"),),
        highlighted=(("development.waiting_ci", "record_pr_mismatch"),),
    ),
    Item(
        "Admission override against a park or close decision",
        "Which plan decisions a recorded `admission_override` overrides.",
        "A decision park only: with a recorded override, `admit_development` accepts a verified "
        "plan that decides park. A verified decision close still takes "
        "`close_planning`, and the reply states that the override was not applied.",
        ("planning.agent_running", "provisioning.idle", "parked.idle", "closed.idle"),
        edges=(("planning.agent_running", "admit_development"),
               ("planning.agent_running", "park_planning"),
               ("planning.agent_running", "close_planning")),
        highlighted=(("planning.agent_running", "admit_development"),
                     ("planning.agent_running", "close_planning")),
    ),
    Item(
        "Resume after a reviewer was answered",
        "Where the resume leads when the pending action was to answer the reviewer.",
        "Two verbs. `resume_upstream_review` leads to `upstream_review.waiting_ci` when the "
        "pending action is `pr_closed_unmerged` (the PR is observed open again), "
        "`answer_reviewer`, or `upstream_head_changed`; after `answer_reviewer` it clears the "
        "pending feedback. `resume_upstream_revision` leads to `upstream_review.fix_needed` "
        "only when revision Work failed: dispatch, Work, publication, or budget exhaustion. "
        "`record_upstream_ci_failure` fires once per head and CI run, so the failure that was "
        "answered does not request a revision again.",
        ("upstream_review.waiting_ci", "upstream_review.fix_needed",
         "upstream_review.agent_running", "upstream_review.waiting_human"),
        edges=(("upstream_review.waiting_ci", "record_upstream_ci_failure"),
               ("upstream_review.agent_running", "require_reviewer_answer"),
               ("upstream_review.waiting_human", "resume_upstream_review"),
               ("upstream_review.waiting_human", "resume_upstream_revision")),
        highlighted=(("upstream_review.waiting_ci", "record_upstream_ci_failure"),
                     ("upstream_review.waiting_human", "resume_upstream_review"),
                     ("upstream_review.waiting_human", "resume_upstream_revision")),
    ),
    Item(
        "The revision rule as checked",
        "Which rows count as invalidating requested or approved work.",
        "The rule stays derived from the effects: a resume, a generation bump, a fresh Work "
        "graph, a change of scope, feedback, refresh, handoff request, approval, or clean head, "
        "a recorded head change (observed, or read back by a Result), and a rejected Result "
        "after which the same Work is due again. "
        "Under it `replan_development` bumps and `park_planning` does not, because a park "
        "is a stop and the restart bumps.",
        ("planning.agent_running", "parked.idle", "development.agent_running",
         "development.fix_needed"),
        edges=(("planning.agent_running", "park_planning"),
               ("development.agent_running", "replan_development")),
        highlighted=(("planning.agent_running", "park_planning"),
                     ("development.agent_running", "replan_development")),
    ),
    Item(
        "The rerun counter after `resume_ci`",
        "Whether `resume_ci` starts `interrupted_reruns` again for the head.",
        "It does, in both `development.waiting_human` and `handoff.waiting_human`. The rule has "
        "no exception: every exhaustion that leads to a human wait names its counter in "
        "`pending_human_action.counter`, and every resume from that wait starts exactly that "
        "counter again. This holds for the budgets spent on infrastructure failures too: "
        "`dispatch_recovery`, `work_recovery`, `publish_recovery`, `operation_launch`, "
        "`upstream_branch`, `submission_comment`, and "
        "`interrupted_reruns`. `require_provisioning_intervention` names `operation_launch` "
        "when that budget is the cause and no counter for its other causes. `resume_stale` "
        "starts the named counter again like every other resume. The check verifies the rule "
        "for every exhaustion and every resume.",
        ("development.waiting_ci", "development.waiting_human", "handoff.waiting_ci",
         "handoff.building_upstream_branch", "handoff.waiting_human"),
        edges=(("development.waiting_ci", "exhaust_interrupted_reruns"),
               ("development.waiting_human", "resume_ci"),
               ("handoff.waiting_ci", "exhaust_interrupted_reruns"),
               ("handoff.waiting_human", "resume_ci"),
               ("handoff.building_upstream_branch", "exhaust_upstream_branch_retries"),
               ("handoff.waiting_human", "resume_upstream_branch")),
        highlighted=(("development.waiting_human", "resume_ci"),
                     ("handoff.waiting_human", "resume_ci")),
    ),
    Item(
        "The first patch after a rejected proposal",
        "Which verb requests Work when a proposal was rejected before the head had any CI "
        "evidence.",
        "`request_patch`: a rejected proposal records no failing CI job for the head. "
        "`request_patch` charges `work_total` and requires that `no_progress` remains; it does "
        "not charge `repair_attempts_cluster`.",
        ("development.fix_needed", "development.agent_running", "queued.idle", "parked.idle"),
        edges=(("development.fix_needed", "request_patch"),
               ("development.agent_running", "reject_patch_proposal"),
               ("development.fix_needed", "request_repair"),
               ("development.fix_needed", "refresh_on_exhaustion"),
               ("development.fix_needed", "exhaust_repair_attempts")),
        highlighted=(("development.fix_needed", "request_patch"),
                     ("development.agent_running", "reject_patch_proposal")),
    ),
    Item(
        "Exhaustion of `work_total` found while Work is running",
        "Whether the resume after `exhaust_work_recovery` starts `work_total` again when "
        "`work_total` was the exhausted budget.",
        "It does. When a Work retry cannot be allocated because `work_total` is exhausted, "
        "`exhaust_work_recovery` records `pending_human_action.counter=work_total`, and one "
        "resume starts it again, in planning, development, and upstream review. When "
        "`work_recovery` itself is exhausted, it names `work_recovery`. The slice shows "
        "planning and development; in upstream review `resume_upstream_revision` does the "
        "same.",
        ("planning.agent_running", "planning.waiting_human", "planning.idle",
         "development.agent_running", "development.waiting_human", "development.fix_needed"),
        edges=(("planning.agent_running", "exhaust_work_recovery"),
               ("planning.waiting_human", "resume_planning"),
               ("development.agent_running", "exhaust_work_recovery"),
               ("development.waiting_human", "resume_development")),
        highlighted=(("planning.agent_running", "exhaust_work_recovery"),
                     ("planning.waiting_human", "resume_planning"),
                     ("development.agent_running", "exhaust_work_recovery"),
                     ("development.waiting_human", "resume_development")),
    ),
    Item(
        "Names and values chosen for the settled items",
        "Names and values the decisions left open.",
        "They stay. `interrupted_reruns` allows as many reruns per head as "
        "`retries.infrastructure_per_head_failure` (2), without a free first one. Its "
        "exhaustion verb is `exhaust_interrupted_reruns`, and `resume_ci` is the resume verb in "
        "both `development.waiting_human` and `handoff.waiting_human`. The pending actions of "
        "the handoff wait are `upstream_branch_failed`, `upstream_branch_blocked`, "
        "`submission_comment_failed`, `ci_interrupted`, and those of the review Work "
        "(`work_budget_exhausted`, `dispatch_failed`, `work_failed`). An exhaustion names its "
        "counter in `pending_human_action.counter`.",
        ("handoff.building_upstream_branch", "handoff.waiting_ci",
         "handoff.preparing_submission", "handoff.waiting_human"),
        edges=(("handoff.building_upstream_branch", "exhaust_upstream_branch_retries"),
               ("handoff.waiting_ci", "rerun_interrupted_ci"),
               ("handoff.waiting_ci", "exhaust_interrupted_reruns"),
               ("handoff.preparing_submission", "exhaust_comment_retries"),
               ("handoff.waiting_human", "resume_ci")),
        highlighted=(("handoff.waiting_ci", "rerun_interrupted_ci"),
                     ("handoff.waiting_ci", "exhaust_interrupted_reruns"),
                     ("handoff.waiting_human", "resume_ci")),
    ),
    Item(
        "Exhaustion of `work_total` found when development Work is due",
        "Where `work_total` exhaustion leads when it is found at `development.fix_needed`.",
        "To the human wait, as in the other phases. At `development.fix_needed`, exhaustion of "
        "`work_total` takes `exhaust_work_total` to `development.waiting_human` with "
        "`pending_human_action.counter=work_total`, and one resume starts it again. "
        "`exhaust_repair_attempts` parks only for `repair_attempts_cluster` and `no_progress`, "
        "after `refresh_on_exhaustion` is used. The reply at exhaustion states the number of "
        "Work allocations spent and names both `/upstreaming resume` and `/upstreaming park`. "
        "The guards of `request_patch`, `request_repair`, `refresh_on_exhaustion`, "
        "`exhaust_repair_attempts`, and `exhaust_work_total` are mutually exclusive, and the "
        "check verifies it: the first four require that `work_total` remains.",
        ("development.fix_needed", "development.agent_running", "development.waiting_human",
         "queued.idle", "parked.idle"),
        edges=(("development.fix_needed", "request_patch"),
               ("development.fix_needed", "request_repair"),
               ("development.fix_needed", "refresh_on_exhaustion"),
               ("development.fix_needed", "exhaust_repair_attempts"),
               ("development.fix_needed", "exhaust_work_total"),
               ("development.waiting_human", "resume_development"),
               ("development.waiting_human", "park_contribution")),
        highlighted=(("development.fix_needed", "exhaust_work_total"),
                     ("development.fix_needed", "exhaust_repair_attempts"),
                     ("development.waiting_human", "resume_development")),
    ),
    Item(
        "Pending feedback and the Work that answers it",
        "Which row clears `pending_feedback_evidence_ids` in development and upstream review.",
        "The Work request carries the pending feedback ids present at allocation, in "
        "`active_work.feedback_evidence_ids`. `record_publish_success` clears exactly those "
        "ids, and `record_revision_publish_success` does the same in upstream review; feedback "
        "recorded while the Work ran stays pending. `reject_patch_proposal`, "
        "`replan_development`, `record_head_change`, every resume that requests Work again, and both refresh verbs "
        "keep the pending feedback; `resume_upstream_review` still clears it after a human "
        "answered the reviewer. A fresh Work graph and a publish retry carry the same ids. Work "
        "that carries feedback cannot take `request_ci_rerun`: that outcome counts as "
        "`reject_patch_proposal`. The check verifies each of these.",
        ("development.fix_needed", "development.agent_running", "development.waiting_ci"),
        edges=(("development.waiting_ci", "record_actionable_review"),
               ("development.fix_needed", "request_patch"),
               ("development.fix_needed", "request_repair"),
               ("development.agent_running", "record_publish_success"),
               ("development.agent_running", "reject_patch_proposal"),
               ("development.agent_running", "replan_development"),
               ("development.agent_running", "request_ci_rerun")),
        highlighted=(("development.agent_running", "record_publish_success"),
                     ("development.agent_running", "request_ci_rerun"),
                     ("development.agent_running", "reject_patch_proposal")),
    ),
    Item(
        "An actionable review at the submission gate",
        "How a review on the candidate head is recorded when it leaves "
        "`handoff.awaiting_submission`.",
        "By its own row. `record_actionable_review` leads from `handoff.awaiting_submission` "
        "to `development.fix_needed`: it bumps the revision once, sets "
        "`pending_feedback_evidence_ids`, and clears `clean_head_sha`, `handoff_request`, and "
        "`pending_human_action`. `return_for_repair` from the gate keeps the CI failure cause "
        "only and sets `last_ci_evidence_id`. The two guards are exclusive: a CI failure on "
        "the candidate head applies first, and a review observed with it is recorded in the "
        "same observation by `record_actionable_review` in `development.fix_needed`, so the "
        "repair Work carries both. That row also records a review posted on the recorded "
        "head while Work is due.",
        ("handoff.awaiting_submission", "development.fix_needed", "development.agent_running"),
        edges=(("handoff.awaiting_submission", "return_for_repair"),
               ("handoff.awaiting_submission", "record_actionable_review"),
               ("development.fix_needed", "record_actionable_review"),
               ("development.fix_needed", "request_patch"),
               ("development.fix_needed", "request_repair")),
        highlighted=(("handoff.awaiting_submission", "return_for_repair"),
                     ("handoff.awaiting_submission", "record_actionable_review"),
                     ("development.fix_needed", "record_actionable_review")),
    ),
    Item(
        "An explanation-only comment at the submission gate",
        "What an explanation-only comment on the development PR does while the contribution "
        "waits for `submit-upstream`.",
        "`record_explanation` is a row of `handoff.awaiting_submission` that keeps the state: "
        "the comment is recorded and linked from the event log of the website. No Work is requested and "
        "nothing is answered automatically; the revision, the candidate head, and the CI "
        "evidence are kept, so the pending `submit-upstream` request stays valid.",
        ("handoff.awaiting_submission", "handoff.review_due"),
        edges=(("handoff.awaiting_submission", "record_explanation"),
               ("handoff.awaiting_submission", "submit_upstream")),
        highlighted=(("handoff.awaiting_submission", "record_explanation"),),
    ),
    Item(
        "Where the rerun counts of a head are kept",
        "Which record field holds the counts of `flaky_reruns` and `interrupted_reruns`.",
        "`budget.reruns = {head_sha, flaky, interrupted}`, for the current head only. It starts "
        "again whenever the recorded head changes: on `record_publish_success`, "
        "`record_head_change`, and `confirm_clean_head`. `request_ci_rerun` counts `flaky`, "
        "`rerun_interrupted_ci` counts `interrupted`, and `resume_ci` starts `interrupted` "
        "again. Counts recorded for another head are counts of zero for the current one.",
        ("development.agent_running", "development.waiting_ci", "development.waiting_human",
         "handoff.building_upstream_branch", "handoff.waiting_ci"),
        edges=(("development.agent_running", "record_publish_success"),
               ("development.agent_running", "request_ci_rerun"),
               ("development.waiting_ci", "record_head_change"),
               ("development.waiting_ci", "rerun_interrupted_ci"),
               ("development.waiting_ci", "exhaust_interrupted_reruns"),
               ("development.waiting_human", "resume_ci"),
               ("handoff.building_upstream_branch", "confirm_clean_head"),
               ("handoff.waiting_ci", "rerun_interrupted_ci")),
        highlighted=(("development.agent_running", "record_publish_success"),
                     ("development.waiting_ci", "record_head_change"),
                     ("handoff.building_upstream_branch", "confirm_clean_head")),
    ),
    Item(
        "The budget of a repair driven only by review feedback",
        "Which cluster a repair charges when no CI job failed.",
        "None: `repair_attempts_cluster` is charged only when a failing CI job is recorded for "
        "the head. In development, `request_repair` is guarded by that, and `request_patch` by "
        "its complement: it covers the first patch, a rejected proposal, and a repair driven "
        "only by feedback, and charges `work_total` only. In upstream review, "
        "`request_upstream_revision` keeps the failing-job guard and the cluster charge, and "
        "`request_review_revision` covers a revision driven only by feedback and charges "
        "`work_total` only. All four require that `no_progress` remains. Exhaustion of "
        "`repair_attempts_cluster` applies only while a failing CI job is recorded, so in each "
        "state exactly one of the rows applies; the check verifies it.",
        ("development.fix_needed", "development.agent_running", "upstream_review.fix_needed",
         "upstream_review.agent_running", "upstream_review.waiting_human"),
        edges=(("development.fix_needed", "request_patch"),
               ("development.fix_needed", "request_repair"),
               ("upstream_review.fix_needed", "request_upstream_revision"),
               ("upstream_review.fix_needed", "request_review_revision"),
               ("upstream_review.fix_needed", "exhaust_repair_attempts")),
        highlighted=(("development.fix_needed", "request_patch"),
                     ("upstream_review.fix_needed", "request_review_revision")),
    ),
    Item(
        "A proposal before the fork exists",
        "Whether patch Work can be requested before `confirm_provisioning`.",
        "It cannot. Patch Work is requested only from `development.fix_needed`, which is "
        "entered after `confirm_provisioning`. The guard of `request_patch` is write admission "
        "only. The checks on `scope_paths` and `scope_coverage_complete` are a guard of "
        "`admit_development`, so an incomplete patch scope is refused before a fork is "
        "provisioned. No proposal is held without a write to the fork, so a stop clears the "
        "pending publication and nothing is published after it.",
        ("planning.agent_running", "provisioning.idle", "development.fix_needed",
         "development.agent_running", "development.fix_needed+paused"),
        edges=(("planning.agent_running", "admit_development"),
               ("provisioning.idle", "confirm_provisioning"),
               ("development.fix_needed", "request_patch"),
               ("development.agent_running", "pause")),
        highlighted=(("planning.agent_running", "admit_development"),
                     ("development.fix_needed", "request_patch")),
    ),
    Item(
        "When a planning run decides close",
        "When a plan may decide close, and who confirms it.",
        "Only when no riscv64 work is needed because PyPI already serves riscv64 for the latest "
        "release of every distribution of the request: a riscv64 wheel, or pure-Python wheels "
        "only. The reconciler reads PyPI itself before it takes `close_planning` and sets "
        "`terminal_reason` `already_released` when a riscv64 wheel is published, else "
        "`no_work`; a close that PyPI does not confirm takes `reject_plan` within "
        "`planning_attempts`. PyPI is the one registry a close is confirmed from: each PyPI "
        "project must name the upstream repository, and a project that is not released on "
        "PyPI is parked with the evidence that riscv64 is served. Every other reason to stop "
        "is a park, which a human can restart with `/upstreaming refresh`.",
        ("planning.agent_running", "planning.idle", "planning.waiting_human",
         "closed.idle"),
        edges=(("planning.agent_running", "close_planning"),
               ("planning.agent_running", "reject_plan"),
               ("planning.agent_running", "exhaust_planning_recovery")),
        highlighted=(("planning.agent_running", "close_planning"),
                     ("planning.agent_running", "reject_plan"),
                     ("planning.agent_running", "exhaust_planning_recovery")),
    ),
    Item(
        "Trusted CI rerun",
        "Which credential reruns the jobs of a fork's workflow run, how a rerun is recorded, "
        "and which row applies when it fails.",
        "No new workflow and nothing in the dispatcher. A failure the repair worker reports not "
        "caused by the patch (`no_change`) is rerun by the trusted publication job of the "
        "worker run, or of its publish-only retry, with a token for the one verified fork that "
        "carries only `actions: write`, bound to the exact head and attempt. Interrupted CI is "
        "rerun by the development PR workflow, launched by the controller, with the same kind "
        "of token. A rerun is an operation of kind `ci_rerun` whose receipt is the readback of "
        "the new run attempt; the attempt is a monotonic counter, so every retry reads it first "
        "and a rerun operation is never left `uncertain`. `request_ci_rerun` is taken only once "
        "the rerun is read back or nothing needed rerunning; a rerun that is refused or not "
        "read back, and a `no_change` Result without the receipt, is a failed publication "
        "(`schedule_publish_retry`, then `exhaust_publish_recovery`), and a moved head takes "
        "`replan_development`. A failed interrupted rerun fires no row: the next observation "
        "takes `rerun_interrupted_ci` again within `interrupted_reruns`, then "
        "`exhaust_interrupted_reruns`; a planned rerun of a paused contribution is closed "
        "the same way, since no receipt can arrive while paused. The wait after `request_ci_rerun` has no pending action "
        "and no reminder; CI ends it. `resume_ci` launches nothing: it restarts "
        "`interrupted_reruns`, and the first observation after it reruns. Interrupted means a "
        "required job that was cancelled, or a blocking job that failed with a runner-loss "
        "annotation, so a head whose CI the gate would read as failed is never nominated; "
        "`timed_out` "
        "and every other executed failure are real failures and win over a rerun. Only "
        "`development.waiting_ci` classifies lost runners. Each run is rerun with "
        "`rerun-failed-jobs`, at most 5 runs per request, and a rerun reserves no runner "
        "capacity. The repair attempt charged at allocation is not refunded.",
        ("development.agent_running", "development.waiting_ci", "development.waiting_human",
         "development.fix_needed"),
        edges=(("development.agent_running", "request_ci_rerun"),
               ("development.agent_running", "schedule_publish_retry"),
               ("development.agent_running", "exhaust_publish_recovery"),
               ("development.agent_running", "replan_development"),
               ("development.waiting_ci", "rerun_interrupted_ci"),
               ("development.waiting_ci", "exhaust_interrupted_reruns"),
               ("development.waiting_ci", "record_ci_failure"),
               ("development.waiting_human", "resume_ci")),
        highlighted=(("development.agent_running", "request_ci_rerun"),
                     ("development.waiting_ci", "rerun_interrupted_ci"),
                     ("development.waiting_ci", "exhaust_interrupted_reruns")),
    ),
    Item(
        "Pending feedback when CI passes",
        "What happens in `development.waiting_ci` when CI passes on the exact head while "
        "actionable feedback is still pending (after a push while Work was due for a review, or "
        "feedback the published Work did not carry).",
        "`return_for_feedback` leads to `development.fix_needed`: it records the passing CI "
        "evidence, keeps the revision and the pending feedback, and `request_patch` then "
        "requests a repair that carries the feedback. A head change never clears pending "
        "feedback; only a published Result does. The guards of `nominate_for_handoff` and "
        "`return_for_feedback` are exclusive, and the check verifies that every state that "
        "blocks a nomination on pending feedback has such a row.",
        ("development.fix_needed", "development.waiting_ci", "development.agent_running",
         "handoff.awaiting_submission"),
        edges=(("development.fix_needed", "record_head_change"),
               ("development.waiting_ci", "return_for_feedback"),
               ("development.waiting_ci", "nominate_for_handoff"),
               ("development.fix_needed", "request_patch"),
               ("development.agent_running", "record_publish_success")),
        highlighted=(("development.waiting_ci", "return_for_feedback"),),
    ),
)

OPEN_ITEMS: tuple[Item, ...] = ()
