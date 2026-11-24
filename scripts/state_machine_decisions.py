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
        "`operation_launch` waits for a human and does not park. A definite failure reported "
        "by the provisioning workflow waits for a human at once; a failure that is not "
        "definite is launched again within `operation_launch`; a refusal before any effect is "
        "neither.",
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
        "clean head, and then keeps the recorded CI evidence.",
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
        "CI again, and starts the wait again. The wait also starts again when CI is started "
        "again: on `rerun_interrupted_ci` there and on `resume_ci` from the human wait.",
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
    Item(
        "Interrupted CI on the clean head",
        "`handoff.waiting_ci` had the rows `rerun_interrupted_ci` and "
        "`exhaust_interrupted_reruns`, but CI on the clean head was read unclassified and "
        "nothing reran it: a cancelled job waited for `require_clean_head_ci`, and a job that "
        "lost its runner read as a failure and returned to development.",
        "CI on the clean head is read with lost runners classified. A required job that was "
        "cancelled or lost its runner, with no job failed on its own, takes "
        "`rerun_interrupted_ci` with a `ci_rerun` operation on the head the runs are on "
        "(`riscv64` at the clean head, or `riscv64-dev` at the approved candidate head whose "
        "tree it has), executed by the `ci-rerun` job of the handoff workflow with a token of "
        "the destination App for the one fork that carries only `actions: write`, minted after "
        "a check of the head of `main` and of the operation; `exhaust_interrupted_reruns` waits "
        "for a human with `ci_interrupted` once `interrupted_reruns` is spent. The executor's "
        "stale fence is that `riscv64` still resolves to the clean head. Interrupted CI takes a "
        "row before the bounded wait, which applies only when no row could be taken; "
        "`rerun_interrupted_ci` and `resume_ci` start that wait again (`clean_head_ci_since`), "
        "so one incident never needs two human resumes. Passing CI behind an open operation "
        "keeps the wait. An executed blocking failure still takes `return_for_repair`. The "
        "rerun counts stay keyed on the fork head, which `confirm_clean_head` starts again, so "
        "no record field changes.",
        ("handoff.waiting_ci", "handoff.waiting_human"),
        edges=(("handoff.waiting_ci", "rerun_interrupted_ci"),
               ("handoff.waiting_ci", "exhaust_interrupted_reruns"),
               ("handoff.waiting_human", "resume_ci")),
        highlighted=(("handoff.waiting_ci", "rerun_interrupted_ci"),
                     ("handoff.waiting_ci", "exhaust_interrupted_reruns"),
                     ("handoff.waiting_human", "resume_ci")),
    ),
    Item(
        "Reports of successful Work and the history every Work carries",
        "What a worker remembers of the work before it.",
        "The owner asked that \"the report generated by a successful worker must be fed to "
        "subsequent runs of the worker\", \"so that it remembers everything that was tried "
        "before, so it doesn't flip flop\", and decided: \"I want the report from a successful "
        "worker to be saved for future worker only. If a worker failed, you can just discard "
        "the report\". Every patch, repair, revision, submission rewrite and submission review "
        "Work returns a report (`schemas/worker_report.schema.json`, at most 32 KiB, a limit "
        "the worker is told); a Work whose payload carries a history and returns an otherwise "
        "acceptable Result without a valid report is rejected by the trusted gate "
        "(`reject_patch_proposal`, or Work without a usable Result at submission), while a "
        "blocked Result stays `worker_blocked` whatever its report. The report "
        "is stored as `worker_report` evidence only when one of six rows folds the Result: "
        "`record_publish_success`, `record_revision_publish_success`, "
        "`accept_submission_rewrite`, `accept_submission_review`, and the two accepted "
        "no-change rows `request_ci_rerun` and `require_reviewer_answer`. Such a row never "
        "drops the report of Work that carries a history: the reconciler checks the bytes "
        "the gate checked, and a report it cannot store pauses the contribution instead of "
        "folding the Result without it. A worker counts as "
        "successful when its own Result is accepted, whatever CI says afterwards: \"if the CI "
        "fails after a worker pass, the report from the worker should absolutely be used\", so "
        "the CI entry of the history names the report of the head it ran on. A Result whose "
        "push may have landed (an `uncertain` Result held for a publish-only retry, or a retry "
        "rejected as stale behind the operation of an earlier attempt) stores its report when "
        "it is folded, with the commit it proposed, since its artifact is never read again "
        "whatever ends the hold; a later success fold of the same run upgrades that record. "
        "Every other fold discards the report, and the rejection reasons of a failed worker are not fed forward "
        "either; a rewrite or review with a fail verdict is a rejection, while its verdict and "
        "reasons stay recorded as submission evidence. Planning writes no report: the verified "
        "plan is its report. When Work is allocated, the tick records its history "
        "(`work_history` evidence, derived from the Work id) in the same commit: the steering "
        "of the owner and one chronological timeline of every successful report, the CI runs "
        "and reviews that followed, and the submission verdicts, all generations included. It "
        "is capped at 64 KiB and condensed deterministically, never dropped uncounted: two "
        "reports are kept longest, the newest by the Work it describes and the report of the "
        "head CI ran on (so a CI rerun or a reviewer answer, which publish nothing, never "
        "displaces the report of the worker whose head failed), with the CI linked to it; "
        "the head a report left is the commit of the confirmed operation its row names, and a "
        "report whose push was not read back records the commit it proposed, which "
        "matches only a head that is that very commit; "
        "they take a reduced form, every section kept, before any of the owner's steering is "
        "folded, and everything else gives way first, oldest first. The payload names it by "
        "pinned commit and SHA-256, and a trusted pre-agent step fetches and verifies it; the "
        "dispatcher defers Work whose history is not committed yet, and dispatch recovery "
        "charges nothing for it for two hours, after which a history never recorded ends in "
        "the human wait of `exhaust_dispatch_recovery`, whose entry says so; the tick that "
        "records a late history launches the dispatcher. No record field changes.",
        ("development.agent_running", "development.waiting_ci",
         "handoff.agent_running", "handoff.review_pending", "handoff.reviewing",
         "handoff.building_upstream_branch"),
        edges=(("development.agent_running", "record_publish_success"),
               ("development.agent_running", "request_ci_rerun"),
               ("handoff.agent_running", "accept_submission_rewrite"),
               ("handoff.reviewing", "accept_submission_review")),
        highlighted=(("development.agent_running", "record_publish_success"),
                     ("handoff.agent_running", "accept_submission_rewrite"),
                     ("handoff.reviewing", "accept_submission_review")),
    ),
    Item(
        "Human steering",
        "How the owner steers the workers and the planner.",
        "The owner asked that \"every user's comment must also be included (tracking issue "
        "description/comments, pr reviews/comments, etc.), to allow the user to stir the work "
        "of the worker\" and that \"the tracking issue's description must of course be "
        "included in the planning prompt\". The tracking issue description (when edited) and "
        "comments, and the reviews, review comments and comments of the development PR and the "
        "upstream PR, are collected when Work is allocated and recorded as `human_steering` "
        "evidence with author, time and link, the newest pages of each list first; a list "
        "with more pages than were read is flagged, never silently cut; every Work, planning "
        "included, receives them in its history. Only accounts in the command authority "
        "steer. The REST API does not name the editor of a text. On the control repository and "
        "the fork, whose write access is part of the authority's trust, an edited description "
        "(when the issue author is in the command authority, as at intake), comment or review "
        "steers. On the upstream PR, where maintainers outside the authority can edit it, an "
        "edited authority comment or a new version of an authority review is shown as an "
        "unverified edit and never steers; the version recorded before the edit still does. "
        "On the upstream PR, accounts whose association is OWNER, MEMBER "
        "or COLLABORATOR are recorded as maintainer feedback, never steering; other accounts "
        "and bots are counted, not recorded. Steering is append-only (an edit is a new version "
        "naming the one it supersedes; a deleted comment stays recorded), never fires a row, "
        "never sets `review_scope_extension`, and can never widen allowed paths, operations or "
        "credentials. The frozen intake description stays the request identity and the plan's "
        "digest. All of this text is untrusted: redacted, bounded, never rendered on the "
        "tracking issue or the status page, and given to agents as a data file separate from "
        "their instructions. If GitHub cannot be read for it, the Work is allocated and its "
        "history waits: the dispatcher defers it and later ticks read again, and once the "
        "Work has waited an hour from its committed request the lists that still fail are "
        "read without, with an alert. A list GitHub answers as gone or not accessible (404, "
        "410 or 451) is named as unreadable in the history at once, which no retry would "
        "change.",
        ("development.fix_needed", "development.agent_running", "planning.idle",
         "planning.agent_running"),
        edges=(("development.fix_needed", "request_patch"),
               ("development.fix_needed", "request_repair"),
               ("planning.idle", "dispatch_planning")),
        highlighted=(("development.fix_needed", "request_repair"),
                     ("planning.idle", "dispatch_planning")),
    ),
    Item(
        "The final check of a handoff effect reads the head of `main`",
        "Whether a pause, a `read_only` policy, or another change of the state committed to "
        "`main` after a handoff job checked its checkout stops the write. Before, each job "
        "checked that its checkout was the head of `main` before it minted its token, and its "
        "final check before the write read that same checkout, so a change committed in the "
        "minutes between was not seen.",
        "Immediately before each write of `handoff.yml` (the push of `riscv64`, the CI dispatch "
        "on it, the post of the package, and the rerun request of the `ci-rerun` job) the head "
        "of `main` is read again (`handoff.unchanged_on_main`). The same commit as the checkout "
        "passes. Otherwise `main` must contain the checkout, and none of the files that "
        "authorize the operation may have changed between the two commits: the policy, the "
        "record, the operation (other than its launch count and `updated_at`, which a relaunch "
        "changes), and the plan, CI, rewrite, and review evidence the record names. An "
        "unrelated commit does not refuse, so other contributions do not spend the launches of "
        "a healthy run; a change to code or workflows does not refuse either, since pause and "
        "`read_only` are the operator's stops. An unreadable head, a `main` that no longer "
        "contains the checkout, or a comparison that is malformed or lists 300 files or more "
        "refuses (fail closed). A refusal is a `refused` receipt (no receipt for the rerun) "
        "that charges no attempt and is not folded: the operation is launched again within "
        "`operation_launch`, not while paused, and spent launches are a failed infrastructure "
        "attempt within `upstream_branch` or `submission_comment`, then a human wait. A change "
        "committed after that last read is not seen: the table already says that an effect "
        "that passed the publisher's final check before a pause may finish, and the reconciler "
        "reads every effect back. No row, budget, or record field changes.",
        ("handoff.building_upstream_branch", "handoff.waiting_ci", "handoff.preparing_submission",
         "handoff.waiting_upstream_pr"),
        edges=(("handoff.building_upstream_branch", "confirm_clean_head"),
               ("handoff.preparing_submission", "confirm_submission_package")),
        highlighted=(("handoff.building_upstream_branch", "confirm_clean_head"),
                     ("handoff.preparing_submission", "confirm_submission_package")),
    ),
    Item(
        "A handoff run in flight holds its attempt",
        "Whether a slow or queued but healthy `handoff.yml` run can have its attempt launched "
        "again or closed as spent. Before, an attempt was launched again 300 seconds after its "
        "last launch without a receipt and closed as failed once its last allowed launch was "
        "300 seconds old, while a run may take 25 minutes and wait behind another run of the "
        "concurrency group `porting-fork-writer`.",
        "The controller workflow reads the `handoff.yml` runs that are not completed before it "
        "collects the receipts, and matches them to operations by their run name `Handoff "
        "<operation id>`. A handoff attempt is neither launched again nor closed as spent while "
        "a run of it is queued or in progress, or while the run list could not be read in the "
        "tick, for at most `handoff.RUN_SECONDS` (3600 seconds) after the last launch; then it "
        "is launched again or closed as before, so a stuck queue still ends in "
        "`handoff.waiting_human`. A failed dispatch is still launched again after 300 seconds, "
        "since the run list then names no run of the attempt. No row, budget, or record field "
        "changes.",
        ("handoff.building_upstream_branch", "handoff.preparing_submission"),
        edges=(("handoff.building_upstream_branch", "retry_upstream_branch"),
               ("handoff.preparing_submission", "retry_submission_comment")),
        highlighted=(("handoff.building_upstream_branch", "retry_upstream_branch"),
                     ("handoff.preparing_submission", "retry_submission_comment")),
    ),
    Item(
        "The review of every development patch before it is published",
        "Whether a development patch is reviewed by a separate model before it is published, "
        "where the unpublished patch lives, what pins it, and what ends successive failed "
        "reviews.",
        "Every development patch is reviewed before it is published. Development Work carries "
        "the review contract; its worker's trusted publication job verifies the patch without "
        "a token and pushes nothing, and `hold_patch_for_review` holds it in "
        "`development.review_pending`, pinned in `held_patch` to the worker run, the "
        "publication artifact that carries the series (30 days) and the run that owns it, the Work, the digest of the "
        "series, and the commit it rebuilds to. A separate uncredentialed review Work "
        "(`development_review`, on its own `reviewer` workflow and model, read and diff "
        "only, no reproduction) reads exactly that patch with the worker's report, the plan, "
        "and the history. The verdict is derived from its findings: a blocker or a major "
        "finding fails it; minor findings and nits pass and are recorded. A pass takes "
        "`accept_patch_review` to `development.publishing`, the one state that publishes a "
        "held patch, through the publish-only workflow, which pushes only the held series "
        "rebuilt to the reviewed commit. A fail takes `reject_patch_review`: the review is the "
        "pending feedback of the next Work, which is given the rejected series "
        "(`context.previous_patch`, digest-verified by its trusted pre-agent step, skipped once "
        "the artifact expired), amends it, and charges "
        "`repair_attempts_cluster` again when CI failed (so `work_total` is 36). `dev_reviews` "
        "(1) returns one failed review in succession for repair; the second failed review in "
        "succession takes `publish_after_failed_reviews`: there is no human wait and no override command; the "
        "patch is published anyway and trusted code posts the latest review and why it failed "
        "on the development PR, durably: `record_publish_success` adds the comment to those "
        "due (`review_comments`, which a later publication or a new generation keeps), and a "
        "failed or lost post is launched again, bounded, then alerted in every tick and "
        "launched again daily until it is posted. Nothing about reviews is posted on the "
        "tracking issue. Every "
        "review, passed or failed, is `patch_review` evidence that the history of every later "
        "Work and review carries; the report of the patch is stored at the hold. A stop or an "
        "expired artifact discards the held patch, and fresh development Work follows. An "
        "exhausted recovery of the review Work keeps it in the human wait "
        "(`patch_review_unavailable`, stated in plain words), and the resume asks for the "
        "review of the same patch again (`resume_patch_review`) while its artifact is current. "
        "Work dispatched before the review keeps "
        "publishing in its own run (`record_publish_success` from `development.agent_running` "
        "with no review contract). New states `development.review_pending`, "
        "`development.reviewing`, and `development.publishing`, the budget `dev_reviews`, and "
        "the record field `held_patch`.",
        ("development.fix_needed", "development.agent_running", "development.review_pending",
         "development.reviewing", "development.publishing", "development.waiting_ci"),
        edges=(("development.fix_needed", "request_patch"),
               ("development.agent_running", "hold_patch_for_review"),
               ("development.review_pending", "request_patch_review"),
               ("development.review_pending", "expire_held_patch"),
               ("development.reviewing", "accept_patch_review"),
               ("development.reviewing", "reject_patch_review"),
               ("development.reviewing", "publish_after_failed_reviews"),
               ("development.publishing", "record_publish_success"),
               ("development.agent_running", "record_publish_success")),
        highlighted=(("development.agent_running", "hold_patch_for_review"),
                     ("development.reviewing", "reject_patch_review"),
                     ("development.reviewing", "publish_after_failed_reviews")),
    ),
    Item(
        "The comment of a patch published anyway, whatever stops its record",
        "When the comment of a patch published after successive failed reviews becomes due "
        "if the record stops before the Result of its push is folded, and whether a pause "
        "or a missing development PR holds its post.",
        "The comment is due whenever a `published` Result is the publication of a patch "
        "published anyway: the `patch_review` evidence with the disposition `publish_anyway` "
        "whose pin names the Result's worker run, Work, and commit makes it due in the write "
        "that records the Result, once per review, on time (`record_publish_success`) or "
        "late (`record_late_effect`, in any state: after a pause, a processing error, a stop, "
        "or the exhaustion of `publish_recovery`), since the patch is on the fork either way. "
        "Posting the comment is not development Work. A pause holds it on a record that is "
        "not terminal, and the resume launches it; a terminal record is never resumed, so its "
        "pause holds nothing and the comment is posted. A post waits, spending no attempt, "
        "until a development PR is recorded. A held comment is alerted on in every tick once "
        "held for 24 hours, and at once on a terminal record. The attempts end at 999, the "
        "last the workflow input admits; the comment then stays due and alerted on. No row, "
        "state, or budget changes.",
        ("development.publishing", "development.waiting_ci", "development.waiting_human",
         "development.fix_needed", "closed.idle"),
        edges=(("development.publishing", "record_publish_success"),
               ("development.publishing", "exhaust_publish_recovery")),
        highlighted=(("development.publishing", "record_publish_success"),),
    ),
    Item(
        "The comment of a patch published anyway, from the branch",
        "When the comment of a patch published after successive failed reviews becomes due "
        "if no `published` Result of its publication ever reaches the reconciler, and which "
        "head Work is requested on after such a publication.",
        "Trusted branch evidence makes the comment due too, in the same write, in any state, "
        "once per review: a branch operation of the contribution that becomes `confirmed` "
        "(by discovery, which reads the branch in a human wait and while paused too, by the "
        "read-back of a publish-only retry, in a Result folded or recorded late, or by a "
        "human with `resolve-operation`) at the commit a `publish_anyway` review pinned, a "
        "fork head at that commit that `record_head_change` records, or the head this tick's "
        "observation read at that commit when a command moves the record out of a state "
        "whose observation does not record the head (a resume, a park, or a replan from a "
        "human wait), since that head is otherwise never recorded. So an uncertain push, or "
        "a retry whose run died before its upload, followed by a pause, a processing error, "
        "or the exhaustion of `publish_recovery`, still leaves the comment due once the "
        "branch shows the commit, whether the record is resumed, parked or replanned. In a "
        "human wait with no stored operation of that push (every retry died before its "
        "upload, or its `uncertain` Result arrived after the wait began) the comment becomes "
        "due only when a command moves the record: an observed head that no write records "
        "would make it due again after every post. The comment names the commit the branch "
        "holds, which discovery that confirmed the operation by its tree and parents may "
        "read under another SHA than the pinned one. "
        "An `uncertain` Result alone makes nothing due, since the comment says the patch was "
        "published at that commit. A resume from a human wait requests no development Work "
        "while this tick's observation read the branch at another head than the recorded "
        "one: the next observation records it by `record_head_change` in "
        "`development.fix_needed`, and Work is requested on it. A comment that becomes due "
        "beyond the 32 the record holds drops the oldest exhausted comment, else the oldest "
        "other one, each with an alert. No row, state, or budget changes.",
        ("development.publishing", "development.waiting_human", "development.fix_needed",
         "development.waiting_ci", "parked.idle"),
        edges=(("development.publishing", "exhaust_publish_recovery"),
               ("development.waiting_human", "resume_development"),
               ("development.waiting_human", "park_contribution"),
               ("development.fix_needed", "record_head_change")),
        highlighted=(("development.fix_needed", "record_head_change"),),
    ),
    Item(
        "The Work identity has no pre-identity reading",
        "Whether a Work payload that names neither `generation` nor `dedupe_key`, or "
        "requested Work recorded without a generation, is still read as generation 0.",
        "No. A Work payload names its identity only with an integer `generation` and a string "
        "`dedupe_key`; one that lacks either, also one that names neither, names no Work "
        "identity in any generation. Its Result is late and takes `record_late_effect`, and "
        "the publisher refuses its write as stale. Requested Work whose recorded generation "
        "is absent or not the record's is never dispatched and matches no Result; the field "
        "migration carries Work recorded without a generation as generation 0. The Work such "
        "a Result leaves requested is terminal without a usable Result and is answered by "
        "`allocate_work_retry` within `work_recovery` and `work_total`, then "
        "`exhaust_work_recovery` to the human wait of the phase. This completes decision 2 "
        "(the generation fence) and decision 19 (late Results): a Result that differs in the "
        "generation, the revision, or the Work id is never folded and never published.",
        ("development.agent_running", "development.waiting_human"),
        edges=(("development.agent_running", "record_late_effect"),
               ("development.agent_running", "allocate_work_retry"),
               ("development.agent_running", "exhaust_work_recovery")),
        highlighted=(("development.agent_running", "record_late_effect"),
                     ("development.agent_running", "allocate_work_retry")),
    ),
    Item(
        "The review fences are effects of the review row",
        "Whether the processed review and comment ids of a recorded review are written beside "
        "`record_actionable_review`, as a separate write of the collector, while "
        "`record_explanation` lists them among its effects.",
        "The row sets them. Every `record_actionable_review` row (at the submission gate, in "
        "`development.waiting_ci`, `development.fix_needed`, and `upstream_review.waiting_ci`) "
        "sets `processed_review_ids`, `processed_review_comment_ids`, and "
        "`processed_pr_conversation_comment_ids` with the pending feedback, as every "
        "`record_explanation` row does, and the collector writes nothing beside it: the record "
        "is written once, by the engine, and a caller that does not give the fences is refused. "
        "A row the engine refuses leaves the comments unfenced, so they are read again by the "
        "next tick. The check reports a review or explanation row that does not set the three "
        "fences. No state, verb, budget, or record field changes.",
        ("development.waiting_ci", "development.fix_needed"),
        edges=(("development.waiting_ci", "record_actionable_review"),
               ("development.fix_needed", "record_actionable_review")),
        highlighted=(("development.waiting_ci", "record_actionable_review"),
                     ("development.fix_needed", "record_actionable_review")),
    ),
    Item(
        "The scope guard of a readmission",
        "Whether a refreshed develop plan whose patch scope is incomplete (coverage the scope "
        "omits, no admission class, or no scope paths) returns to the recorded fork. "
        "`readmit_development` had no scope guard, so such a plan was readmitted with "
        "`scope_coverage_complete` false, and the next repair Work could change paths outside "
        "the admitted scope.",
        "No. `readmit_development` has the guard of `admit_development`: \"the admitted scope "
        "names an admission class and 1 to 32 safe `scope_paths`, and a plan's own scope "
        "matches every path of its `path_coverage`\". A refreshed plan whose "
        "scope is incomplete takes `reject_plan`, which charges `planning_attempts` and keeps "
        "the refresh, the fork, and any override, so a new planning run is due; once the "
        "attempts are spent `exhaust_planning_recovery` waits for a human, with reminders. "
        "The next complete plan returns to the recorded fork. An admission override keeps its "
        "reading on both rows. The check reports either admission without the guard. This "
        "completes O24 and decision 12: no patch Work is requested without a checked scope, "
        "on a new fork or on the recorded one.",
        ("planning.agent_running", "development.fix_needed", "planning.idle"),
        edges=(("planning.agent_running", "readmit_development"),
               ("planning.agent_running", "reject_plan")),
        highlighted=(("planning.agent_running", "readmit_development"),
                     ("planning.agent_running", "reject_plan")),
    ),
    Item(
        "An identity change while paused",
        "What an upstream identity change observed while the contribution is already paused "
        "does, given that `pause_on_identity_change` is guarded by `not paused`.",
        "Nothing is set: the pause keeps its reason and who set it, a human or the "
        "controller. No Work is selected for the contribution while the identity differs, "
        "also in the tick that consumes its resume, so a resume never lets Work run against "
        "another repository; the first tick after the resume takes `pause_on_identity_change`, "
        "with `paused_by` `controller` and `paused_reason` `upstream_identity_changed`. No "
        "row, state, or record field changes.",
        ("development.waiting_ci", "development.waiting_ci+paused"),
        edges=(("development.waiting_ci", "pause_on_identity_change"),
               ("development.waiting_ci+paused", "resume_pause")),
        highlighted=(("development.waiting_ci", "pause_on_identity_change"),),
    ),
    Item(
        "Reminders across a pause",
        "How the reminder interval is counted, and what a pause does to it.",
        "The interval is counted in whole business days from the exact time of the request "
        "or of the last reminder; a day counts when it ends on a weekday in UTC, whatever "
        "offset the times carry, and every pending action of a state of kind `gate`, "
        "`human_wait`, or `locked` gets reminders. A pause posts nothing but does not restart "
        "the interval: a reminder that came due during the pause is posted in the tick that "
        "ends it, after the resume, and names the new revision; the next one is due a full "
        "interval after it. A stale resume clears the pending action, so no reminder follows "
        "it. No row, state, or record field changes.",
        ("handoff.awaiting_submission",),
        edges=(("handoff.awaiting_submission", "record_reminder"),),
        highlighted=(("handoff.awaiting_submission", "record_reminder"),),
    ),
    Item(
        "A resume with nothing to resume",
        "What `/upstreaming resume` answers on a contribution that is not paused and not in "
        "a human wait.",
        "`not paused` in every state that is not a human wait, `parked.idle`, or an end "
        "state, the submission gate and the locked upstream-PR wait among them. In "
        "`parked.idle` the reply gives the refresh form that starts a new generation. In "
        "`closed.idle` and `released.idle` the reply states that the contribution has ended "
        "and no command resumes it, and that nothing was changed, instead of calling an ended "
        "contribution active. No row, state, or record field changes.",
        ("parked.idle", "closed.idle"),
        edges=(("parked.idle", "abandon"),),
        highlighted=(("parked.idle", "abandon"),),
    ),
    Item(
        "A contribution that ends while paused",
        "Whether a contribution that is paused stays paused when it ends.",
        "Every row that ends a contribution (`abandon`, `withdraw_request`, `close_planning`, "
        "`verify_release`) clears `paused`, `paused_at`, `paused_reason`, and `paused_by`, so "
        "an ended contribution is never shown as paused and no resume is offered for it; "
        "`lifecycle check` verifies this. `abandon` is accepted while paused and ends the "
        "pause; `terminal_reason` is `abandoned` and the reason the human gave is kept in the "
        "event only. A record that is paused and ended all the same, which no row produces, "
        "has the flag cleared by a resume (`resume_pause`) and nothing else.",
        ("parked.idle+paused", "closed.idle"),
        edges=(("parked.idle+paused", "abandon"),),
        highlighted=(("parked.idle+paused", "abandon"),),
    ),
    Item(
        "Commands of a contribution a tick leaves out",
        "What happens to the commands of a contribution that a tick does not process.",
        "Staging is all or nothing per contribution. When a collector failure or an "
        "unexpected error leaves a contribution out of a tick, every command on its tracking "
        "issue, `resume` included, stays unconsumed and without a reply; the tick report lists "
        "each as `deferred` (\"the contribution is not processed in this tick\"). The first tick "
        "that processes the contribution applies or rejects them, so nothing is lost and "
        "nothing is applied twice. An error in a contribution that is already stopped, or that "
        "the same tick would have created, is reported in the tick report "
        "(`controller.alerts`, `processing_errors`) and the workflow annotation only, with no "
        "event and no post on the tracking issue: `pause_on_processing_error` requires that "
        "the contribution is not paused and not ended, and a request without a committed "
        "record has nothing to pause. No row, state, or record field changes.",
        ("development.waiting_ci", "development.waiting_ci+paused"),
        edges=(("development.waiting_ci", "pause_on_processing_error"),
               ("development.waiting_ci+paused", "resume_pause")),
        highlighted=(("development.waiting_ci", "pause_on_processing_error"),),
    ),
    Item(
        "Which CI job a repair is charged to",
        "Which job names the cluster that `repair_attempts_cluster` counts.",
        "The failing CI job is the first executed blocking job of the evidence at "
        "`last_ci_evidence_id` (for the head, or the clean head built from it) that did not "
        "succeed, leaving out the publication jobs that run without credentials; else the "
        "first job a `limitations` entry of that evidence names as failing the runner-label "
        "check; else none, and the Work charges no cluster. The job name is the key of "
        "`budget.clusters`: a charge increments only that key, and the counts of other jobs "
        "are kept across heads until `refresh_on_exhaustion` or a new generation starts them "
        "again. A job named by a limitation is used as a key and in event details only. No "
        "row, state, or record field changes.",
        ("development.fix_needed", "development.agent_running"),
        edges=(("development.fix_needed", "request_repair"),),
        highlighted=(("development.fix_needed", "request_repair"),),
    ),
    Item(
        "A fork head that moves while Work runs",
        "Whether a changed fork head is recorded while Work or a held Result is pending.",
        "No head row is added to a state that requests Work or holds a Result for "
        "publication (`development.agent_running`, `development.review_pending`, "
        "`development.reviewing`, `development.publishing`): recording the head there would "
        "race the Result and need a second writer of `fork.head_sha`. The moved head heals "
        "without a human. The publisher's final check rejects a Result built on the old head "
        "as stale, which takes `replan_development` to `development.fix_needed` and charges "
        "nothing; the observation that follows, in the same tick, takes `record_head_change` "
        "there before any Work is requested. A publish-only retry reads the branch first and never pushes over a push "
        "that landed; a confirmed earlier push takes `record_publish_success`, and a later "
        "head is recorded by `record_head_change`. Branch operation discovery runs in every "
        "state. No row, state, or record field changes.",
        ("development.agent_running", "development.fix_needed", "development.waiting_ci"),
        edges=(("development.agent_running", "replan_development"),
               ("development.fix_needed", "record_head_change")),
        highlighted=(("development.agent_running", "replan_development"),
                     ("development.fix_needed", "record_head_change")),
    ),
    Item(
        "The counter a failed Work recovery names",
        "Which counter `exhaust_work_recovery` names when both of its budgets are spent.",
        "`pending_human_action.counter` names `work_total` when that budget is exhausted, also "
        "when `work_recovery` is exhausted too, and `work_recovery` otherwise. A resume starts "
        "exactly the counter the wait names. `work_recovery` is counted in "
        "`active_work.attempt`, which the exhaustion clears with the Work, so the next Work "
        "starts at attempt 1 anyway; naming `work_total` is the only choice with which one "
        "resume makes Work possible again. No row, state, or record field changes.",
        ("development.agent_running", "development.waiting_human"),
        edges=(("development.agent_running", "exhaust_work_recovery"),),
        highlighted=(("development.agent_running", "exhaust_work_recovery"),),
    ),
    Item(
        "The counter an exhausted upstream revision names",
        "Which counter `exhaust_repair_attempts` names in upstream review when several revision "
        "budgets are spent at once.",
        "It names the first spent counter in the order `no_progress`, `work_total`, "
        "`repair_attempts_cluster` (the order of its guard), and its event lists every spent "
        "counter. The resume (`resume_upstream_revision`) starts only the named counter again, "
        "so with several spent the next tick waits again on the next one: one bounded human "
        "decision per budget.",
        ("upstream_review.fix_needed", "upstream_review.waiting_human"),
        edges=(("upstream_review.fix_needed", "exhaust_repair_attempts"),
               ("upstream_review.waiting_human", "resume_upstream_revision")),
        highlighted=(("upstream_review.fix_needed", "exhaust_repair_attempts"),),
    ),
    Item(
        "A human wait always names its pending action",
        "Whether a record in a state of kind `human_wait` may lack its pending action.",
        "It may not. Every row into a human wait sets `pending_human_action`, no row clears it "
        "there without setting it (`lifecycle check`), and `state validate` refuses a record "
        "in a human wait whose pending action is missing or has no kind. A wait for a human "
        "(`engine.waits_for_human`) is therefore any state of kind `gate`, `locked`, or "
        "`human_wait`: only the rows the table lists for it leave it, observation reads "
        "nothing where it has no observation row, and a Result that arrives there takes "
        "`record_late_effect`.",
        ("development.fix_needed", "development.waiting_human"),
        edges=(("development.fix_needed", "exhaust_work_total"),
               ("development.waiting_human", "resume_development")),
        highlighted=(("development.fix_needed", "exhaust_work_total"),),
    ),
    Item(
        "The operation rows a receipt and a push take",
        "How a planned operation run by a trusted workflow, a branch operation stored as "
        "`applying`, and a publisher final check that fails go through the operation table.",
        "A planned operation that a trusted workflow runs stays `planned` across its launches. "
        "Its verified receipt, with the exact readback, takes `begin_effect`, "
        "`record_response`, and `confirm_by_discovery` together: the receipt establishes the "
        "facts of `begin_effect`, because the workflow's own final check read the record not "
        "paused and the operation current, and capacity was admitted when the controller "
        "launched it. A push establishes \"capacity available\" without a runner reservation. "
        "A branch operation that the store holds as `applying` lost the response of its push "
        "(the publisher hands an operation over only after recording the response): the "
        "observation takes `record_response` (\"its response was lost\") and discovers it, "
        "and never pushes again. When the publisher's final re-read of the head before a push "
        "fails or finds it moved, no intent is recorded and the outcome carries no operation, "
        "because `planned` has no row to `failed` other than a resolution.",
        operations=True,
    ),
    Item(
        "The record's reference to its operation",
        "Which write clears `active_operation_id`, given that no operation row lists it.",
        "The reference is not lifecycle state: it is kept by the writes beside a row "
        "`operation_planned` and `operation_released`, and released in the same write that "
        "closes the operation (a receipt, a failure receipt, `resolve-operation`, a closed "
        "development PR or `ci_rerun` operation, or `exhaust_development_pr_launches`). "
        "`record_merge` is the one row that clears it by an effect, because the merge ends fork "
        "work even while an operation is open; that operation still fences through the store "
        "and is reported as orphaned.",
        operations=True,
    ),
    Item(
        "Launches of a planned operation and their exhaustion",
        "Where `operation_launch` is counted, how often a launch is repeated, and when its "
        "exhaustion is taken.",
        "`operation_launch` is counted in `external_ids.launch_attempts` of the planned "
        "operation and started again by the resume that names it. A launch is repeated only "
        "300 seconds (`LAUNCH_INTERVAL_SECONDS`) after the last one without a verified "
        "receipt; each provisioning launch needs available capacity, and a development PR "
        "launch (`launch_development_pr`) has no capacity guard. `require_provisioning_"
        "intervention` for exhausted launches is found once the last allowed launch is that "
        "old without a receipt, and its guard holds only while the contribution is not "
        "paused, so the first tick after the pause ends takes it (pending action "
        "`provisioning_launch_exhausted`, counter `operation_launch`). A failed provisioning "
        "operation (resolved `failed`, or a verified receipt that reports a definite failure) "
        "takes the row at once, also while paused, with `provisioning_operation_failed` and no "
        "counter.",
        ("provisioning.idle", "provisioning.waiting_human"),
        edges=(("provisioning.idle", "start_operation"),
               ("provisioning.idle", "require_provisioning_intervention")),
        highlighted=(("provisioning.idle", "require_provisioning_intervention"),),
    ),
    Item(
        "The resume of a provisioning wait",
        "What establishes \"the prerequisite is corrected\", and what the resume of exhausted "
        "launches relaunches.",
        "The authorized human's `/upstreaming resume` is the statement that the prerequisite "
        "is corrected. The controller establishes it for every pending action the pipeline "
        "writes in `provisioning.waiting_human` (`provisioning_launch_exhausted` with "
        "`operation_launch`, `provisioning_operation_failed`, and `base_branch_unavailable` "
        "and `upstream_unavailable` with `source_reads` or no counter); any other pending "
        "action establishes nothing, so the resume is refused and `park` remains. After the "
        "resume of exhausted launches the same open planned operation is launched again from "
        "a count of zero at the new revision: the provisioning workflow accepts an operation "
        "of an earlier revision when the record names it as active, and its receipt carries "
        "the record's revision. A receipt of a launch before the resume carries the earlier "
        "revision and is not folded; the receipt of the relaunch, which adopts the fork, is.",
        ("provisioning.waiting_human", "provisioning.idle"),
        edges=(("provisioning.waiting_human", "resume_provisioning"),
               ("provisioning.idle", "start_operation")),
        highlighted=(("provisioning.waiting_human", "resume_provisioning"),),
    ),
    Item(
        "A provisioning receipt that reports a failure or a refusal",
        "Which provisioning receipts other than `provisioned` are folded, which of several is "
        "folded, and what a receipt or a confirmed resolution does in the provisioning wait.",
        "A verified receipt `failed` is a definite failure. It is folded only for a `planned` "
        "or `applying` `fork_provision` operation that the record names at its revision; for "
        "any other operation it is reported `failure not folded`. From `provisioning.idle` it "
        "takes `require_provisioning_intervention` (`provisioning_operation_failed`, no "
        "counter); in `provisioning.waiting_human` it closes and releases the operation and "
        "keeps the wait, with no event. `refused` and any other status are reported and never "
        "folded. Of several foldable receipts for one operation only the newest run's is "
        "folded; older ones are reported `superseded`, and a newer refusal does not hide an "
        "older foldable receipt. A `provisioned` receipt in the wait closes the operation as "
        "confirmed and keeps the wait. A human `resolve-operation disposition=confirmed` fires "
        "no row, because `confirm_provisioning` needs the receipt and its readback. This "
        "completes decision 15.",
        ("provisioning.idle", "provisioning.waiting_human", "development.fix_needed"),
        edges=(("provisioning.idle", "require_provisioning_intervention"),
               ("provisioning.idle", "confirm_provisioning"),
               ("provisioning.waiting_human", "resume_provisioning")),
        highlighted=(("provisioning.idle", "require_provisioning_intervention"),),
    ),
    Item(
        "The identity of a retried publication and of a re-planned CI bootstrap",
        "Which attempt makes the operation of a publish-only retry its own, and how a CI "
        "bootstrap `dev_pr_update` planned again is told apart from the earlier ones.",
        "An operation id is never reused once closed. A publish-only retry adds "
        "`publish-attempt:<n>` from the second attempt on, n being the attempt the controller "
        "dispatched the retry with (`pending_publication.attempts + 1`, passed as "
        "`UPSTREAMING_PUBLISH_ATTEMPT` and read by `fork_publish.dispatched_attempt`, never "
        "below 1). The first attempt keeps the id of the Result alone. The `ci_rerun` "
        "operation of a publication job always names the attempt. A CI bootstrap "
        "`dev_pr_update` planned again after a failed or never-launched one adds "
        "`replan:<n>`, n being the number of earlier bootstrap operations for the same head "
        "in the store, whatever their revision. A re-plan needs a closed predecessor, which "
        "only launch exhaustion with a human resume, a pause, or a human resolution produces, "
        "so the number is bounded by human commands.",
        operations=True,
    ),
    Item(
        "An operation the tick stages that the store would refuse",
        "What happens when a contribution's processing stages an operation that the state "
        "publisher's check would refuse against the committed one.",
        "`reconcile.tick` checks every staged operation against the committed one "
        "(`recovery.transition_problem`) before the publisher's final guard. A violation is an "
        "unexpected error of that contribution in step `operation_check`: its staging is "
        "discarded, it takes `pause_on_processing_error`, and the commit goes ahead for the "
        "others. Two such contributions in one tick stop the tick, like any two unexpected "
        "errors.",
        ("development.waiting_ci", "development.waiting_ci+paused"),
        edges=(("development.waiting_ci", "pause_on_processing_error"),
               ("development.waiting_ci+paused", "resume_pause")),
        highlighted=(("development.waiting_ci", "pause_on_processing_error"),),
    ),
    Item(
        "CI evidence the record names but the store does not hold",
        "What a record whose `last_ci_evidence_id` names evidence missing from the store does.",
        "Evidence is never removed from the store. A record in a state where Work is "
        "allocated or its exhaustion decided, whose `last_ci_evidence_id` names missing "
        "evidence, is never deferred silently and never read as \"no failing job\": "
        "`controller.MissingEvidence` in step `work_selection` takes "
        "`pause_on_processing_error`, with the missing id in `paused_reason`. States that "
        "allocate no Work (gates and waits, such as `handoff.awaiting_submission`) do not read "
        "it.",
        ("development.fix_needed", "development.fix_needed+paused"),
        edges=(("development.fix_needed", "pause_on_processing_error"),
               ("development.fix_needed+paused", "resume_pause")),
        highlighted=(("development.fix_needed", "pause_on_processing_error"),),
    ),
    Item(
        "What a park decided by the plan records",
        "What `park_planning` records besides the state, and what becomes of a recorded "
        "refresh.",
        "`park_planning` ends the generation without a revision bump, like every row into "
        "`parked.idle`. `previous_phase` is `planning`. `parked_reason` is the plan's "
        "rationale, bounded to 2000 characters, or \"the plan decided park\" when empty. It "
        "is worker text, stored as data and never rendered on the tracking issue or status "
        "page; the event carries only the decision and the evidence id. A recorded refresh, "
        "and the fork it began with, are kept: nothing reads them in `parked.idle`, and "
        "`restart_generation` resets them with the generation. This completes decisions 13 "
        "and 22.",
        ("planning.agent_running", "parked.idle", "queued.idle"),
        edges=(("planning.agent_running", "park_planning"),
               ("parked.idle", "restart_generation")),
        highlighted=(("planning.agent_running", "park_planning"),),
    ),
    Item(
        "An admission override that names only a class or only paths",
        "What a recorded admission override that names a class without paths, or paths "
        "without a class, does against the plan's decision.",
        "An override applies against a decision park only when it names a class and 1 to 32 "
        "safe paths. `/upstreaming refresh` refuses any other form (`controller."
        "PARTIAL_OVERRIDE`), so only a record written before that refusal can hold a partial "
        "one. Such a record is neither admitted nor parked (the guard of `park_planning` is "
        "\"no admission_override recorded\"): the Result takes `reject_plan` within "
        "`planning_attempts`, with the override and the refresh kept, then "
        "`exhaust_planning_recovery` waits for a human. The exit from that wait is "
        "`/upstreaming park` followed by `/upstreaming refresh class=... paths=...`. With a "
        "decision develop the plan's class and paths are admitted and the override is cleared "
        "(event detail `admission_override_cleared`). With a decision close the override is "
        "not applied. This completes O11.",
        ("planning.agent_running", "planning.idle", "planning.waiting_human",
         "provisioning.idle", "parked.idle"),
        edges=(("planning.agent_running", "reject_plan"),
               ("planning.agent_running", "exhaust_planning_recovery"),
               ("planning.agent_running", "admit_development"),
               ("planning.agent_running", "park_planning")),
        highlighted=(("planning.agent_running", "reject_plan"),),
    ),
    Item(
        "The scope an admission records",
        "How the scope guard of the admissions is established, and which row writes "
        "`scope_coverage_complete`, which the reconciler wrote beside the row.",
        "The guard \"the admitted scope names an admission class and 1 to 32 safe "
        "`scope_paths`, and a plan's own scope matches every path of its `path_coverage`\" "
        "is established by `plan_gate.scope_problem`; a plan without `path_coverage` is "
        "incomplete. Both admissions set `scope_coverage_complete` as an effect of the row, "
        "and `lifecycle check` reports an admission that does not. For an override the class "
        "and the paths are required and the coverage is recorded but does not refuse them. A "
        "record with `scope_coverage_complete` false gives its repair and revision Work "
        "`legacy_scope_extension`, under which the trusted publisher admits only exact, safe, "
        "additional paths.",
        ("planning.agent_running", "provisioning.idle", "development.fix_needed",
         "planning.idle"),
        edges=(("planning.agent_running", "admit_development"),
               ("planning.agent_running", "readmit_development"),
               ("planning.agent_running", "reject_plan")),
        highlighted=(("planning.agent_running", "admit_development"),
                     ("planning.agent_running", "readmit_development")),
    ),
    Item(
        "A worker outcome `proposal`",
        "What a publication artifact whose status is `proposal` does.",
        "No trusted publication job writes the status `proposal` any more. An artifact that "
        "still carries it (a run on older trusted code within the collection window) is "
        "folded as rejected in trusted validation: `reject_patch_proposal` in "
        "`development.agent_running` and `upstream_review.agent_running`, charging "
        "`no_progress`, with the pending feedback kept and nothing held or published; in "
        "`development.publishing` it is folded as a rejected publication. It is bounded by "
        "`no_progress`.",
        ("development.agent_running", "development.fix_needed"),
        edges=(("development.agent_running", "reject_patch_proposal"),),
        highlighted=(("development.agent_running", "reject_patch_proposal"),),
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
        "`answer_reviewer`, or `upstream_head_changed`, and clears the pending feedback: "
        "every resume that requests Work again keeps the pending feedback; "
        "`resume_upstream_review` is the one resume that clears it, because a human answered "
        "it (the other two pending actions are entered from `upstream_review.waiting_ci`, "
        "which holds none). `resume_upstream_revision` leads to `upstream_review.fix_needed` "
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
        "`replan_development`, `record_head_change`, and both refresh verbs keep the pending "
        "feedback, and every resume that requests Work again keeps the pending feedback; "
        "`resume_upstream_review` is the one resume that clears it, because a human answered "
        "it. A fresh Work graph and a publish retry carry the same ids. Work "
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
        "rerun by the development PR workflow in `development.waiting_ci`, and in "
        "`handoff.waiting_ci` by the handoff workflow, launched by the controller, with the "
        "same kind of token; each workflow executes only the reruns of its own phase. A rerun is an operation of kind `ci_rerun` whose receipt is the readback of "
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
        "`development.waiting_ci` and `handoff.waiting_ci` classify lost runners. Each run is rerun with "
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
    Item(
        "An uncertain publication in development",
        "What a Result whose push was not read back (`uncertain`) takes in "
        "`development.agent_running`.",
        "In development, an uncertain Result is a failed publication, exactly as in upstream "
        "review: it takes `schedule_publish_retry` (or `exhaust_publish_recovery`), which clears "
        "the Work and holds the Result, so no Work recovery takes its completed graph. "
        "`record_publish_success` is taken only when the publication is confirmed: by a "
        "`published` Result (its own operation confirmed), by the publish-only retry reading "
        "back the head the lost push left (it confirms the open operation of the earlier "
        "attempt when its Result is folded), or by the operation of an earlier attempt of the "
        "held Result being confirmed in the operation store, including a retry rejected as "
        "stale behind it. The row then names the confirmed operation, which the development PR "
        "request requires. The retry is bounded by `publish_recovery`, then "
        "`exhaust_publish_recovery` leads to `development.waiting_human` with the pending action "
        "`publication_infrastructure_failure` and the reminders of every human wait. No new "
        "state, verb, budget, flag, or record field.",
        ("development.agent_running", "development.waiting_ci", "development.waiting_human",
         "development.fix_needed"),
        edges=(("development.agent_running", "schedule_publish_retry"),
               ("development.agent_running", "exhaust_publish_recovery"),
               ("development.agent_running", "record_publish_success"),
               ("development.agent_running", "replan_development")),
        highlighted=(("development.agent_running", "record_publish_success"),),
    ),
    Item(
        "A CI failure on the clean head",
        "What a blocking CI failure on the clean head of `riscv64` records when it returns the "
        "contribution to development.",
        "Its evidence. `return_for_repair` from `handoff.waiting_ci` sets `last_ci_evidence_id` "
        "to the failing evidence of the clean head. When the clean head is another commit than "
        "the development head, that evidence names the candidate head it was built from "
        "(`ci.candidate_head_sha`), so the failing job is the job of the head and "
        "`request_repair` charges its cluster. A moved `riscv64` has no CI verdict and keeps "
        "the recorded evidence of the candidate head. `return_for_repair` from "
        "`handoff.building_upstream_branch` reads no CI and sets none.",
        ("handoff.waiting_ci", "development.fix_needed", "development.agent_running"),
        edges=(("handoff.waiting_ci", "return_for_repair"),
               ("development.fix_needed", "request_repair")),
        highlighted=(("handoff.waiting_ci", "return_for_repair"),
                     ("development.fix_needed", "request_repair")),
    ),
    Item(
        "A moved `riscv64` before the package is posted",
        "What a package attempt in `handoff.preparing_submission` takes when its final check "
        "finds that `riscv64` no longer resolves to the clean head (moved, deleted, or "
        "renamed). The state had no row for it, so the attempt failed as an infrastructure "
        "failure and ended in a human wait whose resume led to the same step.",
        "`return_for_repair` to `development.fix_needed`, guarded by `riscv64` of the fork not "
        "resolving to the clean head, as from `handoff.building_upstream_branch` and "
        "`handoff.waiting_ci`. Nothing is posted and nothing is written to `riscv64`. The row "
        "bumps the revision, clears the handoff fields, and keeps the recorded CI evidence, "
        "since a moved branch has no CI verdict; it charges no `submission_comment` budget. "
        "The workflow reports the failure class `moved`; the reconciler reads `riscv64` again "
        "before it fires the row, and a branch read at the clean head, or not read, is folded "
        "as an infrastructure failure (`retry_submission_comment`, then "
        "`exhaust_comment_retries`). A missing branch counts as moved only when the fork is "
        "read under its recorded repository id.",
        ("handoff.preparing_submission", "development.fix_needed", "handoff.waiting_human"),
        edges=(("handoff.preparing_submission", "return_for_repair"),
               ("handoff.preparing_submission", "retry_submission_comment"),
               ("handoff.preparing_submission", "exhaust_comment_retries")),
        highlighted=(("handoff.preparing_submission", "return_for_repair"),),
    ),
    Item(
        "Series requirements of the plan are met before nomination",
        "Whether a contribution-policy item of the plan that needs a change to the series (for "
        "example a changelog entry) blocks the nomination for handoff, or is requested as "
        "development Work. Before, nothing checked it until the submission rewrite and its "
        "review, which sent the series back after a human had asked for `submit-upstream`.",
        "It is requested as development Work. A plan of schema version 4 records its "
        "measurable items as `contribution_guidelines.series_requirements`, each a path the "
        "non-debug commits of the series must change, and each path is one of the plan's "
        "`allowed_paths`. When CI passes on the exact head without pending feedback, the "
        "observation reads the changed paths of the series once: `nominate_for_handoff` "
        "requires every requirement met (or none recorded, or a series too large to read, "
        "which the event says), and `return_for_series_requirements` leads to "
        "`development.fix_needed` otherwise. That row records the passing CI evidence, records "
        "the unmet requirements as `series_requirements` evidence, sets it as pending feedback, "
        "and bumps the revision; `request_patch` then requests a repair that carries it, and "
        "the trusted worker gate rejects a patch whose non-debug commits still do not change a "
        "matching path while the fork head is the head the evidence names. No budget is "
        "charged by the row: the Work is bounded by `no_progress` and `work_total`, then the "
        "automatic refresh, park, or a human wait. Qualitative items stay with the submission "
        "rewrite and its review (`reject_submission_rewrite`, `reject_submission_review`). A "
        "plan of an earlier version records none. The check verifies that every state that "
        "blocks a nomination on the requirements has such a row.",
        ("development.waiting_ci", "development.fix_needed", "development.agent_running",
         "handoff.awaiting_submission"),
        edges=(("development.waiting_ci", "nominate_for_handoff"),
               ("development.waiting_ci", "return_for_series_requirements"),
               ("development.waiting_ci", "return_for_feedback"),
               ("development.fix_needed", "request_patch"),
               ("development.agent_running", "reject_patch_proposal"),
               ("development.agent_running", "record_publish_success")),
        highlighted=(("development.waiting_ci", "nominate_for_handoff"),
                     ("development.waiting_ci", "return_for_series_requirements")),
    ),
    Item(
        "Result of a completed Work graph",
        "When a Work graph that the queue reports completed is terminal without a usable "
        "Result. The graph becomes completed in the `safe_outputs` job of the worker, before "
        "its `publish` job uploads the `porting-publication` artifact, and `work_recovery` "
        "allows a single retry, so a tick between the two retried healthy Work and the Result "
        "that arrived later was recorded as late.",
        "A cancelled graph is terminal without a usable Result at once: the publish job runs "
        "only for a completed outcome. A completed graph is when a publication collected in "
        "the tick names the Work and left it requested, when the worker run the queue bound to "
        "it (`queue_result.work_runs`) ended or is gone without an unexpired "
        "`porting-publication` artifact, when the queue binds no run to it, or when the "
        "artifact of the ended run was not collected within 60 minutes of the end of the run. "
        "While the run has not ended, or was not read in the tick, nothing is decided and "
        "nothing is charged: the wait is derived from the queue and the run each tick, with no "
        "new state, flag, record field or deadline, and GitHub's job timeouts bound it. The "
        "event of `allocate_work_retry` or `exhaust_work_recovery` names `worker_run`, "
        "`run_conclusion` and `result`. A Result collected after the retry still takes "
        "`record_late_effect`. The same reading applies in `planning.agent_running` and "
        "`upstream_review.agent_running`.",
        ("development.agent_running", "development.waiting_human", "handoff.agent_running",
         "handoff.reviewing", "handoff.waiting_human"),
        edges=(
            ("development.agent_running", "allocate_work_retry"),
            ("development.agent_running", "exhaust_work_recovery"),
            ("development.agent_running", "record_late_effect"),
            ("handoff.agent_running", "allocate_work_retry"),
            ("handoff.agent_running", "exhaust_work_recovery"),
            ("handoff.agent_running", "record_late_effect"),
            ("handoff.reviewing", "allocate_work_retry"),
            ("handoff.reviewing", "exhaust_work_recovery"),
            ("handoff.reviewing", "record_late_effect"),
        ),
        highlighted=(
            ("development.agent_running", "allocate_work_retry"),
            ("development.agent_running", "exhaust_work_recovery"),
            ("development.agent_running", "record_late_effect"),
            ("handoff.agent_running", "allocate_work_retry"),
            ("handoff.agent_running", "exhaust_work_recovery"),
            ("handoff.agent_running", "record_late_effect"),
            ("handoff.reviewing", "allocate_work_retry"),
            ("handoff.reviewing", "exhaust_work_recovery"),
            ("handoff.reviewing", "record_late_effect"),
        ),
    ),
    Item(
        "Where the development PR operation sits",
        "Which rows of the table take the operation that opens the development PR of a "
        "published head (`dev_pr_create`) and the CI bootstrap (`dev_pr_update`), and what "
        "happens when its launches never return a verified receipt.",
        "Four rows in `development.waiting_ci` and its wait, with no new state, budget or "
        "record field. The controller records the intent and takes `launch_development_pr` in "
        "the same tick, and again for each launch after 300 seconds without a verified "
        "receipt, charging `operation_launch` (counted on the operation, shared with fork "
        "provisioning). A verified receipt with the exact readback takes "
        "`confirm_development_pr`, which records `fork.pr_number` without a revision bump; it "
        "is also taken while paused. Launches spent without one take "
        "`exhaust_development_pr_launches` to `development.waiting_human` with "
        "`development_pr_launch_exhausted`, and the operation is closed as failed and released "
        "so that `park`, `refresh` and the stale resume apply; `resume_development_pr` returns "
        "to `development.waiting_ci` with a new revision and starts `operation_launch` again, "
        "and `resume_development` excludes this pending action. The workflow and the "
        "reconciler accept an open `riscv64-dev` PR that carries this contribution's marker "
        "of any revision or head, so a PR a lost launch left is adopted. A planned operation "
        "of a paused contribution, or one that no longer matches the record, is closed as "
        "failed and released without a row, and a receipt outside `development.waiting_ci` "
        "closes it and keeps the state. While a `Development PR <operation id>` run is queued "
        "or in progress, or the run list is unreadable, the operation is neither launched "
        "again nor exhausted, for at most 1800 seconds after its last launch.",
        ("development.agent_running", "development.waiting_ci", "development.waiting_human",
         "development.fix_needed"),
        edges=(("development.agent_running", "record_publish_success"),
               ("development.waiting_ci", "launch_development_pr"),
               ("development.waiting_ci", "confirm_development_pr"),
               ("development.waiting_ci", "exhaust_development_pr_launches"),
               ("development.waiting_human", "resume_development_pr"),
               ("development.waiting_human", "resume_development")),
        highlighted=(("development.waiting_ci", "launch_development_pr"),
                     ("development.waiting_ci", "confirm_development_pr"),
                     ("development.waiting_ci", "exhaust_development_pr_launches"),
                     ("development.waiting_human", "resume_development_pr")),
    ),
    Item(
        "Who records resolution evidence",
        "Who writes the resolution evidence record with which `/upstreaming resolve-operation` "
        "closes an open operation, given that the command cannot name a record a human has no "
        "way to write? Without a producer every resolution is refused, and since the "
        "provisioning wait after exhausted launches keeps its planned operation open, a stale "
        "(or paused and stale) wait has `park` and the stale resume fenced for good.",
        "The controller is the trusted producer. When it accepts `/upstreaming "
        "resolve-operation operation=<id> disposition=confirmed|failed|not-launched "
        "reason=\"...\"` from an authorized actor on the tracking issue of the operation's "
        "contribution, it writes the `resolution` evidence record "
        "(`resolution-<operation id>-<comment id>`) from the authenticated command (actor, "
        "comment, issue, body digest, disposition, reason) and its own snapshot of the "
        "operation (kind, status, revision, launches, discoveries) and the contribution, in "
        "the same state commit as the `resolve_*` row; a refused resolution writes none. The "
        "guard checks that the record names the operation and states the disposition. The "
        "pipeline does not verify the human's statement: the evidence makes it auditable, and "
        "the reason is stored as bounded data and never rendered. The provisioning wait keeps "
        "its fence instead of closing the operation on exhaustion as "
        "`exhaust_development_pr_launches` does, because provisioning has no in-flight run "
        "hold and a lost receipt is not proof that nothing launched: a stale or paused wait "
        "is left by resolving the operation, then `park` or `resume` (`resume_stale`). The "
        "entry and reminders of the wait, the next commands of a wait or pause with an open "
        "operation, and the reply to a command fenced in a wait or a pause name the exact "
        "resolution. A provisioning operation resolved `not-launched` or `confirmed` in "
        "`provisioning.idle` closes without a row, so the controller plans the intent again "
        "with a new id (`replan:<n>`, as the development PR operations do), each within its "
        "own `operation_launch`, and takes `require_provisioning_intervention` once the "
        "re-plans at one revision are spent.",
        ("provisioning.idle", "provisioning.waiting_human"),
        edges=(("provisioning.idle", "require_provisioning_intervention"),
               ("provisioning.waiting_human", "resume_provisioning")),
        highlighted=(("provisioning.idle", "require_provisioning_intervention"),),
    ),
)

OPEN_ITEMS: tuple[Item, ...] = ()
