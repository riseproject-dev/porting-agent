---
title: Planning
parent: Pipeline
nav_order: 2
permalink: /pipeline/planning/
---

# Planning
{: .no_toc }

Planning turns a frozen request into a verified plan. The planner is an agentic workflow that researches the project and reports; trusted code validates what it returns and decides what happens next.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## What the planner researches

The planner researches:

- upstream history and contribution policy;
- build and test topology and release mechanics;
- dependencies and prior architecture work;
- the complete tracking issue and its comments, and issue and pull request history including reviews;
- CI and release topology, maintainers and contribution rules, project reports, and the project graph;
- the riscv64 availability of the project's dependencies in its ecosystem, and for a Python project the public and RISE dependency-wheel evidence;
- the project's [contribution guidelines]({{ '/pipeline/contribution-guidelines/' | relative_url }}).

## The project record

The project may be written in any language and is planned on its own build, test, and release tooling. The plan records it in `project`: its languages, build systems, how it builds and tests, its release channels with whether each already serves riscv64, and the state of its toolchain on linux/riscv64.

## Dependency audit

The audit covers every build and test dependency and every tool the CI downloads, in the project's ecosystem:

- Go modules and `GOARCH`
- Rust targets and `-sys` crates
- C and C++ libraries
- JDK and JNI natives
- JavaScript native addons
- container base images

A Python project is also audited against public PyPI and the RISE index. That a project has no Python package, wheel, or PyPI release is never a reason to park.

## How planning is dispatched

1. The controller selects a queued contribution and commits an immutable `active_work` request.
2. It launches `dispatcher.lock.yml`.
3. Trusted dispatcher preparation replays that exact request into the native queue. The model never selects or mutates Work.
4. Each dispatcher run admits at most one Claim, for the planner, the worker, or the reviewer profile (`dispatch-workflow` with `max: 1`). Planning dispatches the planner profile through gh-aw native Work.

A requested task that cannot be replayed is reported and skipped, so it never blocks the dispatch of another contribution. That happens when its upstream identity or base is not bound, or when it has no frozen tracking-issue description.

## Queue policy

Before it stages any Work, the dispatcher compares the compiled queue policy of its lock with the Policy installed in the ledger. When the compiled policy drops an installed worker profile, which gh-aw refuses on every dispatch, and the queue is quiescent, the dispatcher publishes the compiled policy as a new Policy epoch through an administrator `policy` request authenticated as its own run.

The `reviewer` profile is added to the compiled policy, which gh-aw installs on demand without a Policy reinstall. A rollback that drops it is reinstalled by the dispatcher on a quiescent queue.

## What the planner reads

Its Work carries a history (`context.history`), which a trusted pre-agent step fetches at the pinned commit and checks. See [Reports and History]({{ '/pipeline/history/' | relative_url }}). The planner reads, in order:

1. The owner's steering: edits of the tracking issue description and comments by accounts in the command authority. This is how the owner steers planning.
2. The tracking issue comments, as untrusted research.

The frozen description stays the plan's `tracking_issue_description_digest`. The planner holds no destination credential; see [Credentials]({{ '/security/credentials/' | relative_url }}#agents).

## The plan's decision

The plan's proposal decides one of three outcomes:

| Decision | Meaning |
|:---------|:--------|
| `develop` | A bounded patch, with its admission class and allowed paths. |
| `park` | Work stops. A human can restart it with `/upstreaming refresh`, optionally with an admission override. |
| `close` | Final. Only for a project that needs no riscv64 work. |

A `close` is only for a project released on PyPI whose latest release of every requested distribution already installs on riscv64 from PyPI: a riscv64 wheel, or pure-Python wheels only. PyPI is the one registry a close is confirmed from.

- The trusted gate refuses a close without a PyPI release channel, with a release channel that lacks riscv64, or without a recorded package.
- The controller confirms the close from PyPI itself, requires each PyPI project to name the upstream repository in its metadata, and rejects a close that PyPI does not confirm.
- A project of another ecosystem that already serves riscv64 is parked with that evidence.

The [Transition Engine]({{ '/state-machine/engine/' | relative_url }}#the-plan-decides) lists the row each case takes.

## Plan schema versions

| Version | What it records |
|:--------|:----------------|
| 1 | No contribution guidelines. |
| 2 | Contribution guidelines, but no `project`. |
| 3 | The project, but no series requirements. |
| 4 | Series requirements. Required for every new plan. |

Plans stored under an older version stay readable.

## Validation and publication of a plan

- One `porting-out` artifact holds a schema-valid plan and proposal, bound to the task, contribution, revision, tracking-body digest, and complete path coverage.
- Trusted pre-safe-output validation runs before native Completion.
- A separate trusted record job resolves the exact Claim's verified Result from the gh-aw ledger, downloads only its authorized artifact, and revalidates it. It then publishes a bounded App-authored plan comment and returns a `porting-publication` artifact for deterministic reconciliation.

## When planning fails

A failed or interrupted planning run fails closed and is recovered by the controller. See [Recovery and Retries]({{ '/operations/recovery/' | relative_url }}).
