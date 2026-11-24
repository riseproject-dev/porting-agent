// SPDX-FileCopyrightText: 2026 The RISE Project
// SPDX-License-Identifier: MIT
// @ts-check
// The dispatcher's Policy reinstall (scripts/work_queue_policy.cjs) against
// the exact gh-aw v1.2.2 queue library: every ledger write goes through the
// library's own commit validation and replay.
import { describe, expect, it } from "vitest";
import { createRequire } from "node:module";
import crypto from "node:crypto";
import path from "node:path";
const require = createRequire(import.meta.url);
const actionsDir = path.dirname(new URL(import.meta.url).pathname);
const root = process.env.PORTING_AGENT_ROOT;
if (!root) throw new Error("PORTING_AGENT_ROOT names the porting-agent checkout");
const { decide, main, reinstall } = require(path.join(root, "scripts/work_queue_policy.cjs"));
const replay = require("./work_queue_replay.cjs");
const codec = require("./work_queue_codec.cjs");
const lib = {
  binding: require("./work_queue_binding.cjs"),
  codec,
  native: require("./work_queue_native.cjs"),
  policy: require("./work_queue_policy.cjs"),
  replay,
};

const REPOSITORY = "owner/repo";
const context = { repo: { owner: "owner", repo: "repo" } };

function profile(name, ref) {
  return {
    logical_contract: crypto.createHash("sha256").update(`${name}@${ref}`).digest("hex"),
    workflow: `.github/workflows/${name}.lock.yml`,
    ref,
    trust_domain: "default",
    credential_scope: "repository",
    effect_scope: REPOSITORY,
    max_claims: 1,
    share_keys: false,
  };
}

function policy(names, ref) {
  return {
    authorization: "aw",
    mode: "weighted-priority",
    class_weights: [8, 4, 2, 1, 1],
    accounting_weights: { "": 1 },
    producers: {},
    pools: {
      default: {
        default_profile: names[0],
        profiles: Object.fromEntries(names.map(name => [name, profile(name, ref)])),
        logical_limit: 16,
        native_limit: 16,
        allowed_repositories: [REPOSITORY],
        max_observation_age_ms: 60000,
        retry: { max_attempts: 3, backoff_ms: 30000 },
        reconciliation: { max_attempts: 5, deadline_ms: 300000 },
      },
    },
    limits: {
      ledger_bytes: 67108864,
      recovery_bytes: 16777216,
      payload_bytes: 16384,
      graph_nodes: 4096,
      predecessors: 64,
      pending_nodes: 4096,
      operations: 256,
      assignment_bytes: 49152,
      result_bytes: 4096,
      evidence_bytes: 1024,
      observation_writes: 4096,
    },
  };
}

const administrator = { role: "administrator", principal: "1001", repository: REPOSITORY, workflow: ".github/workflows/dispatcher.lock.yml", run_id: "77", run_attempt: 1 };
const dispatcher = { ...administrator, role: "dispatcher", run_id: "78" };

// An in-memory ledger whose reads and writes use the library's replay and
// commit validation, as the Git-backed store does.
function ledger(installed) {
  let transactions = [];
  const append = (request, actor, operations, epoch) => {
    const state = transactions.length ? replay.replayTransactions(transactions) : replay.newState();
    const commit = { version: 3, id: replay.proposedCommitId(state, request), previous: state.tip || null, request, actor, policy_epoch: epoch, at: Date.now(), operations };
    transactions = replay.appendCommit(transactions, commit).transactions;
  };
  const genesis = { kind: "Policy", epoch: "epoch_genesis", policy: installed };
  append(replay.newRequest("genesis", "policy", administrator, { operations: [genesis] }), administrator, [genesis], "epoch_genesis");
  const readWorkQueueLog = async () => ({ sha: "a".repeat(40), transactions, state: replay.replayTransactions(transactions) });
  const publishWorkQueueRequest = async options => {
    const state = replay.replayTransactions(transactions);
    const decision = await options.generateOperations(state, options.request, options.actor, Date.now(), "unused");
    const operations = Array.isArray(decision) ? decision : decision.operations;
    const policyOp = operations.find(operation => operation.kind === "Policy");
    append(options.request, options.actor, operations, policyOp ? policyOp.epoch : state.policy_epoch);
    return { state: replay.replayTransactions(transactions) };
  };
  return { readWorkQueueLog, publishWorkQueueRequest, state: () => replay.replayTransactions(transactions) };
}

const authenticate = async role => ({ ...administrator, role, authenticated: true, roles: [role] });
const core = () => {
  const messages = [];
  return { messages, info: message => messages.push(["info", message]), warning: message => messages.push(["warning", message]), setOutput: (name, value) => messages.push(["output", name, value]) };
};

