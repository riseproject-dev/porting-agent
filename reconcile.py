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
import re
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import capacity
import collect
import controller
import dossier_gate
import engine
import handoff
import isolation
import lifecycle
import observe_ci
import recovery
import schemas
import state
import submission
import work
from common import DEV_REF, INTAKE_LABEL, UPSTREAM_REF, load_policy, repository_root, safe_id


@dataclass
class Snapshot:
    contributions: dict[str, dict]
    operations: dict[str, dict]
    consumed: dict[int, dict]
    projects: dict[str, dict]
    evidence: dict[str, dict]
    # What the locked upstream-PR wait already recorded for audit, by
    # contribution: the observation keys of its `record_audit_observation`
    # events, so each observation is recorded once.
    audited: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class TickOutput:
    staged: list[tuple[str, dict]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    # The contributions this tick did not process, with the exception of each.
    failures: list[isolation.ContributionFailure] = field(default_factory=list)


# The wait before the next publish-only retry, by the publications that
# failed so far (`pending_publication.attempts`): 15 minutes after the first,
# 60 after the second, 240 after every later one.
RETRY_BACKOFF_MINUTES = (15, 60, 240)
# A launched publish-only retry whose Result is not folded by then counts as a
# failed publication. The retry workflow's jobs time out after 30 minutes.
PUBLISH_RESULT_WAIT_MINUTES = 60


def publish_retry_delay(attempts: int) -> int:
    """Minutes before the next publish-only retry once ``attempts``
    publications of the held Result have failed."""
    return RETRY_BACKOFF_MINUTES[min(max(attempts, 1), len(RETRY_BACKOFF_MINUTES)) - 1]


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
        "controller": {},
        "requested_effects": [],
        "dispatch_relaunch": [],
        "provision_requests": [],
        "publication_retries": [],
        "dev_pr_requests": [],
        "handoff_requests": [],
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
      (the launch itself was lost; no run ended, so no row is taken);
    * otherwise a dispatcher run ended without its Work being claimed. The
      table's ``retry_dispatch`` launches the dispatcher again within
      ``dispatch_recovery`` (it replays the same committed request, so the
      queue sees the identical Work), and ``exhaust_dispatch_recovery`` waits
      for a human once that budget is spent. Its count is the number of
      dispatcher runs that ended since the request. Each
      ended run is decided once: the record names the last one it decided
      in ``active_work.dispatched_run_id``.

    A graph the queue reports completed or cancelled takes
    ``allocate_work_retry`` or ``exhaust_work_recovery``. A record in a state
    the table does not have is left unchanged and reported.

    Returns (relaunch requests, staged records, report). No model is involved
    in deciding; a relaunch is an ordinary dispatcher run.
    """
    import work
    relaunch: list[dict] = []
    staged: list[tuple[str, dict]] = []
    report: list[dict] = []
    if runs is None or queue_works is None:
        return relaunch, staged, report
    iso = now.isoformat()
    pending = sorted(((k, c) for k, c in contributions.items() if c.get("active_work")),
                     key=lambda kc: kc[1]["active_work"]["requested_at"])
    for key, c in pending:
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "dispatch_recovery"):
            aw = c["active_work"]
            if c["phase"] == "handoff" and engine.state_key(c) not in SUBMISSION_WORK_STATES:
                new = engine.record_beside(c, "handoff_work_dropped", now=iso, active_work=None)
                contributions[key] = new
                staged.append(("contributions", new))
                report.append({"contribution": c["contribution_id"], "task": aw["task"],
                               "decision": "cleared obsolete Work at handoff"})
                continue
            state = queue_works.get(work.graph_id(aw["dedupe_key"]))
            if state not in (None, "available", "completed", "cancelled"):
                continue      # a live Claim is resolved by the worker outcome
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
            if not after:
                if now - requested < timedelta(minutes=DISPATCH_LAUNCH_GRACE_MINUTES):
                    report.append(entry | {"decision": "waiting for the dispatcher to start"})
                    continue
                relaunch.append({"type": "relaunch_dispatcher", "contribution_id": c["contribution_id"]})
                report.append(entry | {"decision": "relaunch: no dispatcher run"})
                continue
            latest = max(after, key=lambda r: r["created_at"])
            if str(latest["id"]) == str(aw.get("dispatched_run_id")):
                # This run was decided in an earlier tick; the relaunch it
                # led to has not shown up as a run yet.
                ended = datetime.fromisoformat(
                    str(latest.get("updated_at") or latest["created_at"]).replace("Z", "+00:00"))
                if now - ended >= timedelta(minutes=DISPATCH_LAUNCH_GRACE_MINUTES):
                    relaunch.append({"type": "relaunch_dispatcher",
                                     "contribution_id": c["contribution_id"]})
                    report.append(entry | {"decision": "relaunch: the requested run did not start"})
                else:
                    report.append(entry | {"decision": "waiting for the relaunched dispatcher"})
                continue
            # A dispatcher run ended since the request with the Work unclaimed.
            new, event, decision = _lost_dispatch(c, aw, latest, len(after), now=now,
                                                  policy=policy)
            if new is None:
                report.append(entry | {"decision": decision})
                continue
            contributions[key] = new
            staged.extend([("contributions", new), ("events", event)])
            if new.get("active_work"):
                relaunch.append({"type": "relaunch_dispatcher", "contribution_id": c["contribution_id"]})
            report.append(entry | {"decision": decision})
    return relaunch[: max(1, policy["concurrency"]["coding_workers"])], staged, report


def _lost_dispatch(c: dict, aw: dict, run: dict, count: int, *, now: datetime,
                   policy: dict) -> tuple[dict | None, dict | None, str]:
    """The table's row for requested Work that a dispatcher run left absent
    or unclaimed: ``retry_dispatch`` while ``dispatch_recovery`` (``count``
    relaunches spent) remains, ``exhaust_dispatch_recovery`` once it is
    spent. ``count`` is the number of dispatcher runs since the request,
    all ended. The record names the decided run in ``dispatched_run_id``."""
    iso = now.isoformat()
    if engine.state_key(c) not in lifecycle.LIFECYCLE:
        return None, None, (f"dispatch lost in {engine.state_key(c)}, a state the table does "
                            "not have")
    counts = {"dispatch_recovery": count}
    details = {"dispatcher_run": str(run["id"]), "work": aw["dedupe_key"]}
    limit = lifecycle.budget_limit("dispatch_recovery", policy)
    out = engine.apply_transition(c, "retry_dispatch", policy=policy, now=iso,
                                  cause_id=f"dispatcher-{run['id']}", facts={"dispatch_lost"},
                                  counts=counts, details=details | {"attempt": count + 1,
                                                                    "limit": limit})
    if out.accepted:
        new = engine.record_beside(out.record, "dispatcher_run_decided", now=iso,
                                   active_work=dict(aw, dispatched_run_id=str(run["id"])))
        return new, out.event, f"relaunch: dispatch attempt {count + 1} of {limit}"
    out = engine.apply_transition(c, "exhaust_dispatch_recovery", policy=policy, now=iso,
                                  cause_id=f"dispatcher-{run['id']}", facts={"dispatch_lost"},
                                  counts=counts, details=details | {"count": count, "limit": limit})
    if not out.accepted:
        return None, None, f"dispatch lost: {out.reason}"
    return out.record, out.event, "dispatch failed; waiting for a human"


def _terminal_work(c: dict, aw: dict, queue_state: str, *, now: datetime,
                   policy: dict) -> tuple[dict | None, dict | None, str]:
    """The table's row for requested Work whose graph is terminal without a
    usable Result (``controller.terminal_work``)."""
    return controller.terminal_work(c, aw, queue_state, now=now, policy=policy)


def _may_hold_work(rec: dict) -> bool:
    """Whether the record is in a state that can request Work at all: a
    working phase, or the submission review of the handoff phase, and not
    paused."""
    return (rec["phase"] in WORK_PHASES or engine.state_key(rec) in SUBMISSION_WORK_STATES) \
        and not rec.get("paused")


def _holds_dispatched_work(rec: dict) -> bool:
    """Whether a relaunch decided by dispatch recovery still applies once the
    record is observed: it holds requested Work, in a working phase or in a
    submission Work state of the handoff phase."""
    return rec.get("active_work") is not None and (
        rec["phase"] != "handoff" or engine.state_key(rec) in SUBMISSION_WORK_STATES)


# The phase in which the table requests Work of each task.
WORK_PHASE = {"investigation": "investigation", "development_patch": "development",
              "development_repair": "development", "upstream_revision": "upstream_review",
              work.SUBMISSION_REWRITE: "handoff", work.SUBMISSION_REVIEW: "handoff"}
# The states of the handoff phase that hold requested Work: the rewrite and
# the review of the submission.
SUBMISSION_REWRITE_STATE = lifecycle.SUBMISSION_REWRITE
SUBMISSION_REVIEW_STATE = lifecycle.SUBMISSION_REVIEW
SUBMISSION_WORK_STATES = (SUBMISSION_REWRITE_STATE, SUBMISSION_REVIEW_STATE)
# The phases whose states can request Work outside the handoff phase.
WORK_PHASES = frozenset({"investigation", "provisioning", "development", "upstream_review"})


def result_mismatch(rec: dict, pub: dict) -> str | None:
    """Why a Result does not match what the record requests, or ``None``.

    A Result is folded only when it matches the Work identity: the current
    generation and revision of the record, and the requested Work, which is
    the Work the record holds (its Work id), or the held Result of the same
    worker run (a publish-only retry). Everything else takes
    ``record_late_effect``. Park, abandon, and
    pause clear the Work and the pending publication, so a matching revision
    alone does not prove a match.

    A Result whose payload names no generation and no Work id was requested
    before the Work identity carried them: it is generation 0 and its Work is
    recognized by the revision (``work.task_identity``).
    """
    if rec["phase"] == "handoff" and engine.state_key(rec) not in SUBMISSION_WORK_STATES:
        return "late Result during handoff"
    if not _may_hold_work(rec):
        return "late Result for a stopped contribution"
    if engine.waits_for_human(rec):
        # A wait for a human has no Result row; only its own rows leave it.
        return "late Result while the contribution waits for a human"
    state = engine.state_key(rec)
    if state in lifecycle.LIFECYCLE and not any(
            edge.transition.trigger == lifecycle.Trigger.RESULT
            and edge.verb != "record_late_effect" for edge in lifecycle.state_edges(state)):
        # Only the `agent_running` states fold a Result; Work that a record
        # holds in another state has none of their rows.
        return f"late Result: {state} has no row that folds a Result"
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
    if (pub.get("status") == "investigation") != (rec["phase"] == "investigation"):
        # A dossier answers investigation Work only, and investigation Work
        # is answered by a dossier only.
        return f"late Result: a {pub.get('status')} Result has no row in the {rec['phase']} phase"
    if pub.get("status") == submission.STATUS and rec["phase"] != "handoff":
        return f"late Result: a submission review has no row in the {rec['phase']} phase"
    requested = rec.get("active_work")
    if requested and WORK_PHASE.get(requested.get("task")) != rec["phase"]:
        # Each phase requests its own Work only: a Result of Work of another
        # phase has no row of this one.
        return (f"late Result: {requested.get('task')} Work is not requested in the "
                f"{rec['phase']} phase")
    if requested and work.work_generation(requested) == current \
            and requested.get("revision") == revision \
            and (requested.get("dedupe_key") == key if key is not None else True) \
            and task.get("task", requested.get("task")) == requested.get("task") \
            and (pub.get("status") == "investigation") == (requested.get("task") == "investigation"):
        # The Work id names the task; a dossier answers investigation Work
        # only, and investigation Work is answered by a dossier only.
        return None
    if (rec.get("pending_publication") or {}).get("worker_run_id") == run \
            and rec["phase"] in ("development", "upstream_review"):
        # A Result is held for a publish retry only where patch Work runs.
        return None
    return "late Result without matching requested Work"


def _late_effect(rec: dict, pub: dict, why: str, *, policy: dict, now: str) -> dict:
    """The audit event of ``record_late_effect`` for one Result."""
    task = pub.get("task") or {}
    generation, key = work.task_identity(task)
    return engine.record_late_effect(
        rec, policy=policy, now=now, cause_id=_result_cause(pub, pub.get("worker_run_id")),
        details={"result_status": pub.get("status"), "task_revision": task.get("revision"),
                 "task_generation": generation, "task_work": key, "why": why})


def _result_cause(outcome: dict, worker_run_id: str | None) -> str | None:
    run = worker_run_id or (outcome.get("artifact") or {}).get("run_id")
    return str(run) if run else None


# The decisions of a verified dossier. A Result without a dossier, or with a
# decision that is none of these, has no usable dossier.
DOSSIER_DECISIONS = ("develop", "park", "close")
# The `terminal_reason` of a contribution closed by its dossier is derived by
# `dossier_gate.close_confirmation` from PyPI documents the reconciler reads
# itself, never taken from the dossier.
_DISTRIBUTION_NAME = re.compile(r"[A-Za-z0-9._-]+")


def _reject_dossier(rec: dict, outcome: dict, *, now: str, policy: dict,
                    worker_run_id: str | None, evidence: dict | None,
                    why: str) -> tuple[dict, list[tuple[str, dict]]]:
    """Take ``reject_dossier``, or ``exhaust_investigation_recovery`` once
    ``investigation_attempts`` is exhausted, for an investigation Result
    without a usable dossier: no dossier, an invalid one (an incomplete
    patch scope included), a decision that is none of develop, park, and
    close, or a decision close that PyPI does not confirm."""
    staged: list[tuple[str, dict]] = [("evidence", evidence)] if evidence else []
    facts = {"dossier_rejected", "no_usable_dossier"}
    details = {"result_status": outcome.get("status"),
               "decision": ((outcome.get("dossier") or {}).get("proposal") or {}).get("decision")
               if isinstance(outcome.get("dossier"), dict) else None,
               "why": why[:500],
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


def _admitted_scope(rec: dict, decision: dict) -> tuple[str | None, list | None]:
    """The admission class and scope paths a dossier decision admits: the
    dossier's own for a decision develop, the recorded ``admission_override``
    for a decision park (the override applies against a park only)."""
    if decision.get("decision") == "park":
        override = rec.get("admission_override") or {}
        return override.get("class"), override.get("paths")
    return decision.get("admission_class"), decision.get("allowed_paths")


def fold_dossier(rec: dict, outcome: dict, *, now: str, policy: dict,
                 worker_run_id: str | None, pypi=None) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold an investigation Result through the table's rows of
    ``investigation.agent_running``.

    * no dossier, a decision that is none of develop, park, and close, a
      patch scope that is incomplete where ``admit_development`` checks it,
      or a decision close that PyPI does not confirm: ``reject_dossier``
      (``exhaust_investigation_recovery`` once ``investigation_attempts`` is
      exhausted);
    * develop, or park with a recorded ``admission_override``:
      ``readmit_development`` to the recorded fork when a refresh began with
      the fork and development PR the record still holds, otherwise
      ``admit_development`` to provisioning. Both clear the override;
    * park without an override: ``park_investigation``;
    * close, confirmed from PyPI: ``close_investigation``. ``pypi`` reads
      the PyPI document of each recorded distribution
      (``collect.pypi_project`` by default); ``terminal_reason`` is
      ``already_released`` when the latest release of a distribution has a
      riscv64 wheel, else ``no_work`` (pure-Python wheels only). A failed
      read raises ``collect.CollectError``: a collector failure, so the tick
      skips the contribution and folds the same Result again. A recorded
      override is not applied, and the event says so.

    The dossier is recorded as evidence first. The rows set the dossier, the
    admission class, and the scope paths; the complete coverage the scope
    was checked for is recorded beside them in ``scope_coverage_complete``.
    """
    if engine.state_key(rec) != "investigation.agent_running":
        raise lifecycle.TransitionError(
            f"an investigation Result is folded in investigation.agent_running only, not in "
            f"{engine.state_key(rec)}")
    dossier = outcome.get("dossier")
    staged: list[tuple[str, dict]] = []
    evidence = None
    if isinstance(dossier, dict):
        eid = safe_id(f"dossier-{rec['contribution_id'].replace('/', '--')}-r{rec['revision']}")
        evidence = {"schema_version": 1, "evidence_id": eid, "contribution_id": rec["contribution_id"],
                    "kind": "dossier", "policy_version": policy.get("policy_version", "unknown"),
                    "collected_at": now,
                    "summary": str((outcome.get("proposal") or {}).get("diagnosis")
                                   or "investigation dossier")[:8000],
                    "limitations": [], "data": dossier,
                    "digests": {"artifact": (outcome.get("artifact") or {}).get("digest") or ""}}
    decision = (dossier.get("proposal") if isinstance(dossier, dict) else None)
    decision = decision if isinstance(decision, dict) else {}
    kind = decision.get("decision")
    common = dict(policy=policy, now=now, worker_run_id=worker_run_id, evidence=evidence)
    if evidence is None:
        return _reject_dossier(rec, outcome, why="the Result has no dossier", **common)
    if kind not in DOSSIER_DECISIONS:
        return _reject_dossier(rec, outcome, why=f"the dossier decision {kind!r} is not develop, "
                               "park, or close", **common)
    close_reason = close_finding = None
    if kind == "close":
        # `closed.idle` is final and the dossier is worker output: PyPI, read
        # here, confirms the close and gives its reason.
        read = pypi or collect.pypi_project
        names = [n for n in (rec.get("distributions") or []) if isinstance(n, str)]
        documents = {name: read(name) if _DISTRIBUTION_NAME.fullmatch(name) else None
                     for name in names}
        close_reason, problem, close_finding = dossier_gate.close_confirmation(names, documents)
        if problem:
            return _reject_dossier(
                rec, outcome, why=f"PyPI does not confirm the decision close: {problem}",
                **common)
    staged.append(("evidence", evidence))
    cause = _result_cause(outcome, worker_run_id)
    override = rec.get("admission_override")
    rationale = str(decision.get("rationale") or "")[:2000]
    details = {"decision": kind, "evidence_id": evidence["evidence_id"]}
    if kind == "close":
        if override:
            details["admission_override_not_applied"] = override
        out = engine.apply_transition(
            rec, "close_investigation", policy=policy, now=now, cause_id=cause,
            facts={"dossier_decision_close", "close_confirmed_by_pypi"},
            values={"terminal_reason": close_reason},
            details=details | {"rationale": rationale, "pypi": close_finding})
    elif kind == "park" and not override:
        out = engine.apply_transition(
            rec, "park_investigation", policy=policy, now=now, cause_id=cause,
            facts={"dossier_decision_park"},
            values={"parked_reason": rationale or "the dossier decided park"},
            details=details)
    else:
        admission_class, paths = _admitted_scope(rec, decision)
        refresh = rec.get("refresh") or {}
        fork = rec.get("fork") or {}
        readmit = bool(refresh and refresh.get("head_sha") and fork.get("repository_id")
                       and fork.get("pr_number") == refresh.get("pr_number"))
        # The scope is a guard of `admit_development`. An override is checked
        # whichever row admits it, since nothing else names its class and
        # paths; its paths are the human's scope, so the dossier's coverage
        # is recorded for it but does not refuse it.
        problem = dossier_gate.scope_problem(decision, admission_class=admission_class,
                                             allowed_paths=paths)
        refused = (dossier_gate.scope_problem(decision, admission_class=admission_class,
                                              allowed_paths=paths, coverage=False)
                   if kind == "park" else problem)
        if refused and (kind == "park" or not readmit):
            return _reject_dossier(
                rec, outcome, why=f"the admitted patch scope is incomplete: {refused}",
                **common)
        if override and kind == "park":
            details["admission_override"] = override
        elif override:
            details["admission_override_cleared"] = override
        values = {"dossier_evidence_id": evidence["evidence_id"],
                  "admission_class": admission_class,
                  "scope_paths": list(paths) if isinstance(paths, list) else paths}
        if readmit:
            out = engine.apply_transition(
                rec, "readmit_development", policy=policy, now=now, cause_id=cause,
                facts={"development_admitted", "recorded_fork_matches"}, values=values,
                details=details)
        else:
            out = engine.apply_transition(
                rec, "admit_development", policy=policy, now=now, cause_id=cause,
                facts={"development_admitted", "patch_scope_complete", "fresh_fork_required"},
                values=values, details=details)
    if not out.accepted:
        raise lifecycle.TransitionError(f"the dossier decision {kind} is refused in "
                                        f"{engine.state_key(rec)}: {out.reason}")
    new = out.record
    if out.verb in ("admit_development", "readmit_development"):
        # The scope was checked complete for the dossier it came from.
        new = engine.record_beside(new, "scope_checked", now=now,
                                   scope_coverage_complete=problem is None)
    staged.extend([("events", out.event), ("contributions", new)])
    return new, staged


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _outcome_basis_problem(rec: dict, outcome: dict, worker_run_id: str | None) -> str | None:
    approval = rec.get("handoff_approval") or {}
    if outcome.get("verdict") not in (submission.PASS, submission.FAIL):
        return "the outcome has no verdict"
    if outcome.get("approved_head") != approval.get("head") \
            or outcome.get("base_sha") != rec["upstream"].get("base_sha"):
        return "the outcome is not for the approved head on the frozen base"
    reasons = outcome.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons) \
            or len(reasons) > submission.MAX_REASONS:
        return "the outcome's reasons are malformed"
    if outcome["verdict"] == submission.FAIL and not reasons:
        return "a failed outcome names no reason"
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(worker_run_id or "")):
        return "the outcome names no worker run"
    artifact = outcome.get("publication_artifact_id")
    if not isinstance(artifact, int) or isinstance(artifact, bool) or artifact <= 0:
        return "the outcome names no publication artifact"
    return None


