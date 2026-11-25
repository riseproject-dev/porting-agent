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
  # Pi 1.5.0 keeps only the last --exclude-tools value, and gh-aw passes
  # `bash` and `edit,write` as two flags, so bash would stay registered.
  # These args come last for the agent; the sub-agents' arguments, which
  # gh-aw builds without engine.args, are set whole in the same way.
  args: ["--exclude-tools", "bash,edit,write"]
  env:
    GH_AW_PI_SUBAGENT_ARGS: '["--print","--mode","json","--no-session","--no-approve","--exclude-tools","bash,edit,write"]'
model: anthropic/claude-haiku-5-5
network:
  allowed: []
tools:
  work-queue: true
  bash: false
  edit: false
  cli-proxy: false
safe-outputs:
  threat-detection: false
  dispatch-workflow:
    # One current contract per profile. Development queue resets replace
    # stale contracts rather than accumulating workflow versions. gh-aw
    # installs a profile the compiled policy adds on demand.
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
  # compiled policy as a new Policy epoch before it stages any queue Work. On a busy
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
        python -m tasks plan --out /tmp/gh-aw/agent/porting-plan.json \
          --control-repo-id "$CONTROL_REPO_ID"
  # The step after the agent loads the intent check from this copy: the
  # sandbox mounts the workspace read-write but ${RUNNER_TEMP}/gh-aw
  # read-only, so the check never loads code from the workspace.
  - name: Copy the trusted intent check out of the agent's reach
    run: |
      set -euo pipefail
      install -D -m 0444 scripts/work_queue_stage.cjs \
        "${RUNNER_TEMP}/gh-aw/trusted-intent-check/work_queue_stage.cjs"
  # No `if: hashFiles(...)` here: hashFiles only sees the workspace, so a
  # plan under /tmp always looked absent and the step was skipped. The
  # script checks for the file itself.
  # A queue Work is immutable, and the payload built for one task changes
  # with the pinned control commit and its history: queue Work the queue
  # already holds (a retry after a cancelled worker run, a relaunch after
  # its history was recorded) is never submitted again, only dispatched.
  - name: Stage the plan as trusted queue intents
    id: stage
    uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3  # v9.0.0
    with:
      script: |
        const fs = require("node:fs");
        const path = require("node:path");
        const file = "/tmp/gh-aw/agent/porting-plan.json";
        if (!fs.existsSync(file)) { core.info("No actionable porting work"); return; }
        const { main } = require(path.join(process.env.RUNNER_TEMP, "gh-aw/trusted-intent-check/work_queue_stage.cjs"));
        await main({ github, context, core,
                     actionsDir: path.join(process.env.RUNNER_TEMP, "gh-aw/actions"),
                     planPath: file,
                     intentsPath: path.join(process.env.RUNNER_TEMP, "gh-aw/safeoutputs/work-queue/work-queue.intents.jsonl"),
                     run: { id: String(context.runId), attempt: Number(process.env.GITHUB_RUN_ATTEMPT || 1) } });
# AWF 0.29.11 bind-mounts these directories of the runner's home into the
# agent's sandbox read-write (its sandbox-mount-policy.json), and the
# rootless install put awf in ~/.local/bin and ~/.local/lib/awf with
# ~/.local/bin first on PATH; ~/.cargo/bin and ~/.config/composer/vendor/bin
# are on the runner's PATH too. A file planted there would run on the
# runner after the agent: awf at its teardown and in "Print firewall logs",
# and any command a later step finds on PATH. Owned by root, they cannot be
# changed by the sandbox's user, with one exception: since the API proxy
# holds ANTHROPIC_API_KEY, AWF's entrypoint makes ~/.claude mode 777 again
# (and its settings.json 666) for Claude Code's key helper. That is benign:
# ~/.claude is not on PATH and nothing on the runner runs from it. A listed
# directory that is a link fails the step; inside the trees, chown changes
# a link itself, not what it points to, and the final check follows none.
# The agent has no shell and no file writes either (`bash: false`,
# `edit: false`). This step covers only these home directories: if a tool
# that writes were given back, the sandbox could still write /tmp/gh-aw
# (checked after the agent below) and the workspace with its .git, where
# gh-aw's "Configure Git credentials" step and the post step of the
# checkout run git. ~/.copilot is left as it is: the MCP gateway writes it,
# nothing runs from it, and it is not on PATH.
pre-agent-steps:
  - name: Make the home directories the sandbox mounts read-only to the agent
    run: |
      set -euo pipefail
      for dir in .cache .config .local/bin .local/lib .local/lib64 .local/share \
        .local/pipx .azure .anthropic .claude .cargo .rustup .npm .nvm; do
        if [ -L "$HOME/$dir" ]; then
          echo "$HOME/$dir is a link" >&2
          exit 1
        fi
        mkdir -p -- "$HOME/$dir"
        sudo chown -RHh root:root -- "$HOME/$dir/"
        sudo chmod -R go-w -- "$HOME/$dir/"
        if [ -L "$HOME/$dir" ] || [ -n "$(find -H "$HOME/$dir/" \( ! -user root -o -perm /022 \) ! -type l -print -quit)" ]; then
          echo "$HOME/$dir is still writable by the agent's user" >&2
          exit 1
        fi
      done
