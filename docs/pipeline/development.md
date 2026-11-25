---
title: Development
parent: Pipeline
nav_order: 4
permalink: /pipeline/development/
---

# Development
{: .no_toc }

Development turns a verified plan into a patch that passes CI on the fork. The worker model proposes; trusted workflows provision, verify, publish, and observe.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## Fork provisioning

`provision.yml` creates or verifies the contribution's fork in `riseproject-upstreaming` with two separate tokens:

1. A creation-only token, minted only when the fork is absent.
2. A token for the one fork, with Administration, Actions, Contents, and Workflows write.

It initializes the exact `riscv64-dev` base from the frozen base, and verifies the fork's parent, the repository ids, the settings, workflow registration, and that base. A definite failure uploads a failure receipt that sends the contribution to a human at once.

## The worker

`worker.md` joins the native work queue and runs uncredentialed. It uses:

- a normalized generated lock;
- pinned QEMU and an optional pinned `gh-act`;
- a read back of the immutable plan;
- direct build and test reproduction in the [reproduction image]({{ '/operations/reproduction-image/' | relative_url }});
- the review loop of its patch;
- bounded proposal evidence.

The worker holds no destination credential and cannot push. See [Agent Isolation]({{ '/security/agents/' | relative_url }}).

## The review loop

The worker reviews its own patch before trusted code publishes it, in the same run, with a review loop that its prompt describes and that runs on the `subagent` tool of `pi-subagents`. Development, review and fixes are one workflow; there is no separate review task and no unpublished patch.

Once the series is committed and validated in the image, the worker runs the loop its prompt describes. The worker is the controller and the only decision-maker; its children only review or implement, one writer at a time, always in the foreground.

- **Rounds.** At most 3. Each round launches fresh reviewers, in parallel and in the foreground, on the worker's model with read, grep, find and ls only. They read the series (`format-patch` output, which holds the commit messages), the tree after it, the task, the plan and the history. Angles: correctness on riscv64, scope and precedent, project conventions and reviewers' expectations, tests and CI coverage, security, and whether the report matches the series.
- **Findings.** Reviewers label findings P0 (blocks), P1 (fix before release) or P2 (report only). After a round the worker fixes what a P0 or a worthwhile P1 asks, through one forked `worker` child, or itself when the child fails. It starts no fix pass after 175 minutes and no round after 210 minutes of the 330 minute run, so that a review round always follows a fix pass.
- **No reviewers.** When the `subagent` tool is missing or a reviewer launch returns an error, the worker does not review the series itself: it delivers the series as usual and records `rounds` 0 and `stopped_because: "reviewers_unavailable"`.
- **Stop.** When fresh reviewers find no P0 and no P1 worth fixing, when only optional notes remain, when the cap is reached, when time runs out, or when a decision that the worker may not take is needed. In the last case the outcome is `needs_clarification` with the question.
- **Closing a P0.** Only the fresh reviewers of a later round close a P0 finding, as fixed or withdrawn. The worker never closes or drops one.

The last round must have reviewed exactly the series that is published, and nothing changes after it. The worker records the loop in `report.json` as `review_loop`: the rounds, the SHA-256 of the series each round reviewed, why it stopped, at most 10 findings with their status and what became of them, and a summary.

## The worker gate

Before any token, the trusted worker gate validates the Result's structural scope, its debug commits, and its reproduction. It has no destination token and no publication path of its own. The [commit rules]({{ '/pipeline/contribution-guidelines/' | relative_url }}#how-the-rules-are-enforced) are part of the gate.

The `changed_files` of the proposal are the net delta from `expected_head` to the series applied, for an append and for a rewrite. The gate, which has only the mbox, requires every path of the series and every claimed file to be in the approved scope, and for an append every claimed file to be a path of the mbox; the trusted publisher then requires the claim to equal `git diff --name-only --no-renames` from `expected_head` to the rebuilt candidate exactly.

A loop that records `reviewers_unavailable`, or that reports more rounds than the agent's log (`agent-stdio.log`, read by the publish job) shows successful launches of the `reviewer` agent, is no review (`review_loop.ReviewersUnavailable`): the gate writes status `unreviewed`, nothing is published and the task is run again with nothing charged (at most 3 free relaunches before each charged retry). The worker's prose is never read for this. For a patch, the gate also checks the review loop (`review_loop.check`). It is the agent's own account, so the gate checks its shape and its P0 findings, not that the reviews happened:

- at least one round ran, and the loop did not stop for a decision;
- no P0 finding is open or deferred;
- a P0 finding is closed only in a later round than the one that raised it.

A patch that fails the check is a rejected proposal: nothing is pushed, `reject_patch_proposal` returns the contribution to `development.fix_needed`, and the attempt is charged to `no_progress`. The pending feedback is kept.

## Reproduction evidence

Every patch, repair, and revision Result carries a reproduction:

- the exact image digest and `riscv64`;
- the exact commands, their exit statuses and log digests;
- the enforced limits.

The trusted worker gate accepts a `patch` or `no_change` outcome only with at least one passing direct `docker run --platform linux/riscv64` command of the exact image reference.

`gh act` is an optional aid. A separate pre-agent step installs it best effort, hash-pinned, and records `available` or `unavailable` in `/tmp/gh-aw/agent/gh-act-status`; it never fails the run. Its workflow, job, and runner-mapping evidence (`reproduction.workflows`) is recorded only when the worker used it, and then in full.

- A `gh act` failure (capacity class `act_unsupported`, advisory per `capacity.blocks_outcome`) never blocks an outcome whose direct commands passed.
- A blocked Result in a `reproduction-image-infrastructure` cluster whose only failed commands are `gh act` is refused as a validation failure, with its reason fed back, instead of being counted as an infrastructure blocker.
- Stored proposals and evidence with `workflows` and `runner_mappings` stay valid.

## Publication

A separate trusted publish job publishes the verified proposal. It:

- reconstructs the proposal in a fresh repository;
- enforces scope and workflow safety;
- persists operation evidence;
- pushes under an exact-head lease.

Once the gate accepts a patch, the publish job of the worker's run pushes it. After the push it posts the summary of the review loop as one comment on the pushed commit, which shows on the development PR; the post is made once, with no retry, and a failure is only logged. The publish-only retry of a failed push posts the same summary and finds an earlier one by its marker. A repair worker's report of a failure not caused by the patch is published by rerunning the failed jobs on the exact head, with a one-fork token that carries only `actions: write`.

## Development PR and CI

The first published patch has no development PR yet: the controller opens it once the push is confirmed, and folds the Result of the worker once the PR is recorded. `dev-pr.yml` creates or discovers one marked same-fork pull request, bootstraps inherited Actions safely, and executes the CI reruns the collector records for interrupted CI. The controller observes CI on the exact head. The [Transition Engine]({{ '/state-machine/engine/' | relative_url }}#development) lists the rows, and [CI reruns]({{ '/state-machine/engine/' | relative_url }}#ci-reruns) describes how reruns are requested and read back.

## Infrastructure blockers

Exact Docker-infrastructure blockers are normalized into durable rehearsal evidence and charged to the bounded `no_progress` budget. The fresh task revision that follows is charged to the attempt budget when it is allocated.