def _rewrite_problem(rec: dict, outcome: dict, worker_run_id: str | None) -> str | None:
    """Why the trusted outcome of a submission rewrite is unusable, or ``None``."""
    problem = _outcome_basis_problem(rec, outcome, worker_run_id)
    if problem or outcome["verdict"] == submission.FAIL:
        return problem
    commits = outcome.get("commits")
    if outcome["reasons"] or not isinstance(commits, list) or not commits \
            or len(commits) > handoff.MAX_COMMITS \
            or not all(isinstance(c, dict) and isinstance(c.get("message"), str)
                       and len(c["message"]) <= 20000 and _SHA_RE.match(str(c.get("tree")))
                       for c in commits) \
            or not all(_SHA_RE.match(str(outcome.get(k) or "")) for k in
                       ("head", "clean_tree", "candidate_tree")) \
            or commits[-1]["tree"] != outcome["clean_tree"] \
            or not all(re.fullmatch(r"[0-9a-f]{64}", str(outcome.get(k) or ""))
                       for k in ("series_digest", "text_digest")) \
            or not isinstance(outcome.get("title"), str) or not outcome["title"].strip() \
            or len(outcome["title"]) > submission.MAX_TITLE \
            or not isinstance(outcome.get("body"), str) or len(outcome["body"]) > submission.MAX_BODY \
            or submission.text_digest(outcome["title"], outcome["body"]) != outcome["text_digest"]:
        return "a passing outcome is malformed"
    return None


