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
  an actionable planning, failure, or feedback item.
* A tick never waits for workers or builds.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import capacity
import ci_rerun
import collect
import commands
import engine
import handoff
import intake
import isolation
import lifecycle
import patch_review
import recovery
import schemas
import state
import work as work_queue
from commands import URGENT, Command
from common import (
    CONTROL_REPO,
    DEV_REF,
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
    # What the observation of the locked upstream-PR wait found this tick, by
    # contribution: the reminder states a mismatch.
    handoff_observations: dict[str, dict] = field(default_factory=dict)
    # The upstream PR as the observation of this tick read it, by contribution
    # (``observe_ci.reads_upstream_pr_state``): whether it is open decides the
    # resume and the restart that depend on it.
    upstream_pr_observations: dict[str, dict] = field(default_factory=dict)
    acceptances: dict[int, int] = field(default_factory=dict)     # issue id -> accepting actor id
    requests_today: dict[int, int] = field(default_factory=dict)  # actor id -> count
    # The queue graphs (``work.graph_id``) of Work that already returned a
    # Result or that the queue holds as terminal. A terminal graph is never
    # submitted again: Work allocated under one takes `allocate_work_retry`
    # or `exhaust_recovery` in the same tick.
    spent_graphs: frozenset[str] = frozenset()
    # The handoff workflow runs that are not completed, by operation (run id);
    # None when the run list was not read this tick.
    handoff_runs: dict[str, int] | None = None
    # The development PR workflow runs that are not completed, by operation
    # (run id); None when the run list was not read this tick.
    development_pr_runs: dict[str, int] | None = None
    # The head of the development branch the observation of this tick read,
    # by contribution, in every state it reads the branch (a human wait
    # too). Development Work is not requested on a recorded head this read
    # contradicts (``_unrecorded_head``).
    observed_dev_heads: dict[str, str] = field(default_factory=dict)


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
    if engine.state_key(c) == "handoff.review_due":
        return work_queue.SUBMISSION_REWRITE
    if engine.state_key(c) == "handoff.review_pending":
        return work_queue.SUBMISSION_REVIEW
    if engine.state_key(c) == "development.review_pending":
        return work_queue.DEVELOPMENT_REVIEW
    if c["phase"] == "development" and c["activity"] == "fix_needed":
        return "development_repair"
    if engine.state_key(c) == "planning.idle":
        return "planning"
    return None


# The deadline recorded with requested Work (`active_work.deadline_at`): the
# queue's attempts at it (the `retry` attempts of the pool's Policy, 3), each
# a worker run of up to 350 minutes (the agent job bound of `worker.md`), so
# 18 hours covers the three with time to spare. No row reads
# it: Work whose graph ends without a usable Result is recovered by
# `allocate_work_retry` and `exhaust_recovery`.
WORK_DEADLINE_HOURS = 18

PRIORITY = {"upstream_revision": 0, "submission_rewrite": 0, "submission_review": 0,
            "development_review": 0, "development_repair": 1,
            "development_patch": 2, "planning": 3}
WRITE_KINDS = {"upstream_revision", "development_repair", "development_patch"}

# The rows of the table that allocate due Work, by the state it is due in, in
# the order they are tried; their guards are exclusive. A request in
# `queued.idle` takes `begin_planning` in a tick of its own, before any of
# these rows is taken from `planning.idle` in a later tick.
ALLOCATIONS = {
    "planning.idle": ("dispatch_planning",),
    "development.fix_needed": ("request_repair", "request_patch"),
    "development.review_pending": ("request_patch_review",),
    "handoff.review_due": ("request_submission_rewrite",),
    "handoff.review_pending": ("request_submission_review",),
    "upstream_review.fix_needed": ("request_upstream_revision", "request_review_revision"),
}
# The rows taken instead when a budget of that Work is exhausted. They need no
# worker slot and are taken as soon as their guard holds.
EXHAUSTIONS = {
    "planning.idle": ("exhaust_work_total",),
    "development.fix_needed": ("exhaust_work_total", "refresh_on_exhaustion",
                               "exhaust_repair_attempts"),
    "development.review_pending": ("exhaust_work_total",),
    "handoff.review_due": ("exhaust_work_total",),
    "handoff.review_pending": ("exhaust_work_total",),
    "upstream_review.fix_needed": ("exhaust_repair_attempts",),
}
# The facts the controller establishes for an allocation it is about to take.
ALLOCATION_FACTS = frozenset({"worker_slot_free", "capacity_available"})


def task_for(verb: str, record: dict) -> str:
    """The task an allocation row asks a worker for. ``request_patch`` asks
    for a repair when the record holds pending feedback for it to answer."""
    if verb == "dispatch_planning":
        return "planning"
    if verb == "request_submission_rewrite":
        return work_queue.SUBMISSION_REWRITE
    if verb == "request_submission_review":
        return work_queue.SUBMISSION_REVIEW
    if verb == "request_patch_review":
        return work_queue.DEVELOPMENT_REVIEW
    if verb in ("request_upstream_revision", "request_review_revision"):
        return "upstream_revision"
    if verb == "request_repair" or record.get("pending_feedback_evidence_ids"):
        return "development_repair"
    return "development_patch"


SOURCE_UNVERIFIED = "source verification unavailable"


def _bind_source(c: dict, inputs: Inputs, by_id: dict[str, dict], res: TickResult,
                 source_blocked: dict[str, dict | None], policy: dict, now: str) -> None:
    """Bind the unbound upstream identity and base of ``c`` from this tick's
    verified snapshot of its upstream; a changed identity pauses it, and a
    source that cannot be verified blocks it for the tick. ``source_blocked``
    maps each blocked contribution to the snapshot that could not be read,
    or ``None``; a contribution is reported as deferred once, when it is
    blocked. A snapshot that was read starts ``source_reads`` again."""
    cid = c["contribution_id"]
    rep = res.report
    snapshot = inputs.upstream_snapshots.get(c["upstream"]["full_name"])
    if snapshot is None:
        return
    if snapshot.get("error"):
        source_blocked[cid] = snapshot
        rep["deferred"].append({"contribution": cid, "why": SOURCE_UNVERIFIED})
        return
    if _upstream_read_resume(c) == "renamed" and c["upstream"].get("repository_id") is None:
        # The wait a renamed repository caused, for a request whose id GitHub
        # did not give: whatever repository is now read under the recorded
        # name may be another one, so it is never bound without a human. A
        # resume ends the wait, and the request is bound once it is due.
        source_blocked[cid] = None
        rep["deferred"].append({"contribution": cid,
                                "why": "the renamed upstream repository has no recorded id; "
                                       "a resume continues"})
        return
    budget = c.get("budget") or {}
    if budget.get("source_unavailable") or any(k in budget for k in engine.SOURCE_READ_FIELDS):
        restarted = {k: v for k, v in budget.items() if k not in engine.SOURCE_READ_FIELDS}
        c = engine.record_beside(c, "upstream_read", now=now,
                                 budget=dict(restarted, source_unavailable=0))
        by_id[cid] = c
        res.stage("contributions", c)
    current = c["upstream"].get("repository_id")
    if current is not None and current != snapshot["repository_id"]:
        source_blocked[cid] = None
        # The table verb for a changed identity sets the paused flag
        # with the controller's reason, clears the requested Work and
        # what is held for publication, and keeps the state (from an
        # `agent_running` state, the state in which the Work is due).
        cause = f"upstream-identity-{snapshot['repository_id']}"
        out = engine.apply_transition(
            c, "pause_on_identity_change", policy=policy, now=now, cause_id=cause,
            facts={"upstream_identity_changed"})
        if out.accepted:
            by_id[cid] = out.record
            res.stage("contributions", out.record)
            res.stage("events", out.event)
        return
    if c["upstream"].get("base_sha") is None \
            and c["upstream"].get("base_ref") not in (None, snapshot["base_ref"]):
        # The base is bound from a snapshot of the branch the record names
        # (the target branch the request asked for) only; a snapshot read
        # at another branch binds nothing, and a later tick reads it again.
        source_blocked[cid] = None
        rep["deferred"].append({"contribution": cid,
                                "why": "upstream snapshot is not of the requested base branch"})
        return
    updated = dict(c["upstream"])
    for key in ("repository_id", "base_ref", "base_sha"):
        if updated.get(key) is None:
            updated[key] = snapshot[key]
    if updated != c["upstream"]:
        changed = engine.record_beside(c, "upstream_identity_bound", now=now,
                                       upstream=updated)
        by_id[cid] = changed
        res.stage("contributions", changed)


# The states in which the record follows a base branch the upstream renamed
# while the upstream PR is open, each with the row it takes.
RENAMED_BASE_ROWS = {"upstream_review.waiting_ci": "follow_renamed_base",
                     "upstream_review.fix_needed": "follow_renamed_base",
                     "upstream_review.waiting_human": "resume_renamed_base"}


def _follow_renamed_base(c: dict, snapshot: dict, by_id: dict[str, dict], operations: dict,
                         res: TickResult, policy: dict, now: str) -> bool:
    """Follow a base branch the upstream renamed while the upstream PR is
    open: the collector verified that the PR the record names is open, that
    it targets the new name, and that the new name contains the recorded base
    commit (``snapshot["pr_base"]``). ``True`` once a row was taken."""
    verb = RENAMED_BASE_ROWS.get(engine.state_key(c))
    pr_base = snapshot.get("pr_base") or {}
    upstream, pr = c["upstream"], c.get("upstream_pr") or {}
    if verb is None or not pr_base or snapshot.get("base_ref") != upstream.get("base_ref") \
            or pr_base.get("pr_id") != pr.get("id") \
            or pr_base.get("base_sha") != upstream.get("base_sha") \
            or pr_base.get("base_ref") != snapshot.get("renamed_to"):
        return False
    details = {"base_ref": upstream["base_ref"], "renamed_to": pr_base["base_ref"],
               "pr_number": pr.get("number"), "base_sha": upstream["base_sha"]}
    out = engine.apply_transition(
        c, verb, policy=policy, now=now, facts={"base_renamed_in_pr"},
        open_operations=_open_operations(c, operations),
        values={"upstream.base_ref": pr_base["base_ref"],
                "upstream_pr.base_ref": pr_base["base_ref"]},
        cause_id=f"base-renamed-{pr_base['base_ref'][:100]}", details=details)
    if not out.accepted:
        return False
    by_id[c["contribution_id"]] = out.record
    res.stage("contributions", out.record)
    res.stage("events", out.event)
    res.report.setdefault("base_branch_renamed", []).append(
        {"contribution": c["contribution_id"], **details})
    return True


def _upstream_read_resume(c: dict) -> str | None:
    """What caused the wait of ``c`` that ``resume_upstream_read`` ends once
    the upstream is read again: ``reads`` for a wait failed reads caused (the
    counter ``source_reads``), ``renamed`` for the wait a renamed repository
    caused (``upstream_unavailable`` with no counter), and ``None`` for any
    other wait."""
    pending = c.get("pending_human_action") or {}
    if engine.state_key(c) not in lifecycle.UPSTREAM_READ_RESUMES:
        return None
    if pending.get("counter") == "source_reads":
        return "reads"
    if pending.get("kind") == lifecycle.UPSTREAM_UNAVAILABLE and pending.get("counter") is None:
        return "renamed"
    return None


def _resume_upstream_read(c: dict, snapshot: dict, by_id: dict[str, dict], operations: dict,
                          res: TickResult, policy: dict, now: str) -> None:
    """End a wait that failed reads caused, or that a renamed repository
    caused, once this tick read the upstream again under its recorded name
    (``_upstream_read_resume``). A snapshot of another branch than the record
    names, or of another repository id, says nothing about it, and a paused
    or non-quiescent record keeps the wait."""
    cause = _upstream_read_resume(c)
    verb = "resume_upstream_read"
    if cause is None or snapshot.get("error") or snapshot.get("repository_id") is None \
            or snapshot.get("base_ref") != c["upstream"].get("base_ref") \
            or snapshot.get("repository_id") != c["upstream"].get("repository_id"):
        return
    ops = _open_operations(c, operations)
    if engine.refusal(c, verb, policy=policy, now=now,
                      facts={"upstream_read_again"}, open_operations=ops):
        return
    pending = c["pending_human_action"]
    details = {"base_ref": c["upstream"].get("base_ref"), "pending_human_action": pending["kind"],
               "repository": c["upstream"]["full_name"], "cause": cause}
    out = engine.apply_transition(c, verb, policy=policy, now=now,
                                  facts={"upstream_read_again"}, open_operations=ops,
                                  cause_id=f"upstream-read-r{c['revision']}", details=details)
    if not out.accepted:
        return
    by_id[c["contribution_id"]] = out.record
    res.stage("contributions", out.record)
    res.stage("events", out.event)
    res.report.setdefault("upstream_read_again", []).append(
        {"contribution": c["contribution_id"], **details})


def _same_repository_renamed(c: dict, snapshot: dict) -> bool:
    """Whether GitHub answered for the recorded repository under another
    name: the snapshot names the new name, and the repository id it read is
    the recorded one (or none is bound yet, as for a request in
    `queued.idle`)."""
    if not snapshot.get("renamed_repository") or "renamed_repository_id" not in snapshot:
        return False
    recorded = c["upstream"].get("repository_id")
    return recorded is None or recorded == snapshot["renamed_repository_id"]


def _replace_rename_wait(c: dict, snapshot: dict, by_id: dict[str, dict], operations: dict,
                         res: TickResult, policy: dict, now: str) -> None:
    """In the wait a renamed repository caused, once GitHub answers for the
    recorded repository id under its recorded name again but says that the
    base branch is missing or was renamed: ``require_base_branch`` replaces
    the wait with the wait of the base branch, with the entry it posts from
    the state the step was due in. A snapshot of another branch or of
    another repository id says nothing about it, and a paused or
    non-quiescent record keeps the wait."""
    recorded = c["upstream"].get("repository_id")
    if not snapshot.get("missing") or recorded is None \
            or snapshot.get("repository_id") != recorded \
            or snapshot.get("base_ref") != c["upstream"].get("base_ref"):
        return
    details: dict[str, Any] = {"base_ref": snapshot["base_ref"], "why": snapshot["error"]}
    if snapshot.get("renamed_to"):
        details["renamed_to"] = snapshot["renamed_to"]
    out = engine.apply_transition(c, "require_base_branch", policy=policy, now=now,
                                  facts={"base_branch_missing"},
                                  open_operations=_open_operations(c, operations),
                                  cause_id=f"source-r{c['revision']}", details=details)
    if not out.accepted:
        return
    by_id[c["contribution_id"]] = out.record
    res.stage("contributions", out.record)
    res.stage("events", out.event)
    res.report.setdefault("base_branch_unavailable", []).append(
        {"contribution": c["contribution_id"], "verb": "require_base_branch",
         **{k: v for k, v in details.items() if k != "why"}})


def _defer(res: TickResult, cid: str, why: str) -> None:
    """Report ``cid`` as deferred for ``why``: the entry `_bind_source`
    already made for it in this tick is made more precise, so that a
    contribution is reported as deferred once."""
    for entry in res.report["deferred"]:
        if entry["contribution"] == cid and entry["why"] == SOURCE_UNVERIFIED:
            entry["why"] = why
            return
    res.report["deferred"].append({"contribution": cid, "why": why})


def _source_unavailable(c: dict, snapshot: dict, by_id: dict[str, dict], operations: dict,
                        res: TickResult, policy: dict, now: str) -> None:
    """Take the row for an upstream repository or base branch that could not
    be read in this tick, in a state of ``lifecycle.BASE_BRANCH_WAITS``. A
    request in `queued.idle` takes it from there; only a paused or
    non-quiescent contribution, or one in another state, stays deferred.

    A snapshot read at another branch than the record names says nothing
    about the record's branch: the collector reads one branch per upstream,
    and that snapshot never leads this record to a wait or a charge."""
    cid = c["contribution_id"]
    if _upstream_read_resume(c) == "renamed":
        _replace_rename_wait(c, snapshot, by_id, operations, res, policy, now)
        return
    if engine.state_key(c) not in lifecycle.BASE_BRANCH_WAITS:
        return
    if "ref" in snapshot and snapshot["ref"] != c["upstream"].get("base_ref"):
        _defer(res, cid, "upstream snapshot is not of the recorded base branch")
        return
    ops = _open_operations(c, operations)
    base_ref = snapshot.get("base_ref") or c["upstream"].get("base_ref")
    details: dict[str, Any] = {"base_ref": base_ref, "why": snapshot["error"]}
    values: dict[str, Any] = {}
    if snapshot.get("missing"):
        verb, facts = "require_base_branch", {"base_branch_missing"}
        if snapshot.get("renamed_to"):
            details["renamed_to"] = snapshot["renamed_to"]
    elif _same_repository_renamed(c, snapshot):
        # GitHub answers for the same repository id under another name: a
        # definitive answer, so the contribution waits at once, with no
        # counter, instead of spending the retry window on a read that
        # cannot change.
        verb = "require_renamed_upstream"
        facts = {"base_branch_unread", "upstream_repository_unread", "upstream_repository_renamed"}
        details.update({"repository": c["upstream"]["full_name"],
                        "renamed_repository": snapshot["renamed_repository"]})
        # The id GitHub answered for is recorded, so that the wait ends only
        # once the same repository is read under the recorded name again: a
        # request not bound yet is never bound to another repository that
        # took the name later.
        recorded = c["upstream"].get("repository_id")
        values["upstream.repository_id"] = recorded if recorded is not None \
            else snapshot.get("renamed_repository_id")
        if values["upstream.repository_id"] is not None:
            details["repository_id"] = values["upstream.repository_id"]
    else:
        current_repository = snapshot.get("scope") == "repository"
        budget = c.get("budget") or {}
        count = int(budget.get("source_unavailable", 0))
        limit = lifecycle.budget_limit("source_reads", policy)
        spent = engine.source_reads_spent(c, policy, now)
        counted = engine.source_read_counted(c, policy, now)
        if not counted and not spent:
            # Counted at most once per spacing slot: a read in the same slot
            # writes nothing, so a burst of ticks adds no events and no
            # commits. A read that exhausts the budget is never held back.
            _defer(res, cid, "upstream read failed; counted at most once per spacing")
            return
        # The counted failed reads of the run, this one included when it is
        # counted, and how many of them could not read the repository
        # itself: the exhaustion names the cause of most of the run, not of
        # the last read only.
        failures = count + (1 if counted else 0)
        repository_failures = int(budget.get("source_unavailable_repository", 0)) \
            + (1 if current_repository and counted else 0)
        if spent:
            repository = 2 * repository_failures > failures
            verb = "require_upstream_access" if repository else "exhaust_source_reads"
        else:
            repository = current_repository
            verb = "record_source_unavailable"
        facts = {"base_branch_unread"} | ({"upstream_repository_unread"} if repository else set())
        details.update({"count": failures, "repository_failures": repository_failures,
                        "limit": limit,
                        "since": budget.get("source_unavailable_since") or now,
                        "minutes": int(lifecycle.policy_value(
                            policy, lifecycle.SOURCE_READS_MINUTES_KEY))})
        if repository or current_repository:
            details["repository"] = c["upstream"]["full_name"]
            if snapshot.get("renamed_repository"):
                details["renamed_repository"] = snapshot["renamed_repository"]
    out = engine.apply_transition(c, verb, policy=policy, now=now, facts=facts, values=values,
                                  open_operations=ops, cause_id=f"source-r{c['revision']}",
                                  details=details)
    if not out.accepted:
        return
    by_id[cid] = out.record
    res.stage("contributions", out.record)
    res.stage("events", out.event)
    if verb != "record_source_unavailable":
        res.report.setdefault("base_branch_unavailable", []).append(
            {"contribution": cid, "verb": verb,
             **{k: v for k, v in details.items() if k != "why"}})


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
            "plan_evidence_id": None, "release_observation": None, "updated_at": now}
        proj = dict(proj, contribution_ids=proj["contribution_ids"] + [cid],
                    active_contribution_id=cid, updated_at=now)
        projects[project_id] = proj
        res.stage("projects", proj)

    # Removing the intake label withdraws only an accepted request that has
    # not requested any Work yet, in `queued.idle` or while it waits for a
    # worker slot in `planning.idle` (`withdraw_request`, guarded by no Work,
    # no plan, and no fork). Once Work exists, explicit lifecycle commands are
    # required so an unlabeled event cannot silently stop work.
    for cid, c in list(by_id.items()):
        if cid in skip:
            continue
        with isolation.contribution(cid, "intake_label"):
            issue = inputs.tracking_issues.get(c["tracking_issue"]["number"])
            labels = {label.get("name") if isinstance(label, dict) else label
                      for label in (issue or {}).get("labels", [])}
            if issue is None or INTAKE_LABEL in labels \
                    or engine.state_key(c) not in ("queued.idle", "planning.idle"):
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
    # never from the model's plan. Already-bound bases remain immutable. A
    # parked upstream is not read here: it is bound again below, in the same
    # tick, once a restart left `parked` and cleared it.
    source_blocked: dict[str, dict | None] = {}
    for cid, c in list(by_id.items()):
        if cid in skip or c["phase"] == "parked":
            continue
        with isolation.contribution(cid, "source_identity"):
            _bind_source(c, inputs, by_id, res, source_blocked, policy, now)

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
                receipt = _apply(d.command, receipt, by_id, operations, inputs, res, now, policy,
                                 evidence or {})
        elif d.accepted:
            receipt = _apply(d.command, receipt, by_id, operations, inputs, res, now, policy,
                             evidence or {})
        consumed[receipt["comment_id"]] = receipt
        res.stage("commands", receipt)
        rep["commands"].append({"comment": receipt["comment_id"], "result": receipt["result"],
                                "reason": receipt.get("reason")})

    # A restart from `parked` (`restart_generation`) cleared the upstream
    # identity and base: they are bound in this tick, before any Work is
    # selected, from the snapshot read for the refresh. Every row that
    # allocates Work requires them; without a snapshot the record waits and
    # a later tick binds them.
    for cid, c in list(by_id.items()):
        if cid in skip or cid in source_blocked or c["phase"] in lifecycle.TERMINAL | {"parked"} \
                or all(c["upstream"].get(k) is not None
                       for k in ("repository_id", "base_ref", "base_sha")):
            continue
        with isolation.contribution(cid, "source_identity"):
            _bind_source(c, inputs, by_id, res, source_blocked, policy, now)

    # An upstream base branch that could not be read. In upstream review, a
    # branch renamed while the upstream PR is open is followed
    # (`follow_renamed_base`, `resume_renamed_base`). Where Work or the fork
    # provisioning is due otherwise: `require_base_branch` once the upstream
    # answered that it is missing, `require_renamed_upstream` at once when
    # GitHub names the same repository otherwise, `exhaust_source_reads` (or
    # `require_upstream_access` when most counted reads could not read the
    # repository itself) once `source_reads` is exhausted, the charge of
    # `record_source_unavailable` otherwise, at most once per spacing.
    for cid, snapshot in list(source_blocked.items()):
        if cid in skip or snapshot is None:
            continue
        with isolation.contribution(cid, "source_unavailable"):
            if not _follow_renamed_base(by_id[cid], snapshot, by_id, operations, res, policy, now):
                _source_unavailable(by_id[cid], snapshot, by_id, operations, res, policy, now)

    # A wait that failed reads caused ends once the upstream is read again
    # (`resume_upstream_read`), and the wait a renamed repository caused
    # once GitHub answers for it under its recorded name again
    # (`resume_upstream_read` too), after the commands, so that a resume
    # posted in the same tick is taken as posted.
    for cid, c in list(by_id.items()):
        if cid in skip or cid in source_blocked:
            continue
        snapshot = inputs.upstream_snapshots.get(c["upstream"]["full_name"])
        if snapshot is None:
            continue
        with isolation.contribution(cid, "upstream_read_again"):
            _resume_upstream_read(c, snapshot, by_id, operations, res, policy, now)

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
                                          details=reminder_details(
                                              c, operations, inputs.handoff_observations.get(cid),
                                              policy))
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

    # The handoff operations. In a handoff state that runs one, the controller
    # records the intent of the next attempt when none is open, and launches
    # the planned attempt through the trusted workflow. No row of the table
    # launches it: its launches are counted like `operation_launch`, on the
    # operation, and it is not launched while the contribution is paused.
    res.report["handoff_requests"] = []
    for cid, c in list(by_id.items()):
        if cid in skip or engine.state_key(c) not in handoff.KINDS:
            continue
        with isolation.contribution(cid, "handoff_operation"):
            _launch_handoff(c, by_id, operations, res, inputs, now, policy, evidence or {})

    # Provisioning is a trusted administrative workflow, not a model task.
    # Persist its normal operation intent before dispatch, with no second queue.
    # In `provisioning.idle` the table's rows launch it: `start_operation`
    # within `operation_launch` (counted on the operation), and
    # `require_provisioning_intervention` once that budget is exhausted.
    res.report["provision_requests"] = []
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
                # Only `provisioning.idle` has a row that launches it; a wait
                # of the table is left only by its own rows.
                if due and _launch_provisioning(c, operation, by_id, operations, inputs, res, now,
                                                policy):
                    break
    for cid, c in list(by_id.items()):
        if res.report["provision_requests"]:
            break
        if cid in skip:
            continue
        with isolation.contribution(cid, "provisioning"):
            if engine.state_key(c) != "provisioning.idle" or c.get("paused") \
                    or recovery.is_open(operations.get(c.get("active_operation_id"))) \
                    or (c.get("active_operation_id") and c["active_operation_id"] not in operations) \
                    or cid in source_blocked:
                continue
            if not c.get("plan_evidence_id") or not c.get("admission_class") \
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
            op = None
            for n in range(MAX_REPLANS + 1):
                # A re-plan after a closed operation of the same intent at the
                # same revision (one a human resolved in `provisioning.idle`)
                # is a new operation: an operation id is never reused once it
                # closed.
                op = recovery.plan(contribution_id=cid, revision=c["revision"], kind="fork_provision",
                                   policy_version=policy["policy_version"],
                                   reason_ids=[c["plan_evidence_id"]] + ([f"replan:{n}"] if n else []),
                                   target={"repository": "riseproject-upstreaming/" + c["upstream"]["full_name"].split('/', 1)[1],
                                           "repository_id": fork.get("repository_id"), "ref": DEV_REF,
                                           "expected_head": fork.get("head_sha"), "base_sha": c["upstream"]["base_sha"]},
                                   proposed={"commit": c["upstream"]["base_sha"], "marker": f"provision:{cid}:{c['revision']}"},
                                   capacity={"selector": "ubuntu-24.04-riscv", "estimated_jobs": 1,
                                             "estimated_minutes": 10, "observed_run_ids": []}, now=now)
                prior = operations.get(op["operation_id"])
                if prior is None or recovery.is_open(prior):
                    break
            prior = operations.get(op["operation_id"])
            if prior is not None and not recovery.is_open(prior):
                # Every re-plan of this intent at this revision is spent: the
                # human wait, whose resume starts a new revision.
                _exhaust_provisioning_replans(c, prior, by_id, operations, res, now, policy)
                continue
            if prior is not None:
                rep["deferred"].append({"contribution": cid, "why": "existing provisioning operation requires reconciliation"})
                continue
            # The intent is recorded and launched in the same tick.
            planned = engine.record_beside(c, "operation_planned", now=now,
                                           active_operation_id=op["operation_id"])
            if not _launch_provisioning(planned, op, by_id, operations, inputs, res, now, policy,
                                        capacity_checked=True):
                raise lifecycle.TransitionError(
                    f"start_operation refused for a new provisioning intent of {cid}")

    # A published development head gets a same-fork development PR before CI
    # observation. Persist intent first; the trusted workflow creates/discovers
    # exactly riscv64-dev -> the frozen upstream base ref and returns a receipt.
    # In `development.waiting_ci` the table's rows launch it:
    # `launch_development_pr` within `operation_launch` (counted on the
    # operation), and `exhaust_development_pr_launches` once that budget is
    # exhausted. A planned operation that no longer matches the record, or of
    # a paused contribution, is closed as failed and released without a row.
    # The `ci_rerun` operation of `rerun_interrupted_ci` shares the loop: no
    # row launches it, and its launches are counted like `operation_launch`.
    res.report["dev_pr_requests"] = []
    for cid, c in list(by_id.items()):
        if cid in skip:
            continue
        with isolation.contribution(cid, "development_pr"):
            operation = operations.get(c.get("active_operation_id"))
            if not operation or operation.get("kind") not in DEVELOPMENT_PR_KINDS \
                    or operation.get("status") != "planned":
                continue
            attempts = recovery.launch_attempts(operation)
            waited = (inputs.now - datetime.fromisoformat(operation["updated_at"])).total_seconds() \
                >= LAUNCH_INTERVAL_SECONDS
            if operation["kind"] in DEV_PR_KINDS:
                why = _stale_development_pr(c, operation)
                if why:
                    _close_development_pr(c, operation, why, by_id, operations, res, now)
                    continue
                if (attempts == 0 or waited) and _launch_development_pr(
                        c, operation, by_id, operations, inputs, res, now, policy):
                    break
                continue
            why = _spent_rerun(c, operation, attempts, waited, policy, evidence or {})
            if why:
                # The rerun failed: it is closed and released, and no row
                # is fired. The next observation of interrupted CI takes
                # `rerun_interrupted_ci` again within `interrupted_reruns`,
                # or `exhaust_interrupted_reruns`.
                closed = recovery.fold_failure_receipt(operation, external_ids={}, now=now)
                operations[closed["operation_id"]] = closed
                released = recovery.release(c, closed, now)
                by_id[cid] = released
                res.stage("operations", closed)
                res.stage("contributions", released)
                res.report.setdefault("ci_rerun_closures", []).append(
                    {"operation_id": closed["operation_id"], "contribution_id": cid,
                     "closed": why})
                continue
            due = attempts == 0 or (
                waited and attempts < lifecycle.budget_limit("operation_launch", policy))
            if due and engine.state_key(c) == "handoff.waiting_ci":
                # CI on the clean head is rerun by the handoff workflow, the
                # workflow that writes `riscv64`; `controller.yml`
                # dispatches every handoff request.
                launched = recovery.charge_launch(operation, now)
                operations[launched["operation_id"]] = launched
                res.stage("operations", launched)
                res.report["handoff_requests"].append({
                    "operation_id": launched["operation_id"], "contribution_id": cid,
                    "kind": "ci_rerun", "attempt": attempts + 1})
                continue
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
        if res.report["dev_pr_requests"]:
            break
        if cid in skip:
            continue
        with isolation.contribution(cid, "development_pr"):
            fork = c.get("fork") or {}
            artifact = c.get("proposal_artifact") or {}
            pubop = operations.get(c.get("publication_operation_id"))
            if engine.state_key(c) != "development.waiting_ci" or c.get("paused") \
                    or c.get("active_operation_id") or fork.get("pr_number") is not None \
                    or not fork.get("repository_id") or not fork.get("head_sha") \
                    or fork.get("head_sha") == c["upstream"].get("base_sha") \
                    or artifact.get("outcome") != "published" or not artifact.get("result_commit") \
                    or not c.get("plan_evidence_id") or not c.get("scope_paths") \
                    or not pubop or pubop.get("status") != "confirmed":
                continue
            marker = f"<!-- upstreaming-development:{cid}:r{c['revision']}:{fork['head_sha']} -->"
            reasons = [c["plan_evidence_id"], str(artifact["result_commit"])]
            op = None
            for n in range(MAX_REPLANS + 1):
                # A re-plan after a closed operation of the same intent is a
                # new operation: an operation id is never reused once it
                # closed.
                op = recovery.plan(contribution_id=cid, revision=c["revision"], kind="dev_pr_create",
                                   policy_version=policy["policy_version"],
                                   reason_ids=reasons + ([f"replan:{n}"] if n else []),
                                   target={"repository": fork["name"],
                                           "repository_id": fork["repository_id"],
                                           "ref": DEV_REF, "expected_head": fork["head_sha"],
                                           "base_sha": c["upstream"]["base_sha"]},
                                   proposed={"marker": marker, "content_digest": artifact["digest"]},
                                   now=now)
                prior = operations.get(op["operation_id"])
                if prior is None or recovery.is_open(prior):
                    break
            if op["operation_id"] in operations:
                rep["deferred"].append({"contribution": cid,
                                        "why": "existing development PR operation requires "
                                               "reconciliation"})
                continue
            # The intent is recorded and launched in the same tick.
            planned = engine.record_beside(c, "operation_planned", now=now,
                                           active_operation_id=op["operation_id"])
            if not _launch_development_pr(planned, op, by_id, operations, inputs, res, now,
                                          policy):
                raise lifecycle.TransitionError(
                    f"launch_development_pr refused for a new development PR intent of {cid}")

    # Bootstrap CI once for an already recorded PR. This separate operation is
    # needed when GitHub registered no inherited workflows at PR creation time.
    for cid, c in list(by_id.items()):
        if len(res.report["dev_pr_requests"]) >= 1:
            break
        if cid in skip:
            continue
        with isolation.contribution(cid, "development_pr"):
            fork = c.get("fork") or {}
            artifact = c.get("proposal_artifact") or {}
            if engine.state_key(c) != "development.waiting_ci" or c.get("paused") \
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
                                       "ref": DEV_REF, "expected_head": fork["head_sha"],
                                       "base_sha": c["upstream"]["base_sha"]},
                               proposed={"marker": f"ci-bootstrap:{cid}:r{c['revision']}",
                                         "content_digest": artifact["digest"]}, now=now)
            if op["operation_id"] in operations:
                continue
            # The intent is recorded and launched in the same tick.
            planned = engine.record_beside(c, "operation_planned", now=now,
                                           active_operation_id=op["operation_id"])
            if not _launch_development_pr(planned, op, by_id, operations, inputs, res, now,
                                          policy):
                raise lifecycle.TransitionError(
                    f"launch_development_pr refused for a new CI bootstrap intent of {cid}")

    # 8-10. Select bounded work, honouring capacity and one worker. In a
    # state of the table, Work is allocated by the row the table names for
    # it, and an exhausted budget takes its exhaustion row instead.
    evidence = evidence or {}
    # Planning begins: a request in `queued.idle` that is not paused takes
    # `begin_planning` in a commit of its own, whatever the worker slot,
    # capacity, upstream binding or source read. Its Work is allocated from
    # `planning.idle` in a later tick.
    begun: set[str] = set()
    for cid, c in list(by_id.items()):
        if cid in skip or engine.state_key(c) != "queued.idle":
            continue
        with isolation.contribution(cid, "begin_planning"):
            out = engine.apply_transition(c, "begin_planning", policy=policy, now=now,
                                          cause_id=f"tick-r{c['revision']}")
            if out.accepted:
                by_id[cid] = out.record
                res.stage("contributions", out.record)
                res.stage("events", out.event)
                begun.add(cid)
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
            if c["contribution_id"] in begun:
                rep["deferred"].append({"contribution": c["contribution_id"],
                                        "why": "planning began in this tick; its Work is "
                                               "allocated in a later tick"})
                continue
            if c["contribution_id"] in blocked_by_op:
                rep["deferred"].append({"contribution": c["contribution_id"], "why": "uncertain operation"})
                continue
            if engine.state_key(c) not in lifecycle.LIFECYCLE:
                continue  # a state the table does not have requests no Work
            due = c
            moved = _unrecorded_head(due, inputs)
            if moved:
                rep["deferred"].append({"contribution": c["contribution_id"], "why": moved})
                continue
            ci = _recorded_ci_evidence(due, evidence)
            ops = _open_operations(due, operations)
            facts = ALLOCATION_FACTS
            if engine.state_key(due) == "handoff.review_pending":
                # The review reads the rewritten series from its artifact:
                # an expired one is never reviewed, a fresh rewrite is due.
                if handoff.rewrite_expired(due, evidence, inputs.now):
                    out = engine.apply_transition(
                        due, "expire_submission_rewrite", policy=policy, now=now,
                        facts={"submission_rewrite_expired"}, open_operations=ops,
                        cause_id=f"expired-{due.get('submission_rewrite_id')}")
                    if out.accepted:
                        by_id[c["contribution_id"]] = out.record
                        res.stage("contributions", out.record)
                        res.stage("events", out.event)
                        continue
                else:
                    facts = ALLOCATION_FACTS | {"submission_rewrite_current"}
            if engine.state_key(due) == "development.review_pending":
                # The review reads the held series from the artifact the
                # held patch pins: an expired one is never reviewed or
                # published, and neither is a held patch a stop discarded
                # during a wait the review's recovery caused; fresh
                # development Work is due.
                if not due.get("held_patch") or patch_review.held_patch_expired(due, inputs.now):
                    out = engine.apply_transition(
                        due, "expire_held_patch", policy=policy, now=now,
                        facts={"held_patch_expired"}, open_operations=ops,
                        cause_id=f"expired-{(due.get('held_patch') or {}).get('worker_run_id')}")
                    if out.accepted:
                        by_id[c["contribution_id"]] = out.record
                        res.stage("contributions", out.record)
                        res.stage("events", out.event)
                        continue
                else:
                    facts = ALLOCATION_FACTS | {"held_patch_current"}
            spent = next((verb for verb in EXHAUSTIONS.get(engine.state_key(due), ())
                          if engine.refusal(due, verb, policy=policy, now=now, ci_evidence=ci,
                                            open_operations=ops) is None), None)
            if spent:
                _exhaust(due, spent, ci, by_id, res, policy, now)
                continue
            verb, why = _allocation_verb(due, ci, ops, policy, now, facts)
            if verb is None:
                rep["deferred"].append({"contribution": c["contribution_id"], "why": why})
                continue
            kind = task_for(verb, due)
            candidates.append((PRIORITY[kind], c["updated_at"], kind, c, verb))
    candidates.sort(key=lambda x: (x[0], x[1]))
    # A Result held for a publish-only retry holds no worker.
    slots = policy["concurrency"]["coding_workers"] - sum(
        1 for c in by_id.values()
        if c.get("active_work")
        or (c["activity"] == "agent_running" and not c.get("pending_publication")))
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
            claimed = c
            ci = evidence.get(claimed.get("last_ci_evidence_id") or "")
            cluster = engine.recorded_failing_job(claimed, ci)[1]
            request = _work_request(claimed, kind, inputs, now)
            source = engine.state_key(claimed)
            claimed = _take(claimed, verb, res, policy, now, cause_id=request["dedupe_key"],
                            facts=ALLOCATION_FACTS | ({"submission_rewrite_current"}
                                                      if verb == "request_submission_review"
                                                      else {"held_patch_current"}
                                                      if verb == "request_patch_review"
                                                      else set()),
                            values={"active_work": request},
                            ci_evidence=ci,
                            open_operations=_open_operations(claimed, operations),
                            details={"task": kind, "work": request["dedupe_key"],
                                     **({"cluster": cluster} if cluster else {})})
            # Only the rows that request patch or revision Work carry the
            # pending feedback (`active_work.feedback_evidence_ids`); an
            # planning carries none and keeps it pending. The submission
            # rewrite and review Work name what they answer in their context.
            carried = claimed["active_work"].get("feedback_evidence_ids") or []
            expected = (list(c.get("pending_feedback_evidence_ids") or [])
                        if _carries_feedback(source, verb)
                        else [])
            if kind not in work_queue.SUBMISSION_TASKS and carried != expected:
                raise lifecycle.TransitionError(
                    f"{verb}: the Work carries {carried}, not {expected}")
            # A row that keeps the revision can name Work whose graph already
            # returned a Result or is terminal in the queue. That Work is
            # terminal without a usable Result: it is never submitted again,
            # and a fresh graph is allocated within `work_recovery`
            # (`allocate_work_retry`), or the contribution waits for a human
            # (`exhaust_recovery`).
            claimed = _retry_spent_work(claimed, inputs, res, policy)
            by_id[c["contribution_id"]] = claimed
            if not claimed.get("active_work"):
                slots += 1
                continue
            aw = claimed["active_work"]
            work = {
                "type": "submit_work",
                "dedupe_key": aw["dedupe_key"],
                "contribution_id": c["contribution_id"],
                "generation": aw["generation"],
                "revision": aw["revision"],
                "expected_head": lifecycle.recorded_head(claimed),
                "task": kind,
                "reason_ids": aw["reason_ids"],
                "feedback_evidence_ids": list(aw.get("feedback_evidence_ids") or []),
                "effect_contract": (
                    {"kind": "none"}
                    if kind == "planning" or kind in work_queue.SUBMISSION_TASKS
                    or kind == work_queue.DEVELOPMENT_REVIEW
                    else {
                        "kind": "fork_branch",
                        "repository": (claimed.get("fork") or {}).get("name"),
                        "ref": lifecycle.work_ref(claimed),
                    }
                ),
            }
            res.requested_effects.append(work)
            rep["selected"].append({"contribution": c["contribution_id"], "task": kind})

    if policy["read_only"]:
        rep["note"] = "read-only: requested effects are reported, not executed"
    return res


