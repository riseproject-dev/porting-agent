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

## CI failure and an actionable review in the same observation can drop the review

- **Where:** `handoff.awaiting_submission`, rows `return_for_repair` and `record_actionable_review`.
- **Today:** `return_for_repair` wins and repair Work is allocated in the same tick, so the review is not read. After the repair publishes, the review is bound to an earlier head and the collector filters it out, so no row records it.
- **Table:** the review is recorded once CI is observed again.
- **Required:** record the review evidence when the failure wins, so the repair Work carries it.

## The stop flag in states the table does not have

- **Where:** `compat.legacy_pause` and `compat.apply_command`, for a record in a state of `compat.LEGACY_STATES` (`upstream_review.idle`, `upstream_review.waiting_capacity`).
- **Today:** such a state has no row, so the engine cannot take `pause`, `pause_on_identity_change`, or `pause_on_processing_error` there. `compat.legacy_pause` records the same fields and clears the same things, but the activity that held the Work or a publication becomes `idle` instead of the table's state in which the Work is due. `resume` and `park` on such a record are applied by the legacy command code. Its park is refused while an operation is open, as the table's. `refresh` and `abandon`, which no row of such a state takes, are refused there.
- **Table:** the flag verbs apply in every non-terminal state, with `set_moves`; `park_contribution` requires no open operation.
- **Required:** take the upstream review rows, then remove the legacy branches.

## No dossier decides close

