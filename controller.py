# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""One controller tick (plan sections 5.3 and 9).

``tick`` is deterministic and side-effect free: it takes loaded state, the
policy and an ``Inputs`` snapshot collected read-only by ``collect.py``, and
returns staged state changes plus a list of *requested* effects (native Work
submissions, notifications). Nothing is executed here.

* In read-only mode (``policy.read_only``, the default) requested effects are
  reported only; trusted code, not a prompt, disables every publisher.
* No model call happens on an idle tick: work is selected only when there is
  an actionable investigation, failure, or feedback item.
* A tick never waits for workers or builds.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import canary
import capacity
import collect
import compat
import engine
import intake
import isolation
import lifecycle
import recovery
import schemas
import work as work_queue
from commands import URGENT, Command
from common import (
    CONTROL_REPO,
    INTAKE_LABEL,
    contribution_file_id,
    load_policy,
    repository_root,
    validate_scope_paths,
)


@dataclass
class Inputs:
    now: datetime
    issues: list[dict] = field(default_factory=list)          # intake issues, freshly fetched
    tracking_issues: dict[int, dict] = field(default_factory=dict)  # every live issue's fresh description
    upstream_snapshots: dict[str, dict] = field(default_factory=dict)  # verified source ID / initial base
    comments: list[dict] = field(default_factory=list)        # {"issue_number", "comment", "event_body"}
    pressure: capacity.Pressure | None = None
    verified_prs: dict[str, dict] = field(default_factory=dict)  # url -> verified PR resource
    resolution_evidence: dict[str, dict] = field(default_factory=dict)
    acceptances: dict[int, int] = field(default_factory=dict)     # issue id -> accepting actor id
    requests_today: dict[int, int] = field(default_factory=dict)  # actor id -> count


@dataclass
class TickResult:
    staged: list[tuple[str, dict]] = field(default_factory=list)   # (kind, record)
    requested_effects: list[dict] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)

    def stage(self, kind: str, record: dict) -> None:
        self.staged.append((kind, record))


def _iso(t: datetime) -> str:
    return t.isoformat()


def _actionable(c: dict) -> str | None:
    """What a coding agent could usefully do now, or None (no AI on idle)."""
    if c["paused"] or c["phase"] in lifecycle.TERMINAL | {"parked"}:
        return None
    if c.get("pending_publication"):
        return None   # a delivered proposal is still being published; never re-run the agent for it
    if c.get("active_work"):
        return None   # a task was already requested for this contribution; wait for its outcome
    if c.get("active_operation_id") or c["activity"] in ("agent_running", "waiting_human",
                                                          "waiting_ci", "waiting_capacity"):
        return None
    if c["phase"] == "upstream_review" and c["activity"] == "fix_needed":
        return "upstream_revision"
    if c["phase"] == "development" and c["activity"] == "fix_needed":
        return "development_repair"
    if c["phase"] in ("queued", "investigation") and c["activity"] == "idle":
        return "investigation"
    if c["phase"] == "development" and c["activity"] == "idle" and not c.get("last_ci_evidence_id"):
        return "development_patch"
    return None


# A requested task that produced no outcome by then is given up on: the
# contribution waits for a human instead of being retried blindly.
WORK_DEADLINE_HOURS = 6

PRIORITY = {"upstream_revision": 0, "development_repair": 1, "development_patch": 2,
            "investigation": 3}
WRITE_KINDS = {"upstream_revision", "development_repair", "development_patch"}

# The rows of the table that allocate due Work, by the state it is due in, in
# the order they are tried; their guards are exclusive. A request in
# `queued.idle` first takes `begin_investigation`, in the tick that allocates
# the investigation Work.
ALLOCATIONS = {
    "investigation.idle": ("dispatch_investigation",),
    "development.fix_needed": ("request_repair", "request_patch"),
    "upstream_review.fix_needed": ("request_upstream_revision", "request_review_revision"),
}
# The rows taken instead when a budget of that Work is exhausted. They need no
# worker slot and are taken as soon as their guard holds.
EXHAUSTIONS = {
    "investigation.idle": ("exhaust_work_total",),
    "development.fix_needed": ("exhaust_work_total", "refresh_on_exhaustion",
                               "exhaust_repair_attempts"),
    "upstream_review.fix_needed": ("exhaust_repair_attempts",),
}
# The facts the controller establishes for an allocation it is about to take.
ALLOCATION_FACTS = frozenset({"worker_slot_free", "capacity_available"})


def task_for(verb: str, record: dict) -> str:
    """The task an allocation row asks a worker for. ``request_patch`` asks
    for a repair when the record holds pending feedback for it to answer."""
    if verb == "dispatch_investigation":
        return "investigation"
    if verb in ("request_upstream_revision", "request_review_revision"):
        return "upstream_revision"
    if verb == "request_repair" or record.get("pending_feedback_evidence_ids"):
        return "development_repair"
    return "development_patch"


