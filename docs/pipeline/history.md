---
title: Reports and History
parent: Pipeline
nav_order: 6
permalink: /pipeline/history/
---

# Reports and History
{: .no_toc }

Each worker reports what it attempted, and every task receives the contribution's history. Together they let a repair start from what was already tried instead of from nothing.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## The worker report

Every patch, repair, revision, and submission rewrite task also uploads `report.json` in its `porting-out` artifact. It follows `schemas/worker_report.schema.json` and is at most 32 KiB. It records:

- what the worker attempted and why;
- each approach and its outcome;
- what failed, with its evidence;
- what remains open;
- the verification done.

## When a report is required

The worker gate and the submission checks (`--report`) require the report whenever the task carries `context.history`. It must name the contribution, revision, task, and the digest of the history the task carried. The publish job and the publish-only retry pass it on in `porting-publication`.

## How reports are stored

- The reconciler stores the report as `worker_report` evidence only when the Result is accepted by a success row, and checks the bytes of the file as the gate did.
- A success Result of a task that carries a history, whose report cannot be stored, pauses that one contribution (`pause_on_processing_error`, naming the task and the reason) instead of being folded without it. It never stops the tick for the others.
- A Result that arrives late although its push landed stores its report too, so the repair of a failing head reads what was tried on it.
- The report of a failed, rejected, or blocked worker is discarded.

Two [Known Gaps]({{ '/state-machine/known-gaps/' | relative_url }}) concern reports: [an accepted Result whose report cannot be stored]({{ '/state-machine/known-gaps/' | relative_url }}#an-accepted-result-whose-report-cannot-be-stored-pauses-the-contribution), and [a publication job lost after its push]({{ '/state-machine/known-gaps/' | relative_url }}#a-publication-job-lost-after-its-push-leaves-no-report).

## The contribution history

The task of every task, planning included, carries `context.history`:

- the repository and the pinned control commit, which the reference to the task file names;
- the path, `.state/evidence/history-<32 hex>.json`;
- the SHA-256 and the evidence id of the `work_history` evidence the controller recorded when it allocated the task.

A trusted pre-agent step fetches the file at that commit, verifies it, and writes `/tmp/gh-aw/agent/history.json`. It holds:

- the owner's steering (below);
- the chronological reports, CI outcomes, reviews, and submission verdicts of the contribution.

## Steering

Steering is the `human_steering` evidence of:

- the tracking issue description edits and comments;
- the development and upstream PR reviews and comments;

by accounts in the command authority. The reviews of upstream maintainers are included as feedback. See [Worker reports, history and steering]({{ '/state-machine/engine/' | relative_url }}#worker-reports-history-and-steering) in the transition engine.

## Treated as untrusted

All of it is untrusted data: redacted, bounded, never rendered on the tracking issue or the status page, and unable to widen any path, operation, or credential. See [Untrusted Input]({{ '/security/untrusted-input/' | relative_url }}).
