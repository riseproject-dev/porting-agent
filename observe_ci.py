# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Turn live CI and branch state into lifecycle changes (plan sections 9, 14.3).

Deterministic, no model. For each contribution in a phase that watches CI:

* read the fork's current head of the branch the state watches: the
  development branch ``riscv64-dev`` in development and at the submission
  gate, and ``riscv64`` from CI on the clean head on (a manual or pipeline
  push shows up as a head change, which bumps the revision); a missing
  branch is reported and changes nothing;
* resolve uncertain branch operations by discovery;
* read the CI snapshot for the exact head and evaluate it against the
  dossier's required jobs and expected wheels;
* record an evidence record and move ``activity`` to ``waiting_ci``,
  ``fix_needed`` (failed) or ``waiting_human`` (approval/access), and
  take ``nominate_for_handoff`` to ``handoff.awaiting_submission`` with
  ``last_ci_evidence_id`` set (passed, plan 14.1);
* at ``handoff.awaiting_submission``, take the table's rows for what is
  observed on the candidate head: ``record_explanation`` for an
  explanation-only comment, then ``return_for_repair`` for a CI failure, or
  else ``record_actionable_review`` for an actionable review;
* in ``handoff.waiting_ci``, take ``accept_clean_head_ci`` or
  ``return_for_repair`` for CI on the clean head of ``riscv64`` (CI of the
  candidate head when the clean head has its tree);
* in the locked ``handoff.waiting_upstream_pr``, discover the upstream PR
  read-only and take ``verify_upstream_pr`` for exactly one that matches, or
  record what does not match once (``record_audit_observation``).

A contribution that waits for a human in a state of the table without an
observation row (``compat.waits_for_human``) is not observed: only the rows
the table lists for the wait leave it.

Returns staged records for the state publisher. Never writes remotely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import ci_evidence
import collect
import compat
import engine
import handoff
import isolation
import lifecycle
import recovery
import schemas
from common import DEV_REF, UPSTREAM_REF, safe_id

WATCH_PHASES = {"development", "handoff", "upstream_review"}
# The states that watch `riscv64`, the upstream branch: CI on its clean head,
# the locked wait for the upstream PR from it, and upstream review. Every
# other watched state watches the development branch `riscv64-dev`.
UPSTREAM_BRANCH_STATES = ("handoff.waiting_ci", "handoff.waiting_upstream_pr")


def watched_ref(record: dict) -> str:
    """The fork branch the observation of ``record`` reads."""
    if record["phase"] == "upstream_review" or engine.state_key(record) in UPSTREAM_BRANCH_STATES:
        return UPSTREAM_REF
    return DEV_REF


@dataclass
class Observation:
    staged: list[tuple[str, dict]] = field(default_factory=list)
    report: list[dict] = field(default_factory=list)


def _evidence_id(repo_id: int, run: dict) -> str:
    return safe_id(f"ci-{repo_id}-{run['id']}-{run['run_attempt']}")


def contract_for(record: dict, contracts: dict[str, dict]) -> dict:
    """The dossier target contract: required job patterns + expected wheels.

    Plan 9.2 accepts exact-head CI when every required build/test job
    executed and passed. Uploaded workflow artifacts are not a development CI
    requirement: many projects build and test wheels in PR CI but upload only
    from release workflows.
    """
    c = contracts.get(record.get("target_contract_id") or "") or {}
    data = c.get("data") or {}
    return {"required_job_patterns": data.get("required_job_patterns") or ["riscv64"],
            "expected_wheels": data.get("expected_wheels") or [],
            "required_runner_label": data.get("required_runner_label")}


def _next_ci_observation(now: str, policy: dict) -> str:
    minutes = (policy.get("recovery_minutes") or [5])[0]
    return (datetime.fromisoformat(now) + timedelta(minutes=minutes)).isoformat()