def tick(*, contributions: dict[str, dict], operations: dict[str, dict], consumed: dict[int, dict],
         projects: dict[str, dict], policy: dict, inputs: Inputs,
         skip: frozenset[str] = frozenset(), evidence: dict[str, dict] | None = None) -> TickResult:
    """``evidence`` is the store's evidence by id, from which the failing CI
    job of a head is decided when Work is due. ``skip`` names the
    contributions this tick does not process: nothing
    is staged or requested for them, and a command on their tracking issue
    is left unconsumed for the next tick. They still count as existing
    contributions (worker slots, one active contribution per upstream).
    Every step that works on one contribution runs in an
    ``isolation.contribution`` block."""
    res = TickResult()
    now = _iso(inputs.now)
    by_id = {c["contribution_id"]: c for c in contributions.values()}
    rep = res.report
    rep.update({"at": now, "policy_version": policy["policy_version"],
                "read_only": policy["read_only"], "commands": [], "intake": [],
                "operations": [], "selected": [], "deferred": [], "alerts": []})

    # 1. Intake first, so a command posted on a brand-new request applies to it.
    parked = [p["upstream"]["full_name"] for p in projects.values() if p.get("parked")]
    active = {c["upstream"]["full_name"]: c["contribution_id"] for c in by_id.values()
              if c["phase"] not in lifecycle.TERMINAL}
    known = [c["tracking_issue"]["id"] for c in by_id.values()]
    for issue in inputs.issues:
        if issue.get("id") in known:
            continue      # already a contribution; the form is not re-read
        try:
            req = intake.parse_request(issue, policy)
        except intake.IntakeError as e:
            rep["intake"].append({"issue": issue["number"], "decision": "invalid", "detail": str(e)})
            continue
        decision, detail = intake.admission_decision(
            req, accepted_by=inputs.acceptances.get(req.issue_id), policy=policy,
            active_by_upstream=active, parked_upstreams=parked, known_issue_ids=known,
            requests_today_by_actor=inputs.requests_today.get(req.actor_id, 0))
        rep["intake"].append({"issue": req.issue_number, "decision": decision, "detail": detail})
        if decision != "accept":
            continue
        project_id = contribution_file_id(req.upstream.replace("/", "--").lower())
        cid = f"{project_id}/{req.issue_number}"
        if cid in skip:
            rep["intake"][-1] = {"issue": req.issue_number, "decision": "deferred",
                                 "detail": "the contribution is not processed in this tick"}
            continue
        rec = lifecycle.new_contribution(
            contribution_id=cid, project_id=project_id,
            issue={
                "id": req.issue_id,
                "number": req.issue_number,
                "url": f"https://github.com/{CONTROL_REPO}/issues/{req.issue_number}",
                "description": req.body,
                "description_digest": req.digest,
                "notes": req.notes,
            },
            upstream=req.upstream,
            distributions=list(req.distributions), base_ref=req.target_branch,
            policy_id=policy["policy_version"], now=now)
        by_id[cid] = rec
        active[req.upstream] = cid
        known.append(req.issue_id)
        res.stage("contributions", rec)
        res.stage("events", {"schema_version": 1, "contribution_id": cid, "sequence": 0, "at": now,
                             "kind": "intake:accepted", "from": {}, "to": {"phase": "queued"},
                             "cause": {"type": "observation", "id": f"issue-{req.issue_id}"},
                             "revision_bumped": False})
        proj = projects.get(project_id) or {
            "schema_version": 1, "project_id": project_id,
            "upstream": {"repository_id": None, "full_name": req.upstream},
            "distributions": list(req.distributions), "parked": False, "parked_reason": None,
            "contribution_ids": [], "active_contribution_id": None, "prerequisites": [],
            "dossier_evidence_id": None, "release_observation": None, "updated_at": now}
        proj = dict(proj, contribution_ids=proj["contribution_ids"] + [cid],
                    active_contribution_id=cid, updated_at=now)
        projects[project_id] = proj
        res.stage("projects", proj)

    # Recover only the exact canonical canary defect that was committed before
    # agent workflows existed. Intent is durable before the trusted issue edit;
    # an exact live readback finalizes state in a second fast-forward commit.
    rep["canary_recovery_requests"] = []
    canary_blocked: set[str] = set()
    for cid, record in list(by_id.items()):
        if not canary.is_target(record):
            continue
        if cid in skip:
            continue
        with isolation.contribution(cid, "canary_recovery"):
            matching = [
                operation
                for operation in operations.values()
                if operation.get("kind") == canary.OPERATION_KIND
                and operation.get("contribution_id") == canary.CONTRIBUTION_ID
            ]
            if len(matching) == 1 and matching[0].get("status") == "confirmed":
                try:
                    canary.validate_operation(matching[0])
                    if not canary.has_expected_tracking(record):
                        raise canary.CanaryError(
                            "confirmed canary operation has mismatched tracking state"
                        )
                except canary.CanaryError as error:
                    canary_blocked.add(cid)
                    rep["alerts"].append({
                        "contribution": cid,
                        "alert": f"canonical canary recovery blocked: {error}",
                    })
                continue
            if canary.is_repaired(record):
                continue
            canary_blocked.add(cid)
            issue = inputs.tracking_issues.get(record["tracking_issue"]["number"])
            try:
                if issue is None:
                    raise canary.CanaryError("canonical canary live issue is unavailable")
                if len(matching) > 1:
                    raise canary.CanaryError("multiple canonical canary recovery intents")
                if matching:
                    operation = matching[0]
                    canary.validate_operation(operation)
                    canary.validate_mismatch(record, operation_id=operation["operation_id"])
                    current_digest = canary.validate_issue(
                        issue, {canary.INCOMPLETE_DIGEST, canary.EXPECTED_DIGEST}
                    )
                else:
                    canary.validate_mismatch(record, operation_id=None)
                    current_digest = canary.validate_issue(issue, {canary.INCOMPLETE_DIGEST})
                    operation = canary.operation_for(record, policy=policy, now=now)
                    record = canary.bind_operation(record, operation, now)
                    operations[operation["operation_id"]] = operation
                    by_id[cid] = record
                    res.stage("operations", operation)
                    res.stage("contributions", record)
                    res.stage("events", canary.planned_event(record, operation, now))
                rep["canary_recovery_requests"].append({
                    "operation_id": operation["operation_id"],
                    "contribution_id": cid,
                    "observed_body_digest": current_digest,
                })
            except canary.CanaryError as error:
                rep["alerts"].append({
                    "contribution": cid,
                    "alert": f"canonical canary recovery blocked: {error}",
                })

    # Before destination writers exist, a verified dossier proceeds to an
    # uncredentialed proposal worker. Retire only an exact, never-launched fork
    # intent created by an earlier controller; no remote resource is touched.
    rep["deferred_destination_operations"] = []
    destination_blocked: set[str] = set()
    if not policy.get("destination_writes", False):
        for cid, record in list(by_id.items()):
            if cid in skip:
                continue
            with isolation.contribution(cid, "destination_deferral"):
                operation = operations.get(record.get("active_operation_id"))
                if not operation or operation.get("kind") != "fork_provision":
                    continue
                if (record.get("phase") != "provisioning" or record.get("fork") is not None
                        or not record.get("dossier_evidence_id")
                        or compat.waits_for_human(record)):
                    continue
                if operation.get("status") != "planned":
                    destination_blocked.add(cid)
                    rep["alerts"].append({
                        "contribution": cid,
                        "alert": "destination operation exists but is not provably unlaunched",
                    })
                    continue
                retired = recovery.legacy_status(operation, "not_launched",
                                                 site="controller.defer_destination_operation",
                                                 now=now)
                changed = compat.legacy_update(
                    record, "controller.defer_destination_operation",
                    phase="development",
                    activity="idle",
                    active_operation_id=None,
                    updated_at=now,
                )
                schemas.validate("contribution", changed)
                operations[operation["operation_id"]] = retired
                by_id[cid] = changed
                res.stage("operations", retired)
                res.stage("contributions", changed)
                res.stage("events", lifecycle._event(
                    record,
                    changed,
                    "destination_operation_deferred",
                    "policy",
                    operation["operation_id"],
                    now,
                    False,
                ))
                rep["deferred_destination_operations"].append(operation["operation_id"])

    # Removing the intake label withdraws only an accepted request that has
    # not requested any Work yet (`withdraw_request`, guarded by no Work, no
    # dossier, and no fork). Once Work exists, explicit lifecycle commands are
    # required so an unlabeled event cannot silently stop work.
    for cid, c in list(by_id.items()):
        if cid in skip:
            continue
        with isolation.contribution(cid, "intake_label"):
            issue = inputs.tracking_issues.get(c["tracking_issue"]["number"])
            labels = {label.get("name") if isinstance(label, dict) else label
                      for label in (issue or {}).get("labels", [])}
            if issue is None or INTAKE_LABEL in labels \
                    or engine.state_key(c) != "queued.idle":
                continue
            out = engine.apply_transition(c, "withdraw_request", policy=policy, now=now,
                                          cause_id=f"issue-{c['tracking_issue']['id']}",
                                          facts={"intake_label_removed"})
            if not out.accepted:
                continue
            changed = out.record
            by_id[cid] = changed
            active.pop(c["upstream"]["full_name"], None)
            res.stage("contributions", changed)
            res.stage("events", out.event)
            project = projects.get(c["project_id"])
            if project and project.get("active_contribution_id") == cid:
                project = dict(project, active_contribution_id=None, updated_at=now)
                projects[c["project_id"]] = project
                res.stage("projects", project)
            rep["intake"].append({"issue": c["tracking_issue"]["number"], "decision": "cancelled",
                                  "detail": f"{INTAKE_LABEL} label removed before Work admission"})

    # Bind initially unspecified source identities/bases from trusted API data,
    # never from the model's dossier. Already-bound bases remain immutable.
    source_blocked = set(canary_blocked | destination_blocked)
    for cid, c in list(by_id.items()):
        if cid in skip:
            continue
        with isolation.contribution(cid, "source_identity"):
            snapshot = inputs.upstream_snapshots.get(c["upstream"]["full_name"])
            if snapshot is None:
                continue
            if snapshot.get("error"):
                source_blocked.add(cid)
                rep["deferred"].append({"contribution": cid, "why": "source verification unavailable"})
                continue
            current = c["upstream"].get("repository_id")
            if current is not None and current != snapshot["repository_id"]:
                source_blocked.add(cid)
                # The table verb for a changed identity sets the paused flag
                # with the controller's reason, clears the requested Work and
                # what is held for publication, and keeps the state (from an
                # `agent_running` state, the state in which the Work is due).
                cause = f"upstream-identity-{snapshot['repository_id']}"
                if compat.is_legacy_state(c):
                    out = compat.legacy_pause(c, "pause_on_identity_change",
                                              reason=compat.IDENTITY_REASON, now=now,
                                              cause_id=cause)
                else:
                    out = engine.apply_transition(
                        c, "pause_on_identity_change", policy=policy, now=now, cause_id=cause,
                        facts={"upstream_identity_changed"})
                if out.accepted:
                    by_id[cid] = out.record
                    res.stage("contributions", out.record)
                    res.stage("events", out.event)
                continue
            updated = dict(c["upstream"])
            for key in ("repository_id", "base_ref", "base_sha"):
                if updated.get(key) is None:
                    updated[key] = snapshot[key]
            if updated != c["upstream"]:
                changed = dict(c, upstream=updated, updated_at=now)
                by_id[cid] = changed
                res.stage("contributions", changed)

    # 2. Authorized commands, urgent stops first, all before any work is selected.
    decisions = []
    for item in inputs.comments:
        d = intake.accept_command(comment=item["comment"], issue_number=item["issue_number"],
                                  event_body=item.get("event_body"), policy=policy,
                                  consumed=consumed, now=now)
        decisions.append(d)
    decisions.sort(key=lambda d: 0 if d.command and d.command.verb in URGENT else 1)
    for d in decisions:
        if d.receipt["comment_id"] in consumed and not d.accepted:
            rep["commands"].append({"comment": d.receipt["comment_id"], "result": d.reason})
            continue
        target = _command_target(d, by_id, operations)
        if target in skip:
            # Left unconsumed: the next tick reads the comment again.
            rep["commands"].append({"comment": d.receipt["comment_id"], "result": "deferred",
                                    "reason": "the contribution is not processed in this tick"})
            continue
        receipt = dict(d.receipt)
        if d.accepted and target is not None:
            # A stop command that cannot be applied makes its contribution
            # errored: the tick is computed again without it, so nothing is
            # launched for it.
            with isolation.contribution(target, "command"):
                receipt = _apply(d.command, receipt, by_id, operations, inputs, res, now, policy)
        elif d.accepted:
            receipt = _apply(d.command, receipt, by_id, operations, inputs, res, now, policy)
        consumed[receipt["comment_id"]] = receipt
        res.stage("commands", receipt)
        rep["commands"].append({"comment": receipt["comment_id"], "result": receipt["result"],
                                "reason": receipt.get("reason")})

    # 2b. Reminders of a pending human step: the table's `record_reminder`, in
    # every state of kind gate, human_wait, or locked, once
    # `human_sla_business_days.reminder` business days have passed since the
    # request or the last reminder. Suppressed while paused.
    reminders = []
    reminder_states = set(lifecycle.rule_states("record_reminder"))
    for cid, c in list(by_id.items()):
        if cid in skip or engine.state_key(c) not in reminder_states:
            continue
        with isolation.contribution(cid, "reminder"):
            if engine.refusal(c, "record_reminder", policy=policy, now=now):
                continue
            out = engine.apply_transition(c, "record_reminder", policy=policy, now=now,
                                          cause_id=f"reminder-{c['revision']}",
                                          details=reminder_details(c))
            by_id[cid] = out.record
            res.stage("contributions", out.record)
            res.stage("events", out.event)
            reminders.append({"contribution": cid, "state": engine.state_key(c),
                              "pending_human_action": c["pending_human_action"]["kind"]})
    if reminders:
        rep["reminders"] = reminders

    # 3. Uncertain operations before any replacement.
    blocked_by_op: set[str] = set()
    for op in operations.values():
        if op["contribution_id"] in skip:
            continue
        with isolation.contribution(op["contribution_id"], "operation_recovery"):
            if op["status"] in recovery.OPEN:
                blocked_by_op.add(op["contribution_id"])
                due = recovery.next_discovery_at(op, policy)
                rep["operations"].append({"id": op["operation_id"], "status": op["status"],
                                          "next_discovery": due.isoformat() if due else None})
                for a in recovery.alerts(op, inputs.now):
                    rep["alerts"].append({"operation": op["operation_id"], "alert": a})

    # Provisioning is a trusted administrative workflow, not a model task.
    # Persist its normal operation intent before dispatch, with no second queue.
    # In `provisioning.idle` the table's rows launch it: `start_operation`
    # within `operation_launch` (counted on the operation), and
    # `require_provisioning_intervention` once that budget is exhausted.
    res.report["provision_requests"] = []
    if policy.get("destination_writes", False):
        for cid, c in list(by_id.items()):
            if cid in skip:
                continue
            with isolation.contribution(cid, "provisioning"):
                operation = operations.get(c.get("active_operation_id"))
                if operation and operation.get("kind") == "fork_provision" \
                        and operation.get("status") == "planned":
                    attempts = recovery.launch_attempts(operation)
                    due = attempts == 0 or (
                        (inputs.now - datetime.fromisoformat(operation["updated_at"])).total_seconds()
                        >= LAUNCH_INTERVAL_SECONDS)
                    if not due:
                        continue
                    if engine.state_key(c) == "provisioning.idle":
                        if _launch_provisioning(c, operation, by_id, operations, inputs, res, now,
                                                policy):
                            break
                        continue
                    if compat.waits_for_human(c):
                        continue  # a wait of the table is left only by its own rows
                    if attempts < lifecycle.budget_limit("operation_launch", policy):
                        # A record planned in a shape the table does not have
                        # (`provisioning.waiting_capacity`, or the provisioning
                        # wait without a pending action).
                        launched = recovery.charge_launch(operation, now)
                        operations[launched["operation_id"]] = launched
                        res.stage("operations", launched)
                        res.report["provision_requests"].append({
                            "operation_id": launched["operation_id"], "contribution_id": cid,
                            "attempt": attempts + 1,
                        })
                        break
                if c.get("fork") is None and c["phase"] == "development" \
                        and not compat.waits_for_human(c) \
                        and (c.get("proposal_artifact") or {}).get("outcome") == "proposal":
                    prior = [op for op in operations.values()
                             if op.get("contribution_id") == cid and op.get("kind") == "fork_provision"]
                    if len(prior) == 1 and prior[0]["status"] == "not_launched":
                        operation = recovery.legacy_status(
                            prior[0], "planned", site="controller.rearm_provisioning", now=now,
                            external_ids=dict(prior[0].get("external_ids") or {},
                                              **{recovery.LAUNCH_COUNTER: 1}))
                        changed = compat.legacy_update(
                            c, "controller.rearm_provisioning", phase="provisioning",
                            activity="waiting_capacity",
                            active_operation_id=operation["operation_id"], updated_at=now)
                        schemas.validate("contribution", changed)
                        operations[operation["operation_id"]] = operation
                        by_id[cid] = changed
                        res.stage("operations", operation)
                        res.stage("contributions", changed)
                        res.stage("events", lifecycle._event(
                            c, changed, "destination_operation_rearmed", "policy",
                            operation["operation_id"], now, False,
                        ))
                        res.report["provision_requests"].append({
                            "operation_id": operation["operation_id"], "contribution_id": cid,
                            "attempt": 1,
                        })
                        break
    for cid, c in list(by_id.items()):
        if not policy.get("destination_writes", False):
            break
        if res.report["provision_requests"]:
            break
        if cid in skip:
            continue
        with isolation.contribution(cid, "provisioning"):
            if c["phase"] != "provisioning" or c.get("paused") \
                    or recovery.is_open(operations.get(c.get("active_operation_id"))) \
                    or (c.get("active_operation_id") and c["active_operation_id"] not in operations) \
                    or cid in source_blocked:
                continue
            if c.get("pending_human_action"):
                # A human wait of the table is left only by its own rows.
                continue
            if not c.get("dossier_evidence_id") or not c.get("admission_class") \
                    or not c.get("scope_paths") \
                    or not c["upstream"].get("repository_id") or not c["upstream"].get("base_sha"):
                rep["deferred"].append({"contribution": cid, "why": "provisioning prerequisites missing"})
                continue
            ok, why = capacity.admit_expensive(inputs.pressure or capacity.Pressure(ok=False, reason="no usage data"),
                                               reserved=capacity.reserved_jobs(operations.values()), estimated_jobs=1, policy=policy)
            if not ok:
                rep["deferred"].append({"contribution": cid, "why": why})
                continue
            fork = c.get("fork") or {}
            op = recovery.plan(contribution_id=cid, revision=c["revision"], kind="fork_provision",
                               policy_version=policy["policy_version"], reason_ids=[c["dossier_evidence_id"]],
                               target={"repository": "riseproject-upstreaming/" + c["upstream"]["full_name"].split('/', 1)[1],
                                       "repository_id": fork.get("repository_id"), "ref": "riscv64",
                                       "expected_head": fork.get("head_sha"), "base_sha": c["upstream"]["base_sha"]},
                               proposed={"commit": c["upstream"]["base_sha"], "marker": f"provision:{cid}:{c['revision']}"},
                               capacity={"selector": "ubuntu-24.04-riscv", "estimated_jobs": 1,
                                         "estimated_minutes": 10, "observed_run_ids": []}, now=now)
            if op["operation_id"] in operations:
                rep["deferred"].append({"contribution": cid, "why": "existing provisioning operation requires reconciliation"})
                continue
            if engine.state_key(c) == "provisioning.idle":
                # The intent is recorded and launched in the same tick.
                planned = dict(c, active_operation_id=op["operation_id"], updated_at=now)
                schemas.validate("contribution", planned)
                if not _launch_provisioning(planned, op, by_id, operations, inputs, res, now, policy,
                                            capacity_checked=True):
                    raise lifecycle.TransitionError(
                        f"start_operation refused for a new provisioning intent of {cid}")
                continue
            op = recovery.charge_launch(op, now)
            res.stage("operations", op)
            planned = compat.legacy_update(c, "controller.plan_provisioning",
                                           active_operation_id=op["operation_id"],
                                           activity="waiting_capacity", updated_at=now)
            res.stage("contributions", planned)
            by_id[cid] = planned
            res.report["provision_requests"].append({
                "operation_id": op["operation_id"], "contribution_id": cid, "attempt": 1,
            })

    # A published development head gets a same-fork development PR before CI
    # observation. Persist intent first; the trusted workflow creates/discovers
    # exactly riscv64 -> the frozen upstream base ref and returns a receipt.
    res.report["dev_pr_requests"] = []
    if policy.get("destination_writes", False):
        for cid, c in list(by_id.items()):
            if cid in skip:
                continue
            with isolation.contribution(cid, "development_pr"):
                operation = operations.get(c.get("active_operation_id"))
                if not operation or operation.get("kind") not in {"dev_pr_create", "dev_pr_update"} \
                        or operation.get("status") != "planned":
                    continue
                # No row of the table launches a development PR operation; its
                # launches are counted like `operation_launch`, on the operation.
                attempts = recovery.launch_attempts(operation)
                due = attempts == 0 or (
                    (inputs.now - datetime.fromisoformat(operation["updated_at"])).total_seconds()
                    >= LAUNCH_INTERVAL_SECONDS
                    and attempts < lifecycle.budget_limit("operation_launch", policy)
                )
                if due:
                    launched = recovery.charge_launch(operation, now)
                    operations[launched["operation_id"]] = launched
                    res.stage("operations", launched)
                    res.report["dev_pr_requests"].append({
                        "operation_id": launched["operation_id"], "contribution_id": cid,
                        "attempt": attempts + 1,
                    })
                    break
    for cid, c in list(by_id.items()):
        if not policy.get("destination_writes", False):
            break
        if res.report["dev_pr_requests"]:
            break
        if len(res.report["dev_pr_requests"]) >= 1:
            break
        if cid in skip:
            continue
        with isolation.contribution(cid, "development_pr"):
            fork = c.get("fork") or {}
            artifact = c.get("proposal_artifact") or {}
            pubop = operations.get(c.get("publication_operation_id"))
            if c["phase"] != "development" or c.get("paused") or compat.waits_for_human(c) \
                    or c.get("active_operation_id") or fork.get("pr_number") is not None \
                    or not fork.get("repository_id") or not fork.get("head_sha") \
                    or fork.get("head_sha") == c["upstream"].get("base_sha") \
                    or artifact.get("outcome") != "published" or not artifact.get("result_commit") \
                    or not c.get("dossier_evidence_id") or not c.get("scope_paths") \
                    or not pubop or pubop.get("status") != "confirmed":
                continue
            marker = f"<!-- upstreaming-development:{cid}:r{c['revision']}:{fork['head_sha']} -->"
            op = recovery.plan(contribution_id=cid, revision=c["revision"], kind="dev_pr_create",
                               policy_version=policy["policy_version"],
                               reason_ids=[c["dossier_evidence_id"], str(artifact["result_commit"])],
                               target={"repository": fork["name"], "repository_id": fork["repository_id"],
                                       "ref": "riscv64", "expected_head": fork["head_sha"],
                                       "base_sha": c["upstream"]["base_sha"]},
                               proposed={"marker": marker, "content_digest": artifact["digest"]}, now=now)
            if op["operation_id"] in operations:
                continue
            op = recovery.charge_launch(op, now)
            res.stage("operations", op)
            changed = compat.legacy_update(c, "controller.plan_development_pr",
                                           active_operation_id=op["operation_id"],
                                           activity="waiting_capacity", updated_at=now)
            by_id[cid] = changed
            res.stage("contributions", changed)
            res.report["dev_pr_requests"].append({
                "operation_id": op["operation_id"], "contribution_id": cid, "attempt": 1,
            })

    # Bootstrap CI once for an already recorded PR. This separate operation is
    # needed when GitHub registered no inherited workflows at PR creation time.
    for cid, c in list(by_id.items()):
        if not policy.get("destination_writes", False):
            break
        if len(res.report["dev_pr_requests"]) >= 1:
            break
        if cid in skip:
            continue
        with isolation.contribution(cid, "development_pr"):
            fork = c.get("fork") or {}
            artifact = c.get("proposal_artifact") or {}
            if c["phase"] != "development" or c.get("paused") or compat.waits_for_human(c) \
                    or c.get("active_operation_id") or fork.get("pr_number") is None \
                    or c.get("last_ci_evidence_id") or artifact.get("outcome") != "published":
                continue
            created_at_head = any(op["contribution_id"] == cid and op["kind"] == "dev_pr_create"
                                  and op["status"] == "confirmed"
                                  and op["target"].get("expected_head") == fork.get("head_sha")
                                  for op in operations.values())
            if not created_at_head:
                continue  # later patch pushes already emit pull_request:synchronize
            reason = f"ci-bootstrap-event-v2:{fork['head_sha']}"
            prior = [op for op in operations.values() if op["contribution_id"] == cid \
                     and op["kind"] == "dev_pr_update" and reason in op["reason_ids"] \
                     and op["target"].get("expected_head") == fork.get("head_sha")]
            if any(op["status"] in recovery.OPEN | {"confirmed"} for op in prior):
                continue
            # A re-plan after a failed or never-launched operation is a new
            # operation: an operation id is never reused once it closed.
            op = recovery.plan(contribution_id=cid, revision=c["revision"], kind="dev_pr_update",
                               policy_version=policy["policy_version"],
                               reason_ids=[reason] + ([f"replan:{len(prior)}"] if prior else []),
                               target={"repository": fork["name"], "repository_id": fork["repository_id"],
                                       "ref": "riscv64", "expected_head": fork["head_sha"],
                                       "base_sha": c["upstream"]["base_sha"]},
                               proposed={"marker": f"ci-bootstrap:{cid}:r{c['revision']}",
                                         "content_digest": artifact["digest"]}, now=now)
            if op["operation_id"] in operations:
                continue
            op = recovery.charge_launch(op, now)
            res.stage("operations", op)
            changed = compat.legacy_update(c, "controller.plan_ci_bootstrap",
                                           active_operation_id=op["operation_id"],
                                           activity="waiting_capacity", updated_at=now)
            by_id[cid] = changed
            res.stage("contributions", changed)
            res.report["dev_pr_requests"].append({
                "operation_id": op["operation_id"], "contribution_id": cid, "attempt": 1,
            })

    # A pre-fork proposal is immutable worker output. Once provisioning is
    # independently confirmed, launch the trusted publication path without a
    # second model call and fence duplicate worker selection durably.
    res.report["proposal_publications"] = []
    for cid, c in list(by_id.items()):
        if cid in skip:
            continue
        with isolation.contribution(cid, "proposal_publication"):
            artifact = c.get("proposal_artifact") or {}
            fork = c.get("fork") or {}
            if (not policy.get("destination_writes", False)
                    or c["phase"] != "development"
                    or compat.stopped(c)
                    # A wait for a human is left only by its own rows.
                    or compat.waits_for_human(c)
                    or artifact.get("outcome") != "proposal"
                    or not artifact.get("run_id")
                    or not fork.get("repository_id")
                    or c.get("pending_publication")
                    or c.get("publication_operation_id")):
                continue
            pending = {
                "worker_run_id": str(artifact["run_id"]),
                "attempts": 1,
                "last_reason": "verified pre-fork proposal awaiting trusted publication",
                "next_attempt_at": now,
                "exhausted": False,
            }
            changed = compat.legacy_update(c, "controller.publish_pre_fork_proposal",
                                           pending_publication=pending,
                                           activity="waiting_capacity", updated_at=now)
            by_id[cid] = changed
            res.stage("contributions", changed)
            res.report["proposal_publications"].append({
                "contribution_id": cid,
                "worker_run_id": str(artifact["run_id"]),
                "attempt": 1,
            })
            break

    # 8-10. Select bounded work, honouring capacity and one worker. In a
    # state of the table, Work is allocated by the row the table names for
    # it, and an exhausted budget takes its exhaustion row instead.
    evidence = evidence or {}
    candidates = []
    for c in by_id.values():
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "work_selection"):
            if c["contribution_id"] in source_blocked:
                continue
            kind = _actionable(c)
            if kind is None:
                continue
            if c["contribution_id"] in blocked_by_op:
                rep["deferred"].append({"contribution": c["contribution_id"], "why": "uncertain operation"})
                continue
            if engine.state_key(c) not in lifecycle.LIFECYCLE:
                # `development.idle`, a state the table does not have: the
                # legacy selection requests the first patch.
                why = _legacy_patch_refusal(c, policy, now)
                if why:
                    rep["deferred"].append({"contribution": c["contribution_id"], "why": why})
                    continue
                candidates.append((PRIORITY[kind], c["updated_at"], kind, c, None))
                continue
            due = _begun(c, policy, now)
            if due is None:
                continue
            ci = _recorded_ci_evidence(due, evidence)
            ops = _open_operations(due, operations)
            spent = next((verb for verb in EXHAUSTIONS.get(engine.state_key(due), ())
                          if engine.refusal(due, verb, policy=policy, now=now, ci_evidence=ci,
                                            open_operations=ops) is None), None)
            if spent:
                _exhaust(c, due, spent, ci, by_id, res, policy, now)
                continue
            verb, why = _allocation_verb(due, ci, ops, policy, now)
            if verb is None:
                rep["deferred"].append({"contribution": c["contribution_id"], "why": why})
                continue
            kind = task_for(verb, due)
            candidates.append((PRIORITY[kind], c["updated_at"], kind, c, verb))
    candidates.sort(key=lambda x: (x[0], x[1]))
    slots = policy["concurrency"]["coding_workers"] - sum(
        1 for c in by_id.values() if c["activity"] == "agent_running" or c.get("active_work"))
    pressure = inputs.pressure
    for _p, _t, kind, c, verb in candidates:
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "work_selection"):
            if slots <= 0:
                rep["deferred"].append({"contribution": c["contribution_id"], "why": "no worker slot"})
                continue
            if kind in WRITE_KINDS:
                ok, why = capacity.admit_expensive(
                    pressure or capacity.Pressure(ok=False, reason="no usage data"),
                    reserved=capacity.reserved_jobs(operations.values()), estimated_jobs=1, policy=policy)
                if not ok:
                    rep["deferred"].append({"contribution": c["contribution_id"], "why": why})
                    continue
            slots -= 1
            if verb is None:
                claimed = _legacy_patch(c, kind, inputs, now)
                res.stage("contributions", claimed)
            else:
                claimed = c
                if engine.state_key(c) == "queued.idle":
                    claimed = _take(c, "begin_investigation", res, policy, now,
                                    cause_id=f"tick-r{c['revision']}")
                ci = evidence.get(claimed.get("last_ci_evidence_id") or "")
                cluster = engine.recorded_failing_job(claimed, ci)[1]
                request = _work_request(claimed, kind, inputs, now)
                claimed = _take(claimed, verb, res, policy, now, cause_id=request["dedupe_key"],
                                facts=ALLOCATION_FACTS, values={"active_work": request},
                                ci_evidence=ci,
                                open_operations=_open_operations(claimed, operations),
                                details={"task": kind, "work": request["dedupe_key"],
                                         **({"cluster": cluster} if cluster else {})})
                carried = claimed["active_work"].get("feedback_evidence_ids") or []
                if carried != list(c.get("pending_feedback_evidence_ids") or []):
                    raise lifecycle.TransitionError(
                        f"{verb}: the Work carries {carried}, not the pending feedback")
            by_id[c["contribution_id"]] = claimed
            aw = claimed["active_work"]
            work = {
                "type": "submit_work",
                "dedupe_key": aw["dedupe_key"],
                "contribution_id": c["contribution_id"],
                "generation": aw["generation"],
                "revision": aw["revision"],
                "expected_head": (claimed.get("fork") or {}).get("head_sha"),
                "task": kind,
                "reason_ids": aw["reason_ids"],
                "feedback_evidence_ids": list(aw.get("feedback_evidence_ids") or []),
                "effect_contract": (
                    {"kind": "none"}
                    if kind == "investigation"
                    else {"kind": "proposal_artifact"}
                    if kind == "development_patch" and not claimed.get("fork")
                    else {
                        "kind": "fork_branch",
                        "repository": (claimed.get("fork") or {}).get("name"),
                        "ref": "riscv64",
                    }
                ),
            }
            res.requested_effects.append(work)
            rep["selected"].append({"contribution": c["contribution_id"], "task": kind})

    if policy["read_only"]:
        rep["note"] = "read-only: requested effects are reported, not executed"
    return res


