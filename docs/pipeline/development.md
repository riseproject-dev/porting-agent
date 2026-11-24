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
- bounded proposal evidence.

The worker holds no destination credential and cannot push. See [Agent isolation]({{ '/security/agents/' | relative_url }}).

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

## The worker gate

Before any token, the trusted worker gate validates the Result's structural scope, its debug commits, and its reproduction. It has no destination token and no publication path of its own. The [commit rules]({{ '/pipeline/contribution-guidelines/' | relative_url }}#how-the-rules-are-enforced) are part of the gate.

## Publication

A separate trusted publish job publishes the verified proposal. It:

- reconstructs the proposal in a fresh repository;
- enforces scope and workflow safety;
- persists operation evidence;
- pushes under an exact-head lease.

A development patch is first held for review, and its publish job mints no token; see [Patch review]({{ '/pipeline/patch-review/' | relative_url }}). A repair worker's report of a failure not caused by the patch is published by rerunning the failed jobs on the exact head, with a one-fork token that carries only `actions: write`.

## Development PR and CI

`dev-pr.yml` creates or discovers one marked same-fork pull request, bootstraps inherited Actions safely, and executes the CI reruns the collector records for interrupted CI. The controller observes CI on the exact head. The [transition engine]({{ '/state-machine/engine/' | relative_url }}#development) lists the rows, and [CI reruns]({{ '/state-machine/engine/' | relative_url }}#ci-reruns) describes how reruns are requested and read back.

## Infrastructure blockers

Exact Docker-infrastructure blockers are normalized into durable rehearsal evidence and charged to the bounded `no_progress` budget. The fresh Work revision that follows is charged to the attempt budget when it is allocated.
