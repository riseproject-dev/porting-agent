# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Turn live CI and branch state into lifecycle changes (plan sections 9, 14.3).

Deterministic, no model. For each contribution in a phase that watches CI:

* read the fork's current head of the branch the state watches: the
  development branch ``riscv64-dev`` in development and at the submission
  gate, and ``riscv64`` from CI on the clean head on; a missing branch is
  reported and changes nothing; resolve uncertain branch operations by
  discovery;
* in ``development.fix_needed``, take ``record_head_change`` for a fork head
  that moved while Work is due (back to ``development.waiting_ci``);
* in ``development.waiting_ci``, take the table's rows for what is observed:
  ``record_head_change``, ``record_pr_mismatch``, ``record_explanation``,
  ``record_actionable_review``, then for CI on the exact head
  ``record_ci_failure`` or ``nominate_for_handoff`` (passing CI that does not
  satisfy nomination keeps the state);
* at ``handoff.awaiting_submission``, take the gate's rows for what is
  observed on the candidate head: ``record_head_change``,
  ``record_explanation`` for an explanation-only comment, then
  ``return_for_repair`` for a CI failure, or else
  ``record_actionable_review`` for an actionable review;
* in ``handoff.waiting_ci``, take ``accept_clean_head_ci`` or
  ``return_for_repair`` for CI on the clean head of ``riscv64`` (CI of the
  candidate head when the clean head has its tree);
* in the locked ``handoff.waiting_upstream_pr``, discover the upstream PR
  read-only and take ``verify_upstream_pr`` for exactly one that matches, or
  record what does not match once (``record_audit_observation``);
* in upstream review, record a head change and set the activity from CI as
  the code before the table did (``compat``).

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
            if rec["phase"] == "development":
                _observe_development(api, obs, entry, rec, head["head"], contracts=contracts,
                                     policy=policy, now=now)
            elif rec["phase"] == "handoff":
                _observe_gate(api, obs, entry, rec, head["head"], contracts=contracts,
                              policy=policy, now=now)
            else:
                _observe_upstream_review(api, obs, entry, rec, head["head"], contracts=contracts,
                                         policy=policy, now=now)
            obs.report.append(entry)
    return obs


def _ci(api: collect.Api, record: dict, head: str, *, contracts: dict, policy: dict, now: str,
        repository: str) -> tuple[ci_evidence.CiVerdict, dict | None, str | None]:
    """CI on the exact ``head``: the verdict, the evidence record of its
    newest run (for a verdict that names one), and the next poll time while
    CI is incomplete."""
    snap = collect.ci_snapshot(api, repository, head)
    ctr = contract_for(record, contracts)
    verdict = collect.evaluate_snapshot(snap, required_job_patterns=ctr["required_job_patterns"],
                                        expected_wheels=ctr["expected_wheels"],
                                        required_runner_label=ctr["required_runner_label"])
    latest = ci_evidence.latest_attempts([r for r in snap.runs if r["head_sha"] == head])
    next_poll_at = (_next_ci_observation(now, policy)
                    if verdict.state in {"pending", "missing", "not_executed", "access_wait"}
                    else None)
    ev = None
    if verdict.state in ("passed", "failed", "missing", "not_executed") and latest:
        run = max(latest, key=lambda r: r["id"])
        ev = ci_evidence.evidence_record(
            evidence_id=_evidence_id(snap.repository["id"], run), contribution_id=record["contribution_id"],
            policy_version=policy["policy_version"], collected_at=now, repository=snap.repository,
            run=run, jobs=[j for r in latest for j in snap.jobs_by_run.get(r["id"], [])],
            base_sha=record["upstream"].get("base_sha"), test_merge_sha=None,
            expected_wheels=ctr["expected_wheels"], artifacts=[], verdict=verdict,
            debug_present=bool(record.get("debug_commits")) and record.get("upstream_pr") is None,
            limitations=[] if verdict.state == "passed" else ["see problems"])
    return verdict, ev, next_poll_at


