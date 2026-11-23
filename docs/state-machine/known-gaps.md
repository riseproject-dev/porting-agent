---
title: Known gaps
parent: State machine
nav_order: 4
---

# Known gaps between the code and the table
{: .no_toc }

The [lifecycle table]({{ '/state-machine/table/' | relative_url }}) is the source of truth. The places below are where the code does not yet do what the table says, or where `lifecycle check` does not yet catch a violation. Each entry names the table rows involved and the behavior the table requires. Remove an entry in the change that fixes it.

<details open markdown="block">
<summary>Contents</summary>
- TOC
{:toc}
</details>

## A publication held in a legacy state is not stopped by the table

- **Where:** a Result held for a publish retry; the legacy publication outcome in `reconcile.py` and `compat.legacy_pause`.
- **Today:** a pause in a state of the table follows the table: from `development.agent_running` it leads to `development.fix_needed` and clears the pending publication. But the legacy publication outcome holds a Result for a publish retry in `development.waiting_capacity`, a state the table does not have, and a stop there leaves the record in `development.idle`. With CI evidence recorded for the head, `development.idle` requests no Work, so after the resume it waits for CI with nothing held and no pending human action. `nominate_for_handoff` is also taken with `no_pending_actionable_feedback` asserted unconditionally, while nothing in development clears `pending_feedback_evidence_ids` yet.
- **Table:** a held publication exists only in `development.agent_running` (`schedule_publish_retry`), a pause there leads to `development.fix_needed`, and nomination requires no pending actionable feedback.
- **Required:** take the publication outcome through the table's rows, then assert `no_pending_actionable_feedback` from the record.

## A green CI rerun at the gate changes the record without a row

- **Where:** `handoff.awaiting_submission`, CI observation in `observe_ci.py`.
- **Today:** a new run or attempt replaces `last_ci_evidence_id` with no event and no revision bump. `handoff_request.command_id` still names the old evidence.
- **Table:** no row at the gate sets `last_ci_evidence_id` on a passing run.
- **Required:** either declare the row in the table or keep the evidence id unchanged at the gate.

## CI failure and an actionable review in the same observation can drop the review

- **Where:** `handoff.awaiting_submission`, rows `return_for_repair` and `record_actionable_review`.
- **Today:** `return_for_repair` wins and repair Work is allocated in the same tick, so the review is not read. After the repair publishes, the review no longer matches the head and is filtered out. It is recorded only if no Work was allocated in between, and then through the legacy path, which overwrites `last_ci_evidence_id` with the review evidence id.
- **Table:** the review is recorded once CI is observed again.
- **Required:** record the review evidence when the failure wins, so the repair Work carries it.

## `lifecycle check` misses some decision violations

These table mutations break a settled decision but still pass the check:

- `record_actionable_review` at the gate not setting `pending_feedback_evidence_ids`, not clearing `clean_head_sha`, `handoff_request` or `pending_human_action`, or touching `last_ci_evidence_id`.
- `return_for_repair` at the gate not setting `last_ci_evidence_id`, or setting pending feedback.
- `record_explanation` at the gate clearing `last_ci_evidence_id`.
- `request_review_revision` charging `no_progress`, or losing the controlled-ref guard.
- `request_patch` gaining guards beyond write admission.
- `resume_development` keeping a pending publication.

**Required:** add a check for each.

## The stop flag in states the table does not have

- **Where:** `compat.legacy_pause` and `compat.apply_command`, for a record in a state of `compat.LEGACY_STATES` (the development and upstream review states the table does not have, for example `development.idle`, `development.waiting_capacity`, `upstream_review.idle`).
- **Today:** such a state has no row, so the engine cannot take `pause`, `pause_on_identity_change`, or `pause_on_processing_error` there. `compat.legacy_pause` records the same fields and clears the same things, but the activity that held the Work or a publication becomes `idle` instead of the table's state in which the Work is due. `resume` and `park` on such a record are applied by the legacy command code: its resume has no `resume_stale` (a long stop is not followed by a refresh). Its park is refused while an operation is open, as the table's. `refresh` and `abandon`, which no row of such a state takes, are refused there.
- **Table:** the flag verbs apply in every non-terminal state, with `set_moves`; `resume_stale` applies after a long stop in provisioning and development; `park_contribution` requires no open operation.
- **Required:** migrate the legacy states to table states (development and upstream review rows), then remove the legacy branches.