class MissingEvidence(LookupError):
    """The evidence a record names is not in the state store."""


def _recorded_ci_evidence(record: dict, evidence: dict[str, dict]) -> dict | None:
    """The evidence at ``last_ci_evidence_id``, which the allocation and
    exhaustion rows read to decide the failing CI job of the head.

    Evidence is never removed from the store, so a record that names
    evidence the store does not hold cannot be decided: it is an unexpected
    error of that contribution (``isolation.ProcessingError``), which
    ``pause_on_processing_error`` records and posts on its tracking issue."""
    evidence_id = record.get("last_ci_evidence_id")
    if evidence_id and evidence_id not in evidence:
        raise MissingEvidence(f"last_ci_evidence_id {evidence_id} names evidence that is "
                              "not in the state store")
    return evidence.get(evidence_id or "")


def _take(record: dict, verb: str, res: TickResult, policy: dict, now: str, **kwargs) -> dict:
    """Take a table row the caller established, and stage the record and the
    event. A refusal here is a bug of the caller."""
    out = engine.apply_transition(record, verb, policy=policy, now=now, **kwargs)
    if not out.accepted:
        raise lifecycle.TransitionError(f"{verb} refused in {engine.state_key(record)}: {out.reason}")
    res.stage("contributions", out.record)
    res.stage("events", out.event)
    return out.record


