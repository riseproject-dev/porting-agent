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
from commands import URGENT, Command
from common import CONTROL_REPO, INTAKE_LABEL, contribution_file_id, load_policy, repository_root


@dataclass
class Inputs:
    now: datetime
    issues: list[dict] = field(default_factory=list)          # intake issues, freshly fetched
    tracking_issues: dict[int, dict] = field(default_factory=dict)  # every live issue's fresh description
    upstream_snapshots: dict[str, dict] = field(default_factory=dict)  # verified source ID / initial base
    comments: list[dict] = field(default_factory=list)        # {"issue_number", "comment", "event_body"}
    pressure: capacity.Pressure | None = None
    verified_prs: dict[str, dict] = field(default_factory=dict)  # url -> verified PR resource
    users: dict[str, dict | None] = field(default_factory=dict)  # login -> {"id","login"} or None
    provisioning: dict[str, dict] = field(default_factory=dict)  # contribution -> verify_provisioning()
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


def tick(*, contributions: dict[str, dict], operations: dict[str, dict], consumed: dict[int, dict],
         projects: dict[str, dict], policy: dict, inputs: Inputs,
         skip: frozenset[str] = frozenset()) -> TickResult:
    """``skip`` names the contributions this tick does not process: nothing
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
                        or not record.get("dossier_evidence_id")):
                    continue
                if operation.get("status") != "planned":
                    destination_blocked.add(cid)
                    rep["alerts"].append({
                        "contribution": cid,
                        "alert": "destination operation exists but is not provably unlaunched",
                    })
                    continue
                retired = dict(operation, status="not_launched", updated_at=now)
                schemas.validate("operation", retired)
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

    # Removing the intake label cancels only an accepted request that has not
    # requested any Work yet. Once active_work exists, explicit lifecycle
    # commands are required so an unlabeled event cannot silently stop work.
    for cid, c in list(by_id.items()):
        if cid in skip:
            continue
        with isolation.contribution(cid, "intake_label"):
            issue = inputs.tracking_issues.get(c["tracking_issue"]["number"])
            labels = {label.get("name") if isinstance(label, dict) else label
                      for label in (issue or {}).get("labels", [])}
            unprocessed = c["phase"] == "queued" and c["activity"] == "idle" \
                and not c.get("active_work") and not c.get("dossier_evidence_id") and not c.get("fork")
            if issue is None or INTAKE_LABEL in labels or not unprocessed:
                continue
            changed = compat.legacy_update(
                c, "controller.cancel_unlabeled_request", phase="abandoned", activity="idle",
                terminal_reason=f"{INTAKE_LABEL} label removed before Work admission", updated_at=now)
            schemas.validate("contribution", changed)
            by_id[cid] = changed
            active.pop(c["upstream"]["full_name"], None)
            res.stage("contributions", changed)
            res.stage("events", lifecycle._event(c, changed, "intake:label_removed", "observation",
                                                  f"issue-{c['tracking_issue']['id']}", now, False))
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
    res.report["provision_requests"] = []
    if policy.get("destination_writes", False):
        for cid, c in list(by_id.items()):
            if cid in skip:
                continue
            with isolation.contribution(cid, "provisioning"):
                operation = operations.get(c.get("active_operation_id"))
                if operation and operation.get("kind") == "fork_provision" \
                        and operation.get("status") == "planned":
                    attempts = int((operation.get("external_ids") or {}).get("launch_attempts", 0))
                    due = attempts == 0 or (
                        (inputs.now - datetime.fromisoformat(operation["updated_at"])).total_seconds() >= 300
                        and attempts < 3
                    )
                    if due:
                        launched = dict(operation, status="planned", updated_at=now,
                                        external_ids=dict(operation.get("external_ids") or {},
                                                          launch_attempts=attempts + 1))
                        schemas.validate("operation", launched)
                        operations[launched["operation_id"]] = launched
                        res.stage("operations", launched)
                        res.report["provision_requests"].append({
                            "operation_id": launched["operation_id"], "contribution_id": cid,
                            "attempt": attempts + 1,
                        })
                        break
                if c.get("fork") is None and c["phase"] == "development" \
                        and (c.get("proposal_artifact") or {}).get("outcome") == "proposal":
                    prior = [op for op in operations.values()
                             if op.get("contribution_id") == cid and op.get("kind") == "fork_provision"]
                    if len(prior) == 1 and prior[0]["status"] == "not_launched":
                        operation = dict(
                            prior[0], status="planned", updated_at=now,
                            external_ids=dict(prior[0].get("external_ids") or {}, launch_attempts=1),
                        )
                        changed = compat.legacy_update(
                            c, "controller.rearm_provisioning", phase="provisioning",
                            activity="waiting_capacity",
                            active_operation_id=operation["operation_id"], updated_at=now)
                        schemas.validate("operation", operation)
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
        if len(res.report["provision_requests"]) >= 1:
            break
        if cid in skip:
            continue
        with isolation.contribution(cid, "provisioning"):
            if c["phase"] != "provisioning" or c.get("paused") \
                    or c.get("active_operation_id") or cid in source_blocked:
                continue
            if (c.get("pending_human_action") or {}).get("kind") not in (None, "provision_fork"):
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
            op = dict(op, status="planned", external_ids={"launch_attempts": 1})
            schemas.validate("operation", op)
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
                attempts = int((operation.get("external_ids") or {}).get("launch_attempts", 0))
                due = attempts == 0 or (
                    (inputs.now - datetime.fromisoformat(operation["updated_at"])).total_seconds() >= 300
                    and attempts < 3
                )
                if due:
                    launched = dict(operation, updated_at=now,
                                    external_ids=dict(operation.get("external_ids") or {},
                                                      launch_attempts=attempts + 1))
                    schemas.validate("operation", launched)
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
            if c["phase"] != "development" or c.get("paused") \
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
            op = dict(op, external_ids={"launch_attempts": 1})
            schemas.validate("operation", op)
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
            if c["phase"] != "development" or c.get("paused") \
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
            op = recovery.plan(contribution_id=cid, revision=c["revision"], kind="dev_pr_update",
                               policy_version=policy["policy_version"], reason_ids=[reason],
                               target={"repository": fork["name"], "repository_id": fork["repository_id"],
                                       "ref": "riscv64", "expected_head": fork["head_sha"],
                                       "base_sha": c["upstream"]["base_sha"]},
                               proposed={"marker": f"ci-bootstrap:{cid}:r{c['revision']}",
                                         "content_digest": artifact["digest"]}, now=now)
            op = dict(op, external_ids={"launch_attempts": 1})
            schemas.validate("operation", op)
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

    # 8-10. Select bounded work, honouring capacity and one worker.
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
            if kind in WRITE_KINDS:
                why = (
                    lifecycle.proposal_admission(c)
                    if kind == "development_patch" and not c.get("fork")
                    else lifecycle.write_admission(c)
                )
                if why:
                    rep["deferred"].append({"contribution": c["contribution_id"], "why": why})
                    continue
            candidates.append((PRIORITY[kind], c["updated_at"], kind, c))
    candidates.sort(key=lambda x: (x[0], x[1]))
    slots = policy["concurrency"]["coding_workers"] - sum(
        1 for c in by_id.values() if c["activity"] == "agent_running" or c.get("active_work"))
    pressure = inputs.pressure
    for _p, _t, kind, c in candidates:
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
            reason_ids = []
            if kind in ("development_repair", "upstream_revision"):
                reason_ids = list(dict.fromkeys(
                    (c.get("pending_feedback_evidence_ids") or [])
                    + ([c["last_ci_evidence_id"]] if c.get("last_ci_evidence_id") else [])
                    + ([c["dossier_evidence_id"]] if c.get("dossier_evidence_id") else [])))
            if not reason_ids and c.get("dossier_evidence_id"):
                reason_ids = [c["dossier_evidence_id"]]
            work = {
                "type": "submit_work",
                "dedupe_key": f"{c['contribution_id']}@{c['revision']}:{kind}",
                "contribution_id": c["contribution_id"],
                "revision": c["revision"],
                "expected_head": (c.get("fork") or {}).get("head_sha"),
                "task": kind,
                "reason_ids": reason_ids,
                "effect_contract": (
                    {"kind": "none"}
                    if kind == "investigation"
                    else {"kind": "proposal_artifact"}
                    if kind == "development_patch" and not c.get("fork")
                    else {
                        "kind": "fork_branch",
                        "repository": (c.get("fork") or {}).get("name"),
                        "ref": "riscv64",
                    }
                ),
            }
            res.requested_effects.append(work)
            rep["selected"].append({"contribution": c["contribution_id"], "task": kind})
            # Record the request in the same state commit, so the next tick does
            # not request it again. Cleared by the task's outcome or by timeout.
            from datetime import timedelta
            claimed = compat.legacy_update(
                c, "controller.select_work", activity="agent_running", updated_at=now,
                phase="investigation" if kind == "investigation" and c["phase"] == "queued"
                else c["phase"],
                active_work={
                "dedupe_key": work["dedupe_key"], "task": kind, "revision": c["revision"], "attempt": 1,
                "requested_at": now,
                "deadline_at": (inputs.now + timedelta(hours=WORK_DEADLINE_HOURS)).isoformat(),
                "dispatched_run_id": None, "reason_ids": reason_ids})
            frozen_issue = c["tracking_issue"]
            if all(
                key in frozen_issue
                for key in ("id", "number", "url", "description", "description_digest")
            ):
                claimed["active_work"]["tracking_issue"] = {
                    key: frozen_issue[key]
                    for key in ("id", "number", "url", "description", "description_digest")
                }
            else:
                # Compatibility for records created before intake-level freezing.
                issue = inputs.tracking_issues.get(c["tracking_issue"]["number"]) or next(
                    (i for i in inputs.issues if i["number"] == c["tracking_issue"]["number"]), None)
                if issue is not None:
                    claimed["active_work"]["tracking_issue"] = collect.tracking_issue_context(
                        issue, c["tracking_issue"]
                    )
            by_id[c["contribution_id"]] = claimed
            res.stage("contributions", claimed)

    if policy["read_only"]:
        rep["note"] = "read-only: requested effects are reported, not executed"
    return res


def _open_operations(record: dict, operations: dict) -> tuple[str, ...]:
    """The open operations of a record: those of the store, and the one the
    record names as active, also when the store does not hold it."""
    ops = {op["operation_id"] for op in operations.values()
           if op["contribution_id"] == record["contribution_id"] and op["status"] in recovery.OPEN}
    active = record.get("active_operation_id")
    if active and (active not in operations or operations[active]["status"] in recovery.OPEN):
        ops.add(active)
    return tuple(sorted(ops))


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


def _command_values(rec: dict, verb: str, cmd: Command, receipt: dict) -> dict:
    """The values of the fields a command's row sets that the engine cannot
    derive: the refresh a refresh or a stale resume records."""
    if verb not in ("refresh_investigation", "resume_stale"):
        return {}
    fork = rec.get("fork") or {}
    return {"refresh": {"command_id": receipt["command_id"], "reason": cmd.args["reason"],
                        "from_phase": rec["phase"], "head_sha": fork.get("head_sha"),
                        "pr_number": fork.get("pr_number")}}


def _apply_table_command(rec: dict, cmd: Command, receipt: dict, operations: dict,
                         policy: dict, now: str) -> engine.Outcome:
    """Apply a command to a record in a table state, as the table defines it."""
    open_ops = _open_operations(rec, operations)
    verb, why = engine.resolve_command(rec, cmd, policy=policy, now=now,
                                       open_operations=open_ops)
    if verb is None:
        return engine.Outcome(rec, False, _refusal(rec, cmd, why), None)
    if cmd.verb in engine.HELD_COMMANDS:
        return engine.Outcome(rec, False, engine.HELD_COMMANDS[cmd.verb], None, verb)
    details = {}
    pending = (rec.get("pending_human_action") or {}).get("kind")
    if verb == "resume_pause" and pending:
        # A resume clears the flag only; the reply names the wait that remains.
        details["pending_human_action"] = pending
    return engine.apply_transition(rec, verb, policy=policy, now=now,
                                   cause_id=receipt["command_id"], command=cmd,
                                   values=_command_values(rec, verb, cmd, receipt),
                                   open_operations=open_ops, actor=receipt["actor"],
                                   details=details)


def apply_command(rec: dict, cmd: Command, receipt: dict, *, operations: dict, policy: dict,
                  now: str, provisioning: dict | None = None,
                  backup_user: dict | None = None) -> engine.Outcome:
    """Apply an authenticated command to the record it concerns: through the
    transition engine where the table defines it (``engine.takes_command``),
    through the compatibility layer otherwise."""
    if engine.takes_command(rec, cmd.verb):
        return _apply_table_command(rec, cmd, receipt, operations, policy, now)
    out = compat.apply_command(rec, cmd, command_id=receipt["command_id"],
                               actor=receipt["actor"], now=now, provisioning=provisioning,
                               backup_user=backup_user)
    return engine.Outcome(out.record, out.accepted, out.reason, out.event)


def _apply(cmd: Command, receipt: dict, by_id: dict, operations: dict, inputs: Inputs,
           res: TickResult, now: str, policy: dict) -> dict:
    actor = receipt["actor"]
    if cmd.verb == "resolve-operation":
        op = operations.get(cmd.args["operation"])
        if op is None:
            return dict(receipt, result="rejected", reason="unknown operation")
        try:
            new = recovery.resolve(op, cmd, actor=actor, command_id=receipt["command_id"],
                                   evidence=inputs.resolution_evidence.get(cmd.args["evidence"]), now=now)
        except recovery.RecoveryError as e:
            return dict(receipt, result="rejected", reason=str(e))
        operations[op["operation_id"]] = new
        res.stage("operations", new)
        return receipt
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
        out = apply_command(rec, cmd, receipt, operations=operations, policy=policy, now=now,
                            provisioning=inputs.provisioning.get(rec["contribution_id"]),
                            backup_user=inputs.users.get(cmd.args.get("backup", "")))
    except lifecycle.TransitionError as e:
        return dict(receipt, result="rejected", reason=str(e))
    if not out.accepted:
        return dict(receipt, result="rejected", reason=out.reason)
    by_id[rec["contribution_id"]] = out.record
    res.stage("contributions", out.record)
    if out.event:
        res.stage("events", out.event)
    return receipt


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
        users, provisioning = {}, {}
        tracking_issues, upstream_snapshots = {}, {}
    else:
        snapshot = reconcile.reconcile_snapshot(root, policy)
        users, provisioning = collect.command_lookups(api, snapshot, comments)
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
            users=users,
            provisioning=provisioning,
        ),
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
