---
title: Migration inventory
nav_order: 6
permalink: /migration/
---

# Migration inventory

This repository replaces the earlier implementation in `riseproject-dev/python-wheels`. Commit `c21e458f67541af8b6e988eda7a20e30fbad201d` of that repository is frozen as the proven implementation reference. `migration-inventory.json` assigns every regular file under its `upstreaming/` directory to one target path in this repository, or records an explicit removal. `workflow-migration.json` separately records every control workflow rename and its owner.

The inventory is a completeness record: it shows where each piece of the earlier implementation now lives. It does not by itself authorize a workflow or a remote effect; those are enabled only by the reviewed workflows described below.

Earlier implementation evidence: [the frozen `upstreaming/` tree](https://github.com/riseproject-dev/python-wheels/tree/c21e458f67541af8b6e988eda7a20e30fbad201d/upstreaming) and [its workflow directory](https://github.com/riseproject-dev/python-wheels/tree/c21e458f67541af8b6e988eda7a20e30fbad201d/.github/workflows) are historical sources only.

## Repository layout

The control-plane Python package is flattened into repository-root modules, schemas are rooted at `schemas/`, operational utilities are rooted at `scripts/`, and the proven local and read-only tests are rooted at `tests/`. Aggregate commands moved to their owning modules; there is no replacement aggregate entry point.

`test-deferrals.json` records every proven test case that was carried over with a workflow prerequisite, together with the workflow that restores it. Every recorded deferral is restored, and the full proven test suite runs in validation.

## Control and website workflows

`controller.yml` owns the controller and `website.yml` owns the public site. Lifecycle authority is `.state/`; publication is a bounded plain fast-forward under `protect-main-history`, with no bypass or force path. Tracking issue ledgers, deterministic failure replies, and terminal reactions are recoverable from durable events and command receipts.

Flattened artifact extraction resolves control state through `common.repository_root()` rather than package-depth arithmetic. Trusted controller collection can recover an investigation record job interrupted after exact queue Result resolution by revalidating the partial trusted publication; it does not reset or resubmit Work. Pages deployment waits until the uploaded artifact is visible through the same Actions Results lookup the official deploy action uses, then deploys; a failed deployment is retried up to three more times after fixed waits of 15, 45 and 90 seconds, each followed by the same bounded readback, and the job fails with an explicit error after the last attempt.

## Investigation

`dispatcher.md` and `investigator.md` own native Work admission and authenticated read-only research. Their generated locks are compiler-owned outputs of pinned gh-aw v1.2.2. The controller automatically selects a queued contribution, commits its immutable task, launches the dispatcher, folds only an exact verified investigation Result, and recovers through explicit continuations plus schedule and workflow-completion fallbacks.

## Reproduction image and worker

`image.yml` and `image/` own the exact-base, linux/riscv64 reproduction image and anonymous exact-digest verification. `worker.md` joins the native queue with its normalized generated lock, pinned QEMU setup, pinned `gh-act`, stop-command isolation, immutable dossier readback, selected build and test reproduction, and bounded proposal evidence. The trusted worker record job performs structural scope, debug, and reproduction validation; it has no destination token or publication path of its own.

The published reproduction image is intentionally riscv64-only. Every Docker pull and local image inspection passes `--platform linux/riscv64`; base-index inspection selects exactly one Linux riscv64 manifest before extracting its digest. Dockerfiles rely on that exact manifest plus Buildx `platforms: linux/riscv64`, so they carry no constant `FROM --platform` flag.

The strict AWF sandbox cannot reach the host Docker daemon, so the worker uses the same operator-approved gh-aw v1.2.2 unsandboxed-agent mechanism as investigation. A hash-pinned Pi provider patch captures Anthropic authentication and deletes it from the process environment before tools start, and a custom Pi command wrapper removes and verifies GitHub, App, destination, Actions, cloud, and SSH credentials. Trusted MCP servers retain separately scoped read-only tokens; Docker/QEMU and `gh-act` receive no repository or destination credential. Exact Docker-infrastructure blockers are normalized into durable rehearsal evidence, charged to bounded attempt and no-progress budgets, and retried as a fresh Work revision after fork provisioning. A failed dispatcher conclusion with an already live queue Claim remains fenced rather than duplicated.

## Destination effects

Destination effects are enabled only through reviewed trusted workflows. `provision.yml` separates repository-creation authority from a one-fork Administration/Actions/Contents/Workflows token and verifies fork parent, IDs, settings, workflow registration, and exact `riscv64` base. Worker and retry publication reconstruct proposals in a fresh repository, enforce scope and workflow safety, persist operation evidence, and push under an exact-head lease. `dev-pr.yml` creates or discovers one marked same-fork PR and bootstraps inherited Actions safely. The controller collects exact receipts, observes exact-head CI, and retries bounded durable launches.
