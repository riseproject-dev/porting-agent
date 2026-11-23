// SPDX-FileCopyrightText: 2026 The RISE Project
// SPDX-License-Identifier: MIT
"use strict";
// Reinstall the work-queue Policy when the compiled policy no longer
// preserves it.
//
// gh-aw v1.2.2 installs new pools and worker profiles on demand, but refuses
// every dispatch (`work_queue_policy_proposal_mismatch`) once the compiled
// policy drops or changes an installed profile, for example after a worker
// workflow is renamed. Its supported remedy is an administrator `policy`
// request on a quiescent queue. The dispatcher runs this module before it
// stages any Work: on a quiescent queue it publishes the compiled policy as
// a new Policy epoch through the gh-aw library itself, authenticated as this
// workflow run with the administrator role. On a busy queue it does nothing:
// the controller does not charge the dispatch while the queue drains.
//
// Every queue function comes from the gh-aw actions directory of the run
// (`actionsDir`), so the checks are exactly those of the pinned gh-aw.

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

function library(actionsDir) {
  const load = name => require(path.join(actionsDir, name));
  return {
    binding: load("work_queue_binding.cjs"),
    codec: load("work_queue_codec.cjs"),
    native: load("work_queue_native.cjs"),
    policy: load("work_queue_policy.cjs"),
    replay: load("work_queue_replay.cjs"),
  };
}

function withoutPools(policy) {
  const { pools: _pools, ...rest } = policy;
  return rest;
}

function profileBoundary(profile) {
  const { ref: _ref, logical_contract: _contract, ...boundary } = profile;
  return boundary;
}

// gh-aw v1.2.2 `preservesPool`: every installed profile is still compiled,
// with the same authority. The ref and the logical contract change with every
// compilation and are refreshed by deployment, and the default profile of an
// installed pool is kept.
function preservesPool(canonical, installed, proposed) {
  if (Object.keys(installed.profiles).some(name => !Object.hasOwn(proposed.profiles, name))) return false;
  const boundary = pool => ({ ...pool, profiles: Object.fromEntries(Object.entries(pool.profiles).map(([name, profile]) => [name, profileBoundary(profile)])) });
  const before = boundary(installed);
  const after = boundary(proposed);
  after.default_profile = before.default_profile;
  after.profiles = Object.fromEntries(Object.keys(before.profiles).map(name => [name, after.profiles[name]]));
  return canonical(before) === canonical(after);
}

// Whether gh-aw's automatic pool installation accepts `proposal` over the
// installed policy, the condition under which every dispatch is refused.
function preserved(canonical, installed, proposal) {
  if (canonical(withoutPools(installed)) !== canonical(withoutPools(proposal))) return false;
  return Object.entries(installed.pools).every(([name, pool]) => Object.hasOwn(proposal.pools, name) && preservesPool(canonical, pool, proposal.pools[name]));
}

/**
 * The decision for one queue projection: `absent` (no queue yet: the first
 * submission installs the compiled policy), `foreign` (not AW-managed: never
 * touched), `current` (the compiled policy preserves the installed one),
 * `not_quiescent` (a reinstall is due once the queue drains) or `reinstall`.
 */
function decide(lib, projection, proposal) {
  lib.policy.validatePolicy(proposal);
  if (!projection.policy) return "absent";
  if (projection.policy.authorization !== "aw" || proposal.authorization !== "aw") return "foreign";
  if (preserved(lib.codec.canonical, projection.policy, proposal)) return "current";
  return lib.replay.quiescent(projection) ? "reinstall" : "not_quiescent";
}

// A new, unique epoch bound to the replaced epoch, the queue tip and the
// compiled policy.
function epochFor(lib, projection, proposal) {
  const seed = lib.codec.canonical({ previous: projection.policy_epoch, tip: projection.tip, policy: proposal });
  return `epoch_${crypto.createHash("sha256").update(seed, "utf8").digest("hex")}`;
}

async function reinstall(options) {
  const lib = options.lib || library(options.actionsDir);
  const proposal = options.proposal ?? JSON.parse(fs.readFileSync(options.proposalPath, "utf8"));
  const queue = { githubClient: options.github, context: options.context, core: options.core, policyProposal: undefined, ...(options.queue || {}) };
  const latest = await lib.binding.loadQueue(queue);
  const decision = decide(lib, latest.projection, proposal);
  if (decision !== "reinstall") return { decision, epoch: latest.projection.policy_epoch ?? null };
  const authenticate = options.authenticate || (role => lib.native.authenticatePublisher({ githubClient: options.github, context: options.context, role }));
  const administrator = await authenticate("administrator");
  const epoch = epochFor(lib, latest.projection, proposal);
  const operation = { kind: "Policy", epoch, policy: proposal };
  await lib.binding.publishOperations(queue, administrator, ["policy-reinstall", latest.projection.tip, epoch], "policy", [operation], state => {
    // The ledger's own replay also refuses a Policy on a busy queue; refuse
    // before writing when the queue changed since it was read.
    if (state.policy_epoch !== latest.projection.policy_epoch || !lib.replay.quiescent(state)) throw lib.codec.queueError("policy_not_quiescent", "the queue changed before the Policy reinstall");
  });
  const after = await lib.binding.loadQueue(queue);
  if (after.projection.policy_epoch !== epoch) throw new Error("work_queue_policy_reinstall_not_durable");
  return { decision, epoch, previous: latest.projection.policy_epoch };
}

// The dispatcher step. A failure never fails the run: the trusted dispatch
// processing reports the policy refusal itself, and the controller decides.
async function main(options) {
  const core = options.core;
  try {
    const result = await reinstall(options);
    const messages = {
      absent: "No work queue yet: the first submission installs the compiled policy.",
      foreign: "The installed queue policy is not AW-managed; it is left unchanged.",
      current: "The compiled queue policy preserves the installed policy.",
      not_quiescent: "The compiled queue policy drops or changes an installed worker profile; the Policy is reinstalled once the queue is quiescent.",
      reinstall: `Reinstalled the compiled queue policy as epoch ${result.epoch}, replacing ${result.previous}.`,
    };
    core.info(messages[result.decision]);
    core.setOutput("decision", result.decision);
    return result;
  } catch (error) {
    core.warning(`Queue policy reinstall failed: ${error && error.message ? error.message : error}`);
    core.setOutput("decision", "failed");
    return { decision: "failed" };
  }
}

module.exports = { decide, epochFor, main, preserved, reinstall };
