// SPDX-FileCopyrightText: 2026 The RISE Project
// SPDX-License-Identifier: MIT
"use strict";
// Stage the dispatcher's plan as trusted queue intents, never submitting
// again Work the queue already holds.
//
// A gh-aw Work is immutable: its id is derived from its graph and node key,
// and a submission of the same Work id with another payload is refused
// (`work_conflict`, "immutable node definition differs"), which the trusted
// control step reports only as an unclassified failure that requires
// recovery. The payload the dispatcher builds for one requested Work is not
// stable: it pins the control commit the run starts from, and it carries the
// history once the controller has recorded it. A dispatcher run launched
// again for Work the queue already holds, after a worker run that was
// cancelled, timed out or lost its runner (the queue retries that Work), or
// after the history of a Work submitted earlier was recorded, would submit a
// second payload for that Work id and fail.
//
// This step reads the ledger through the gh-aw library of the run and
// stages only the Work the queue does not hold yet. The payload of a queued
// Work is authoritative; that Work is dispatched as it was first submitted
// (a completed or cancelled one is decided by the controller, which
// allocates a fresh graph). When no Work is left to submit, the step stages
// the dispatch request alone, so the queue still starts a worker for queued
// Work that is available. When the ledger cannot be read, the plan is
// staged unchanged and gh-aw's own checks decide.
//
// The agent that runs after this step exposes gh-aw's work-queue tools,
// which append intents to the same file, and gh-aw rejects extra intents
// only in a batch that holds plan intents. The intents this step wrote are
// therefore its output, and a step after the agent (`enforce`) keeps only
// those in the collected intent file, drops every other intent and reports
// it. A dispatcher holds no Claim, so a staged finish intent is dropped too.
// The dispatcher copies this file before the agent into a directory the
// agent's sandbox mounts read-only and loads `enforce` from that copy.
//
// Every queue function comes from the gh-aw actions directory of the run
// (`actionsDir`), so the checks are exactly those of the pinned gh-aw.

const fs = require("node:fs");
const path = require("node:path");

function library(actionsDir) {
  const load = name => require(path.join(actionsDir, name));
  return {
    binding: load("work_queue_binding.cjs"),
    codec: load("work_queue_codec.cjs"),
    graph: load("work_queue_graph.cjs"),
    intents: load("work_queue_intents.cjs"),
  };
}

function lookup(works, id) {
  if (!works) return undefined;
  if (typeof works.get === "function") return works.get(id);
  return Object.hasOwn(works, id) ? works[id] : undefined;
}

/**
 * Split the plan's nodes into the Work the queue does not hold (`fresh`)
 * and the Work it holds (`queued`, with its queue state and whether the
 * payload built now is the queued one). The Work id is gh-aw's own
 * (`nodeId(graph_id, node_key)`), with gh-aw's defaults for a node that
 * names neither.
 */
function partition(lib, projection, plan) {
  const fresh = [];
  const queued = [];
  for (const node of plan.nodes) {
    const graphId = Object.hasOwn(node, "graph_id") ? node.graph_id : lib.codec.digest(node.payload);
    const nodeKey = Object.hasOwn(node, "node_key") ? node.node_key : "root";
    const workId = lib.graph.nodeId(graphId, nodeKey);
    const existing = lookup(projection && projection.works, workId);
    if (!existing) {
      fresh.push(node);
      continue;
    }
    queued.push({
      work_id: workId,
      graph_id: graphId,
      state: existing.state || "unknown",
      payload_matches: lib.codec.canonical(existing.payload) === lib.codec.canonical(node.payload),
    });
  }
  return { fresh, queued };
}

// The dispatch request staged alone, with an id bound to this run and the
// request, so a rerun of the step stages it once.
function dispatchIntent(lib, plan, run) {
  const id = lib.codec.digest({ purpose: "porting-dispatch", run, dispatch: plan.dispatch });
  return { version: 3, intent_id: `intent:porting-dispatch-${id}`, kind: "dispatch_next", parameters: plan.dispatch };
}