def _review_problem(rec: dict, outcome: dict, worker_run_id: str | None,
                    rewrite: dict | None) -> str | None:
    """Why the trusted outcome of a submission review is unusable, or
    ``None``: it must review exactly the rewrite the record names."""
    problem = _outcome_basis_problem(rec, outcome, worker_run_id)
    if problem:
        return problem
    data = (rewrite or {}).get("data") or {}
    if not rewrite or outcome.get("rewrite_evidence_id") != rec.get("submission_rewrite_id") \
            or outcome.get("series_digest") != data.get("series_digest") \
            or outcome.get("text_digest") != data.get("text_digest"):
        return "the outcome does not review the rewrite the record names"
    if not isinstance(outcome.get("expectations"), list) or len(outcome["expectations"]) > 60:
        return "the outcome's expectations are malformed"
    return None


def _unusable_submission_result(rec: dict, why: str, *, now: str,
                                policy: dict) -> tuple[dict, list[tuple[str, dict]]]:
    """A rewrite or review Result that is not usable: Work without a usable
    Result (``allocate_work_retry``, then ``exhaust_work_recovery``)."""
    aw = rec.get("active_work") or {}
    new, event, decision = _terminal_work(rec, aw, "result_unusable",
                                          now=datetime.fromisoformat(now), policy=policy)
    if new is None:
        raise lifecycle.TransitionError(f"an unusable submission Result: {decision}")
    event = dict(event, details=dict(event.get("details") or {},
                                     why=isolation.clean_text(why, 300)))
    return new, [("contributions", new), ("events", event)]