def terminal_work(c: dict, aw: dict, queue_state: str, *, now: datetime,
                  policy: dict, details: dict | None = None
                  ) -> tuple[dict | None, dict | None, str]:
    """The table's row for requested Work whose graph is terminal without a
    usable Result: the record, its event, and the decision for the report.
    ``details`` (why the graph has no usable Result: ``worker_run``,
    ``run_conclusion``, ``result``) are added to the event details. A state
    outside the table has no row: nothing is decided."""
    iso = now.isoformat()
    if engine.state_key(c) not in lifecycle.LIFECYCLE:
        return None, None, f"terminal Work in {engine.state_key(c)}, a state the table does not have"
    facts = {"work_terminal_without_result"}
    details = {"queue_state": queue_state, "work": aw["dedupe_key"]} | (details or {})
    generation, revision = int(c.get("generation", 0)), c["revision"] + 1
    fresh = dict(aw, dedupe_key=work_queue.work_key(c["contribution_id"], generation, revision,
                                                    aw["task"], engine.head_reruns(c)["flaky"]),
                 generation=generation, revision=revision, requested_at=iso,
                 deadline_at=(now + timedelta(hours=WORK_DEADLINE_HOURS)).isoformat(),
                 dispatched_run_id=None)
    # `allocate_work_retry` is guarded by the bound upstream identity, and a
    # refusal here would fall through to `exhaust_recovery`. A record
    # without it never reaches this point: it holds Work only once a row
    # guarded by the same identity allocated it, the identity is cleared
    # only by `restart_generation`, which clears the Work in the same row,
    # and binding fills unbound fields only. Were it unbound all the same,
    # the exhaustion row refuses too while its budgets remain, and nothing
    # is decided for the record.
    out = engine.apply_transition(c, "allocate_work_retry", policy=policy, now=iso,
                                  cause_id=f"queue-{queue_state}", facts=facts,
                                  values={"active_work": fresh}, details=details)
    if out.accepted:
        attempt = out.record["active_work"]["attempt"]
        limit = lifecycle.budget_limit("work_recovery", policy)
        return out.record, out.event, f"new revision for terminal Work, attempt {attempt} of {limit}"
    spent = engine.exhausted(c, ("work_total", "work_recovery"), policy=policy, now=iso)
    out = engine.apply_transition(c, "exhaust_recovery", policy=policy, now=iso,
                                  cause_id=f"queue-{queue_state}", facts=facts,
                                  details=details | {"exhausted": spent})
    if not out.accepted:
        return None, None, f"terminal Work: {out.reason}"
    return out.record, out.event, "automatic Work recovery exhausted; waiting for a human"