async function stage(options) {
  const lib = options.lib || library(options.actionsDir);
  const core = options.core;
  const plan = options.plan ?? JSON.parse(fs.readFileSync(options.planPath, "utf8"));
  const queue = { githubClient: options.github, context: options.context, core, policyProposal: undefined, ...(options.queue || {}) };
  let projection;
  try {
    projection = (await lib.binding.loadQueue(queue)).projection;
  } catch (error) {
    core.warning(`The work queue could not be read before staging (${error && error.message ? error.message : error}); the plan is staged unchanged.`);
    lib.intents.stagePlanIntents(options.intentsPath, plan);
    return { decision: "unread", submitted: plan.nodes.length, queued: [], intents: lib.intents.readStagedIntents(options.intentsPath) };
  }
  const { fresh, queued } = partition(lib, projection, plan);
  for (const item of queued) {
    core.info(
      `Work ${item.work_id} (${item.graph_id}) is already in the queue (${item.state}): it is dispatched as it was first submitted and not submitted again` +
        (item.payload_matches ? "." : "; the payload built in this run differs from the queued one (payload drift), which a submission would make the queue refuse.")
    );
  }
  if (fresh.length) {
    lib.intents.stagePlanIntents(options.intentsPath, { ...plan, nodes: fresh });
    return { decision: queued.length ? "partial" : "submit", submitted: fresh.length, queued, intents: lib.intents.readStagedIntents(options.intentsPath) };
  }
  const intent = dispatchIntent(lib, plan, options.run || {});
  const previous = lib.intents.readStagedIntents(options.intentsPath);
  if (!previous.some(existing => existing.intent_id === intent.intent_id)) lib.intents.stageIntent(options.intentsPath, intent);
  return { decision: "dispatch_only", submitted: 0, queued, intents: lib.intents.readStagedIntents(options.intentsPath) };
}

// The dispatcher step. A failure to stage fails the step, as staging the
// plan always did.
async function main(options) {
  const result = await stage(options);
  options.core.info(`Staged ${result.submitted} porting task(s) for submission; ${result.queued.length} already queued.`);
  options.core.setOutput("decision", result.decision);
  options.core.setOutput("intents", JSON.stringify(result.intents));
  return result;
}

// Describe an intent the agent staged, bounded, for the report.
function describe(line) {
  let intent;
  try {
    intent = JSON.parse(line);
  } catch {
    return "an unparseable line";
  }
  const field = value => (typeof value === "string" ? JSON.stringify(value.slice(0, 120)) : "none");
  return `intent ${field(intent && intent.intent_id)} (${field(intent && intent.kind)})`;
}

// Keep exactly the `expected` intents in one collected intent file
// (`target`), reading what was collected from `source`: the target itself,
// or the place a step before set it aside so that nothing unchecked stays
// at the uploaded path when this check cannot run.
function enforceFile(lib, source, target, expected) {
  const wanted = expected.map(intent => lib.codec.canonical(intent));
  const content = wanted.length ? `${wanted.join("\n")}\n` : undefined;
  let stat;
  try {
    stat = fs.lstatSync(source);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const current = stat && stat.isFile() ? fs.readFileSync(source, "utf8") : undefined;
  const dropped = stat && !stat.isFile() ? ["a non-file entry"] : [];
  for (const line of (current || "").split("\n").filter(text => text.trim())) {
    let kept = false;
    try {
      kept = wanted.includes(lib.codec.canonical(JSON.parse(line)));
    } catch {
      kept = false;
    }
    if (!kept) dropped.push(describe(line));
  }
  if (source === target && current === content && !dropped.length) return { dropped };
  fs.rmSync(target, { force: true, recursive: true });
  if (content !== undefined) fs.writeFileSync(target, content, { mode: 0o644, flag: "wx" });
  return { dropped };
}

// The step after the agent: the collected intent file holds exactly the
// intents the trusted stage wrote (`trusted`, its `intents` output; empty
// when no plan was staged), and the finish file holds none. The collected
// files are read from `heldIntentsPath` and `heldFinishPath` when a step
// before set them aside, and from the target paths otherwise. Every other
// intent is dropped and reported.
function enforce(options) {
  const lib = options.lib || library(options.actionsDir);
  const core = options.core;
  const trusted = options.trusted ? JSON.parse(options.trusted) : [];
  if (!Array.isArray(trusted)) throw new TypeError("the trusted intents are not a list");
  const intents = enforceFile(lib, options.heldIntentsPath || options.intentsPath, options.intentsPath, trusted);
  const finish = options.finishPath
    ? enforceFile(lib, options.heldFinishPath || options.finishPath, options.finishPath, [])
    : { dropped: [] };
  const dropped = [...intents.dropped, ...finish.dropped];
  if (dropped.length) {
    core.warning(
      `Dropped ${dropped.length} work-queue intent(s) the trusted stage did not write: ${dropped.slice(0, 8).join(", ")}${dropped.length > 8 ? ", ..." : ""}. Only the ${trusted.length} staged intent(s) reach the queue.`
    );
  } else {
    core.info(`The collected intents are the ${trusted.length} the trusted stage wrote.`);
  }
  return { kept: trusted.length, dropped: dropped.length };
}

module.exports = { dispatchIntent, enforce, main, partition, stage };
