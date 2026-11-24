---
title: Agent Isolation
parent: Security Model
nav_order: 2
permalink: /security/agents/
---

# Agent Isolation
{: .no_toc }

Three agentic workflows run models: the dispatcher, the planner, and the worker. Each has a different amount of isolation, chosen for what it needs to do. None holds a destination credential; trusted jobs verify and publish their results.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## Each agent

| Agent | Sandbox | Network | Tools | Credentials |
|:------|:--------|:--------|:------|:------------|
| Dispatcher | gh-aw strict sandbox | None allowed | No shell, no file writes, no web access, no extension. The one locked-down agent. | Queue token for the Policy reinstall, in a trusted step |
| Planner | None. Operator-approved unsandboxed agent, for research. | Unrestricted | Pi's tools and extensions, with the read-only GitHub MCP server and the project graph | Scoped read-only `GITHUB_TOKEN` |
| Worker | None. Operator-approved unsandboxed agent: the strict sandbox cannot reach the host Docker daemon. | Unrestricted | Pi's tools and extensions, with the read-only GitHub MCP server, Docker and QEMU, and the optional `gh-act` | None; see below |
| Worker review children | The worker's own process, in the foreground | Inherited from the worker | Pi's `read`, `grep`, `find` and `ls` only | None; the provider and the credential-free environment of the worker |

The reviewers are children of the worker, not a separate agent: the worker launches them as sub-agents of its own process, so they run unsandboxed like it, with read-only tools and no destination credential.

## Worker credential separation

The worker runs unsandboxed, so credentials are removed rather than fenced off.

- A hash-pinned gh-aw Pi provider patch (`scripts/pi_provider_credential.py`) captures `ANTHROPIC_API_KEY` into the registered provider configuration and deletes it from `process.env` before any tools are exposed. Pi can authenticate, while bash, Docker, and `gh-act` cannot inherit the key.
- A custom engine wrapper, `pi-scrubbed`, strips and verifies the absence of GitHub, App, destination, Actions, cloud, and SSH credentials before Pi starts. The `gh-act` wrapper unsets them as well.
- Trusted MCP servers keep separately scoped read-only tokens, as separate processes.
- Docker and QEMU, and the optional `gh-act`, receive no repository or destination credential.

## The worker's review children

The worker is set up to run `pi-subagents` children in its own Pi process, in the foreground. A foreground child inherits the registered provider and the credential-free environment of the parent, and anything its `bash` runs is a subprocess of the same process; a background child would be a detached runner without either, so the trusted install step turns background launches, workflow scripts and nested subagents off. The pi-subagents configuration, written by that step, also replaces the 30 minute backstop of a foreground child with 40 minutes.

Two builtin agents are overridden in Pi's settings (`engine.config`). The `reviewer` child inherits the parent's model and has only `read`, `grep`, `find` and `ls`; the same step installs the pinned `ripgrep` and `fd-find` that `grep` and `find` run. The `worker` child keeps the parent's model and tools, without `contact_supervisor`, which has nobody to answer in a run with no user.

A separate trusted publish job, never the model, reconstructs and publishes a verified proposal with a token narrowed to one fork, after the worker gate checked that the patch's [review loop]({{ '/pipeline/development/' | relative_url }}#the-review-loop) left no P0 finding open. The same job posts the summary of the loop on the pushed commit with that token, once.

## What an agent is given

An agent reads the task a trusted pre-agent step resolved from the task file the controller committed to the control repository (`/tmp/gh-aw/agent/task.json`), after the step checked its SHA-256 and the Work identity of the claim. The plan and the history the task names are fetched and checked by digest in the same way. The task and everything it names is data: the model reads it, and nothing in it widens the allowed paths, the allowed operations, or where and what is pushed. The model never reads the work queue.

## Known gaps

The accepted risks of the unsandboxed agents, and that the worker reports its own review loop, are listed under [Accepted risks and open gaps]({{ '/security/' | relative_url }}#accepted-risks-and-open-gaps). The worker's `::stop-commands::` pair covers only its own pre-agent step and post-step, so it protects nothing in the agent step.