def _terminal_blocking_ci(evidence: dict | None, head_sha: str) -> bool:
    if not evidence or evidence.get("kind") != "ci" or (evidence.get("ci") or {}).get("head_sha") != head_sha:
        return False
    for job in (evidence.get("ci") or {}).get("jobs") or []:
        if job.get("executed") and job.get("conclusion") != ci_evidence.SUCCESS \
                and not any(marker in job.get("name", "").lower()
                            for marker in ci_evidence.CREDENTIALLESS_JOB_MARKERS):
            return True
    return any("failed runner-label check" in problem for problem in evidence.get("limitations") or [])


def _obsolete_undispatched_ci_repair(record: dict, verdict: ci_evidence.CiVerdict,
                                     evidence: dict[str, dict], head_sha: str,
                                     unadmitted_work: set[str] | None = None) -> bool:
    """Recognize a repair request that could only have come from incomplete CI.

    The request is development Work for the published head: a repair, or a
    patch (``request_patch`` asks for one when no failing CI job and no
    feedback is recorded).

    This is deliberately narrow: the old request must still be undispatched,
    the published head must be unchanged, green required jobs must now have
    executed, and no human or terminal CI reason may support the repair.
    Undispatched means proven by the native queue replay and dispatcher runs
    (``unadmitted_work``). ``dispatched_run_id`` is not proof either way: it
    stays null while the Work is claimed and its worker runs, and it names a
    finished dispatcher run even when that run admitted nothing.
    """
    active = record.get("active_work") or {}
    artifact = record.get("proposal_artifact") or {}
    if active.get("dedupe_key") not in (unadmitted_work or set()):
        return False
    if active.get("task") not in ("development_repair", "development_patch") \
            or record.get("phase") != "development" or artifact.get("outcome") != "published" \
            or (record.get("fork") or {}).get("head_sha") != head_sha \
            or verdict.state not in {"passed", "missing"} or not verdict.executed_jobs:
        return False
    reasons = [evidence.get(reason_id) for reason_id in active.get("reason_ids") or []]
    return not any((reason or {}).get("kind") == "human_relay"
                   or _terminal_blocking_ci(reason, head_sha) for reason in reasons)


def human_wait_without_observation(record: dict) -> bool:
    """Whether ``record`` waits for a human (``compat.waits_for_human``) in a
    state that has no observation row. Observation changes nothing there."""
    if not compat.waits_for_human(record):
        return False
    return not any(edge.transition.trigger == lifecycle.Trigger.OBSERVATION
                   for edge in lifecycle.state_edges(engine.state_key(record)))


def holds_requested_work(record: dict) -> bool:
    """Whether the record holds requested Work, or a Result held for a
    publish retry that is not exhausted."""
    pending = record.get("pending_publication") or {}
    return bool(record.get("active_work")) or bool(pending and not pending.get("exhausted"))


def _record_gate_review(obs: Observation, entry: dict, cur: dict, eid: str, ev: dict,
                        feedback: dict, *, policy: dict, now: str) -> dict:
    """Take ``record_actionable_review`` at the submission gate.

    The row records the review as pending feedback and returns the
    contribution to ``development.fix_needed``; the CI evidence of the
    candidate head is kept. The ids of the comments are added to the replay
    fences of the same record, so the same comments are not read again."""
    recorded = engine.apply_transition(
        cur, "record_actionable_review", policy=policy, now=now, cause_id=eid,
        facts={"actionable_review", "candidate_head_not_failed"},
        values={"pending_feedback_evidence_ids": list(dict.fromkeys(
            (cur.get("pending_feedback_evidence_ids") or []) + [eid]))[-64:]},
        details={"evidence_id": eid})
    if not recorded.accepted:
        entry["review_feedback_deferred"] = recorded.reason
        return cur
    new = dict(
        recorded.record,
        processed_review_ids=((cur.get("processed_review_ids") or [])
                              + feedback["review_ids"])[-256:],
        processed_review_comment_ids=((cur.get("processed_review_comment_ids") or [])
                                      + feedback["comment_ids"])[-512:],
        processed_pr_conversation_comment_ids=(
            (cur.get("processed_pr_conversation_comment_ids") or [])
            + feedback["conversation_comment_ids"])[-512:])
    schemas.validate("contribution", new)
    obs.staged.extend([("evidence", ev), ("contributions", new), ("events", recorded.event)])
    entry.update(review_feedback=eid, returned_to_development=True)
    return new