def _begun(record: dict, policy: dict, now: str) -> dict | None:
    """The record as it is once its investigation has begun: a request in
    `queued.idle` takes `begin_investigation` in the tick that allocates its
    Work or exhausts its budget. ``None`` when it cannot begin."""
    if engine.state_key(record) != "queued.idle":
        return record
    out = engine.apply_transition(record, "begin_investigation", policy=policy, now=now)
    return out.record if out.accepted else None


def _allocation_verb(record: dict, ci: dict | None, ops: tuple[str, ...], policy: dict,
                     now: str) -> tuple[str | None, str | None]:
    """The row that allocates the Work due in this state, once a worker slot
    and capacity are free, or the reason none applies."""
    reasons = []
    for verb in ALLOCATIONS.get(engine.state_key(record), ()):
        why = engine.refusal(record, verb, policy=policy, now=now, facts=ALLOCATION_FACTS,
                             ci_evidence=ci, open_operations=ops)
        if why is None:
            return verb, None
        reasons.append(why)
    if not reasons:
        return None, f"no Work is allocated in {engine.state_key(record)}"
    return None, "; ".join(dict.fromkeys(reasons))


def _exhaustion_values(record: dict, verb: str, ci: dict | None, policy: dict,
                       now: str) -> tuple[dict, dict]:
    """The values an exhaustion row sets that the engine cannot derive, and
    the event details that state the spent counters."""
    if verb == "exhaust_work_total":
        return {}, {"counter": "work_total",
                    "count": int(record["budget"].get("attempts_used", 0)),
                    "limit": lifecycle.budget_limit("work_total", policy)}
    if engine.state_key(record) == "upstream_review.fix_needed":
        spent = engine.exhausted(record, ("no_progress", "work_total", "repair_attempts_cluster"),
                                 policy=policy, now=now, ci_evidence=ci)
        return {lifecycle.PENDING_COUNTER: spent[0]}, {"exhausted": spent}
    spent = engine.exhausted(record, ("repair_attempts_cluster", "no_progress"), policy=policy,
                             now=now, ci_evidence=ci)
    cluster = engine.recorded_failing_job(record, ci)[1]
    details = {"exhausted": spent, **({"cluster": cluster} if cluster else {})}
    text = f"repair budgets exhausted: {', '.join(spent)}"
    if verb == "refresh_on_exhaustion":
        fork = record.get("fork") or {}
        return {"refresh": {"command_id": f"refresh_on_exhaustion:r{record['revision']}",
                            "reason": text, "from_phase": record["phase"],
                            "head_sha": fork.get("head_sha"),
                            "pr_number": fork.get("pr_number")}}, details
    return {"parked_reason": f"{text}, after the automatic refresh"}, details


