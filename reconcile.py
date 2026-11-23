# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""One trusted reconciliation tick (plan section 9), producing state only.

Inputs, all read-only:
* committed ``.state/``;
* GitHub: intake issues, command comments, verified PRs, fork branch heads,
  live CI on the exact head;
* worker publication outcomes (``porting-publication`` artifacts of
  finished worker runs), which carry the native Result's artifact identity
  and the independent branch operation receipt;
* runner pressure.

Output: the set of state records to commit (by ``state_commit.publish``)
and a report. Remote effects are never performed here: dispatching work is
the dispatcher's job, pushing is the worker publish job's job.

A queue Result alone never advances the fork head or enters ``waiting_ci``;
only the publication receipt plus an observed branch head does.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import canary
import capacity
import collect
import compat
import controller
import engine
import isolation
import lifecycle
import observe_ci
import recovery
import schemas
import state
import work
from common import INTAKE_LABEL, load_policy, repository_root, safe_id


@dataclass
class Snapshot:
    contributions: dict[str, dict]
    operations: dict[str, dict]
    consumed: dict[int, dict]
    projects: dict[str, dict]
    evidence: dict[str, dict]


@dataclass
class TickOutput:
    staged: list[tuple[str, dict]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    # The contributions this tick did not process, with the exception of each.
    failures: list[isolation.ContributionFailure] = field(default_factory=list)


RETRY_BACKOFF_MINUTES = (15, 60, 240)

# A dispatcher launch that has not shown up as a run by then is launched again.
DISPATCH_LAUNCH_GRACE_MINUTES = 15
DISPATCHER_WORKFLOW = ".github/workflows/dispatcher.lock.yml"


def state_text_migration_report(
    written: list[str], *, pending: list[str] | None = None
) -> dict[str, Any]:
    """Return a workflow-safe no-op report for a migration-only tick."""
    return migration_report("state_text_migration", written, pending=pending)


def migration_report(
    key: str, written: list[str], *, pending: list[str] | None = None
) -> dict[str, Any]:
    """A workflow-safe report of a tick that only migrated state: it requests
    no effect, so every launcher of the controller workflow does nothing."""
    migration = {"written": written}
    if pending is not None:
        migration["pending"] = pending
    return {
        key: migration,
        "written": written,
        "controller": {"canary_recovery_requests": []},
        "requested_effects": [],
        "dispatch_relaunch": [],
        "provision_requests": [],
        "proposal_publications": [],
        "publication_retries": [],
        "dev_pr_requests": [],
    }


def dispatch_recovery(contributions: dict[str, dict], *, runs: list[dict] | None,
                      queue_works: dict[str, str] | None, now: datetime, policy: dict,
                      skip: frozenset[str] = frozenset()) -> tuple[list[dict], list[tuple[str, dict]], list[dict]]:
    """Requested tasks whose dispatch never reached the queue (plan 15.3).

    ``runs`` are recent dispatcher workflow runs (GitHub API objects);
    ``queue_works`` maps a Work's ``graph_id`` to its queue state, from the
    pinned gh-aw CLI's validating replay ({} when the queue does not exist
    yet). Either being None means "unknown": nothing is decided this tick.

    For each ``active_work`` (oldest first) whose Work is absent from the
    queue, or admitted but still unclaimed:

    * a dispatcher run created after the request is still queued/running: wait;
    * no dispatcher run after the request, past a short grace: launch again
      (the launch itself was lost);
    * otherwise every such run finished without its Work being claimed. If
      fewer than ``1 + retries.infrastructure_per_head_failure`` runs did,
      launch the dispatcher again (it replays the same committed request, so
      the queue sees the identical Work); else give up and wait for a human.

    Returns (relaunch requests, staged records, report). No model is involved
    in deciding; a relaunch is an ordinary dispatcher run.
    """
    from datetime import timedelta

    import work
    relaunch: list[dict] = []
    staged: list[tuple[str, dict]] = []
    report: list[dict] = []
    if runs is None or queue_works is None:
        return relaunch, staged, report
    limit = 1 + ((policy.get("retries") or {}).get("infrastructure_per_head_failure", 2))
    iso = now.isoformat()
    pending = sorted(((k, c) for k, c in contributions.items() if c.get("active_work")),
                     key=lambda kc: kc[1]["active_work"]["requested_at"])
    for key, c in pending:
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "dispatch_recovery"):
            aw = c["active_work"]
            if c["phase"] == "handoff":
                new = dict(c, active_work=None, updated_at=iso)
                contributions[key] = new
                schemas.validate("contribution", new)
                staged.append(("contributions", new))
                report.append({"contribution": c["contribution_id"], "task": aw["task"],
                               "decision": "cleared obsolete Work at handoff"})
                continue
            state = queue_works.get(work.graph_id(aw["dedupe_key"]))
            if state not in (None, "available", "completed", "cancelled"):
                continue      # a live Claim is resolved by the worker outcome or deadline
            requested = datetime.fromisoformat(aw["requested_at"])
            after = [r for r in runs if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) >= requested]
            entry = {"contribution": c["contribution_id"], "task": aw["task"], "queue_state": state,
                     "dispatcher_runs": [r["id"] for r in after]}
            if state in ("completed", "cancelled"):
                # Work graphs are immutable and terminal. Reusing the same graph
                # can only fail: the table allocates a fresh graph at a new
                # revision within `work_recovery` and `work_total`
                # (`allocate_work_retry`), and waits for a human otherwise
                # (`exhaust_work_recovery`).
                new, event, decision = _terminal_work(c, aw, state, now=now, policy=policy)
                if new is None:
                    report.append(entry | {"decision": decision})
                    continue
                contributions[key] = new
                staged.extend([("contributions", new), ("events", event)])
                if new.get("active_work"):
                    relaunch.append({"type": "relaunch_dispatcher",
                                     "contribution_id": new["contribution_id"]})
                report.append(entry | {"decision": decision})
                continue
            if any(r.get("status") != "completed" for r in after):
                report.append(entry | {"decision": "dispatcher running"})
                continue
            latest = max(after, key=lambda r: r["created_at"], default=None)
            if latest is not None and str(latest["id"]) != str(aw.get("dispatched_run_id")):
                c = dict(c, active_work=dict(aw, dispatched_run_id=str(latest["id"])))
                contributions[key] = c
                staged.append(("contributions", c))
            if not after:
                if now - requested < timedelta(minutes=DISPATCH_LAUNCH_GRACE_MINUTES):
                    report.append(entry | {"decision": "waiting for the dispatcher to start"})
                    continue
                relaunch.append({"type": "relaunch_dispatcher", "contribution_id": c["contribution_id"]})
                report.append(entry | {"decision": "relaunch: no dispatcher run"})
                continue
            # Every finished dispatcher run since the request left the Work unclaimed.
            if len(after) < limit:
                relaunch.append({"type": "relaunch_dispatcher", "contribution_id": c["contribution_id"]})
                report.append(entry | {"decision": f"relaunch: dispatch attempt {len(after) + 1} of {limit}"})
                continue
            new = compat.legacy_update(
                c, "reconcile.dispatch_exhausted", active_work=None, activity="waiting_human",
                updated_at=iso,
                pending_human_action={"kind": "dispatch_failed", "requested_at": iso,
                                      "reminded_at": None, "escalated_at": None})
            contributions[key] = new
            staged.append(("contributions", new))
            report.append(entry | {"decision": "dispatch failed; waiting for a human"})
    return relaunch[: max(1, policy["concurrency"]["coding_workers"])], staged, report


