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
  that moved while Work is due (to ``development.waiting_ci``, which is then
  observed); on the recorded head, read only the review feedback of the
  development PR and take ``record_actionable_review`` for an actionable
  review, so the Work requested next carries it;
* in ``development.waiting_ci``, take the table's rows for what is observed:
  ``record_head_change``, ``record_pr_mismatch``, ``record_explanation``,
  then for CI on the exact head ``record_ci_failure`` (followed by
  ``record_actionable_review`` in ``development.fix_needed`` for an
  actionable review observed with it), or else ``record_actionable_review``
  for an actionable review, ``rerun_interrupted_ci`` with a ``ci_rerun``
  operation (``exhaust_interrupted_reruns`` once its budget is spent), or
  ``nominate_for_handoff`` when the series meets the series requirements
  of the verified plan, or ``return_for_feedback`` to
  ``development.fix_needed`` when CI passed while actionable feedback is
  pending, or ``return_for_series_requirements`` to
  ``development.fix_needed`` when CI passed and a series requirement is
  unmet (passing CI behind an open operation keeps the state);
* at ``handoff.awaiting_submission``, take the gate's rows for what is
  observed on the candidate head: ``record_head_change``,
  ``record_explanation`` for an explanation-only comment, then
  ``return_for_repair`` for a CI failure, followed by
  ``record_actionable_review`` in ``development.fix_needed`` for an
  actionable review observed with it, or else ``record_actionable_review``
  for an actionable review;
* in ``handoff.waiting_ci``, take for CI on the clean head of ``riscv64``
  (CI of the candidate head when the clean head has its tree), read with
  jobs that lost their runner classified: ``rerun_interrupted_ci`` with a
  ``ci_rerun`` operation (``exhaust_interrupted_reruns`` once its budget is
  spent), ``accept_clean_head_ci`` (passing CI behind an open operation
  keeps the state), ``return_for_repair`` (a blocking failure records its
  evidence, a moved head keeps the recorded one), or ``require_clean_head_ci``
  once the bounded wait for CI that is not complete has passed;
* in the locked ``handoff.waiting_upstream_pr``, discover the upstream PR
  read-only and take ``verify_upstream_pr`` for exactly one that matches, or
  record what does not match once (``record_audit_observation``);
* in ``upstream_review.waiting_ci``, take the table's rows for the upstream
  PR: ``record_merge``, ``record_pr_closed_unmerged``, ``record_head_change``
  for a head the pipeline did not publish, ``record_explanation``,
  ``record_actionable_review``, and ``record_upstream_ci_failure`` once per
  head and CI run; upstream CI is never rerun;
* in ``release_wait.idle``, read the official release evidence and take
  ``verify_release``, ``require_release_intervention``, or
  ``record_release_incomplete`` (``release.decide``);
* read, without changing anything, the upstream PR of a record whose resume,
  or whose refresh from ``parked.idle``, depends on whether it is open
  (``reads_upstream_pr_state``).

A contribution that waits for a human (a state of kind ``gate``, ``locked``,
or ``human_wait``, ``engine.waits_for_human``) in a state without an
observation row is not observed: only the rows
the table lists for the wait leave it.