def _retry_spent_work(record: dict, inputs: Inputs, res: TickResult, policy: dict) -> dict:
    """``record`` once the Work it was just allocated no longer names a graph
    in ``inputs.spent_graphs``: each spent graph takes ``allocate_work_retry``
    (a fresh graph at a new revision) or ``exhaust_recovery``, which
    clears the Work."""
    while record.get("active_work") and \
            work_queue.graph_id(record["active_work"]["dedupe_key"]) in inputs.spent_graphs:
        spent = record["active_work"]["dedupe_key"]
        new, event, decision = terminal_work(record, record["active_work"], "spent",
                                             now=inputs.now, policy=policy)
        if new is None:
            raise lifecycle.TransitionError(f"Work {spent} is spent: {decision}")
        res.stage("contributions", new)
        res.stage("events", event)
        res.report.setdefault("spent_work", []).append(
            {"contribution": record["contribution_id"], "work": spent, "decision": decision})
        record = new
    return record


def _carries_feedback(state: str, verb: str) -> bool:
    """Whether the allocation row ``verb`` of ``state`` sets the pending
    feedback its Work carries (``active_work.feedback_evidence_ids``)."""
    return lifecycle.WORK_FEEDBACK in lifecycle.find_edge(state, verb).transition.effects.sets


class MissingEvidence(LookupError):
    """The evidence a record names is not in the state store."""