def _terminal_work(c: dict, aw: dict, queue_state: str, *, now: datetime,
                   policy: dict) -> tuple[dict | None, dict | None, str]:
    """The table's row for requested Work whose graph is terminal without a
    usable Result: the record, its event, and the decision for the report.
    A state outside the table has no row: the Work is left to its deadline."""
    iso = now.isoformat()
    if engine.state_key(c) not in lifecycle.LIFECYCLE:
        return None, None, f"terminal Work in {engine.state_key(c)}, a state the table does not have"
    facts = {"work_terminal_without_result"}
    details = {"queue_state": queue_state, "work": aw["dedupe_key"]}
    generation, revision = int(c.get("generation", 0)), c["revision"] + 1
    fresh = dict(aw, dedupe_key=work.work_key(c["contribution_id"], generation, revision, aw["task"]),
                 generation=generation, revision=revision, requested_at=iso,
                 deadline_at=(now + timedelta(hours=controller.WORK_DEADLINE_HOURS)).isoformat(),
                 dispatched_run_id=None)
    out = engine.apply_transition(c, "allocate_work_retry", policy=policy, now=iso,
                                  cause_id=f"queue-{queue_state}", facts=facts,
                                  values={"active_work": fresh}, details=details)
    if out.accepted:
        attempt = out.record["active_work"]["attempt"]
        limit = lifecycle.budget_limit("work_recovery", policy)
        return out.record, out.event, f"new revision for terminal Work, attempt {attempt} of {limit}"
    spent = engine.exhausted(c, ("work_total", "work_recovery"), policy=policy, now=iso)
    out = engine.apply_transition(c, "exhaust_work_recovery", policy=policy, now=iso,
                                  cause_id=f"queue-{queue_state}", facts=facts,
                                  values={lifecycle.PENDING_COUNTER: (spent or ["work_recovery"])[0]},
                                  details=details | {"exhausted": spent})
    if not out.accepted:
        return None, None, f"terminal Work: {out.reason}"
    return out.record, out.event, "automatic Work recovery exhausted; waiting for a human"


def _may_hold_work(rec: dict) -> bool:
    """Whether the record is in a state that can request Work at all: a
    working phase, and not paused."""
    return rec["phase"] in compat.WORK_PHASES and not rec.get("paused")


def result_mismatch(rec: dict, pub: dict) -> str | None:
    """Why a Result does not match what the record requests, or ``None``.

    A Result is folded only when it matches the Work identity: the current
    generation and revision of the record, and the requested Work, which is
    the Work the record holds (its Work id), or the held Result of the same
    worker run (a publish-only retry, or a verified proposal the record
    holds). Everything else takes ``record_late_effect``. Park, abandon, and
    pause clear the Work and the pending publication, so a matching revision
    alone does not prove a match.

    A Result whose payload names no generation and no Work id was requested
    before the Work identity carried them: it is generation 0 and its Work is
    recognized by the revision (``work.task_identity``).
    """
    if rec["phase"] == "handoff":
        return "late Result during handoff"
    if not _may_hold_work(rec):
        return "late Result for a stopped contribution"
    if compat.waits_for_human(rec):
        # A wait for a human has no Result row; only its own rows leave it.
        return "late Result while the contribution waits for a human"
    task = pub.get("task") or {}
    generation, key = work.task_identity(task)
    if generation is None:
        return "late Result: its task names no Work identity"
    current = int(rec.get("generation", 0))
    if generation != current:
        return f"late Result of generation {generation}, current is {current}"
    revision = task.get("revision")
    if revision != rec["revision"]:
        return f"late Result of revision {revision}, current is {rec['revision']}"
    run = str(pub.get("worker_run_id"))
    requested = rec.get("active_work")
    if requested and work.work_generation(requested) == current \
            and requested.get("revision") == revision \
            and (requested.get("dedupe_key") == key if key is not None else True) \
            and task.get("task", requested.get("task")) == requested.get("task") \
            and (pub.get("status") == "investigation") == (requested.get("task") == "investigation"):
        # The Work id names the task; a dossier answers investigation Work
        # only, and investigation Work is answered by a dossier only.
        return None
    if (rec.get("pending_publication") or {}).get("worker_run_id") == run:
        return None
    proposal = rec.get("proposal_artifact") or {}
    if proposal.get("outcome") == "proposal" and str(proposal.get("run_id")) == run:
        return None
    return "late Result without matching requested Work"


def _late_effect(rec: dict, pub: dict, why: str, *, policy: dict, now: str) -> dict:
    """The audit event of ``record_late_effect`` for one Result."""
    task = pub.get("task") or {}
    generation, key = work.task_identity(task)
    return compat.record_late_effect(
        rec, policy=policy, now=now, cause_id=_result_cause(pub, pub.get("worker_run_id")),
        details={"result_status": pub.get("status"), "task_revision": task.get("revision"),
                 "task_generation": generation, "task_work": key, "why": why})


def provably_unadmitted_work(contributions: dict[str, dict], *, runs: list[dict] | None,
                             queue_works: dict[str, str] | None,
                             skip: frozenset[str] = frozenset()) -> set[str] | None:
    """Dedupe keys of requested Work that no worker can be running for.

    Plan 12.3: if queue or run state cannot be read, nothing is inferred.
    Work counts only when its graph is absent from the native queue replay
    and no dispatcher run started since the request is still in flight.
    ``active_work.dispatched_run_id`` is not proof: it is not recorded while a
    Claim is live, and it names finished dispatcher runs that admitted nothing.
    """
    import work
    if runs is None or queue_works is None:
        return None
    keys = set()
    for c in contributions.values():
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "dispatch_recovery"):
            aw = c.get("active_work")
            if not aw or work.graph_id(aw["dedupe_key"]) in queue_works:
                continue
            requested = datetime.fromisoformat(aw["requested_at"])
            if any(r.get("status") != "completed"
                   and datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) >= requested
                   for r in runs):
                continue
            keys.add(aw["dedupe_key"])
    return keys


def _result_cause(outcome: dict, worker_run_id: str | None) -> str | None:
    run = worker_run_id or (outcome.get("artifact") or {}).get("run_id")
    return str(run) if run else None


def _charge_no_progress(record: dict) -> None:
    """Charge ``no_progress`` for a rejected or blocked outcome, as
    ``reject_patch_proposal`` does. Its exhaustion is taken where the next
    Work is due (``refresh_on_exhaustion``, ``exhaust_repair_attempts``)."""
    budget = record["budget"]
    record["budget"] = dict(budget, no_progress_streak=int(budget.get("no_progress_streak", 0)) + 1)


def _restart_no_progress(record: dict) -> None:
    """A published patch starts ``no_progress`` again (``BUDGETS``)."""
    record["budget"] = dict(record["budget"], no_progress_streak=0)


# The investigation Results without a usable dossier: no dossier, or a
# decision that is none of develop, park, and close.
DOSSIER_DECISIONS = ("develop", "park", "close")


