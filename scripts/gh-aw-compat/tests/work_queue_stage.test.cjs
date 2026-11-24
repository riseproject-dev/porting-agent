// SPDX-FileCopyrightText: 2026 The RISE Project
// SPDX-License-Identifier: MIT
// @ts-check
// The dispatcher's staging of its plan (scripts/work_queue_stage.cjs)
// against the exact gh-aw v1.2.2 queue library: the ledger is built and
// checked by the library's own submission processing and replay.
import { describe, expect, it, vi } from "vitest";
import { createRequire } from "node:module";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
const require = createRequire(import.meta.url);
const actionsDir = path.dirname(new URL(import.meta.url).pathname);
const root = process.env.PORTING_AGENT_ROOT;
if (!root) throw new Error("PORTING_AGENT_ROOT names the porting-agent checkout");
const { enforce, main, partition, stage } = require(path.join(root, "scripts/work_queue_stage.cjs"));
const replay = require("./work_queue_replay.cjs");
const codec = require("./work_queue_codec.cjs");
const intents = require("./work_queue_intents.cjs");
const logging = require("./work_queue_logging.cjs");
const mcp = require("./work_queue_mcp_server.cjs");
const lib = {
  binding: require("./work_queue_binding.cjs"),
  codec,
  graph: require("./work_queue_graph.cjs"),
  intents,
};

const REPOSITORY = "owner/repo";
const context = { repo: { owner: "owner", repo: "repo" } };
const REF = "1".repeat(40);

function profile(name) {
  return {
    logical_contract: crypto.createHash("sha256").update(`${name}@${REF}`).digest("hex"),
    workflow: `.github/workflows/${name}.lock.yml`,
    ref: REF,
    trust_domain: "default",
    credential_scope: "repository",
    effect_scope: REPOSITORY,
    max_claims: 1,
    share_keys: false,
  };
}

const POLICY = {
  authorization: "aw",
  mode: "weighted-priority",
  class_weights: [8, 4, 2, 1, 1],
  accounting_weights: { "": 1 },
  producers: {},
  pools: {
    default: {
      default_profile: "planner",
      profiles: { planner: profile("planner"), worker: profile("worker") },
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

const administrator = { role: "administrator", principal: "1001", repository: REPOSITORY, workflow: ".github/workflows/dispatcher.lock.yml", run_id: "77", run_attempt: 1 };
const dispatcher = { ...administrator, role: "dispatcher", run_id: "78" };

// The Work node the dispatcher plans for one requested task (`work.node`):
// the payload pins the control commit the run started from.
function node(graph, controlRef, extra = {}) {
  return {
    graph_id: graph,
    node_key: "task",
    pool: "default",
    priority: 3,
    fairness_key: "",
    worker_profile: "worker",
    depends_on: [],
    payload: { contribution_id: "owner--proj/3", task: "development_patch", context: { reason_evidence: [{ evidence_id: "plan-1", reference: { ref: controlRef } }], ...extra } },
  };
}

function plan(...nodes) {
  return { version: 1, nodes, dispatch: { pool: "default", max_claims: 1, max_dispatches: 1 } };
}

// An in-memory ledger that admits Work exactly as the trusted control step
// does: the submitted nodes are normalized by gh-aw, the request is checked
// against the replayed state, and the commit is appended through replay.
function ledger() {
  let transactions = [];
  const state = () => (transactions.length ? replay.replayTransactions(transactions) : replay.newState());
  const append = (request, actor, operations, epoch) => {
    const current = state();
    const commit = { version: 3, id: replay.proposedCommitId(current, request), previous: current.tip || null, request, actor, policy_epoch: epoch, at: Date.now(), operations };
    transactions = replay.appendCommit(transactions, commit).transactions;
  };
  const genesis = { kind: "Policy", epoch: "epoch_genesis", policy: POLICY };
  append(replay.newRequest("genesis", "policy", administrator, { operations: [genesis] }), administrator, [genesis], "epoch_genesis");
  const submit = nodes => {
    const current = state();
    const at = Date.now();
    const parameters = intents.normalizeSubmitParameters({ nodes }, current.policy, at, current);
    const request = replay.newRequest(`request:${codec.digest(parameters)}`, "submit", dispatcher, parameters);
    const decision = replay.generateRequestOperations(current, request, dispatcher, at, replay.proposedCommitId(current, request));
    if (decision.operations.length) append(request, dispatcher, decision.operations, current.policy_epoch);
    return decision;
  };
  const readWorkQueueLog = async () => ({ sha: "a".repeat(40), transactions, state: state() });
  return { submit, state, queue: { readWorkQueueLog } };
}

const core = () => {
  const messages = [];
  return { messages, info: m => messages.push(["info", m]), warning: m => messages.push(["warning", m]), setOutput: (n, v) => messages.push(["output", n, v]) };
};

function intentsFile() {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), "porting-stage-")), "work-queue.intents.jsonl");
}

