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
- **Today:** such a state has no row, so the engine cannot take `pause`, `pause_on_identity_change`, or `pause_on_processing_error` there. `compat.legacy_pause` records the same fields and clears the same things, but the activity that held the Work or a publication becomes `idle` instead of the table's state in which the Work is due. `resume`, `park`, and `refresh` on such a record are applied by the legacy command code: its resume has no `resume_stale` (a long stop is not followed by a refresh), and its park has no open-operation guard.
- **Table:** the flag verbs apply in every non-terminal state, with `set_moves`; `resume_stale` applies after a long stop in provisioning and development; `park_contribution` requires no open operation.
- **Required:** migrate the legacy states to table states (investigation, provisioning, and development rows), then remove the legacy branches.

## Some resumes lead where nothing continues yet

- **`resume_provisioning`** requires the fact that the prerequisite is corrected, which no caller establishes yet: `/upstreaming resume` in `provisioning.waiting_human` is refused unless the wait is long enough for `resume_stale`. The legacy `provision` command still exists for that wait.
- **`resume_ci`** in `development.waiting_human` and `handoff.waiting_human` leads to the CI wait, but the rerun of the interrupted jobs that the row states is not launched. No record enters a wait with the pending action `ci_interrupted` yet.
- **`restart_generation`** from `parked.idle` takes no class, paths, or reason, so `admission_override` stays empty; `force-develop` remains the legacy way to override a dossier park.
- **Required:** the provisioning prerequisite check, collector reruns, and the `resume` arguments of the command grammar.

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
- `/upstreaming resume` on a contribution that is neither paused nor in a human wait or in `parked.idle` is answered with `not paused`.
- A contribution skipped by a tick, after a collector failure or an unexpected error, leaves every command on its tracking issue, `resume` included, unconsumed and without a reply until a tick processes it. An error in a contribution that is already stopped, or that the same tick would have created, is reported in the tick report (`controller.alerts`) and the workflow annotation only, with no event and no post on the tracking issue.

## Code still outside the engine

Transitions not yet taken through `engine.py` are written by the compatibility layer in `compat.py`, which keeps a registry of every remaining write site and every legacy state still admitted. `python -m engine` prints how many table verbs the pipeline takes through the engine and how many legacy write sites remain.
