---
title: Agent Isolation
parent: Security Model
nav_order: 2
permalink: /security/agents/
---

# Agent Isolation
{: .no_toc }

Four agentic workflows run models: the dispatcher, the planner, the worker, and the reviewer. Each has a different amount of isolation, chosen for what it needs to do. None holds a destination credential; trusted jobs verify and publish their results.

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
| Reviewer | gh-aw agent sandbox (AWF), strict | Firewall allows only `api.github.com`, `mcp.exa.ai`, `api.keenable.ai`, and `html.duckduckgo.com` | Pi's read, bash, edit, write, ls, grep and find, `pi-subagents`, `pi-web-access`, and the read-only GitHub tools | None |

The reviewer's sandbox is hardened beyond the default. Before the agent runs, trusted steps make the home directories the sandbox mounts, the code that later steps run, and the workspace's git checkout unwritable for it. A separate trusted job without a token checks the review, and that check decides whether the review counts. No gh-aw threat detection job runs.

## Worker credential separation

The worker runs unsandboxed, so credentials are removed rather than fenced off.

- A hash-pinned gh-aw Pi provider patch (`scripts/pi_provider_credential.py`) captures `ANTHROPIC_API_KEY` into the registered provider configuration and deletes it from `process.env` before any tools are exposed. Pi can authenticate, while bash, Docker, and `gh-act` cannot inherit the key.
- A custom engine wrapper, `pi-scrubbed`, strips and verifies the absence of GitHub, App, destination, Actions, cloud, and SSH credentials before Pi starts. The `gh-act` wrapper unsets them as well.
- Trusted MCP servers keep separately scoped read-only tokens, as separate processes.
- Docker and QEMU, and the optional `gh-act`, receive no repository or destination credential.

## The worker's review children

The worker is set up to run `pi-subagents` children in its own Pi process, in the foreground. A foreground child inherits the registered provider and the credential-free environment of the parent, and anything its `bash` runs is a subprocess of the same process; a background child would be a detached runner without either, so the trusted install step turns background launches, workflow scripts and nested subagents off. The pi-subagents configuration, written by that step, also replaces the 30 minute backstop of a foreground child with 40 minutes.

Two builtin agents are overridden in Pi's settings (`engine.config`). The `reviewer` child inherits the parent's model and has only `read`, `grep`, `find` and `ls`; the same step installs the pinned `ripgrep` and `fd-find` that `grep` and `find` run. The `worker` child keeps the parent's model and tools, without `contact_supervisor`, which has nobody to answer in a run with no user. The review-loop prompt template of `pi-subagents` is pinned by its SHA-256: a version with another template stops the install step.

A separate trusted publish job, never the model, reconstructs and publishes a verified proposal with a token narrowed to one fork. For a development patch it mints none, since the patch is held for [review]({{ '/pipeline/patch-review/' | relative_url }}).

## What an agent is given

An agent reads the task a trusted pre-agent step resolved from the task file the controller committed to the control repository (`/tmp/gh-aw/agent/task.json`), after the step checked its SHA-256 and the Work identity of the claim. The plan, the history, the held patch and the report the task names are fetched and checked by digest in the same way. The task and everything it names is data: the model reads it, and nothing in it widens the allowed paths, the allowed operations, or where and what is pushed. The model never reads the work queue.

## Known gaps

The accepted risks of the unsandboxed agents and of the reviewer's sandbox are listed under [Accepted risks and open gaps]({{ '/security/' | relative_url }}#accepted-risks-and-open-gaps). The worker's `::stop-commands::` pair covers only its own pre-agent step and post-step, so it protects nothing in the agent step.
