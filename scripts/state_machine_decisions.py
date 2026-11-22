# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Open decisions about the lifecycle table in ``lifecycle.py``.

Each decision names the table states and verbs it concerns. ``edges`` are
table rows (``(from_state, verb)``) drawn solid, ``contested`` are the table
rows the decision may change (drawn thick), and ``candidates`` are edges an
option would add (drawn dashed). A node written ``state+flag`` is ``state``
with that flag set; a node that is not a table state is a candidate state.
The page is rendered by ``scripts/generate_state_machine_doc.py``.
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
class Decision:
    title: str
    question: str
    table: str
    options: tuple[tuple[str, str], ...]
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...] = ()
    contested: tuple[tuple[str, str], ...] = ()
    candidates: tuple[Candidate, ...] = ()


DECISIONS: tuple[Decision, ...] = (
    Decision(
        "Pause semantics",
        "What `pause` stops, and whether `resume_pause` changes the revision.",
        "`pause` only sets `paused`; Work and activity are kept, and the `paused` mask fences "
        "ticks and non-stop commands. `resume_pause` clears the flag without a revision bump. "
        "The controller today also clears the requested Work at `pause` and bumps the revision "
        "at resume, so a Result for the paused revision is dropped as stale.",
        (("A", "Keep the table: pause is a launch and allocation fence only; Results, operations, "
               "and observations fold normally and may change phase or activity."),
         ("B", "Clear the requested Work at `pause` and bump the revision at `resume_pause`."),
         ("C", "As A, but hold every Result or observation that would change phase until "
               "`resume_pause`.")),
        ("development.agent_running", "development.agent_running+paused", "development.idle+paused"),
        edges=(("development.agent_running", "pause"),
               ("development.agent_running+paused", "resume_pause")),
        contested=(("development.agent_running", "pause"),
                   ("development.agent_running+paused", "resume_pause")),
        candidates=(Candidate("development.agent_running", "pause, clear Work",
                              "development.idle+paused", "B"),),
    ),
    Decision(
        "Park quiescence and the generation fence",
        "How `restart_generation` knows the ended generation is quiescent, and how a late Result "
        "of that generation is kept out of the new one.",
        "Every transition into `parked.idle` clears the requested Work. `restart_generation` "
        "requires quiescence, and `record_late_effect` reconciles a late published Result for "
        "audit. Work carries no generation, so quiescence cannot see Work cleared at park.",
        (("A", "Keep the Work at park, marked stopped; quiescence waits until its claim is "
               "terminal or its Result is folded."),
         ("B", "Clear the Work at park but record it as parked Work that quiescence checks."),
         ("C", "Add the generation to the Work identity and drop any Result of another generation "
               "as a late effect. Combines with A or B.")),
        ("development.agent_running", "parked.idle", "queued.idle"),
        edges=(("development.agent_running", "exhaust_work_recovery"),
               ("parked.idle", "record_late_effect"),
               ("parked.idle", "restart_generation")),
        contested=(("parked.idle", "restart_generation"), ("parked.idle", "record_late_effect")),
    ),
    Decision(
        "Resume targets of recorded human waits in upstream review",
        "Where `resume_upstream_review` leads when the wait was entered by "
        "`exhaust_dispatch_recovery` rather than by `record_pr_closed_unmerged`.",
        "`resume_upstream_review` always leads to `upstream_review.waiting_ci`.",
        (("A", "Make the resume target depend on the recorded `pending_human_action`: "
               "`upstream_review.fix_needed` for Work and publication failures, "
               "`upstream_review.waiting_ci` for a closed PR."),
         ("B", "Park on dispatch exhaustion in upstream review instead of waiting for a human."),
         ("C", "Allow no automatic Work in upstream review until its rows are settled.")),
        ("upstream_review.agent_running", "upstream_review.waiting_human",
         "upstream_review.waiting_ci", "upstream_review.fix_needed", "parked.idle"),
        edges=(("upstream_review.agent_running", "exhaust_dispatch_recovery"),
               ("upstream_review.waiting_ci", "record_pr_closed_unmerged"),
               ("upstream_review.waiting_human", "resume_upstream_review")),
        contested=(("upstream_review.waiting_human", "resume_upstream_review"),
                   ("upstream_review.agent_running", "exhaust_dispatch_recovery")),
        candidates=(Candidate("upstream_review.waiting_human", "resume_upstream_review",
                              "upstream_review.fix_needed", "A"),
                    Candidate("upstream_review.agent_running", "exhaust_dispatch_recovery",
                              "parked.idle", "B")),
    ),
    Decision(
        "Handoff entry and the handoff commands",
        "Whether `handoff.awaiting_submission`, `submit_upstream`, and the observed "
        "`verify_upstream_pr` replace the `request-handoff`, `approve-handoff`, and "
        "`register-upstream-pr` commands the controller accepts today.",
        "Nomination enters `handoff.awaiting_submission`; one `submit_upstream` command drives "
        "the handoff sub-states; `verify_upstream_pr` is an observation. The table has no rows "
        "for the three commands; the controller today enters `handoff.waiting_human` and "
        "registers the upstream PR by command.",
        (("A", "Keep the table and retire the three commands."),
         ("B", "Keep `register-upstream-pr` as a command exit of `handoff.waiting_upstream_pr` "
               "instead of the observation."),
         ("C", "Name the entry state `handoff.waiting_human` and keep the commands.")),
        ("development.waiting_ci", "handoff.awaiting_submission", "handoff.cleaning_debug",
         "handoff.waiting_upstream_pr", "upstream_review.waiting_ci"),
        edges=(("development.waiting_ci", "nominate_for_handoff"),
               ("handoff.awaiting_submission", "submit_upstream"),
               ("handoff.waiting_upstream_pr", "verify_upstream_pr")),
        contested=(("handoff.waiting_upstream_pr", "verify_upstream_pr"),
                   ("handoff.awaiting_submission", "submit_upstream")),
        candidates=(Candidate("handoff.waiting_upstream_pr",
                              "register_upstream_pr<br/>/upstreaming register-upstream-pr",
                              "upstream_review.waiting_ci", "B"),),
    ),
    Decision(
        "One terminal phase for work that is not needed",
        "Whether `closed.idle` with `terminal_reason` replaces the separate terminal phases "
        "`no_work`, `deferred`, `abandoned`, and `already_released` the record schema has today.",
        "`withdraw_request` and `close_investigation` lead to `closed.idle`; the reason is "
        "`terminal_reason`.",
        (("A", "Keep `closed.idle` and migrate existing records."),
         ("B", "Keep the separate terminal phases, rendered as `closed`.")),
        ("queued.idle", "investigation.agent_running", "closed.idle", "abandoned.idle",
         "no_work.idle"),
        edges=(("queued.idle", "withdraw_request"),
               ("investigation.agent_running", "close_investigation")),
        contested=(("queued.idle", "withdraw_request"),
                   ("investigation.agent_running", "close_investigation")),
        candidates=(Candidate("queued.idle", "withdraw_request", "abandoned.idle", "B"),
                    Candidate("investigation.agent_running", "close_investigation",
                              "no_work.idle", "B")),
    ),
    Decision(
        "Capacity waits",
        "Whether lack of capacity, a planned operation, or a pending publication retry is an "
        "activity.",
        "There is no `waiting_capacity` activity: `start_operation` and `schedule_publish_retry` "
        "keep the state, and the pending operation or publication is read from "
        "`active_operation_id` and `pending_publication`. The controller today writes "
        "`waiting_capacity` in these cases.",
        (("A", "Keep the table."),
         ("B", "Add `waiting_capacity` states to the table."),
         ("C", "Model the wait as a flag beside `paused`.")),
        ("provisioning.idle", "provisioning.waiting_capacity", "development.agent_running",
         "development.waiting_capacity"),
        edges=(("provisioning.idle", "start_operation"),
               ("development.agent_running", "schedule_publish_retry")),
        contested=(("provisioning.idle", "start_operation"),
                   ("development.agent_running", "schedule_publish_retry")),
        candidates=(Candidate("provisioning.idle", "start_operation",
                              "provisioning.waiting_capacity", "B"),
                    Candidate("development.agent_running", "schedule_publish_retry",
                              "development.waiting_capacity", "B")),
    ),
    Decision(
        "Repair-budget exhaustion",
        "Whether `no_progress` exhaustion parks straight from `development.agent_running` or "
        "through `development.fix_needed`.",
        "`reject_patch_proposal` charges `no_progress` and leads to `development.fix_needed`, "
        "where `exhaust_repair_attempts` parks when any repair budget is exhausted at allocation.",
        (("A", "Keep the table."),
         ("B", "Add `exhaust_repair_attempts` from `development.agent_running` for the "
               "`no_progress` streak.")),
        ("development.agent_running", "development.fix_needed", "parked.idle"),
        edges=(("development.agent_running", "reject_patch_proposal"),
               ("development.fix_needed", "request_repair"),
               ("development.fix_needed", "exhaust_repair_attempts")),
        contested=(("development.fix_needed", "exhaust_repair_attempts"),),
        candidates=(Candidate("development.agent_running", "exhaust_repair_attempts",
                              "parked.idle", "B"),),
    ),
    Decision(
        "Investigation retry budget",
        "Which budget `reject_dossier` charges.",
        "`reject_dossier` charges `investigation_attempts`, which counts against "
        "`retries.max_attempts_per_contribution`.",
        (("A", "Keep the table."),
         ("B", "Add a dedicated `retries.investigation_attempts` key."),
         ("C", "Charge `work_recovery` instead.")),
        ("investigation.idle", "investigation.agent_running", "investigation.waiting_human"),
        edges=(("investigation.idle", "dispatch_investigation"),
               ("investigation.agent_running", "reject_dossier"),
               ("investigation.agent_running", "exhaust_investigation_recovery"),
               ("investigation.waiting_human", "resume_investigation")),
        contested=(("investigation.agent_running", "reject_dossier"),),
    ),
    Decision(
        "Publication recovery exhaustion and its resume",
        "From which states `exhaust_publish_recovery` is taken, and where `resume_development` "
        "leads.",
        "`exhaust_publish_recovery` leaves only `development.agent_running`; "
        "`resume_development` leads to `development.fix_needed`.",
        (("A", "Keep the table."),
         ("B", "Also take `exhaust_publish_recovery` from `development.fix_needed` and resume "
               "to `development.idle`.")),
        ("development.agent_running", "development.fix_needed", "development.waiting_human",
         "development.idle"),
        edges=(("development.agent_running", "schedule_publish_retry"),
               ("development.agent_running", "exhaust_publish_recovery"),
               ("development.waiting_human", "resume_development")),
        contested=(("development.agent_running", "exhaust_publish_recovery"),
                   ("development.waiting_human", "resume_development")),
        candidates=(Candidate("development.fix_needed", "exhaust_publish_recovery",
                              "development.waiting_human", "B"),
                    Candidate("development.waiting_human", "resume_development<br/>/upstreaming resume",
                              "development.idle", "B")),
    ),
    Decision(
        "Dispatch and Work recovery exhaustion targets",
        "Whether dispatch exhaustion and Work exhaustion should lead to the same place.",
        "`exhaust_dispatch_recovery` waits for a human; `exhaust_work_recovery` parks. "
        "A stale rejection takes `replan_development`. A development PR resource that does not "
        "match has no row.",
        (("A", "Keep the table."),
         ("B", "Both wait for a human."),
         ("C", "Both park.")),
        ("development.agent_running", "development.waiting_human", "parked.idle", "development.idle"),
        edges=(("development.agent_running", "exhaust_dispatch_recovery"),
               ("development.agent_running", "exhaust_work_recovery"),
               ("development.agent_running", "replan_development")),
        contested=(("development.agent_running", "exhaust_dispatch_recovery"),
                   ("development.agent_running", "exhaust_work_recovery")),
        candidates=(Candidate("development.agent_running", "exhaust_work_recovery",
                              "development.waiting_human", "B"),
                    Candidate("development.agent_running", "exhaust_dispatch_recovery",
                              "parked.idle", "C")),
    ),
    Decision(
        "Where park and refresh apply",
        "From which states `park_contribution` and `refresh_investigation` are accepted.",
        "Each state lists them explicitly: `park_contribution` from idle, fix-needed, gate, and "
        "human-wait states, never from `handoff.waiting_upstream_pr`; `refresh_investigation` "
        "from `development.idle` and `handoff.awaiting_submission`. The controller today "
        "accepts `park` from every non-terminal state and `refresh` from every development and "
        "handoff activity when no Work or operation is active.",
        (("A", "Allow both from every non-terminal state except the listed exclusions, with "
               "quiescence guards."),
         ("B", "Keep the explicit rows.")),
        ("development.agent_running", "development.idle", "handoff.awaiting_submission",
         "queued.idle", "parked.idle"),
        edges=(("development.idle", "refresh_investigation"),
               ("development.idle", "park_contribution"),
               ("handoff.awaiting_submission", "refresh_investigation"),
               ("handoff.awaiting_submission", "park_contribution")),
        candidates=(Candidate("development.agent_running", "park_contribution<br/>/upstreaming park",
                              "parked.idle", "A"),
                    Candidate("development.agent_running",
                              "refresh_investigation<br/>/upstreaming refresh", "queued.idle", "A")),
    ),
    Decision(
        "Return path after refresh",
        "Where a refreshed investigation that decides to develop leads.",
        "`readmit_development` leads to `development.fix_needed` on the existing fork; "
        "`admit_development` without a recorded refresh leads to `provisioning.idle`.",
        (("A", "Keep the table."),
         ("B", "Always provision again through `admit_development`.")),
        ("development.idle", "queued.idle", "investigation.agent_running", "provisioning.idle",
         "development.fix_needed"),
        edges=(("development.idle", "refresh_investigation"),
               ("investigation.agent_running", "admit_development"),
               ("investigation.agent_running", "readmit_development")),
        contested=(("investigation.agent_running", "readmit_development"),),
    ),
    Decision(
        "Park by dossier decision and park by command",
        "Whether parking from investigation is one verb or two.",
        "Two verbs: `park_investigation` is a Result transition taken on a dossier decision; "
        "`park_contribution` is the `/upstreaming park` command.",
        (("A", "Keep two verbs."),
         ("B", "One verb with two triggers.")),
        ("investigation.idle", "investigation.agent_running", "parked.idle"),
        edges=(("investigation.agent_running", "park_investigation"),
               ("investigation.idle", "park_contribution")),
        contested=(("investigation.agent_running", "park_investigation"),),
        candidates=(Candidate("investigation.agent_running",
                              "park_contribution<br/>/upstreaming park", "parked.idle", "B"),),
    ),
    Decision(
        "Beginning the investigation",
        "Whether `begin_investigation` and `dispatch_investigation` are two writes.",
        "`begin_investigation` leads to `investigation.idle`, and `dispatch_investigation` "
        "allocates Work from there. The controller today writes `investigation.agent_running` "
        "directly from `queued.idle`.",
        (("A", "Keep two transitions, written in two commits."),
         ("B", "One compound write from `queued.idle` to `investigation.agent_running` that "
               "records both events.")),
        ("queued.idle", "investigation.idle", "investigation.agent_running"),
        edges=(("queued.idle", "begin_investigation"),
               ("investigation.idle", "dispatch_investigation")),
        contested=(("queued.idle", "begin_investigation"),),
        candidates=(Candidate("queued.idle", "begin_investigation",
                              "investigation.agent_running", "B"),),
    ),
    Decision(
        "Provisioning failures",
        "How provisioning failures are recovered, and whether the `approve-scope` and `provision` "
        "commands the controller accepts today stay.",
        "`start_operation` charges `operation_launch`; its exhaustion and unsafe prerequisites "
        "take `require_provisioning_intervention`. `resume_provisioning` charges "
        "`provisioning_attempts`, and `exhaust_provisioning_attempts` parks. "
        "`close_investigation` also leaves `provisioning.idle`.",
        (("A", "Keep the table and retire `approve-scope` and `provision`."),
         ("B", "Keep the commands as the resume path."),
         ("C", "Park directly when `operation_launch` is exhausted.")),
        ("provisioning.idle", "provisioning.waiting_human", "development.idle", "parked.idle",
         "closed.idle"),
        edges=(("provisioning.idle", "start_operation"),
               ("provisioning.idle", "require_provisioning_intervention"),
               ("provisioning.waiting_human", "resume_provisioning"),
               ("provisioning.waiting_human", "exhaust_provisioning_attempts"),
               ("provisioning.idle", "confirm_provisioning"),
               ("provisioning.idle", "close_investigation")),
        contested=(("provisioning.waiting_human", "exhaust_provisioning_attempts"),
                   ("provisioning.idle", "close_investigation")),
        candidates=(Candidate("provisioning.idle", "exhaust_operation_launch", "parked.idle", "C"),),
    ),
    Decision(
        "Upstream revision outcomes",
        "What happens when revision Work in upstream review is rejected or reports no change.",
        "`upstream_review.agent_running` has no rejection or no-change transition; only "
        "`record_revision_publish_success` and the recovery transitions leave it.",
        (("A", "Mirror development: add `reject_patch_proposal` and `record_no_change`."),
         ("B", "Send every unusable revision outcome to `upstream_review.waiting_human`.")),
        ("upstream_review.waiting_ci", "upstream_review.fix_needed", "upstream_review.agent_running",
         "upstream_review.waiting_human"),
        edges=(("upstream_review.waiting_ci", "record_upstream_ci_failure"),
               ("upstream_review.fix_needed", "request_upstream_revision"),
               ("upstream_review.agent_running", "record_revision_publish_success")),
        candidates=(Candidate("upstream_review.agent_running", "reject_patch_proposal",
                              "upstream_review.fix_needed", "A"),
                    Candidate("upstream_review.agent_running", "record_no_change",
                              "upstream_review.waiting_ci", "A"),
                    Candidate("upstream_review.agent_running", "reject_upstream_revision",
                              "upstream_review.waiting_human", "B")),
    ),
    Decision(
        "Commands without table rows",
        "Which commands the controller accepts today become table rows: `abandon`, `restart`, "
        "`clarify`, `revise-scope`, `force-develop`, `approve-scope`, `provision`, "
        "`request-handoff`, `approve-handoff`, `register-upstream-pr`, and "
        "`authorize-followup`.",
        "None of them has a row. `block` is the `blocked` flag.",
        (("A", "Add a row for each command that stays, such as `abandon` to `closed.idle` and "
               "`force-develop` from `parked.idle`."),
         ("B", "Retire the commands that table verbs replace (the handoff commands, "
               "`approve-scope`, `provision`, `abandon`, and `restart`).")),
        ("development.idle", "parked.idle", "provisioning.idle", "closed.idle", "queued.idle"),
        candidates=(Candidate("development.idle", "abandon<br/>/upstreaming abandon", "closed.idle", "A"),
                    Candidate("closed.idle", "restart<br/>/upstreaming restart", "queued.idle", "A"),
                    Candidate("parked.idle", "force_develop<br/>/upstreaming force-develop",
                              "provisioning.idle", "A")),
    ),
    Decision(
        "Observations and outcomes without verbs",
        "Whether CI passing without nomination, a CI access wait, and a withdrawn obsolete repair "
        "become table verbs.",
        "The table has no rows for them. The controller today moves to an idle activity when CI "
        "passes without nomination, to `waiting_human` without a pending action on a CI access "
        "wait, and back to `waiting_ci` when an obsolete repair is withdrawn.",
        (("A", "Name each as a verb in the table."),
         ("B", "Make them self-transitions that never change phase or activity."),
         ("C", "Remove them from the controller.")),
        ("development.agent_running", "development.waiting_ci", "development.idle",
         "development.waiting_human"),
        edges=(("development.agent_running", "record_publish_success"),),
        candidates=(Candidate("development.waiting_ci", "record_ci_passed", "development.idle", "A"),
                    Candidate("development.waiting_ci", "record_ci_access_wait",
                              "development.waiting_human", "A"),
                    Candidate("development.agent_running", "withdraw_obsolete_repair",
                              "development.waiting_ci", "A")),
    ),
    Decision(
        "Late Results during handoff",
        "What a late published Result does in the handoff states.",
        "No handoff state has a Result transition, so a late Result changes nothing there. The "
        "controller today folds a late published Result in handoff and moves to `waiting_ci`.",
        (("A", "Keep the table: ignore late Results in handoff."),
         ("B", "Record them with `record_late_effect`, as in `parked.idle`."),
         ("C", "Accept them only before `handoff.waiting_upstream_pr`.")),
        ("handoff.awaiting_submission", "handoff.waiting_upstream_pr", "parked.idle"),
        edges=(("parked.idle", "record_late_effect"),
               ("handoff.waiting_upstream_pr", "record_audit_observation")),
        candidates=(Candidate("handoff.awaiting_submission", "record_late_effect",
                              "handoff.awaiting_submission", "B"),
                    Candidate("handoff.waiting_upstream_pr", "record_late_effect",
                              "handoff.waiting_upstream_pr", "B")),
    ),
    Decision(
        "Budget keys",
        "Whether budgets get their own `policy.json` keys.",
        "`dispatch_recovery`, `work_recovery`, `publish_recovery`, `operation_launch`, "
        "`provisioning_attempts`, `internal_pr_closure`, and `submission_comment` share "
        "`retries.infrastructure_per_head_failure`; `investigation_attempts` and "
        "`repair_attempts_total` share `retries.max_attempts_per_contribution`. The "
        "`record_reminder` interval of 7 days is not a policy key.",
        (("A", "One policy key per budget, plus a reminder interval key."),
         ("B", "Keep shared keys.")),
        ("development.agent_running", "development.waiting_human", "parked.idle",
         "handoff.closing_internal_pr", "handoff.waiting_internal_pr"),
        edges=(("development.agent_running", "retry_dispatch"),
               ("development.agent_running", "allocate_work_retry"),
               ("development.agent_running", "schedule_publish_retry"),
               ("development.agent_running", "exhaust_dispatch_recovery"),
               ("development.agent_running", "exhaust_work_recovery"),
               ("handoff.closing_internal_pr", "retry_internal_pr_closure"),
               ("handoff.closing_internal_pr", "exhaust_internal_pr_retries")),
        contested=(("development.agent_running", "retry_dispatch"),
                   ("development.agent_running", "allocate_work_retry"),
                   ("development.agent_running", "schedule_publish_retry"),
                   ("handoff.closing_internal_pr", "retry_internal_pr_closure")),
    ),
    Decision(
        "Flaky CI reruns",
        "How a CI failure is classified flaky before `rerun_failed_ci` is taken.",
        "`rerun_failed_ci` charges `flaky_reruns`; when it is exhausted or the failure is not "
        "flaky, `record_ci_failure` is taken. The classification is not defined.",
        (("A", "The worker classifies the failure and requests a rerun."),
         ("B", "The collector classifies from job logs."),
         ("C", "Drop `rerun_failed_ci` and repair every failure.")),
        ("development.waiting_ci", "development.fix_needed"),
        edges=(("development.waiting_ci", "rerun_failed_ci"),
               ("development.waiting_ci", "record_ci_failure")),
        contested=(("development.waiting_ci", "rerun_failed_ci"),),
    ),
    Decision(
        "Revision bumps",
        "Whether the `revision` effect of every row is the complete revision rule.",
        "Each row states whether it bumps the revision. Examples: `allocate_work_retry` and "
        "`confirm_provisioning` bump, `resume_pause` does not, `resume_block` does.",
        (("A", "The table is the complete rule."),
         ("B", "Bump only on changed requested work, and drop the bumps the table adds beyond it.")),
        ("development.waiting_ci", "development.waiting_ci+paused", "development.waiting_ci+blocked",
         "development.agent_running"),
        edges=(("development.waiting_ci", "pause"),
               ("development.waiting_ci+paused", "resume_pause"),
               ("development.waiting_ci", "block"),
               ("development.waiting_ci+blocked", "resume_block"),
               ("development.agent_running", "allocate_work_retry")),
        contested=(("development.waiting_ci+paused", "resume_pause"),
                   ("development.waiting_ci+blocked", "resume_block"),
                   ("development.agent_running", "allocate_work_retry")),
    ),
    Decision(
        "Blocked flag",
        "Whether `block` changes the activity.",
        "`block` only sets `blocked_reason`. The controller today also sets the activity to "
        "`waiting_human`.",
        (("A", "Keep the table."),
         ("B", "Also enter the phase's `waiting_human` activity.")),
        ("development.waiting_ci", "development.waiting_ci+blocked", "development.waiting_human+blocked"),
        edges=(("development.waiting_ci", "block"),
               ("development.waiting_ci+blocked", "resume_block")),
        contested=(("development.waiting_ci", "block"),),
        candidates=(Candidate("development.waiting_ci", "block<br/>/upstreaming block",
                              "development.waiting_human+blocked", "B"),),
    ),
    Decision(
        "Remote operation states",
        "Whether remote operations get their own table beside this one.",
        "The table names operation receipts as triggers but does not model the operation's own "
        "states (planned, applying, uncertain, confirmed, failed, not launched). Publication "
        "retries live in `pending_publication`.",
        (("A", "Add an operation table with the same tests."),
         ("B", "Leave operations outside the table."),
         ("C", "Move publication retries into the operation table.")),
        ("provisioning.idle", "provisioning.waiting_human", "development.idle"),
        edges=(("provisioning.idle", "start_operation"),
               ("provisioning.idle", "require_provisioning_intervention"),
               ("provisioning.idle", "confirm_provisioning")),
        contested=(("provisioning.idle", "start_operation"),),
    ),
)