def _exhaust(record: dict, due: dict, verb: str, ci: dict | None, by_id: dict, res: TickResult,
             policy: dict, now: str) -> None:
    """Take the exhaustion row ``verb`` from ``due`` (``record`` once its
    investigation has begun)."""
    if due is not record:
        due = _take(record, "begin_investigation", res, policy, now,
                    cause_id=f"tick-r{record['revision']}")
    values, details = _exhaustion_values(due, verb, ci, policy, now)
    changed = _take(due, verb, res, policy, now, cause_id=f"tick-r{due['revision']}",
                    values=values, ci_evidence=ci, details=details)
    by_id[record["contribution_id"]] = changed
    res.report.setdefault("exhausted", []).append(
        {"contribution": record["contribution_id"], "verb": verb, **details})


def _work_request(record: dict, kind: str, inputs: Inputs, now: str) -> dict:
    """The requested Work (``active_work``) for ``kind`` at the record's
    generation and revision. Its reason ids are the pending feedback it
    carries, then the CI evidence and the dossier."""
    from datetime import timedelta

    generation, revision = int(record.get("generation", 0)), record["revision"]
    reason_ids: list[str] = []
    if kind in ("development_repair", "upstream_revision"):
        reason_ids = list(dict.fromkeys(
            list(record.get("pending_feedback_evidence_ids") or [])
            + ([record["last_ci_evidence_id"]] if record.get("last_ci_evidence_id") else [])
            + ([record["dossier_evidence_id"]] if record.get("dossier_evidence_id") else [])))
    if not reason_ids and record.get("dossier_evidence_id"):
        reason_ids = [record["dossier_evidence_id"]]
    request = {
        "dedupe_key": work_queue.work_key(record["contribution_id"], generation, revision, kind),
        "task": kind, "generation": generation, "revision": revision, "attempt": 1,
        "requested_at": now,
        "deadline_at": (inputs.now + timedelta(hours=WORK_DEADLINE_HOURS)).isoformat(),
        "dispatched_run_id": None, "reason_ids": reason_ids}
    frozen_issue = record["tracking_issue"]
    keys = ("id", "number", "url", "description", "description_digest")
    if all(key in frozen_issue for key in keys):
        request["tracking_issue"] = {key: frozen_issue[key] for key in keys}
    else:
        # Compatibility for records created before intake-level freezing.
        issue = inputs.tracking_issues.get(record["tracking_issue"]["number"]) or next(
            (i for i in inputs.issues if i["number"] == record["tracking_issue"]["number"]), None)
        if issue is not None:
            request["tracking_issue"] = collect.tracking_issue_context(issue, frozen_issue)
    return request


