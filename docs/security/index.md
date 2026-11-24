---
title: Security Model
nav_order: 5
permalink: /security/
has_children: true
---

# Security Model

## Separation of authority

The porting agent separates trusted authorization and remote effects from model judgment.

- Trusted code authorizes and performs every remote effect. Models plan, write patches, and review; they never hold a destination credential.
- Lifecycle state, native Work queue state, tracking issues, and remote operation receipts have distinct ownership.
- Every remote effect binds the exact repository identity, contribution revision, Git head, and verified readback.

## Trust boundaries at a glance

| Component | Runs as | Sandbox | Credentials | Writes |
|:----------|:--------|:--------|:------------|:-------|
| Controller | Trusted workflow | n/a | Read token; control App tokens, each scoped to one purpose (see [Credentials]({{ '/security/credentials/' | relative_url }})) | `.state/` on `main`, tracking issues, workflow dispatches |
| Dispatcher | Agentic workflow | Strict sandbox, no network, no shell, no file writes | Control token for its Policy reinstall | Native queue Work and the Policy |
| Planner | Agentic workflow | None, operator-approved | Scoped read-only `GITHUB_TOKEN`; no App key | One `porting-out` artifact |
| Worker | Agentic workflow | None, operator-approved | No destination credential; the model provider key is removed before tools run | One `porting-out` artifact |
| Reviewer | Agentic workflow | gh-aw agent sandbox with a firewall | Read-only GitHub tools | One review in `porting-out` |
| Publish, handoff, provisioning, and development PR jobs | Trusted workflows, no model | n/a | Destination tokens for one fork, except an organization-wide token that only creates a missing fork; each after state checks | The fork, and the tracking issue for the package |
| Website | Trusted workflow, no model | Jekyll safe mode for previews | Pages and read tokens | The Pages deployment, and one preview comment |

Pull-request validation is read-only. It receives no App private key, destination token, package credential, or state-publisher environment.

## Accepted risks and open gaps

The [Known Gaps]({{ '/state-machine/known-gaps/' | relative_url }}) page records every place where the code does not yet match the lifecycle table, including the accepted risks of the agents:

- [The planner and worker agents run on the runner, outside the sandbox]({{ '/state-machine/known-gaps/' | relative_url }}#the-planner-and-worker-agents-run-on-the-runner-outside-the-sandbox)
- [The reviewer agent has a shell, file writes and web search inside the sandbox]({{ '/state-machine/known-gaps/' | relative_url }}#the-reviewer-agent-has-a-shell-file-writes-and-web-search-inside-the-sandbox)
- [Workflow commands in the agent step's output are not suspended]({{ '/state-machine/known-gaps/' | relative_url }}#workflow-commands-in-the-agent-steps-output-are-not-suspended)
- [The reviewer's upload staging is wider than its one output]({{ '/state-machine/known-gaps/' | relative_url }}#the-reviewers-upload-staging-is-wider-than-its-one-output)
- [Provisioning and development PR operations validate the checked-out state only]({{ '/state-machine/known-gaps/' | relative_url }}#provisioning-and-development-pr-operations-validate-the-checked-out-state-only)

## Pages

| Page | Contents |
|:-----|:---------|
| [Credentials]({{ '/security/credentials/' | relative_url }}) | The two Apps, and which job mints which token. |
| [Agent Isolation]({{ '/security/agents/' | relative_url }}) | The sandbox, network, tools, and credentials of each agent. |
| [State Publication]({{ '/security/state-publication/' | relative_url }}) | How `.state/` reaches `main`. |
| [Untrusted Input]({{ '/security/untrusted-input/' | relative_url }}) | How requests, upstream text, history, reviews, and previews are treated as data. |