const GRAPH = "porting:7b6f929106ae7b6ebcb43fa97a04988b";

describe("dispatcher staging", () => {
  it("reproduces the live refusal of a second payload for one Work id", () => {
    const queue = ledger();
    queue.submit([node(GRAPH, "c".repeat(40))]);
    // The relaunched dispatcher pinned a later control commit: the queue
    // refuses the submission, and the control step can only call it
    // unclassified (dispatcher runs 45278935001 and 45287416687).
    let error;
    try {
      queue.submit([node(GRAPH, "7".repeat(40))]);
    } catch (caught) {
      error = caught;
    }
    expect(error && error.code).toBe("work_conflict");
    expect(logging.queueControlErrorCode(error)).toBe("queue_control_authority_or_publication_failed");
    expect(logging.queueControlErrorExplanation(logging.queueControlErrorCode(error))).toMatch(/unclassified reason/);
    // The same payload is an idempotent submission.
    expect(queue.submit([node(GRAPH, "c".repeat(40))]).reason).toBe("already_submitted");
  });

  it("stages only the dispatch request for Work the queue holds with another payload", async () => {
    const queue = ledger();
    queue.submit([node(GRAPH, "c".repeat(40))]);
    const file = intentsFile();
    const log = core();
    const drifted = plan(node(GRAPH, "7".repeat(40), { history: { evidence_id: "history-1" } }));
    const result = await stage({ lib, plan: drifted, context, core: log, queue: queue.queue, intentsPath: file, run: { id: "45278935001", attempt: 1 } });
    expect(result.decision).toBe("dispatch_only");
    expect(result.queued).toEqual([expect.objectContaining({ graph_id: GRAPH, state: "available", payload_matches: false })]);
    const batch = intents.readStagedIntentBatch(file);
    expect(batch.errors).toEqual([]);
    expect(batch.intents.map(intent => intent.kind)).toEqual(["dispatch_next"]);
    expect(batch.intents[0].parameters).toEqual(drifted.dispatch);
    // A batch without plan intents is a valid control batch.
    expect(intents.validatePreparedPlanIntentBatch(batch.intents, batch.errors)).toBeNull();
    expect(log.messages.some(([kind, text]) => kind === "info" && /payload drift/.test(text))).toBe(true);
    // A rerun of the step stages the request once.
    await stage({ lib, plan: drifted, context, core: log, queue: queue.queue, intentsPath: file, run: { id: "45278935001", attempt: 1 } });
    expect(intents.readStagedIntents(file)).toHaveLength(1);
  });

  it("submits new Work as the plan, which the queue admits", async () => {
    const queue = ledger();
    const file = intentsFile();
    const fresh = plan(node(GRAPH, "c".repeat(40)));
    const result = await stage({ lib, plan: fresh, context, core: core(), queue: queue.queue, intentsPath: file });
    expect(result.decision).toBe("submit");
    const staged = intents.readStagedIntents(file);
    expect(staged.map(intent => intent.kind)).toEqual(["submit", "dispatch_next"]);
    expect(intents.validatePreparedPlanIntentBatch(staged, [])).toBeNull();
    expect(queue.submit(staged[0].parameters.nodes).operations).toHaveLength(1);
  });

  it("submits only the Work the queue does not hold", async () => {
    const queue = ledger();
    const other = "porting:0123456789abcdef0123456789abcdef";
    queue.submit([node(GRAPH, "c".repeat(40))]);
    const both = plan(node(GRAPH, "7".repeat(40)), node(other, "7".repeat(40)));
    const { fresh, queued } = partition(lib, queue.state(), both);
    expect(fresh.map(item => item.graph_id)).toEqual([other]);
    expect(queued.map(item => item.graph_id)).toEqual([GRAPH]);
    const file = intentsFile();
    const result = await stage({ lib, plan: both, context, core: core(), queue: queue.queue, intentsPath: file });
    expect(result.decision).toBe("partial");
    const staged = intents.readStagedIntents(file);
    expect(staged[0].parameters.nodes.map(item => item.graph_id)).toEqual([other]);
    expect(() => queue.submit(staged[0].parameters.nodes)).not.toThrow();
  });

  it("stages the plan unchanged when the ledger cannot be read", async () => {
    const file = intentsFile();
    const log = core();
    const result = await stage({ lib, plan: plan(node(GRAPH, "c".repeat(40))), context, core: log, queue: { readWorkQueueLog: async () => { throw new Error("ledger unavailable"); } }, intentsPath: file });
    expect(result.decision).toBe("unread");
    expect(intents.readStagedIntents(file).map(intent => intent.kind)).toEqual(["submit", "dispatch_next"]);
    expect(log.messages.some(([kind]) => kind === "warning")).toBe(true);
  });
});