def _legacy_patch_refusal(record: dict, policy: dict, now: str) -> str | None:
    """Why the legacy selection in `development.idle` requests no first
    patch: the admission of ``request_patch``, and its budgets."""
    why = (lifecycle.proposal_admission(record) if not record.get("fork")
           else lifecycle.write_admission(record))
    if why:
        return why
    spent = engine.exhausted(record, ("work_total", "no_progress"), policy=policy, now=now)
    return f"{' and '.join(spent)} exhausted" if spent else None


def _legacy_patch(record: dict, kind: str, inputs: Inputs, now: str) -> dict:
    """Request the first patch from `development.idle`, a state the table
    does not have. Like ``request_patch``, the Work carries the pending
    feedback and the allocation charges ``work_total``."""
    request = dict(_work_request(record, kind, inputs, now),
                   feedback_evidence_ids=list(record.get("pending_feedback_evidence_ids") or []))
    budget = dict(record["budget"], attempts_used=int(record["budget"].get("attempts_used", 0)) + 1)
    return compat.legacy_update(record, "controller.select_work", activity="agent_running",
                                updated_at=now, active_work=request, budget=budget)


def reminder_details(record: dict) -> dict:
    """What a reminder repeats: the pending human action, when it was
    requested, and, where the step binds to a head, the revision and head."""
    pending = record["pending_human_action"]
    heads = {"handoff.awaiting_submission": (record.get("handoff_request") or {}).get("head"),
             "handoff.waiting_upstream_pr": record.get("clean_head_sha")}
    state = engine.state_key(record)
    return {"pending_human_action": pending["kind"], "requested_at": pending["requested_at"],
            "previous_reminder_at": pending.get("reminded_at"),
            **({"bound_to": {"revision": record["revision"], "head": heads[state]}}
               if state in heads else {})}


def _open_operations(record: dict, operations: dict) -> tuple[str, ...]:
    """The open operations of a record: those of the store, and the one the
    record names as active, also when the store does not hold it."""
    return recovery.open_operations(record["contribution_id"], operations,
                                    record.get("active_operation_id"))


# A planned operation is launched again once this long has passed since its
# last launch without a receipt, within `operation_launch`.
LAUNCH_INTERVAL_SECONDS = 300
# The pending human action of `require_provisioning_intervention` when
# `operation_launch` is exhausted.
LAUNCH_EXHAUSTED = "provisioning_launch_exhausted"
# The pending human action of `require_provisioning_intervention` when the
# provisioning operation failed.
PROVISIONING_FAILED = "provisioning_operation_failed"


