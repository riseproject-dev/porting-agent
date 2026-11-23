---
title: Transition engine
parent: State machine
nav_order: 3
---

# Transition engine
{: .no_toc }

The [lifecycle table]({{ '/state-machine/table/' | relative_url }}) defines every state and transition of a contribution. `engine.py` applies it: `engine.apply_transition(record, verb, ...)` is the write path of the lifecycle fields `phase`, `activity`, `paused`, `revision`, and `generation`. This page describes how a transition is taken, which parts of the pipeline take their transitions through the engine, and how an existing record is brought to a table state.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## Taking a transition

`apply_transition` takes a contribution record and a verb of the table. It never performs a remote effect; it returns the new record and the event to persist, or the reason the transition is refused.

1. **Resolve the verb.** The record's state is `phase.activity`. The verb is a row of that state, a rule row, or a setter or clearer of the `paused` flag. A verb that does not leave the state is an error of the caller.
2. **Apply the mask of the flag.** While `paused` is set, a controller tick is refused, a Result takes `record_late_effect` only, and of the commands only `resume`, `park`, `refresh`, `retract-pr`, `abandon`, and `resolve-operation` apply.
3. **Evaluate every guard.** Each guard text of the table is registered once in `engine.PREDICATES` under a name. A guard is one of two kinds:
   - a *record* predicate is decided by the engine from the record, the policy, the time, and the command: the exact revision, the candidate head, the pending human action, quiescence, staleness of a stop, the reminder interval, and the budgets;
   - a *fact* is something only the caller can observe, such as a CI verdict, an operation receipt, or a free worker slot. The caller asserts the fact by its name. A fact that is not asserted does not hold.

   A transition is refused unless every guard is established. A budget remains while its count is below its limit from `policy.json`; a budget whose count lives outside the record (dispatcher runs, an operation's attempts) is refused until the caller supplies the count. The rerun counts of the current head are in the record, in `budget.reruns` (`head_sha`, `flaky`, `interrupted`); counts recorded for another head, or no counts at all, are counts of zero for the current head. Whether a failing CI job is recorded for the head is decided from the job the caller names from the CI evidence the record points at; without one, `repair_attempts_cluster` is neither charged nor exhausted. Two facts that state the opposite of each other, such as a CI failure on the candidate head and its absence, are refused together.
4. **Apply the effects of the row.** The engine sets and clears exactly the fields the row names. The caller supplies the value of a field the engine cannot derive; a value for a field the row does not set is an error. The requested Work is kept, cleared, or replaced as the row states. Some values are always derived by the engine: Work that a row allocates for a patch or a revision carries the pending feedback ids present at that moment (`active_work.feedback_evidence_ids`), a fresh Work graph and a Result held for a publish retry carry the same ids, and a row that records a new head starts `budget.reruns` again for that head. A row may clear a part of a field: a published Result clears exactly the feedback ids its Work carried, and a stop clears `proposal_artifact` only while it holds a proposal that is not published. A generation bump resets every mutable lifecycle field and keeps identity and replay fences.
5. **Charge and restart budgets.** Each budget in the row's charges is counted. A resume starts again the one counter named in `pending_human_action.counter`, and no other.
6. **Enforce the revision rule.** The revision changes exactly when the row states a bump.
7. **Emit the event.** The event kind is the verb. Its `details` record the source and target state, the budgets charged, the counter started again with its count and the acting human, and what the caller adds (for the handoff entry, the exact head and revision).

`engine.apply_command` resolves an `/upstreaming` command to the one verb whose guards hold in the record's state and takes it. A `resume` of a paused contribution clears the flag first: it takes `resume_pause`, or `resume_stale` after a long stop in provisioning or development.

## What the pipeline takes through the engine

`engine.PIPELINE_VERBS` lists the verbs that pipeline code takes through the engine; `engine.UNWIRED_VERBS` is every other verb of the table. The two sets partition the table, and a test fails when a verb is in neither or in both. `uv run --locked python -m engine` prints both sets, the number of record and fact guards, and the remaining entries of the compatibility shim.

Every state of the `handoff` phase is written by the engine only. The stop flag, its commands, and the reminders are taken by the engine in every phase, for a record in a state of the table:

| Verb | Taken by | When |
|:-----|:---------|:-----|
| `nominate_for_handoff` | CI observation | Every required CI job executed and passed on the exact head, with no requested Work and no unprocessed actionable feedback. The contribution enters `handoff.awaiting_submission` with the pending action `submit_upstream`. |
| `return_for_repair` | CI observation | An executed blocking failure on the candidate head. The evidence of the failure is recorded as `last_ci_evidence_id`. |
| `record_actionable_review` | CI observation | Authorized actionable review feedback on the development PR while CI has not failed on the candidate head. The contribution returns to `development.fix_needed` with one revision bump, the feedback is recorded in `pending_feedback_evidence_ids`, and the CI evidence of the candidate head is kept. |
| `record_explanation` | CI observation | An authorized explanation-only comment on the development PR. The contribution stays in `handoff.awaiting_submission` with its revision, candidate head, and CI evidence; the tracking-issue entry quotes the comment. No Work is requested and nothing is answered automatically. |
| `record_head_change` | CI observation | The observed fork head differs from the candidate head. |
| `submit_upstream`, `retract_submission` | Command handling | See [Requesting upstream submission](#requesting-upstream-submission). Both commands are checked against the table and then held. |
| `pause`, `resume_pause`, `resume_stale`, `park_contribution`, `refresh_investigation`, `restart_generation`, `abandon` | Command handling | Every command of the table (`pause`, `resume`, `park`, `refresh`, `abandon`, `submit-upstream`, `retract-pr`) on a record in a state of the table, in every phase; in the handoff phase every command. See [Stop and resume](#stop-and-resume) and [Commands](#commands). A command no row of the state takes is rejected with the state it does not apply in and the states it applies in. |
| `withdraw_request` | Controller | The intake label was removed from the tracking issue of a request in `queued.idle` with no Work, no dossier, and no fork. The contribution is closed with `terminal_reason` `withdrawn`. |
| `resume_investigation`, `resume_provisioning`, `resume_ci`, `resume_development`, `resume_debug_cleanup`, `resume_internal_pr_closure`, `resume_submission_preparation`, `resume_upstream_review`, `resume_upstream_revision`, `resume_release_observation` | Command handling | `/upstreaming resume` in a human wait of the table, guarded by the pending human action; each starts again the one counter that `pending_human_action.counter` names. |
| `record_reminder` | Controller | A pending human step in a state of kind `gate`, `human_wait`, or `locked`, once `human_sla_business_days.reminder` business days have passed since the request or the last reminder. See [Reminders](#reminders). |
| `pause_on_identity_change` | Controller | The observed upstream repository identity differs from the recorded one. |
| `pause_on_processing_error` | Reconciler | An unexpected error while the tick processed the contribution, see [Errors while a tick runs](#errors-while-a-tick-runs). The caller states `paused_reason`; the engine refuses the verb without one. |

A record in a state that only the compatibility layer writes (`compat.LEGACY_STATES`, for example `development.idle` or `development.waiting_capacity`) has no row to take. For such a record the three verbs that set `paused` are written by `compat.legacy_pause` with the same fields (`paused_at`, `paused_by`, `paused_reason`) and the same clears (the requested Work, the pending publication, a held proposal); the activity that held the Work or the publication becomes `idle`. `/upstreaming pause`, `resume`, and `park` on such a record are applied by `compat.apply_command`. Every other command of the table (`refresh`, `abandon`, `submit-upstream`, `retract-pr`) applies in states of the table only and is rejected there with the states it applies in.

At the submission gate the collector reads the review comments and CI on the candidate head in one observation and then takes at most one of the two rows that leave the gate for repair. Their guards are exclusive: when CI failed on the candidate head, `return_for_repair` applies and the review comments are left unread, so they are recorded when the development PR is observed again; otherwise `record_actionable_review` applies. The ids of the comments a row recorded are added to the replay fences of the record in the same write.

A worker Result is folded only when the record still asks for it: the contribution is in a working phase (`investigation`, `provisioning`, `development`, or `upstream_review`), it is not paused, and the Result answers the requested Work at its revision or the trusted publication of the same worker run (a publish-only retry, or a verified proposal the record holds). A record in a state that holds Work without recorded Work is matched by the Result's revision. `park` and `pause` clear the requested Work and the pending publication without a revision bump, so an equal revision alone is not a match. Every other Result takes `record_late_effect`, in every state: the reconciler marks the publication artifact as processed and appends one event with the Result's status and task revision, and nothing else in the record changes. An artifact that is already marked as processed is not recorded again, so each late Result is recorded once.

A stop invalidates what is held for publication. `park_contribution`, `abandon`, the verbs that set `paused`, and every other transition that ends the generation clear `pending_publication` and a held proposal that is not published (`proposal_artifact` whose outcome is `proposal`) together with the requested Work; `python -m lifecycle check` verifies it. A published proposal is kept. Nothing is therefore published after a stop: the resume finds nothing held and requests fresh Work. The legacy `park` and `pause` paths in `compat.py` do the same, and so does the reconciler's park after exhausted Work retries. A record stopped before stops cleared these fields is rewritten by the [flag migration](#flag-migration). The publication Result that arrives afterwards takes `record_late_effect`. No publication is launched for a paused record: the publish-only retry and the publication of a held verified proposal both skip it. A stop therefore never keeps `/upstreaming resume` from applying because of a publication that was pending when it was taken.

A branch that the worker run already pushed is recorded by head observation, which reads the fork branch in the `development` and `upstream_review` phases and in `handoff.awaiting_submission`, also while the record is paused: the observed head replaces the recorded one and the revision is bumped. A parked or closed record is not observed. Its recorded head stays as it was, and the pushed commit stays on the fork branch; a resume from `parked.idle` begins a new generation that records the fork and its head again at provisioning. The verb is taken through `compat.record_late_effect`, which uses the engine for a table state and writes the same event for a state of `LEGACY_STATES`.

In the other phases the controller, the reconciler, and the collector still decide state changes in their own code. They write the lifecycle fields through `compat.py`, a compatibility shim with two registries: `LEGACY_SITES` names each call site that still writes state outside the table, and `LEGACY_STATES` lists the states those call sites produce that the table does not have (for example `development.idle` and the capacity waits). The shim refuses any write that enters the handoff phase, changes a record that is in it, or leaves a record in a state that is in neither the table nor `LEGACY_STATES`. No module other than `engine.py` and `compat.py` assigns a lifecycle field; a test scans the source for such assignments.

The final check before a fork write (`lifecycle.publication_check`) reads the table for a record in a handoff state: an automated writer may write the fork only in a handoff state the table marks as writing. At the submission gate it may not.

State validation applies the same rule to committed records: a contribution is in a table state or in a state of `LEGACY_STATES`, and a handoff wait carries a pending action with which the table enters that wait.

## Errors while a tick runs

An error in one contribution does not stop the controller tick. Every step of a tick that works on one contribution runs inside `isolation.contribution(contribution_id, step)`: the confirmation of a development PR or provisioning receipt, the fold of a worker Result, the Work deadline, dispatch recovery, the recovery of recorded review feedback, the observation of the fork branch and CI, the canary recovery, the destination and intake-label checks, the source identity check, the application of a command, the discovery alerts of an operation, the provisioning and development PR plans, the publication of a held proposal, Work selection, and the publish-only retry. An exception that leaves such a block is attributed to that contribution and classified by its class:

| Exception | Class | What the tick does |
|:----------|:------|:-------------------|
| `collect.CollectError`, `http.client.HTTPException`, `OSError` | Collector failure: GitHub could not be read for the contribution. | The contribution is skipped for the tick: no state change, no pause. The report lists it under `skipped_contributions` with the step, the error class, and the message. |
| Every other exception, including `lifecycle.TransitionError` and `schemas.SchemaError` raised outside the application of a command | Unexpected error (`isolation.ProcessingError`). | Everything staged for the contribution is discarded, none of its effects is requested, the other contributions are processed, and the contribution takes `pause_on_processing_error`. |

Every GitHub read of the collector goes through `collect.Api`, which turns each failure to read a response into `collect.CollectError` with a bounded message: an HTTP error status, an error of the HTTP layer (`http.client.HTTPException`, for example `IncompleteRead` or `RemoteDisconnected`), an error of the transport (`OSError`, which covers `urllib.error.URLError`, TLS errors such as `ssl.SSLEOFError`, socket timeouts, and reset connections), and a response body that is not JSON (`json.JSONDecodeError`, `UnicodeDecodeError`), for example one cut off in transit. The body itself is never quoted. Only the read and the decoding are converted: a `KeyError`, `TypeError`, or `ValueError` raised while a well-formed response is used is an unexpected error. The HTTP-layer and transport classes are also in the collector class above, as a safety net for a read that does not go through `collect.Api`.

Expected rejections never reach the block and pause nothing, as before: a `TransitionError` raised while a command is applied and a command the table or the legacy path refuses are recorded as a rejected command with the reason, a command that fails authentication or the grammar is recorded as rejected, and an intake form that raises `intake.IntakeError` is reported as invalid. A malformed development PR receipt (`KeyError`, `ValueError`) is ignored until a valid one arrives.

Staging is all or nothing per contribution. `reconcile.tick` computes the tick on a copy of the committed state. When a contribution fails, the tick is computed again from the committed state with that contribution left out: no step processes it, so no record, event, operation, evidence, or command receipt is staged for it and no Work, dispatcher launch, provisioning, development PR, publication, or publish retry is requested for it. It still counts as an existing contribution for the others: it keeps its worker slot and its upstream. A command posted on its tracking issue, `resume` included, is left unconsumed and gets no reply: it is read again by every tick until one processes the contribution, and is applied or rejected then. A request that would become a new contribution in this tick is deferred and read again by the next tick. Before the result is returned, the tick verifies that nothing staged and nothing requested names the contribution, and stops otherwise.

After an unexpected error the controller takes `pause_on_processing_error` on the record as it was committed before the tick, through the engine for a record in a state of the table and through `compat.legacy_pause` for a record in a legacy state. The verb sets `paused` with `paused_by` `controller` and `paused_at`, clears the requested Work, the pending publication, and a held proposal, and keeps the revision; from an `agent_running` state it leads to the state in which the Work is due again; `paused_reason` is `processing_error: <error class>: <message> (step <step>, run <run id>)`. The record and the event are the only things the tick stages for the contribution. The event carries the step, the error class, the message, and the run id in its `details`; the status ledger posts them on the tracking issue with the statement that everything else of that run was discarded. The report lists the error under `controller.alerts` and, with its fields, under `processing_errors`. `python -m reconcile` still exits with status 0, since the tick goes on, and prints one GitHub Actions `::error::` annotation per processing error, built from the same bounded text and escaped as the workflow command format requires, so that a pipeline bug charged to a contribution is visible on the workflow run. `/upstreaming resume` at the current revision clears the pause and bumps the revision.

The error text is bounded and sanitized before it is stored or posted (`isolation.clean_text`): the text normalization of the state store is applied, everything that matches a credential pattern of the store is replaced, control characters and line breaks become one space, and the message is cut to 300 characters. The traceback is written to the log of the workflow run only.

A contribution that is already stopped is not paused again. The verb is guarded by `not paused`, like every verb that sets the flag, so an error in a contribution that is paused, by a human or by the controller, changes nothing and appends no event, whatever the error is; the same holds for a terminal contribution. Observations still fold while a contribution is paused, so an error that persists is met again by every tick: each tick leaves the contribution out, lists the error in its report, and writes nothing. The next error after a resume pauses it again. An error in a contribution that is already stopped, and in a contribution that the same tick would have created from an intake request, which has no committed record to pause, is reported only in the tick report (`controller.alerts` and `processing_errors`) and in the workflow annotation: no event is appended and nothing is posted on the tracking issue.

Stop commands are applied before any Work is selected. When applying `pause` or `park` fails with an unexpected error, the contribution is errored like in any other step: the tick is computed again without it, so nothing is launched for it, and it is paused by the controller.

The whole tick stops, nothing is committed, and no contribution is paused when:

- unexpected errors occur in two or more contributions in one tick. Collector failures do not count: any number of contributions can be skipped;
- `pause_on_processing_error` cannot be applied to the errored contribution, for example because its record is in no admitted state;
- an exception is raised outside every block: the state load and the validation of the store, the policy, the text and record migrations, the collection of the tick's inputs (intake issues, command comments, tracking issues, source snapshots), command authentication, the intake of a request that is not a contribution yet, a command on an issue that tracks no contribution, the staging of the result into the store, and the state commit.

`python -m reconcile` exits with status 1 and a message that names the contributions and their errors in the first two cases, and with the error itself in the third, as before. The record migration is in the third group although it works on one record: a record that awaits the migration is not in a state of the table, so no row could pause it, and the state load refuses a handoff record in any other shape.

## Stop and resume

`/upstreaming pause`, `resume`, `park`, `refresh`, and `abandon` on a record in a state of the table are applied by `engine.apply_command`, in every phase; the controller resolves the one verb whose guards hold and refuses the command with the guard that does not.

- `pause` sets `paused` with `paused_by` `human`, `paused_at`, and the optional reason in `paused_reason`. It clears the requested Work, the pending publication, and a held proposal, and keeps the revision. In an `agent_running` state it leads to the state in which the Work is due again (`investigation.idle`, `development.fix_needed`, `upstream_review.fix_needed`), so the resume requests fresh Work. A Result of the stopped Work takes `record_late_effect`. While paused no Work is requested, no budget is charged, no publication or publish retry is launched, and no reminder is posted.
- `resume` on a paused record clears the flag and bumps the revision (`resume_pause`). After a stop longer than `refresh.stale_after_days` days in `provisioning` or `development`, counted from the older of `paused_at` and `pending_human_action.requested_at`, it takes `resume_stale` instead: a complete investigation runs again from `queued.idle` while the fork and development PR are kept, and the refresh records the phase, head, and development PR it began from (no head or PR when there is no fork yet). A refreshed dossier returns to development on the recorded fork only when the refresh began with that fork and its development PR; otherwise development is admitted afresh. In the handoff phase the resume never refreshes: the candidate head, `handoff_request`, and the CI evidence are kept, and `submit-upstream` then names the new revision and the same head.
- `resume` in a human wait that is not paused takes the resume of that wait that its pending human action selects; on a paused human wait the first `resume` clears the flag only, and its tracking-issue entry names the pending action that remains, so a second `resume` leaves the wait.
- `resume` in `parked.idle` starts a new generation at `queued.idle` (`restart_generation`); it requires the exact revision, no open operation and no pending publication, and no open upstream PR. It accepts an optional admission class and paths, `/upstreaming resume revision=N class=<class> paths="<glob>,<glob>" reason="..."`, which are recorded with the reason as `admission_override`; the paths must be safe relative paths, as for a scope. The new generation still runs the complete investigation. The dossier admission does not apply the override yet: a dossier that decides park parks the contribution again (see the known gaps). `class` and `paths` are refused by every other resume.
- `abandon` applies in `parked.idle` only, with a reason and at any revision, also while paused. It leads to `closed.idle` with `terminal_reason` `abandoned`; the reason the human gave is kept in the event. A closed contribution is final: no command resumes it. Elsewhere the command is rejected with the statement that it applies in `parked.idle`; park first.
- `refresh` applies in the states whose rows take it: `development.fix_needed`, `development.waiting_ci`, `development.waiting_human`, `handoff.awaiting_submission`, and `handoff.waiting_upstream_pr`, with the exact revision, no requested Work, no open operation or pending publication, and a recorded development PR and head; it is accepted while paused. In `handoff.waiting_upstream_pr` it names the exact clean head, `/upstreaming refresh revision=N head=<clean head> reason="..."`; elsewhere a `head` is refused.
- `park` takes `park_contribution` from every state the rule covers, with a reason and with no open operation; it is accepted while paused. It does not apply in `handoff.waiting_upstream_pr`, `release_wait.*`, and the `upstream_review` states other than `upstream_review.waiting_human`.

A stop command is honored at a stale revision; every other command names the current revision. The reply to a `pause` of a paused contribution says it is already paused, and the reply to a `resume` of a contribution that is neither paused nor waiting says it is not paused.

`/upstreaming block` is retired: the lifecycle has one stop flag, `paused`, with its reason and who set it. The command is rejected with a message that names `pause`. The controller sets the same flag when the upstream repository identity changes (`pause_on_identity_change`, `paused_by` `controller`, `paused_reason` `upstream_identity_changed`) and after a processing error.

## Commands

The grammar (`commands.GRAMMAR`) has the commands of the table and two intake commands, and one deprecated command:

| Command | Applies in |
|:--------|:-----------|
| `pause`, `resume`, `park`, `refresh`, `abandon`, `submit-upstream`, `retract-pr` | The states whose rows, or the `paused` flag, take them; see [Stop and resume](#stop-and-resume) and [Requesting upstream submission](#requesting-upstream-submission). |
| `authorize-followup` | A merged contribution (`release_wait`, `released`); it records a follow-up request. Outside the table. |
| `resolve-operation` | An operation, by its id; see the operation table. Outside the table. |
| `force-develop` | `parked.idle`: overrides a dossier park and starts provisioning with the class and paths of the command. Deprecated: its replacement is `resume` from `parked.idle` with `class` and `paths`, which takes effect once the dossier admission applies the recorded override. Until then the command still works, and its tracking-issue entry names the replacement. |

The retired commands are rejected with a message that names their replacement, and the reply on the tracking issue shows the replacement's form:

| Retired | Replacement |
|:--------|:------------|
| `request-handoff`, `approve-handoff`, `register-upstream-pr` | `submit-upstream` at the submission gate; the upstream PR is verified by observation. |
| `block` | `pause`, with a reason. |
| `approve-scope` | None needed: the verified dossier decides admission and provisioning starts automatically. A parked contribution resumes from `parked.idle`, with `class` and `paths` as an admission override. |
| `provision` | None needed: the fork is provisioned automatically. In `provisioning.waiting_human`, `resume` once the prerequisite is corrected. |
| `restart` | `resume` from `parked.idle`; `closed.idle` is final. |
| `clarify` | `resume` in the human wait once the cause is corrected. |
| `revise-scope` | `refresh`, or `resume` from `parked.idle` with `class` and `paths`. |

The tracking-issue entries offer, under "Possible next commands", the commands the table takes in the contribution's state (and `force-develop` in `parked.idle`); in a state that only the compatibility layer writes they offer `pause`, `park`, and `resume`. A retired command is never offered.

## Reminders

Every tick, the controller takes `record_reminder` for each record in a state of kind `gate`, `human_wait`, or `locked` whose pending human step was requested, or last reminded, at least `human_sla_business_days.reminder` business days ago (Monday to Friday, counted in whole days from the exact time of the request or of the last reminder). The row sets `pending_human_action.reminded_at` and nothing else; the revision is kept. While the record is paused no reminder is posted. The event carries the pending action and, for `handoff.awaiting_submission`, the revision and candidate head; its tracking-issue entry repeats the exact `submit-upstream` command for them, with the statement that the command is refused for now.

## Requesting upstream submission

`handoff.awaiting_submission` is the gate at which a human requests upstream submission. The tracking-issue entry for `nominate_for_handoff` states the exact head on which CI passed and shows the command to post:

```text
/upstreaming submit-upstream revision=N head=<full head SHA>
```

The grammar requires the revision and the full 40-character head. The engine accepts the command exactly as the table defines `submit_upstream`: the revision is the current one, the head is the candidate head recorded in `handoff_request` and still the fork head, and the contribution is not paused. A resume from a pause bumps the revision and keeps the candidate head, so the command then names the new revision and the same head. Outside the handoff phase the command is rejected with a reason that names the state the contribution is in and `handoff.awaiting_submission`, the state the command applies in.

`submit_upstream` leads to `handoff.cleaning_debug` and authorizes the deterministic handoff preparation: debug cleanup, CI on the clean head, closing the development PR, and the submission package. The controller does not run those operations. A contribution taken into `handoff.cleaning_debug` would stay there, so the controller holds the command (`engine.HELD_COMMANDS`): it checks the command against the table, so a wrong revision or head gets its precise reason, and then rejects it with the statement that nothing was changed. The tracking-issue entry that asks for the command says so, and the contribution stays in `handoff.awaiting_submission` with its fork, development PR, head, and evidence.

The commands `request-handoff`, `approve-handoff`, and `register-upstream-pr` are retired. Each is rejected with a message that names `submit-upstream`.

`/upstreaming retract-pr revision=N head=<clean head>` withdraws the submission package in `handoff.waiting_upstream_pr` (`retract_submission`, to `development.fix_needed`). Nothing reaches that state while `submit-upstream` is held, and the reminders the retraction stops are not posted yet, so the controller holds this command too: it checks the revision, the exact clean head, and quiescence against the table and then rejects the command with the statement that nothing was changed.

## Record migration

Before the lifecycle table, passing CI moved a contribution to the phase `handoff` with the activity `waiting_human` and the pending action `approve_handoff`. The table has no such wait. The controller rewrites such a record itself, before it does anything else in a tick:

- The migration is bound to one exact shape: the old approval wait with no requested Work, no open operation, no pending publication, no approval, no upstream PR, not paused, no pending actionable feedback, the candidate head equal to the fork head, the request for the current revision, and CI evidence that states that CI passed on that head. A handoff record in any other shape that is not a table state has no migration: the controller stops with an error and writes nothing.
- The new record is what `nominate_for_handoff` gives for the same CI evidence: `handoff.awaiting_submission`, the pending action `submit_upstream`, and no clean head (the clean head is produced by the debug cleanup). The revision, the fork, the development PR, the head, the dossier, the CI evidence, the budgets, and the replay fences are kept.
- The record and one appended event of kind `handoff_wait_migrated` are the only paths of that state commit; the tick requests no effect. The state publisher's final check accepts the commit and refuses any commit that writes the old shape again.
- The migration is idempotent: a migrated record is in a table state and is not selected again.

The tracking-issue entry for the migration event states that the fork, development PR, head, revision, and CI evidence are unchanged, that the earlier request to run `approve-handoff` no longer applies, and shows the `submit-upstream` command for the exact revision and head.

## Flag migration

Before the stop flag was merged, a `block` wrote `blocked_reason` and a human wait in place, a pause did not record `paused_at`, and a stop did not always clear what was held for publication. The controller rewrites such a record in the same way, in its own state-only commit with one appended event of kind `paused_flag_migrated` (cause `migration`, `paused-flag-merge`), before the ordinary tick:

- A `blocked_reason` becomes `paused`: `paused_by` `controller` and `paused_reason` `upstream_identity_changed` for the controller's block on a changed upstream identity, `paused_by` `human` and the block's reason otherwise. `blocked_reason` becomes null; the schema admits no other value. A block's human wait without a pending action becomes `idle`, as the old `resume` left it; with a pending action the wait is kept.
- A paused record without `paused_at` gets its `updated_at`, and one without `paused_by` gets `human`.
- A paused, parked, or terminal record loses the requested Work, the pending publication, and a held proposal, as the table's stops clear them.
- Nothing else changes. A blocked record in the handoff phase, or a block over a human wait that the table does not have, has no migration: the controller stops with an error and writes nothing. A record that needs none of this is not touched. The migration is idempotent.

## Terminal migration

Before the lifecycle table, `/upstreaming abandon` in any phase and the removal of the intake label wrote the terminal phase `abandoned`, and the schema also admitted `no_work`, `deferred`, and `already_released`. The table has one terminal phase for work that is not needed, `closed.idle`, with the reason in `terminal_reason`. The controller rewrites such a record in its own state-only commit with one appended event of kind `terminal_phase_migrated` (cause `migration`, `closed-terminal-phase`), before the ordinary tick:

- The phase becomes `closed` and the activity stays `idle`. `terminal_reason` becomes `withdrawn` for the removed intake label (the reason the controller recorded), `abandoned` for every other `abandoned` record, and the name of the old phase for the others. The text it held before is kept in the event.
- Nothing else changes: the revision, the generation, the flag, the evidence, and the replay fences are kept.
- A record with requested Work, an open operation, a pending publication, a held proposal, a text `blocked_reason`, or an activity other than `idle` has no migration: the controller stops with an error and writes nothing. The state publisher's final check refuses any commit that writes an old terminal phase. The migration is idempotent.

## Checks

`tests/test_engine.py` derives its cases from the table: every row is taken through the engine with its guards established and its effects verified, every row is refused when one of its facts is missing, every command row that requires the exact revision refuses another one, and the mask of the `paused` flag is verified for every row. `tests/test_live_handoff_migration.py` runs the migration and the ordinary controller on a copy of a committed record in the old approval wait. `tests/test_terminal_migration.py` runs the terminal migration on fixtures of each old terminal shape. `tests/test_command_grammar.py` verifies that the grammar is the table's commands plus the intake commands and the deprecated `force-develop`, that every retired command names its replacement, and that each table command applies in exactly the states whose rows take it. `tests/test_processing_errors.py` injects an exception into each step of a tick for one of two contributions and verifies each case of [Errors while a tick runs](#errors-while-a-tick-runs); `tests/test_late_results.py` injects one into every step for a record in every admitted state.
