---
title: Operations
nav_order: 4
permalink: /operations/
has_children: true
---

# Operations

This section is for the people who run the porting agent.

## What to watch

- The [status page]({{ '/status/' | relative_url }}) shows every contribution and its event log.
- The controller's `reconcile` job writes its report, with the alerts, to its step summary under "Porting reconciliation" and uploads it as part of the `porting-control-receipts` artifact. Alerts call out work that needs a person, such as a development PR that lacks its review comment. It is the controller's only reporting job: there is no separate read-only report.
- Contributions that wait for a human carry a pending action on their tracking issue, and the controller posts [reminders]({{ '/state-machine/engine/' | relative_url }}#reminders).

## Pages

| Page | Contents |
|:-----|:---------|
| [Repository Setup]({{ '/operations/setup/' | relative_url }}) | Reviewed repository settings, bootstrap resources, and the canonical request. |
| [Workflows]({{ '/operations/workflows/' | relative_url }}) | Every workflow, who owns it, and what renaming one involves. |
| [Reproduction Image]({{ '/operations/reproduction-image/' | relative_url }}) | The `riscv64` image the worker reproduces builds in. |
| [Recovery and Retries]({{ '/operations/recovery/' | relative_url }}) | How the pipeline continues and recovers after a failure. |
| [Website]({{ '/operations/website/' | relative_url }}) | How these pages and the status page are built, previewed, and deployed. |

## Repository layout

Root Python modules own trusted control behavior. There is no package wrapper and no `src/` tree.

- Schemas are under `schemas/`, operational utilities under `scripts/`, and tests under `tests/`.
- Commands live in their owning modules. There is no aggregate entry point.
- `policy.json` is the reviewed policy. Its `read_only` flag stops the controller from publishing state or launching any workflow.

For the commands that validate a change locally, see the [README](https://github.com/riseproject-dev/porting-agent#local-validation).
