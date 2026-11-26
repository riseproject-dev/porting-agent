---
title: Credentials
parent: Security Model
nav_order: 1
permalink: /security/credentials/
---

# Credentials
{: .no_toc }

No workflow holds a credential that can write an upstream repository, and no model holds a destination credential. This page lists every token the workflows mint, and when.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## Secrets and Apps

Two GitHub Apps carry all write authority:

| App | Installed on | Used for |
|:----|:-------------|:---------|
| Control, `riseproject-dev` | All repositories of its owner; every token is scoped to this repository | Committing state, projecting it onto tracking issues, posting the submission package, and launching workflows. |
| Destination, `riseproject-upstreaming` | All repositories of the forks' organization; every token except the creation token is scoped to one fork | Creating and writing forks, development pull requests, and CI reruns. |

Their private keys and `ANTHROPIC_API_KEY` are organization secrets selected for this repository, and the App ids are organization variables. The repository has no secrets of its own, and no `GH_AW_*` secret may exist; the [repository audit]({{ '/operations/setup/' | relative_url }}#declared-settings-and-the-audit) fails on one. Workflows default to read-only permissions.

## Tokens by workflow

Every App token is minted for one purpose, with the narrowest permissions and repositories that purpose needs. A destination token names exactly one fork.

| Workflow and job | App | Repository | Permissions | Minted when |
|:-----------------|:----|:-----------|:------------|:------------|
| Controller, `acknowledge-command` | Control | This repository | `issues: write` | On every issue comment, before and separately from reconciliation. It adds an `eyes` reaction to an `/upstreaming` comment. |
| Controller, `reconcile`: state commit | Control | This repository | `contents: write` | Only when the policy is not `read_only`, after state is staged. Environment `state`. |
| Controller, `reconcile`: projection | Control | This repository | `contents: read`, `issues: write` | After state publication. |
| Controller, `reconcile`: launches | Control | This repository | `contents: read`, `actions: write` | On every reconcile run whose policy is not `read_only`. It launches the dispatcher, provisioning, publication retries, development PR and handoff operations, and, after a state commit, one follow-up controller run. |
| Dispatcher, queue token | Control | This repository | `contents: write`, `actions: read` | In a pre-agent step of every run, to republish the compiled queue Policy on a quiescent queue. A failure to mint it does not fail the run. |
| Fork Provisioning | Destination | The organization | `contents: read` and repository creation write | Only when the fork is absent. |
| Fork Provisioning | Destination | The one fork | `contents`, `workflows`, `actions`, `administration`: write | After verifying the fork. |
| Development PR | Destination | The one fork | `contents: read`; `pull-requests`, `actions`, `administration`: write | To create or discover the development PR. |
| Development PR, CI rerun | Destination | The one fork | `actions: write` | To rerun the interrupted jobs the collector recorded. |
| Worker, publish job | Destination | The one fork | `contents`, `workflows`, `pull-requests`: write | To push a verified proposal, and to post the summary of its review loop on the pushed commit. |
| Worker, publish job, feedback response | Destination | The one fork | `pull-requests: write` | After a confirmed push of a task that carried review feedback, to reply to each feedback item and resolve the inline review threads (GraphQL `resolveReviewThread`). It is handed to that one step only, never to the agent, and a failure to mint or use it never fails the publication. |
| Worker, publish job, rerun | Destination | The one fork | `actions: write` | To rerun the failed jobs for a failure not caused by the patch. |
| Publish Retry, `publish` | Destination | The one fork | As the worker's publish job, feedback response and rerun | When the job's preflight says to publish. A separate token is minted for a rerun. |
| Publish Retry, `notify` | Control | This repository | `contents: read`, `issues: write` | To post, or find again, the plan comment of a retried verified plan. |
| Planner, `notify` | Control | This repository | `contents: read`, `issues: write` | To post the plan comment. |
| Handoff, `upstream-branch` | Destination | The one fork | `contents`, `workflows`: write | To build `riscv64`. |
| Handoff, `upstream-branch`, CI | Destination | The one fork | `actions: write` | Only when the clean head needs its own CI dispatched. |
| Handoff, `submission-comment` | Control | This repository | `issues: write` | To post the submission package. |
| Handoff, `ci-rerun` | Destination | The one fork | `actions: write` | To rerun interrupted CI on the clean head. |
| Every `continuation` job | Control | This repository | `contents: read`, `actions: write` | To continue the controller. |
| Website, `deploy` | `GITHUB_TOKEN` | This repository | `contents: read`, `pull-requests: read`, `pages: write`, `id-token: write` | Always. |
| Website, `preview-comment` | `GITHUB_TOKEN` | This repository | `pull-requests: write` | To link a preview. |

Every handoff job mints its token only after checking that the state it checked out is the head of `main` and still runs the operation. See [The handoff operations]({{ '/state-machine/engine/' | relative_url }}#the-handoff-operations).

## What no workflow holds

- A credential that can write an upstream repository.
- A model credential or destination App credential in the Controller or Website workflows.
- An App private key, destination token, or package credential in pull-request validation, which is read-only.

## Agents

The planner uses the scoped `GITHUB_TOKEN` and a server-enforced read-only GitHub MCP server. It has no control App private key, destination token, package credential, fork writer, or repository mutation tool.

The worker holds no destination credential either, nor do the review children it runs. How each is isolated is on [Agent Isolation]({{ '/security/agents/' | relative_url }}).
