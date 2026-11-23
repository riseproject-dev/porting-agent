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

## Submission is requested but not yet accepted

- **Where:** `handoff.awaiting_submission`, verb `submit_upstream`.
- **Today:** the tracking issue asks an authorized human to post `/upstreaming submit-upstream revision=N head=<head>`. The controller validates the command against the table, then refuses it and changes nothing.
- **Table:** `submit_upstream` leads to `handoff.cleaning_debug`, followed by clean-head CI, closing the development PR, and posting the submission package with a prefilled compare link. None of these operations runs yet, so accepting the command would leave the contribution in a state nothing advances.
- **Required:** implement the handoff operations, then accept the command.

## Retraction is checked but not accepted

- **Where:** `handoff.waiting_upstream_pr`, verb `retract_submission`.
- **Today:** `/upstreaming retract-pr revision=N head=<clean head>` is checked against the table (exact revision, exact clean head, quiescent) and then refused without change, like `submit-upstream`. Nothing reaches the state while `submit-upstream` is held.
- **Table:** `retract_submission` leads to `development.fix_needed`, stops the reminders, and unlocks branch work.
- **Required:** accept the command together with the handoff operations.

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
- `resume_development` keeping a held proposal or pending publication.

**Required:** add a check for each.

## Legacy stop paths do not clear everything the table clears

- **Dossier park** (`park_investigation`, legacy site in `reconcile.py`) does not clear a held proposal or pending publication. It is inert today because nothing is retried while stopped and resume from park starts a new generation.
- **Required:** take the dossier park through the engine.

## The stop flag in states the table does not have

- **Where:** `compat.legacy_pause` and `compat.apply_command`, for a record in a state of `compat.LEGACY_STATES` (for example `development.idle`, `development.waiting_capacity`, `upstream_review.idle`).
- **Today:** such a state has no row, so the engine cannot take `pause`, `pause_on_identity_change`, or `pause_on_processing_error` there. `compat.legacy_pause` records the same fields and clears the same things, but the activity that held the Work or a publication becomes `idle` instead of the table's state in which the Work is due. `resume` and `park` on such a record are applied by the legacy command code: its resume has no `resume_stale` (a long stop is not followed by a refresh). Its park is refused while an operation is open, as the table's. `refresh` and `abandon`, which no row of such a state takes, are refused there.
- **Table:** the flag verbs apply in every non-terminal state, with `set_moves`; `resume_stale` applies after a long stop in provisioning and development; `park_contribution` requires no open operation.
- **Required:** migrate the legacy states to table states (investigation, provisioning, and development rows), then remove the legacy branches.

## Some resumes lead where nothing continues yet

- **`resume_provisioning`** requires the fact that the prerequisite is corrected, which no caller establishes yet: `/upstreaming resume` in `provisioning.waiting_human` is refused unless the wait is long enough for `resume_stale`. The retired `provision` command no longer leaves the wait; `park` and a resume from `parked.idle` do. After `require_provisioning_intervention` for exhausted launches the planned operation is still open, so `park` and `resume_stale` are refused (naming the operation) until `/upstreaming resolve-operation` closes it; the reply of the retired `provision` does not say so.
- **`resume_ci`** in `development.waiting_human` and `handoff.waiting_human` leads to the CI wait, but the rerun of the interrupted jobs that the row states is not launched. No record enters a wait with the pending action `ci_interrupted` yet.
- **Required:** the provisioning prerequisite check and collector reruns.

## The admission override is recorded but not applied

- **Where:** `restart_generation` from `parked.idle`, the dossier fold in `reconcile.py` (`admit_development`, `park_investigation`), and the deprecated `/upstreaming force-develop` in `compat.apply_command`.
- **Today:** `/upstreaming resume` from `parked.idle` records `class`, `paths`, and the reason as `admission_override`, but the legacy dossier fold ignores it: a dossier that decides park parks the contribution again, and a dossier that decides develop does not clear the override. `force-develop`, which the decisions retire, is kept as the override that takes effect: from `parked.idle` it moves the record to `provisioning.idle` with the command's class and paths, without a row of the table. Its tracking-issue entry and its rejection name the replacement.
- **Table:** `admit_development` accepts a verified dossier decision develop, or decision park with a recorded `admission_override`, and clears the override; `park_investigation` requires no override; the grammar has no `force-develop`.
- **Required:** take the dossier fold through `admit_development`, `readmit_development`, and `park_investigation`, then retire `force-develop`.

