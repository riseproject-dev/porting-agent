---
title: Home
nav_order: 1
permalink: /
---

# RISE porting agent

[`riseproject-dev/porting-agent`](https://github.com/riseproject-dev/porting-agent) is the trusted control plane for AI-assisted RISC-V porting work. The active profile helps upstream Python projects build, test, and publish Linux `riscv64` wheels through their own release processes.

A request starts as an issue labelled `upstreaming-request` in the control repository. The controller records the request in committed lifecycle state under `.state/`, runs an investigation that produces a verified dossier, provisions a fork in `riseproject-upstreaming`, and drives bounded development until continuous integration passes on the exact head. A human then submits the contribution upstream under their own identity.

## How the pipeline is built

- `.state/` on `main` is lifecycle authority. Only schema-valid state may be published, in a plain fast-forward commit.
- Pull-request validation is read-only and receives no App private key, destination token, or package credential.
- The investigator and the worker run as pinned agentic workflows. Neither receives a destination credential; trusted jobs verify and publish their results.
- The [status page]({{ '/status/' | relative_url }}) is regenerated from committed state whenever that state or these pages change.

## Pages

| Page | Contents |
|:-----|:---------|
| [Status]({{ '/status/' | relative_url }}) | Current state of every contribution, generated from committed lifecycle state. |
| [Repository setup]({{ '/setup/' | relative_url }}) | Reviewed repository settings, bootstrap resources, and the canonical request fixture. |
| [Integration contract]({{ '/integration/' | relative_url }}) | Ownership of lifecycle state, queue state, tracking issues, and remote operation receipts. |
| [Investigation contract]({{ '/investigation/' | relative_url }}) | What the investigator researches and how its dossier is validated. |
| [State machine]({{ '/state-machine/' | relative_url }}) | The contribution lifecycle state machine, generated from the lifecycle table in `lifecycle.py`, with the decisions behind it and the items still open. |
| [Migration inventory]({{ '/migration/' | relative_url }}) | How the earlier `python-wheels` implementation maps onto this repository. |

## Contributing to these pages

The pages are the Markdown files under [`docs/`](https://github.com/riseproject-dev/porting-agent/tree/main/docs). A pull request from a branch of this repository that changes `docs/` gets a preview at `https://riseproject-dev.github.io/porting-agent/pr/<number>/`, linked from a comment on the pull request. The preview is removed after the pull request is closed.