def _submission_evidence(rec: dict, kind: str, outcome: dict, data: dict, summary: str, *,
                         now: str, policy: dict, worker_run_id: str | None) -> dict:
    run = str(worker_run_id or "0")
    eid = safe_id(f"{kind.replace('_', '-')}-{rec['contribution_id'].replace('/', '--')}"
                  f"-r{rec['revision']}-{run}")
    evidence = {"schema_version": 1, "evidence_id": eid, "contribution_id": rec["contribution_id"],
                "kind": kind, "policy_version": policy.get("policy_version", "unknown"),
                "collected_at": now, "summary": summary[:8000], "limitations": [], "data": data,
                "digests": {"series": str(outcome.get("series_digest") or "")}}
    schemas.validate("evidence", evidence)
    return evidence


def fold_submission_rewrite(rec: dict, outcome: dict, *, now: str, policy: dict,
                            worker_run_id: str | None) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold the Result of the submission rewrite Work through the table's
    rows of ``handoff.agent_running``.

    The worker's publish job ran the trusted deterministic check
    (``submission.check_rewrite``) without a token: its outcome is the
    verdict. A pass takes ``accept_submission_rewrite`` with the rewritten
    series, title, and description recorded as rewrite evidence, which the
    review Work reads and the build of `riscv64` writes. A fail takes
    ``reject_submission_rewrite``: the reasons are recorded and become the
    pending feedback of the next development Work; nothing is posted. An
    outcome that is not usable is Work without a usable Result."""
    why = (_rewrite_problem(rec, outcome, worker_run_id)
           if outcome.get("status") == submission.REWRITE
           else f"the rewrite Result is not usable: {outcome.get('status')}: "
                f"{outcome.get('reason') or ''}")
    if why:
        return _unusable_submission_result(rec, why, now=now, policy=policy)
    passed = outcome["verdict"] == submission.PASS
    data = {"verdict": outcome["verdict"], "reasons": list(outcome["reasons"]),
            "approved_head": outcome["approved_head"], "base_sha": outcome["base_sha"],
            "artifact": {"run_id": str(worker_run_id),
                         "artifact_id": outcome.get("publication_artifact_id")}}
    if passed:
        data.update({k: outcome[k] for k in ("title", "body", "head", "clean_tree",
                                             "candidate_tree", "series_digest", "text_digest")},
                    commits=[{"message": c["message"], "tree": c["tree"]}
                             for c in outcome["commits"]],
                    rules_from=[str(n)[:200] for n in outcome.get("rules_from") or []][:4])
    summary = ("The submission rewrite passed the trusted check." if passed else
               "The submission rewrite did not pass:\n"
               + "\n".join(f"- {r}" for r in data["reasons"]))
    evidence = _submission_evidence(rec, submission.REWRITE, outcome, data, summary, now=now,
                                    policy=policy, worker_run_id=worker_run_id)
    eid = evidence["evidence_id"]
    cause = _result_cause(outcome, worker_run_id)
    details = {"evidence_id": eid, "verdict": outcome["verdict"],
               "reasons": [isolation.clean_text(r, 300) for r in data["reasons"][:8]]}
    if passed:
        out = engine.apply_transition(
            rec, "accept_submission_rewrite", policy=policy, now=now, cause_id=cause,
            facts={"submission_rewrite_passed"}, values={"submission_rewrite_id": eid},
            details=dict(details, head=outcome["head"]))
    else:
        pending = list(dict.fromkeys((rec.get("pending_feedback_evidence_ids") or []) + [eid]))
        out = engine.apply_transition(
            rec, "reject_submission_rewrite", policy=policy, now=now, cause_id=cause,
            facts={"submission_rewrite_failed"},
            values={"pending_feedback_evidence_ids": pending[-64:]}, details=details)
    if not out.accepted:
        raise lifecycle.TransitionError(f"{out.verb} refused: {out.reason}")
    return out.record, [("evidence", evidence), ("events", out.event),
                        ("contributions", out.record)]


def fold_submission_review(rec: dict, outcome: dict, *, now: str, policy: dict,
                           worker_run_id: str | None,
                           evidence: dict | None = None) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold the Result of the submission review Work through the table's
    rows of ``handoff.reviewing``.

    The worker's publish job checked that the review answers the Work and
    reviews exactly the rewritten series and text
    (``submission.check_review``). A pass takes ``accept_submission_review``:
    the review evidence carries the rewrite it passed (the series, title,
    description, and the artifact that holds the series), which the build of
    `riscv64` and the package read. A fail takes ``reject_submission_review``
    with its reasons as pending feedback. An outcome that is not usable is
    Work without a usable Result. ``evidence`` holds the rewrite evidence the
    record names."""
    rewrite = (evidence or {}).get(rec.get("submission_rewrite_id") or "")
    why = (_review_problem(rec, outcome, worker_run_id, rewrite)
           if outcome.get("status") == submission.REVIEW
           else f"the review Result is not usable: {outcome.get('status')}: "
                f"{outcome.get('reason') or ''}")
    if why:
        return _unusable_submission_result(rec, why, now=now, policy=policy)
    passed = outcome["verdict"] == submission.PASS
    data = dict(copy.deepcopy(rewrite["data"]), verdict=outcome["verdict"],
                reasons=list(outcome["reasons"]),
                rewrite_evidence_id=rewrite["evidence_id"],
                review_artifact={"run_id": str(worker_run_id),
                                 "artifact_id": outcome.get("publication_artifact_id")},
                expectations=[{k: str(e.get(k))[:1000] if k != "met" else bool(e.get("met"))
                               for k in ("expectation", "met", "source", "note") if k in e}
                              for e in outcome["expectations"][:60] if isinstance(e, dict)])
    summary = ("The submission review passed the rewritten series and text." if passed else
               "The submission review did not pass:\n"
               + "\n".join(f"- {r}" for r in data["reasons"]))
    review = _submission_evidence(rec, submission.REVIEW, outcome, data, summary, now=now,
                                  policy=policy, worker_run_id=worker_run_id)
    eid = review["evidence_id"]
    cause = _result_cause(outcome, worker_run_id)
    details = {"evidence_id": eid, "verdict": outcome["verdict"],
               "reasons": [isolation.clean_text(r, 300) for r in data["reasons"][:8]]}
    if passed:
        out = engine.apply_transition(
            rec, "accept_submission_review", policy=policy, now=now, cause_id=cause,
            facts={"submission_review_passed"}, values={"submission_review_id": eid},
            details=dict(details, head=data.get("head")))
    else:
        pending = list(dict.fromkeys((rec.get("pending_feedback_evidence_ids") or []) + [eid]))
        out = engine.apply_transition(
            rec, "reject_submission_review", policy=policy, now=now, cause_id=cause,
            facts={"submission_review_failed"},
            values={"pending_feedback_evidence_ids": pending[-64:]}, details=details)
    if not out.accepted:
        raise lifecycle.TransitionError(f"{out.verb} refused: {out.reason}")
    return out.record, [("evidence", review), ("events", out.event),
                        ("contributions", out.record)]