def _launch_provisioning(c: dict, operation: dict, by_id: dict, operations: dict, inputs: Inputs,
                         res: TickResult, now: str, policy: dict, *,
                         capacity_checked: bool = False) -> bool:
    """Launch the planned fork provisioning operation of a record in
    `provisioning.idle` by the table: `start_operation`, which charges
    `operation_launch` (counted on the operation), or, once that budget is
    exhausted, `require_provisioning_intervention`. Returns whether a launch
    was requested."""
    cid, oid = c["contribution_id"], operation["operation_id"]
    if engine.state_key(c) != "provisioning.idle":
        return False  # no other state has a row that launches it
    counts = recovery.launch_count(operation)
    attempts = recovery.launch_attempts(operation)
    open_ops = recovery.open_operations(cid, {**operations, oid: operation}, oid)
    facts = {"fork_provision_planned"}
    if capacity_checked:
        facts.add("capacity_available")
    else:
        others = [op for op in operations.values() if op["operation_id"] != oid]
        ok, _why = capacity.admit_expensive(
            inputs.pressure or capacity.Pressure(ok=False, reason="no usage data"),
            reserved=capacity.reserved_jobs(others), estimated_jobs=1, policy=policy)
        if ok:
            facts.add("capacity_available")
    why = engine.refusal(c, "start_operation", policy=policy, now=now, facts=facts, counts=counts,
                         open_operations=open_ops)
    if why is None:
        out = engine.apply_transition(c, "start_operation", policy=policy, now=now, cause_id=oid,
                                      facts=facts, counts=counts, open_operations=open_ops,
                                      details={"operation": oid, "attempt": attempts + 1})
        if out.charged != ("operation_launch",):
            raise lifecycle.TransitionError(f"start_operation charged {out.charged}")
        launched = recovery.charge_launch(operation, now)
        operations[oid] = launched
        by_id[cid] = out.record
        res.stage("operations", launched)
        res.stage("contributions", out.record)
        res.stage("events", out.event)
        res.report["provision_requests"].append({
            "operation_id": oid, "contribution_id": cid, "attempt": attempts + 1})
        return True
    if not c.get("paused") and attempts >= lifecycle.budget_limit("operation_launch", policy):
        out = engine.apply_transition(
            c, "require_provisioning_intervention", policy=policy, now=now, cause_id=oid,
            counts=counts, open_operations=open_ops,
            values={"pending_human_action": LAUNCH_EXHAUSTED,
                    lifecycle.PENDING_COUNTER: "operation_launch"},
            details={"operation": oid, "launches": attempts})
        if not out.accepted:
            raise lifecycle.TransitionError(f"require_provisioning_intervention refused: {out.reason}")
        by_id[cid] = out.record
        res.stage("contributions", out.record)
        res.stage("events", out.event)
        return False
    res.report.setdefault("deferred", []).append({"contribution": cid, "why": why})
    return False


def _command_target(decision, by_id: dict, operations: dict) -> str | None:
    """The contribution a command comment concerns, or ``None``: the one its
    operation belongs to for ``resolve-operation``, else the one tracked by
    the issue the comment was posted on."""
    cmd = decision.command
    if cmd is not None and cmd.verb == "resolve-operation":
        return (operations.get(cmd.args.get("operation")) or {}).get("contribution_id")
    rec = intake.contribution_for_issue(by_id.values(), decision.receipt.get("issue_number"))
    return rec["contribution_id"] if rec else None


def _refusal(record: dict, cmd: Command, why: str) -> str:
    """The reply to a command the table refuses, in the words the command
    feedback recognizes for a pause or resume that has nothing to do."""
    if cmd.verb == "pause" and record.get("paused") and why == "contribution is paused":
        return "already paused"
    state = lifecycle.LIFECYCLE.get(engine.state_key(record))
    if cmd.verb == "resume" and not record.get("paused") and state is not None \
            and state.kind not in ("human_wait", "generation_end"):
        return "not paused"
    return why


def admission_override(cmd: Command) -> dict | None:
    """The admission override a resume from ``parked.idle`` records: its
    ``class`` and ``paths`` with the command's reason, or ``None`` when it
    names neither. Raises ``ValueError`` for an unsafe scope path."""
    if "class" not in cmd.args and "paths" not in cmd.args:
        return None
    override: dict = {"reason": cmd.args["reason"]}
    if "class" in cmd.args:
        override["class"] = cmd.args["class"]
    if "paths" in cmd.args:
        override["paths"] = validate_scope_paths([g for g in cmd.args["paths"].split(",") if g])
    return override


def _command_values(rec: dict, verb: str, cmd: Command, receipt: dict) -> dict:
    """The values of the fields a command's row sets that the engine cannot
    derive: the refresh a refresh or a stale resume records, and the
    admission override of a restart."""
    if verb == "restart_generation":
        return {"admission_override": admission_override(cmd)}
    if verb not in ("refresh_investigation", "resume_stale"):
        return {}
    fork = rec.get("fork") or {}
    return {"refresh": {"command_id": receipt["command_id"], "reason": cmd.args["reason"],
                        "from_phase": rec["phase"], "head_sha": fork.get("head_sha"),
                        "pr_number": fork.get("pr_number")}}


# Optional command fields, and the guards or verbs that read them. A field a
# command gives that the transition it takes does not read is refused.
_HEAD_GUARDS = frozenset({"exact candidate head", "exact clean head"})


def _unread_field(cmd: Command, verb: str, state: str) -> str | None:
    """Why the command gives a field the transition ``verb`` does not read."""
    if ("class" in cmd.args or "paths" in cmd.args) and verb != "restart_generation":
        return ("`class` and `paths` apply only to a resume from `parked.idle` that starts a new "
                "generation")
    transition, _target = engine.resolve(state, verb)
    if "head" in cmd.args and not _HEAD_GUARDS & set(transition.guard):
        return f"`head` does not apply to `/upstreaming {cmd.verb}` in `{state}`"
    return None


# The pending actions with which the controller leaves a record in
# `provisioning.waiting_human`, each with the counter it names: exhausted
# launches of the planned operation, and a provisioning operation that failed.
PROVISIONING_WAITS = frozenset({(LAUNCH_EXHAUSTED, "operation_launch"),
                                (PROVISIONING_FAILED, None)})


def _command_facts(rec: dict, cmd: Command) -> frozenset[str]:
    """The facts a command establishes by itself. A human `/upstreaming
    resume` of a provisioning wait the controller entered is the human's
    assertion that the prerequisite is corrected."""
    pending = rec.get("pending_human_action") or {}
    if cmd.verb == "resume" and engine.state_key(rec) == "provisioning.waiting_human" \
            and (pending.get("kind"), pending.get("counter")) in PROVISIONING_WAITS:
        return frozenset({"prerequisite_corrected"})
    return frozenset()


def _apply_table_command(rec: dict, cmd: Command, receipt: dict, operations: dict,
                         policy: dict, now: str) -> engine.Outcome:
    """Apply a command to a record in a table state, as the table defines it."""
    open_ops = _open_operations(rec, operations)
    counts = recovery.launch_count(operations.get(rec.get("active_operation_id")))
    facts = _command_facts(rec, cmd)
    verb, why = engine.resolve_command(rec, cmd, policy=policy, now=now, counts=counts,
                                       open_operations=open_ops, facts=facts)
    if verb is None:
        return engine.Outcome(rec, False, _refusal(rec, cmd, why), None)
    unread = _unread_field(cmd, verb, engine.state_key(rec))
    if unread:
        return engine.Outcome(rec, False, unread, None, verb)
    if cmd.verb in engine.HELD_COMMANDS:
        return engine.Outcome(rec, False, engine.HELD_COMMANDS[cmd.verb], None, verb)
    try:
        values = _command_values(rec, verb, cmd, receipt)
    except ValueError as error:
        return engine.Outcome(rec, False, str(error), None, verb)
    details = {}
    pending = (rec.get("pending_human_action") or {}).get("kind")
    if verb == "resume_pause" and pending:
        # A resume clears the flag only; the reply names the wait that remains.
        details["pending_human_action"] = pending
    if verb == "abandon":
        # The table records `terminal_reason=abandoned`; the event keeps the
        # reason the human gave.
        details["reason"] = cmd.args["reason"]
    if values.get("admission_override"):
        details["admission_override"] = values["admission_override"]
    return engine.apply_transition(rec, verb, policy=policy, now=now,
                                   cause_id=receipt["command_id"], command=cmd,
                                   facts=facts, values=values, counts=counts,
                                   open_operations=open_ops, actor=receipt["actor"],
                                   details=details)


def apply_command(rec: dict, cmd: Command, receipt: dict, *, operations: dict, policy: dict,
                  now: str) -> engine.Outcome:
    """Apply an authenticated command to the record it concerns: through the
    transition engine where the table defines it (``engine.takes_command``),
    through the compatibility layer otherwise."""
    if engine.takes_command(rec, cmd.verb):
        return _apply_table_command(rec, cmd, receipt, operations, policy, now)
    open_ops = _open_operations(rec, operations)
    if cmd.verb == "park" and open_ops:
        # `park_contribution` requires no open operation, in every state.
        return engine.Outcome(rec, False, f"operation {open_ops[0]} is open", None)
    out = compat.apply_command(rec, cmd, command_id=receipt["command_id"],
                               actor=receipt["actor"], now=now)
    return engine.Outcome(out.record, out.accepted, out.reason, out.event)