def _observe_clean_head_ci(api: collect.Api, obs: Observation, entry: dict, rec: dict, head: str,
                           contracts: dict[str, dict], *, policy: dict, now: str,
                           evidence: dict[str, dict], operations: dict[str, dict]) -> None:
    """``handoff.waiting_ci``: CI on the clean head of `riscv64`.

    ``accept_clean_head_ci`` when every required job executed and passed on
    the clean head, ``return_for_repair`` for a blocking failure there or a
    `riscv64` head that is not the clean head. CI is a function of the tree:
    when the reviewed series has the tree of the candidate head, the CI of
    the candidate head is read (``handoff.ci_head``). Anything else (CI still
    running, missing, or not executed) keeps the wait; nothing is rerun."""
    clean = rec["clean_head_sha"]
    if head != clean:
        out = engine.apply_transition(
            rec, "return_for_repair", policy=policy, now=now, cause_id=f"branch:{head[:12]}",
            facts={"clean_head_needs_repair"}, details={"head": head, "clean_head": clean})
        obs.staged.extend([("contributions", out.record), ("events", out.event)])
        entry.update(returned_to_development=True,
                     reason=f"the {UPSTREAM_REF} head is not the clean head")
        return
    clean = handoff.ci_head(rec, evidence.get(rec.get("submission_review_id") or "") or {})
    entry["ci_head"] = clean
    snap = collect.ci_snapshot(api, rec["fork"]["name"], clean)
    ctr = contract_for(rec, contracts)
    verdict = collect.evaluate_snapshot(snap, required_job_patterns=ctr["required_job_patterns"],
                                        expected_wheels=ctr["expected_wheels"],
                                        required_runner_label=ctr["required_runner_label"])
    entry["ci"] = {"repository": rec["fork"]["name"], "state": verdict.state,
                   "problems": verdict.problems[:10]}
    latest = ci_evidence.latest_attempts([r for r in snap.runs if r["head_sha"] == clean])
    if verdict.state not in ("passed", "failed") or not latest:
        entry["note"] = "CI on the clean head is not complete"
        if handoff.clean_head_ci_missing(rec, operations, now, has_runs=bool(latest)):
            # `riscv64` has no pull request: CI that was not dispatched, or
            # that never completes, waits for a human after a bounded wait.
            out = engine.apply_transition(
                rec, "require_clean_head_ci", policy=policy, now=now,
                cause_id=f"clean-head-ci-{clean[:12]}", facts={"clean_head_ci_missing"},
                details={"head": clean, "ci_state": verdict.state})
            obs.staged.extend([("contributions", out.record), ("events", out.event)])
            entry["waiting_for_human"] = "clean_head_ci_missing"
        return
    run = max(latest, key=lambda r: r["id"])
    eid = _evidence_id(snap.repository["id"], run)
    ev = ci_evidence.evidence_record(
        evidence_id=eid, contribution_id=rec["contribution_id"],
        policy_version=policy["policy_version"], collected_at=now, repository=snap.repository,
        run=run, jobs=[j for r in latest for j in snap.jobs_by_run.get(r["id"], [])],
        base_sha=rec["upstream"].get("base_sha"), test_merge_sha=None,
        expected_wheels=ctr["expected_wheels"], artifacts=[], verdict=verdict,
        debug_present=False, limitations=[] if verdict.state == "passed" else ["see problems"])
    obs.staged.append(("evidence", ev))
    if verdict.state == "passed":
        out = engine.apply_transition(
            rec, "accept_clean_head_ci", policy=policy, now=now, cause_id=eid,
            facts={"ci_passed_on_clean_head"}, values={"last_ci_evidence_id": eid},
            details={"evidence_id": eid, "head": clean, "clean_head": rec["clean_head_sha"]})
    else:
        out = engine.apply_transition(
            rec, "return_for_repair", policy=policy, now=now, cause_id=eid,
            facts={"clean_head_needs_repair"}, details={"evidence_id": eid, "head": clean})
        entry["returned_to_development"] = True
    obs.staged.extend([("contributions", out.record), ("events", out.event)])
    entry["evidence"] = eid


