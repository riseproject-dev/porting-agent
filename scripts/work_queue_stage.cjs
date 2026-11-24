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

// The collected files are read and replaced without following a link and
// without any command found on PATH. The agent ran as the runner's user,
// so it may have left the directory or a file read-only (`chmod 555`), a
// directory tree with unreadable children, or a link in place of a file.
// This step runs as the same user: it makes the parent writable again,
// unlocks a tree before removing it, and when an entry still cannot be
// unlinked, empties a regular file in place. It then checks the final
// state, and any other outcome throws, so the step fails (and the caller
// keeps the agent's artifacts from being uploaded).
const MAX_COLLECTED_BYTES = 8 * 1024 * 1024;

function lstatOrNull(file) {
  try {
    return fs.lstatSync(file);
  } catch (error) {
    if (error && error.code === "ENOENT") return null;
    throw error;
  }
}

function tryChmod(file, mode) {
  try {
    fs.chmodSync(file, mode);
  } catch {
    // Best effort: the removal below and the check of the final state decide.
  }
}

// Make every directory of a tree the agent left searchable and writable,
// without following links, so that it can be removed.
function unlockTree(root) {
  const pending = [root];
  while (pending.length) {
    const dir = pending.pop();
    tryChmod(dir, 0o700);
    let entries;
    try {
      entries = fs.readdirSync(dir);
    } catch {
      continue;
    }
    for (const entry of entries) {
      const child = path.join(dir, entry);
      const stat = lstatOrNull(child);
      if (stat && stat.isDirectory()) pending.push(child);
    }
  }
}

function truncateInPlace(file) {
  tryChmod(file, 0o600);
  const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_TRUNC | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    if (!fs.fstatSync(fd).isFile()) throw new Error(`${file} is not a regular file`);
  } finally {
    fs.closeSync(fd);
  }
}

// Remove whatever is at `file`. Returns "absent" when nothing is left, and
// "empty" when a regular file could not be unlinked and was emptied instead.
function clear(file) {
  tryChmod(path.dirname(file), 0o755);
  let stat = lstatOrNull(file);
  if (!stat) return "absent";
  if (stat.isDirectory()) unlockTree(file);
  try {
    fs.rmSync(file, { force: true, recursive: true });
  } catch {
    // Checked below.
  }
  stat = lstatOrNull(file);
  if (!stat) return "absent";
  if (stat.isFile()) {
    truncateInPlace(file);
    stat = lstatOrNull(file);
    if (stat && stat.isFile() && stat.size === 0) return "empty";
  }
  throw new Error(`${file} could not be removed`);
}

// Read a collected file the agent may have planted: only a regular file,
// never through a link, and bounded.
function readCollected(file) {
  const stat = lstatOrNull(file);
  if (!stat) return { content: undefined, dropped: [] };
  if (!stat.isFile()) return { content: undefined, dropped: ["a non-file entry"] };
  if (stat.size > MAX_COLLECTED_BYTES) return { content: undefined, dropped: ["an oversized file"] };
  tryChmod(file, 0o600);
  let fd;
  try {
    fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  } catch {
    return { content: undefined, dropped: ["an unreadable file"] };
  }
  try {
    if (!fs.fstatSync(fd).isFile()) return { content: undefined, dropped: ["a non-file entry"] };
    return { content: fs.readFileSync(fd, "utf8"), dropped: [] };
  } finally {
    fs.closeSync(fd);
  }
}

function writeExactly(file, content) {
  if (clear(file) === "absent") {
    fs.writeFileSync(file, content, { mode: 0o644, flag: "wx" });
  } else {
    const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_TRUNC | fs.constants.O_NOFOLLOW);
    try {
      fs.writeSync(fd, content);
    } finally {
      fs.closeSync(fd);
    }
  }
}

// The final state of one collected file: exactly `content`, or no intent
// at all (absent, or an empty regular file).
function verify(file, content) {
  const stat = lstatOrNull(file);
  if (content === undefined) {
    if (!stat || (stat.isFile() && stat.size === 0)) return;
    throw new Error(`${file} still holds collected intents`);
  }
  const read = stat && stat.isFile() ? readCollected(file) : { content: undefined };
  if (read.content !== content) throw new Error(`${file} does not hold exactly the staged intents`);
}

// Keep exactly the `expected` intents in one collected intent file.
function enforceFile(lib, file, expected) {
  const wanted = expected.map(intent => lib.codec.canonical(intent));
  const content = wanted.length ? `${wanted.join("\n")}\n` : undefined;
  const current = readCollected(file);
  const dropped = [...current.dropped];
  for (const line of (current.content || "").split("\n").filter(text => text.trim())) {
    let kept = false;
    try {
      kept = wanted.includes(lib.codec.canonical(JSON.parse(line)));
    } catch {
      kept = false;
    }
    if (!kept) dropped.push(describe(line));
  }
  if (!(current.content === content && !dropped.length)) {
    if (content === undefined) clear(file);
    else writeExactly(file, content);
  }
  verify(file, content);
  return { dropped };
}

// The step after the agent: the collected intent file holds exactly the
// intents the trusted stage wrote (`trusted`, its `intents` output; empty
// when no plan was staged), and the finish file holds none. Every other
// intent is dropped and reported. Any failure throws.
function enforce(options) {
  const lib = options.lib || library(options.actionsDir);
  const core = options.core;
  const trusted = options.trusted ? JSON.parse(options.trusted) : [];
  if (!Array.isArray(trusted)) throw new TypeError("the trusted intents are not a list");
  const intents = enforceFile(lib, options.intentsPath, trusted);
  const finish = options.finishPath ? enforceFile(lib, options.finishPath, []) : { dropped: [] };
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

module.exports = { clear, dispatchIntent, enforce, main, partition, stage };