def _reject_dossier(rec: dict, outcome: dict, *, now: str, policy: dict,
                    worker_run_id: str | None, evidence: dict | None) -> tuple[dict, list[tuple[str, dict]]] | None:
    """Take ``reject_dossier``, or ``exhaust_investigation_recovery`` once
    ``investigation_attempts`` is exhausted, for an investigation Result
    without a usable dossier. ``None`` when the record is not in the state
    the rows leave (a state outside the table keeps the legacy fold)."""
    if engine.state_key(rec) != "investigation.agent_running":
        return None
    staged: list[tuple[str, dict]] = [("evidence", evidence)] if evidence else []
    facts = {"dossier_rejected", "no_usable_dossier"}
    details = {"result_status": outcome.get("status"),
               "decision": ((outcome.get("dossier") or {}).get("proposal") or {}).get("decision")
               if isinstance(outcome.get("dossier"), dict) else None,
               **({"evidence_id": evidence["evidence_id"]} if evidence else {})}
    cause = _result_cause(outcome, worker_run_id)
    for verb in ("reject_dossier", "exhaust_investigation_recovery"):
        out = engine.apply_transition(rec, verb, policy=policy, now=now, cause_id=cause,
                                      facts=facts, details=details)
        if out.accepted:
            staged.extend([("events", out.event), ("contributions", out.record)])
            return out.record, staged
    raise lifecycle.TransitionError(f"no row takes an investigation Result without a usable "
                                    f"dossier in {engine.state_key(rec)}: {out.reason}")