## No dossier decides close

- **Where:** `close_investigation` from `investigation.agent_running`; the dossier contract (`schemas/dossier.schema.json`, `dossier_gate.py`, the investigator's instructions).
- **Today:** the reconciler takes `close_investigation` for a verified dossier whose decision is close, with `terminal_reason` `no_work`. But the dossier schema and the investigator's instructions admit the decisions develop and park only, so the research gate refuses a dossier that decides close before it reaches the reconciler, and no contribution is closed by its dossier.
- **Table:** a verified dossier decision close leads to `closed.idle` and sets `terminal_reason`.
- **Required:** a decision on when an investigation decides close (for example riscv64 wheels already published for the latest release), then the decision in the dossier contract and the investigator's instructions.

## Some resumes lead where nothing continues yet

- **`resume_ci`** in `development.waiting_human` and `handoff.waiting_human` leads to the CI wait, but the rerun of the interrupted jobs that the row states is not launched. No record enters a wait with the pending action `ci_interrupted` yet.
- **Required:** collector reruns.

## CI on the clean head is not rerun

- **Where:** `handoff.waiting_ci`, rows `rerun_interrupted_ci` and `exhaust_interrupted_reruns`; the CI observation in `observe_ci.py`.
- **Today:** the observation takes `accept_clean_head_ci` or `return_for_repair`. A required job that was cancelled or lost its runner is a job that did not execute: the contribution stays in `handoff.waiting_ci` and nothing reruns it, so `interrupted_reruns` is never charged and its exhaustion wait is never entered. `pause` and `park` still apply there.
- **Table:** the collector reruns the interrupted jobs on the exact head within `interrupted_reruns` and waits for a human with `ci_interrupted` once it is exhausted.
- **Required:** a trusted rerun of the interrupted jobs on the exact head and attempt, then take the two rows.

## The failing evidence of the clean head is not recorded

- **Where:** `handoff.waiting_ci`, row `return_for_repair`; the CI observation in `observe_ci.py`.
- **Today:** a blocking CI failure on a clean head that the cleanup rewrote stores the failing evidence and takes `return_for_repair`, which sets nothing. The record keeps `last_ci_evidence_id` of the candidate head, whose CI passed, so in `development.fix_needed` the repair is requested against passing evidence of a head that is no longer the fork head: it names no failing job, and the clean head's failure is in the event details only.
- **Table:** `return_for_repair` from `handoff.waiting_ci` bumps the revision and clears the clean head, `handoff_request`, and the pending action; it sets no evidence. `return_for_repair` from `handoff.awaiting_submission` records the failing evidence.
- **Required:** a table decision that `return_for_repair` from `handoff.waiting_ci` sets `last_ci_evidence_id` to the failing evidence of the clean head, then set it in the observation.

## A moved branch before the package is posted waits for a human

- **Where:** `handoff.preparing_submission` (and `handoff.closing_internal_pr`); `handoff.post_package` and `handoff.close_development_pr`.
- **Today:** the workflow posts the package only while `riscv64` still resolves to the clean head, and closes the development PR only at the clean head. When the branch moved, the attempt fails as an infrastructure failure: `retry_submission_comment` (or `retry_internal_pr_closure`) within the budget, then the exhaustion row to `handoff.waiting_human`, whose only exit is the resume to the same step. Nothing returns the contribution to development, so a human restores the branch or abandons.
- **Table:** these states have no row for a changed fork head; the decision that a changed head before the package is posted returns to development is taken by `return_for_repair` in `handoff.cleaning_debug` and `handoff.waiting_ci` only.
- **Required:** a table decision for a changed head in `handoff.closing_internal_pr` and `handoff.preparing_submission` (for example `return_for_repair`), then take it from the attempt's failure.

## Dossier contribution-policy items are not enforced before handoff

- **Where:** `nominate_for_handoff` from development CI in `observe_ci.py`; the submission package in `handoff.build_package`.
- **Today:** the dossier's contribution rules (for example "add a CHANGELOG entry") and its admitted paths are not checked against the series before the contribution is nominated for handoff. The submission package only flags them: it shows the rules and names the admitted paths the series does not change, for the human to resolve before submitting. The live contribution `closeio--ciso8601/1` is in this case: its dossier asks for a CHANGELOG entry and its series does not change `CHANGELOG.md`.
- **Table:** `nominate_for_handoff` requires every required CI job to pass on the exact head; it states no contribution-policy check.
- **Required:** a table decision on whether unmet contribution-policy items block nomination or are requested as development Work, then the check from the dossier.

## The development PR stays closed after a retraction

- **Where:** `retract_submission` and `refresh_investigation` from `handoff.waiting_upstream_pr`; the development PR plans in `controller.py`.
- **Today:** the development PR was closed during the handoff. After a retraction or a refresh nothing reopens it: the development PR plans start only for a fork without a recorded PR, so later development heads have no open development PR for CI and review.
- **Table:** `retract_submission` unlocks branch work; no row reopens or creates the development PR.
- **Required:** decide where the development PR operation sits in the table (see "Development PR operations have no row").


## A legacy pending action in upstream review has no resume

- **Where:** `upstream_review.waiting_human` with `pending_human_action` `task_without_outcome`, written by the legacy Work deadline in `reconcile.py`.
- **Today:** no resume row's guard admits that pending action, and `restart_generation` is refused while the upstream PR is open, so the only exit is `park` then `abandon`.
- **Table:** Work that ends without a Result takes `allocate_work_retry` within its budget and `exhaust_work_recovery` into the human wait with a pending action that `resume_upstream_revision` admits.
- **Required:** replace the legacy Work deadline by the table's Work recovery verbs before upstream review becomes reachable.

## The first patch in `development.idle` is selected outside the table

- **Where:** `controller._legacy_patch`, for a record in `development.idle` without CI evidence (the legacy `no_change` and stale outcomes, and the provisioning migration of a proposal held on the fork, leave records there).
- **Today:** the controller requests `development_patch` itself. Like `request_patch`, the Work carries the pending feedback and the allocation charges `work_total`, and no Work is requested while `work_total` or `no_progress` is exhausted; but the record then stays in `development.idle`, deferred, with no exhaustion row and no human wait.
- **Table:** patch Work is requested only from `development.fix_needed`, where an exhausted budget takes `exhaust_work_total`, `refresh_on_exhaustion`, or `exhaust_repair_attempts`.
- **Required:** migrate `development.idle` to the table's development states.

## Worker outcomes in development and upstream review are folded by legacy code

- **Where:** `reconcile.apply_publication`, for Results of development and upstream revision Work.
- **Today, development:** a rejected or blocked outcome charges `no_progress` and nothing else, bumps the revision, and leads to `fix_needed`, as `reject_patch_proposal` does, but it is written by the legacy outcome: a blocked outcome also records its rehearsal evidence as pending feedback and clears `proposal_artifact`, a rejected one records its artifact in `proposal_artifact`. A stale rejection leads to the legacy `development.idle` without a revision bump; `no_change` leads to `development.idle`; a published or uncertain Result leads to `waiting_ci` and starts `no_progress` again, without clearing the feedback its Work carried.
- **Today, upstream review** (from `upstream_review.agent_running`):
  - published or uncertain: `upstream_review.waiting_ci`, `no_progress` started again, the carried feedback kept (table: `record_revision_publish_success`, which clears exactly the carried feedback);
  - `no_change`: the legacy `upstream_review.idle`, which the next CI observation moves to `upstream_review.waiting_ci` with no pending action (table: `require_reviewer_answer` to `upstream_review.waiting_human` with the pending action `answer_reviewer`);
  - `proposal`, which no publisher produces any more: rejected in trusted validation, as below;
  - `infra_error` within the retry limit: the legacy `upstream_review.waiting_capacity` with the pending publication, which the next CI observation moves to `upstream_review.waiting_ci` while the publish-only retry is still scheduled (table: `schedule_publish_retry`, which keeps `upstream_review.agent_running`); beyond the limit, `upstream_review.waiting_human` with the pending action `publication_infrastructure_failure` and no `counter` (table: `exhaust_publish_recovery`, whose pending action names `publish_recovery`);
  - rejected for validation, or blocked: `upstream_review.fix_needed` with a revision bump and `no_progress` charged, as `reject_patch_proposal` does; a blocked outcome also records its rehearsal evidence as pending feedback;
  - rejected as stale: the legacy `upstream_review.idle` without a revision bump, then `upstream_review.waiting_ci` (the table has no row for it).
- **Table:** in development `reject_patch_proposal`, `replan_development` (to `development.fix_needed` with a revision bump), `request_ci_rerun`, `record_publish_success` (clears exactly the carried feedback), `schedule_publish_retry`, `exhaust_publish_recovery`; in upstream review `record_revision_publish_success`, `reject_patch_proposal`, `require_reviewer_answer`, `schedule_publish_retry`, `exhaust_publish_recovery`.
- **Required:** take the publication outcome through the table's rows.

## A completed Work graph whose Result is not collected yet is retried

- **Where:** `reconcile.dispatch_recovery`, `allocate_work_retry` and `exhaust_work_recovery`.
- **Today:** the reconciler establishes the fact "the Work graph is terminal without a usable Result" for a graph the queue reports `completed` or `cancelled` while the record still holds its Work. A graph completed by a worker whose publication artifact the tick has not collected yet is therefore retried at a new revision, and the Result that arrives later takes `record_late_effect`.
- **Table:** `allocate_work_retry` applies to terminal Work without a usable Result.
- **Required:** establish the fact only once the Result of a completed graph is known to be missing (its worker run finished without a publication artifact).

## Development PR operations have no row

- **Where:** the development PR and CI bootstrap plans in `controller.py` and the development PR receipt in `reconcile.py` (legacy sites `controller.plan_development_pr`, `controller.plan_ci_bootstrap`, `reconcile.confirm_development_pr`).
- **Today:** the operations `dev_pr_create` and `dev_pr_update` follow the operation table, but no lifecycle row starts them or is fired by them: the plan moves the record to the legacy `development.waiting_capacity`, the confirmed operation moves it to `development.waiting_ci`, and launches that never return a receipt stop at the launch limit with the operation open and no human wait.
- **Table:** `record_publish_success` leads to `development.waiting_ci`; no row of the table creates the development PR.
- **Required:** decide where the development PR operation sits in the table, then take it through rows.

## A failed provisioning run writes no receipt

- **Where:** `provision.yml` and `provisioning.py`; the provisioning receipt in `reconcile.py`.
- **Today:** the workflow writes `outcome.json` only when provisioning succeeds. A run that fails definitely (a refused API write, a readback mismatch) leaves no receipt, so the controller sees a launch without a receipt: it launches the operation again within `operation_launch` and then waits for a human with `provisioning_launch_exhausted`. The reconciler already folds a verified receipt whose status is not `provisioned` as a definite failure (`record_failure`, then `require_provisioning_intervention` with `provisioning_operation_failed`).
- **Table:** a remote call that returns a definite failure takes `record_failure`.
- **Required:** write a failure receipt from the workflow for a definite failure of the effect.

## Resolution evidence has no producer

- **Where:** `/upstreaming resolve-operation`, guard "a resolution evidence record states the disposition".
- **Today:** the command takes the table's `resolve_*` rows with a record of kind `resolution` from the state store, but nothing writes such a record, so every resolution is refused with "resolution evidence record not found".
- **Table:** `resolve-operation` closes an open operation with a resolution evidence record.
- **Consequence:** after `require_provisioning_intervention` for exhausted launches the planned provisioning operation is still open. Within `refresh.stale_after_days` the resume leaves the wait (`resume_provisioning`). Once the wait is older, `resume_stale` and `park` are refused while the operation is open (both require no open operation), and only a resolution or a late receipt that closes the operation lets the contribution leave the wait. A failed provisioning run writes no receipt, so a definite failure reaches this wait through launch exhaustion. A `pause` taken in the wait is accepted, and the resume after it is refused once the stop is stale, so the contribution is then paused and stuck as well. While it is not paused, an operator can run the provisioning workflow again for the planned operation once the cause is corrected; its receipt closes the operation, after which `park` and `resume_stale` apply.
- **Required:** a trusted way to record resolution evidence, or a table decision on the exits of this wait once it is stale (for example `resume_provisioning` without the not-stale guard, or no `pause` in it).

## Work requested before the Work identity carried the generation

A Work payload written before the Work identity carried `generation` and `dedupe_key` names neither: the reconciler and the publisher read it as generation 0 and recognize its Work by its revision (`work.task_identity`), and requested Work recorded without `active_work.generation` is generation 0 (`work.work_generation`) and is dispatched with the payload it was first submitted with. Remove both fallbacks once no such Work or worker is in flight.

## Records written before `budget.reruns` existed

The migrated live record has no `budget.reruns`. The schema makes it optional and the engine counts from zero. Make the field required once every record carries it.

## `blocked_reason` is still a field

The stop flag is `paused` alone, and the schema admits only null for `blocked_reason`. Records still carry the field. Remove it once every record is rewritten by the strict-schema migration.

## Readings to confirm

These follow the literal reading of a decision and are kept until a decision says otherwise:

- `resume_upstream_review` still clears pending feedback after `answer_reviewer`, although the pending-feedback decision says every resume keeps it.
- When a CI failure and a review arrive together at the gate, the failure wins.
- `record_actionable_review` does not list the processed comment and review ids; the collector records them beside the row.
- The admission scope guard is on `admit_development` only, not on `readmit_development`.
- `resume_stale` from a `provisioning` state, which has no fork, records a refresh with the phase `provisioning` and no head or development PR. The refreshed dossier returns to development on the recorded fork only when the refresh began with the fork and development PR the record still holds; otherwise development is admitted afresh.
- The flag migration turns a block's human wait without a pending action into the `idle` activity of its phase, as the old `resume` did (for `development` that is the legacy `development.idle`), records `paused_at` as the record's `updated_at`, and records the controller's identity block with `paused_reason` `upstream_identity_changed`, the reason the table names.
- `pause_on_identity_change` is guarded by `not paused`: an identity change observed while a contribution is paused sets nothing; the controller still selects no Work for it while the identity differs.
- The reminder interval is counted in whole days from the exact time of the request or of the last reminder; a day counts when it ends on a weekday (UTC). Any pending action in a human wait of the table gets reminders, including the pending actions that only the legacy paths write (for example `task_without_outcome`).
- A reminder is suppressed while paused and counted from the request, so after a long pause the resume and an overdue reminder can be posted in the same tick.
- `/upstreaming resume` on a contribution that is neither paused nor in a human wait or in `parked.idle` is answered with `not paused`.
- `/upstreaming resume` from `parked.idle` records `admission_override` only when it names `class` or `paths`; the override's reason is the resume's reason, and `class` alone or `paths` alone is recorded as given. Every other resume refuses `class` and `paths`.
- `abandon` records `terminal_reason` `abandoned`; the reason the human gives is kept in the event only. A paused record stays paused when it is abandoned, and a later `resume` of the closed record takes `resume_pause`, which clears the flag and changes nothing else (the clearer of the flag has no terminal guard).
- A command field that the row taken does not read is refused, not ignored: `head` on a `refresh` outside `handoff.waiting_upstream_pr`, `class` and `paths` outside `parked.idle`.
- The terminal migration records `terminal_reason` `withdrawn` for a request whose intake label was removed (recognized by the reason the controller wrote), `abandoned` for every other `abandoned` record, and the name of the old phase (`no_work`, `deferred`, `already_released`) for the others.
- A contribution skipped by a tick, after a collector failure or an unexpected error, leaves every command on its tracking issue, `resume` included, unconsumed and without a reply until a tick processes it. An error in a contribution that is already stopped, or that the same tick would have created, is reported in the tick report (`controller.alerts`) and the workflow annotation only, with no event and no post on the tracking issue.
- `begin_investigation` is taken in the tick that allocates the investigation Work (or exhausts its budget), not as soon as a request is in `queued.idle`: a request waiting for a worker slot stays in `queued.idle`, where removing the intake label still withdraws it.
- The task that `request_patch` asks for is a patch, or a repair when feedback is pending for the Work to answer; `request_repair` always asks for a repair. A refreshed dossier that returns to the fork without a failing CI job or pending feedback is therefore answered by a patch.
- Every rejected or blocked outcome charges `no_progress`, whatever progress the worker reports, because `reject_patch_proposal` charges it unconditionally; a published or uncertain Result starts it again, as `BUDGETS` states, although no row lists that restart.
- The failing CI job of the head is the first executed blocking job of the evidence at `last_ci_evidence_id` that did not succeed, else the first job the evidence names as failing the runner-label check. Its name is the key of `budget.clusters`; existing keys are kept.
- A Work payload without `generation` and `dedupe_key` is generation 0, also when the record is in a later generation: its Result is then late. A payload that names only one of them matches no Work.
- While the record holds requested Work, or a Result held for a publish retry that is not exhausted, a changed fork head is not recorded; the table has no head row in an `agent_running` state.
- `exhaust_work_recovery` names `work_total` when both budgets are exhausted.
- In upstream review, `exhaust_repair_attempts` names the first exhausted counter of `no_progress`, `work_total`, and `repair_attempts_cluster`.
- A wait for a human is a state of kind `gate` or `locked`, or of kind `human_wait` with a recorded pending action (`compat.waits_for_human`). Only the rows the table lists for it leave it: observation reads nothing in a wait that has no observation row, a legacy write cannot change its phase or activity, and a Result that arrives there takes `record_late_effect`. A `waiting_human` activity without a pending action, a shape written before the table, is still handled by the legacy paths in development and upstream review; in provisioning it is rewritten by the provisioning migration.
- A planned operation that a trusted workflow runs stays `planned` across its launches; `begin_effect`, `record_response`, and `confirm_by_discovery` are taken together when its verified receipt is folded with the exact readback. The receipt establishes the facts of `begin_effect` (capacity available, the contribution not paused, the final check passed): the workflow's own final check read them before the effect. A push establishes "capacity available" without a runner reservation.
- A branch operation that the store holds as `applying` lost the response of its push: the branch observation takes `record_response` ("its response was lost") before discovering it, as the publisher hands an operation over only after it recorded the response.
- When the publisher's final check before a push does not pass (the head re-read fails or moved), no intent is recorded and the outcome carries no operation, instead of a `failed` operation, because `planned` has no row to `failed` other than a resolution.
- `active_operation_id` names an open operation only: the reference is released in the write that closes the operation, although no row of the table lists that clear (`record_merge` clears it explicitly).
- The counter of `operation_launch` is `external_ids.launch_attempts` of the operation, where launches were counted before the table. A launch is repeated only 300 seconds after the last one, and the relaunch requires available capacity, like the first launch. The launches of a development PR operation have the same limit.
- `require_provisioning_intervention` for exhausted launches is taken once the last allowed launch is 300 seconds old without a receipt, and not while the contribution is paused. Its pending action is `provisioning_launch_exhausted` with the counter `operation_launch`; after the provisioning operation failed (a human resolves it `failed`, or a verified receipt reports a definite failure), it is `provisioning_operation_failed` without a counter.
- A human `/upstreaming resume` in `provisioning.waiting_human` whose pending action is one the controller writes there (`provisioning_launch_exhausted` with the counter `operation_launch`, or `provisioning_operation_failed`) is the human's assertion that the prerequisite is corrected: the controller establishes "the prerequisite is corrected" for that command. Another pending action in that wait establishes nothing.
- After the resume of exhausted launches the same planned operation is launched again, from a launch count of zero, although the resume bumped the revision; the provisioning workflow accepts an operation of an earlier revision when the record names it as its active operation, and the receipt carries the record's revision.
- A verified provisioning receipt whose status is a string other than `provisioned` reports a definite failure of the effect; it is folded only for a `planned` or `applying` operation of a record in `provisioning.idle` (which fires `require_provisioning_intervention`) or in `provisioning.waiting_human` (which keeps the wait).
- The attempt that makes the operation of a publish-only retry its own is the attempt the retry was dispatched with (`UPSTREAMING_PUBLISH_ATTEMPT`); the first attempt adds nothing, so its id is unchanged. A CI bootstrap `dev_pr_update` planned again counts the earlier ones for the same head that the store holds.
- An operation the tick stages that the state publisher's check would refuse is an unexpected error of its contribution (step `operation_check`): the contribution is paused by the controller and the commit goes ahead for the others.
- A provisioning operation resolved `confirmed` by a human fires no row: `confirm_provisioning` needs the receipt and the readback. A receipt that arrives while the contribution waits for a human closes the operation and keeps the state.
- A record whose `last_ci_evidence_id` names evidence that the store does not hold, in a state where Work is allocated, is an unexpected error of that contribution and takes `pause_on_processing_error`; the pause names the missing evidence id.
- A verified dossier that decides close closes the contribution with `terminal_reason` `no_work` (shown as "not applicable"); the dossier's rationale is kept in the event.
- `park_investigation` records `previous_phase` `investigation` and keeps a recorded refresh; like every row that ends the generation it does not bump the revision. `parked_reason` is the dossier's rationale.
- An admission override applies against a decision park only when it names both a class and paths; one that names only one of them does not admit, and the Result takes `reject_dossier`, so the next investigation runs with the override still recorded, within `investigation_attempts`. The override's paths are the human's scope: the dossier's path coverage is recorded for them in `scope_coverage_complete` but does not refuse them. With a decision develop the dossier's class and paths are admitted and the override is cleared.
- "The verified dossier names `scope_paths`, and `scope_coverage_complete` holds" is established when the admitted scope has an admission class, names 1 to 32 safe scope paths, and matches every path of the dossier's `path_coverage`; a dossier without `path_coverage` has an incomplete scope. `admit_development` and `readmit_development` do not list `scope_coverage_complete`; the reconciler records it beside the row, as repair Work reads it.
- A worker outcome `proposal`, which no publisher produces any more, is folded as an outcome rejected in trusted validation.
- A Result of Work that the record holds but that its phase does not request (investigation Work outside `investigation`, patch Work outside `development`, revision Work outside `upstream_review`) is late, and so are a dossier outside `investigation`, any other Result in `investigation`, and the Result of a held publication outside `development` and `upstream_review`.
- The provisioning migration treats the retired `provision_fork` wait like the provisioning wait without a pending action: the fork is provisioned automatically from `provisioning.idle`. Development without a fork returns to `provisioning.idle`, and a proposal held on the provisioned fork is discarded for fresh patch Work in `development.idle`; both at a new revision.
- `submit_upstream` records `handoff_approval` with the CI evidence and the dossier the approval saw (`evidence_ids`), and the first debug cleanup attempt as `active_operation_id`.
- The handoff operations are launched without a row, like the development PR operations: within `operation_launch`, again after 300 seconds without a receipt, and not while the contribution is paused. Launches spent without a receipt are a definite infrastructure failure of the attempt: once the last allowed launch is 300 seconds old with no receipt, and the contribution is not paused, the controller closes the attempt as failed (`begin_effect` and `record_failure`, with the facts of a receipt, although no workflow reported its final check) and fires the retry row of the state or its exhaustion row. A receipt of that attempt that arrives later is not folded; the next attempt finds the effect already in place and reads it back. The receipt of an attempt the workflow refused is not folded either, so refusals count as launches without a receipt. The trusted workflow accepts an attempt of an earlier revision when the record names it as its active operation (a pause and its resume bump the revision).
- The counter of `debug_cleanup`, `internal_pr_closure`, and `submission_comment` ("the handoff operation's attempts") is the number of attempts in the current round, recorded on each attempt as `count:<k>`: a failed attempt with a count below `retries.infrastructure_per_head_failure` plus one takes the retry row, which the next attempt (count plus one) charges; at that count it takes the exhaustion row. After the resume of that exhaustion the next attempt counts from one. A pause does not start the count again.
- A cleanup that cannot produce an admissible clean head (a conflict once the debug commits are removed, a merge commit, nothing left, a validation problem) or whose exact-head lease conflicts (the branch is neither the approved head nor the clean head) is not charged to `debug_cleanup`: it takes `return_for_repair`, whatever the count.
- A cleanup without a debug commit writes nothing, and the CI evidence already recorded for that exact head is CI on the clean head: `accept_clean_head_ci` can be taken in the tick that confirms the clean head.
- `return_for_repair` from `handoff.cleaning_debug` and `handoff.waiting_ci` keeps `handoff_approval`, as its row clears only the clean head, `handoff_request`, and the pending action; the next `submit_upstream` replaces it.
- In the locked wait `handoff_request.command_id` names the submission package operation; the reminder links the comment that operation read back.
- "A compatible human author" of the upstream PR is a `User` account whose id is in `command_authority.actor_ids`; it need not be the human who requested the submission. A draft pull request is open.
- An observation of the locked wait that does not verify the upstream PR is recorded by `record_audit_observation` once per distinct observation (fork head, failing candidates, several matching candidates); every reminder after it states the mismatch the observation of that tick finds.
- The submission package's title is the subject of the one commit of the clean series, or `Build riscv64 wheels` for several commits; its description is the commit body (each commit with its body for a series), a `Testing` section from the CI evidence of the clean head, and the open issues of the upstream repository that the dossier's history records.
- Handoff receipts are read only from completed `handoff.yml` runs on `main` dispatched by `workflow_dispatch`, and only for the open operation the record runs.
- The workflow posts the submission package only while `riscv64` resolves to the clean head. A moved branch fails the attempt as an infrastructure failure ("posting the package failed"), the only failure row of `handoff.preparing_submission`; a package already posted is found by its marker and read back without that check.
- `/upstreaming retract-pr` takes the revision and the head only. The spec's form also carries `reason="..."`, but `retract_submission` reads no reason, so the grammar refuses it.
- The submission package names the revision of the record when it was posted in its `retract-pr` command; after a pause and its resume, the command of the latest tracking-issue entry carries the current revision, as the package states.
- Contribution rules of the dossier that mention a sign-off, a DCO, a CLA, or a contributor agreement state the signing policy: the package then shows no generic sign-off line.
- A mention in the proposed title or description (`@user` or `@org/team` outside code) is put in a code span, so the upstream pull request notifies nobody; the package says so.
- A handoff operation that a human resolves with `resolve-operation` fires no row: the confirm rows need the receipt and its readback, and the retry and exhaustion rows a reported failure. The record releases the operation and the controller plans the next attempt, which finds the cleanup, the closure, or the package already in place and reads it back.

## Code still outside the engine

Transitions not yet taken through `engine.py` are written by the compatibility layer in `compat.py`, which keeps a registry of every remaining write site and every legacy state still admitted. Every operation status change is a row of the operation table. `python -m engine` prints how many table verbs the pipeline takes through the engine, how many legacy write sites remain, and the operation verbs taken through `recovery.py`.