def _take(obs: Observation, record: dict, verb: str, *, policy: dict, now: str,
          **kwargs: Any) -> dict:
    """Take a table row the observation established and stage the record
    and the event. A refusal here is a bug of the caller."""
    out = engine.apply_transition(record, verb, policy=policy, now=now, **kwargs)
    if not out.accepted:
        raise lifecycle.TransitionError(f"{verb} refused in {engine.state_key(record)}: {out.reason}")
    obs.staged.extend([("contributions", out.record), ("events", out.event)])
    return out.record


def _poll(obs: Observation, record: dict, next_poll_at: str | None) -> dict:
    """Record when CI is read again (bookkeeping; no state change)."""
    if record.get("next_poll_at") == next_poll_at:
        return record
    changed = dict(record, next_poll_at=next_poll_at)
    schemas.validate("contribution", changed)
    obs.staged.append(("contributions", changed))
    return changed


def _feedback(api: collect.Api, record: dict, head: str, *, policy: dict, now: str,
              check_pull: bool = False) -> tuple[tuple[str, dict, dict] | None, tuple[str, dict, dict, list] | None,
                             str | None]:
    """New authorized feedback on the development PR for the head: the
    actionable review (evidence id, evidence, feedback), the
    explanation-only inquiry (evidence id, evidence, the replay fences with
    its ids added, the comments), each or both ``None``, and why the
    development PR does not match the record (no feedback is read then)."""
    authority = policy["command_authority"]["actor_ids"]
    feedback = (collect.development_review_feedback(api, record, authority, check_pull=True)
                if check_pull else collect.development_review_feedback(api, record, authority))
    if feedback and feedback.get("pr_mismatch"):
        return None, None, str(feedback["pr_mismatch"])
    if not feedback:
        return None, None, None
    cid, fork = record["contribution_id"], record["fork"]
    inquiry = None
    inquiry_ids = feedback["inquiry_comment_ids"] + feedback["inquiry_conversation_comment_ids"]
    if inquiry_ids:
        items = feedback["inquiry_comments"] + feedback["inquiry_conversation_comments"]
        iid = safe_id(f"inquiry-{fork['repository_id']}-{fork['pr_number']}-{max(inquiry_ids)}")
        iev = {"schema_version": 1, "evidence_id": iid, "contribution_id": cid,
               "kind": "human_inquiry", "policy_version": policy["policy_version"],
               "collected_at": now,
               "summary": "\n\n".join(item["body"] for item in items)[:8000],
               "limitations": ["Explanation-only; no repair or mutation requested."],
               "data": {"comments": feedback["inquiry_comments"],
                        "conversation_comments": feedback["inquiry_conversation_comments"]},
               "digests": {"head": head}}
        schemas.validate("evidence", iev)
        processed = {
            "processed_review_ids": list(record.get("processed_review_ids") or []),
            "processed_review_comment_ids": ((record.get("processed_review_comment_ids") or [])
                                             + feedback["inquiry_comment_ids"])[-512:],
            "processed_pr_conversation_comment_ids": (
                (record.get("processed_pr_conversation_comment_ids") or [])
                + feedback["inquiry_conversation_comment_ids"])[-512:]}
        inquiry = (iid, iev, processed, items)
    review = None
    ids = feedback["review_ids"] + feedback["comment_ids"] + feedback["conversation_comment_ids"]
    if ids:
        eid = safe_id(f"review-{fork['repository_id']}-{fork['pr_number']}-{max(ids)}")
        summaries = [item["body"] for item in
                     feedback["reviews"] + feedback["comments"] + feedback["conversation_comments"]]
        ev = {"schema_version": 1, "evidence_id": eid, "contribution_id": cid,
              "kind": "human_relay", "policy_version": policy["policy_version"],
              "collected_at": now, "summary": "\n\n".join(summaries)[:8000],
              "limitations": [], "data": feedback, "digests": {"head": head}}
        schemas.validate("evidence", ev)
        review = (eid, ev, feedback)
    return review, inquiry, None