## A dossier decision close does not close the contribution

- **Where:** the dossier fold in `reconcile.py`, verb `close_investigation`.
- **Today:** a dossier whose decision is close is treated as a stale contract: investigation is due again at a new revision, and no budget is charged. (A Result without a dossier, or with a decision that is none of develop, park, and close, takes `reject_dossier` or `exhaust_investigation_recovery`.)
- **Table:** a verified dossier decision close leads to `closed.idle` and sets `terminal_reason`.
- **Required:** take the dossier fold through the table's verbs.

## The locked-wait reminder lacks the package link and exact head

- **Where:** `record_reminder` in `handoff.waiting_upstream_pr`, `status_ledger.py`.
- **Today:** the generic reminder text is used.
- **Table:** the reminder in that state carries the package link and the exact head, and states when the observed upstream PR does not match.
- **Required:** render the reminder from the posted package once the handoff operations exist. Nothing reaches the state while `submit-upstream` is held.

## A legacy pending action in upstream review has no resume

- **Where:** `upstream_review.waiting_human` with `pending_human_action` `task_without_outcome`, written by the legacy Work deadline in `reconcile.py`.
- **Today:** no resume row's guard admits that pending action, and `restart_generation` is refused while the upstream PR is open, so the only exit is `park` then `abandon`. Nothing reaches the state while `submit-upstream` is held.
- **Table:** Work that ends without a Result takes `allocate_work_retry` within its budget and `exhaust_work_recovery` into the human wait with a pending action that `resume_upstream_revision` admits.
- **Required:** replace the legacy Work deadline by the table's Work recovery verbs before upstream review becomes reachable.

## The first patch in `development.idle` is selected outside the table

- **Where:** `controller._legacy_patch`, for a record in `development.idle` without CI evidence (the legacy provisioning confirmation, the dossier fold without destination writes, and the legacy `no_change` and stale outcomes leave records there).
- **Today:** the controller requests `development_patch` itself. Like `request_patch`, the Work carries the pending feedback and the allocation charges `work_total`, and no Work is requested while `work_total` or `no_progress` is exhausted; but the record then stays in `development.idle`, deferred, with no exhaustion row and no human wait.
- **Table:** patch Work is requested only from `development.fix_needed`, where an exhausted budget takes `exhaust_work_total`, `refresh_on_exhaustion`, or `exhaust_repair_attempts`.
- **Required:** migrate `development.idle` to the table's development states.

## Worker outcomes in development and upstream review are folded by legacy code

- **Where:** `reconcile.apply_publication`, for Results of development and upstream revision Work.
- **Today, development:** a rejected or blocked outcome charges `no_progress` and nothing else, bumps the revision, and leads to `fix_needed`, as `reject_patch_proposal` does, but it is written by the legacy outcome: a blocked outcome also records its rehearsal evidence as pending feedback and clears `proposal_artifact`, a rejected one records its artifact in `proposal_artifact`. A stale rejection leads to the legacy `development.idle` without a revision bump; `no_change` leads to `development.idle`; a published or uncertain Result leads to `waiting_ci` and starts `no_progress` again, without clearing the feedback its Work carried.
- **Today, upstream review** (from `upstream_review.agent_running`; nothing reaches it while `submit-upstream` is held):
  - published or uncertain: `upstream_review.waiting_ci`, `no_progress` started again, the carried feedback kept (table: `record_revision_publish_success`, which clears exactly the carried feedback);
  - `no_change`: the legacy `upstream_review.idle`, which the next CI observation moves to `upstream_review.waiting_ci` with no pending action (table: `require_reviewer_answer` to `upstream_review.waiting_human` with the pending action `answer_reviewer`);
  - `proposal`: the legacy `upstream_review.waiting_capacity` holding the proposal, which nothing publishes; the next CI observation moves it to `upstream_review.waiting_ci` (the table has no row for it);
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

## Provisioning planned before the operation table drove it