describe("queue Policy reinstall", () => {
  it("accepts a compiled policy that only moves refs and contracts", () => {
    const installed = policy(["planner", "worker"], "1".repeat(40));
    const state = { ...replay.newState(), policy: installed };
    expect(decide(lib, state, policy(["planner", "worker"], "2".repeat(40)))).toBe("current");
    expect(decide(lib, state, policy(["planner", "worker", "extra"], "2".repeat(40)))).toBe("current");
  });

  it("reinstalls a renamed worker profile on a quiescent queue", async () => {
    const queue = ledger(policy(["investigator", "worker"], "1".repeat(40)));
    const proposal = policy(["planner", "worker"], "2".repeat(40));
    const before = queue.state().policy_epoch;
    const result = await reinstall({ lib, proposal, context, authenticate, queue });
    expect(result.decision).toBe("reinstall");
    const after = queue.state();
    expect(after.policy_epoch).toBe(result.epoch);
    expect(after.policy_epoch).not.toBe(before);
    expect(Object.keys(after.policy.pools.default.profiles).sort()).toEqual(["planner", "worker"]);
    expect(after.policy.pools.default.default_profile).toBe("planner");
    expect([...after.deployments.get("default").keys()].sort()).toEqual(["planner", "worker"]);
    // The installed policy is now current: a second run changes nothing.
    expect((await reinstall({ lib, proposal, context, authenticate, queue })).decision).toBe("current");
    // gh-aw's own dispatch check accepts the compiled policy again.
    expect(() => lib.binding.assertPolicyProposal(after, { policyProposal: proposal })).not.toThrow();
  });

  it("installs an added reviewer profile on a live queue without a reinstall", async () => {
    // The queue as it is installed today, and the compiled policy once the
    // dispatcher dispatches the reviewer workflow too.
    const before = policy(["planner", "worker"], "1".repeat(40));
    const queue = ledger(before);
    const proposal = policy(["planner", "worker", "reviewer"], "2".repeat(40));
    expect(proposal.pools.default.profiles.reviewer.workflow).toBe(".github/workflows/reviewer.lock.yml");
    // Nothing for the dispatcher's reinstall to do: the compiled policy
    // preserves every installed profile, so no dispatch is refused.
    expect(decide(lib, queue.state(), proposal)).toBe("current");
    const epoch = queue.state().policy_epoch;
    expect((await reinstall({ lib, proposal, context, authenticate, queue })).decision).toBe("current");
    expect(queue.state().policy_epoch).toBe(epoch);
    expect(() => lib.binding.assertPolicyProposal(queue.state(), { policyProposal: proposal })).not.toThrow();
    // gh-aw's own on-demand installation, as a dispatcher submission or
    // dispatch runs it, installs the new profile at the verified default
    // revision. Only the GitHub reads that verify the workflow on the
    // default branch are stubbed; the pool installation, its checks and the
    // ledger commit are the library's own.
    const configRef = "3".repeat(40);
    const provisioningPath = require.resolve("./work_queue_provisioning.cjs");
    const installPath = require.resolve("./work_queue_pool_install.cjs");
    const real = require(provisioningPath);
    const verified = [];
    require.cache[provisioningPath].exports = {
      ...real,
      verifiedDefaultReference: async () => configRef,
      configuredWorkerDeployment: async ({ profile: wanted }, revision) => {
        verified.push(["deployment", wanted.workflow, revision]);
        return { profile: { ...wanted, ref: revision, logical_contract: profile("reviewer", revision).logical_contract }, available: true };
      },
      verifyWorkerRoute: async ({ profile: route }) => { verified.push(["route", route.workflow, route.ref]); },
    };
    delete require.cache[installPath];
    try {
      const { installWorkQueuePools } = require(installPath);
      await installWorkQueuePools({ context, githubClient: {}, readWorkQueueLog: queue.readWorkQueueLog, publishWorkQueueRequest: queue.publishWorkQueueRequest, policyProposal: proposal, core: core() }, { ...dispatcher, authenticated: true, roles: ["dispatcher"] }, ["default"]);
    } finally {
      require.cache[provisioningPath].exports = real;
      delete require.cache[installPath];
    }
    expect(verified).toEqual([
      ["deployment", ".github/workflows/reviewer.lock.yml", configRef],
      ["route", ".github/workflows/reviewer.lock.yml", configRef],
    ]);
    const after = queue.state();
    expect(after.policy_epoch).toBe(epoch);
    expect(Object.keys(after.policy.pools.default.profiles).sort()).toEqual(["planner", "reviewer", "worker"]);
    expect(after.policy.pools.default.default_profile).toBe("planner");
    expect([...after.deployments.get("default").keys()].sort()).toEqual(["planner", "reviewer", "worker"]);
    expect(decide(lib, after, proposal)).toBe("current");
    expect(() => lib.binding.assertPolicyProposal(after, { policyProposal: proposal })).not.toThrow();
    // A rollback that drops the reviewer again is a reinstall on a quiescent queue.
    expect(decide(lib, after, policy(["planner", "worker"], "4".repeat(40)))).toBe("reinstall");
  });

  it("waits for a busy queue", () => {
    const state = { ...replay.newState(), policy: policy(["investigator", "worker"], "1".repeat(40)) };
    state.works = new Map([["work", { state: "available" }]]);
    expect(decide(lib, state, policy(["planner", "worker"], "2".repeat(40)))).toBe("not_quiescent");
  });

  it("leaves a missing queue to the first submission", () => {
    expect(decide(lib, replay.newState(), policy(["planner", "worker"], "2".repeat(40)))).toBe("absent");
  });

  it("never fails the dispatcher run", async () => {
    const log = core();
    const result = await main({ lib, proposal: policy(["planner"], "2".repeat(40)), context, core: log, authenticate, queue: { readWorkQueueLog: async () => { throw new Error("ledger unavailable"); } } });
    expect(result.decision).toBe("failed");
    expect(log.messages).toContainEqual(["output", "decision", "failed"]);
  });
});

// The default loader resolves the library from the actions directory.
it("loads the gh-aw library from the actions directory", () => {
  expect(typeof require(path.join(actionsDir, "work_queue_binding.cjs")).loadQueue).toBe("function");
});
