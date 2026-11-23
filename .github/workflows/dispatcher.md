---
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
name: Dispatcher
description: Admit at most one controller-selected porting task and request one planner
on:
  workflow_dispatch:
imports:
  - shared/work-queue-staged-plan.md
permissions:
  contents: read
  actions: read
  issues: read
engine:
  id: pi
  version: "1.5.0"
model: anthropic/claude-haiku-5-5
network:
  allowed: []
tools:
  work-queue: true
  bash:
    - cat /tmp/gh-aw/agent/porting-plan.json
safe-outputs:
  threat-detection: false
  dispatch-workflow:
    # One current planner contract. Development queue resets replace stale
    # contracts rather than accumulating workflow versions.
    workflows: [planner, worker]
    target-ref: ${{ github.event.repository.default_branch }}
    max: 1
steps:
  - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d  # v10.0.1
    with:
      enable-cache: false
  # gh-aw refuses every dispatch once the compiled queue policy drops an
  # installed worker profile (a renamed worker workflow). Its remedy is an
  # administrator Policy update on a quiescent queue: this run publishes the
  # compiled policy as a new Policy epoch before it stages any Work. On a busy
  # queue nothing is changed, and the controller charges no dispatch.
  - name: Mint a queue token for the Policy reinstall
    id: queue-token
    continue-on-error: true
    uses: actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510  # v3.3.0
    with:
      client-id: ${{ vars.RISEPROJECT_APP_CLIENT_ID }}
      private-key: ${{ secrets.RISEPROJECT_APP_PRIVATE_KEY }}
      owner: riseproject-dev
      repositories: porting-agent
      permission-contents: write
      permission-actions: read
  - name: Write the compiled queue policy of this run
    continue-on-error: true
    env:
      REPOSITORY: ${{ github.repository }}
      SHA: ${{ github.sha }}
    run: |
      set -euo pipefail
      uv run --locked python -m queue_policy proposal \
        --lock .github/workflows/dispatcher.lock.yml \
        --sha "$SHA" --repository "$REPOSITORY" \
        --out /tmp/gh-aw/queue-policy/proposal.json
  - name: Reinstall the queue Policy when the compiled policy drops an installed profile
    continue-on-error: true
    uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3  # v9.0.0
    with:
      github-token: ${{ steps.queue-token.outputs.token }}
      script: |
        const path = require("node:path");
        const { main } = require(path.join(process.env.GITHUB_WORKSPACE, "scripts/work_queue_policy.cjs"));
        await main({ github, context, core,
                     actionsDir: path.join(process.env.RUNNER_TEMP, "gh-aw/actions"),
                     proposalPath: "/tmp/gh-aw/queue-policy/proposal.json" });
  - name: Replay the controller-requested task from committed state (deterministic, no model)
    id: select
    env:
      GH_TOKEN: ${{ github.token }}
      CONTROL_REPO_ID: ${{ github.event.repository.id }}
    run: |
      set -euo pipefail
      uv run --locked \
        python -m work plan --out /tmp/gh-aw/agent/porting-plan.json \
          --control-repo-id "$CONTROL_REPO_ID"
  # No `if: hashFiles(...)` here: hashFiles only sees the workspace, so a
  # plan under /tmp always looked absent and the step was skipped. The
  # script checks for the file itself.
  # A Work is immutable in the queue, and the payload built for one Work
  # changes with the pinned control commit and its history: Work the queue
  # already holds (a retry after a cancelled worker run, a relaunch after
  # its history was recorded) is never submitted again, only dispatched.
  - name: Stage the plan as trusted queue intents
    uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3  # v9.0.0
    with:
      script: |
        const fs = require("node:fs");
        const path = require("node:path");
        const file = "/tmp/gh-aw/agent/porting-plan.json";
        if (!fs.existsSync(file)) { core.info("No actionable porting work"); return; }
        const { main } = require(path.join(process.env.GITHUB_WORKSPACE, "scripts/work_queue_stage.cjs"));
        await main({ github, context, core,
                     actionsDir: path.join(process.env.RUNNER_TEMP, "gh-aw/actions"),
                     planPath: file,
                     intentsPath: path.join(process.env.RUNNER_TEMP, "gh-aw/safeoutputs/work-queue/work-queue.intents.jsonl"),
                     run: { id: String(context.runId), attempt: Number(process.env.GITHUB_RUN_ATTEMPT || 1) } });
jobs:
  continuation:
    needs: [safe_outputs]
    if: always()
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      contents: read
    steps:
      - uses: actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510  # v3.3.0
        id: control-token
        with:
          client-id: ${{ vars.RISEPROJECT_APP_CLIENT_ID }}
          private-key: ${{ secrets.RISEPROJECT_APP_PRIVATE_KEY }}
          owner: riseproject-dev
          repositories: porting-agent
          permission-contents: read
          permission-actions: write
      - name: Continue reconciliation after the dispatch decision
        env:
          GH_TOKEN: ${{ steps.control-token.outputs.token }}
        run: gh workflow run controller.yml --repo "$GITHUB_REPOSITORY" --ref main
timeout-minutes: 10
strict: true
---

# Porting dispatcher

Read `/tmp/gh-aw/agent/porting-plan.json`, then call `work_queue_read`
with `{"pool":"default","limit":8}`. An absent queue is empty; the
trusted submission installs Policy and Work together. An existing
policyless ledger or a tool error is a failure, not an empty backlog.

The trusted preparation step read the task the porting controller
requested in the committed `.state/` and staged at most one task
(for the `planner` or `worker` profile) followed by
`{"pool":"default","max_claims":1,"max_dispatches":1}`. When the queue
already holds that task's Work, it staged only the dispatch request: the
queued Work is dispatched as it was first submitted. Explain the
staged plan in one short paragraph: the contribution, the task kind and the
reason it was selected. Do not submit again, choose a winner, dispatch
directly, or emit `noop`. A staged request is not a launch.