def _observe_upstream_pr(api: collect.Api, obs: Observation, entry: dict, rec: dict, head: str,
                         *, policy: dict, now: str, audited: dict[str, set[str]]) -> None:
    """``handoff.waiting_upstream_pr``, the locked wait: read-only discovery
    of the upstream PR the package asked for.

    Exactly one candidate that satisfies every filter (``handoff.pull_mismatch``)
    takes ``verify_upstream_pr``. Anything else that does not match (another
    fork head, a candidate that fails a filter, several matching candidates)
    is recorded once by ``record_audit_observation`` and never changes the
    state; the reminder states it."""
    cid = rec["contribution_id"]
    authority = policy["command_authority"]["actor_ids"]
    matching, rejected = handoff.matching_upstream_prs(
        rec, collect.upstream_pull_requests(api, rec), authority)
    if len(matching) == 1:
        pr = matching[0]
        out = engine.apply_transition(
            rec, "verify_upstream_pr", policy=policy, now=now, cause_id=f"upstream-pr-{pr['id']}",
            facts={"upstream_pr_open_on_base", "upstream_pr_at_clean_head",
                   "upstream_pr_human_author"},
            values={"upstream_pr": handoff.upstream_pr_value(pr)},
            details={"pr_number": pr["number"], "url": pr["url"], "head": pr["head_sha"],
                     "author": pr["user"]["login"]})
        obs.staged.extend([("contributions", out.record), ("events", out.event)])
        entry["verified_upstream_pr"] = pr["number"]
        return
    key = handoff.observation_key(head, rec, rejected, len(matching))
    mismatch = handoff.mismatch_text(head, rec, rejected, len(matching))
    entry["handoff"] = {"observed_head": head, "mismatch": mismatch}
    if key is None or key in audited.get(cid, set()):
        return
    out = engine.apply_transition(
        rec, "record_audit_observation", policy=policy, now=now, cause_id=f"audit-{key}",
        facts={"unverified_upstream_observation"},
        details={"observation_key": key, "observed_head": head, "mismatch": mismatch})
    obs.staged.append(("events", out.event))
    audited.setdefault(cid, set()).add(key)
    entry["audit_observation"] = key