# What a human resolution, or a trusted receipt of a definite failure,
# establishes for the lifecycle row an operation fires, by operation kind and
# closed status: the facts and the values of the fields the row sets. A row
# whose guards a resolution does not establish (`confirm_provisioning` needs
# the receipt and its readback) is not taken.
RESOLUTION_FIRES: dict[tuple[str, str], tuple[frozenset[str], dict]] = {
    ("fork_provision", "failed"): (frozenset({"provisioning_blocked"}),
                                   {"pending_human_action": PROVISIONING_FAILED}),
}


def _resolve_operation(cmd: Command, receipt: dict, by_id: dict, operations: dict,
                       inputs: Inputs, res: TickResult, now: str, policy: dict) -> dict:
    """`/upstreaming resolve-operation` by the operation table: the one
    `resolve_*` row whose guards hold. The authority of the actor was checked
    at intake. An operation that reaches `confirmed` or `failed` fires its
    lifecycle row when the resolution establishes the row's guards; the
    record's reference to the closed operation is released."""
    op = operations.get(cmd.args["operation"])
    if op is None:
        return dict(receipt, result="rejected", reason="unknown operation")
    evidence = inputs.resolution_evidence.get(cmd.args["evidence"])
    try:
        new = recovery.resolve(op, cmd, actor=receipt["actor"], command_id=receipt["command_id"],
                               evidence=evidence, now=now)
    except recovery.RecoveryError as e:
        return dict(receipt, result="rejected", reason=str(e))
    operations[op["operation_id"]] = new
    res.stage("operations", new)
    rec = by_id.get(new["contribution_id"])
    if rec is None:
        return receipt
    facts, values = RESOLUTION_FIRES.get((new["kind"], new["status"]), (frozenset(), {}))
    fired = None
    if engine.state_key(rec) in lifecycle.LIFECYCLE and recovery.fired_verbs(rec, new) \
            and (facts or values):
        fired = recovery.fire(rec, new, policy=policy, now=now, facts=facts, values=values,
                              cause_id=receipt["command_id"],
                              open_operations=_open_operations(rec, operations))
    changed = fired.record if fired is not None and fired.accepted else recovery.release(rec, new, now)
    if changed is not rec:
        by_id[rec["contribution_id"]] = changed
        res.stage("contributions", changed)
    if fired is not None and fired.accepted:
        res.stage("events", fired.event)
    elif fired is not None:
        return dict(receipt, reason=f"the lifecycle row is not taken: {fired.reason}")
    return receipt


def _apply(cmd: Command, receipt: dict, by_id: dict, operations: dict, inputs: Inputs,
           res: TickResult, now: str, policy: dict) -> dict:
    if cmd.verb == "resolve-operation":
        return _resolve_operation(cmd, receipt, by_id, operations, inputs, res, now, policy)
    # The contribution is the one tracked by the issue the comment was posted on.
    rec = intake.contribution_for_issue(by_id.values(), receipt["issue_number"])
    if rec is None:
        return dict(receipt, result="rejected", reason="this issue tracks no contribution")
    if cmd.contribution is not None and cmd.contribution != rec["contribution_id"]:
        return dict(receipt, result="rejected",
                    reason=f"command names {cmd.contribution}, but this issue tracks {rec['contribution_id']}")
    receipt = dict(receipt, contribution_id=rec["contribution_id"],
                   revision=cmd.revision if cmd.revision is not None else rec["revision"])
    try:
        out = apply_command(rec, cmd, receipt, operations=operations, policy=policy, now=now)
    except lifecycle.TransitionError as e:
        return dict(receipt, result="rejected", reason=str(e))
    if not out.accepted:
        return dict(receipt, result="rejected", reason=out.reason)
    by_id[rec["contribution_id"]] = out.record
    res.stage("contributions", out.record)
    if out.event:
        res.stage("events", out.event)
    _restart_launches(out, operations, res, now)
    return receipt


def _restart_launches(out: engine.Outcome, operations: dict, res: TickResult, now: str) -> None:
    """A resume that starts `operation_launch` again starts the launches of
    the record's planned operation again: that is where the counter lives."""
    op = operations.get(out.record.get("active_operation_id"))
    if (out.restarted or {}).get("counter") == "operation_launch" and op \
            and op["status"] == "planned":
        op = recovery.restart_launches(op, now)
        operations[op["operation_id"]] = op
        res.stage("operations", op)


def cmd_observe(args: argparse.Namespace) -> int:
    """Run one read-only observation tick and print its deterministic report."""
    import reconcile
    import state

    policy = load_policy()
    root = Path(args.root) if args.root else repository_root()
    try:
        pending_migrations = state.pending_text_migrations(root)
    except state.StateError as error:
        print(f"legacy state text migration refused: {error}", file=sys.stderr)
        return 1
    if pending_migrations:
        report = reconcile.state_text_migration_report(
            [], pending=[migration.path for migration in pending_migrations]
        )
        print(json.dumps(report, indent=2))
        return 0
    try:
        pending_records = state.pending_record_migrations(root, policy)
    except state.StateError as error:
        print(f"record migration refused: {error}", file=sys.stderr)
        return 1
    if pending_records:
        print(json.dumps(reconcile.migration_report("record_migration", [],
                                                    pending=pending_records), indent=2))
        return 0
    store = state.StateStore(root, policy)
    contributions = dict(store.load_all("contributions"))
    operations = {
        value["operation_id"]: value for value in store.load_all("operations").values()
    }
    consumed = {
        value["comment_id"]: value for value in store.load_all("commands").values()
    }
    projects = {value["project_id"]: value for value in store.load_all("projects").values()}
    evidence = {value["evidence_id"]: value for value in store.load_all("evidence").values()}
    now = datetime.now(timezone.utc)
    if args.offline:
        observed_pressure = capacity.Pressure(ok=False, reason="offline")
    else:
        try:
            usage_snapshot, fetched_at = collect.usage(policy["capacity"]["usage_url"])
            observed_pressure = capacity.pressure(
                policy, usage_snapshot, fetched_at=fetched_at, now=now
            )
        except Exception as error:
            observed_pressure = capacity.Pressure(
                ok=False, reason=f"usage unavailable: {error}"
            )
    api = collect.Api()
    issues = collect.intake_issues(api) if not args.offline else []
    issue_numbers = sorted(
        {record["tracking_issue"]["number"] for record in contributions.values()}
        | {issue["number"] for issue in issues}
    )
    comments = (
        collect.command_comments(api, issue_numbers, None) if not args.offline else []
    )
    if args.offline:
        tracking_issues, upstream_snapshots = {}, {}
    else:
        tracking_issues = collect.tracking_issues(api, contributions.values(), issues)
        upstream_snapshots = collect.upstream_snapshots(
            api, contributions.values(), issues, policy
        )
    result = tick(
        contributions=contributions,
        operations=operations,
        consumed=consumed,
        projects=projects,
        policy=dict(policy, read_only=True),
        inputs=Inputs(
            now=now,
            issues=issues,
            comments=comments,
            pressure=observed_pressure,
            upstream_snapshots=upstream_snapshots,
            tracking_issues=tracking_issues,
        ),
        evidence=evidence,
    )
    result.report["would_stage"] = [
        state.record_path(kind, record)
        if kind != "events"
        else f"events/{record['contribution_id']}"
        for kind, record in result.staged
    ]
    result.report["requested_effects"] = result.requested_effects
    print(json.dumps(result.report, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m controller")
    subcommands = parser.add_subparsers(dest="command", required=True)
    observe_parser = subcommands.add_parser("observe")
    observe_parser.add_argument("root", nargs="?")
    observe_parser.add_argument("--offline", action="store_true")
    observe_parser.set_defaults(handler=cmd_observe)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