def apply_publication(rec: dict, outcome: dict, *, now: str, policy: dict | None = None,
                      worker_run_id: str | None = None, evidence: dict | None = None,
                      pypi=None) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold one Result (from fork_publish / queue_result) into the record.

    * investigation Work: the dossier decides, by the table (``fold_dossier``);
    * the submission rewrite and review Work: their trusted checks decide
      (``fold_submission_rewrite``, ``fold_submission_review``);
    * patch Work in development and revision Work in upstream review: the
      table's rows (``fold_patch_result``).

    ``pypi`` reads a PyPI document for the confirmation of a decision close
    (``collect.pypi_project`` by default).
    """
    if outcome.get("status") == "investigation":
        return fold_dossier(rec, outcome, now=now, policy=policy or {},
                            worker_run_id=worker_run_id, pypi=pypi)
    if engine.state_key(rec) == SUBMISSION_REWRITE_STATE:
        return fold_submission_rewrite(rec, outcome, now=now, policy=policy or {},
                                       worker_run_id=worker_run_id)
    if engine.state_key(rec) == SUBMISSION_REVIEW_STATE:
        return fold_submission_review(rec, outcome, now=now, policy=policy or {},
                                      worker_run_id=worker_run_id, evidence=evidence)
    return fold_patch_result(rec, outcome, now=now, policy=policy or {},
                             worker_run_id=worker_run_id)


_ARTIFACT_KEYS = ("work_id", "claim_id", "artifact_id", "name", "digest", "run_id")


def _rehearsal_evidence(rec: dict, outcome: dict, run: str, policy: dict, now: str) -> dict:
    """The evidence a worker reports when its reproduction infrastructure
    was blocked."""
    proposal = outcome.get("proposal") or {}
    evidence = {
        "schema_version": 1,
        "evidence_id": safe_id(
            f"rehearsal-{rec['contribution_id'].replace('/', '--')}-r{rec['revision']}-{run}"),
        "contribution_id": rec["contribution_id"],
        "kind": "rehearsal",
        "policy_version": policy.get("policy_version", "unknown"),
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
    return evidence


def _fail_publication(rec: dict, run: str, reason: str, *, now: str, policy: dict,
                      cause: str | None, details: dict) -> engine.Outcome:
    """A publication of the Result of ``run`` failed: ``schedule_publish_retry``
    holds the Result for a publish-only retry and charges ``publish_recovery``;
    ``exhaust_publish_recovery`` once it is spent."""
    before = int((rec.get("pending_publication") or {}).get("attempts", 0))
    pending = {"worker_run_id": run, "attempts": before + 1, "last_reason": reason[:2000],
               "next_attempt_at": (datetime.fromisoformat(now) + timedelta(
                   minutes=publish_retry_delay(before + 1))).isoformat()}
    out = engine.apply_transition(
        rec, "schedule_publish_retry", policy=policy, now=now, cause_id=cause,
        facts={"publication_infrastructure_error"}, values={"pending_publication": pending},
        details=details | {"next_attempt_at": pending["next_attempt_at"]})
    if not out.accepted:
        out = engine.apply_transition(
            rec, "exhaust_publish_recovery", policy=policy, now=now, cause_id=cause,
            facts={"publication_infrastructure_error"}, details=details | {"attempts": before})
    return out


def _charged_launch(held: dict, outcome: dict) -> bool:
    """Whether the publication failure an ``infra_error`` Result reports is
    already charged to the held publication.

    Every failed publication is charged once (``schedule_publish_retry``):
    the worker's own as attempt 1, and each launched publish-only retry,
    dispatched with attempt ``pending_publication.attempts + 1``, either by
    its Result or, when none is folded within the wait, by the wait. So a
    Result of an attempt not above ``attempts`` is charged already, and so
    is any Result while no retry is launched (``launched_at`` absent): the
    launches before were all charged."""
    attempt = outcome.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 1:
        return attempt <= int(held.get("attempts", 0))
    return not held.get("launched_at")


# The states whose Results ``fold_patch_result`` folds, and the rows that
# differ between them: the success row, the Result statuses it folds, what a
# `no_change` report and a stale proposal take, and the statuses that are a
# failed publication. In upstream review the success row records the head
# read back on `riscv64`, so a publication whose push was not read back
# (`uncertain`) is a failed publication there.
WORK_RESULT_STATES = {
    "development.agent_running": {"published": "record_publish_success",
                                  "success": ("published", "uncertain"),
                                  "failed": ("infra_error",),
                                  "stale": "replan_development"},
    "upstream_review.agent_running": {"published": "record_revision_publish_success",
                                      "success": ("published",),
                                      "failed": ("infra_error", "uncertain"),
                                      "no_change": "require_reviewer_answer"},
}

_SHA = re.compile(r"[0-9a-f]{40}")


def published_head(outcome: dict) -> str:
    """The head a published Result read back on `riscv64`, which
    ``record_revision_publish_success`` records in ``upstream_pr.head_sha``:
    the commit of its branch operation, which the publisher confirmed by
    reading the branch back after the push, or, for a publish-only retry
    that found the push of an earlier attempt on the branch (no operation),
    the candidate it read there. A head that was not read back is refused."""
    op = outcome.get("operation")
    candidate = outcome.get("candidate")
    head = candidate
    if op:
        head = (op.get("proposed") or {}).get("commit")
        if op.get("status") != "confirmed" or op.get("kind") not in (
                "branch_update", "branch_rewrite") \
                or (op.get("target") or {}).get("ref") != UPSTREAM_REF \
                or (candidate is not None and candidate != head):
            head = None
    if not (isinstance(head, str) and _SHA.fullmatch(head)):
        raise lifecycle.TransitionError(
            f"the published revision names no head read back on {UPSTREAM_REF}")
    return head


def fold_patch_result(rec: dict, outcome: dict, *, now: str, policy: dict,
                      worker_run_id: str | None) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold the Result of patch Work through the rows of
    ``development.agent_running``, or of revision Work through the rows of
    ``upstream_review.agent_running``.

    In upstream review the rows are the same but four: a published Result
    takes ``record_revision_publish_success`` (which also clears exactly the
    carried feedback, records the head read back on `riscv64` in
    ``upstream_pr.head_sha``, and bumps the revision), an uncertain Result
    (its push was not read back) is a failed publication like an
    infrastructure error, a ``no_change`` report takes
    ``require_reviewer_answer`` (a human answers the reviewer; nothing is
    posted on the upstream PR), and a proposal rejected as stale takes
    ``reject_patch_proposal`` like any other rejection, as no row replans
    there. Upstream CI is never rerun.

    * published or uncertain: ``record_publish_success`` (clears exactly the
      feedback the Work carried); the published files join ``scope_paths``
      and ``no_progress`` starts again beside the row;
    * rejected in trusted validation, blocked, or a ``proposal`` (no
      publisher produces one any more): ``reject_patch_proposal``; a blocked
      Result's rehearsal evidence is recorded and named in the event;
    * a ``no_change`` report (the repair worker found a failure not caused
      by the patch): ``request_ci_rerun`` when the Work carries no feedback
      and ``flaky_reruns`` remains, ``reject_patch_proposal`` otherwise;
    * rejected as stale (the fork head moved): ``replan_development``;
    * an infrastructure error of the publication (in upstream review also an
      uncertain Result): ``schedule_publish_retry`` holds the same Result for
      a publish-only retry within ``publish_recovery``,
      ``exhaust_publish_recovery`` once it is spent.

    The Result of a publish-only retry ends the held publication: every row
    that folds it is taken with ``pending_publication`` cleared beside it.
    A refused row raises ``TransitionError``.
    """
    rows = WORK_RESULT_STATES.get(engine.state_key(rec))
    if rows is None:
        raise lifecycle.TransitionError(
            f"the Result of patch or revision Work is folded in "
            f"{' or '.join(WORK_RESULT_STATES)} only, not in {engine.state_key(rec)}")
    status, fclass = outcome.get("status"), outcome.get("failure_class")
    if status == "proposal":
        status, fclass = "rejected", "validation"
    held = rec.get("pending_publication")
    run = str(worker_run_id or (held or {}).get("worker_run_id")
              or (outcome.get("artifact") or {}).get("run_id") or "0")
    cause = _result_cause(outcome, worker_run_id)
    staged: list[tuple[str, dict]] = []
    op = outcome.get("operation")
    if op:
        staged.append(("operations", op))
    details: dict[str, Any] = {"result_status": status}
    if fclass:
        details["failure_class"] = fclass
    if outcome.get("reason"):
        details["reason"] = str(outcome["reason"])[:500]
    failed = status in rows["failed"]
    if failed and held and not rec.get("active_work") \
            and _charged_launch(held, outcome):
        # The failed publication of this launch is already charged, by the
        # wait for its Result: the late Result is recorded once, for audit,
        # and charges nothing again.
        event = engine.record_late_effect(
            rec, policy=policy, now=now, cause_id=cause,
            details={"result_status": status, "task_revision": rec["revision"],
                     "task_generation": int(rec.get("generation", 0)),
                     "task_work": (outcome.get("task") or {}).get("dedupe_key"),
                     "why": "the failed publication of this publish-only retry was already "
                            "charged when its Result did not arrive within "
                            f"{PUBLISH_RESULT_WAIT_MINUTES} minutes"})
        return rec, staged + [("contributions", rec), ("events", event)]
    if failed:
        out = _fail_publication(rec, run, str(outcome.get("reason") or ""), now=now,
                                policy=policy, cause=cause, details=details)
    else:
        if status in rows["success"]:
            verb = rows["published"]
            facts = {"result_published" if verb == "record_publish_success" else "revision_published"}
        elif status == "rejected" and fclass == "stale" and "stale" in rows:
            verb, facts = rows["stale"], {"proposal_stale"}
        elif status == "no_change" and "no_change" in rows:
            verb, facts = rows["no_change"], {"no_change_needed"}
        elif status == "no_change":
            # The repair worker reports a failure not caused by the patch:
            # `request_ci_rerun` when the Work carries no feedback and
            # `flaky_reruns` remains, else the outcome counts as
            # `reject_patch_proposal`, as both rows state.
            facts = {"failure_not_caused_by_patch"}
            verb = ("reject_patch_proposal" if engine.refusal(
                rec, "request_ci_rerun", policy=policy, now=now, facts=facts)
                else "request_ci_rerun")
        elif status in ("rejected", "worker_blocked"):
            verb, facts = "reject_patch_proposal", {"proposal_rejected_or_blocked"}
        else:
            raise lifecycle.TransitionError(f"no row folds a patch Result with status {status!r}")
        values: dict[str, Any] = {}
        if verb in ("record_publish_success", "record_revision_publish_success"):
            art = outcome.get("artifact") or {}
            artifact = rec.get("proposal_artifact")
            if all(k in art for k in _ARTIFACT_KEYS):
                artifact = {k: art[k] for k in _ARTIFACT_KEYS} | {
                    "result_commit": art.get("result_commit"), "outcome": status}
            values = {"proposal_artifact": artifact,
                      "publication_operation_id": (op or {}).get("operation_id")
                      or rec.get("publication_operation_id")}
        if verb == "record_revision_publish_success":
            head = published_head(outcome)
            values["upstream_pr.head_sha"] = head
            details.update(head=head, previous_head=lifecycle.recorded_head(rec))
        if status == "worker_blocked":
            evidence = _rehearsal_evidence(rec, outcome, run, policy, now)
            staged.append(("evidence", evidence))
            details["evidence_id"] = evidence["evidence_id"]
        out = engine.apply_transition(rec, verb, policy=policy, now=now, cause_id=cause,
                                      facts=facts, values=values, details=details)
    if not out.accepted:
        raise lifecycle.TransitionError(f"no row folds the patch Result ({status}) in "
                                        f"{engine.state_key(rec)}: {out.reason}")
    new = out.record
    if out.verb in ("record_publish_success", "record_revision_publish_success"):
        # Beside the row: the published files join the admitted scope (a
        # repair backed by review feedback may change a path the dossier
        # missed), and a published patch starts `no_progress` again.
        changed_files = ((op or {}).get("proposed") or {}).get("changed_files") or []
        scope = (list(dict.fromkeys((new.get("scope_paths") or []) + changed_files))
                 if changed_files else new.get("scope_paths"))
        new = engine.record_beside(new, "patch_published", now=now, scope_paths=scope,
                                   budget=dict(new["budget"], no_progress_streak=0))
    if out.verb == "request_ci_rerun":
        # Beside the row: the CI run and attempt the classified Result
        # answered (the evidence the row clears from `last_ci_evidence_id`)
        # is kept with the rerun counts of the head. While no trusted rerun
        # exists, `development.waiting_ci` acts only on CI evidence of
        # another run or attempt.
        new = engine.record_beside(new, "answered_ci_run", now=now, budget=dict(
            new["budget"], reruns=dict(new["budget"]["reruns"],
                                       answered_ci_evidence_id=rec.get("last_ci_evidence_id"))))
    if held and out.verb not in ("schedule_publish_retry", "exhaust_publish_recovery"):
        # The held Result is answered: the publication is no longer pending.
        new = engine.record_beside(new, "publication_answered", now=now,
                                   pending_publication=None)
    schemas.validate("contribution", new)
    staged.extend([("contributions", new), ("events", out.event)])
    return new, staged


