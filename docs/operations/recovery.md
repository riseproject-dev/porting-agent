---
title: Recovery and Retries
parent: Operations
nav_order: 4
permalink: /operations/recovery/
---

# Recovery and Retries
{: .no_toc }

The pipeline is built to continue after a failure without duplicating a remote effect and without inventing a result. This page summarizes how.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## Continuations and fallbacks

Every terminal path of a trusted workflow explicitly continues `controller.yml`: the dispatcher, planner, and worker paths, and the `continuation` job of provisioning, development PR, publish retry, and handoff. Workflow completion events and the schedule are fallbacks if a continuation is lost. Scheduled runs can themselves be delayed or skipped, so a fallback is not a guarantee of timing.

## Failing closed

Missing Work, unavailable queue state, invalid research, malformed artifacts, and exhausted dispatch retries fail closed. None of them fabricates Completion, a Result, or state.

## A dispatch that failed for the pipeline

A dispatch that never reached the queue is a failure of the dispatch path, never of the contribution. The controller does not request Work whose task it cannot commit and queue: it holds the request with an alert, charges nothing, and checks again in every tick. A contribution that waits for a human with `dispatch_failed` is resumed by the controller once the pipeline code changes (once per version of the code, which the next failure records), gives back the budgets the lost request charged, and requests the Work again: no human resume is needed, and `/upstreaming resume` still works.

## An interrupted planning record job

If the trusted record job of a planning run is interrupted after queue Result resolution, its partial `porting-publication` still contains the trusted task, the exact Result receipt, the plan, and the proposal. Ordinary controller collection:

1. revalidates all four;
2. requires the original planner run, attempt, and authorized artifact identity;
3. reconstructs only the missing deterministic outcome;
4. retries the idempotent exact-Result comment.

It never resets Work, fabricates a Result, or reruns the planner.

## A dispatcher that fails after an accepted dispatch

A dispatcher workflow may conclude `failure` after gh-aw reports `recovery_required`, even though the native workflow dispatch was accepted. For example, the durable ledger may record one accepted worker launch while an earlier control intent stays blocked, so safe-output processing fails closed and the worker still starts.

This is expected self-healing, not proof that nothing launched. Controller replay treats a live Claim as authoritative and neither releases nor duplicates it. Explicit continuation and the schedule fallback reconcile the terminal result.

## Deploying the review loop

The worker reviews its own patch, so the states of the separate review (`development.review_pending`, `development.reviewing`, `development.publishing`, `handoff.review_pending` and `handoff.reviewing`) and their record fields (`held_patch`, `review_comments`, `budget.review_rejections`, `submission_review_id`) no longer exist. The record migration (`state.migrate_record_fields`, one `record_fields_migrated` event per record) drops `held_patch` and `review_comments` whatever they hold, moves a record in a removed development state to `development.fix_needed` with no Work and no held publication Result (the next patch is requested again) and a record in `handoff.review_pending` or `handoff.reviewing` to `handoff.review_due` with no rewrite (the rewrite is made again), drops `submission_review_id` whatever its value, and turns the pending action `patch_review_unavailable` into `work_failed`. It refuses nothing, so a store with such records is migrated rather than stopped. The unpublished patch of a held record, and the accepted rewrite of a record in a removed handoff state, are lost: their Work is requested again. Before merging the change, confirm that no publish-only retry of a held patch is in flight.

A worker run that began before the deploy returns a Result the controller reads as Work without a usable Result of the earlier pipeline and relaunches, with nothing charged.

The installed queue Policy still lists the `reviewer` profile until the dispatcher installs the compiled Policy, which no longer has it. The dispatcher reinstalls it only on a quiescent queue (no live Claim), so after the deploy a dispatch is refused, and the request waits with `dispatch_failed`, until that reinstall has happened; `rearm_dispatch` then re-arms the dispatch and gives back the budgets the lost allocation charged. Expect the first dispatches after the deploy to wait for the queue to drain.

## Rejections of the changed files contradiction

The worker gate and the fork publisher once required different sets for the `changed_files` of a rewrite, so a rewrite whose series touched a file already on the branch was rejected by one of them whatever list the worker gave (`mbox paths do not match proposal changed_files` from the gate, `proposal claims files [...] but series changes [...]` from the publisher). Each rejection was a `reject_patch_proposal` that charged `no_progress`, and two of them parked `luhenry--d2vm/3` with "repair budgets exhausted: no_progress, after the automatic refresh". `changed_files` is now the net delta from `expected_head` for both checks.

Nothing is edited by hand: the first controller tick after the deploy runs the record migration `state.migrate_rejection_refund` in its own state-only commit, before anything else. It refunds the `no_progress` charges of these rejections that were recorded before the cutoff `engine.CONTRADICTION_CUTOFF` (the publisher still emits the second reason, and after the deploy it is a genuine mistake that `no_progress` limits) and still count in the record's streak, returns the automatic refresh that earlier rejections of the same kind used up (`automatic_refresh_used` is cleared), and returns a record parked for exhausted repair budgets, whose budgets remain after the refund, to `development.fix_needed` with no Work; the next tick requests repair Work. It appends one `record_rejections_refunded` event naming the refunded event sequences (and the refresh it returned) and refunds none of them again. Before the deploy, `python -m state validate` lists the record as awaiting a migration. After it, check the event, the record in `development.fix_needed` with `budget.no_progress_streak` 0, and that the following tick took `request_patch`. A record that is still exhausted after the refund (other rejections charged the streak too) stays parked, with its refund recorded: `/upstreaming refresh` restarts it.

## Repairing tracking-issue output

Human-facing ledger entries, failure replies, and terminal reactions scan durable state and hidden markers. A failure after the state commit is repaired by a later tick without duplicate comments.

## Where the details are

- [State Publication]({{ '/security/state-publication/' | relative_url }}) covers retries when `main` moves.
- [Errors while a tick runs]({{ '/state-machine/engine/' | relative_url }}#errors-while-a-tick-runs) covers how an error in one contribution pauses only that contribution.
- [Remote operations]({{ '/state-machine/engine/' | relative_url }}#remote-operations) covers operation receipts, discovery, and resolving an operation by hand.
