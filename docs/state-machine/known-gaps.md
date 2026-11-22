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

## Pause while a publication is held does not request fresh Work

- **Where:** `pause` in `development.agent_running`; the legacy stop path in `compat.py`.
- **Today:** the stop resets the activity to `idle` only from `agent_running` or `waiting_capacity`. One tick after a Result the activity is normally `waiting_ci`, so after pause and resume the record can sit in `development.waiting_ci` with nothing held. If CI for the old head is missing or pending, it waits silently with no Work and no pending human action. If CI is green on a feedback-driven repair, `nominate_for_handoff` fires while `pending_feedback_evidence_ids` is still set.
- **Table:** a pause in an `agent_running` state leads to the state in which the Work is due again (`development.fix_needed`), and nomination requires no pending actionable feedback.
- **Required:** take the pause through the engine everywhere, and assert `no_pending_actionable_feedback` from the record instead of unconditionally.

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
- **Upstream identity change** blocks the contribution without clearing requested Work.
- **Records stopped before stops cleared the pending publication** keep it. A paused one resumes normally; a parked one needs `abandon` then `restart`.
- **Required:** route every stop through the engine.

## The pause after a processing error uses the legacy stop outside the handoff phase

- **Where:** `pause_on_processing_error`; `compat.pause_on_processing_error`.
- **Today:** the engine takes the verb only for a record in a handoff state. For every other record the stop is written by the compatibility layer with the effects of the legacy `pause`: a record in an `agent_running` state is left in the `idle` activity of its phase, and a record in a state the table does not have (for example `development.idle`) is paused in place.
- **Table:** the verb applies in every non-terminal state of the table, and setting the flag in an `agent_running` state leads to the state in which the Work is due again (`investigation.idle`, `development.fix_needed`, or `upstream_review.fix_needed`).
- **Required:** take the verb through the engine everywhere, together with `pause`.

## Records written before `budget.reruns` existed

The migrated live record has no `budget.reruns`. The schema makes it optional and the engine counts from zero. Make the field required once every record carries it.

## Readings to confirm

These follow the literal reading of a decision and are kept until a decision says otherwise:

- Every stop, and `resume_development` and `resume_upstream_revision`, clears a held unpublished proposal together with the pending publication.
- `resume_upstream_review` still clears pending feedback after `answer_reviewer`, although the pending-feedback decision says every resume keeps it.
- When a CI failure and a review arrive together at the gate, the failure wins.
- `record_actionable_review` does not list the processed comment and review ids; the collector records them beside the row.
- The admission scope guard is on `admit_development` only, not on `readmit_development`.
- A contribution skipped by a tick, after a collector failure or an unexpected error, leaves every command on its tracking issue, `resume` included, unconsumed and without a reply until a tick processes it. An error in a contribution that is already stopped, or that the same tick would have created, is reported in the tick report (`controller.alerts`) and the workflow annotation only, with no event and no post on the tracking issue.

## Code still outside the engine

Transitions not yet taken through `engine.py` are written by the compatibility layer in `compat.py`, which keeps a registry of every remaining write site and every legacy state still admitted. `python -m engine` prints how many table verbs the pipeline takes through the engine and how many legacy write sites remain.