def _record_explanation(obs: Observation, entry: dict, record: dict,
                        inquiry: tuple[str, dict, dict, list], *, policy: dict, now: str) -> dict:
    """Take ``record_explanation``: the comment is recorded and its ids are
    added to the replay fences; revision and CI evidence are kept."""
    iid, iev, processed, items = inquiry
    obs.staged.append(("evidence", iev))
    entry["review_inquiry"] = iid
    return _take(obs, record, "record_explanation", policy=policy, now=now, cause_id=iid,
                 facts={"explanation_inquiry"}, values=processed,
                 details={"evidence_id": iid,
                          # Shown on the tracking issue by the ledger.
                          "comments": [{"url": item.get("url"), "body": item["body"][:500]}
                                       for item in items[:8]]})


def _record_review(obs: Observation, entry: dict, record: dict, review: tuple[str, dict, dict],
                   *, policy: dict, now: str, facts: frozenset[str]) -> dict | None:
    """Take ``record_actionable_review``: the review is recorded as pending
    feedback; the CI evidence is kept. The ids of the comments are added to
    the replay fences beside the row, so the same comments are not read
    again. ``None`` when the row does not apply."""
    eid, ev, feedback = review
    recorded = engine.apply_transition(
        record, "record_actionable_review", policy=policy, now=now, cause_id=eid,
        facts=facts,
        values={"pending_feedback_evidence_ids": list(dict.fromkeys(
            (record.get("pending_feedback_evidence_ids") or []) + [eid]))[-64:]},
        details={"evidence_id": eid})
    if not recorded.accepted:
        entry["review_feedback_deferred"] = recorded.reason
        return None
    new = dict(
        recorded.record,
        processed_review_ids=((record.get("processed_review_ids") or [])
                              + feedback["review_ids"])[-256:],
        processed_review_comment_ids=((record.get("processed_review_comment_ids") or [])
                                      + feedback["comment_ids"])[-512:],
        processed_pr_conversation_comment_ids=(
            (record.get("processed_pr_conversation_comment_ids") or [])
            + feedback["conversation_comment_ids"])[-512:])
    schemas.validate("contribution", new)
    obs.staged.extend([("evidence", ev), ("contributions", new), ("events", recorded.event)])
    entry.update(review_feedback=eid)
    return new