- **Where:** `controller.py` (the provisioning plan and launches) and `reconcile.py` (the provisioning receipt), legacy sites `controller.plan_provisioning` and `reconcile.confirm_provisioning`.
- **Today:** from `provisioning.idle` the table's rows run provisioning. A record in a shape the table does not have, `provisioning.waiting_human` without a pending action (written before the table), is still planned by the legacy code into `provisioning.waiting_capacity`; such a record is launched again without `start_operation`, within the same launch limit, has no exhaustion wait, and its receipt moves it to the legacy `development.idle` with a revision bump instead of `confirm_provisioning`.
- **Table:** provisioning is planned and launched from `provisioning.idle` only (`start_operation`, `require_provisioning_intervention`, `confirm_provisioning` to `development.fix_needed` without a revision bump).
- **Required:** migrate the legacy provisioning shapes to `provisioning.idle`, then remove the legacy branches.

## Operation status changes without a row

- **Where:** `controller.defer_destination_operation` and `controller.rearm_provisioning` (`recovery.LEGACY_SITES`).
- **Today:** while destination writes are disabled, the controller retires a planned fork provisioning intent as `not_launched` by itself, and rearms it as `planned` when destination writes are enabled again. The state publisher admits exactly these two transitions (`recovery.LEGACY_TRANSITIONS`).
- **Table:** a planned operation becomes `not_launched` only by `resolve_not_launched`, and a closed operation never changes status.
- **Required:** remove the path that defers destination writes, with the pre-fork proposal.

## Development PR operations have no row

- **Where:** the development PR and CI bootstrap plans in `controller.py` and the development PR receipt in `reconcile.py` (legacy sites `controller.plan_development_pr`, `controller.plan_ci_bootstrap`, `reconcile.confirm_development_pr`).
- **Today:** the operations `dev_pr_create` and `dev_pr_update` follow the operation table, but no lifecycle row starts them or is fired by them: the plan moves the record to the legacy `development.waiting_capacity`, the confirmed operation moves it to `development.waiting_ci`, and launches that never return a receipt stop at the launch limit with the operation open and no human wait.
- **Table:** `record_publish_success` leads to `development.waiting_ci`; no row of the table creates the development PR.
- **Required:** decide where the development PR operation sits in the table, then take it through rows.

## Resolution evidence has no producer

- **Where:** `/upstreaming resolve-operation`, guard "a resolution evidence record states the disposition".
- **Today:** the command takes the table's `resolve_*` rows with a record of kind `resolution` from the state store, but nothing writes such a record, so every resolution is refused with "resolution evidence record not found".
- **Table:** `resolve-operation` closes an open operation with a resolution evidence record.
- **Required:** a trusted way to record resolution evidence.

## Work requested before the Work identity carried the generation

A Work payload written before the Work identity carried `generation` and `dedupe_key` names neither: the reconciler and the publisher read it as generation 0 and recognize its Work by its revision (`work.task_identity`), and requested Work recorded without `active_work.generation` is generation 0 (`work.work_generation`) and is dispatched with the payload it was first submitted with. Remove both fallbacks once no such Work or worker is in flight.

## Records written before `budget.reruns` existed

The migrated live record has no `budget.reruns`. The schema makes it optional and the engine counts from zero. Make the field required once every record carries it.

## `blocked_reason` is still a field

The stop flag is `paused` alone, and the schema admits only null for `blocked_reason`. Records still carry the field. Remove it once every record is rewritten by the strict-schema migration.

## Readings to confirm

These follow the literal reading of a decision and are kept until a decision says otherwise:

- Every stop, and `resume_development` and `resume_upstream_revision`, clears a held unpublished proposal together with the pending publication.
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
- `retract-pr` is held like `submit-upstream`: checked against the table, then refused without change.
- The tracking-issue entries offer the deprecated `force-develop` in `parked.idle`, beside `resume` and `abandon`, while it is the override that takes effect.
- A contribution skipped by a tick, after a collector failure or an unexpected error, leaves every command on its tracking issue, `resume` included, unconsumed and without a reply until a tick processes it. An error in a contribution that is already stopped, or that the same tick would have created, is reported in the tick report (`controller.alerts`) and the workflow annotation only, with no event and no post on the tracking issue.
- `begin_investigation` is taken in the tick that allocates the investigation Work (or exhausts its budget), not as soon as a request is in `queued.idle`: a request waiting for a worker slot stays in `queued.idle`, where removing the intake label still withdraws it.
- The task that `request_patch` asks for is a patch, or a repair when feedback is pending for the Work to answer; `request_repair` always asks for a repair. A refreshed dossier that returns to the fork without a failing CI job or pending feedback is therefore answered by a patch.
- Every rejected or blocked outcome charges `no_progress`, whatever progress the worker reports, because `reject_patch_proposal` charges it unconditionally; a published or uncertain Result starts it again, as `BUDGETS` states, although no row lists that restart.
- The failing CI job of the head is the first executed blocking job of the evidence at `last_ci_evidence_id` that did not succeed, else the first job the evidence names as failing the runner-label check. Its name is the key of `budget.clusters`; existing keys are kept.
- A Work payload without `generation` and `dedupe_key` is generation 0, also when the record is in a later generation: its Result is then late. A payload that names only one of them matches no Work.
- While the record holds requested Work, or a Result held for a publish retry that is not exhausted, a changed fork head is not recorded; the table has no head row in an `agent_running` state.
- `exhaust_work_recovery` names `work_total` when both budgets are exhausted.
- In upstream review, `exhaust_repair_attempts` names the first exhausted counter of `no_progress`, `work_total`, and `repair_attempts_cluster`.
- A wait for a human is a state of kind `gate` or `locked`, or of kind `human_wait` with a recorded pending action (`compat.waits_for_human`). Only the rows the table lists for it leave it: observation reads nothing in a wait that has no observation row, a legacy write cannot change its phase or activity, and a Result that arrives there takes `record_late_effect`. A `waiting_human` activity without a pending action, a shape written before the table (the legacy provisioning wait), is still handled by the legacy paths: the legacy provisioning plan starts from `provisioning.waiting_human` only without a pending action.
- A planned operation that a trusted workflow runs stays `planned` across its launches; `begin_effect`, `record_response`, and `confirm_by_discovery` are taken together when its verified receipt is folded with the exact readback. The receipt establishes the facts of `begin_effect` (capacity available, the contribution not paused, the final check passed): the workflow's own final check read them before the effect. A push establishes "capacity available" without a runner reservation.
- A branch operation that the store holds as `applying` lost the response of its push: the branch observation takes `record_response` ("its response was lost") before discovering it, as the publisher hands an operation over only after it recorded the response.
- When the publisher's final check before a push does not pass (the head re-read fails or moved), no intent is recorded and the outcome carries no operation, instead of a `failed` operation, because `planned` has no row to `failed` other than a resolution.
- `active_operation_id` names an open operation only: the reference is released in the write that closes the operation, although no row of the table lists that clear (`record_merge` clears it explicitly).
- The counter of `operation_launch` is `external_ids.launch_attempts` of the operation, where launches were counted before the table. A launch is repeated only 300 seconds after the last one, and the relaunch requires available capacity, like the first launch. The launches of a development PR operation have the same limit.
- `require_provisioning_intervention` for exhausted launches is taken once the last allowed launch is 300 seconds old without a receipt, and not while the contribution is paused. Its pending action is `provisioning_launch_exhausted` with the counter `operation_launch`; after a human resolves the provisioning operation `failed`, it is `provisioning_operation_failed` without a counter.
- A provisioning operation resolved `confirmed` by a human fires no row: `confirm_provisioning` needs the receipt and the readback. A receipt that arrives while the contribution waits for a human closes the operation and keeps the state.
- A record whose `last_ci_evidence_id` names evidence that the store does not hold, in a state where Work is allocated, is an unexpected error of that contribution and takes `pause_on_processing_error`; the pause names the missing evidence id.

## Code still outside the engine

Transitions not yet taken through `engine.py` are written by the compatibility layer in `compat.py`, which keeps a registry of every remaining write site and every legacy state still admitted. Operation status changes without a row are listed in `recovery.LEGACY_SITES`. `python -m engine` prints how many table verbs the pipeline takes through the engine, how many legacy write sites remain, and the operation verbs taken through `recovery.py` with its remaining legacy sites.