def observe(api: collect.Api, *, contributions: dict[str, dict], operations: dict[str, dict],
            contracts: dict[str, dict], policy: dict, now: str,
            evidence: dict[str, dict] | None = None,
            unadmitted_work: set[str] | None = None,
            skip: frozenset[str] = frozenset(),
            audited: dict[str, set[str]] | None = None) -> Observation:
    """Observe every watched contribution that is not in ``skip``.

    Each contribution is observed in its own ``isolation.contribution``
    block: a failed GitHub read, or any other exception, is attributed to
    the contribution that was being observed."""
    obs = Observation()
    evidence = dict(evidence or {})
    audited = {cid: set(keys) for cid, keys in (audited or {}).items()}
    for rec in contributions.values():
        if rec["phase"] not in WATCH_PHASES or not rec.get("fork"):
            continue
        cid = rec["contribution_id"]
        if cid in skip:
            continue
        with isolation.contribution(cid, "observation"):
            fork = rec["fork"]["name"]
            ref = watched_ref(rec)
            head = collect.branch_head(api, fork, ref)
            entry: dict[str, Any] = {"contribution": cid, "fork": fork, "ref": ref,
                                     "observed_head": head and head["head"]}
            # Uncertain branch operations are resolved by discovery first
            # (`confirm_by_discovery` or `record_discovery` of the operation
            # table). An operation the store holds as `applying` lost the
            # response of its push (the publisher records the response before
            # it hands the operation over): `record_response` makes it
            # uncertain, then it is discovered.
            for op in operations.values():
                if op["contribution_id"] == cid and op["status"] in ("applying", "uncertain") and \
                        op["kind"] in ("branch_update", "branch_rewrite"):
                    if op["status"] == "applying":
                        op = recovery.mark_result(op, response_ok=None, external_ids=None, now=now)
                    result = recovery.discover_branch(op, head)
                    new = recovery.record_discovery(op, result, f"{ref} at {head and head['head']}", now)
                    obs.staged.append(("operations", new))
                    operations[op["operation_id"]] = new
                    entry["operation"] = {"id": op["operation_id"], "discovery": result}
            if head is None:
                # Nothing is decided from a missing branch: it is reported,
                # and the next tick reads it again.
                entry["note"] = f"{ref} branch missing"
                obs.report.append(entry)
                continue
            cur = rec
            if human_wait_without_observation(rec):
                # A human wait is left only by the rows the table lists for it
                # (a resume, `park_contribution`, `refresh_investigation`,
                # `resume_stale`). It has no observation row: the head, CI,
                # and review are read again once a human has resumed it.
                entry["note"] = f"{engine.state_key(rec)} waits for a human and is not observed"
                obs.report.append(entry)
                continue
            if engine.state_key(rec) == "handoff.waiting_ci":
                _observe_clean_head_ci(api, obs, entry, rec, head["head"], contracts,
                                       policy=policy, now=now, evidence=evidence,
                                       operations=operations)
                obs.report.append(entry)
                continue
            if engine.state_key(rec) == "handoff.waiting_upstream_pr":
                _observe_upstream_pr(api, obs, entry, rec, head["head"], policy=policy, now=now,
                                     audited=audited)
                obs.report.append(entry)
                continue
            if rec["phase"] == "handoff" and engine.state_key(rec) != "handoff.awaiting_submission":
                # The other handoff states belong to the handoff operations; this
                # observation has no table row for them and changes nothing.
                entry["note"] = f"{engine.state_key(rec)} is not observed here"
                obs.report.append(entry)
                continue
            if head["head"] != lifecycle.recorded_head(rec) and rec["phase"] != "handoff" \
                    and holds_requested_work(rec):
                # The table records no head change while Work is requested or
                # its Result is held for publication: the Result is folded at
                # the revision it was requested for, and the head it published
                # is recorded by the observation after it.
                entry["note"] = "head change is recorded once the requested Work ends"
                obs.report.append(entry)
                continue
            if head["head"] != lifecycle.recorded_head(rec):
                if rec["phase"] == "handoff":
                    out = engine.apply_transition(
                        rec, "record_head_change", policy=policy, now=now,
                        cause_id=f"branch:{head['head'][:12]}", facts={"candidate_head_changed"},
                        values={"fork.head_sha": head["head"]},
                        details={"head": head["head"]})
                else:
                    out = compat.observe_head(rec, head["head"], now=now,
                                              cause_id=f"branch:{head['head'][:12]}")
                cur = out.record
                obs.staged.append(("contributions", cur))
                if out.event:
                    obs.staged.append(("events", out.event))
                entry["head_changed"] = True
            if cur["phase"] == "development" and cur["activity"] == "idle" \
                    and not cur.get("proposal_artifact") and not cur.get("publication_operation_id") \
                    and not cur.get("last_ci_evidence_id") and head["head"] == cur["upstream"].get("base_sha"):
                # The provisioned base is not a delivered contribution. Missing or
                # fork-guarded base CI must not suppress the initial patch task.
                entry["note"] = "awaiting initial development patch"
                obs.report.append(entry)
                continue
            at_gate = engine.state_key(cur) == "handoff.awaiting_submission"
            gate_review: tuple[str, dict, dict] | None = None
            if not cur.get("active_work") and cur["activity"] != "agent_running":
                feedback = collect.development_review_feedback(
                    api, cur, policy["command_authority"]["actor_ids"])
                if feedback:
                    inquiry_ids = (feedback["inquiry_comment_ids"]
                                   + feedback["inquiry_conversation_comment_ids"])
                    if inquiry_ids:
                        inquiry_items = (feedback["inquiry_comments"]
                                         + feedback["inquiry_conversation_comments"])
                        inquiry_id = safe_id(
                            f"inquiry-{cur['fork']['repository_id']}-{cur['fork']['pr_number']}-{max(inquiry_ids)}")
                        inquiry_data = {"comments": feedback["inquiry_comments"],
                                        "conversation_comments": feedback["inquiry_conversation_comments"]}
                        inquiry_ev = {"schema_version": 1, "evidence_id": inquiry_id,
                                      "contribution_id": cid, "kind": "human_inquiry",
                                      "policy_version": policy["policy_version"], "collected_at": now,
                                      "summary": "\n\n".join(item["body"] for item in inquiry_items)[:8000],
                                      "limitations": ["Explanation-only; no repair or mutation requested."],
                                      "data": inquiry_data, "digests": {"head": head["head"]}}
                        schemas.validate("evidence", inquiry_ev)
                        before_inquiry = cur
                        processed = {
                            "processed_review_ids": list(cur.get("processed_review_ids") or []),
                            "processed_review_comment_ids": (
                                (cur.get("processed_review_comment_ids") or [])
                                + feedback["inquiry_comment_ids"])[-512:],
                            "processed_pr_conversation_comment_ids": (
                                (cur.get("processed_pr_conversation_comment_ids") or [])
                                + feedback["inquiry_conversation_comment_ids"])[-512:]}
                        if at_gate:
                            # The gate's own row: the comment is recorded and the
                            # gate, the candidate head, and the CI evidence are kept.
                            explained = engine.apply_transition(
                                cur, "record_explanation", policy=policy, now=now,
                                cause_id=inquiry_id, facts={"explanation_inquiry"}, values=processed,
                                details={"evidence_id": inquiry_id,
                                         # Shown on the tracking issue by the ledger.
                                         "comments": [{"url": item.get("url"),
                                                       "body": item["body"][:500]}
                                                      for item in inquiry_items[:8]]})
                            if not explained.accepted:
                                raise lifecycle.TransitionError(
                                    f"record_explanation refused at the gate: {explained.reason}")
                            cur = explained.record
                            obs.staged.extend([("evidence", inquiry_ev), ("contributions", cur),
                                               ("events", explained.event)])
                        else:
                            cur = dict(cur, **processed, updated_at=now)
                            schemas.validate("contribution", cur)
                            obs.staged.extend([("evidence", inquiry_ev), ("contributions", cur),
                                               ("events", lifecycle._event(
                                                   before_inquiry, cur, "development_review_inquiry",
                                                   "observation", inquiry_id, now, False))])
                        entry["review_inquiry"] = inquiry_id
                    ids = feedback["review_ids"] + feedback["comment_ids"] + feedback["conversation_comment_ids"]
                    if ids:
                        eid = safe_id(f"review-{cur['fork']['repository_id']}-{cur['fork']['pr_number']}-{max(ids)}")
                        summaries = [item["body"] for item in
                                     feedback["reviews"] + feedback["comments"] + feedback["conversation_comments"]]
                        ev = {"schema_version": 1, "evidence_id": eid, "contribution_id": cid,
                              "kind": "human_relay", "policy_version": policy["policy_version"],
                              "collected_at": now, "summary": "\n\n".join(summaries)[:8000],
                              "limitations": [], "data": feedback, "digests": {"head": head["head"]}}
                        schemas.validate("evidence", ev)
                        if at_gate:
                            # At the gate the review is recorded by the table's
                            # row, once CI on the candidate head is read: a CI
                            # failure there takes `return_for_repair` instead.
                            gate_review = (eid, ev, feedback)
                        else:
                            before = cur
                            cur = compat.legacy_update(
                                       cur, "observe_ci.review_feedback",
                                       revision=cur["revision"] + 1, phase="development", activity="fix_needed",
                                       active_work=None, active_operation_id=None, clean_head_sha=None,
                                       handoff_request=None, handoff_approval=None, pending_human_action=None,
                                       last_ci_evidence_id=eid,
                                       processed_review_ids=((cur.get("processed_review_ids") or []) + feedback["review_ids"])[-256:],
                                       processed_review_comment_ids=((cur.get("processed_review_comment_ids") or [])
                                                                     + feedback["comment_ids"])[-512:],
                                       processed_pr_conversation_comment_ids=(
                                           (cur.get("processed_pr_conversation_comment_ids") or [])
                                           + feedback["conversation_comment_ids"])[-512:],
                                       pending_feedback_evidence_ids=list(dict.fromkeys(
                                           (cur.get("pending_feedback_evidence_ids") or []) + [eid]))[-64:],
                                       updated_at=now)
                            schemas.validate("contribution", cur)
                            obs.staged.extend([("evidence", ev), ("contributions", cur),
                                               ("events", lifecycle._event(before, cur, "development_review_feedback",
                                                                           "observation", eid, now, True))])
                            entry["review_feedback"] = eid
                    if not at_gate:
                        obs.report.append(entry)
                        continue
            # A requested repair must reach the controller before another CI poll
            # can reclassify the same unchanged head as waiting for a human.
            if cur["activity"] == "fix_needed":
                entry["note"] = "repair remains required"
                obs.report.append(entry)
                continue
            upstream_facing = cur.get("upstream_pr") is not None
            repo_for_ci = cur["upstream"]["full_name"] if upstream_facing else fork
            snap = collect.ci_snapshot(api, repo_for_ci, head["head"])
            ctr = contract_for(cur, contracts)
            verdict = collect.evaluate_snapshot(snap, required_job_patterns=ctr["required_job_patterns"],
                                                expected_wheels=ctr["expected_wheels"],
                                                required_runner_label=ctr["required_runner_label"])
            entry["ci"] = {"repository": repo_for_ci, "state": verdict.state, "problems": verdict.problems[:10]}
            latest = ci_evidence.latest_attempts([r for r in snap.runs if r["head_sha"] == head["head"]])
            next_poll_at = (_next_ci_observation(now, policy)
                            if verdict.state in {"pending", "missing", "not_executed"} else None)
            new_activity = {"passed": "idle", "failed": "fix_needed", "pending": "waiting_ci",
                            "missing": "waiting_ci", "not_executed": "waiting_ci",
                            "access_wait": "waiting_human"}[verdict.state]
            if verdict.state in ("passed", "failed", "missing", "not_executed") and latest:
                run = max(latest, key=lambda r: r["id"])
                eid = _evidence_id(snap.repository["id"], run)
                ev = ci_evidence.evidence_record(
                    evidence_id=eid, contribution_id=cid, policy_version=policy["policy_version"],
                    collected_at=now, repository=snap.repository, run=run,
                    jobs=[j for r in latest for j in snap.jobs_by_run.get(r["id"], [])],
                    base_sha=cur["upstream"].get("base_sha"), test_merge_sha=None,
                    expected_wheels=ctr["expected_wheels"],
                    artifacts=[], verdict=verdict,
                    debug_present=bool(cur.get("debug_commits")) and not upstream_facing,
                    limitations=[] if verdict.state == "passed" else ["see problems"])
                obs.staged.append(("evidence", ev))
                evidence[eid] = ev
                if verdict.state in {"passed", "failed"} and cur.get("last_ci_evidence_id") != eid:
                    cur = dict(cur, last_ci_evidence_id=eid, next_poll_at=None, updated_at=now)
                    obs.staged.append(("contributions", cur))
                if verdict.state == "failed" and cur["phase"] == "handoff":
                    # Only an executed failure on the candidate head leaves the gate.
                    returned = engine.apply_transition(
                        cur, "return_for_repair", policy=policy, now=now, cause_id=eid,
                        facts={"candidate_head_failed"}, values={"last_ci_evidence_id": eid},
                        details={"evidence_id": eid})
                    cur = returned.record
                    obs.staged.extend([("contributions", cur), ("events", returned.event)])
                    entry.update(activity="fix_needed", returned_to_development=True)
                    obs.report.append(entry)
                    continue
                # Plan 14.1 clears only obsolete active Work. A dispatched or
                # human-requested repair is still requested work on this head.
                work_active = bool(cur.get("active_work")) or cur["activity"] == "agent_running"
                if work_active and verdict.state == "passed" and cur["phase"] == "development" \
                        and not _obsolete_undispatched_ci_repair(cur, verdict, evidence, head["head"],
                                                            unadmitted_work):
                    entry["handoff_deferred"] = "requested Work is still active"
                elif verdict.state == "passed" and cur["phase"] == "development" \
                        and not cur.get("pending_publication") and not cur.get("active_operation_id"):
                    entry["evidence"] = eid
                    if cur.get("active_work"):
                        # Proven never admitted to the queue: withdrawn before the
                        # head it was requested for is nominated.
                        before = cur
                        cur = compat.legacy_update(cur, "observe_ci.withdraw_obsolete_repair",
                                                   active_work=None, activity="waiting_ci",
                                                   next_poll_at=None, updated_at=now)
                        obs.staged.append(("events", lifecycle._event(
                            before, cur, "obsolete_ci_repair_withdrawn", "observation", eid, now,
                            False)))
                        entry["obsolete_repair_withdrawn"] = True
                    elif engine.state_key(cur) != "development.waiting_ci":
                        # The table nominates from `development.waiting_ci`, where
                        # passing CI keeps a contribution; older paths leave it idle.
                        cur = compat.legacy_update(cur, "observe_ci.ci_passed", activity="waiting_ci")
                    nominated = engine.apply_transition(
                        cur, "nominate_for_handoff", policy=policy, now=now, cause_id=eid,
                        # Unprocessed actionable feedback on the development PR is
                        # folded above and ends this observation before CI is read.
                        facts={"ci_passed_on_head", "no_pending_actionable_feedback"},
                        values={"last_ci_evidence_id": eid,
                                "handoff_request": {"command_id": f"observation-{eid}",
                                                    "head": head["head"],
                                                    "revision": cur["revision"]}},
                        details={"head": head["head"], "revision": cur["revision"]})
                    if nominated.accepted:
                        cur = nominated.record
                        obs.staged.extend([("contributions", cur), ("events", nominated.event)])
                        entry["handoff_ready"] = True
                        obs.report.append(entry)
                        continue
                    entry["handoff_deferred"] = nominated.reason
                    new_activity = cur["activity"]
                    obs.staged.append(("contributions", cur))
                entry["evidence"] = eid
            if cur["phase"] == "handoff":
                if gate_review and verdict.state != "failed":
                    cur = _record_gate_review(obs, entry, cur, *gate_review, policy=policy, now=now)
                # Otherwise repeated observation of the candidate head keeps the gate.
                obs.report.append(entry)
                continue
            if _obsolete_undispatched_ci_repair(cur, verdict, evidence, head["head"], unadmitted_work):
                before = cur
                cur = compat.legacy_update(cur, "observe_ci.withdraw_obsolete_repair",
                                           active_work=None, activity="waiting_ci",
                                           next_poll_at=next_poll_at, updated_at=now)
                schemas.validate("contribution", cur)
                obs.staged.extend([
                    ("contributions", cur),
                    ("events", lifecycle._event(before, cur, "obsolete_ci_repair_withdrawn",
                                                "observation", entry.get("evidence"), now, False)),
                ])
                entry["obsolete_repair_withdrawn"] = True
            if cur["activity"] != new_activity and cur["activity"] != "agent_running":
                out = compat.observe_status(cur, activity=new_activity,
                                               next_poll_at=next_poll_at, now=now)
                if next_poll_at is None and out.record.get("next_poll_at") is not None:
                    out.record["next_poll_at"] = None
                obs.staged.append(("contributions", out.record))
                if out.event:
                    obs.staged.append(("events", out.event))
                entry.update(activity=new_activity)
            elif next_poll_at is not None and cur.get("next_poll_at") != next_poll_at \
                    and cur["activity"] != "agent_running":
                out = compat.observe_status(cur, next_poll_at=next_poll_at, now=now)
                obs.staged.append(("contributions", out.record))
            obs.report.append(entry)
    return obs


def dedupe(staged: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Keep the last version of each non-event record, all events in order."""
    from state import record_path
    last: dict[str, tuple[str, dict]] = {}
    events: list[tuple[str, dict]] = []
    for kind, rec in staged:
        if kind == "events":
            events.append((kind, rec))
        else:
            last[record_path(kind, rec)] = (kind, rec)
    return list(last.values()) + events