def _unrecorded_head(record: dict, inputs: Inputs) -> str | None:
    """Why development Work is not requested this tick: the observation of
    this tick read the development branch at a head other than the recorded
    one, which a row of this tick (a resume from a human wait, which is not
    observed) did not record. Work built on the recorded head would be
    stale; the next observation takes ``record_head_change`` in
    ``development.fix_needed``, and Work, when still due, is requested on the
    observed head (which, when it is the commit of a patch published anyway,
    also makes its comment due)."""
    if engine.state_key(record) != "development.fix_needed":
        return None
    observed = inputs.observed_dev_heads.get(record["contribution_id"])
    if observed and observed != (record.get("fork") or {}).get("head_sha"):
        return (f"the development branch is at {observed[:12]}, not at the recorded head; "
                "the head is recorded before Work is requested")
    return None


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


def _allocation_verb(record: dict, ci: dict | None, ops: tuple[str, ...], policy: dict,
                     now: str, facts: frozenset = ALLOCATION_FACTS) -> tuple[str | None, str | None]:
    """The row that allocates the Work due in this state, once a worker slot
    and capacity are free, or the reason none applies."""
    reasons = []
    for verb in ALLOCATIONS.get(engine.state_key(record), ()):
        why = engine.refusal(record, verb, policy=policy, now=now, facts=facts,
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


def _exhaust(due: dict, verb: str, ci: dict | None, by_id: dict, res: TickResult,
             policy: dict, now: str) -> None:
    """Take the exhaustion row ``verb`` from ``due``."""
    values, details = _exhaustion_values(due, verb, ci, policy, now)
    changed = _take(due, verb, res, policy, now, cause_id=f"tick-r{due['revision']}",
                    values=values, ci_evidence=ci, details=details)
    by_id[due["contribution_id"]] = changed
    res.report.setdefault("exhausted", []).append(
        {"contribution": due["contribution_id"], "verb": verb, **details})


def _work_request(record: dict, kind: str, inputs: Inputs, now: str) -> dict:
    """The requested Work (``active_work``) for ``kind`` at the record's
    generation and revision. Its reason ids are the pending feedback it
    carries, then the CI evidence and the plan."""
    from datetime import timedelta

    generation, revision = int(record.get("generation", 0)), record["revision"]
    reason_ids: list[str] = []
    if kind in work_queue.SUBMISSION_TASKS:
        # The plan (with its contribution guidelines) and the CI evidence
        # of the candidate head the rewrite and the review describe.
        reason_ids = [i for i in (record.get("plan_evidence_id"),
                                  record.get("last_ci_evidence_id")) if i]
    if kind == work_queue.DEVELOPMENT_REVIEW:
        # What the worker of the held patch answered: the reasons of its
        # Work (its pending feedback, the CI evidence, the plan).
        reason_ids = list((record.get("held_patch") or {}).get("reason_ids") or [])
    if kind in ("development_repair", "upstream_revision"):
        reason_ids = list(dict.fromkeys(
            list(record.get("pending_feedback_evidence_ids") or [])
            + ([record["last_ci_evidence_id"]] if record.get("last_ci_evidence_id") else [])
            + ([record["plan_evidence_id"]] if record.get("plan_evidence_id") else [])))
    if not reason_ids and record.get("plan_evidence_id"):
        reason_ids = [record["plan_evidence_id"]]
    request = {
        "dedupe_key": work_queue.work_key(record["contribution_id"], generation, revision, kind,
                                          engine.head_reruns(record)["flaky"]),
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


def reminder_details(record: dict, operations: dict | None = None,
                     observation: dict | None = None, policy: dict | None = None) -> dict:
    """What a reminder repeats: the pending human action, when it was
    requested, and, where the step binds to a head, the revision and head.
    In the locked upstream-PR wait it also carries the link to the posted
    package, the human who requested the submission, and what the
    observation of this tick found that does not match."""
    pending = record["pending_human_action"]
    heads = {"handoff.awaiting_submission": (record.get("handoff_request") or {}).get("head"),
             "handoff.waiting_upstream_pr": record.get("clean_head_sha")}
    state = engine.state_key(record)
    details = {"pending_human_action": pending["kind"], "requested_at": pending["requested_at"],
               "previous_reminder_at": pending.get("reminded_at"),
               **({"bound_to": {"revision": record["revision"], "head": heads[state]}}
                  if state in heads else {})}
    if pending["kind"] == DEVELOPMENT_PR_LAUNCH_EXHAUSTED:
        # The operation whose launches were spent: the latest development PR
        # operation closed as failed at this revision.
        spent = [op for op in (operations or {}).values()
                 if op["contribution_id"] == record["contribution_id"]
                 and op["kind"] in DEV_PR_KINDS and op["status"] == "failed"
                 and op["revision"] == record["revision"]]
        if spent:
            latest = max(spent, key=lambda op: op["updated_at"])
            details["operation"] = latest["operation_id"]
            details["launches"] = recovery.launch_attempts(latest)
    if pending["kind"] == LAUNCH_EXHAUSTED:
        # The planned provisioning operation whose launches were spent: it
        # stays open, and the reminder names it and the resolution.
        op = (operations or {}).get(record.get("active_operation_id"))
        if op is not None and recovery.is_open(op):
            details["operation"] = op["operation_id"]
            details["launches"] = recovery.launch_attempts(op)
            if policy is not None:
                details["stale_after_days"] = policy["refresh"]["stale_after_days"]
    if pending["kind"] == PROVISIONING_FAILED:
        # The provisioning operation that failed: the latest failed fork
        # provisioning operation of the contribution, with the closed code
        # its receipt reported.
        failed = [op for op in (operations or {}).values()
                  if op["contribution_id"] == record["contribution_id"]
                  and op["kind"] == "fork_provision" and op["status"] == "failed"
                  and op["revision"] <= record["revision"]]
        if failed:
            latest = max(failed, key=lambda op: op["updated_at"])
            details["operation"] = latest["operation_id"]
            details["failure"] = (latest.get("external_ids") or {}).get("provision_failure")
    if state == "handoff.waiting_upstream_pr":
        package = (operations or {}).get((record.get("handoff_request") or {}).get("command_id"))
        details["package_url"] = ((package or {}).get("external_ids") or {}).get("comment_url")
        details["submitter"] = ((record.get("handoff_approval") or {}).get("actor") or {}).get("login")
        details["mismatch"] = (observation or {}).get("mismatch")
    return details


def _launch_handoff(c: dict, by_id: dict, operations: dict, res: TickResult, inputs: Inputs,
                    now: str, policy: dict, evidence: dict) -> None:
    """Record the next attempt of the handoff operation of the record's state
    when none is open, and launch the planned attempt when it is due."""
    cid, state = c["contribution_id"], engine.state_key(c)
    kind = handoff.KINDS[state]
    rep = res.report
    if c.get("paused"):
        rep["deferred"].append({"contribution": cid, "why": f"paused in {state}"})
        return
    active = c.get("active_operation_id")
    op = operations.get(active) if active else None
    if active and op is None:
        raise lifecycle.TransitionError(f"{state} names the operation {active}, which the store "
                                        "does not hold")
    if state == lifecycle.UPSTREAM_BRANCH_WRITER and (op is None or not recovery.is_open(op)) \
            and handoff.rewrite_expired(c, evidence, inputs.now, review=True):
        # The series `riscv64` would be built from is no longer available: a
        # fresh rewrite and review are due, never an endless wait.
        out = engine.apply_transition(
            c, "expire_submission_rewrite", policy=policy, now=now,
            facts={"submission_rewrite_expired"}, open_operations=_open_operations(c, operations),
            cause_id=f"expired-{c.get('submission_rewrite_id')}")
        if not out.accepted:
            raise lifecycle.TransitionError(f"expire_submission_rewrite refused: {out.reason}")
        by_id[cid] = out.record
        res.stage("contributions", out.record)
        res.stage("events", out.event)
        return
    if op is None:
        op = handoff.plan_attempt(c, operations, policy=policy, now=now, evidence=evidence)
        if op["operation_id"] in operations:
            raise lifecycle.TransitionError(f"the next {kind} attempt {op['operation_id']} exists")
        c = engine.record_beside(c, "operation_planned", now=now,
                                 active_operation_id=op["operation_id"])
        by_id[cid] = c
        operations[op["operation_id"]] = op
        res.stage("contributions", c)
        res.stage("operations", op)
    if op["kind"] != kind or op["status"] != "planned":
        return
    launches = recovery.launch_attempts(op)
    limit = lifecycle.budget_limit("operation_launch", policy)
    op_id = op["operation_id"]
    age = (inputs.now - datetime.fromisoformat(op["updated_at"])).total_seconds()
    if launches and age < LAUNCH_INTERVAL_SECONDS:
        return
    # A run of the attempt that is still queued or in progress (or a run list
    # that was not read) holds the attempt: it is neither launched again nor
    # closed as spent, for at most `handoff.RUN_SECONDS` after the last
    # launch, so a slow or queued healthy run is not charged.
    runs = inputs.handoff_runs
    run = None if runs is None else runs.get(op_id)
    if launches and (runs is None or run is not None):
        if age < handoff.RUN_SECONDS:
            why = (f"{kind} run status not observed" if runs is None
                   else f"{kind} run {run} is still in flight")
            rep["deferred"].append({"contribution": cid, "operation": op_id, "why": why})
            return
    late = (f" (run still listed after {handoff.RUN_SECONDS} s)"
            if launches and run is not None else "")
    if launches >= limit:
        # The last allowed launch is old and no receipt came back: the attempt
        # is a definite infrastructure failure. It is closed as failed and
        # fires the retry row of the state, or its exhaustion row.
        failed, fired = handoff.fail_attempt(
            c, op, policy=policy, now=now, failure=handoff.INFRASTRUCTURE,
            reason=f"{kind} {handoff.LAUNCHES_SPENT}{late}",
            external_ids={"launches_spent": launches})
        operations[op["operation_id"]] = failed
        by_id[cid] = fired.record
        res.stage("operations", failed)
        res.stage("contributions", fired.record)
        res.stage("events", fired.event)
        rep["alerts"].append({"contribution": cid, "operation": op["operation_id"],
                              "alert": f"{kind} {handoff.LAUNCHES_SPENT}: {fired.verb}"})
        if engine.state_key(fired.record) == state:
            _launch_handoff(fired.record, by_id, operations, res, inputs, now, policy, evidence)
        return
    launched = recovery.charge_launch(op, now)
    operations[op["operation_id"]] = launched
    res.stage("operations", launched)
    rep["handoff_requests"].append({"operation_id": op["operation_id"], "contribution_id": cid,
                                    "kind": kind, "attempt": launches + 1})


def _open_operations(record: dict, operations: dict) -> tuple[str, ...]:
    """The open operations of a record: those of the store, and the one the
    record names as active, also when the store does not hold it."""
    return recovery.open_operations(record["contribution_id"], operations,
                                    record.get("active_operation_id"))


# A planned operation is launched again once this long has passed since its
# last launch without a receipt, within `operation_launch`. It is also the
# time a dispatched run needs to appear in the run list. A handoff attempt
# additionally waits while a run of it is in flight (`handoff.RUN_SECONDS`).
LAUNCH_INTERVAL_SECONDS = 300
# The operations the development PR workflow executes (a `ci_rerun` of
# `handoff.waiting_ci` is executed by the handoff workflow).
DEVELOPMENT_PR_KINDS = frozenset({"dev_pr_create", "dev_pr_update", "ci_rerun"})
# The development PR operation of `development.waiting_ci`, launched by
# `launch_development_pr`: the PR and the CI bootstrap.
DEV_PR_KINDS = frozenset({"dev_pr_create", "dev_pr_update"})
# The pending human action of `exhaust_development_pr_launches`.
DEVELOPMENT_PR_LAUNCH_EXHAUSTED = lifecycle.DEVELOPMENT_PR_LAUNCH_EXHAUSTED
# A launched development PR operation whose run is still queued or in
# progress (or whose run list was not read) is neither launched again nor
# exhausted for at most this long after its last launch: `dev-pr.yml`
# shares its concurrency group with the handoff workflow, whose run can
# hold the group for most of this time.
DEV_PR_RUN_SECONDS = 1800
# The bound on re-plans of one development PR intent at one revision.
MAX_REPLANS = 20


def _stale_development_pr(record: dict, op: dict) -> str | None:
    """Why a planned development PR operation is closed as failed without a
    row: the contribution is paused (the workflow refuses a paused record,
    so the operation would otherwise burn its launches and fence `park` and
    a stale `resume`), or it no longer matches the record (another state,
    revision, fork, head or base, or a PR already recorded for a create, or
    none for the CI bootstrap). A new one is planned when
    `development.waiting_ci` again holds a published head without the PR."""
    if record.get("paused"):
        return "the contribution is paused"
    fork = record.get("fork") or {}
    target = op.get("target") or {}
    if engine.state_key(record) != "development.waiting_ci" \
            or op["revision"] != record["revision"] \
            or target.get("repository") != fork.get("name") \
            or target.get("repository_id") != fork.get("repository_id") \
            or target.get("expected_head") != fork.get("head_sha") \
            or target.get("base_sha") != record["upstream"].get("base_sha") \
            or (op["kind"] == "dev_pr_create") != (fork.get("pr_number") is None):
        return "the operation no longer matches the record"
    return None


def _close_development_pr(c: dict, operation: dict, why: str, by_id: dict, operations: dict,
                          res: TickResult, now: str) -> dict:
    """Close a planned development PR operation as failed and release the
    record's reference to it; no row is fired. Returns the record."""
    closed = recovery.fold_failure_receipt(operation, external_ids={}, now=now)
    operations[closed["operation_id"]] = closed
    released = recovery.release(c, closed, now)
    by_id[c["contribution_id"]] = released
    res.stage("operations", closed)
    if released is not c:
        res.stage("contributions", released)
    res.report.setdefault("development_pr_closures", []).append(
        {"operation_id": closed["operation_id"], "contribution_id": c["contribution_id"],
         "closed": why})
    return released


def _launch_development_pr(c: dict, operation: dict, by_id: dict, operations: dict,
                           inputs: Inputs, res: TickResult, now: str, policy: dict) -> bool:
    """Launch the planned development PR operation of a record in
    `development.waiting_ci` by the table: `launch_development_pr`, which
    charges `operation_launch` (counted on the operation), or, once that
    budget is exhausted, `exhaust_development_pr_launches`, which closes the
    operation as failed and releases it. A launched operation whose run is
    still listed in flight, or whose run list was not read, is held for at
    most `DEV_PR_RUN_SECONDS` after its last launch. Returns whether a
    launch was requested."""
    cid, oid = c["contribution_id"], operation["operation_id"]
    if engine.state_key(c) != "development.waiting_ci":
        return False  # no other state has a row that launches it
    counts = recovery.launch_count(operation)
    attempts = recovery.launch_attempts(operation)
    open_ops = recovery.open_operations(cid, {**operations, oid: operation}, oid)
    facts = {"development_pr_operation_planned"}
    rep = res.report
    if attempts:
        runs = inputs.development_pr_runs
        run = None if runs is None else runs.get(oid)
        age = (inputs.now - datetime.fromisoformat(operation["updated_at"])).total_seconds()
        if (runs is None or run is not None) and age < DEV_PR_RUN_SECONDS:
            why = ("development PR run status not observed" if runs is None
                   else f"development PR run {run} is still in flight")
            rep.setdefault("deferred", []).append(
                {"contribution": cid, "operation": oid, "why": why})
            return False
    why = engine.refusal(c, "launch_development_pr", policy=policy, now=now, facts=facts,
                         counts=counts, open_operations=open_ops)
    if why is None:
        out = engine.apply_transition(c, "launch_development_pr", policy=policy, now=now,
                                      cause_id=oid, facts=facts, counts=counts,
                                      open_operations=open_ops,
                                      details={"operation": oid, "kind": operation["kind"],
                                               "attempt": attempts + 1})
        if out.charged != ("operation_launch",):
            raise lifecycle.TransitionError(f"launch_development_pr charged {out.charged}")
        launched = recovery.charge_launch(operation, now)
        operations[oid] = launched
        by_id[cid] = out.record
        res.stage("operations", launched)
        res.stage("contributions", out.record)
        res.stage("events", out.event)
        rep["dev_pr_requests"].append({
            "operation_id": oid, "contribution_id": cid, "kind": operation["kind"],
            "attempt": attempts + 1})
        return True
    if not c.get("paused") and attempts >= lifecycle.budget_limit("operation_launch", policy):
        out = engine.apply_transition(
            c, "exhaust_development_pr_launches", policy=policy, now=now, cause_id=oid,
            facts=facts, counts=counts, open_operations=open_ops,
            values={"pending_human_action": DEVELOPMENT_PR_LAUNCH_EXHAUSTED,
                    lifecycle.PENDING_COUNTER: "operation_launch"},
            details={"operation": oid, "kind": operation["kind"], "launches": attempts})
        if not out.accepted:
            raise lifecycle.TransitionError(
                f"exhaust_development_pr_launches refused: {out.reason}")
        closed = recovery.fold_failure_receipt(operation, external_ids={}, now=now)
        operations[oid] = closed
        released = recovery.release(out.record, closed, now)
        by_id[cid] = released
        res.stage("operations", closed)
        res.stage("contributions", released)
        res.stage("events", out.event)
        rep.setdefault("alerts", []).append(
            {"contribution": cid, "operation": oid,
             "alert": f"{operation['kind']} operation_launch is spent without a receipt"})
        return False
    rep.setdefault("deferred", []).append({"contribution": cid, "why": why})
    return False


def _spent_rerun(record: dict, op: dict, attempts: int, waited: bool, policy: dict,
                 evidence: dict) -> str | None:
    """Why a planned ``ci_rerun`` operation is closed as failed: the
    contribution is paused (the workflow refuses a paused record, so the
    operation would otherwise stay open through the pause and fence `park`
    and a stale `resume`), its launches are spent without a receipt, or it
    no longer matches the record (another head, revision, or a state that
    does not rerun interrupted CI, ``ci_rerun.INTERRUPTED_STATES``). The
    head is the one the CI of the state is on (``ci_rerun.expected_head``)."""
    if record.get("paused"):
        return "the contribution is paused"
    if engine.state_key(record) not in ci_rerun.INTERRUPTED_STATES \
            or op["target"].get("expected_head") != ci_rerun.expected_head(record, evidence) \
            or op["revision"] != record["revision"]:
        return "the operation no longer matches the record"
    if waited and attempts >= lifecycle.budget_limit("operation_launch", policy):
        return "operation_launch is spent without a receipt"
    return None
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
    if engine.refusal(c, "require_provisioning_intervention", policy=policy, now=now,
                      counts=counts, open_operations=open_ops) is None:
        # The exhausted launches, not while paused, by the row's guard.
        out = engine.apply_transition(
            c, "require_provisioning_intervention", policy=policy, now=now, cause_id=oid,
            counts=counts, open_operations=open_ops,
            values={"pending_human_action": LAUNCH_EXHAUSTED,
                    lifecycle.PENDING_COUNTER: "operation_launch"},
            details={"operation": oid, "launches": attempts,
                     "stale_after_days": policy["refresh"]["stale_after_days"]})
        if not out.accepted:
            raise lifecycle.TransitionError(f"require_provisioning_intervention refused: {out.reason}")
        by_id[cid] = out.record
        res.stage("contributions", out.record)
        res.stage("events", out.event)
        return False
    res.report.setdefault("deferred", []).append({"contribution": cid, "why": why})
    return False


def _exhaust_provisioning_replans(c: dict, last: dict, by_id: dict, operations: dict,
                                  res: TickResult, now: str, policy: dict) -> None:
    """`require_provisioning_intervention` once `MAX_REPLANS` re-plans of the
    provisioning intent at one revision have all closed without provisioning
    the fork: the launches of the intent are spent (`operation_launch`), and
    the resume of the wait starts a new revision, whose operations have new
    ids."""
    cid = c["contribution_id"]
    limit = lifecycle.budget_limit("operation_launch", policy)
    out = engine.apply_transition(
        c, "require_provisioning_intervention", policy=policy, now=now,
        cause_id=last["operation_id"], counts={"operation_launch": limit},
        open_operations=_open_operations(c, operations),
        values={"pending_human_action": LAUNCH_EXHAUSTED,
                lifecycle.PENDING_COUNTER: "operation_launch"},
        details={"replans": MAX_REPLANS})
    if not out.accepted:
        raise lifecycle.TransitionError(f"require_provisioning_intervention refused: {out.reason}")
    by_id[cid] = out.record
    res.stage("contributions", out.record)
    res.stage("events", out.event)


def _command_target(decision, by_id: dict, operations: dict) -> str | None:
    """The contribution a command comment concerns, or ``None``: the one its
    operation belongs to for ``resolve-operation``, else the one tracked by
    the issue the comment was posted on."""
    cmd = decision.command
    if cmd is not None and cmd.verb == "resolve-operation":
        return (operations.get(cmd.args.get("operation")) or {}).get("contribution_id")
    rec = intake.contribution_for_issue(by_id.values(), decision.receipt.get("issue_number"))
    return rec["contribution_id"] if rec else None


# The commands an open operation fences (`park`, `refresh`, and the stale
# resume require no open operation).
FENCED_COMMANDS = frozenset({"resume", "park", "refresh"})


def resolve_line(operation_id: str) -> str:
    """The resolution that closes an open operation a human has verified."""
    return (f"/upstreaming resolve-operation operation={operation_id} "
            'disposition=failed|not-launched reason="..."')


def _refusal(record: dict, cmd: Command, why: str, open_ops: tuple[str, ...] = ()) -> str:
    """The reply to a command the table refuses, in the words the command
    feedback recognizes for a pause or resume that has nothing to do. A
    command the fence of an open operation refuses in a wait or a pause is
    answered with the resolution that closes it, naming the pipeline's own
    operation id; elsewhere the operation is in flight, and the refusal only
    names it."""
    if cmd.verb == "pause" and record.get("paused") and why == "contribution is paused":
        return "already paused"
    state = lifecycle.LIFECYCLE.get(engine.state_key(record))
    if cmd.verb == "resume" and not record.get("paused") and state is not None:
        if state.kind == "generation_end":
            return commands.PARKED_RESUME
        if state.kind == "terminal":
            return commands.ENDED_RESUME
        if state.kind != "human_wait":
            return "not paused"
    if cmd.verb in FENCED_COMMANDS and open_ops \
            and (record.get("paused") or (state is not None and state.kind == "human_wait")) \
            and f"operation {open_ops[0]} is open" in why.split("; "):
        return (f"operation {open_ops[0]} is open: once you have verified what its runs did, "
                f"close it with `{resolve_line(open_ops[0])}`, then post this command again")
    return why


PARTIAL_OVERRIDE = ("an admission override names both `class` and `paths`, or neither: the "
                    "override admits development only with a class and paths, so one of them "
                    "alone could never admit")


def admission_override(cmd: Command) -> dict | None:
    """The admission override a refresh from ``parked.idle`` records: its
    ``class`` and ``paths`` with the command's reason, or ``None`` when it
    names neither. Raises ``ValueError`` when it names only one of them (the
    plan fold refuses such an override against every decision park, so
    each planning it starts would be wasted) and for an unsafe scope
    path."""
    if "class" not in cmd.args and "paths" not in cmd.args:
        return None
    if "class" not in cmd.args or "paths" not in cmd.args:
        raise ValueError(PARTIAL_OVERRIDE)
    paths = validate_scope_paths([g for g in cmd.args["paths"].split(",") if g])
    if not cmd.args["class"] or not paths:
        raise ValueError(PARTIAL_OVERRIDE)
    return {"reason": cmd.args["reason"], "class": cmd.args["class"], "paths": paths}


def _command_values(rec: dict, verb: str, cmd: Command, receipt: dict, operations: dict,
                    policy: dict, now: str) -> dict:
    """The values of the fields a command's row sets that the engine cannot
    derive: the refresh a refresh or a stale resume records, the admission
    override of a restart, and the approval of a submission."""
    if verb == "restart_generation":
        return {"admission_override": admission_override(cmd)}
    if verb == "submit_upstream":
        return {"handoff_approval": handoff.approval(
            rec, command_id=receipt["command_id"], actor=receipt["actor"], head=cmd.args["head"])}
    if verb not in ("refresh_planning", "resume_stale"):
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
        return ("`class` and `paths` apply only to a refresh from `parked.idle` that starts a "
                "new generation")
    if "branch" in cmd.args and verb != "restart_generation":
        return ("`branch` applies only to a refresh from `parked.idle` that starts a new "
                "generation")
    transition, _target = engine.resolve(state, verb)
    if "head" in cmd.args and not _HEAD_GUARDS & set(transition.guard):
        return f"`head` does not apply to `/upstreaming {cmd.verb}` in `{state}`"
    return None


# The pending actions with which the controller leaves a record in
# `provisioning.waiting_human`, each with the counter it names: exhausted
# launches of the planned operation, a provisioning operation that failed,
# and an upstream that could not be read: a missing or renamed base branch,
# failed reads, and a renamed repository (`require_renamed_upstream`, with no
# counter), whose resume waits again at once while it is still renamed.
PROVISIONING_WAITS = frozenset({(LAUNCH_EXHAUSTED, "operation_launch"),
                                (PROVISIONING_FAILED, None),
                                (lifecycle.BASE_BRANCH_UNAVAILABLE, "source_reads"),
                                (lifecycle.BASE_BRANCH_UNAVAILABLE, None),
                                (lifecycle.UPSTREAM_UNAVAILABLE, "source_reads"),
                                (lifecycle.UPSTREAM_UNAVAILABLE, None)})


def _command_facts(rec: dict, cmd: Command, upstream_pr: dict | None = None) -> frozenset[str]:
    """The facts a command establishes by itself, or with the upstream PR the
    observation of this tick read. A human `/upstreaming resume` of a
    provisioning wait the controller entered is the human's assertion that
    the prerequisite is corrected. A resume after `pr_closed_unmerged` needs
    the upstream PR observed open again or merged (the observation of
    `upstream_review.waiting_ci` then records the merge), and a refresh from
    `parked.idle` of a record that names an upstream PR needs it observed
    closed or merged."""
    pending = rec.get("pending_human_action") or {}
    state = engine.state_key(rec)
    if cmd.verb == "resume" and state == "provisioning.waiting_human" \
            and (pending.get("kind"), pending.get("counter")) in PROVISIONING_WAITS:
        return frozenset({"prerequisite_corrected"})
    if upstream_pr is not None:
        is_open = upstream_pr.get("state") == "open" and not upstream_pr.get("merged")
        if cmd.verb == "resume" and state == "upstream_review.waiting_human" \
                and (is_open or upstream_pr.get("merged")):
            return frozenset({"upstream_pr_open_or_merged"})
        if cmd.verb == "refresh" and state == "parked.idle" and not is_open:
            return frozenset({"upstream_pr_not_open"})
    return frozenset()


def _apply_table_command(rec: dict, cmd: Command, receipt: dict, operations: dict,
                         policy: dict, now: str, upstream_pr: dict | None = None) -> engine.Outcome:
    """Apply a command to a record in a table state, as the table defines it."""
    open_ops = _open_operations(rec, operations)
    counts = {**recovery.launch_count(operations.get(rec.get("active_operation_id"))),
              **handoff.counts(rec, operations)}
    facts = _command_facts(rec, cmd, upstream_pr)
    verb, why = engine.resolve_command(rec, cmd, policy=policy, now=now, counts=counts,
                                       open_operations=open_ops, facts=facts)
    if verb is None:
        return engine.Outcome(rec, False, _refusal(rec, cmd, why, open_ops), None)
    unread = _unread_field(cmd, verb, engine.state_key(rec))
    if unread:
        return engine.Outcome(rec, False, unread, None, verb)
    if cmd.verb in engine.HELD_COMMANDS:
        return engine.Outcome(rec, False, engine.HELD_COMMANDS[cmd.verb], None, verb)
    try:
        values = _command_values(rec, verb, cmd, receipt, operations, policy, now)
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
                  now: str, upstream_pr: dict | None = None) -> engine.Outcome:
    """Apply an authenticated command to the record it concerns: through the
    transition engine where the table defines it (``engine.takes_command``);
    the intake command ``authorize-followup`` otherwise. ``upstream_pr`` is
    the upstream PR as the observation of this tick read it, if it did."""
    if engine.takes_command(rec, cmd.verb):
        return _apply_table_command(rec, cmd, receipt, operations, policy, now, upstream_pr)
    return authorize_followup(rec, cmd, command_id=receipt["command_id"], now=now)


# The command that is not the table's: ``authorize-followup`` on a merged
# contribution records an event; intake creates the new identity.
FOLLOWUP_COMMANDS = frozenset({"authorize-followup"})


def authorize_followup(record: dict, cmd: Command, *, command_id: str,
                       now: str) -> engine.Outcome:
    """Apply an authenticated command the engine does not take: only
    ``authorize-followup`` on a merged contribution, which records an event
    and leaves the record intact. Any other command is rejected with the
    states it applies in. Actor authority, replay and digest checks happen in
    ``intake.accept_command`` before this is called."""
    def reject(reason: str) -> engine.Outcome:
        return engine.Outcome(record, False, reason, None)

    if cmd.contribution is not None and cmd.contribution != record["contribution_id"]:
        return reject("command names another contribution than this issue's")
    if cmd.verb not in FOLLOWUP_COMMANDS:
        return reject(engine.not_applicable(cmd.verb, engine.state_key(record)))
    rev = cmd.revision if cmd.revision is not None else record["revision"]
    if rev > record["revision"]:
        return reject("revision is not a known revision")
    if rev != record["revision"]:
        return reject(f"stale revision {rev}, current is {record['revision']}")
    if record["phase"] not in ("release_wait", "released"):
        return reject("follow-ups need a merged contribution")
    rec = dict(record, updated_at=now)
    schemas.validate("contribution", rec)
    return engine.Outcome(rec, True, None, lifecycle._event(
        record, rec, "command:authorize-followup", "command", command_id, now, False))


# What a human resolution, or a trusted receipt of a definite failure,
# establishes for the lifecycle row an operation fires, by operation kind and
# closed status: the facts and the values of the fields the row sets. A row
# whose guards a resolution does not establish (`confirm_provisioning` needs
# the receipt and its readback) is not taken.
RESOLUTION_FIRES: dict[tuple[str, str], tuple[frozenset[str], dict]] = {
    ("fork_provision", "failed"): (frozenset({"provisioning_blocked"}),
                                   {"pending_human_action": PROVISIONING_FAILED}),
}


def resolution_evidence(op: dict, cmd: Command, receipt: dict, record: dict | None,
                        policy: dict, now: str) -> dict:
    """The `resolution` evidence record of `/upstreaming resolve-operation`,
    written by the controller from the authorized command and its own
    snapshot of the operation: who stated the disposition, where, why, and
    what the pipeline held about the operation then. The pipeline does not
    verify the statement; the record makes it auditable. The reason is
    untrusted text kept as bounded data: it is not in the summary and is
    never rendered."""
    oid = op["operation_id"]
    # Bounded at intake: the normalized reason fits the schemas.
    reason = state.normalize_state_text(cmd.args["reason"])
    summary = (f"Operation {oid} ({op['kind']}, {op['status']}) resolved as "
               f"{cmd.args['disposition']} by command {receipt['command_id']}.")
    return {
        "schema_version": 1,
        "evidence_id": f"resolution-{oid}-{receipt['comment_id']}",
        "contribution_id": op["contribution_id"],
        "kind": "resolution",
        "policy_version": policy["policy_version"],
        "collected_at": now,
        "summary": summary,
        "resolution": {
            "operation_id": oid,
            "disposition": cmd.args["disposition"],
            "command_id": receipt["command_id"],
            "comment_id": receipt["comment_id"],
            "issue_number": receipt["issue_number"],
            "content_digest": receipt["content_digest"],
            "actor": {"id": receipt["actor"]["id"], "login": receipt["actor"]["login"]},
            "reason": reason,
            "operation": {"kind": op["kind"], "status": op["status"], "revision": op["revision"],
                          "launch_attempts": recovery.launch_attempts(op),
                          "discoveries": len(op.get("discovery") or []),
                          "updated_at": op["updated_at"]},
            "contribution_state": engine.state_key(record) if record else None,
            "paused": bool((record or {}).get("paused")),
        },
    }


def _resolve_operation(cmd: Command, receipt: dict, by_id: dict, operations: dict,
                       inputs: Inputs, res: TickResult, now: str, policy: dict,
                       store: dict[str, dict]) -> dict:
    """`/upstreaming resolve-operation` by the operation table: the one
    `resolve_*` row whose guards hold. The authority of the actor was checked
    at intake; the command is accepted only on the tracking issue of the
    operation's contribution. The controller writes the resolution evidence
    from the command (`resolution_evidence`) and stages it with the closed
    operation only when the row is taken, so both land in one state commit.
    An operation that reaches `confirmed` or `failed` fires its lifecycle row
    when the resolution establishes the row's guards; the record's reference
    to the closed operation is released. ``store`` is the store's evidence by
    id: a branch operation confirmed at the commit a ``publish_anyway`` review
    pinned makes its comment due."""
    op = operations.get(cmd.args["operation"])
    if op is None:
        return dict(receipt, result="rejected", reason="unknown operation")
    cid = op["contribution_id"]
    rec = by_id.get(cid)
    if rec is None:
        return dict(receipt, result="rejected",
                    reason=f"operation {op['operation_id']} belongs to no tracked contribution")
    if rec["tracking_issue"]["number"] != receipt.get("issue_number"):
        return dict(receipt, result="rejected",
                    reason=f"post this command on the tracking issue of {cid}")
    receipt = dict(receipt, contribution_id=cid)
    evidence = resolution_evidence(op, cmd, receipt, rec, policy, now)
    try:
        new = recovery.resolve(op, cmd, actor=receipt["actor"], command_id=receipt["command_id"],
                               evidence=evidence, now=now)
    except recovery.RecoveryError as e:
        return dict(receipt, result="rejected", reason=str(e))
    operations[op["operation_id"]] = new
    res.stage("evidence", evidence)
    res.stage("operations", new)
    facts, values = RESOLUTION_FIRES.get((new["kind"], new["status"]), (frozenset(), {}))
    fired = None
    if engine.state_key(rec) in lifecycle.LIFECYCLE and recovery.fired_verbs(rec, new) \
            and (facts or values):
        fired = recovery.fire(rec, new, policy=policy, now=now, facts=facts, values=values,
                              cause_id=receipt["command_id"],
                              open_operations=_open_operations(rec, operations))
    changed = fired.record if fired is not None and fired.accepted else recovery.release(rec, new, now)
    # A branch operation a human confirms whose commit a `publish_anyway`
    # review pinned: the patch published anyway is on the branch, so its
    # comment is due, in any state.
    changed = _landed_due(changed, patch_review.confirmed_heads(changed, [new]), store, res,
                          now)
    if changed is not rec:
        by_id[rec["contribution_id"]] = changed
        res.stage("contributions", changed)
    if fired is not None and fired.accepted:
        res.stage("events", fired.event)
    elif fired is not None:
        return dict(receipt, reason=f"the lifecycle row is not taken: {fired.reason}")
    return receipt


def _apply(cmd: Command, receipt: dict, by_id: dict, operations: dict, inputs: Inputs,
           res: TickResult, now: str, policy: dict, evidence: dict[str, dict]) -> dict:
    if cmd.verb == "resolve-operation":
        return _resolve_operation(cmd, receipt, by_id, operations, inputs, res, now, policy,
                                  evidence)
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
                            upstream_pr=inputs.upstream_pr_observations.get(rec["contribution_id"]))
    except lifecycle.TransitionError as e:
        return dict(receipt, result="rejected", reason=str(e))
    if not out.accepted:
        return dict(receipt, result="rejected", reason=out.reason)
    moved = out.record
    if engine.state_key(moved) != engine.state_key(rec):
        # A command that moves the record out of a state whose observation
        # does not record the head (a human wait) when the development
        # branch is at a head other than the recorded one: a patch
        # published anyway that is that head makes its comment due now,
        # since a park, an abandon or a replan never records it.
        observed = inputs.observed_dev_heads.get(rec["contribution_id"])
        if observed and observed != (moved.get("fork") or {}).get("head_sha"):
            moved = _landed_due(moved, {observed}, evidence, res, now)
    by_id[rec["contribution_id"]] = moved
    res.stage("contributions", moved)
    if out.event:
        res.stage("events", out.event)
    _restart_launches(out, operations, res, now)
    return receipt


def _landed_due(rec: dict, heads, evidence: dict[str, dict], res: TickResult,
                now: str) -> dict:
    """The record with the comment of every ``publish_anyway`` review whose
    pinned commit is one of ``heads`` due beside the command
    (``patch_review.landed_anyway``). The comments made due, and any dropped
    to fit them, are named in the report (``comments_due``); the tick
    alerts on each dropped."""
    due, dropped = patch_review.comment_due(
        rec, patch_review.landed_anyway(rec, heads, evidence), now=now)
    if due == (rec.get("review_comments") or []):
        return rec
    before = {item["evidence_id"] for item in rec.get("review_comments") or []}
    entry = {"contribution": rec["contribution_id"],
             "review_comments_due": [item["evidence_id"] for item in due
                                     if item["evidence_id"] not in before]}
    if dropped:
        entry["review_comments_dropped"] = [item["evidence_id"] for item in dropped]
    res.report.setdefault("comments_due", []).append(entry)
    return engine.record_beside(rec, "review_comment_due", now=now, review_comments=due)


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
            api, contributions.values(), issues, policy, comments, consumed
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
