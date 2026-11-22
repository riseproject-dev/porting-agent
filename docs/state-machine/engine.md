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

Every state of the `handoff` phase is written by the engine only:

| Verb | Taken by | When |
|:-----|:---------|:-----|
| `nominate_for_handoff` | CI observation | Every required CI job executed and passed on the exact head, with no requested Work and no unprocessed actionable feedback. The contribution enters `handoff.awaiting_submission` with the pending action `submit_upstream`. |
| `return_for_repair` | CI observation | An executed blocking failure on the candidate head. The evidence of the failure is recorded as `last_ci_evidence_id`. |
| `record_actionable_review` | CI observation | Authorized actionable review feedback on the development PR while CI has not failed on the candidate head. The contribution returns to `development.fix_needed` with one revision bump, the feedback is recorded in `pending_feedback_evidence_ids`, and the CI evidence of the candidate head is kept. |
| `record_explanation` | CI observation | An authorized explanation-only comment on the development PR. The contribution stays in `handoff.awaiting_submission` with its revision, candidate head, and CI evidence; the tracking-issue entry quotes the comment. No Work is requested and nothing is answered automatically. |
| `record_head_change` | CI observation | The observed fork head differs from the candidate head. |
| `submit_upstream` | Command handling | See [Requesting upstream submission](#requesting-upstream-submission). |
| `pause`, `resume_pause`, `park_contribution`, `refresh_investigation` | Command handling | The table's commands for a record in a handoff state. Any other command is rejected with the state it does not apply in. |
| `pause_on_identity_change` | Controller | The observed upstream repository identity differs from the recorded one while the record is in a handoff state. |

At the submission gate the collector reads the review comments and CI on the candidate head in one observation and then takes at most one of the two rows that leave the gate for repair. Their guards are exclusive: when CI failed on the candidate head, `return_for_repair` applies and the review comments are left unread, so they are recorded when the development PR is observed again; otherwise `record_actionable_review` applies. The ids of the comments a row recorded are added to the replay fences of the record in the same write.

A worker Result is folded only when the record still asks for it: the contribution is in a working phase (`investigation`, `provisioning`, `development`, or `upstream_review`), it is not paused or blocked, and the Result answers the requested Work at its revision or the trusted publication of the same worker run (a publish-only retry, or a verified proposal the record holds). A record in a state that holds Work without recorded Work is matched by the Result's revision. `park`, `abandon`, `pause`, and `block` clear the requested Work and the pending publication without a revision bump, so an equal revision alone is not a match. Every other Result takes `record_late_effect`, in every state: the reconciler marks the publication artifact as processed and appends one event with the Result's status and task revision, and nothing else in the record changes. An artifact that is already marked as processed is not recorded again, so each late Result is recorded once.

A stop invalidates what is held for publication. `park_contribution`, `abandon`, the verbs that set `paused`, and every other transition that ends the generation clear `pending_publication` and a held proposal that is not published (`proposal_artifact` whose outcome is `proposal`) together with the requested Work; `python -m lifecycle check` verifies it. A published proposal is kept. Nothing is therefore published after a stop: the resume finds nothing held and requests fresh Work. The legacy `park`, `abandon`, `pause`, and `block` paths in `compat.py` do the same, and so do the controller's block on a changed upstream identity and the reconciler's park after exhausted Work retries. The command that ends such a stop clears a held proposal as well. The publication Result that arrives afterwards takes `record_late_effect`. No publication is launched for a paused or blocked record: the publish-only retry and the publication of a held verified proposal both skip it. A stop therefore never keeps `/upstreaming resume` or `/upstreaming restart` from applying because of a publication that was pending when it was taken.

A branch that the worker run already pushed is recorded by head observation, which reads the fork branch in the `development` and `upstream_review` phases and in `handoff.awaiting_submission`, also while the record is paused or blocked: the observed head replaces the recorded one and the revision is bumped. A parked, abandoned, or closed record is not observed. Its recorded head stays as it was, and the pushed commit stays on the fork branch; a restart begins a new generation that records the fork and its head again at provisioning. The verb is taken through `compat.record_late_effect`, which uses the engine for a table state and writes the same event for a state of `LEGACY_STATES`.

In the other phases the controller, the reconciler, and the collector still decide state changes in their own code. They write the lifecycle fields through `compat.py`, a compatibility shim with two registries: `LEGACY_SITES` names each call site that still writes state outside the table, and `LEGACY_STATES` lists the states those call sites produce that the table does not have (for example `development.idle` and the capacity waits). The shim refuses any write that enters the handoff phase, changes a record that is in it, or leaves a record in a state that is in neither the table nor `LEGACY_STATES`. No module other than `engine.py` and `compat.py` assigns a lifecycle field; a test scans the source for such assignments.

The final check before a fork write (`lifecycle.publication_check`) reads the table for a record in a handoff state: an automated writer may write the fork only in a handoff state the table marks as writing. At the submission gate it may not.

State validation applies the same rule to committed records: a contribution is in a table state or in a state of `LEGACY_STATES`, and a handoff wait carries a pending action with which the table enters that wait.

## Requesting upstream submission

`handoff.awaiting_submission` is the gate at which a human requests upstream submission. The tracking-issue entry for `nominate_for_handoff` states the exact head on which CI passed and shows the command to post:

```text
/upstreaming submit-upstream revision=N head=<full head SHA>
```

The grammar requires the revision and the full 40-character head. The engine accepts the command exactly as the table defines `submit_upstream`: the revision is the current one, the head is the candidate head recorded in `handoff_request` and still the fork head, and the contribution is not paused. A resume from a pause bumps the revision and keeps the candidate head, so the command then names the new revision and the same head. Outside the handoff phase the command is rejected with a reason that names the state the contribution is in and `handoff.awaiting_submission`, the state the command applies in.

`submit_upstream` leads to `handoff.cleaning_debug` and authorizes the deterministic handoff preparation: debug cleanup, CI on the clean head, closing the development PR, and the submission package. The controller does not run those operations. A contribution taken into `handoff.cleaning_debug` would stay there, so the controller holds the command (`engine.HELD_COMMANDS`): it checks the command against the table, so a wrong revision or head gets its precise reason, and then rejects it with the statement that nothing was changed. The tracking-issue entry that asks for the command says so, and the contribution stays in `handoff.awaiting_submission` with its fork, development PR, head, and evidence.

The commands `request-handoff`, `approve-handoff`, and `register-upstream-pr` are retired. Each is rejected with a message that names `submit-upstream`.

## Record migration

Before the lifecycle table, passing CI moved a contribution to the phase `handoff` with the activity `waiting_human` and the pending action `approve_handoff`. The table has no such wait. The controller rewrites such a record itself, before it does anything else in a tick:

- The migration is bound to one exact shape: the old approval wait with no requested Work, no open operation, no pending publication, no approval, no upstream PR, not paused, no pending actionable feedback, the candidate head equal to the fork head, the request for the current revision, and CI evidence that states that CI passed on that head. A handoff record in any other shape that is not a table state has no migration: the controller stops with an error and writes nothing.
- The new record is what `nominate_for_handoff` gives for the same CI evidence: `handoff.awaiting_submission`, the pending action `submit_upstream`, and no clean head (the clean head is produced by the debug cleanup). The revision, the fork, the development PR, the head, the dossier, the CI evidence, the budgets, and the replay fences are kept.
- The record and one appended event of kind `handoff_wait_migrated` are the only paths of that state commit; the tick requests no effect. The state publisher's final check accepts the commit and refuses any commit that writes the old shape again.
- The migration is idempotent: a migrated record is in a table state and is not selected again.

The tracking-issue entry for the migration event states that the fork, development PR, head, revision, and CI evidence are unchanged, that the earlier request to run `approve-handoff` no longer applies, and shows the `submit-upstream` command for the exact revision and head.

## Checks

`tests/test_engine.py` derives its cases from the table: every row is taken through the engine with its guards established and its effects verified, every row is refused when one of its facts is missing, every command row that requires the exact revision refuses another one, and the mask of the `paused` flag is verified for every row. `tests/test_live_handoff_migration.py` runs the migration and the ordinary controller on a copy of a committed record in the old approval wait.