Returns staged records for the state publisher. Never writes remotely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import ci_evidence
import ci_rerun
import collect
import commit_rules
import engine
import handoff
import isolation
import lifecycle
import patch_review
import recovery
import release
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
    """The plan target contract: required job patterns + expected wheels
    (the field keeps its name; it is empty unless a contract names Python
    wheels).

    Plan 9.2 accepts exact-head CI when every required build/test job
    executed and passed, whatever the project's language. Uploaded workflow
    artifacts are not a development CI requirement: many projects build and
    test in PR CI but upload release artifacts only from release workflows.
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
    """Whether ``record`` waits for a human (``engine.waits_for_human``) in a
    state that has no observation row. Observation changes nothing there."""
    if not engine.waits_for_human(record):
        return False
    return not any(edge.transition.trigger == lifecycle.Trigger.OBSERVATION
                   for edge in lifecycle.state_edges(engine.state_key(record)))


def _observe_clean_head_ci(api: collect.Api, obs: Observation, entry: dict, rec: dict, head: str,
                           contracts: dict[str, dict], *, policy: dict, now: str,
                           evidence: dict[str, dict], operations: dict[str, dict]) -> None:
    """``handoff.waiting_ci``: CI on the clean head of `riscv64`.

    A `riscv64` head that is not the clean head takes ``return_for_repair``
    and keeps the recorded CI evidence of the candidate head.
    CI is a function of the tree: when the reviewed series has the tree of
    the candidate head, the CI of the candidate head is read
    (``handoff.ci_head``), with jobs that lost their runner classified as
    not executed. In order: nothing is decided while the snapshot is behind
    a confirmed rerun (``ci_rerun.awaited``); a required job that was
    cancelled or lost its runner, with no job failed on its own, takes
    ``rerun_interrupted_ci`` with a ``ci_rerun`` operation the handoff
    workflow executes (``exhaust_interrupted_reruns`` once its budget is
    spent); CI that is not complete keeps the wait until its bounded wait
    takes ``require_clean_head_ci``; passing CI takes
    ``accept_clean_head_ci``, except behind an open operation, which keeps
    the wait; a blocking failure takes ``return_for_repair`` and records its
    evidence as ``last_ci_evidence_id``, annotated with the candidate head it
    was built from when the clean head is another commit
    (``ci_evidence.for_clean_head``), so the repair names the failing job."""
    clean = rec["clean_head_sha"]
    if head != clean:
        # No CI verdict: the recorded evidence of the candidate head is kept.
        kept = rec.get("last_ci_evidence_id")
        out = engine.apply_transition(
            rec, "return_for_repair", policy=policy, now=now, cause_id=f"branch:{head[:12]}",
            facts={"clean_head_needs_repair"}, values={"last_ci_evidence_id": kept},
            details={"head": head, "clean_head": clean, "kept_evidence_id": kept})
        obs.staged.extend([("contributions", out.record), ("events", out.event)])
        entry.update(returned_to_development=True,
                     reason=f"the {UPSTREAM_REF} head is not the clean head")
        return
    clean = handoff.ci_head(rec, evidence.get(rec.get("submission_review_id") or "") or {})
    entry["ci_head"] = clean
    fork = rec["fork"]["name"]
    verdict, ev, _next_poll_at, snap = _ci_snapshot(
        api, rec, clean, contracts=contracts, policy=policy, now=now, repository=fork,
        classify_interrupted=True, debug_present=False)
    entry["ci"] = {"repository": fork, "state": verdict.state, "problems": verdict.problems[:10]}
    if ci_rerun.awaited(operations, rec["contribution_id"], clean, verdict.attempts):
        # A confirmed rerun recorded a newer attempt than this snapshot
        # shows: nothing is decided from the older one.
        entry["note"] = "CI on the clean head is behind a confirmed rerun"
        return
    if verdict.state == "not_executed" and verdict.interrupted_jobs and ev is not None:
        if _rerun_interrupted(obs, entry, rec, clean, ev, snap, operations, policy=policy,
                              now=now, poll=False):
            obs.staged.append(("evidence", ev))
            entry["evidence"] = ev["evidence_id"]
            return
    latest = ci_evidence.latest_attempts([r for r in snap.runs if r["head_sha"] == clean])
    if verdict.state not in ("passed", "failed") or not latest or ev is None:
        entry.setdefault("note", "CI on the clean head is not complete")
        if handoff.clean_head_ci_missing(rec, operations, now, has_runs=bool(latest)):
            # `riscv64` has no pull request: CI that was not dispatched, or
            # that never completes (also interrupted CI that could not be
            # rerun), waits for a human after a bounded wait.
            out = engine.apply_transition(
                rec, "require_clean_head_ci", policy=policy, now=now,
                cause_id=f"clean-head-ci-{clean[:12]}", facts={"clean_head_ci_missing"},
                details={"head": clean, "ci_state": verdict.state})
            obs.staged.extend([("contributions", out.record), ("events", out.event)])
            entry["waiting_for_human"] = "clean_head_ci_missing"
        return
    if verdict.state == "failed":
        # Failing evidence of a clean head that is another commit names the
        # candidate head it was built from.
        ev = ci_evidence.for_clean_head(ev, candidate=rec["fork"]["head_sha"], ref=UPSTREAM_REF,
                                        dev_ref=DEV_REF)
    eid = ev["evidence_id"]
    if verdict.state == "passed" and recovery.open_operations(
            rec["contribution_id"], operations, rec.get("active_operation_id")):
        # As in development: passing CI behind an open operation (a rerun
        # still to be read back) keeps the wait, bounded by its launches.
        active = rec.get("active_operation_id") or "of the contribution"
        entry["accept_deferred"] = f"operation {active} is open"
        return
    obs.staged.append(("evidence", ev))
    if verdict.state == "passed":
        out = engine.apply_transition(
            rec, "accept_clean_head_ci", policy=policy, now=now, cause_id=eid,
            facts={"ci_passed_on_clean_head"}, values={"last_ci_evidence_id": eid},
            details={"evidence_id": eid, "head": clean, "clean_head": rec["clean_head_sha"]})
    else:
        out = engine.apply_transition(
            rec, "return_for_repair", policy=policy, now=now, cause_id=eid,
            facts={"clean_head_needs_repair"}, values={"last_ci_evidence_id": eid},
            details={"evidence_id": eid, "head": clean,
                     "candidate_head": rec["fork"]["head_sha"]})
        entry["returned_to_development"] = True
    obs.staged.extend([("contributions", out.record), ("events", out.event)])
    entry["evidence"] = eid


def _observe_upstream_pr(api: collect.Api, obs: Observation, entry: dict, rec: dict, head: str,
                         *, policy: dict, now: str, audited: dict[str, set[str]]) -> None:
    """``handoff.waiting_upstream_pr``, the locked wait: read-only discovery
    of the upstream PR the package asked for.

    Exactly one candidate that satisfies every filter (``handoff.pull_mismatch``)
    takes ``verify_upstream_pr``. When none does, the candidates that satisfy
    every filter but the base and target the name GitHub now redirects the
    frozen base to (``collect.base_redirect``) are considered: a single one,
    whose branch still contains the recorded base commit
    (``collect.base_contained``), takes it too, and the record follows the
    new name: the upstream renamed its base branch before the PR was opened.
    A candidate on another branch never makes it ambiguous. Anything else
    that does not match (another fork head, a candidate that fails a
    filter, several matching candidates) is recorded
    once by ``record_audit_observation`` and never changes the state; the
    reminder states it."""
    cid = rec["contribution_id"]
    authority = policy["command_authority"]["actor_ids"]
    prs = collect.upstream_pull_requests(api, rec)
    matching, rejected = handoff.matching_upstream_prs(rec, prs, authority)
    base_ref = rec["upstream"].get("base_ref")
    if not matching:
        renamed = handoff.renamed_base_candidates(rec, prs, authority)
        # Only the candidates on the name GitHub redirects the frozen base
        # to count: one on another branch never makes it ambiguous.
        target = collect.base_redirect(api, rec) if renamed else None
        renamed = [pr for pr in renamed if target is not None and pr["base_ref"] == target]
        if len(renamed) == 1 and collect.base_contained(api, rec, target):
            matching, base_ref = renamed, target
    if len(matching) == 1:
        pr = matching[0]
        details = {"pr_number": pr["number"], "url": pr["url"], "head": pr["head_sha"],
                   "author": pr["user"]["login"]}
        if base_ref != rec["upstream"].get("base_ref"):
            details.update({"base_ref": rec["upstream"].get("base_ref"), "renamed_to": base_ref,
                            "base_sha": rec["upstream"].get("base_sha")})
        out = engine.apply_transition(
            rec, "verify_upstream_pr", policy=policy, now=now, cause_id=f"upstream-pr-{pr['id']}",
            facts={"upstream_pr_open_on_base", "upstream_pr_at_clean_head",
                   "upstream_pr_human_author"},
            values={"upstream_pr": handoff.upstream_pr_value(pr), "upstream.base_ref": base_ref},
            details=details)
        obs.staged.extend([("contributions", out.record), ("events", out.event)])
        entry["verified_upstream_pr"] = pr["number"]
        if base_ref != rec["upstream"].get("base_ref"):
            entry["base_branch_renamed"] = base_ref
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
            audited: dict[str, set[str]] | None = None, pypi=None) -> Observation:
    """Observe every watched contribution that is not in ``skip``.

    Each contribution is observed in its own ``isolation.contribution``
    block: a failed GitHub read, or any other exception, is attributed to
    the contribution that was being observed. ``pypi`` reads the PyPI
    document of a distribution (``collect.pypi_project`` by default)."""
    obs = Observation()
    evidence = dict(evidence or {})
    audited = {cid: set(keys) for cid, keys in (audited or {}).items()}
    for rec in contributions.values():
        cid = rec["contribution_id"]
        if cid in skip:
            continue
        if engine.state_key(rec) == "release_wait.idle":
            with isolation.contribution(cid, "observation"):
                entry = {"contribution": cid}
                _observe_release(api, obs, entry, rec, contracts=contracts, policy=policy,
                                 now=now, audited=audited, pypi=pypi or collect.pypi_project)
                obs.report.append(entry)
            continue
        if reads_upstream_pr_state(rec):
            with isolation.contribution(cid, "observation"):
                entry = {"contribution": cid,
                         "note": f"{engine.state_key(rec)}: the upstream PR is read for a resume only"}
                _observe_upstream_pr_state(api, entry, rec)
                obs.report.append(entry)
            continue
        if rec["phase"] not in WATCH_PHASES or not rec.get("fork"):
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
            discovered = []
            for op in list(operations.values()):
                if op["contribution_id"] == cid and op["status"] in ("applying", "uncertain") and \
                        op["kind"] in ("branch_update", "branch_rewrite"):
                    if op["status"] == "applying":
                        op = recovery.mark_result(op, response_ok=None, external_ids=None, now=now)
                    result = recovery.discover_branch(op, head)
                    new = recovery.record_discovery(op, result, f"{ref} at {head and head['head']}", now)
                    obs.staged.append(("operations", new))
                    operations[op["operation_id"]] = new
                    discovered.append(new)
                    entry["operation"] = {"id": op["operation_id"], "discovery": result}
            # A branch operation discovery confirms whose commit a
            # `publish_anyway` review pinned: the patch published anyway is
            # on the branch, so its comment is due, in any state. The
            # comment names the head discovery read, which a signed replay
            # confirmed by its tree and parents may have changed.
            rec = landed_comment(obs, entry, rec,
                                 patch_review.confirmed_heads(rec, discovered,
                                                              head and head["head"]),
                                 evidence, now=now)
            if rec["phase"] == "upstream_review" and not human_wait_without_observation(rec):
                # The upstream PR, not the branch, is what upstream review
                # observes: a merged PR whose branch was deleted is still
                # recorded. The branch is read only to discover its
                # operations above.
                _observe_upstream_review(api, obs, entry, rec, contracts=contracts,
                                         policy=policy, now=now)
                obs.report.append(entry)
                continue
            if head is None:
                # Nothing is decided from a missing branch: it is reported,
                # and the next tick reads it again.
                entry["note"] = f"{ref} branch missing"
                obs.report.append(entry)
                continue
            if human_wait_without_observation(rec):
                # A human wait is left only by the rows the table lists for it
                # (a resume, `park_contribution`, `refresh_planning`,
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
                                     policy=policy, now=now, operations=operations,
                                     evidence=evidence)
            else:
                _observe_gate(api, obs, entry, rec, head["head"], contracts=contracts,
                              policy=policy, now=now, operations=operations,
                              evidence=evidence)
            obs.report.append(entry)
    return obs


def _ci(api: collect.Api, record: dict, head: str, *, contracts: dict, policy: dict, now: str,
        repository: str) -> tuple[ci_evidence.CiVerdict, dict | None, str | None]:
    """CI on the exact ``head``: the verdict, the evidence record of its
    newest run (for a verdict that names one), and the next poll time while
    CI is incomplete. Jobs are not classified for a lost runner."""
    return _ci_snapshot(api, record, head, contracts=contracts, policy=policy, now=now,
                        repository=repository)[:3]


def _ci_snapshot(api: collect.Api, record: dict, head: str, *, contracts: dict, policy: dict,
                 now: str, repository: str, classify_interrupted: bool = False,
                 debug_present: bool | None = None
                 ) -> tuple[ci_evidence.CiVerdict, dict | None, str | None, collect.CiSnapshot]:
    """``_ci`` and the snapshot it read; with ``classify_interrupted`` a job
    that lost its runner did not execute (``collect.classify_runner_loss``).
    ``debug_present`` overrides what the evidence records about debug
    commits (by default, whether the record holds debug commits and no
    upstream PR)."""
    snap = collect.ci_snapshot(api, repository, head)
    if classify_interrupted:
        collect.classify_runner_loss(api, repository, snap)
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
            debug_present=(bool(record.get("debug_commits")) and record.get("upstream_pr") is None
                           if debug_present is None else debug_present),
            limitations=[] if verdict.state == "passed" else ["see problems"])
    return verdict, ev, next_poll_at, snap


def landed_comment(obs: Observation, entry: dict, record: dict,
                   heads: set[str] | dict[str, str],
                   evidence: dict[str, dict], *, now: str) -> dict:
    """Make due, beside the observation, the comment of every
    ``publish_anyway`` review whose pinned commit is one of ``heads``, the
    commits trusted branch evidence shows on the development branch
    (``patch_review.landed_anyway``), and stage the record. The comments
    made due, and any dropped to fit them, are named in the report, and the
    tick alerts on each dropped."""
    due, dropped = patch_review.comment_due(
        record, patch_review.landed_anyway(record, heads, evidence), now=now)
    if due == (record.get("review_comments") or []):
        return record
    before = {item["evidence_id"] for item in record.get("review_comments") or []}
    entry["review_comments_due"] = [item["evidence_id"] for item in due
                                    if item["evidence_id"] not in before]
    if dropped:
        entry["review_comments_dropped"] = [item["evidence_id"] for item in dropped]
    new = engine.record_beside(record, "review_comment_due", now=now, review_comments=due)
    obs.staged.append(("contributions", new))
    return new


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
    fork = record["fork"]
    review, inquiry = _feedback_evidence(record, feedback, policy=policy, now=now,
                                         repository_id=fork["repository_id"],
                                         number=fork["pr_number"], head=head)
    return review, inquiry, None


def _feedback_evidence(record: dict, feedback: dict, *, policy: dict, now: str,
                       repository_id: Any, number: int, head: str
                       ) -> tuple[tuple[str, dict, dict] | None, tuple[str, dict, dict, list] | None]:
    """The evidence of new authorized feedback on pull request ``number`` of
    the repository ``repository_id``: the actionable review (evidence id,
    evidence, feedback) and the explanation-only inquiry (evidence id,
    evidence, the replay fences with its ids added, the comments)."""
    cid = record["contribution_id"]
    inquiry = None
    inquiry_ids = feedback["inquiry_comment_ids"] + feedback["inquiry_conversation_comment_ids"]
    if inquiry_ids:
        items = feedback["inquiry_comments"] + feedback["inquiry_conversation_comments"]
        iid = safe_id(f"inquiry-{repository_id}-{number}-{max(inquiry_ids)}")
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
        eid = safe_id(f"review-{repository_id}-{number}-{max(ids)}")
        summaries = [item["body"] for item in
                     feedback["reviews"] + feedback["comments"] + feedback["conversation_comments"]]
        ev = {"schema_version": 1, "evidence_id": eid, "contribution_id": cid,
              "kind": "human_relay", "policy_version": policy["policy_version"],
              "collected_at": now, "summary": "\n\n".join(summaries)[:8000],
              "limitations": [], "data": feedback, "digests": {"head": head}}
        schemas.validate("evidence", ev)
        review = (eid, ev, feedback)
    return review, inquiry


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
    """Take ``record_actionable_review`` from the state of ``record`` (the
    gate, ``development.waiting_ci``, ``development.fix_needed``, or
    ``upstream_review.waiting_ci``): the review is recorded as pending
    feedback, and the row adds the ids of its comments to the replay fences,
    so the same comments are not read again; the CI evidence is kept.
    ``None`` when the row does not apply: the ids are then left unfenced and
    read again on the next tick."""
    eid, ev, feedback = review
    recorded = engine.apply_transition(
        record, "record_actionable_review", policy=policy, now=now, cause_id=eid,
        facts=facts,
        values={"pending_feedback_evidence_ids": list(dict.fromkeys(
                    (record.get("pending_feedback_evidence_ids") or []) + [eid]))[-64:],
                "processed_review_ids": ((record.get("processed_review_ids") or [])
                                         + feedback["review_ids"])[-256:],
                "processed_review_comment_ids": (
                    (record.get("processed_review_comment_ids") or [])
                    + feedback["comment_ids"])[-512:],
                "processed_pr_conversation_comment_ids": (
                    (record.get("processed_pr_conversation_comment_ids") or [])
                    + feedback["conversation_comment_ids"])[-512:]},
        details={"evidence_id": eid})
    if not recorded.accepted:
        entry["review_feedback_deferred"] = recorded.reason
        return None
    obs.staged.extend([("evidence", ev), ("contributions", recorded.record),
                       ("events", recorded.event)])
    entry.update(review_feedback=eid)
    return recorded.record


def _observe_development(api: collect.Api, obs: Observation, entry: dict, rec: dict, head: str,
                         *, contracts: dict, policy: dict, now: str,
                         operations: dict | None = None,
                         evidence: dict[str, dict] | None = None) -> None:
    """Observe a contribution in development by the table's rows.

    ``development.waiting_ci`` is observed by its rows, in order:
    ``record_head_change`` for a new fork head; ``record_pr_mismatch`` for a
    development PR that does not match the record; ``record_explanation``
    for an explanation-only comment; and, for CI on the exact head, read
    with jobs that lost their runner classified: ``record_ci_failure`` for
    an executed blocking failure, followed by ``record_actionable_review``
    in ``development.fix_needed`` for an actionable review observed with it,
    so the repair Work carries the failing job and the review;
    ``record_actionable_review`` for an actionable review when CI did not
    fail (the observation then ends in ``development.fix_needed``, and
    nothing else is decided from CI); nothing while the snapshot is behind
    a confirmed rerun (``ci_rerun.awaited``), but the review alone;
    ``rerun_interrupted_ci`` (or ``exhaust_interrupted_reruns``)
    when a required job was cancelled or lost its runner and no job failed
    on its own, ``nominate_for_handoff`` when every required job passed,
    no actionable feedback is pending, and the series meets the series
    requirements of the verified plan (``_series_requirements``),
    ``return_for_feedback`` when every required job passed while actionable
    feedback is pending: the passing evidence is recorded and Work carrying
    the feedback is due again in ``development.fix_needed``, or
    ``return_for_series_requirements`` when every required job passed
    without pending feedback and a series requirement is unmet: the unmet
    requirements are recorded as ``series_requirements`` evidence, which
    becomes the pending feedback of the Work due again. Passing CI behind
    an open operation keeps the contribution in ``development.waiting_ci``,
    and a CI run that waits for approval is collector health, not a state.

    ``development.agent_running`` (requested Work, or a Result held for
    publication) is not observed. ``development.fix_needed`` (Work is due)
    has two observation rows: a fork head that moved there (a human push)
    takes ``record_head_change`` to ``development.waiting_ci``, which is
    then observed in the same tick; on the recorded head, an actionable
    review of the development PR takes ``record_actionable_review`` and
    keeps the state, so the Work requested next carries it. There CI, the
    match of the development PR, and explanation-only comments are not read
    (the state has no row for them), and an inquiry is left unfenced."""
    operations = {} if operations is None else operations
    state = engine.state_key(rec)
    recorded = rec["fork"].get("head_sha")
    if state not in ("development.waiting_ci", "development.fix_needed"):
        entry["note"] = f"{state} has no observation row"
        return
    if state == "development.fix_needed" and head == recorded:
        if rec["fork"].get("pr_number"):
            review, _inquiry, _mismatch = _feedback(api, rec, head, policy=policy, now=now)
            if review:
                _record_review(obs, entry, rec, review, policy=policy, now=now,
                               facts=frozenset({"actionable_review"}))
        entry.update(activity="fix_needed", note="development Work is due on the recorded head")
        return
    cur = rec
    if head != recorded:
        cur = _take(obs, cur, "record_head_change", policy=policy, now=now,
                    cause_id=f"branch:{head[:12]}", facts={"fork_head_changed"},
                    values={"fork.head_sha": head}, details={"head": head})
        entry["head_changed"] = True
        cur = landed_comment(obs, entry, cur, {head}, evidence or {}, now=now)
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
    else:
        review = None
    verdict, ev, next_poll_at, snap = _ci_snapshot(
        api, cur, head, contracts=contracts, policy=policy, now=now,
        repository=cur["fork"]["name"], classify_interrupted=True)
    entry["ci"] = {"repository": cur["fork"]["name"], "state": verdict.state,
                   "problems": verdict.problems[:10]}
    awaited = ci_rerun.awaited(operations, cur["contribution_id"], head, verdict.attempts)
    if review and (awaited or verdict.state != "failed" or ev is None):
        # CI did not fail on the exact head (or is behind a confirmed
        # rerun): the review alone is recorded, and nothing is decided from
        # CI in this observation.
        if _record_review(obs, entry, cur, review, policy=policy, now=now,
                          facts=frozenset({"actionable_review", "head_not_failed"})) is not None:
            entry["activity"] = "fix_needed"
            return
    if awaited:
        # A confirmed rerun recorded a newer attempt than this snapshot
        # shows: CI is read again and nothing is decided from the older one.
        entry["note"] = "CI on the head is behind a confirmed rerun; CI is read again"
        _poll(obs, cur, _next_ci_observation(now, policy))
        return
    if ev is not None:
        obs.staged.append(("evidence", ev))
        entry["evidence"] = ev["evidence_id"]
    if verdict.state == "not_executed" and verdict.interrupted_jobs and ev is not None:
        _rerun_interrupted(obs, entry, cur, head, ev, snap, operations, policy=policy, now=now)
        return
    if verdict.state == "failed" and ev is not None:
        cur = _take(obs, cur, "record_ci_failure", policy=policy, now=now,
                    cause_id=ev["evidence_id"], facts={"ci_failed_on_head"},
                    values={"last_ci_evidence_id": ev["evidence_id"]},
                    details={"evidence_id": ev["evidence_id"]})
        if review:
            # The review observed with the failure is recorded in
            # development.fix_needed, so the repair Work carries the failing
            # job and the review: once the repair publishes, the review is
            # bound to an earlier head and would not be read again.
            cur = _record_review(obs, entry, cur, review, policy=policy, now=now,
                                 facts=frozenset({"actionable_review"})) or cur
        _poll(obs, cur, None)
        entry["activity"] = "fix_needed"
        return
    if verdict.state == "passed" and ev is not None:
        pending = list(cur.get("pending_feedback_evidence_ids") or [])
        if cur.get("active_operation_id"):
            entry["handoff_deferred"] = f"operation {cur['active_operation_id']} is open"
        elif pending:
            # Only a published Result clears pending feedback: Work that
            # carries it is due again rather than the nomination waiting.
            returned = engine.apply_transition(
                cur, "return_for_feedback", policy=policy, now=now, cause_id=ev["evidence_id"],
                facts={"ci_passed_on_head"}, values={"last_ci_evidence_id": ev["evidence_id"]},
                details={"evidence_id": ev["evidence_id"], "head": head,
                         "feedback": pending[:8]})
            if returned.accepted:
                obs.staged.extend([("contributions", returned.record),
                                   ("events", returned.event)])
                _poll(obs, returned.record, None)
                entry.update(activity="fix_needed", feedback_due=pending[:8])
                return
            entry["handoff_deferred"] = returned.reason
        else:
            unmet, note, sev = _series_requirements(api, cur, head, ev, evidence or {},
                                                    policy=policy, now=now)
            if sev is not None:
                returned = engine.apply_transition(
                    cur, "return_for_series_requirements", policy=policy, now=now,
                    cause_id=sev["evidence_id"],
                    facts={"ci_passed_on_head", "series_requirements_unmet"},
                    values={"last_ci_evidence_id": ev["evidence_id"],
                            "pending_feedback_evidence_ids": list(dict.fromkeys(
                                (cur.get("pending_feedback_evidence_ids") or [])
                                + [sev["evidence_id"]]))[-64:]},
                    details={"evidence_id": sev["evidence_id"], "head": head,
                             "series_requirements_unmet": unmet})
                if returned.accepted:
                    obs.staged.extend([("evidence", sev), ("contributions", returned.record),
                                       ("events", returned.event)])
                    _poll(obs, returned.record, None)
                    entry.update(activity="fix_needed", series_requirements_unmet=unmet)
                    return
                entry["handoff_deferred"] = returned.reason
                _poll(obs, cur, next_poll_at)
                return
            details = {"head": head, "revision": cur["revision"]}
            if note:
                details["series_requirements"] = note
            nominated = engine.apply_transition(
                cur, "nominate_for_handoff", policy=policy, now=now, cause_id=ev["evidence_id"],
                facts={"ci_passed_on_head", "series_requirements_met"},
                values={"last_ci_evidence_id": ev["evidence_id"],
                        "handoff_request": {"command_id": f"observation-{ev['evidence_id']}",
                                            "head": head, "revision": cur["revision"]}},
                details=details)
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


def _series_requirements(api: collect.Api, record: dict, head: str, ci: dict,
                         evidence: dict[str, dict], *, policy: dict, now: str
                         ) -> tuple[list[str], str | None, dict | None]:
    """Whether the series on the exact ``head`` meets the series
    requirements of the verified plan (``commit_rules.series_requirements_from_plan``):
    ``(unmet ids, note, evidence)``. The evidence, of kind
    ``series_requirements``, is returned when a requirement is unmet; its id
    is the same for the same head, so a restaged observation is idempotent.

    A plan that records none (every plan before schema version 4) needs no
    read. A series too large to read is not checked, and ``note`` says so.
    A plan the store does not hold records none, as for the commit rules
    of the Work payload. A malformed requirement is an unexpected error of
    the contribution; a failed read raises ``collect.CollectError``, which
    skips the contribution this tick."""
    plan_id = record.get("plan_evidence_id")
    plan = evidence.get(plan_id or "")
    try:
        requirements = commit_rules.series_requirements_from_plan((plan or {}).get("data"))
    except commit_rules.RuleError as error:
        raise lifecycle.TransitionError(
            f"the plan {plan_id} records malformed series requirements: {error}") from None
    if not requirements:
        return [], None, None
    paths = collect.series_changed_paths(api, record["fork"]["name"],
                                         record["upstream"]["base_sha"], head)
    if paths is None:
        return [], "not checked: the series is too large to read", None
    met, unmet = commit_rules.changed_paths_by_requirement(requirements, sorted(paths))
    if not unmet:
        return [], None, None
    repo_id = record["fork"].get("repository_id") or record["fork"]["name"].replace("/", "--")
    eid = safe_id(f"series-requirements-{repo_id}-{head[:12]}")
    lines = [f"- `{item['id']}`: change {', '.join(f'`{p}`' for p in item['paths'])}: "
             f"{isolation.clean_text(item['description'], 300)}" for item in unmet]
    ev = {"schema_version": 1, "evidence_id": eid, "contribution_id": record["contribution_id"],
          "kind": "series_requirements", "policy_version": policy["policy_version"],
          "collected_at": now,
          "summary": ("CI passed on the exact head, but the non-debug commits of the series do "
                      "not change a path the plan's contribution guidelines require. Change "
                      "a matching path in a non-debug commit for each requirement below, or "
                      "report blocked with the reason it does not apply:\n" + "\n".join(lines)),
          "limitations": ["A path changed only by a {debug} commit, or reverted later in the "
                          "series, does not count."],
          "data": {"head": head, "base_sha": record["upstream"]["base_sha"],
                   "ci_evidence_id": ci["evidence_id"],
                   "unmet": [{"id": item["id"], "paths": item["paths"],
                              "description": isolation.clean_text(item["description"], 300)}
                             for item in unmet],
                   "met": met},
          "digests": {"head": head}}
    schemas.validate("evidence", ev)
    return [item["id"] for item in unmet], None, ev


def _rerun_interrupted(obs: Observation, entry: dict, cur: dict, head: str, ev: dict,
                       snap: collect.CiSnapshot, operations: dict, *, policy: dict,
                       now: str, poll: bool = True) -> bool:
    """A required job on the exact head was cancelled or lost its runner,
    and no job failed on its own: ``rerun_interrupted_ci`` records a
    ``ci_rerun`` operation (the intent, before the effect) that the
    controller launches, within ``interrupted_reruns``;
    ``exhaust_interrupted_reruns`` waits for a human once that budget is
    spent. An open operation, a pause, or no completed run that holds an
    interrupted job keeps the state and reads CI again. Returns whether a
    row was taken. With ``poll`` the next CI observation is recorded
    (``next_poll_at``, which development records and handoff does not)."""
    later = _next_ci_observation(now, policy)

    def again(record: dict, at: str | None) -> dict:
        return _poll(obs, record, at) if poll else record

    if recovery.open_operations(cur["contribution_id"], operations,
                                cur.get("active_operation_id")):
        entry["note"] = (f"interrupted CI waits for the open operation "
                         f"{cur.get('active_operation_id') or 'of the contribution'}")
        again(cur, later)
        return False
    if cur.get("paused"):
        entry["note"] = "interrupted CI is not rerun while the contribution is paused"
        again(cur, later)
        return False
    facts = {"ci_interrupted_on_head"}
    cause = ev["evidence_id"]
    jobs = [str(j["name"])[:200] for j in ev["ci"]["jobs"] if j.get("interrupted")]
    details = {"evidence_id": cause, "head": head, "interrupted_jobs": jobs[:10]}
    if engine.refusal(cur, "rerun_interrupted_ci", policy=policy, now=now, facts=facts):
        cur = _take(obs, cur, "exhaust_interrupted_reruns", policy=policy, now=now,
                    cause_id=cause, facts=facts, details=details)
        again(cur, None)
        entry["waiting_for_human"] = "ci_interrupted"
        return True
    runs, _in_progress = ci_rerun.targets(snap, head, interrupted_only=True)
    if not runs:
        entry["note"] = "no completed run on the head holds an interrupted job; CI is read again"
        again(cur, later)
        return False
    count = engine.head_reruns(cur)["interrupted"]
    op = ci_rerun.operation(cur, head, runs, [cause, f"rerun:{count}"], policy, now,
                            ref=ci_rerun.target_ref(cur, head))
    if op["operation_id"] in operations:
        raise lifecycle.TransitionError(
            f"the CI rerun operation {op['operation_id']} of {cause} is already recorded")
    cur = _take(obs, cur, "rerun_interrupted_ci", policy=policy, now=now, cause_id=cause,
                facts=facts, details=details | {"operation": op["operation_id"],
                                                 "runs": runs})
    cur = engine.record_beside(cur, "operation_planned", now=now,
                               active_operation_id=op["operation_id"])
    operations[op["operation_id"]] = op
    obs.staged.append(("operations", op))
    cur = again(cur, later)
    obs.staged.append(("contributions", cur))
    entry.update(ci_rerun=op["operation_id"], activity="waiting_ci")
    return True


def _observe_gate(api: collect.Api, obs: Observation, entry: dict, rec: dict, head: str, *,
                  contracts: dict, policy: dict, now: str,
                  operations: dict | None = None,
                  evidence: dict[str, dict] | None = None) -> None:
    """Observe ``handoff.awaiting_submission`` by the gate's rows:
    ``record_head_change`` for a new fork head (to ``development.waiting_ci``,
    which is then observed), ``record_explanation`` for an explanation-only
    comment, then ``return_for_repair`` for a CI failure on the candidate
    head, or else ``record_actionable_review`` for an actionable review.
    The two guards are exclusive and the failure applies first; a review
    observed with the failure is recorded in the same observation by
    ``record_actionable_review`` in ``development.fix_needed``, so the repair
    Work allocated next carries the failing evidence and the review.
    Passing CI keeps the gate and its recorded evidence."""
    cur = rec
    if head != rec["fork"].get("head_sha"):
        cur = _take(obs, cur, "record_head_change", policy=policy, now=now,
                    cause_id=f"branch:{head[:12]}", facts={"candidate_head_changed"},
                    values={"fork.head_sha": head}, details={"head": head})
        entry["head_changed"] = True
        cur = landed_comment(obs, entry, cur, {head}, evidence or {}, now=now)
        _observe_development(api, obs, entry, cur, head, contracts=contracts, policy=policy,
                             now=now, operations=operations, evidence=evidence)
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
        cur = _take(obs, cur, "return_for_repair", policy=policy, now=now,
                    cause_id=ev["evidence_id"], facts={"candidate_head_failed"},
                    values={"last_ci_evidence_id": ev["evidence_id"]},
                    details={"evidence_id": ev["evidence_id"]})
        if review:
            # The review observed with the failure is recorded in
            # development.fix_needed: once the repair publishes, it is bound
            # to an earlier head and would not be read again.
            _record_review(obs, entry, cur, review, policy=policy, now=now,
                           facts=frozenset({"actionable_review"}))
        entry.update(activity="fix_needed", returned_to_development=True)
        return
    if review:
        if _record_review(obs, entry, cur, review, policy=policy, now=now,
                          facts=frozenset({"actionable_review", "candidate_head_not_failed"})):
            entry["returned_to_development"] = True
    # Otherwise repeated observation of the candidate head keeps the gate.


def _upstream_feedback(api: collect.Api, record: dict, *, policy: dict, now: str
                       ) -> tuple[tuple[str, dict, dict] | None, tuple[str, dict, dict, list] | None]:
    """New authorized feedback on the upstream PR for its recorded head: the
    actionable review and the explanation-only inquiry, each or both
    ``None``. The evidence ids name the upstream repository and PR."""
    feedback = collect.upstream_review_feedback(api, record, policy["command_authority"]["actor_ids"])
    if not feedback:
        return None, None
    return _feedback_evidence(record, feedback, policy=policy, now=now,
                              repository_id=record["upstream"].get("repository_id"),
                              number=record["upstream_pr"]["number"],
                              head=record["upstream_pr"]["head_sha"])


def _observe_upstream_review(api: collect.Api, obs: Observation, entry: dict, rec: dict, *,
                             contracts: dict, policy: dict, now: str) -> None:
    """Observe ``upstream_review.waiting_ci`` by its rows, in order:
    ``record_merge`` for a merged upstream PR, ``record_pr_closed_unmerged``
    for one closed without merge, ``record_head_change`` for a head other
    than the recorded one (the head the pipeline published is recorded by
    ``record_revision_publish_success``), ``record_explanation`` for an explanation-only comment,
    ``record_actionable_review`` for an actionable review (the observation
    then ends in ``upstream_review.fix_needed``), and
    ``record_upstream_ci_failure`` for an executed blocking failure of a CI
    run on the upstream PR head that is not recorded yet. Passing CI keeps
    the state; upstream CI is never rerun.

    ``upstream_review.fix_needed`` (revision Work is due) and
    ``upstream_review.agent_running`` (Work requested, or a Result held for
    publication) have no observation row and are not read."""
    state = engine.state_key(rec)
    if state != "upstream_review.waiting_ci":
        entry["note"] = f"{state} has no observation row"
        return
    pr = collect.upstream_pull(api, rec)
    entry["upstream_pr"] = {"number": pr["number"], "state": pr["state"], "merged": pr["merged"],
                            "head": pr["head_sha"]}
    cause = f"upstream-pr-{pr['id']}"
    if pr["merged"]:
        merge_sha, merged_at = pr.get("merge_commit_sha"), pr.get("merged_at")
        if not (isinstance(merge_sha, str) and len(merge_sha) == 40 and merged_at):
            raise collect.CollectError(f"upstream PR #{pr['number']} is merged without a merge "
                                       "commit or time")
        eid = safe_id(f"merge-{rec['upstream'].get('repository_id')}-{pr['number']}-{merge_sha[:12]}")
        ev = {"schema_version": 1, "evidence_id": eid, "contribution_id": rec["contribution_id"],
              "kind": "merge", "policy_version": policy["policy_version"], "collected_at": now,
              "summary": f"upstream PR #{pr['number']} merged into {pr['base_ref']} as {merge_sha}",
              "limitations": [], "data": {"pr_number": pr["number"], "merge_sha": merge_sha,
                                          "merged_at": merged_at, "base_ref": pr["base_ref"],
                                          "head_sha": pr["head_sha"]},
              "digests": {"head": pr["head_sha"]}}
        schemas.validate("evidence", ev)
        obs.staged.append(("evidence", ev))
        _take(obs, rec, "record_merge", policy=policy, now=now, cause_id=eid,
              facts={"upstream_pr_merged"},
              values={"merge": {"merge_sha": merge_sha, "merged_at": merged_at,
                                "target_ref": pr["base_ref"], "evidence_id": eid}},
              details={"evidence_id": eid, "merge_sha": merge_sha})
        entry["merged"] = merge_sha
        return
    if pr["state"] != "open":
        _take(obs, rec, "record_pr_closed_unmerged", policy=policy, now=now, cause_id=cause,
              facts={"upstream_pr_closed_unmerged"},
              values={"pending_human_action": "pr_closed_unmerged"},
              details={"pr_number": pr["number"]})
        entry["activity"] = "waiting_human"
        return
    cur = rec
    head = pr["head_sha"]
    if head != lifecycle.recorded_head(cur):
        # The head a revision of the pipeline published is recorded by
        # `record_revision_publish_success` when its Result is folded, so any
        # other head is a change the pipeline did not publish.
        _take(obs, cur, "record_head_change", policy=policy, now=now,
              cause_id=f"upstream-head:{head[:12]}",
              facts={"upstream_head_changed_externally"},
              values={"upstream_pr.head_sha": head},
              details={"head": head, "previous_head": lifecycle.recorded_head(cur)})
        entry.update(head_changed=True, activity="waiting_human")
        return
    review, inquiry = _upstream_feedback(api, cur, policy=policy, now=now)
    if inquiry:
        cur = _record_explanation(obs, entry, cur, inquiry, policy=policy, now=now)
    if review:
        if _record_review(obs, entry, cur, review, policy=policy, now=now,
                          facts=frozenset({"actionable_review"})) is not None:
            entry["activity"] = "fix_needed"
            return
    repository = cur["upstream"]["full_name"]
    verdict, ev, next_poll_at = _ci(api, cur, head, contracts=contracts, policy=policy, now=now,
                                    repository=repository)
    entry["ci"] = {"repository": repository, "state": verdict.state,
                   "problems": verdict.problems[:10]}
    if ev is not None:
        obs.staged.append(("evidence", ev))
        entry["evidence"] = ev["evidence_id"]
    if verdict.state == "failed" and ev is not None:
        if ev["evidence_id"] == cur.get("last_ci_evidence_id"):
            # Recorded once per head and CI run: a failure already recorded
            # does not request a revision again (after a resume, for
            # example). Upstream CI is never rerun by the pipeline.
            entry["note"] = f"{ev['evidence_id']} is already recorded for this head"
            _poll(obs, cur, None)
            return
        cur = _take(obs, cur, "record_upstream_ci_failure", policy=policy, now=now,
                    cause_id=ev["evidence_id"],
                    facts={"upstream_ci_failed", "upstream_failure_not_recorded"},
                    values={"last_ci_evidence_id": ev["evidence_id"]},
                    details={"evidence_id": ev["evidence_id"]})
        _poll(obs, cur, None)
        entry["activity"] = "fix_needed"
        return
    if verdict.state == "access_wait":
        entry["note"] = "a CI run waits for approval; CI is read again"
    _poll(obs, cur, next_poll_at)


def _observe_upstream_pr_state(api: collect.Api, entry: dict, rec: dict) -> None:
    """Read the upstream PR of a record whose resume or restart depends on
    it, without changing anything: ``resume_upstream_review`` after
    ``pr_closed_unmerged`` requires the PR open again, and
    ``restart_generation`` (a refresh from ``parked.idle``) requires no open
    upstream PR. The controller establishes those facts from
    ``entry["upstream_pr"]``. A failed read establishes nothing, so the
    command is refused, and keeps every other
    command of the tick (``park``, ``abandon``) from being held back."""
    try:
        pr = collect.upstream_pull(api, rec)
    except collect.CollectError as error:
        entry["upstream_pr_unavailable"] = str(error)[:300]
        return
    entry["upstream_pr"] = {"number": pr["number"], "state": pr["state"], "merged": pr["merged"],
                            "head": pr["head_sha"]}


def reads_upstream_pr_state(record: dict) -> bool:
    """Whether a record's upstream PR is read for a resume, or a refresh
    from ``parked.idle``, only (``_observe_upstream_pr_state``)."""
    if not record.get("upstream_pr"):
        return False
    state = engine.state_key(record)
    if state == "parked.idle":
        return True
    return state == "upstream_review.waiting_human" \
        and (record.get("pending_human_action") or {}).get("kind") == "pr_closed_unmerged"


# The rows of `release_wait.idle`, one of which ``release.decide`` names.
RELEASE_ROWS = ("record_release_incomplete", "require_release_intervention", "verify_release")


def _observe_release(api: collect.Api, obs: Observation, entry: dict, rec: dict, *,
                     contracts: dict, policy: dict, now: str, audited: dict[str, set[str]],
                     pypi) -> None:
    """Observe ``release_wait.idle``, at most every
    ``release.RELEASE_POLL_MINUTES`` minutes: the official release evidence
    decides ``verify_release``, ``require_release_intervention``, or
    ``record_release_incomplete``, which is recorded once per distinct
    observation (``release.decide``)."""
    if rec.get("next_poll_at") and rec["next_poll_at"] > now:
        entry["note"] = f"release evidence is read again at {rec['next_poll_at']}"
        return
    contract = release.acceptance_contract(contracts.get(rec.get("target_contract_id") or ""))
    found = {name: pypi(name) for name in (contract.distributions if contract else ())}
    merge = rec["merge"]
    decision = release.decide(
        contract, merge=merge, pypi=found,
        contains=lambda version: collect.release_contains(
            api, rec["upstream"]["full_name"], version, merge["merge_sha"]))
    if decision.verb not in RELEASE_ROWS:
        raise lifecycle.TransitionError(f"{decision.verb} is not a row of release_wait.idle")
    entry["release"] = {"verb": decision.verb, **decision.details}
    cause = f"release-{merge['merge_sha'][:12]}"
    if decision.verb == "record_release_incomplete":
        cid = rec["contribution_id"]
        if decision.key not in audited.get(cid, set()):
            out = engine.apply_transition(rec, decision.verb, policy=policy, now=now,
                                          cause_id=f"{cause}-{decision.key}",
                                          facts=decision.facts, details=decision.details)
            if not out.accepted:
                raise lifecycle.TransitionError(f"{decision.verb} refused: {out.reason}")
            obs.staged.append(("events", out.event))
            audited.setdefault(cid, set()).add(decision.key)
        _poll(obs, rec, (datetime.fromisoformat(now)
                         + timedelta(minutes=release.RELEASE_POLL_MINUTES)).isoformat())
        return
    values = dict(decision.values)
    if decision.verb == "verify_release":
        eid = safe_id(f"release-{rec['contribution_id'].replace('/', '--')}-{merge['merge_sha'][:12]}"
                      f"-{'-'.join(sorted(decision.details['releases'].values()))}")
        ev = {"schema_version": 1, "evidence_id": eid, "contribution_id": rec["contribution_id"],
              "kind": "release_acceptance", "policy_version": policy["policy_version"],
              "collected_at": now,
              "summary": "official release coverage satisfies the acceptance contract: "
                         + ", ".join(f"{n} {v}" for n, v in sorted(decision.details["releases"].items())),
              "limitations": ["Metadata coverage and tag provenance; the wheels are not installed."],
              "data": decision.details, "digests": {"merge": merge["merge_sha"]}}
        schemas.validate("evidence", ev)
        obs.staged.append(("evidence", ev))
        values["release_acceptance"] = {"evidence_id": eid, "as_of": now, "complete": True}
        cause = eid
    cur = _take(obs, rec, decision.verb, policy=policy, now=now, cause_id=cause,
                facts=decision.facts, values=values, details=decision.details)
    _poll(obs, cur, None)


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