# The lists of a tick report that name what the tick asks the workflow to launch.
_EFFECT_LISTS = ("requested_effects", "dispatch_relaunch", "provision_requests",
                 "dev_pr_requests", "publication_retries", "handoff_requests")


def _touched(out: TickOutput, contribution_id: str) -> list[str]:
    """What ``out`` stages or requests for one contribution."""
    found = [f"staged {kind}" for kind, record in out.staged
             if record.get("contribution_id") == contribution_id]
    requests = [(name, item) for name in _EFFECT_LISTS for item in out.report.get(name) or []]
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
            check_staged_operations(out.staged, snap.operations)
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


def check_staged_operations(staged: list[tuple[str, dict]], committed: dict[str, dict]) -> None:
    """Refuse, as a processing error of its contribution, an operation the
    tick stages over a committed one along no sequence of rows of the
    operation table (``recovery.transition_problem``), before the state
    publisher refuses the whole commit for it. The publisher keeps the same
    check as the final guard."""
    for kind, op in staged:
        if kind != "operations":
            continue
        problem = recovery.transition_problem(committed.get(op["operation_id"]), op)
        if problem:
            raise isolation.ProcessingError(
                op["contribution_id"], "operation_check",
                recovery.RecoveryError(f"{op['operation_id']}: {problem}"))


PROCESSING_ERROR_VERB = "pause_on_processing_error"


def pause_on_processing_error(record: dict, description: dict, *, policy: dict,
                              now: str) -> engine.Outcome:
    """Take the table's ``pause_on_processing_error`` on the committed record.

    It sets ``paused`` with the error as ``paused_reason``, ``paused_by``
    ``controller`` and ``paused_at``, and clears the requested Work and the
    pending publication; from an ``agent_running`` state
    it leads to the state in which the Work is due. The outcome is not
    accepted, and nothing changes, when the
    contribution is already paused or terminal.
    """
    reason = isolation.pause_reason(description)
    details = {k: description[k] for k in ("step", "error_class", "message", "run_id")}
    return engine.apply_transition(record, PROCESSING_ERROR_VERB, policy=policy, now=now,
                                   cause_id=description["run_id"], facts={"processing_error"},
                                   values={"paused_reason": reason}, details=details)


def fold_handoff_receipts(api: collect.Api, receipts: list[dict], contributions: dict[str, dict],
                          by_cid: dict[str, str], snap: Snapshot, out: TickOutput, *,
                          policy: dict, now: str, skip: frozenset[str]) -> list[dict]:
    """Fold the verified receipts of the handoff operations.

    A receipt is folded only for the open operation the record runs in the
    state of its kind. A confirmed receipt is read back independently
    (`riscv64` at the reviewed head, tree, and series, or the App-authored
    package comment); the operation then takes `begin_effect`,
    `record_response`, and `confirm_by_discovery` and fires the confirm row.
    A failed receipt takes `record_failure` and fires the retry row within the
    budget of the state, the exhaustion row beyond it, or, for a build of
    `riscv64` whose reviewed series does not rebuild or whose lease
    conflicts, `return_for_repair`, and for one that found an upstream PR open
    from `riscv64`, `require_branch_intervention`. A receipt of an attempt
    the workflow refused before any effect is not folded."""
    report = []
    for receipt in receipts:
        cid = receipt.get("contribution_id")
        if cid not in by_cid or cid in skip:
            continue
        with isolation.contribution(cid, "handoff_receipt"):
            key = by_cid[cid]
            c = contributions[key]
            op = snap.operations.get(receipt.get("operation_id"))
            if not op or handoff.receipt_problem(c, op, receipt):
                continue
            kind, result = op["kind"], receipt.get("result")
            run = {"handoff_run_id": str(receipt.get("run_id") or "")}
            entry = {"contribution": cid, "operation": op["operation_id"], "kind": kind}
            if result == "applied":
                checked = handoff.readback(api, c, op, receipt, snap.evidence)
                if isinstance(checked, str):
                    report.append(dict(entry, outcome="readback mismatch", detail=checked))
                    continue
                external_ids, values, detail = checked
                new_op = recovery.fold_receipt(op, external_ids=external_ids, detail=detail, now=now)
                details = {"attempt": int(handoff.reason_value(op, "attempt") or 0)}
                if kind == "upstream_branch":
                    replaced = receipt.get("replaced")
                    details.update(head=values["clean_head_sha"],
                                   written=bool(receipt.get("written")),
                                   replaced=replaced if isinstance(replaced, str)
                                   and len(replaced) == 40 else None)
                else:
                    values = {"handoff_request": {"command_id": op["operation_id"],
                                                  "head": c["clean_head_sha"],
                                                  "revision": c["revision"]}}
                    details.update(package_url=external_ids["comment_url"],
                                   head=c["clean_head_sha"])
                fired = recovery.fire(c, new_op, policy=policy, now=now,
                                      facts={handoff.CONFIRM_FACTS[kind]}, values=values,
                                      counts={}, details=details)
            elif result == "failed":
                new_op, fired = handoff.fail_attempt(
                    c, op, policy=policy, now=now, failure=str(receipt.get("failure") or "unknown"),
                    reason=str(receipt.get("reason") or ""), external_ids=run)
            else:
                report.append(dict(entry, outcome=f"not folded: {str(result)[:40]}",
                                   detail=isolation.clean_text(receipt.get("reason") or "", 300)))
                continue
            if fired is None or not fired.accepted:
                raise lifecycle.TransitionError(
                    f"{kind} receipt fires no row in {engine.state_key(c)}: "
                    f"{fired.reason if fired else 'none bound'}")
            contributions[key] = fired.record
            snap.operations[op["operation_id"]] = new_op
            out.staged.extend([("operations", new_op), ("contributions", fired.record),
                               ("events", fired.event)])
            report.append(dict(entry, outcome=new_op["status"], verb=fired.verb))
    return report