// The agent's own work-queue tools, as gh-aw v1.2.2 serves them to a
// dispatcher, staging into the file the trusted stage wrote.
function agentTools(queue, file) {
  const snapshot = { worker: null, role: "dispatcher", projection: queue.state() };
  const tools = Object.fromEntries(mcp.createWorkQueueTools(snapshot, { intentPath: file }).map(tool => [tool.name, tool]));
  return {
    submit: nodes => tools.work_queue_submit.handler({ nodes }),
    dispatch: () => tools.work_queue_dispatch_next.handler({ pool: "default", max_claims: 8, max_dispatches: 8 }),
  };
}

// The step after the agent, run on the collected files with the stage's
// `intents` output.
async function stageOutput(options) {
  const log = core();
  await main({ ...options, core: log });
  const output = log.messages.find(([kind, name]) => kind === "output" && name === "intents");
  return output[2];
}

describe("intents after the agent", () => {
  it("drops Work the agent submits next to the dispatch request alone", async () => {
    const queue = ledger();
    queue.submit([node(GRAPH, "c".repeat(40))]);
    const file = intentsFile();
    const trusted = await stageOutput({ lib, plan: plan(node(GRAPH, "7".repeat(40))), context, queue: queue.queue, intentsPath: file, run: { id: "1", attempt: 1 } });
    const agent = agentTools(queue, file);
    agent.submit([node("porting:ffffffffffffffffffffffffffffffff", "7".repeat(40))]);
    agent.dispatch();
    // Without plan intents, gh-aw's prepared-batch guard admits the agent's
    // extra intents.
    const before = intents.readStagedIntentBatch(file);
    expect(before.intents.map(intent => intent.kind)).toEqual(["dispatch_next", "submit", "dispatch_next"]);
    expect(intents.validatePreparedPlanIntentBatch(before.intents, before.errors)).toBeNull();
    const finish = path.join(path.dirname(file), "work-queue.finish.jsonl");
    fs.writeFileSync(finish, `${codec.canonical({ version: 3, intent_id: "intent:finish", kind: "finish", parameters: { outcome: "completed" } })}\n`);
    const log = core();
    expect(enforce({ lib, core: log, trusted, intentsPath: file, finishPath: finish })).toEqual({ kept: 1, dropped: 3 });
    const after = intents.readStagedIntents(file);
    expect(after).toEqual(JSON.parse(trusted));
    expect(after.map(intent => intent.intent_id)).toEqual([expect.stringMatching(/^intent:porting-dispatch-/)]);
    expect(after[0].parameters).toEqual({ pool: "default", max_claims: 1, max_dispatches: 1 });
    expect(fs.existsSync(finish)).toBe(false);
    expect(log.messages.filter(([kind]) => kind === "warning")).toEqual([["warning", expect.stringMatching(/^Dropped 3 work-queue intent\(s\).*"submit".*"finish"/)]]);
  });

  it("restores the plan batch after the agent adds to it", async () => {
    const queue = ledger();
    const file = intentsFile();
    const trusted = await stageOutput({ lib, plan: plan(node(GRAPH, "c".repeat(40))), context, queue: queue.queue, intentsPath: file });
    agentTools(queue, file).dispatch();
    const before = intents.readStagedIntentBatch(file);
    expect(intents.validatePreparedPlanIntentBatch(before.intents, before.errors)).toBe("work_queue_plan_intent_batch_invalid");
    expect(enforce({ lib, core: core(), trusted, intentsPath: file })).toEqual({ kept: 2, dropped: 1 });
    const after = intents.readStagedIntents(file);
    expect(after.map(intent => intent.kind)).toEqual(["submit", "dispatch_next"]);
    expect(intents.validatePreparedPlanIntentBatch(after, [])).toBeNull();
  });

  it("keeps no intent when no plan was staged", () => {
    const queue = ledger();
    const file = intentsFile();
    agentTools(queue, file).submit([node(GRAPH, "c".repeat(40))]);
    fs.appendFileSync(file, "not json\n");
    const log = core();
    expect(enforce({ lib, core: log, trusted: "", intentsPath: file })).toEqual({ kept: 0, dropped: 2 });
    expect(fs.existsSync(file)).toBe(false);
    expect(log.messages.some(([kind, text]) => kind === "warning" && /an unparseable line/.test(text))).toBe(true);
  });

  it("leaves the staged intents as they are when the agent adds none", async () => {
    const queue = ledger();
    const file = intentsFile();
    const trusted = await stageOutput({ lib, plan: plan(node(GRAPH, "c".repeat(40))), context, queue: queue.queue, intentsPath: file });
    const bytes = fs.readFileSync(file, "utf8");
    const log = core();
    expect(enforce({ lib, core: log, trusted, intentsPath: file })).toEqual({ kept: 2, dropped: 0 });
    expect(fs.readFileSync(file, "utf8")).toBe(bytes);
    expect(log.messages.some(([kind]) => kind === "warning")).toBe(false);
    expect(enforce({ lib, core: core(), trusted: "", intentsPath: intentsFile() })).toEqual({ kept: 0, dropped: 0 });
  });

  it("removes what the agent planted after it locked the directory (chmod 555)", async () => {
    // The agent runs as the runner's user and owns /tmp/gh-aw: it can leave
    // the directory and its files read-only, a locked tree or a link in
    // place of a collected file. The check, as the same user, still leaves
    // exactly the staged intents and no finish intent.
    const queue = ledger();
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "intents-locked-"));
    const file = path.join(dir, "work-queue.intents.jsonl");
    const finish = path.join(dir, "work-queue.finish.jsonl");
    const trusted = await stageOutput({ lib, plan: plan(node(GRAPH, "c".repeat(40))), context, queue: queue.queue, intentsPath: file });
    agentTools(queue, file).dispatch();
    fs.chmodSync(file, 0o444);
    fs.mkdirSync(path.join(finish, "a", "b"), { recursive: true });
    fs.chmodSync(path.join(finish, "a"), 0o000);
    fs.chmodSync(dir, 0o555);
    const log = core();
    expect(enforce({ lib, core: log, trusted, intentsPath: file, finishPath: finish })).toEqual({ kept: 2, dropped: 2 });
    expect(intents.readStagedIntents(file)).toEqual(JSON.parse(trusted));
    expect(fs.existsSync(finish)).toBe(false);
    expect(log.messages.some(([kind, text]) => kind === "warning" && /a non-file entry/.test(text))).toBe(true);
    // No plan: the locked intent file is removed, not left for the upload.
    fs.chmodSync(dir, 0o555);
    expect(enforce({ lib, core: core(), trusted: "", intentsPath: file })).toEqual({ kept: 0, dropped: 2 });
    expect(fs.existsSync(file)).toBe(false);
  });

  it("never follows a link the agent left in place of a collected file", async () => {
    const queue = ledger();
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "intents-link-"));
    const outside = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "intents-outside-")), "target");
    fs.writeFileSync(outside, "outside\n");
    const file = path.join(dir, "work-queue.intents.jsonl");
    const trusted = await stageOutput({ lib, plan: plan(node(GRAPH, "c".repeat(40))), context, queue: queue.queue, intentsPath: path.join(dir, "staged.jsonl") });
    fs.symlinkSync(outside, file);
    expect(enforce({ lib, core: core(), trusted, intentsPath: file })).toEqual({ kept: 2, dropped: 1 });
    expect(fs.lstatSync(file).isFile()).toBe(true);
    expect(intents.readStagedIntents(file)).toEqual(JSON.parse(trusted));
    expect(fs.readFileSync(outside, "utf8")).toBe("outside\n");
  });

  it("empties a collected file it cannot unlink, and fails when nothing works", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "intents-stuck-"));
    const file = path.join(dir, "work-queue.intents.jsonl");
    fs.writeFileSync(file, `${codec.canonical({ version: 3, intent_id: "intent:agent", kind: "dispatch_next", parameters: {} })}\n`);
    const rm = vi.spyOn(fs, "rmSync").mockImplementation(() => {
      throw Object.assign(new Error("EACCES"), { code: "EACCES" });
    });
    try {
      expect(enforce({ lib, core: core(), trusted: "", intentsPath: file })).toEqual({ kept: 0, dropped: 1 });
      expect(fs.statSync(file).size).toBe(0);
      const tree = path.join(dir, "work-queue.finish.jsonl");
      fs.mkdirSync(tree);
      expect(() => enforce({ lib, core: core(), trusted: "", intentsPath: file, finishPath: tree })).toThrow(/could not be removed/);
    } finally {
      rm.mockRestore();
    }
  });

  it("fails gh-aw's final recovery check once the dispatcher plants its tripwire", () => {
    // When the check cannot run or fails, the dispatcher links
    // /tmp/gh-aw/work-queue.unchecked-<random hex> to /. gh-aw v1.2.2 then
    // refuses the artifact roots in the step that gates both agent uploads.
    const recovery = require("./validate_recovery_envelopes.cjs");
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gh-aw-root-"));
    const file = path.join(dir, "work-queue.intents.jsonl");
    fs.writeFileSync(file, "{}\n");
    const run = () => recovery.main({ roots: [dir], additionalRoots: [file], core: { info() {} } });
    expect(run()).toEqual({ removed: 0 });
    fs.symlinkSync("/", path.join(dir, `work-queue.unchecked-${"0".repeat(32)}`));
    expect(run).toThrow(/escapes trusted roots/);
  });
});
