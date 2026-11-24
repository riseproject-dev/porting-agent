---
title: Home
nav_order: 1
permalink: /
---

# RISE porting agent

[`riseproject-dev/porting-agent`](https://github.com/riseproject-dev/porting-agent) is the trusted control plane for AI-assisted RISC-V porting work. It helps upstream projects of any language build, test, and release for Linux `riscv64` through their own build, test, and release processes; Python wheels are one case among others.

A request starts as an issue labelled `upstreaming-request` in the control repository. The controller records the request in committed lifecycle state under `.state/`, runs planning that produces a verified plan, provisions a fork in `riseproject-upstreaming`, and drives bounded development until continuous integration passes on the exact head.

A human then requests submission with `/upstreaming submit-upstream`. Trusted workflows check a rewrite of the series with the debug commits removed, build the `riscv64` branch, check CI on the clean head, and post a submission package with a prefilled pull request link on the tracking issue. The human opens the upstream pull request under their own identity, and the controller verifies it by observation.

## Where to start

| If you want to | Read |
|:---------------|:-----|
| Request a port, or follow one | [Pipeline]({{ '/pipeline/' | relative_url }}), starting with [Requests]({{ '/pipeline/requests/' | relative_url }}) |
| Operate the repository: settings, workflows, recovery, the website | [Operations]({{ '/operations/' | relative_url }}) |
| Audit what holds a credential and what is isolated | [Security Model]({{ '/security/' | relative_url }}) |
| See where every contribution stands today | [Status]({{ '/status/' | relative_url }}) |

## How the pipeline is built

- `.state/` on `main` is lifecycle authority. Only schema-valid state may be published, in a plain fast-forward commit. See [State Publication]({{ '/security/state-publication/' | relative_url }}).
- Pull-request validation is read-only and receives no App private key, destination token, or package credential. See [Credentials]({{ '/security/credentials/' | relative_url }}).
- The planner, the worker, and the reviewer run as pinned agentic workflows. None receives a destination credential; trusted jobs verify and publish their results. Every development patch is reviewed by the reviewer, on its own model, before it is published. See [Agent Isolation]({{ '/security/agents/' | relative_url }}).
- The [status page]({{ '/status/' | relative_url }}) is regenerated from committed state whenever that state or these pages change.

## Pages

| Page | Contents |
|:-----|:---------|
| [Status]({{ '/status/' | relative_url }}) | Current state of every contribution, generated from committed lifecycle state, with each contribution's event log. |
| [Pipeline]({{ '/pipeline/' | relative_url }}) | The path of a contribution from request to upstream review: requests, planning, contribution guidelines, development, patch review, reports and history, submission and handoff. |
| [Operations]({{ '/operations/' | relative_url }}) | Repository setup, the workflows and how they are owned, the reproduction image, recovery and retries, and the website. |
| [Security Model]({{ '/security/' | relative_url }}) | Separation of authority, credentials by workflow, agent isolation, state publication, and the treatment of untrusted input. |
| [State Machine]({{ '/state-machine/' | relative_url }}) | The contribution lifecycle, generated from the lifecycle table in `lifecycle.py`: the [Lifecycle Table]({{ '/state-machine/table/' | relative_url }}), the [decisions]({{ '/state-machine/decisions/' | relative_url }}) behind it, the [Transition Engine]({{ '/state-machine/engine/' | relative_url }}) that applies it to a record, and the [Known Gaps]({{ '/state-machine/known-gaps/' | relative_url }}) between the code and the table. |

## Contributing to these pages

The pages are the Markdown files under [`docs/`](https://github.com/riseproject-dev/porting-agent/tree/main/docs). A pull request from a branch of this repository that changes them gets a preview, described on the [Website]({{ '/operations/website/' | relative_url }}) page.
