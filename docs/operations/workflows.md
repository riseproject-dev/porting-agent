---
title: Workflows
parent: Operations
nav_order: 2
permalink: /operations/workflows/
---

# Workflows
{: .no_toc }

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## The workflows

Display names are title case without a prefix.

| Display name | File | Trigger |
|:-------------|:-----|:--------|
| Controller | `controller.yml` | Schedule, pushes to `main` that change trusted code, issue and comment events, completed runs of the workflows below, manual dispatch |
| Dispatcher | `dispatcher.md` | Dispatch by the controller |
| Planner | `planner.md` | Native Work queue |
| Worker | `worker.md` | Native Work queue |
| Reviewer | `reviewer.md` | Native Work queue |
| Reproduction Image | `image.yml` | Weekly schedule, pushes and pull requests that change `image/`, manual dispatch |
| Fork Provisioning | `provision.yml` | Dispatch by the controller |
| Development PR | `dev-pr.yml` | Dispatch by the controller |
| Publish Retry | `publish-retry.yml` | Dispatch by the controller |
| Review Comment | `review-comment.yml` | Dispatch by the controller |
| Handoff | `handoff.yml` | Dispatch by the controller |
| Agent Validation | `validate.yml` | Pull requests, pushes to `main` |
| Website | `website.yml` | Pushes to `main`, pull requests that change `docs/`, manual dispatch |

## Names are load-bearing

`controller.yml` subscribes to completed runs of the Dispatcher, Planner, Worker, Reviewer, Reproduction Image, Fork Provisioning, Development PR, Publish Retry, Review Comment, and Handoff workflows by display name. The plan comment publisher accepts runs whose `GITHUB_WORKFLOW` is Planner or Publish Retry on dispatch, and Controller only when `PORTING_PLAN_RECOVERY=1` is set. Run discovery and dispatch otherwise address workflows by file name.

To rename a workflow:

1. Change its `name:`.
2. Update the `workflow_run` list in `controller.yml`, and the plan comment publisher if the workflow is Planner, Publish Retry, or Controller.
3. Recompile the gh-aw locks from their Markdown sources.
4. Update the tests that pin the display names, `tests/test_workflow_contracts.py` and `tests/test_milestone4_workflows.py`.
5. Update `.github/workflow-owners.json` if the file name changed, and this page.

## Ownership

`.github/workflow-owners.json` lists every top-level file in `.github/workflows/` with exactly one owner and whether it is agentic. A test enforces that every file has one entry, so a workflow cannot be added without a reviewed owner. The file is the one home of the owner descriptions.

- An agentic Markdown workflow is committed with exactly one generated `.lock.yml` file.
- `.github/workflows/shared/` holds imports, not workflows.
- Only trusted workflows may hold a destination credential; see [Credentials]({{ '/security/credentials/' | relative_url }}).

## Generated locks

The dispatcher, planner, worker, and reviewer locks are exact normalized gh-aw v1.2.2 output. Authored Markdown workflows and their generated locks move together.

| Script | Purpose |
|:-------|:--------|
| `scripts/compile_gh_aw.sh` | Compile the Markdown workflows with the pinned compiler. |
| `scripts/check_gh_aw.sh` | Check that authored and generated workflows agree. |
| `scripts/normalize_gh_aw_locks.py` | Normalize the compiler output. |