- **Where:** `close_investigation` from `investigation.agent_running`; the dossier contract (`schemas/dossier.schema.json`, `dossier_gate.py`, the investigator's instructions).
- **Today:** the reconciler takes `close_investigation` for a verified dossier whose decision is close, with `terminal_reason` `no_work`. But the dossier schema and the investigator's instructions admit the decisions develop and park only, so the research gate refuses a dossier that decides close before it reaches the reconciler, and no contribution is closed by its dossier.
- **Table:** a verified dossier decision close leads to `closed.idle` and sets `terminal_reason`.
- **Required:** a decision on when an investigation decides close (for example riscv64 wheels already published for the latest release), then the decision in the dossier contract and the investigator's instructions.

## CI is never rerun

- **Where:** `development.agent_running` (`request_ci_rerun`), `development.waiting_ci` (`rerun_interrupted_ci`, `exhaust_interrupted_reruns`), and `resume_ci` in `development.waiting_human` and `handoff.waiting_human` (CI on the clean head in `handoff.waiting_ci` is a separate entry below).
- **Today:** no trusted workflow reruns the failed or interrupted jobs of a run on the exact head and attempt. The repair worker's `no_change` report takes `request_ci_rerun` when the Work carries no feedback and `flaky_reruns` remains, and charges `flaky_reruns`, but nothing is rerun. `development.waiting_ci` acts only on CI evidence of a run or attempt other than the one the classified Result answered (`budget.reruns.answered_ci_evidence_id`): a failure of another run or attempt takes `record_ci_failure`, and a passing run is nominated once `nominate_for_handoff` holds. Without a trusted rerun, nothing produces newer evidence unless a human reruns the CI or pushes to the development branch, so the contribution waits in `development.waiting_ci`. That state is of kind `auto_wait`, not a human wait: it records no pending action, so no reminder or escalation runs. So the tracking issue receives no entry for `request_ci_rerun` either: the wait is shown only in the event log of the status page, and the tick report names the answered run on every tick. A required job that was cancelled or lost its runner leaves CI on the head not executed: the contribution stays in `development.waiting_ci` and CI is read again, with no rerun and no exhaustion, so no record enters a wait with the pending action `ci_interrupted`, and `resume_ci` leads to the CI wait without launching the rerun the row states.
- **Table:** `request_ci_rerun` reruns the failed jobs and leads to `development.waiting_ci`, charging `flaky_reruns`; the collector reruns only cancelled or runner-lost jobs within `interrupted_reruns`, and `exhaust_interrupted_reruns` waits for a human; `resume_ci` reruns them.
- **Required:** a trusted rerun of the jobs of the exact run and attempt (an operation with a receipt and readback of the new attempt), then take the rows. Until then, a table decision on whether the wait after `request_ci_rerun` needs a pending action and its reminder.

## Pending feedback after a head change while Work is due

- **Where:** `development.fix_needed`, row `record_head_change`; `nominate_for_handoff` in `development.waiting_ci`.
- **Today:** a push to `riscv64-dev` while development Work is due takes `record_head_change` to `development.waiting_ci` and keeps `pending_feedback_evidence_ids`, as the row states. When Work was due for actionable review feedback, that feedback stays pending: a failure on the new head takes `record_ci_failure` and the repair Work carries the feedback, but passing CI cannot take `nominate_for_handoff` ("no pending actionable feedback"), and `development.waiting_ci` requests no Work. The contribution then waits in `development.waiting_ci` until another push, a new review, `/upstreaming refresh`, or `/upstreaming park`; the tick report states the deferred nomination, and no reminder runs.
- **Table:** `record_head_change` from `development.fix_needed` sets the head and the rerun counts and clears `last_ci_evidence_id`; it keeps the pending feedback, and no row of `development.waiting_ci` requests Work for it.
- **Required:** a table decision on the pending feedback a head change carries into `development.waiting_ci` (for example clear it with the head change, or return to `development.fix_needed` when CI passes with feedback pending).

## An uncertain publication is recorded before it is confirmed

- **Where:** `record_publish_success` in `reconcile.fold_patch_result`.
- **Today:** a publication whose push response was lost (`uncertain`) takes `record_publish_success` when its Result is folded; the branch operation is discovered afterwards by the branch observation, and the head it pushed is recorded by `record_head_change`.
- **Table:** the guard is "the Result is published, or uncertain and later confirmed".
- **Required:** keep the Result in `development.agent_running` until discovery confirms the branch operation, without the Work recovery taking the completed Work graph for a lost Result.

## CI on the clean head is not rerun

- **Where:** `handoff.waiting_ci`, rows `rerun_interrupted_ci` and `exhaust_interrupted_reruns`; the CI observation in `observe_ci.py`.
- **Today:** the observation takes `accept_clean_head_ci` or `return_for_repair`. A required job that was cancelled or lost its runner is a job that did not execute: nothing reruns it, so `interrupted_reruns` is never charged and its exhaustion wait is never entered; the contribution stays in `handoff.waiting_ci` until the bounded wait of `require_clean_head_ci` asks a human. `pause` and `park` still apply there.
- **Table:** the collector reruns the interrupted jobs on the exact head within `interrupted_reruns` and waits for a human with `ci_interrupted` once it is exhausted.
- **Required:** a trusted rerun of the interrupted jobs on the exact head and attempt, then take the two rows.

## The failing evidence of the clean head is not recorded

- **Where:** `handoff.waiting_ci`, row `return_for_repair`; the CI observation in `observe_ci.py`.
- **Today:** a blocking CI failure on a clean head that the cleanup rewrote stores the failing evidence and takes `return_for_repair`, which sets nothing. The record keeps `last_ci_evidence_id` of the candidate head, whose CI passed, so in `development.fix_needed` the repair is requested against passing evidence of a head that is no longer the fork head: it names no failing job, and the clean head's failure is in the event details only.
- **Table:** `return_for_repair` from `handoff.waiting_ci` bumps the revision and clears the clean head, `handoff_request`, and the pending action; it sets no evidence. `return_for_repair` from `handoff.awaiting_submission` records the failing evidence.
- **Required:** a table decision that `return_for_repair` from `handoff.waiting_ci` sets `last_ci_evidence_id` to the failing evidence of the clean head, then set it in the observation.

## A moved branch before the package is posted waits for a human

- **Where:** `handoff.preparing_submission`; `handoff.post_package`.
- **Today:** the workflow posts the package only while `riscv64` still resolves to the clean head. When the branch moved, the attempt fails as an infrastructure failure: `retry_submission_comment` within the budget, then `exhaust_comment_retries` to `handoff.waiting_human`, whose only exit is the resume to the same step. Nothing returns the contribution to development, so a human restores the branch or abandons.
- **Table:** this state has no row for a changed `riscv64` head; the decision that a changed head before the package is posted returns to development is taken by `return_for_repair` in `handoff.building_upstream_branch` and `handoff.waiting_ci` only.
- **Required:** a table decision for a changed head in `handoff.preparing_submission` (for example `return_for_repair`), then take it from the attempt's failure.

## Dossier contribution-policy items are not enforced before nomination

- **Where:** `nominate_for_handoff` from development CI in `observe_ci.py`.
- **Today:** the contributing instructions and reviewer expectations that need a change to the series (for example "add a CHANGELOG entry") are checked at `submit-upstream`, by the submission rewrite and its review, which fail with them as reasons and return the contribution to development; the worker is told to follow them, and its gate checks only the measurable commit rules. Nothing checks them before the contribution is nominated for handoff, so the gate can ask for `submit-upstream` for a series the rewrite or review then sends back. The live contribution `closeio--ciso8601/1` is in this case: its dossier asks for a CHANGELOG entry and its series does not change `CHANGELOG.md`.
- **Table:** `nominate_for_handoff` requires every required CI job to pass on the exact head; it states no contribution-policy check.
- **Required:** a table decision on whether unmet contribution-policy items block nomination or are requested as development Work, then the check from the dossier.

## A pause or retraction after the final check is not seen by the effect

- **Where:** `handoff.yml`; `python -m handoff check` and the effects in `handoff.build_upstream_branch` and `handoff.post_package`.
- **Today:** each job checks that its checkout is the head of `main` and that the state still runs the operation before it mints its token; the effect then runs `load_request` again, but on the same checkout. A pause, a retraction, or any other change committed to `main` after that check is not seen, so the effect can still run once. The effects check the GitHub side right before they write (the exact-head lease on `riscv64` and the open upstream PRs from it, the branch at the clean head before the package is posted), which keeps the window narrow.
- **Table:** `pause` stops new Work and writer admission; `retract_submission` requires quiescence (no open operation).
- **Required:** a check of the head of `main` immediately before each effect, or a decision that the remaining window is acceptable.

## A slow healthy handoff run can be closed as spent

- **Where:** the handoff launches in `controller.py`; `LAUNCH_INTERVAL_SECONDS` and `handoff.fail_attempt`.
- **Today:** an attempt is launched again after 300 seconds without a receipt and closed as failed once its last allowed launch is 300 seconds old. A `handoff.yml` run may take up to 20 minutes (its job timeouts) and waits in the shared concurrency group `porting-fork-writer` behind other runs, so a slow but healthy run can be closed as spent while it still runs. The effects are idempotent (the lease write, the close of the exact PR, the package found by its marker), so the next attempt finds the effect in place and reads it back, and the late receipt is not folded; the cost is a charged attempt.
- **Table:** `operation_launch` counts launches; it states no interval tied to the run timeout or the queueing of the workflow.
- **Required:** an interval at least as long as the workflow's timeout plus its queueing, or a check of the run's status before an attempt is closed as spent.

## Worker outcomes in upstream review are folded by legacy code

- **Where:** `reconcile._fold_revision_outcome` and the publish-only retry in `reconcile.py`, for Results of upstream revision Work (from `upstream_review.agent_running`).
- **Today:**
  - published or uncertain: `upstream_review.waiting_ci`, `no_progress` started again, the carried feedback kept (table: `record_revision_publish_success`, which clears exactly the carried feedback);
  - `no_change`: the legacy `upstream_review.idle`, which the next CI observation moves to `upstream_review.waiting_ci` with no pending action (table: `require_reviewer_answer` to `upstream_review.waiting_human` with the pending action `answer_reviewer`);
  - `proposal`, which no publisher produces any more: rejected in trusted validation, as below;
  - `infra_error` within the retry limit: the legacy `upstream_review.waiting_capacity` with the pending publication, which the next CI observation moves to `upstream_review.waiting_ci` while the publish-only retry is still scheduled (table: `schedule_publish_retry`, which keeps `upstream_review.agent_running`); beyond the limit, `upstream_review.waiting_human` with the pending action `publication_infrastructure_failure` and no `counter` (table: `exhaust_publish_recovery`, whose pending action names `publish_recovery`);
  - rejected for validation, or blocked: `upstream_review.fix_needed` with a revision bump and `no_progress` charged, as `reject_patch_proposal` does; a blocked outcome also records its rehearsal evidence as pending feedback;
  - rejected as stale: the legacy `upstream_review.idle` without a revision bump, then `upstream_review.waiting_ci` (the table has no row for it).
- **Table:** in upstream review `record_revision_publish_success`, `reject_patch_proposal`, `require_reviewer_answer`, `schedule_publish_retry`, `exhaust_publish_recovery`.
- **Required:** take the publication outcome through the table's rows.

## A completed Work graph whose Result is not collected yet is retried

- **Where:** `reconcile.dispatch_recovery`, `allocate_work_retry` and `exhaust_work_recovery`.
- **Today:** the reconciler establishes the fact "the Work graph is terminal without a usable Result" for a graph the queue reports `completed` or `cancelled` while the record still holds its Work. A graph completed by a worker whose publication artifact the tick has not collected yet is therefore retried at a new revision, and the Result that arrives later takes `record_late_effect`.
- **Table:** `allocate_work_retry` applies to terminal Work without a usable Result.
- **Required:** establish the fact only once the Result of a completed graph is known to be missing (its worker run finished without a publication artifact).

## Development PR operations have no row

- **Where:** the development PR and CI bootstrap plans in `controller.py` and the development PR receipt in `reconcile.py`.
- **Today:** the operations `dev_pr_create` and `dev_pr_update` follow the operation table, but no lifecycle row starts them or is fired by them. They are planned in `development.waiting_ci`, which they leave unchanged; while one is open, nomination is deferred and no Work is allocated. Its verified receipt records `fork.pr_number` and releases the operation, without a row. Launches that never return a receipt stop at the launch limit with the operation open and no human wait.
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
- The flag migration turns a block's human wait without a pending action into the `idle` activity of its phase, as the old `resume` did (for `development` the state the development migration names: `development.waiting_ci` or `development.fix_needed`), records `paused_at` as the record's `updated_at`, and records the controller's identity block with `paused_reason` `upstream_identity_changed`, the reason the table names.
- `pause_on_identity_change` is guarded by `not paused`: an identity change observed while a contribution is paused sets nothing; the controller still selects no Work for it while the identity differs.
- The reminder interval is counted in whole days from the exact time of the request or of the last reminder; a day counts when it ends on a weekday (UTC). Any pending action in a human wait of the table gets reminders, including the pending actions that only the legacy paths of upstream review write.
- A reminder is suppressed while paused and counted from the request, so after a long pause the resume and an overdue reminder can be posted in the same tick.
- `/upstreaming resume` on a contribution that is neither paused nor in a human wait or in `parked.idle` is answered with `not paused`.
- `/upstreaming resume` from `parked.idle` records `admission_override` only when it names both `class` and `paths`; the override's reason is the resume's reason. A resume that names only one of them is refused when it is posted and changes nothing, because such an override could never admit development. Every other resume refuses `class` and `paths`.
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
- A wait for a human is a state of kind `gate` or `locked`, or of kind `human_wait` with a recorded pending action (`compat.waits_for_human`). Only the rows the table lists for it leave it: observation reads nothing in a wait that has no observation row, a legacy write cannot change its phase or activity, and a Result that arrives there takes `record_late_effect`. A `waiting_human` activity without a pending action, a shape written before the table, is still handled by the legacy paths in upstream review; in provisioning and development it is rewritten by the record migrations.
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
- An admission override applies against a decision park only when it names both a class and paths; one that names only one of them (recorded before such a resume was refused) does not admit, and the Result takes `reject_dossier`, so the next investigation runs with the override still recorded, within `investigation_attempts`. The override's paths are the human's scope: the dossier's path coverage is recorded for them in `scope_coverage_complete` but does not refuse them. With a decision develop the dossier's class and paths are admitted and the override is cleared.
- "The verified dossier names `scope_paths`, and `scope_coverage_complete` holds" is established when the admitted scope has an admission class, names 1 to 32 safe scope paths, and matches every path of the dossier's `path_coverage`; a dossier without `path_coverage` has an incomplete scope. `admit_development` and `readmit_development` do not list `scope_coverage_complete`; the reconciler records it beside the row, as repair Work reads it.
- A worker outcome `proposal`, which no publisher produces any more, is folded as an outcome rejected in trusted validation.
- A Result of Work that the record holds but that its phase does not request (investigation Work outside `investigation`, patch Work outside `development`, revision Work outside `upstream_review`) is late, and so are a dossier outside `investigation`, any other Result in `investigation`, and the Result of a held publication outside `development` and `upstream_review`.
- The provisioning migration treats the retired `provision_fork` wait like the provisioning wait without a pending action: the fork is provisioned automatically from `provisioning.idle`. Development without a fork returns to `provisioning.idle`, and a proposal held on the provisioned fork is discarded, and development continues in the state the development migration names; both at a new revision.
- The development migration names `development.waiting_ci` for a record without Work or a held publication when `last_ci_evidence_id` is set or the fork head differs from the provisioned base ("evidence or a published head"), and `development.fix_needed` otherwise; the evidence a legacy record names is not read (a legacy review evidence id there counts as evidence). The Work deadline's `task_without_outcome` becomes `work_failed` with no counter, so the resume starts no counter again.
- A held publication whose `publish_recovery` is exhausted (`pending_publication.exhausted`) launches nothing more and is not a pending publication for the quiescence guard ("no pending publication"), so `resume_development` and `resume_stale` apply to the wait `exhaust_publish_recovery` entered, and clear it.
- The Result of a publish-only retry ends the held publication: `pending_publication` is cleared beside the row that folds it (`record_publish_success`, `reject_patch_proposal`, `replan_development`, `request_ci_rerun`), although none of those rows lists that clear.
- `record_publish_success` adds the published files to `scope_paths` beside the row, as a repair backed by review feedback may change a path the dossier missed.
- The worker's `no_change` report is "a failure not caused by the patch" for `request_ci_rerun`: it takes the row when the Work carries no feedback (`active_work.feedback_evidence_ids`; the Work's `reason_ids` are not feedback) and `flaky_reruns` remains for the head, and counts as `reject_patch_proposal` otherwise. While no trusted rerun exists, `request_ci_rerun` leads to `development.waiting_ci`, where only CI evidence of another run or attempt than the one the Result answered is acted on: beside the row, the evidence the row clears from `last_ci_evidence_id` is kept in `budget.reruns.answered_ci_evidence_id` (none when the Work was allocated without CI evidence), and a new head starts the rerun counts, and this field, again. The row keeps the revision, so the Work id names the head's `flaky_reruns` count from the first rerun on, and the repair Work requested after a rerun is never the Work whose Result requested it. A blocked Result's rehearsal evidence is stored and named in the event, and is not recorded as pending feedback, since no row sets it; a rehearsal id that the code before the table recorded as pending feedback still holds `nominate_for_handoff`.
- `publish_recovery` counts the failed publications of the held Result (`pending_publication.attempts`), each charged by `schedule_publish_retry`, so a Result is published at most `retries.infrastructure_per_head_failure` plus two times before `exhaust_publish_recovery`; the launch of a scheduled retry charges nothing.
- A launched publish-only retry whose Result is not folded within 60 minutes of the launch (`pending_publication.launched_at`) is "publication failed with an infrastructure error": `schedule_publish_retry` charges `publish_recovery`, or `exhaust_publish_recovery` waits for a human, as for a lost dispatch. A Result of that retry that arrives later, while the Result is still held and the contribution does not wait for a human, matches the held publication and is folded by its row; in the wait it takes `record_late_effect`. An infrastructure error such a Result reports was already charged by the wait: it takes `record_late_effect` and charges nothing again. A launch is charged once its Result or its wait charges it, so a held Result's `infra_error` is charged already when its attempt (`UPSTREAMING_PUBLISH_ATTEMPT`) is not above `pending_publication.attempts`, or, without an attempt, when no retry is launched.
- The provisioning migration treats `development.waiting_human` without a pending action that holds a proposal never published, with the publication pending for it, like `development.waiting_capacity` holding one: the proposal and the publication are cleared, and the record continues at a new revision in the state the development migration names (`development.waiting_ci` with evidence or a published head, `development.fix_needed` otherwise).
- Work that an allocation row names under a graph that returned a Result in the same tick or is completed or cancelled in the queue is "the Work graph is terminal without a usable Result": the controller takes the allocation row and then `allocate_work_retry` or `exhaust_work_recovery` in the same tick, so the allocation charges `work_total` and the fresh graph charges `work_recovery` and `work_total` again, and the spent graph is never submitted.
- `dispatch_recovery` counts the dispatcher runs that ended since the Work was requested; each ended run is decided once (`retry_dispatch` or `exhaust_dispatch_recovery`), and `active_work.dispatched_run_id` names the last decided run. A dispatcher launch that never shows up as a run is launched again after 15 minutes without a row, since no run ended.
- In `development.waiting_ci` an actionable review observed with a CI failure in the same tick is recorded (`record_actionable_review`); the failure is not, since `development.fix_needed` reads no CI (its only observation row is `record_head_change`), and the review would be filtered out once the repair publishes a new head. The repair Work carries the review but no failing CI job.
- "An executed blocking failure on the exact head" (`record_ci_failure`, `return_for_repair`) is established by a CI verdict `failed`, which also covers a required job that ran on a runner without the contract's runner label.
- "The observed development PR does not match the recorded fork, ref, or PR" holds when GitHub has no such PR, or its number, head repository, head ref, or base ref differs from the record's; a closed development PR still matches.
- `nominate_for_handoff` and Work allocation wait while a development PR or CI bootstrap operation is open, although the nomination row has no operation guard.
- A Result of Work the record holds in a state of the table without a row that folds a Result (for example Work held in `development.fix_needed`) is late.
- `submit_upstream` records `handoff_approval` with the CI evidence and the dossier the approval saw (`evidence_ids`); it records no operation. The review Work is allocated in `handoff.review_due` by `request_submission_review`, in the same tick when a worker slot is free.
- The submission rewrite reshapes the commits of the approved series "only if CONTRIBUTING or reviewers require it" for a squash or a split; that requirement is the model's judgment, checked by the separate review Work, and trusted code checks that every commit is valid upstream and holds only content of the base or of the approved series, that the final tree is unchanged, and that the measurable rules, including `max_commits`, hold.
- "The same final tree and no content the approved series does not have" is read as: the tree of every reviewed commit is the tree of a commit of the approved series without its debug commits, later in that series than the tree of the reviewed commit before it. Squashing and rewording are admitted; reordering and content the approved series removed are refused. A split is therefore refused unless every tree it produces is a tree of that series, so a split the project requires sends the submission back to development, where the split commits can be made and approved first.
- The revision is bumped when the rewrite is accepted (`accept_submission_rewrite`), since the review is new Work of the next revision and the revision rule requires a bump on a Result that leads to a state where Work is due; the package names the approval's revision, and the `retract-pr` command it carries names the current one.
- The required CI workflow the build of `riscv64` dispatches on the clean head is the workflow of the latest required CI run on the candidate head (`workflow_path` of its evidence), read at the clean head as data: dispatchable when its triggers include `workflow_dispatch` and none of its inputs is required without a default. A project whose required jobs span several workflows gets that one dispatched; the others run only if a push triggers them, and the bounded wait of 12 hours then asks a human (`require_clean_head_ci`).
- In upstream review the recorded head is `upstream_pr.head_sha` (`lifecycle.recorded_head`), the field the table's `record_head_change` there updates; `fork.head_sha` keeps the approved head of `riscv64-dev`. A head change in upstream review is still recorded by the legacy head observation, which updates both.
- A rewrite is expired once its evidence is one day short of the retention of the publication artifact that holds the series (`handoff.rewrite_expired`), 29 days by default: the artifact is kept 30 days, or the days of the repository variable `GH_AW_DEFAULT_ARTIFACT_RETENTION_DAYS` when it is set (1 to 400; 0 or another value counts as 30), which the controller's workflow passes to the tick; for a retention of one day the margin is half a day. The day of margin covers a review or build that starts just before the expiry. The trusted workflow also refuses an artifact GitHub reports as expired; that attempt fails as an infrastructure failure, and the next plan finds the rewrite expired by its age. A repository or organization retention limit below that value shortens the artifact's life without the tick knowing: the build attempts then fail as infrastructure failures until the rewrite is old enough or the attempts are exhausted and a human is asked.
- A dossier of schema version 1 records no measurable rules and no template sections: the submission check then applies the rules and sections the review reports having found in the project. They are untrusted but can only refuse a submission, never admit one.
- A required template section is present when the description has a Markdown heading with the same words (case and emphasis ignored). Whether every checklist item is answered honestly is the review's judgment; the check does not parse checklists.
- CI is a function of the tree: when the reviewed series has the tree of the candidate head, the CI evidence of the candidate head on `riscv64-dev` is CI on the clean head, and `accept_clean_head_ci` records it.
- "An upstream PR is open from `riscv64`" is any open pull request of the upstream repository whose head is `riscv64` of the fork owner, whoever opened it. It is checked only when `riscv64` must be written; a `riscv64` that already resolves to the reviewed head is read back without a write and without that check.
- A failed review, a failed check, or an unusable review Result never posts anything and never writes `riscv64`. `reject_submission_review` keeps `handoff_approval`, like `return_for_repair`; the next `submit_upstream` replaces it.
- The worker gate reads the commit messages of a proposed series from its mbox as `git am` would (the subject without its `[PATCH]` prefix, the body up to the first `---` line); the submission check reads them from the rebuilt commits.
- The handoff operations are launched without a row, like the development PR operations: within `operation_launch`, again after 300 seconds without a receipt, and not while the contribution is paused. Launches spent without a receipt are a definite infrastructure failure of the attempt: once the last allowed launch is 300 seconds old with no receipt, and the contribution is not paused, the controller closes the attempt as failed (`begin_effect` and `record_failure`, with the facts of a receipt, although no workflow reported its final check) and fires the retry row of the state or its exhaustion row. A receipt of that attempt that arrives later is not folded; the next attempt finds the effect already in place and reads it back. The receipt of an attempt the workflow refused is not folded either, so refusals count as launches without a receipt. The trusted workflow accepts an attempt of an earlier revision when the record names it as its active operation (a pause and its resume bump the revision).
- The counter of `upstream_branch` and `submission_comment` ("the handoff operation's attempts") is the number of attempts in the current round, recorded on each attempt as `count:<k>`: a failed attempt with a count below `retries.infrastructure_per_head_failure` plus one takes the retry row, which the next attempt (count plus one) charges; at that count it takes the exhaustion row. After the resume of that exhaustion the next attempt counts from one. A pause does not start the count again.
- A build of `riscv64` whose reviewed series does not rebuild to the reviewed head, or whose lease on `riscv64` conflicts (the branch moved between the read and the push), is not charged to `upstream_branch`: it takes `return_for_repair`, whatever the count; one that finds an upstream PR open from `riscv64` takes `require_branch_intervention`.
- When the reviewed series has the tree of the candidate head, `accept_clean_head_ci` can be taken in the tick that confirms the clean head, from the CI evidence already recorded for the candidate head.
- `return_for_repair` from `handoff.building_upstream_branch` and `handoff.waiting_ci` keeps `handoff_approval`, as its row clears only the clean head, the review, `handoff_request`, and the pending action; the next `submit_upstream` replaces it.
- In the locked wait `handoff_request.command_id` names the submission package operation; the reminder links the comment that operation read back.
- "A compatible human author" of the upstream PR is a `User` account whose id is in `command_authority.actor_ids`; it need not be the human who requested the submission. A draft pull request is open.
- An observation of the locked wait that does not verify the upstream PR is recorded by `record_audit_observation` once per distinct observation (fork head, failing candidates, several matching candidates); every reminder after it states the mismatch the observation of that tick finds.
- The submission package's title and description are the reviewed ones; a reviewed title that is empty once cleaned is `Build riscv64 wheels`. The package states in the comment, not in the description, how many required CI jobs passed and on which head.
- Handoff receipts are read only from completed `handoff.yml` runs on `main` dispatched by `workflow_dispatch`, and only for the open operation the record runs.
- The workflow posts the submission package only while `riscv64` resolves to the clean head. A moved branch fails the attempt as an infrastructure failure ("posting the package failed"), the only failure row of `handoff.preparing_submission`; a package already posted is found by its marker and read back without that check.
- `/upstreaming retract-pr` takes the revision and the head only. The spec's form also carries `reason="..."`, but `retract_submission` reads no reason, so the grammar refuses it.
- The submission package names the revision of the record when it was posted in its `retract-pr` command; after a pause and its resume, the command of the latest tracking-issue entry carries the current revision, as the package states.
- The submission package asks for no change to the series: a change the contribution policy needs (a CHANGELOG entry, a commit sign-off) withdraws the package with `retract-pr`, and only steps outside the series (such as signing a CLA) are the human's before submitting.
- In the proposed title and description every at sign that could start a mention gets a word joiner (U+2060) after it, whatever the Markdown around it, so the upstream pull request notifies nobody; the package says so.
- A dossier stored before the contribution guidelines were part of the contract (schema version 1, such as the dossier of `closeio--ciso8601/1`) stays the verified dossier of its record: it records no measurable rules, so the worker gate checks no commit rule for it. A new investigation, a refresh included, must record them, and the gate refuses a dossier of version 1 from any investigation run after the change, also one recovered from an earlier run.
- A handoff operation that a human resolves with `resolve-operation` fires no row: the confirm rows need the receipt and its readback, and the retry and exhaustion rows a reported failure. The record releases the operation and the controller plans the next attempt, which finds `riscv64` or the package already in place and reads it back.

## Code still outside the engine

Transitions not yet taken through `engine.py` are written by the compatibility layer in `compat.py`, which keeps a registry of every remaining write site and every legacy state still admitted. Every operation status change is a row of the operation table. `python -m engine` prints how many table verbs the pipeline takes through the engine, how many legacy write sites remain, and the operation verbs taken through `recovery.py`.
