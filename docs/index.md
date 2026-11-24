---
title: Home
nav_order: 1
permalink: /
---

# RISE porting agent

[`riseproject-dev/porting-agent`](https://github.com/riseproject-dev/porting-agent) is the trusted control plane for AI-assisted RISC-V porting work. It helps upstream projects of any language build, test, and release for Linux `riscv64` through their own build, test, and release processes; Python wheels are one case among others.

A request starts as an issue labelled `upstreaming-request` in the control repository. The controller records the request in committed lifecycle state under `.state/`, runs planning that produces a verified plan, provisions a fork in `riseproject-upstreaming`, and drives bounded development until continuous integration passes on the exact head. A human then requests submission with `/upstreaming submit-upstream`: trusted workflows remove debug commits, check CI on the clean head, close the development PR, and post a submission package with a prefilled pull request link on the tracking issue. The human opens the upstream pull request under their own identity, and the controller verifies it by observation.

## How the pipeline is built

- `.state/` on `main` is lifecycle authority. Only schema-valid state may be published, in a plain fast-forward commit.
- Pull-request validation is read-only and receives no App private key, destination token, or package credential.
- The planner, the worker, and the reviewer run as pinned agentic workflows. None receives a destination credential; trusted jobs verify and publish their results. Every development patch is reviewed by the reviewer, on its own model, before it is published.
- The [status page]({{ '/status/' | relative_url }}) is regenerated from committed state whenever that state or these pages change.

## Pages

| Page | Contents |
|:-----|:---------|
| [Status]({{ '/status/' | relative_url }}) | Current state of every contribution, generated from committed lifecycle state, with each contribution's event log. |
| [Repository setup]({{ '/setup/' | relative_url }}) | Reviewed repository settings, bootstrap resources, and the canonical request fixture. |
| [Integration contract]({{ '/integration/' | relative_url }}) | Ownership of lifecycle state, queue state, tracking issues, and remote operation receipts. |
| [Planning contract]({{ '/planning/' | relative_url }}) | What the planner researches and how its plan is validated. |
| [State machine]({{ '/state-machine/' | relative_url }}) | The contribution lifecycle state machine, generated from the lifecycle table in `lifecycle.py`, with the decisions behind it, and the [transition engine]({{ '/state-machine/engine/' | relative_url }}) that applies the table to a record. |
| [Migration inventory]({{ '/migration/' | relative_url }}) | How the earlier `python-wheels` implementation maps onto this repository. |

## Contributing to these pages

The pages are the Markdown files under [`docs/`](https://github.com/riseproject-dev/porting-agent/tree/main/docs). A pull request from a branch of this repository that changes `docs/` gets a preview at `https://riseproject-dev.github.io/porting-agent/pr/<number>/`, linked from a comment on the pull request. The preview is removed after the pull request is closed.