# The agent's work-queue tools append to the intent file the stage wrote,
# and gh-aw rejects extra intents only in a batch with plan intents. After
# the agent, one step loads the check copied before the agent and writes
# back exactly the intents of the stage's output (none without a plan); it
# drops and reports every other intent. It runs no command from PATH and
# no git, loads no code from the workspace, and follows no link the agent
# left. When it cannot run or fails, it plants a link out of /tmp/gh-aw
# under a random name, which makes gh-aw's final recovery check fail, and
# that check gates both agent artifact uploads, so nothing unchecked is
# uploaded; when no link can be planted, the step says so in its failure.
post-steps:
  - name: Keep only the intents the trusted stage wrote
    if: always()
    uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3  # v9.0.0
    env:
      TRUSTED_INTENTS: ${{ steps.stage.outputs.intents }}
    with:
      script: |
        const fs = require("node:fs");
        const path = require("node:path");
        const intentsPath = "/tmp/gh-aw/work-queue.intents.jsonl";
        const finishPath = "/tmp/gh-aw/work-queue.finish.jsonl";
        try {
          const { enforce } = require(path.join(process.env.RUNNER_TEMP, "gh-aw/trusted-intent-check/work_queue_stage.cjs"));
          enforce({ core, actionsDir: path.join(process.env.RUNNER_TEMP, "gh-aw/actions"),
                    trusted: process.env.TRUSTED_INTENTS, intentsPath, finishPath });
        } catch (error) {
          // gh-aw's final recovery check scans all of /tmp/gh-aw and fails on
          // a link out of it. The name is random so the agent cannot have
          // taken it first; the link is read back before it is relied on.
          const { randomBytes } = require("node:crypto");
          try { fs.chmodSync("/tmp/gh-aw", 0o755); } catch {}
          let tripwire = "";
          for (let attempt = 0; attempt < 3 && !tripwire; attempt++) {
            const name = `/tmp/gh-aw/work-queue.unchecked-${randomBytes(16).toString("hex")}`;
            try {
              fs.symlinkSync("/", name);
              if (fs.readlinkSync(name) === "/") tripwire = name;
            } catch {}
          }
          for (const file of [intentsPath, finishPath]) {
            try { fs.rmSync(file, { force: true, recursive: true }); } catch {}
          }
          const reason = error && error.message ? error.message : error;
          if (tripwire) {
            core.setFailed(`The collected work-queue intents could not be checked (${reason}); none are uploaded.`);
          } else {
            core.setFailed(`The collected work-queue intents could not be checked (${reason}), and no link could be planted under /tmp/gh-aw to stop their upload: the agent artifacts of this run may hold unchecked intents and must not be trusted.`);
          }
        }
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
trusted submission installs Policy and queue Work together. An existing
policyless ledger or a tool error is a failure, not an empty backlog.

The trusted preparation step read the task the porting controller
requested in the committed `.state/` and staged at most one task
(for the `planner` or `worker` profile) followed by
`{"pool":"default","max_claims":1,"max_dispatches":1}`. When the queue
already holds queue Work for that task, it staged only the dispatch request: the
queued Work is dispatched as it was first submitted. Explain the
staged plan in one short paragraph: the contribution, the task kind and the
reason it was selected. Do not submit again, choose a winner, dispatch
directly, or emit `noop`. A staged request is not a launch.