def _observe_development(api: collect.Api, obs: Observation, entry: dict, rec: dict, head: str,
                         *, contracts: dict, policy: dict, now: str) -> None:
    """Observe a contribution in development by the table's rows.

    Only ``development.waiting_ci`` has observation rows. There, in order:
    ``record_head_change`` for a new fork head; ``record_pr_mismatch`` for a
    development PR that does not match the record; ``record_explanation``
    for an explanation-only comment; ``record_actionable_review`` for an
    actionable review (the observation then ends in
    ``development.fix_needed``, which has no observation row); and, for CI
    on the exact head, ``record_ci_failure`` for an executed blocking
    failure of a run and attempt other than the one ``request_ci_rerun``
    answered (``budget.reruns.answered_ci_evidence_id``), or
    ``nominate_for_handoff`` when every required job passed and
    no actionable feedback is pending. Passing CI that does not satisfy
    nomination keeps the contribution in ``development.waiting_ci``, and
    a CI run that waits for approval is collector health, not a state.

    ``development.agent_running`` (requested Work, or a Result held for
    publication) and ``development.fix_needed`` (Work is due) are not
    observed; a fork head that moved while Work is due is reported in
    ``stale_head``, and no Work is allocated for it while it differs."""
    state = engine.state_key(rec)
    recorded = rec["fork"].get("head_sha")
    if state != "development.waiting_ci":
        if head != recorded and state == "development.fix_needed":
            entry["stale_head"] = head
            entry["note"] = ("the fork head differs from the recorded head while Work is due; no "
                             "row of development.fix_needed records it")
        else:
            entry["note"] = f"{state} has no observation row"
        return
    cur = rec
    if head != recorded:
        cur = _take(obs, cur, "record_head_change", policy=policy, now=now,
                    cause_id=f"branch:{head[:12]}", facts={"fork_head_changed"},
                    values={"fork.head_sha": head}, details={"head": head})
        entry["head_changed"] = True
    if cur["fork"].get("pr_number"):
        review, inquiry, mismatch = _feedback(api, cur, head, policy=policy, now=now,
                                              check_pull=True)
        if mismatch:
            _take(obs, cur, "record_pr_mismatch", policy=policy, now=now,
                  cause_id=f"pr-{cur['fork']['pr_number']}", facts={"development_pr_mismatch"},
                  details={"mismatch": mismatch[:500]})
            entry.update(activity="waiting_human", pr_mismatch=mismatch)
            return
        if inquiry:
            cur = _record_explanation(obs, entry, cur, inquiry, policy=policy, now=now)
        if review:
            # Recorded before CI is read: a CI failure observed with it has
            # no row in development.fix_needed, and the review would be
            # filtered out once the next head is published.
            if _record_review(obs, entry, cur, review, policy=policy, now=now,
                              facts=frozenset({"actionable_review"})) is not None:
                entry["activity"] = "fix_needed"
                return
    verdict, ev, next_poll_at = _ci(api, cur, head, contracts=contracts, policy=policy, now=now,
                                    repository=cur["fork"]["name"])
    entry["ci"] = {"repository": cur["fork"]["name"], "state": verdict.state,
                   "problems": verdict.problems[:10]}
    if ev is not None:
        obs.staged.append(("evidence", ev))
        entry["evidence"] = ev["evidence_id"]
    answered = engine.head_reruns(cur).get("answered_ci_evidence_id")
    if verdict.state == "failed" and ev is not None and ev["evidence_id"] == answered:
        # `request_ci_rerun` answered this run and attempt, and no trusted
        # rerun exists: only CI evidence of another run or attempt on the
        # head is acted on. Without a rerun, nothing produces it unless a
        # human reruns the CI or pushes.
        entry["note"] = (f"{ev['evidence_id']} is the CI run request_ci_rerun answered; CI "
                         "evidence of another run or attempt is awaited")
        _poll(obs, cur, _next_ci_observation(now, policy))
        return
    if verdict.state == "failed" and ev is not None:
        cur = _take(obs, cur, "record_ci_failure", policy=policy, now=now,
                    cause_id=ev["evidence_id"], facts={"ci_failed_on_head"},
                    values={"last_ci_evidence_id": ev["evidence_id"]},
                    details={"evidence_id": ev["evidence_id"]})
        _poll(obs, cur, None)
        entry["activity"] = "fix_needed"
        return
    if verdict.state == "passed" and ev is not None:
        if cur.get("active_operation_id"):
            entry["handoff_deferred"] = f"operation {cur['active_operation_id']} is open"
        else:
            nominated = engine.apply_transition(
                cur, "nominate_for_handoff", policy=policy, now=now, cause_id=ev["evidence_id"],
                facts={"ci_passed_on_head"},
                values={"last_ci_evidence_id": ev["evidence_id"],
                        "handoff_request": {"command_id": f"observation-{ev['evidence_id']}",
                                            "head": head, "revision": cur["revision"]}},
                details={"head": head, "revision": cur["revision"]})
            if nominated.accepted:
                cur = _poll(obs, nominated.record, None)
                obs.staged.append(("events", nominated.event))
                obs.staged.append(("contributions", cur))
                entry["handoff_ready"] = True
                return
            entry["handoff_deferred"] = nominated.reason
    if verdict.state == "access_wait":
        entry["note"] = "a CI run waits for approval; CI is read again"
    _poll(obs, cur, next_poll_at)