# The phases in which a record holds the fork it develops on.
_FORK_PHASES = ("development", "handoff", "upstream_review", "release_wait")


def development_branch_problem(record: dict) -> str | None:
    """Why a record names a development branch other than `riscv64-dev`, or
    ``None``."""
    fork = record.get("fork")
    if record.get("phase") not in _FORK_PHASES or not isinstance(fork, dict):
        return None
    if fork.get("head_ref") != DEV_REF:
        return (f"the record names the development branch {str(fork.get('head_ref'))[:60]!r}, "
                f"not {DEV_REF!r}; no row of the table handles it")
    return None


def _tick(api: collect.Api, snap: Snapshot, *, policy: dict, now: datetime,
         publications: list[dict], pressure: capacity.Pressure,
         issues: list[dict] | None = None, comments: list[dict] | None = None,
         acceptances: dict[int, int] | None = None,
         observe: bool = True, dispatcher_runs: list[dict] | None = None,
         queue_works: dict[str, str] | None = None, tracking_issues: dict | None = None,
         upstream_snapshots: dict | None = None, provisions: list[dict] | None = None,
         development_prs: list[dict] | None = None, handoffs: list[dict] | None = None,
         skip: frozenset[str] = frozenset(), pypi=None) -> TickOutput:
    """One attempt at the tick. ``skip`` names the contributions it does not
    process. Every step that works on one contribution runs in an
    ``isolation.contribution`` block; an exception that leaves such a block
    reaches ``tick`` attributed to that contribution. ``pypi`` reads PyPI
    for the confirmation of a decision close (``collect.pypi_project`` by
    default)."""
    out = TickOutput()
    iso = now.isoformat()
    contributions = dict(snap.contributions)
    by_cid = {c["contribution_id"]: k for k, c in contributions.items()}

    # Development happens on `riscv64-dev`. A record that names another
    # development branch is a shape no row of the table handles: it is an
    # unexpected processing error of that contribution, never acted on.
    for c in contributions.values():
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "record_shape"):
            problem = development_branch_problem(c)
            if problem:
                raise lifecycle.TransitionError(problem)

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
                    or (pr.get("head") or {}).get("ref") != DEV_REF \
                    or (pr.get("head") or {}).get("repo", {}).get("id") != op["target"]["repository_id"] \
                    or (pr.get("base") or {}).get("ref") != c["upstream"]["base_ref"]:
                dev_pr_report.append({"contribution": cid, "status": "PR readback mismatch"})
                continue
            new_op = recovery.fold_receipt(
                op, external_ids={"pr_id": int(pr["id"]), "pr_number": int(pr["number"]),
                                  "development_pr_run_id": str(receipt["development_pr_run_id"])},
                detail=f"development PR {pr['number']} verified", now=iso)
            # No row of the table is fired by the development PR operation:
            # the PR number is recorded and the closed operation released,
            # and the state is kept.
            new = engine.record_beside(c, "development_pr_recorded", now=iso,
                                       fork=dict(c["fork"], pr_number=int(pr["number"])),
                                       active_operation_id=None)
            contributions[key] = new
            snap.operations[op["operation_id"]] = new_op
            out.staged.extend([("operations", new_op), ("contributions", new),
                               ("events", lifecycle._event(c, new, "development_pr_created", "observation",
                                                           op["operation_id"], iso, False))])
            dev_pr_report.append({"contribution": cid, "status": "confirmed", "pr_number": pr["number"]})
    out.report["development_prs"] = dev_pr_report

    # Verified receipts of the handoff operations, each read back
    # independently before its operation fires a row of the table.
    out.report["handoffs"] = fold_handoff_receipts(
        api, handoffs or [], contributions, by_cid, snap, out, policy=policy, now=iso, skip=skip)

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
                    or c["revision"] != receipt.get("revision") or not isinstance(receipt.get("status"), str):
                continue
            external_ids = {}
            if receipt.get("provision_run_id"):
                external_ids["provision_run_id"] = str(receipt["provision_run_id"])
            if receipt.get("provision_artifact_id"):
                external_ids["provision_artifact_id"] = int(receipt["provision_artifact_id"])
            if receipt["status"] != "provisioned":
                # The trusted workflow reports a definite failure: the
                # operation takes `record_failure` and, from
                # `provisioning.idle`, fires `require_provisioning_intervention`;
                # in the provisioning wait the state is kept.
                fired = None
                if op["status"] not in ("planned", "applying"):
                    # Only a launched operation fails by a receipt.
                    provision_report.append({"contribution": cid, "status": "failure not folded"})
                    continue
                new_op = recovery.fold_failure_receipt(op, external_ids=external_ids, now=iso)
                if recovery.fired_verbs(c, new_op):
                    facts, values = controller.RESOLUTION_FIRES[(new_op["kind"], new_op["status"])]
                    fired = recovery.fire(c, new_op, policy=policy, now=iso, facts=facts,
                                          values=values,
                                          details={"receipt_status": receipt["status"][:200]})
                    if not fired.accepted:
                        raise lifecycle.TransitionError(
                            f"require_provisioning_intervention refused in {engine.state_key(c)}: "
                            f"{fired.reason}")
                    new = fired.record
                else:
                    new = recovery.release(c, new_op, iso)
                schemas.validate("contribution", new)
                contributions[key] = new
                snap.operations[op["operation_id"]] = new_op
                out.staged.extend([("operations", new_op), ("contributions", new)])
                if fired is not None:
                    out.staged.append(("events", fired.event))
                provision_report.append({"contribution": cid, "status": "failed"})
                continue
            if receipt.get("fork") != op["target"]["repository"] \
                    or receipt.get("upstream_id") != c["upstream"]["repository_id"] \
                    or receipt.get("base_sha") != c["upstream"]["base_sha"] \
                    or receipt.get("actions_read_only") is not True:
                provision_report.append({"contribution": cid, "status": "receipt mismatch"})
                continue
            repo = collect.repository(api, receipt["fork"])
            head = collect.branch_head(api, receipt["fork"], DEV_REF)
            expected = op["target"].get("expected_head") or op["target"]["base_sha"]
            if not head or head["head"] != receipt.get("head_sha") or head["head"] != expected \
                    or repo["id"] != receipt.get("fork_id") or repo.get("parent_id") != c["upstream"]["repository_id"]:
                provision_report.append({"contribution": cid, "status": "fork identity/ref mismatch"})
                continue
            new_op = recovery.fold_receipt(
                op, external_ids=external_ids,
                detail="fork/source identity, branch and trusted setup receipt verified", now=iso)
            fork = {"repository_id": repo["id"], "name": repo["full_name"], "head_ref": DEV_REF,
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
            else:
                # The provisioning wait has no row for the operation: the
                # state is kept and the closed operation is released.
                new, event = recovery.release(c, new_op, iso), None
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
            rec, staged = apply_publication(rec, pub, now=iso, policy=policy,
                                            worker_run_id=pub.get("worker_run_id"),
                                            evidence=snap.evidence, pypi=pypi)
            contributions[key] = rec
            out.staged.extend(staged)
            for kind, r in staged:
                if kind == "operations":
                    snap.operations[r["operation_id"]] = r
            pubs_report.append({"contribution": cid, "status": pub.get("status"), "reason": pub.get("reason")})
    out.report["publications"] = pubs_report

    # 1b. Requested Work whose graph is terminal without a usable Result is
    # recovered by the table's rows in dispatch recovery; no deadline applies.
    # 1c. Requested tasks whose dispatch never reached the queue: relaunch the
    # dispatcher (bounded), then wait for a human.
    relaunch, staged, rep = dispatch_recovery(contributions, runs=dispatcher_runs, queue_works=queue_works,
                                              now=now, policy=policy, skip=skip)
    out.staged.extend(staged)
    out.report["dispatch_recovery"] = rep
    out.report["dispatch_relaunch"] = relaunch

    # 2. Live branch and CI observation.
    if observe:
        contracts = {e["evidence_id"]: e for e in snap.evidence.values() if e.get("kind") == "target_contract"}
        obs = observe_ci.observe(api, contributions=contributions, operations=snap.operations,
                                 contracts=contracts, policy=policy, now=iso,
                                 evidence=snap.evidence, skip=skip, audited=snap.audited)
        for kind, r in obs.staged:
            if kind == "contributions":
                contributions[by_cid[r["contribution_id"]]] = r
        out.staged.extend(obs.staged)
        out.report["observation"] = obs.report
        # Recovery is evaluated before live CI. A record that no longer holds
        # its Work once observed (CI accepted the head, or the head moved) is
        # not launched again; the submission rewrite and review Work of the
        # handoff phase are relaunched like development Work.
        out.report["dispatch_relaunch"] = [
            item for item in out.report["dispatch_relaunch"]
            if _holds_dispatched_work(contributions[by_cid[item["contribution_id"]]])]

    # 3. Commands, intake and work selection (controller). The failing CI job
    # of a head is decided from the evidence the record names, including the
    # evidence this tick recorded.
    evidence = dict(snap.evidence)
    evidence.update({r["evidence_id"]: r for kind, r in out.staged if kind == "evidence"})
    handoff_observations = {item["contribution"]: item["handoff"]
                            for item in out.report.get("observation") or []
                            if isinstance(item.get("handoff"), dict)}
    upstream_pr_observations = {item["contribution"]: item["upstream_pr"]
                                for item in out.report.get("observation") or []
                                if isinstance(item.get("upstream_pr"), dict)}
    # Work that returned a Result in this tick, or whose graph the queue
    # holds as terminal, is never allocated again under the same id.
    spent_graphs = frozenset(
        [work.graph_id(key) for key in ((pub.get("task") or {}).get("dedupe_key")
                                        for pub in publications) if isinstance(key, str)]
        + [graph for graph, state in (queue_works or {}).items()
           if state in ("completed", "cancelled")])
    res = controller.tick(contributions=contributions, operations=snap.operations, consumed=snap.consumed,
                          projects=snap.projects, policy=policy,
                          inputs=controller.Inputs(now=now, issues=issues or [], comments=comments or [],
                                                   pressure=pressure,
                                                   handoff_observations=handoff_observations,
                                                   upstream_pr_observations=upstream_pr_observations,
                                                   tracking_issues=tracking_issues or {},
                                                   upstream_snapshots=upstream_snapshots or {},
                                                   acceptances=acceptances or {},
                                                   spent_graphs=spent_graphs,
                                                   resolution_evidence={
                                                       e["evidence_id"]: e for e in evidence.values()
                                                       if e.get("kind") == "resolution"}),
                          skip=skip, evidence=evidence)
    out.staged.extend(res.staged)
    out.report["controller"] = res.report
    out.report["requested_effects"] = res.requested_effects
    out.report["provision_requests"] = res.report["provision_requests"]
    out.report["dev_pr_requests"] = res.report["dev_pr_requests"]
    out.report["handoff_requests"] = res.report["handoff_requests"]
    # 4. Publish-only retries due now (infrastructure failures; no model call).
    retries = []
    for key, c in list(contributions.items()):
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "publication_retry"):
            pp = c.get("pending_publication")
            if pp and not pp.get("exhausted") and not engine.stopped(c) \
                    and engine.state_key(c) in WORK_RESULT_STATES \
                    and datetime.fromisoformat(pp["next_attempt_at"]) <= now:
                # `schedule_publish_retry` held the Result and charged
                # `publish_recovery` when its publication failed; the retry
                # is launched once it is due, and the launch time is
                # recorded. The retry's own Result is folded by the table's
                # rows. A launched retry whose Result is not folded within
                # PUBLISH_RESULT_WAIT_MINUTES is a failed publication, like
                # a Result that reports one: `schedule_publish_retry` charges
                # `publish_recovery` again, or `exhaust_publish_recovery`
                # waits for a human once it is spent.
                if pp.get("launched_at"):
                    fired = _fail_publication(
                        c, pp["worker_run_id"],
                        f"the publish-only retry launched at {pp['launched_at']} returned no "
                        f"Result within {PUBLISH_RESULT_WAIT_MINUTES} minutes",
                        now=now.isoformat(), policy=policy, cause=pp["worker_run_id"],
                        details={"result_status": "no_result", "launched_at": pp["launched_at"]})
                    if not fired.accepted:
                        raise lifecycle.TransitionError(
                            f"no row records the lost publish-only retry: {fired.reason}")
                    schemas.validate("contribution", fired.record)
                    contributions[key] = fired.record
                    out.staged.extend([("contributions", fired.record), ("events", fired.event)])
                    continue
                attempt = pp["attempts"] + 1
                changed = engine.record_beside(c, "publish_retry_launched", now=now.isoformat(),
                                               pending_publication=dict(
                    pp, launched_at=now.isoformat(),
                    next_attempt_at=(now + timedelta(
                        minutes=PUBLISH_RESULT_WAIT_MINUTES)).isoformat()))
                contributions[key] = changed
                out.staged.append(("contributions", changed))
                retries.append({"type": "retry_publication", "contribution_id": c["contribution_id"],
                                "worker_run_id": pp["worker_run_id"], "attempt": attempt})
    out.report["publication_retries"] = retries
    out.staged = observe_ci.dedupe(out.staged)
    return out