def apply_publication(rec: dict, outcome: dict, *, now: str, policy: dict | None = None,
                      worker_run_id: str | None = None) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold one publication outcome (from fork_publish / queue_result) into the record.

    * published / uncertain -> waiting_ci (observation confirms the head)
    * proposal              -> waiting_capacity (verified patch, no destination write yet)
    * no_change             -> idle (preserved; no revision bump, no repair)
    * rejected, validation  -> fix_needed (a new agent task at a new revision),
                               charged to the attempt budget; exhaustion parks
                               (plan 3.3.3 development failure case 1)
    * rejected, stale       -> idle (the world moved; the next tick re-plans)
    * infra_error           -> keep the proposal, schedule a publish-only retry
                               of the same worker run with backoff; after
                               ``retries.infrastructure_per_head_failure``
                               attempts, wait for a human (no model call ever)
    """
    from datetime import datetime as _dt
    from datetime import timedelta
    staged: list[tuple[str, dict]] = []
    new = copy.deepcopy(rec)
    new["active_work"] = None          # any outcome ends the requested task
    status = outcome.get("status")
    fclass = outcome.get("failure_class")
    pending = new.get("pending_publication")
    if status == "infra_error":
        attempts = (pending or {}).get("attempts", 0) + 1 if (pending or {}).get("worker_run_id") == worker_run_id \
            else max(outcome.get("attempt", 1), 1)
        limit = ((policy or {}).get("retries") or {}).get("infrastructure_per_head_failure", 2)
        exhausted = attempts > limit
        delay = RETRY_BACKOFF_MINUTES[min(attempts - 1, len(RETRY_BACKOFF_MINUTES) - 1)]
        new["pending_publication"] = {
            "worker_run_id": str(worker_run_id or (pending or {}).get("worker_run_id") or "0"),
            "attempts": attempts, "last_reason": (outcome.get("reason") or "")[:2000],
            "next_attempt_at": (_dt.fromisoformat(now) + timedelta(minutes=delay)).isoformat(),
            "exhausted": exhausted}
        compat.legacy_assign(new, "reconcile.publication_outcome",
                             activity="waiting_human" if exhausted else "waiting_capacity")
        if exhausted:
            new["pending_human_action"] = {"kind": "publication_infrastructure_failure", "requested_at": now,
                                           "reminded_at": None, "escalated_at": None}
        op = outcome.get("operation")
        if op:
            staged.append(("operations", op))
            new["publication_operation_id"] = op["operation_id"]
        new["updated_at"] = now
        staged.append(("contributions", new))
        return new, staged
    new["pending_publication"] = None
    art = outcome.get("artifact")
    if outcome.get("status") not in ("investigation", "worker_blocked") and art \
            and all(k in art for k in ("work_id", "claim_id", "artifact_id", "name", "digest", "run_id")):
        new["proposal_artifact"] = {k: art[k] for k in ("work_id", "claim_id", "artifact_id", "name",
                                                        "digest", "run_id")} | {
            "result_commit": art.get("result_commit"), "outcome": outcome.get("status")}
    op = outcome.get("operation")
    if op:
        staged.append(("operations", op))
        new["publication_operation_id"] = op["operation_id"]
    status = outcome.get("status")
    if status in ("published", "uncertain") and op:
        changed_files = (op.get("proposed") or {}).get("changed_files") or []
        if changed_files:
            new["scope_paths"] = list(dict.fromkeys((new.get("scope_paths") or []) + changed_files))
    if status == "published":
        # The branch head itself is recorded by the next observation (head
        # change bumps revision); here we only leave agent_running.
        compat.legacy_assign(new, "reconcile.publication_outcome", activity="waiting_ci")
        _restart_no_progress(new)
    elif status == "uncertain":
        compat.legacy_assign(new, "reconcile.publication_outcome", activity="waiting_ci")   # discovery happens in observe_ci
        _restart_no_progress(new)
    elif status == "proposal":
        compat.legacy_assign(new, "reconcile.publication_outcome", activity="waiting_capacity")
        new["pending_human_action"] = None
    elif status == "worker_blocked":
        proposal = outcome.get("proposal") or {}
        run = str(worker_run_id or (outcome.get("artifact") or {}).get("run_id") or "0")
        evidence_id = safe_id(
            f"rehearsal-{new['contribution_id'].replace('/', '--')}-r{new['revision']}-{run}"
        )
        evidence = {
            "schema_version": 1,
            "evidence_id": evidence_id,
            "contribution_id": new["contribution_id"],
            "kind": "rehearsal",
            "policy_version": (policy or {}).get("policy_version", "unknown"),
            "collected_at": now,
            "summary": str(outcome.get("reason") or "worker reproduction infrastructure blocked")[:8000],
            "limitations": list(proposal.get("validation_not_performed") or [])[:16],
            "data": {
                "worker_run_id": run,
                "failure_class": outcome.get("failure_class"),
                "failure_cluster": proposal.get("failure_cluster"),
                "reproduction": proposal.get("reproduction"),
            },
            "digests": {"artifact": (outcome.get("artifact") or {}).get("digest") or ""},
        }
        schemas.validate("evidence", evidence)
        staged.append(("evidence", evidence))
        new["proposal_artifact"] = None
        new["pending_publication"] = None
        compat.legacy_assign(new, "reconcile.publication_outcome", revision=new["revision"] + 1)
        pending = list(new.get("pending_feedback_evidence_ids") or [])
        if evidence_id not in pending:
            pending.append(evidence_id)
        new["pending_feedback_evidence_ids"] = pending[-64:]
        _charge_no_progress(new)
        compat.legacy_assign(new, "reconcile.publication_outcome", activity="fix_needed")
        new["pending_human_action"] = None
        staged.append(("events", lifecycle._event(
            rec, new, "worker_reproduction_blocked", "result", run, now, True)))
    elif status == "no_change":
        # Plan 8.6: a valid exact-task no_change Result is preserved. It ends
        # the requested Work without a revision bump or new repair Work; the
        # unchanged exact head is then judged by ordinary CI observation.
        compat.legacy_assign(new, "reconcile.publication_outcome", activity="idle")
        new["pending_human_action"] = None
        staged.append(("events", lifecycle._event(
            rec, new, "worker_no_change", "result", _result_cause(outcome, worker_run_id), now, False
        )))
    elif status == "rejected":
        compat.legacy_assign(new, "reconcile.publication_outcome",
                             activity="idle" if fclass == "stale" else "fix_needed")
        if fclass != "stale":
            # A failed immutable Work graph cannot be submitted again. A new
            # repair attempt gets a new revision and therefore a new graph ID.
            # The rejected outcome charges `no_progress`; the next Work charges
            # `work_total` (and the cluster of a failing CI job) when allocated.
            compat.legacy_assign(new, "reconcile.publication_outcome", revision=new["revision"] + 1)
            new["clean_head_sha"] = None
            _charge_no_progress(new)
        staged.append(("events", lifecycle._event(
            rec, new, "worker_proposal_rejected",
            "result", _result_cause(outcome, worker_run_id), now, fclass != "stale",
        )))
    elif status == "investigation":
        # Investigation artifacts are evidence, never patch proposals. Keeping
        # one in proposal_artifact would make base-branch CI look like patch CI
        # and suppress the first development task.
        new["proposal_artifact"] = None
        # The dossier is recorded as evidence. Its validated decision either
        # enters automatic provisioning or parks without launching a writer.
        dossier = outcome.get("dossier")
        eid = None
        if isinstance(dossier, dict):
            eid = safe_id(f"dossier-{new['contribution_id'].replace('/', '--')}-r{new['revision']}")
            ev = {"schema_version": 1, "evidence_id": eid, "contribution_id": new["contribution_id"],
                  "kind": "dossier", "policy_version": (policy or {}).get("policy_version", "unknown"),
                  "collected_at": now,
                  "summary": str((outcome.get("proposal") or {}).get("diagnosis") or "investigation dossier")[:8000],
                  "limitations": [], "data": dossier,
                  "digests": {"artifact": (outcome.get("artifact") or {}).get("digest") or ""}}
            staged.append(("evidence", ev))
        new["dossier_evidence_id"] = eid
        refresh = new.get("refresh")
        # The refreshed dossier returns to development on the recorded fork
        # (`readmit_development`) only when the refresh began with a fork,
        # and its development PR, that the record still holds; otherwise
        # development is admitted afresh (`admit_development`).
        fork = new.get("fork") or {}
        readmit = bool(refresh and refresh.get("head_sha") and fork.get("repository_id")
                       and fork.get("pr_number") == refresh.get("pr_number"))
        decision = (dossier.get("proposal") or {}).get("decision") if isinstance(dossier, dict) else None
        if not eid or decision not in DOSSIER_DECISIONS:
            rejected = _reject_dossier(rec, outcome, now=now, policy=policy or {},
                                       worker_run_id=worker_run_id,
                                       evidence=staged[-1][1] if eid else None)
            if rejected is not None:
                return rejected
        if not eid:
            compat.legacy_assign(new, "reconcile.publication_outcome", activity="waiting_human")
            new["pending_human_action"] = {"kind": "investigation_without_dossier", "requested_at": now,
                                           "reminded_at": None, "escalated_at": None}
        elif decision not in ("develop", "park"):
            compat.legacy_assign(new, "reconcile.publication_outcome", phase="investigation", activity="idle",
                                 revision=new["revision"] + 1)
            new["pending_human_action"] = None
            staged.append(("events", lifecycle._event(rec, new, "dossier_contract_stale", "observation",
                                                       str(worker_run_id or eid), now, True)))
        elif decision == "park":
            new["previous_phase"] = (refresh or {}).get("from_phase") or new["phase"]
            compat.legacy_assign(new, "reconcile.publication_outcome", phase="parked", activity="idle",
                                 revision=new["revision"] + 1)
            new["parked_reason"] = dossier["proposal"]["rationale"][:2000]
            new["pending_human_action"] = None
            new["refresh"] = None
            staged.append(("events", lifecycle._event(rec, new, "dossier_parked", "observation",
                                                       str(worker_run_id or eid), now, True)))
        else:
            compat.legacy_assign(
                new, "reconcile.publication_outcome",
                activity="fix_needed" if readmit else "idle",
                phase=("development"
                       if readmit or not (policy or {}).get("destination_writes", False)
                       else "provisioning"),
                revision=new["revision"] + 1)
            new["admission_class"] = dossier["proposal"]["admission_class"]
            new["scope_paths"] = list(dossier["proposal"]["allowed_paths"])
            new["scope_coverage_complete"] = True
            new["pending_human_action"] = None
            new["refresh"] = None
            kind = "dossier_refreshed" if readmit else "dossier_develop"
            staged.append(("events", lifecycle._event(rec, new, kind, "observation",
                                                       str(worker_run_id or eid), now, True)))
    new["updated_at"] = now
    staged.append(("contributions", new))
    return new, staged


# The lists of a tick report that name what the tick asks the workflow to launch.
_EFFECT_LISTS = ("requested_effects", "dispatch_relaunch", "provision_requests",
                 "dev_pr_requests", "proposal_publications", "publication_retries")


def _touched(out: TickOutput, contribution_id: str) -> list[str]:
    """What ``out`` stages or requests for one contribution."""
    found = [f"staged {kind}" for kind, record in out.staged
             if record.get("contribution_id") == contribution_id]
    requests = [(name, item) for name in _EFFECT_LISTS for item in out.report.get(name) or []]
    requests += [("canary_recovery_requests", item) for item in
                 (out.report.get("controller") or {}).get("canary_recovery_requests") or []]
    found += [name for name, item in requests if item.get("contribution_id") == contribution_id]
    return found


def tick(api: collect.Api, snap: Snapshot, *, policy: dict, now: datetime,
         run_id: str | None = None, **inputs: Any) -> TickOutput:
    """One reconciliation tick, with each contribution isolated from the others.

    ``inputs`` are the collected inputs of ``_tick``. Staging is all or
    nothing per contribution. When the processing of one contribution fails,
    the whole tick is computed again from the committed state without that
    contribution: nothing is staged for it and no effect is requested for
    it, and it still counts as an existing contribution for the others.

    * A collector failure (``isolation.CollectorFailure``) skips the
      contribution for the tick: no state change, no pause, an entry in the
      report under ``skipped_contributions``.
    * An unexpected error (``isolation.ProcessingError``) in one contribution
      is followed by ``pause_on_processing_error`` on its committed record;
      the record and the event are the only things staged for it. The error
      is listed under ``alerts`` and ``processing_errors``. A contribution
      that is already stopped, or terminal, is not paused again and gets no
      event.
    * Unexpected errors in two or more contributions, a pause that cannot be
      applied, and every exception outside a contribution stop the tick:
      ``isolation.TickFailure`` or the original exception is raised and
      nothing is returned to commit.
    """
    iso = now.isoformat()
    failures: dict[str, isolation.ContributionFailure] = {}
    while True:
        # Each attempt works on its own copy of the committed state, so a
        # failed attempt leaves nothing behind.
        work = copy.deepcopy(snap)
        try:
            out = _tick(api, work, policy=policy, now=now, skip=frozenset(failures), **inputs)
            break
        except isolation.ContributionFailure as failure:
            if failure.contribution_id in failures:
                raise isolation.TickFailure(
                    f"{failure.contribution_id} was processed although it is skipped: {failure}"
                ) from failure
            failures[failure.contribution_id] = failure
            errors = [f for f in failures.values() if isinstance(f, isolation.ProcessingError)]
            if len(errors) > 1:
                raise isolation.TickFailure(
                    "processing failed for more than one contribution in one tick; the tick "
                    "stops, nothing is committed, and no contribution is paused: "
                    + "; ".join(f"{d['contribution']} ({d['step']}: {d['error_class']}: "
                                f"{d['message']})"
                                for d in (isolation.describe(f, run_id) for f in errors)),
                    tuple(errors)) from failure
    # The caller's snapshot follows the attempt that succeeded.
    for name in ("operations", "consumed", "projects"):
        target, source = getattr(snap, name), getattr(work, name)
        target.clear()
        target.update(source)

    out.failures = list(failures.values())
    # Both lists are in the report only when the tick did not process a
    # contribution.
    skipped: list[dict] = []
    errored: list[dict] = []
    alerts = out.report["controller"]["alerts"]
    committed = {c["contribution_id"]: c for c in snap.contributions.values()}
    for cid, failure in failures.items():
        touched = _touched(out, cid)
        if touched:
            raise isolation.TickFailure(
                f"{cid} failed in this tick but the tick still holds {', '.join(touched)} for it")
        description = isolation.describe(failure, run_id)
        if isinstance(failure, isolation.CollectorFailure):
            skipped.append(
                description | {"decision": "skipped for this tick: no state change, no pause"})
            continue
        record = committed.get(cid)
        outcome = None
        if record is not None:
            try:
                outcome = pause_on_processing_error(record, description, policy=policy, now=iso)
            except Exception as error:
                raise isolation.TickFailure(
                    f"pause_on_processing_error could not be applied to {cid} after "
                    f"{description['error_class']} in {description['step']}: "
                    f"{isolation.clean_text(error)}") from error
        if outcome is not None and outcome.accepted:
            out.staged.extend([("contributions", outcome.record), ("events", outcome.event)])
            decision = "paused by the controller"
        elif outcome is not None:
            decision = f"not paused again: {outcome.reason}"
        else:
            decision = "not paused: no committed record"
        errored.append(description | {"decision": decision})
        alerts.append({"contribution": cid,
                       "alert": (f"processing error in {description['step']}: "
                                 f"{description['error_class']}: {description['message']}; "
                                 f"everything staged for it was discarded; {decision}")})
    if skipped:
        out.report["skipped_contributions"] = skipped
    if errored:
        out.report["processing_errors"] = errored
    return out


PROCESSING_ERROR_VERB = "pause_on_processing_error"


def pause_on_processing_error(record: dict, description: dict, *, policy: dict,
                              now: str) -> engine.Outcome:
    """Take the table's ``pause_on_processing_error`` on the committed record.

    It sets ``paused`` with the error as ``paused_reason``, ``paused_by``
    ``controller`` and ``paused_at``, and clears the requested Work, the
    pending publication, and a held proposal; from an ``agent_running`` state
    it leads to the state in which the Work is due. The engine takes it for a
    record in a state of the table, ``compat.legacy_pause`` for one in a
    legacy state. The outcome is not accepted, and nothing changes, when the
    contribution is already paused or terminal.
    """
    reason = isolation.pause_reason(description)
    details = {k: description[k] for k in ("step", "error_class", "message", "run_id")}
    if compat.is_legacy_state(record):
        out = compat.legacy_pause(record, PROCESSING_ERROR_VERB, reason=reason, now=now,
                                  cause_id=description["run_id"], details=details)
        return engine.Outcome(out.record, out.accepted, out.reason, out.event,
                              PROCESSING_ERROR_VERB)
    return engine.apply_transition(record, PROCESSING_ERROR_VERB, policy=policy, now=now,
                                   cause_id=description["run_id"], facts={"processing_error"},
                                   values={"paused_reason": reason}, details=details)


def _tick(api: collect.Api, snap: Snapshot, *, policy: dict, now: datetime,
         publications: list[dict], pressure: capacity.Pressure,
         issues: list[dict] | None = None, comments: list[dict] | None = None,
         verified_prs: dict[str, dict] | None = None, acceptances: dict[int, int] | None = None,
         observe: bool = True, dispatcher_runs: list[dict] | None = None,
         queue_works: dict[str, str] | None = None, tracking_issues: dict | None = None,
         upstream_snapshots: dict | None = None, provisions: list[dict] | None = None,
         development_prs: list[dict] | None = None,
         skip: frozenset[str] = frozenset()) -> TickOutput:
    """One attempt at the tick. ``skip`` names the contributions it does not
    process. Every step that works on one contribution runs in an
    ``isolation.contribution`` block; an exception that leaves such a block
    reaches ``tick`` attributed to that contribution."""
    out = TickOutput()
    iso = now.isoformat()
    contributions = dict(snap.contributions)
    by_cid = {c["contribution_id"]: k for k, c in contributions.items()}

    # Trusted same-fork development PR receipts are independently read back.
    dev_pr_report = []
    for receipt in development_prs or []:
        cid = receipt.get("contribution_id")
        if cid not in by_cid:
            continue
        if cid in skip:
            continue
        with isolation.contribution(cid, "development_pr_receipt"):
            key = by_cid[cid]
            c = contributions[key]
            op = snap.operations.get(receipt.get("operation_id"))
            if not op or op["kind"] not in ("dev_pr_create", "dev_pr_update") or op["status"] not in recovery.OPEN \
                    or c["phase"] != "development" or c.get("active_operation_id") != op["operation_id"] \
                    or c["revision"] != receipt.get("revision") or receipt.get("status") != "published":
                continue
            if receipt.get("repository") != op["target"]["repository"] \
                    or receipt.get("repository_id") != op["target"]["repository_id"] \
                    or receipt.get("head_sha") != op["target"]["expected_head"]:
                dev_pr_report.append({"contribution": cid, "status": "receipt mismatch"})
                continue
            try:
                pr = api.get(f"/repos/{receipt['repository']}/pulls/{int(receipt['pr_number'])}")
            except (ValueError, KeyError):
                dev_pr_report.append({"contribution": cid, "status": "readback unavailable"})
                continue
            marker = op["proposed"].get("marker")
            body_ok = (marker in (pr.get("body") or "") if op["kind"] == "dev_pr_create"
                       else "<!-- upstreaming-development:" in (pr.get("body") or ""))
            if pr.get("id") != receipt.get("pr_id") or pr.get("state") != "open" \
                    or not body_ok \
                    or (pr.get("head") or {}).get("sha") != op["target"]["expected_head"] \
                    or (pr.get("head") or {}).get("ref") != "riscv64" \
                    or (pr.get("head") or {}).get("repo", {}).get("id") != op["target"]["repository_id"] \
                    or (pr.get("base") or {}).get("ref") != c["upstream"]["base_ref"]:
                dev_pr_report.append({"contribution": cid, "status": "PR readback mismatch"})
                continue
            new_op = recovery.fold_receipt(
                op, external_ids={"pr_id": int(pr["id"]), "pr_number": int(pr["number"]),
                                  "development_pr_run_id": str(receipt["development_pr_run_id"])},
                detail=f"development PR {pr['number']} verified", now=iso)
            fork = dict(c["fork"], pr_number=int(pr["number"]))
            new = compat.legacy_update(c, "reconcile.confirm_development_pr", fork=fork,
                                       active_operation_id=None, activity="waiting_ci",
                                       updated_at=iso)
            schemas.validate("contribution", new)
            contributions[key] = new
            snap.operations[op["operation_id"]] = new_op
            out.staged.extend([("operations", new_op), ("contributions", new),
                               ("events", lifecycle._event(c, new, "development_pr_created", "observation",
                                                           op["operation_id"], iso, False))])
            dev_pr_report.append({"contribution": cid, "status": "confirmed", "pr_number": pr["number"]})
    out.report["development_prs"] = dev_pr_report

    # Verified administrative provisioning receipts are separate from model
    # artifact Results. Re-read the actual fork/ref before advancing state.
    provision_report = []
    for receipt in provisions or []:
        cid = receipt.get("contribution_id")
        if cid not in by_cid:
            continue
        key = by_cid[cid]
        if cid in skip:
            continue
        with isolation.contribution(cid, "provisioning_receipt"):
            c = contributions[key]
            op = snap.operations.get(receipt.get("operation_id"))
            if not op or op["kind"] != "fork_provision" or op["status"] not in recovery.OPEN \
                    or c["phase"] != "provisioning" or c.get("active_operation_id") != op["operation_id"] \
                    or c["revision"] != receipt.get("revision") or receipt.get("status") != "provisioned":
                continue
            if receipt.get("fork") != op["target"]["repository"] \
                    or receipt.get("upstream_id") != c["upstream"]["repository_id"] \
                    or receipt.get("base_sha") != c["upstream"]["base_sha"] \
                    or receipt.get("actions_read_only") is not True:
                provision_report.append({"contribution": cid, "status": "receipt mismatch"})
                continue
            repo = collect.repository(api, receipt["fork"])
            head = collect.branch_head(api, receipt["fork"], "riscv64")
            expected = op["target"].get("expected_head") or op["target"]["base_sha"]
            if not head or head["head"] != receipt.get("head_sha") or head["head"] != expected \
                    or repo["id"] != receipt.get("fork_id") or repo.get("parent_id") != c["upstream"]["repository_id"]:
                provision_report.append({"contribution": cid, "status": "fork identity/ref mismatch"})
                continue
            external_ids = {}
            if receipt.get("provision_run_id"):
                external_ids["provision_run_id"] = str(receipt["provision_run_id"])
            if receipt.get("provision_artifact_id"):
                external_ids["provision_artifact_id"] = int(receipt["provision_artifact_id"])
            new_op = recovery.fold_receipt(
                op, external_ids=external_ids,
                detail="fork/source identity, branch and trusted setup receipt verified", now=iso)
            fork = {"repository_id": repo["id"], "name": repo["full_name"], "head_ref": "riscv64",
                    "head_sha": head["head"], "pr_number": None}
            if recovery.fired_verbs(c, new_op):
                # The receipt matches the operation and the fork and ref were
                # read back: the operation fires `confirm_provisioning`.
                fired = recovery.fire(
                    c, new_op, policy=policy, now=iso,
                    facts={"receipt_matches_operation", "fork_and_ref_read_back"},
                    values={"fork": fork,
                            "upstream.repository_id": c["upstream"]["repository_id"],
                            "upstream.base_ref": c["upstream"]["base_ref"],
                            "upstream.base_sha": c["upstream"]["base_sha"]})
                if not fired.accepted:
                    raise lifecycle.TransitionError(
                        f"confirm_provisioning refused in {engine.state_key(c)}: {fired.reason}")
                new, event = fired.record, fired.event
            elif compat.waits_for_human(c):
                # A wait of the table has no row for the operation: the state
                # is kept and the closed operation is released.
                new, event = recovery.release(c, new_op, iso), None
            else:
                has_pre_fork_proposal = (c.get("proposal_artifact") or {}).get("outcome") == "proposal"
                new = compat.legacy_update(
                    c, "reconcile.confirm_provisioning",
                    fork=fork,
                    phase="development",
                    activity="waiting_capacity" if has_pre_fork_proposal else "idle",
                    active_operation_id=None,
                    pending_human_action=None,
                    revision=c["revision"] if has_pre_fork_proposal else c["revision"] + 1,
                    updated_at=iso,
                )
                event = lifecycle._event(c, new, "fork_provisioned", "observation", op["operation_id"],
                                         iso, not has_pre_fork_proposal)
            schemas.validate("contribution", new)
            contributions[key] = new
            snap.operations[op["operation_id"]] = new_op
            out.staged.extend([("operations", new_op), ("contributions", new)])
            if event is not None:
                out.staged.append(("events", event))
            provision_report.append({"contribution": cid, "status": "provisioned"})
    out.report["provisions"] = provision_report

    # 1. Publication outcomes from finished workers. A development reset or an
    # infrastructure retry can leave multiple artifacts for the same immutable
    # contribution revision. Fold only the newest artifact; an older one no
    # longer matches anything once the newest is folded, so it takes
    # `record_late_effect` like every other Result that matches nothing.
    latest: dict[tuple[str, int], tuple[tuple[int, int], dict]] = {}
    superseded = []
    for pub in publications:
        task = pub.get("task") or {}
        cid, revision = task.get("contribution_id"), task.get("revision")
        if not isinstance(cid, str) or not isinstance(revision, int):
            continue
        def numeric(value):
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0
        rank = (numeric(pub.get("publication_artifact_id")), numeric(pub.get("worker_run_id")))
        key = (cid, revision)
        prior = latest.get(key)
        if prior is None or rank > prior[0]:
            if prior is not None:
                superseded.append(prior[1])
            latest[key] = (rank, pub)
        else:
            superseded.append(pub)
    publications = [item[1] for item in latest.values()] + [
        dict(pub, superseded=True) for pub in superseded]
    pubs_report = []
    for pub in publications:
        cid = (pub.get("task") or {}).get("contribution_id")
        if cid not in by_cid:
            pubs_report.append({"ignored": "unknown contribution"})
            continue
        if cid in skip:
            continue
        with isolation.contribution(cid, "publication"):
            key = by_cid[cid]
            rec = contributions[key]
            pid = pub.get("publication_artifact_id")
            if bool(pid) and pid in (rec.get("processed_publications") or []):
                # Already folded in by an earlier tick, or already recorded as a
                # late effect: the audit event is written once per artifact.
                continue
            if pub.get("superseded") and not pid:
                # Without an artifact id there is nothing to record it once by.
                pubs_report.append({"contribution": cid, "ignored": "superseded publication artifact"})
                continue
            if pid:
                rec = dict(rec, processed_publications=((rec.get("processed_publications") or []) + [pid])[-64:])
                contributions[key] = rec
            unrequested = ("superseded publication artifact" if pub.get("superseded")
                           else result_mismatch(rec, pub))
            if unrequested:
                # The table's `record_late_effect`: a Result that does not match
                # the current generation, revision, and requested Work is recorded
                # for audit and never folded, in every state. A branch effect it
                # carried is seen by head observation.
                out.staged.extend([("contributions", rec), ("events", _late_effect(
                    rec, pub, unrequested, policy=policy, now=iso))])
                pubs_report.append({"contribution": cid, "ignored": unrequested})
                continue
            rec, staged = apply_publication(rec, pub, now=iso, policy=policy, worker_run_id=pub.get("worker_run_id"))
            contributions[key] = rec
            out.staged.extend(staged)
            for kind, r in staged:
                if kind == "operations":
                    snap.operations[r["operation_id"]] = r
            pubs_report.append({"contribution": cid, "status": pub.get("status"), "reason": pub.get("reason")})
    out.report["publications"] = pubs_report

    # 1b. Requested tasks that never produced an outcome: give up, ask a human.
    timed_out = []
    for key, c in list(contributions.items()):
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "work_timeout"):
            aw = c.get("active_work")
            tracking = c.get("tracking_issue") or {}
            if (canary.is_target(c)
                    and tracking.get("description_digest") == canary.INCOMPLETE_DIGEST):
                # The exact pre-dispatch canary defect is owned by its durable
                # recovery operation, never by generic timeout/hand-unblock logic.
                continue
            if aw and c["phase"] != "handoff" and datetime.fromisoformat(aw["deadline_at"]) <= now:
                new = compat.legacy_update(
                    c, "reconcile.work_timeout", active_work=None, activity="waiting_human",
                    updated_at=iso,
                    pending_human_action={"kind": "task_without_outcome", "requested_at": iso,
                                          "reminded_at": None, "escalated_at": None})
                contributions[key] = new
                out.staged.append(("contributions", new))
                timed_out.append({"contribution": c["contribution_id"], "task": aw["task"],
                                  "requested_at": aw["requested_at"]})
    out.report["timed_out_work"] = timed_out

    # 1c. Requested tasks whose dispatch never reached the queue: relaunch the
    # dispatcher (bounded), then wait for a human.
    relaunch, staged, rep = dispatch_recovery(contributions, runs=dispatcher_runs, queue_works=queue_works,
                                              now=now, policy=policy, skip=skip)
    out.staged.extend(staged)
    out.report["dispatch_recovery"] = rep
    out.report["dispatch_relaunch"] = relaunch

    # 1d. Carry durable review constraints across head changes. Older records
    # predate pending_feedback_evidence_ids, so recover their already-ingested
    # human relay evidence before selecting another repair.
    for key, c in list(contributions.items()):
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "feedback_recovery"):
            feedback_ids = sorted(
                e["evidence_id"] for e in snap.evidence.values()
                if e.get("kind") == "human_relay" and e.get("contribution_id") == c["contribution_id"])
            if feedback_ids and "pending_feedback_evidence_ids" not in c \
                    and c["phase"] in {"development", "handoff", "upstream_review"}:
                c = dict(c, pending_feedback_evidence_ids=feedback_ids[-64:])
                contributions[key] = c
                out.staged.append(("contributions", c))

    # 2. Live branch and CI observation.
    if observe:
        contracts = {e["evidence_id"]: e for e in snap.evidence.values() if e.get("kind") == "target_contract"}
        obs = observe_ci.observe(api, contributions=contributions, operations=snap.operations,
                                 contracts=contracts, policy=policy, now=iso,
                                 evidence=snap.evidence,
                                 unadmitted_work=provably_unadmitted_work(
                                     contributions, runs=dispatcher_runs, queue_works=queue_works,
                                     skip=skip),
                                 skip=skip)
        for kind, r in obs.staged:
            if kind == "contributions":
                contributions[by_cid[r["contribution_id"]]] = r
        out.staged.extend(obs.staged)
        out.report["observation"] = obs.report
        # Recovery is evaluated before live CI. If CI accepts the current head
        # in this same tick, do not launch a dispatcher for the now-obsolete
        # retry request.
        out.report["dispatch_relaunch"] = [
            item for item in out.report["dispatch_relaunch"]
            if (contributions[by_cid[item["contribution_id"]]].get("active_work") is not None
                and contributions[by_cid[item["contribution_id"]]]["phase"] != "handoff")]

    # 3. Commands, intake and work selection (controller). The failing CI job
    # of a head is decided from the evidence the record names, including the
    # evidence this tick recorded.
    evidence = dict(snap.evidence)
    evidence.update({r["evidence_id"]: r for kind, r in out.staged if kind == "evidence"})
    res = controller.tick(contributions=contributions, operations=snap.operations, consumed=snap.consumed,
                          projects=snap.projects, policy=policy,
                          inputs=controller.Inputs(now=now, issues=issues or [], comments=comments or [],
                                                   pressure=pressure, verified_prs=verified_prs or {},
                                                   tracking_issues=tracking_issues or {},
                                                   upstream_snapshots=upstream_snapshots or {},
                                                   acceptances=acceptances or {},
                                                   resolution_evidence={
                                                       e["evidence_id"]: e for e in evidence.values()
                                                       if e.get("kind") == "resolution"}),
                          skip=skip, evidence=evidence)
    out.staged.extend(res.staged)
    out.report["controller"] = res.report
    out.report["requested_effects"] = res.requested_effects
    out.report["provision_requests"] = res.report["provision_requests"]
    out.report["dev_pr_requests"] = res.report["dev_pr_requests"]
    out.report["proposal_publications"] = res.report["proposal_publications"]
    # 4. Publish-only retries due now (infrastructure failures; no model call).
    retries = []
    max_launches = 1 + policy["retries"]["infrastructure_per_head_failure"]
    for key, c in list(contributions.items()):
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "publication_retry"):
            pp = c.get("pending_publication")
            if pp and not pp.get("exhausted") and not compat.stopped(c) \
                    and c["phase"] in (lifecycle.WRITE_PHASES - {"handoff"}) | {"investigation"} \
                    and datetime.fromisoformat(pp["next_attempt_at"]) <= now:
                if pp["attempts"] >= max_launches:
                    pending = dict(pp, exhausted=True)
                    changed = compat.legacy_update(
                        c, "reconcile.publish_retry_exhausted",
                        pending_publication=pending,
                        activity="waiting_human",
                        pending_human_action={
                            "kind": "publication_infrastructure_failure",
                            "requested_at": now.isoformat(),
                            "reminded_at": None,
                            "escalated_at": None,
                        },
                        updated_at=now.isoformat(),
                    )
                    contributions[key] = changed
                    out.staged.append(("contributions", changed))
                    continue
                attempt = pp["attempts"] + 1
                delay = RETRY_BACKOFF_MINUTES[min(attempt - 1, len(RETRY_BACKOFF_MINUTES) - 1)]
                pending = dict(
                    pp,
                    attempts=attempt,
                    next_attempt_at=(now + timedelta(minutes=delay)).isoformat(),
                )
                changed = dict(c, pending_publication=pending, updated_at=now.isoformat())
                contributions[key] = changed
                out.staged.append(("contributions", changed))
                retries.append({"type": "retry_publication", "contribution_id": c["contribution_id"],
                                "worker_run_id": pp["worker_run_id"], "attempt": attempt})
    out.report["publication_retries"] = retries
    out.staged = observe_ci.dedupe(out.staged)
    return out


def mark_dispatched(rec: dict, *, now: str) -> dict:
    """The dispatcher staged Work for this contribution: an agent is running."""
    out = compat.observe_status(rec, activity="agent_running", now=now)
    return out.record


def load_snapshot(root: Path, policy: dict):
    """Load validated lifecycle state relative to an explicit repository root."""
    import state

    store = state.StateStore(root, policy)
    return store, Snapshot(
        contributions=dict(store.load_all("contributions")),
        operations={
            value["operation_id"]: value for value in store.load_all("operations").values()
        },
        consumed={
            value["comment_id"]: value for value in store.load_all("commands").values()
        },
        projects={value["project_id"]: value for value in store.load_all("projects").values()},
        evidence={
            value["evidence_id"]: value for value in store.load_all("evidence").values()
        },
    )


def reconcile_snapshot(root: Path, policy: dict) -> Snapshot:
    return load_snapshot(root, policy)[1]


def _publication_outcomes(directory: str | None) -> list[dict]:
    publications = []
    for path in sorted(Path(directory).glob("*/outcome.json")) if directory else []:
        outcome = json.loads(path.read_text())
        task = path.parent / "task.json"
        outcome["task"] = json.loads(task.read_text()) if task.exists() else {}
        worker_run = path.parent / "worker-run-id"
        outcome["worker_run_id"] = (
            worker_run.read_text().strip() if worker_run.exists() else path.parent.name
        )
        artifact_id = path.parent / "artifact-id"
        outcome["publication_artifact_id"] = (
            int(artifact_id.read_text().strip()) if artifact_id.exists() else None
        )
        for name, key in (
            ("dossier.json", "dossier"),
            ("proposal.json", "proposal"),
            ("result.json", "artifact"),
        ):
            candidate = path.parent / name
            if candidate.exists() and key not in outcome:
                try:
                    outcome[key] = json.loads(candidate.read_text())
                except json.JSONDecodeError:
                    pass
        publications.append(outcome)
    return publications


def _receipt_outcomes(directory: str | None, *, kind: str) -> list[dict]:
    receipts = []
    for path in sorted(Path(directory).glob("*/outcome.json")) if directory else []:
        receipt = json.loads(path.read_text())
        artifact_id = path.parent / "artifact-id"
        if kind == "development_pr":
            receipt["development_pr_artifact_id"] = (
                int(artifact_id.read_text()) if artifact_id.exists() else None
            )
        else:
            receipt["provision_run_id"] = path.parent.name
            receipt["provision_artifact_id"] = (
                int(artifact_id.read_text()) if artifact_id.exists() else None
            )
        receipts.append(receipt)
    return receipts


def cmd_reconcile(args: argparse.Namespace) -> int:
    import queue_result

    policy = load_policy()
    root = Path(args.root) if args.root else repository_root()
    now = datetime.now(timezone.utc)
    try:
        migrated = state.apply_pending_text_migration(root, policy, now=now.isoformat())
    except state.StateError as error:
        print(f"legacy state text migration refused: {error}", file=sys.stderr)
        return 1
    if migrated:
        # Keep the reviewed legacy repair in its own state-only commit. In
        # particular, do not disturb active Work while its worker is running.
        import state_commit
        try:
            changed = state_commit.records_from_checkout(root, "HEAD")
        except state_commit.PublishError as error:
            print(f"legacy state text migration found unrelated mutation: {error}", file=sys.stderr)
            return 1
        if set(changed) != set(migrated):
            unexpected = sorted(set(changed) ^ set(migrated))
            print(
                f"legacy state text migration found unrelated state paths: {unexpected}",
                file=sys.stderr,
            )
            return 1
        report = state_text_migration_report(migrated)
        text = json.dumps(report, indent=2)
        if args.report:
            Path(args.report).write_text(text)
        else:
            print(text)
        return 0
    # A record the lifecycle table does not describe is migrated in its own
    # state-only commit, before anything reads it as ordinary state.
    try:
        migrated = state.apply_pending_record_migrations(root, policy, now=now.isoformat())
    except state.StateError as error:
        print(f"record migration refused: {error}", file=sys.stderr)
        return 1
    if migrated:
        import state_commit
        try:
            changed = state_commit.records_from_checkout(root, "HEAD")
        except state_commit.PublishError as error:
            print(f"record migration found unrelated mutation: {error}", file=sys.stderr)
            return 1
        if set(changed) != set(migrated):
            unexpected = sorted(set(changed) ^ set(migrated))
            print(f"record migration found unrelated state paths: {unexpected}", file=sys.stderr)
            return 1
        text = json.dumps(migration_report("record_migration", migrated), indent=2)
        if args.report:
            Path(args.report).write_text(text)
        else:
            print(text)
        return 0
    store, snapshot = load_snapshot(root, policy)
    api = collect.Api()
    publications = _publication_outcomes(args.publications)
    development_prs = _receipt_outcomes(args.development_prs, kind="development_pr")
    provisions = _receipt_outcomes(args.provisions, kind="provision")
    issues = [] if args.offline else collect.intake_issues(api)
    tracking = sorted(
        {record["tracking_issue"]["number"] for record in snapshot.contributions.values()}
        | {issue["number"] for issue in issues}
    )
    comments = [] if args.offline else collect.command_comments(api, tracking, args.since)
    source_snapshots = (
        {}
        if args.offline
        else collect.upstream_snapshots(
            api, snapshot.contributions.values(), issues, policy
        )
    )
    issue_snapshots = (
        {}
        if args.offline
        else collect.tracking_issues(api, snapshot.contributions.values(), issues)
    )
    acceptances = (
        {}
        if args.offline
        else collect.label_acceptances(
            api, issues, INTAKE_LABEL, policy["command_authority"]["actor_ids"]
        )
    )
    runs = queue = None
    if not args.offline and args.gh_aw:
        try:
            runs = collect.dispatcher_runs(api)
            queue = queue_result.queue_work_states(args.gh_aw, collect.CONTROL_REPO)
        except (collect.CollectError, OSError, ValueError) as error:
            print(f"dispatch recovery skipped this tick: {error}", file=sys.stderr)
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
    try:
        output = tick(
            api,
            snapshot,
            policy=policy,
            now=now,
            run_id=os.environ.get("GITHUB_RUN_ID"),
            publications=publications,
            pressure=observed_pressure,
            issues=issues,
            comments=comments,
            observe=not args.offline,
            dispatcher_runs=runs,
            queue_works=queue,
            acceptances=acceptances,
            tracking_issues=issue_snapshots,
            upstream_snapshots=source_snapshots,
            provisions=provisions,
            development_prs=development_prs,
        )
    except isolation.TickFailure as failure:
        # A pipeline failure: nothing is written, so nothing is committed.
        print(f"reconciliation stopped: {failure}", file=sys.stderr)
        for cause in failure.failures or (failure,):
            traceback.print_exception(cause, file=sys.stderr)
        return 1
    for failure in output.failures:
        # The traceback stays in the workflow log; state and the tracking
        # issue get the bounded description only.
        print(f"contribution not processed in this tick: {failure}", file=sys.stderr)
        traceback.print_exception(failure, file=sys.stderr)
    for kind, record in output.staged:
        store.stage(kind, record)
    output.report["written"] = store.write_staged()
    text = json.dumps(output.report, indent=2, default=str)
    if args.report:
        Path(args.report).write_text(text)
    else:
        print(text)
    # A processing error is charged to one contribution and the tick goes
    # on, so the run succeeds. The annotation keeps a pipeline bug visible
    # on the workflow run. It goes to stdout unless the report does.
    for line in processing_error_annotations(output.report):
        print(line, file=sys.stdout if args.report else sys.stderr)
    return 0


# Bound of one workflow annotation.
MAX_ANNOTATION = 600


def processing_error_annotations(report: dict) -> list[str]:
    """One GitHub Actions ``::error::`` line per processing error of a tick.

    The text is built from the bounded description in the report, cleaned
    again, and escaped as the workflow command format requires, so a message
    can neither span lines nor start another command."""
    lines = []
    for item in report.get("processing_errors") or []:
        text = isolation.clean_text(
            f"processing error for {item.get('contribution')} in {item.get('step')}: "
            f"{item.get('error_class')}: {item.get('message')}; {item.get('decision')}",
            MAX_ANNOTATION)
        text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        lines.append(f"::error::{text}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m reconcile")
    parser.add_argument("--root")
    parser.add_argument("--publications")
    parser.add_argument("--provisions")
    parser.add_argument("--development-prs")
    parser.add_argument("--report")
    parser.add_argument("--since")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--gh-aw", help="pinned gh-aw v1.2.2 binary used only to read queue state"
    )
    args = parser.parse_args(argv)
    return cmd_reconcile(args)


if __name__ == "__main__":
    raise SystemExit(main())