def _observe_gate(api: collect.Api, obs: Observation, entry: dict, rec: dict, head: str, *,
                  contracts: dict, policy: dict, now: str) -> None:
    """Observe ``handoff.awaiting_submission`` by the gate's rows:
    ``record_head_change`` for a new fork head (to ``development.waiting_ci``,
    which is then observed), ``record_explanation`` for an explanation-only
    comment, then ``return_for_repair`` for a CI failure on the candidate
    head, or else ``record_actionable_review`` for an actionable review.
    Passing CI keeps the gate and its recorded evidence."""
    cur = rec
    if head != rec["fork"].get("head_sha"):
        cur = _take(obs, cur, "record_head_change", policy=policy, now=now,
                    cause_id=f"branch:{head[:12]}", facts={"candidate_head_changed"},
                    values={"fork.head_sha": head}, details={"head": head})
        entry["head_changed"] = True
        _observe_development(api, obs, entry, cur, head, contracts=contracts, policy=policy,
                             now=now)
        return
    # The gate has no row for a development PR that does not match; it is
    # not read here.
    review, inquiry, _mismatch = _feedback(api, cur, head, policy=policy, now=now)
    if inquiry:
        # The gate's own row: the comment is recorded and the gate, the
        # candidate head, and the CI evidence are kept.
        cur = _record_explanation(obs, entry, cur, inquiry, policy=policy, now=now)
    verdict, ev, _next = _ci(api, cur, head, contracts=contracts, policy=policy, now=now,
                             repository=cur["fork"]["name"])
    entry["ci"] = {"repository": cur["fork"]["name"], "state": verdict.state,
                   "problems": verdict.problems[:10]}
    if ev is not None:
        obs.staged.append(("evidence", ev))
        entry["evidence"] = ev["evidence_id"]
    if verdict.state == "failed" and ev is not None:
        # Only an executed failure on the candidate head leaves the gate.
        _take(obs, cur, "return_for_repair", policy=policy, now=now, cause_id=ev["evidence_id"],
              facts={"candidate_head_failed"}, values={"last_ci_evidence_id": ev["evidence_id"]},
              details={"evidence_id": ev["evidence_id"]})
        entry.update(activity="fix_needed", returned_to_development=True)
        return
    if review:
        if _record_review(obs, entry, cur, review, policy=policy, now=now,
                          facts=frozenset({"actionable_review", "candidate_head_not_failed"})):
            entry["returned_to_development"] = True
    # Otherwise repeated observation of the candidate head keeps the gate.


def _observe_upstream_review(api: collect.Api, obs: Observation, entry: dict, rec: dict,
                             head: str, *, contracts: dict, policy: dict, now: str) -> None:
    """Observe upstream review as the code before the table did: a new fork
    head bumps the revision (``compat.observe_head``), and CI on the head
    sets the activity (``compat.observe_status``)."""
    cur = rec
    if head != lifecycle.recorded_head(rec) and holds_requested_work(rec):
        # The table records no head change while Work is requested or its
        # Result is held for publication: the Result is folded at the
        # revision it was requested for, and the head it published is
        # recorded by the observation after it.
        entry["note"] = "head change is recorded once the requested Work ends"
        return
    if head != lifecycle.recorded_head(rec):
        out = compat.observe_head(rec, head, now=now, cause_id=f"branch:{head[:12]}")
        cur = out.record
        obs.staged.append(("contributions", cur))
        if out.event:
            obs.staged.append(("events", out.event))
        entry["head_changed"] = True
    if cur["activity"] == "fix_needed":
        entry["note"] = "repair remains required"
        return
    snap_repo = cur["upstream"]["full_name"]
    verdict, ev, next_poll_at = _ci(api, cur, head, contracts=contracts, policy=policy, now=now,
                                    repository=snap_repo)
    entry["ci"] = {"repository": snap_repo, "state": verdict.state, "problems": verdict.problems[:10]}
    if ev is not None:
        obs.staged.append(("evidence", ev))
        entry["evidence"] = ev["evidence_id"]
        if verdict.state in {"passed", "failed"} and cur.get("last_ci_evidence_id") != ev["evidence_id"]:
            cur = dict(cur, last_ci_evidence_id=ev["evidence_id"], next_poll_at=None, updated_at=now)
            obs.staged.append(("contributions", cur))
    if verdict.state in ("pending", "missing", "not_executed"):
        next_poll_at = _next_ci_observation(now, policy)
    else:
        next_poll_at = None
    new_activity = {"passed": "idle", "failed": "fix_needed", "pending": "waiting_ci",
                    "missing": "waiting_ci", "not_executed": "waiting_ci",
                    "access_wait": "waiting_human"}[verdict.state]
    if cur["activity"] != new_activity and cur["activity"] != "agent_running":
        out = compat.observe_status(cur, activity=new_activity, next_poll_at=next_poll_at, now=now)
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
