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

## The failing-job guard is half asserted by the caller

- **Where:** `engine.py`, guard "a failing CI job is recorded for the head" used by `request_repair`, `request_upstream_revision`, and the cluster exhaustion guards.
- **Today:** the guard needs both a recorded `last_ci_evidence_id` and a job name supplied by the caller. With evidence recorded but no job named, `request_patch` is taken and no cluster is charged.
- **Required:** decide the guard from the recorded evidence before the development rows move to the engine.

## Legacy stop paths do not clear everything the table clears

- **Dossier park** (`park_investigation`, legacy site in `reconcile.py`) does not clear a held proposal or pending publication. It is inert today because nothing is retried while stopped and resume from park starts a new generation.
- **Required:** take the dossier park through the engine.

## The stop flag in states the table does not have

- **Where:** `compat.legacy_pause` and `compat.apply_command`, for a record in a state of `compat.LEGACY_STATES` (for example `development.idle`, `development.waiting_capacity`, `upstream_review.idle`).
- **Today:** such a state has no row, so the engine cannot take `pause`, `pause_on_identity_change`, or `pause_on_processing_error` there. `compat.legacy_pause` records the same fields and clears the same things, but the activity that held the Work or a publication becomes `idle` instead of the table's state in which the Work is due. `resume` and `park` on such a record are applied by the legacy command code: its resume has no `resume_stale` (a long stop is not followed by a refresh), and its park has no open-operation guard. `refresh` and `abandon`, which no row of such a state takes, are refused there.
- **Table:** the flag verbs apply in every non-terminal state, with `set_moves`; `resume_stale` applies after a long stop in provisioning and development; `park_contribution` requires no open operation.
- **Required:** migrate the legacy states to table states (investigation, provisioning, and development rows), then remove the legacy branches.

## Some resumes lead where nothing continues yet

- **`resume_provisioning`** requires the fact that the prerequisite is corrected, which no caller establishes yet: `/upstreaming resume` in `provisioning.waiting_human` is refused unless the wait is long enough for `resume_stale`. The retired `provision` command no longer leaves the wait; `park` and a resume from `parked.idle` do.
- **`resume_ci`** in `development.waiting_human` and `handoff.waiting_human` leads to the CI wait, but the rerun of the interrupted jobs that the row states is not launched. No record enters a wait with the pending action `ci_interrupted` yet.
- **Required:** the provisioning prerequisite check and collector reruns.

## The admission override is recorded but not applied

- **Where:** `restart_generation` from `parked.idle`, the dossier fold in `reconcile.py` (`admit_development`, `park_investigation`), and the deprecated `/upstreaming force-develop` in `compat.apply_command`.
- **Today:** `/upstreaming resume` from `parked.idle` records `class`, `paths`, and the reason as `admission_override`, but the legacy dossier fold ignores it: a dossier that decides park parks the contribution again, and a dossier that decides develop does not clear the override. `force-develop`, which the decisions retire, is kept as the override that takes effect: from `parked.idle` it moves the record to `provisioning.idle` with the command's class and paths, without a row of the table. Its tracking-issue entry and its rejection name the replacement.
- **Table:** `admit_development` accepts a verified dossier decision develop, or decision park with a recorded `admission_override`, and clears the override; `park_investigation` requires no override; the grammar has no `force-develop`.
- **Required:** take the dossier fold through `admit_development`, `readmit_development`, and `park_investigation`, then retire `force-develop`.

## A dossier decision close does not close the contribution

- **Where:** the dossier fold in `reconcile.py`, verb `close_investigation`.
- **Today:** a dossier whose decision is neither develop nor park is treated as a stale contract: investigation is due again at a new revision.
- **Table:** a verified dossier decision close leads to `closed.idle` and sets `terminal_reason`.
- **Required:** take the dossier fold through the table's verbs.

## A pause during the first development patch makes the next Work a repair

- **Where:** `pause` (and `pause_on_processing_error`) in `development.agent_running`; `controller._actionable` in `development.fix_needed`.
- **Today:** the pause moves the record to `development.fix_needed`, as the table says. The legacy selection there always requests `development_repair`, so after the resume the first patch is requested as a repair, with only the dossier as its reason.
- **Table:** in `development.fix_needed`, `request_patch` applies when no failing CI job is recorded for the head, and `request_repair` when one is.
- **Required:** select development Work through `request_patch` and `request_repair`.

## A leftover pending action can turn a short pause into a refresh

- **Where:** `engine.stopped_since`, used by `resume_stale` and `resume_pause`.
- **Today:** the stop is measured from the older of `paused_at` and `pending_human_action.requested_at` in any state. The legacy CI observation moves `development.waiting_human` to `development.waiting_ci` and keeps the pending action, so a record that carries a pending action older than 7 days refreshes after a pause of one day.
- **Table:** the clock is the older of `paused_at` and `pending_human_action.requested_at`.
- **Required:** clear the pending action whenever a legacy path leaves a human wait, so the clock only sees a wait that is still current.

## The locked-wait reminder lacks the package link and exact head

- **Where:** `record_reminder` in `handoff.waiting_upstream_pr`, `status_ledger.py`.
- **Today:** the generic reminder text is used.
- **Table:** the reminder in that state carries the package link and the exact head, and states when the observed upstream PR does not match.
- **Required:** render the reminder from the posted package once the handoff operations exist. Nothing reaches the state while `submit-upstream` is held.

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

## Code still outside the engine

Transitions not yet taken through `engine.py` are written by the compatibility layer in `compat.py`, which keeps a registry of every remaining write site and every legacy state still admitted. `python -m engine` prints how many table verbs the pipeline takes through the engine and how many legacy write sites remain.
