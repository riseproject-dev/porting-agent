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

Every terminal path of a trusted workflow explicitly continues `controller.yml`: the dispatcher, planner, worker, and reviewer paths, and the `continuation` job of provisioning, development PR, publish retry, review comment, and handoff. Workflow completion events and the schedule are fallbacks if a continuation is lost. Scheduled runs can themselves be delayed or skipped, so a fallback is not a guarantee of timing.

## Failing closed

Missing Work, unavailable queue state, invalid research, malformed artifacts, and exhausted dispatch retries fail closed. None of them fabricates Completion, a Result, or state.

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

## Repairing tracking-issue output

Human-facing ledger entries, failure replies, and terminal reactions scan durable state and hidden markers. A failure after the state commit is repaired by a later tick without duplicate comments.

## Where the details are

- [State Publication]({{ '/security/state-publication/' | relative_url }}) covers retries when `main` moves.
- [Errors while a tick runs]({{ '/state-machine/engine/' | relative_url }}#errors-while-a-tick-runs) covers how an error in one contribution pauses only that contribution.
- [Remote operations]({{ '/state-machine/engine/' | relative_url }}#remote-operations) covers operation receipts, discovery, and resolving an operation by hand.