def load_snapshot(root: Path, policy: dict):
    """Load validated lifecycle state relative to an explicit repository root."""
    import state

    store = state.StateStore(root, policy)
    contributions = dict(store.load_all("contributions"))
    return store, Snapshot(
        contributions=contributions,
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
        audited=audited_observations(store, contributions),
    )


# The states whose observation records each distinct observation once, and
# the verb that records it.
AUDITED_OBSERVATIONS = {"handoff.waiting_upstream_pr": "record_audit_observation",
                        "release_wait.idle": "record_release_incomplete"}


def audited_observations(store, contributions: dict[str, dict]) -> dict[str, set[str]]:
    """The observation keys already recorded, by contribution: from the
    `record_audit_observation` events of the contributions in the locked
    upstream-PR wait, and the `record_release_incomplete` events of those in
    `release_wait.idle`. No event is read when none is in such a state."""
    waiting = {record["contribution_id"]: AUDITED_OBSERVATIONS[engine.state_key(record)]
               for record in contributions.values()
               if engine.state_key(record) in AUDITED_OBSERVATIONS}
    audited: dict[str, set[str]] = {}
    if not waiting:
        return audited
    for event in store.load_all("events").values():
        key = (event.get("details") or {}).get("observation_key")
        if key and event.get("kind") == waiting.get(event.get("contribution_id")):
            audited.setdefault(event["contribution_id"], set()).add(key)
    return audited


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
        elif kind == "handoff":
            receipt["handoff_artifact_id"] = (
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
    handoffs = _receipt_outcomes(getattr(args, "handoffs", None), kind="handoff")
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
            handoffs=handoffs,
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
    parser.add_argument("--handoffs")
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
