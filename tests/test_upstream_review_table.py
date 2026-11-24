# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Upstream review and release on the table's rows.

``upstream_review.waiting_ci`` is observed by its rows (merge, closure,
head change, explanation, actionable review, CI failure once per head and
run); the Result of revision Work is folded by the rows of
``upstream_review.agent_running``; revision Work is allocated by the rows of
``upstream_review.fix_needed``; nothing parks from upstream review without a
command; ``release_wait.idle`` is observed by its rows.
"""

from __future__ import annotations

import contextlib
import copy
import io
import unittest
from datetime import datetime, timedelta
from unittest import mock

import capacity
import ci_evidence
import collect
import commands
import engine
import fork_publish
import lifecycle
import reconcile
import recovery
import release
import review_response
import schemas
import status_ledger
from tests import helpers
from tests.helpers import NOW, OWNER, SHA_A, SHA_B, SHA_C, apply_command, contribution, policy
from tests.test_late_results import PENDING, WORK, ci_snapshot, publication

T = datetime.fromisoformat("2026-11-23T09:00:00+00:00")
POLICY = policy()
UPSTREAM_PR = {"id": 9, "number": 9, "url": "https://github.com/example/proj/pull/9",
               "head_repository_id": 2, "head_ref": "riscv64", "head_sha": SHA_A,
               "base_ref": "main", "registered_by": OWNER}
REVIEW = {"review_ids": [77], "comment_ids": [], "conversation_comment_ids": [],
          "reviews": [{"id": 77, "body": "Please keep the aarch64 row."}], "comments": [],
          "conversation_comments": [], "inquiry_comment_ids": [],
          "inquiry_conversation_comment_ids": [], "inquiry_comments": [],
          "inquiry_conversation_comments": []}
INQUIRY = dict(REVIEW, review_ids=[], reviews=[], inquiry_comment_ids=[88],
               inquiry_comments=[{"id": 88, "body": "Why riscv64 @maintainer?",
                                  "url": "https://github.com/example/proj/pull/9#c88"}])
MERGE = {"merge_sha": "e" * 40, "merged_at": "2026-11-20T09:00:00+00:00", "target_ref": "main",
         "evidence_id": "merge-1"}
REVISION_WORK = dict(WORK, task="upstream_revision", dedupe_key="example--proj/12@0:upstream_revision")


def record(activity="waiting_ci", **fields):
    rec = contribution(phase="upstream_review", activity=activity, plan_evidence_id="plan-1",
                       scope_paths=["a"], scope_coverage_complete=True,
                       upstream_pr=copy.deepcopy(UPSTREAM_PR), **fields)
    schemas.validate("contribution", rec)
    return rec


def released_wait(**fields):
    rec = contribution(phase="release_wait", activity="idle", upstream_pr=copy.deepcopy(UPSTREAM_PR),
                       merge=dict(MERGE), target_contract_id=fields.pop("contract", None), **fields)
    schemas.validate("contribution", rec)
    return rec


def pull(head=SHA_A, state="open", merged=False):
    return {"id": 9, "number": 9, "state": state, "merged": merged,
            "merged_at": "2026-11-23T08:00:00Z" if merged else None,
            "merge_commit_sha": "e" * 40 if merged else None, "base_ref": "main",
            "base_repository_id": 1, "head_ref": "riscv64", "head_sha": head,
            "head_repository_id": 2}


def tick(rec, publications=(), *, verdict="pending", pr=None, branch=SHA_A, feedback=None,
         now=T, evidence=None, operations=None, contracts=(), pypi=None, contains=None,
         audited=None, comments=(), capacity_ok=False, snap=None):
    evidence = dict(evidence or {})
    evidence.update({c["evidence_id"]: c for c in contracts})
    snapshot = reconcile.Snapshot({"c": copy.deepcopy(rec)}, copy.deepcopy(operations or {}), {},
                                  {}, evidence, audited or {})
    reads = []

    def upstream_pull(_api, _record):
        reads.append("pull")
        return copy.deepcopy(pr or pull())

    head = {"head": branch, "tree": "t" * 40, "parents": [SHA_C]} if branch else None
    with mock.patch("observe_ci.collect.branch_head", return_value=head), \
            mock.patch("observe_ci.collect.upstream_pull", side_effect=upstream_pull), \
            mock.patch("observe_ci.collect.upstream_review_feedback",
                       return_value=copy.deepcopy(feedback)), \
            mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
            mock.patch("observe_ci.collect.ci_snapshot",
                       return_value=snap or ci_snapshot((pr or pull())["head_sha"], verdict)), \
            mock.patch("observe_ci.collect.pypi_project",
                       side_effect=lambda name: copy.deepcopy((pypi or {}).get(name))), \
            mock.patch("observe_ci.collect.release_contains",
                       side_effect=lambda _api, _repo, version, _sha: (contains or {}).get(version)), \
            contextlib.redirect_stderr(io.StringIO()):
        out = reconcile.tick(helpers.NoReadApi(), snapshot, policy=POLICY, now=now,
                             publications=list(publications), comments=list(comments),
                             pressure=capacity.Pressure(ok=capacity_ok, reason="test"),
                             observe=True,
                             dispatcher_runs=[], queue_works={})
    out.reads = reads
    return out


def run_snapshot(*runs, head=SHA_A):
    """A CI snapshot of ``head`` from ``(run id, attempt, job id, conclusion)``
    tuples: one run with one required job each."""
    run_rows, jobs = [], {}
    for run_id, attempt, job_id, conclusion in runs:
        run_rows.append({"id": run_id, "head_sha": head, "run_attempt": attempt,
                         "status": "completed", "conclusion": conclusion, "workflow_id": 1,
                         "event": "pull_request", "path": ".github/workflows/lint.yml"})
        jobs[run_id] = [{"id": job_id, "run_id": run_id, "name": "Build - ubuntu-24.04-riscv (riscv64)",
                         "status": "completed", "conclusion": conclusion, "started_at": "x",
                         "labels": ["ubuntu-24.04-riscv"]}]
    return collect.CiSnapshot(repository={"id": 2, "full_name": "riseproject-upstreaming/proj"},
                              head_sha=head, runs=run_rows, jobs_by_run=jobs, artifacts_by_run={})


class StaticApi:
    """An API that serves fixed lists for the review reads of the upstream PR."""

    def __init__(self, reviews, comments, conversation):
        self.lists = {"reviews": reviews, "pulls/9/comments": comments,
                      "issues/9/comments": conversation}

    def paginate(self, path, **_kwargs):
        for key, items in self.lists.items():
            if f"/{key}" in path or (key == "reviews" and path.endswith("/reviews?per_page=100")):
                return copy.deepcopy(items)
        return []

    def get(self, _path, **_kwargs):
        return {"commit": {"committer": {"date": "2026-11-20T00:00:00Z"}}}


def branch_operation(rec, head, *, confirmed=True):
    """The branch operation of a publication to `riscv64` that put ``head``
    there: confirmed by reading the branch back, or uncertain."""
    op = recovery.plan(contribution_id=rec["contribution_id"], revision=rec["revision"],
                       kind="branch_update", policy_version=POLICY["policy_version"],
                       reason_ids=[f"publish:{head[:12]}"],
                       target={"repository_id": 2, "repository": "riseproject-upstreaming/proj",
                               "ref": "riscv64", "expected_head": SHA_A, "base_sha": SHA_C},
                       proposed={"commit": head, "tree": "d" * 40, "parents": [SHA_A],
                                 "marker": None, "content_digest": "0" * 64,
                                 "changed_files": ["a"]}, now=NOW)
    op = recovery.mark_applying(op, NOW, facts=fork_publish.PUSH_FACTS)
    op = recovery.mark_result(op, response_ok=None if not confirmed else True,
                              external_ids={}, now=NOW)
    if confirmed:
        op = recovery.record_discovery(op, "confirmed", f"riscv64 at {head}", NOW)
    return op


def published(rec, head, **fields):
    """A published Result of revision Work whose push put ``head`` on `riscv64`."""
    op = branch_operation(rec, head)
    return dict(publication(rec, "published"), candidate=head, operation=op, **fields)


def final(output, rec):
    staged = [r for kind, r in output.staged if kind == "contributions"]
    return staged[-1] if staged else rec


def kinds(output):
    return [e["kind"] for kind, e in output.staged if kind == "events"]


def staged(output, kind):
    return [r for k, r in output.staged if k == kind]


class TestObservation(unittest.TestCase):
    """The observation matrix of upstream review, per state."""

    def test_waiting_ci_takes_one_row_per_observation(self):
        cases = {
            "merged": (dict(pr=pull(merged=True, state="closed")), ["record_merge"],
                       "release_wait.idle"),
            "closed": (dict(pr=pull(state="closed")), ["record_pr_closed_unmerged"],
                       "upstream_review.waiting_human"),
            "head moved": (dict(pr=pull(head=SHA_B), branch=SHA_B), ["record_head_change"],
                           "upstream_review.waiting_human"),
            "review": (dict(feedback=REVIEW, verdict="failed"), ["record_actionable_review"],
                       "upstream_review.fix_needed"),
            "explanation": (dict(feedback=INQUIRY, verdict="passed"), ["record_explanation"],
                            "upstream_review.waiting_ci"),
            "ci failed": (dict(verdict="failed"), ["record_upstream_ci_failure"],
                          "upstream_review.fix_needed"),
            "ci passed": (dict(verdict="passed"), [], "upstream_review.waiting_ci"),
            "ci pending": (dict(verdict="pending"), [], "upstream_review.waiting_ci"),
        }
        for name, (kwargs, verbs, state) in cases.items():
            with self.subTest(case=name):
                rec = record(last_ci_evidence_id=None)
                out = tick(rec, **kwargs)
                self.assertEqual(out.failures, [])
                self.assertEqual(kinds(out), verbs)
                after = final(out, rec)
                self.assertEqual(engine.state_key(after), state)
                self.assertFalse(after["paused"])

    def test_pending_ci_changes_no_contribution_on_consecutive_ticks(self):
        rec = record(last_ci_evidence_id=None)
        for _ in range(2):
            out = tick(rec, verdict="pending")
            self.assertEqual(staged(out, "contributions"), [])
            rec = final(out, rec)
        self.assertEqual(engine.state_key(rec), "upstream_review.waiting_ci")

    def test_the_rows_record_what_the_table_says(self):
        rec = record()
        merged = final(tick(rec, pr=pull(merged=True, state="closed")), rec)
        self.assertEqual(merged["merge"]["merge_sha"], "e" * 40)
        self.assertEqual(merged["merge"]["target_ref"], "main")
        out = tick(rec, pr=pull(merged=True, state="closed"))
        [ev] = staged(out, "evidence")
        self.assertEqual((ev["kind"], merged["merge"]["evidence_id"]), ("merge", ev["evidence_id"]))

        closed = final(tick(rec, pr=pull(state="closed")), rec)
        self.assertEqual(closed["pending_human_action"]["kind"], "pr_closed_unmerged")
        self.assertEqual(closed["revision"], rec["revision"])

        moved = final(tick(dict(rec, last_ci_evidence_id="ci-old"), pr=pull(head=SHA_B),
                           branch=SHA_B), rec)
        self.assertEqual((moved["upstream_pr"]["head_sha"], moved["revision"],
                          moved["last_ci_evidence_id"], moved["pending_human_action"]["kind"]),
                         (SHA_B, rec["revision"] + 1, None, "upstream_head_changed"))
        self.assertEqual(moved["fork"], rec["fork"])

        reviewed = final(tick(rec, feedback=REVIEW), rec)
        self.assertEqual(reviewed["revision"], rec["revision"] + 1)
        self.assertEqual(len(reviewed["pending_feedback_evidence_ids"]), 1)
        self.assertTrue(reviewed["pending_feedback_evidence_ids"][0].startswith("review-1-9-"))
        self.assertEqual(reviewed["processed_review_ids"], [77])

        explained = final(tick(rec, feedback=INQUIRY, verdict="passed"), rec)
        self.assertEqual((explained["revision"], explained["processed_review_comment_ids"]),
                         (rec["revision"], [88]))

        failed = final(tick(rec, verdict="failed"), rec)
        self.assertEqual((failed["last_ci_evidence_id"], failed["revision"]),
                         ("ci-2-777-1", rec["revision"]))

    def test_a_failure_is_recorded_once_per_head_and_run(self):
        rec = record(last_ci_evidence_id="ci-2-777-1")
        out = tick(rec, verdict="failed")
        self.assertEqual(kinds(out), [])
        self.assertEqual(engine.state_key(final(out, rec)), "upstream_review.waiting_ci")

    def test_feedback_from_outside_the_command_authority_is_not_recorded(self):
        def items(user):
            return ([{"id": 1, "body": "Please change this.", "commit_id": SHA_A,
                      "state": "CHANGES_REQUESTED", "user": dict(user)}],
                    [{"id": 2, "body": "Please rename it.", "commit_id": SHA_A, "path": "a",
                      "line": 1, "user": dict(user)}],
                    [{"id": 3, "body": "Please add a test.", "created_at": "2026-11-21T00:00:00Z",
                      "user": dict(user)}])
        rec = record()
        stranger = {"id": 999, "login": "maintainer"}
        self.assertIsNone(collect.upstream_review_feedback(StaticApi(*items(stranger)), rec,
                                                           [OWNER["id"]]))
        found = collect.upstream_review_feedback(StaticApi(*items(OWNER)), rec, [OWNER["id"]])
        self.assertEqual((found["review_ids"], found["comment_ids"],
                          found["conversation_comment_ids"]), ([1], [2], [3]))

    def test_a_rerun_of_the_failing_run_is_recorded_and_a_new_passing_run_is_not(self):
        rec = record(last_ci_evidence_id=None)
        first = run_snapshot((777, 1, 1, "failure"), (778, 1, 2, "success"))
        out = tick(rec, snap=first)
        self.assertEqual(kinds(out), ["record_upstream_ci_failure"])
        recorded = final(out, rec)
        self.assertEqual(recorded["last_ci_evidence_id"], "ci-2-778-1")
        store = {e["evidence_id"]: e for e in staged(out, "evidence")}
        # A new passing run has a higher id, and the failure is recorded.
        newer = run_snapshot((777, 1, 1, "failure"), (778, 1, 2, "success"),
                             (779, 1, 3, "success"))
        waiting = dict(recorded, activity="waiting_ci")
        again = tick(waiting, snap=newer, evidence=store)
        self.assertEqual(kinds(again), [])
        self.assertEqual(engine.state_key(final(again, waiting)), "upstream_review.waiting_ci")
        # A rerun of the failing run has new job ids and fails again: recorded
        # anew, while the newest run is still 778.
        rerun = run_snapshot((777, 2, 5, "failure"), (778, 1, 2, "success"))
        out = tick(waiting, snap=rerun, evidence=store)
        self.assertEqual(kinds(out), ["record_upstream_ci_failure"])
        self.assertEqual(final(out, waiting)["last_ci_evidence_id"], "ci-2-778-1")
        # Without the recorded evidence in the store the evidence ids are compared.
        out = tick(waiting, snap=first)
        self.assertEqual(kinds(out), [])

    def test_the_recorded_failure_is_judged_by_its_failing_jobs(self):
        def evidence(eid, head, *jobs):
            return {"evidence_id": eid, "kind": "ci", "ci": {"head_sha": head, "jobs": [
                {"job_id": job, "name": "build", "executed": True, "conclusion": conclusion}
                for job, conclusion in jobs]}}
        old = evidence("ci-2-778-1", SHA_A, (1, "failure"), (2, "success"))
        self.assertEqual(ci_evidence.failed_job_ids(old), frozenset({1}))
        self.assertEqual(ci_evidence.failed_job_ids(None), frozenset())
        self.assertEqual(ci_evidence.failed_job_ids({"kind": "review"}), frozenset())
        publish = evidence("x", SHA_A, (9, "failure"))
        publish["ci"]["jobs"][0]["name"] = "Publish to PyPI"
        self.assertEqual(ci_evidence.failed_job_ids(publish), frozenset())
        same = evidence("ci-2-779-1", SHA_A, (1, "failure"), (3, "success"))
        rerun = evidence("ci-2-778-1", SHA_A, (5, "failure"))
        other_head = evidence("ci-2-778-1", SHA_B, (1, "failure"))
        self.assertTrue(ci_evidence.failure_recorded(same, old))
        self.assertFalse(ci_evidence.failure_recorded(rerun, old))
        # Another head, a failure without a failing job, or no recorded evidence
        # fall back to the evidence ids.
        self.assertTrue(ci_evidence.failure_recorded(other_head, old))
        self.assertFalse(ci_evidence.failure_recorded(dict(other_head, evidence_id="z"), old))
        nameless = evidence("ci-2-778-1", SHA_A, (1, "success"))
        self.assertTrue(ci_evidence.failure_recorded(nameless, old))
        self.assertFalse(ci_evidence.failure_recorded(same, None))

    def test_a_merged_pr_is_recorded_without_its_branch(self):
        rec = record()
        out = tick(rec, pr=pull(merged=True, state="closed"), branch=None)
        self.assertEqual(kinds(out), ["record_merge"])

    def test_a_head_other_than_the_recorded_one_is_an_external_change(self):
        # Even with a confirmed publication operation that put the head on
        # `riscv64`: only the row that folds its Result records that head.
        op = branch_operation(record(), SHA_B)
        rec = record(publication_operation_id=op["operation_id"])
        out = tick(rec, pr=pull(head=SHA_B), branch=SHA_B, operations={op["operation_id"]: op})
        self.assertEqual(kinds(out), ["record_head_change"])
        after = final(out, rec)
        self.assertEqual(after["pending_human_action"]["kind"], "upstream_head_changed")
        # The recorded head of upstream review is the upstream PR's; the fork
        # keeps the approved head of `riscv64-dev`.
        self.assertEqual(after["upstream_pr"]["head_sha"], SHA_B)
        self.assertEqual(after["fork"]["head_sha"], rec["fork"]["head_sha"])
        self.assertEqual(lifecycle.recorded_head(after), SHA_B)

    def test_states_without_an_observation_row_are_not_read(self):
        pending = {"kind": "answer_reviewer", "requested_at": NOW, "reminded_at": None,
                   "escalated_at": None, "counter": None}
        for rec in (record("fix_needed"), record("agent_running", active_work=dict(REVISION_WORK)),
                    record("waiting_human", pending_human_action=pending)):
            with self.subTest(state=engine.state_key(rec)):
                out = tick(rec, pr=pull(merged=True, state="closed"), verdict="failed",
                           feedback=REVIEW)
                self.assertEqual(out.reads, [])
                self.assertNotIn("record_merge", kinds(out))
                self.assertEqual(engine.state_key(final(out, rec)), engine.state_key(rec))

    def test_a_closed_pr_is_resumed_only_once_it_is_open_again(self):
        rec = final(tick(record(), pr=pull(state="closed")), record())
        resume = f'/upstreaming resume revision={rec["revision"]} reason="reopened"'
        out = tick(rec, pr=pull(state="closed"),
                   comments=[{"issue_number": 12, "comment": {"id": 900, "body": resume,
                                                              "user": dict(OWNER)}}])
        self.assertEqual(out.reads, ["pull"])
        self.assertNotIn("resume_upstream_review", kinds(out))
        self.assertEqual(engine.state_key(final(out, rec)), "upstream_review.waiting_human")
        out = tick(rec, pr=pull(),
                   comments=[{"issue_number": 12, "comment": {"id": 901, "body": resume,
                                                              "user": dict(OWNER)}}])
        self.assertIn("resume_upstream_review", kinds(out))
        self.assertEqual(engine.state_key(final(out, rec)), "upstream_review.waiting_ci")

    def test_a_closed_pr_merged_since_is_resumed_and_recorded(self):
        rec = final(tick(record(), pr=pull(state="closed")), record())
        resume = f'/upstreaming resume revision={rec["revision"]} reason="merged"'
        merged = pull(state="closed", merged=True)
        out = tick(rec, pr=merged,
                   comments=[{"issue_number": 12, "comment": {"id": 903, "body": resume,
                                                              "user": dict(OWNER)}}])
        self.assertEqual(kinds(out), ["resume_upstream_review"])
        resumed = final(out, rec)
        self.assertEqual(engine.state_key(resumed), "upstream_review.waiting_ci")
        out = tick(resumed, pr=merged)
        self.assertEqual(kinds(out), ["record_merge"])
        self.assertEqual(engine.state_key(final(out, resumed)), "release_wait.idle")

    def test_a_failed_read_for_a_resume_holds_no_other_command_back(self):
        parked = contribution(phase="parked", activity="idle", parked_reason="x",
                              previous_phase="upstream_review",
                              upstream_pr=copy.deepcopy(UPSTREAM_PR))
        snapshot = reconcile.Snapshot({"c": copy.deepcopy(parked)}, {}, {}, {}, {})
        abandon = {"issue_number": 12, "comment": {"id": 902, "user": dict(OWNER),
                                                   "body": '/upstreaming abandon reason="gone"'}}
        with mock.patch("observe_ci.collect.upstream_pull",
                        side_effect=collect.CollectError("GET pulls/9: HTTP 404")), \
                contextlib.redirect_stderr(io.StringIO()):
            out = reconcile.tick(helpers.NoReadApi(), snapshot, policy=POLICY, now=T, publications=[],
                                 comments=[abandon], pressure=capacity.Pressure(ok=False),
                                 observe=True, dispatcher_runs=[], queue_works={})
        self.assertEqual(out.failures, [])
        self.assertEqual(kinds(out), ["abandon"])

    def test_the_resume_and_refresh_facts_follow_the_observed_pr(self):
        closed = record("waiting_human", pending_human_action={
            "kind": "pr_closed_unmerged", "requested_at": NOW, "reminded_at": None,
            "escalated_at": None, "counter": None})
        body = commands.parse(f'/upstreaming resume revision={closed["revision"]} reason="r"')
        self.assertFalse(apply_command(closed, body, upstream_pr={"state": "closed"}).accepted)
        self.assertFalse(apply_command(closed, body).accepted)
        self.assertTrue(apply_command(closed, body, upstream_pr={"state": "open"}).accepted)
        merged = apply_command(closed, body, upstream_pr={"state": "closed", "merged": True})
        self.assertTrue(merged.accepted, merged.reason)
        self.assertEqual(engine.state_key(merged.record), "upstream_review.waiting_ci")
        parked = contribution(phase="parked", activity="idle", parked_reason="x",
                              previous_phase="upstream_review",
                              upstream_pr=copy.deepcopy(UPSTREAM_PR))
        body = commands.parse(f'/upstreaming refresh revision={parked["revision"]} reason="r"')
        self.assertFalse(apply_command(parked, body).accepted)
        self.assertFalse(apply_command(parked, body, upstream_pr={"state": "open"}).accepted)
        out = apply_command(parked, body, upstream_pr={"state": "closed", "merged": False})
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(engine.state_key(out.record), "queued.idle")


class TestRevisionResults(unittest.TestCase):
    """Every Result of revision Work takes a row of `upstream_review.agent_running`."""

    def running(self, **fields):
        fields.setdefault("active_work", dict(REVISION_WORK,
                                              feedback_evidence_ids=["review-old"]))
        return record("agent_running", pending_feedback_evidence_ids=["review-old", "review-new"],
                      **fields)

    def test_each_publisher_result_takes_its_row(self):
        expected = {
            "published": ("record_revision_publish_success", "upstream_review.waiting_ci", True),
            "uncertain": ("schedule_publish_retry", "upstream_review.agent_running", False),
            "no_change": ("require_reviewer_answer", "upstream_review.waiting_human", False),
            "proposal": ("reject_patch_proposal", "upstream_review.fix_needed", True),
            "rejected_validation": ("reject_patch_proposal", "upstream_review.fix_needed", True),
            "rejected_stale": ("reject_patch_proposal", "upstream_review.fix_needed", True),
            "worker_blocked": ("reject_patch_proposal", "upstream_review.fix_needed", True),
            "infra_error": ("schedule_publish_retry", "upstream_review.agent_running", False),
        }
        for name, (verb, state, bumped) in expected.items():
            with self.subTest(result=name):
                rec = self.running()
                result = (published(rec, SHA_B) if name == "published"
                          else publication(rec, name))
                at = SHA_B if name == "published" else SHA_A
                out = tick(rec, [result], pr=pull(head=at), branch=at)
                self.assertEqual(out.failures, [])
                self.assertEqual(kinds(out)[0], verb)
                after = final(out, rec)
                self.assertEqual(engine.state_key(after), state)
                self.assertEqual(after["revision"], rec["revision"] + bumped)
                self.assertIsNone(after["active_work"])
                if verb == "record_revision_publish_success":
                    # Exactly the feedback the Work carried is answered.
                    self.assertEqual(after["pending_feedback_evidence_ids"], ["review-new"])
                else:
                    self.assertEqual(after["pending_feedback_evidence_ids"],
                                     ["review-old", "review-new"])
                if verb == "require_reviewer_answer":
                    self.assertEqual(after["pending_human_action"]["kind"], "answer_reviewer")

    def test_no_change_of_a_ci_revision_asks_a_human(self):
        rec = record("agent_running", last_ci_evidence_id="ci-2-777-1",
                     pending_feedback_evidence_ids=[],
                     active_work=dict(REVISION_WORK, feedback_evidence_ids=[]))
        out = tick(rec, [publication(rec, "no_change")])
        self.assertEqual(kinds(out)[0], "require_reviewer_answer")
        waiting = final(out, rec)
        self.assertEqual(engine.state_key(waiting), "upstream_review.waiting_human")
        self.assertEqual(waiting["pending_human_action"]["kind"], "answer_reviewer")
        resume = f'/upstreaming resume revision={waiting["revision"]} reason="judged"'
        out = tick(waiting, comments=[{"issue_number": 12, "comment": {
            "id": 910, "body": resume, "user": dict(OWNER)}}])
        self.assertEqual(kinds(out), ["resume_upstream_review"])
        resumed = final(out, waiting)
        self.assertEqual((engine.state_key(resumed), resumed["last_ci_evidence_id"]),
                         ("upstream_review.waiting_ci", "ci-2-777-1"))
        # The same failing run is not recorded again, so no revision is requested.
        out = tick(resumed, verdict="failed")
        self.assertEqual(kinds(out), [])

    def test_the_published_head_is_recorded_by_the_row(self):
        rec = self.running()
        # In the tick that folds it, the upstream PR is already at the head.
        out = tick(rec, [published(rec, SHA_B)], pr=pull(head=SHA_B), branch=SHA_B)
        self.assertEqual(out.failures, [])
        self.assertEqual(kinds(out), ["record_revision_publish_success"])
        event = staged(out, "events")[0]
        self.assertEqual((event["details"]["head"], event["details"]["previous_head"]),
                         (SHA_B, SHA_A))
        self.assertTrue(event["revision_bumped"])
        after = final(out, rec)
        self.assertEqual((after["upstream_pr"]["head_sha"], after["revision"]),
                         (SHA_B, rec["revision"] + 1))
        # The published head is not a head change: its CI is read.
        out = tick(after, pr=pull(head=SHA_B), branch=SHA_B, verdict="failed")
        self.assertEqual(kinds(out), ["record_upstream_ci_failure"])
        failed = final(out, after)
        self.assertEqual((failed["upstream_pr"]["head_sha"], failed["revision"]),
                         (SHA_B, after["revision"]))
        # A push by someone else after it still is.
        out = tick(after, pr=pull(head=SHA_C), branch=SHA_C)
        self.assertEqual(kinds(out), ["record_head_change"])
        self.assertEqual(engine.state_key(final(out, after)), "upstream_review.waiting_human")

    def test_the_next_revision_work_has_a_fresh_id(self):
        rec = self.running()
        after = final(tick(rec, [published(rec, SHA_B)], pr=pull(head=SHA_B), branch=SHA_B), rec)
        failed = tick(after, pr=pull(head=SHA_B), branch=SHA_B, verdict="failed")
        due = final(failed, after)
        evidence = {e["evidence_id"]: e for e in staged(failed, "evidence")}
        out = tick(due, pr=pull(head=SHA_B), branch=SHA_B, evidence=evidence, capacity_ok=True)
        self.assertEqual(kinds(out), ["request_upstream_revision"])
        work = final(out, due)["active_work"]
        self.assertEqual(work["revision"], rec["revision"] + 1)
        self.assertNotEqual(work["dedupe_key"], rec["active_work"]["dedupe_key"])
        self.assertNotIn("allocate_work_retry", kinds(out))

    def test_a_head_that_was_not_read_back_is_not_recorded(self):
        rec = self.running()
        # The push was not read back: a failed publication, the head is kept.
        lost = branch_operation(rec, SHA_B, confirmed=False)
        out = tick(rec, [dict(publication(rec, "uncertain"), candidate=SHA_B, operation=lost)])
        self.assertEqual(kinds(out), ["schedule_publish_retry"])
        held = final(out, rec)
        self.assertEqual((held["upstream_pr"]["head_sha"], held["revision"]),
                         (SHA_A, rec["revision"]))
        # The publish-only retry reads the earlier push back on the branch,
        # and confirms the operation of the earlier attempt, which the row
        # names.
        ops = {lost["operation_id"]: lost}
        out = tick(held, [dict(publication(held, "published", artifact_id=1000), candidate=SHA_B,
                                attempt=2)], pr=pull(head=SHA_B), branch=SHA_B, operations=ops)
        self.assertEqual(kinds(out), ["record_revision_publish_success"])
        self.assertEqual([(o["operation_id"], o["status"]) for o in staged(out, "operations")],
                         [(lost["operation_id"], "confirmed")])
        after = final(out, held)
        self.assertEqual(after["upstream_pr"]["head_sha"], SHA_B)
        self.assertEqual(after["publication_operation_id"], lost["operation_id"])
        self.assertIsNone(after["pending_publication"])
        # A published Result whose operation is not confirmed, or that names
        # no head, is refused and changes nothing.
        unconfirmed = dict(published(rec, SHA_B), operation=branch_operation(
            rec, SHA_B, confirmed=False))
        for result in (unconfirmed, publication(rec, "published")):
            with self.subTest(result=result.get("operation") and "unconfirmed"):
                out = tick(rec, [result])
                self.assertEqual(len(out.failures), 1)
                self.assertNotIn("record_revision_publish_success", kinds(out))

    def test_publish_retries_then_a_human_wait(self):
        rec = self.running()
        out = tick(rec, [publication(rec, "infra_error")])
        rec = final(out, rec)
        self.assertEqual(rec["pending_publication"]["attempts"], 1)
        now = T
        for _ in range(12):
            now = now + timedelta(hours=6)
            out = tick(rec, now=now)
            rec = final(out, rec)
            if engine.state_key(rec) != "upstream_review.agent_running":
                break
        self.assertEqual(engine.state_key(rec), "upstream_review.waiting_human")
        self.assertEqual((rec["pending_human_action"]["kind"],
                          rec["pending_human_action"]["counter"]),
                         ("publication_infrastructure_failure", "publish_recovery"))
        resumed = apply_command(rec, commands.parse(
            f'/upstreaming resume revision={rec["revision"]} reason="fixed"'))
        self.assertTrue(resumed.accepted, resumed.reason)
        self.assertEqual(engine.state_key(resumed.record), "upstream_review.fix_needed")

    def test_a_published_revision_extends_the_scope_only_outside_it(self):
        for files, expected in ((["a"], ["a"]), (["a", "b", "b"], ["a", "b"])):
            with self.subTest(files=files):
                rec = dict(self.running(), scope_paths=["a"])
                result = dict(published(rec, SHA_B))
                result["operation"] = dict(result["operation"],
                                           proposed=dict(result["operation"]["proposed"],
                                                         changed_files=files))
                out = tick(rec, [result], pr=pull(head=SHA_B), branch=SHA_B)
                self.assertEqual(kinds(out)[0], "record_revision_publish_success")
                self.assertEqual(final(out, rec)["scope_paths"], expected)

    def test_every_row_that_folds_a_held_result_clears_it(self):
        rec = self.running()
        held = final(tick(rec, [publication(rec, "infra_error")]), rec)
        self.assertIsNotNone(held["pending_publication"])
        for name, verb in (("published", "record_revision_publish_success"),
                           ("rejected_validation", "reject_patch_proposal"),
                           ("no_change", "require_reviewer_answer")):
            with self.subTest(result=name):
                result = (published(held, SHA_B) if name == "published"
                          else publication(held, name))
                at = SHA_B if name == "published" else SHA_A
                out = tick(held, [dict(result, attempt=2, publication_artifact_id=1000)],
                           pr=pull(head=at), branch=at)
                self.assertEqual(out.failures, [])
                self.assertEqual(kinds(out)[0], verb)
                row = next(r for k, r in out.staged if k == "contributions")
                self.assertIsNone(row["pending_publication"])

    def test_a_held_publication_is_retried_in_upstream_review(self):
        rec = record("agent_running", pending_publication=dict(PENDING))
        out = tick(rec)
        self.assertEqual([r["contribution_id"] for r in out.report["publication_retries"]],
                         [rec["contribution_id"]])

    def test_the_answer_to_the_reviewer_is_resumed_without_work(self):
        rec = final(tick(self.running(), [publication(self.running(), "no_change")]),
                    self.running())
        out = apply_command(rec, commands.parse(
            f'/upstreaming resume revision={rec["revision"]} reason="answered"'))
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(engine.state_key(out.record), "upstream_review.waiting_ci")
        self.assertEqual(out.record["pending_feedback_evidence_ids"], [])
        self.assertIsNone(out.record["active_work"])


    def waiting(self, kind):
        return record("waiting_human", pending_feedback_evidence_ids=["review-1"],
                      pending_human_action={"kind": kind, "requested_at": NOW,
                                            "reminded_at": None, "escalated_at": None,
                                            "counter": None})

    def resumed(self, rec):
        body = f'/upstreaming resume revision={rec["revision"]} reason="answered"'
        return tick(rec, comments=[{"issue_number": 12, "comment": {
            "id": 903, "body": body, "user": dict(OWNER)}}])

    def test_the_resume_after_an_answered_reviewer_clears_the_feedback(self):
        """`resume_upstream_review` is the one resume that clears the
        pending feedback: a human answered it, so no revision Work carries
        it again; the review stays fenced, so nothing is requested next."""
        rec = dict(self.waiting("answer_reviewer"), processed_review_ids=[77])
        out = self.resumed(rec)
        self.assertEqual(kinds(out), ["resume_upstream_review"])
        after = final(out, rec)
        self.assertEqual((engine.state_key(after), after["revision"],
                          after["pending_feedback_evidence_ids"], after["active_work"]),
                         ("upstream_review.waiting_ci", rec["revision"] + 1, [], None))
        # The review stays fenced, so the collector reads nothing new and no
        # Work is requested for it.
        self.assertEqual(after["processed_review_ids"], [77])
        again = tick(after, capacity_ok=True)
        self.assertEqual(kinds(again), [])
        self.assertEqual(engine.state_key(final(again, after)), "upstream_review.waiting_ci")

    def test_a_resume_that_requests_work_again_keeps_the_feedback(self):
        rec = self.waiting("work_failed")
        out = self.resumed(rec)
        self.assertEqual(kinds(out), ["resume_upstream_revision"])
        after = final(out, rec)
        self.assertEqual((engine.state_key(after), after["pending_feedback_evidence_ids"]),
                         ("upstream_review.fix_needed", ["review-1"]))
        work = final(tick(after, capacity_ok=True), after)
        self.assertEqual(work["active_work"]["task"], "upstream_revision")
        self.assertEqual(work["active_work"]["feedback_evidence_ids"], ["review-1"])

    def test_only_published_results_and_the_answered_review_clear_feedback(self):
        self.assertEqual(lifecycle.check_table(POLICY), [])
        self.assertEqual(lifecycle.FEEDBACK_ANSWERED_BY, ("resume_upstream_review",))
        clearing = {e.verb for e in lifecycle.table_edges()
                    if "pending_feedback_evidence_ids" in e.transition.effects.clears}
        resumes = {verb for verb in clearing if verb.startswith("resume")}
        self.assertEqual(resumes, {"resume_upstream_review"})


class TestRevisionWork(unittest.TestCase):
    """Revision Work is allocated and exhausted by the rows of
    `upstream_review.fix_needed`; nothing parks without a command."""

    def test_a_failing_job_charges_its_cluster_and_review_only_work_total(self):
        rec = record("waiting_ci")
        failed = tick(rec, verdict="failed")
        observed = final(failed, rec)
        self.assertEqual(engine.state_key(observed), "upstream_review.fix_needed")
        evidence = {e["evidence_id"]: e for e in staged(failed, "evidence")}
        out = tick(observed, evidence=evidence, capacity_ok=True)
        self.assertIn("request_upstream_revision", kinds(out))
        after = final(out, observed)
        self.assertEqual(after["active_work"]["task"], "upstream_revision")
        self.assertEqual(after["budget"]["attempts_used"], 1)
        self.assertEqual(sum(after["budget"]["clusters"].values()), 1)

        reviewed = final(tick(rec, feedback=REVIEW), rec)
        out = tick(reviewed, capacity_ok=True)
        self.assertIn("request_review_revision", kinds(out))
        after = final(out, reviewed)
        self.assertEqual(after["active_work"]["feedback_evidence_ids"],
                         reviewed["pending_feedback_evidence_ids"])
        self.assertEqual((after["budget"]["attempts_used"], after["budget"]["clusters"]), (1, {}))

    def test_exhaustion_waits_for_a_human_and_never_parks(self):
        limit = lifecycle.budget_limit("no_progress", POLICY)
        reviewed = record("fix_needed", pending_feedback_evidence_ids=["review-1"])
        reviewed["budget"] = dict(reviewed["budget"], no_progress_streak=limit)
        out = tick(reviewed)
        self.assertEqual(kinds(out), ["exhaust_repair_attempts"])
        after = final(out, reviewed)
        self.assertEqual(engine.state_key(after), "upstream_review.waiting_human")
        self.assertEqual(after["pending_human_action"]["counter"], "no_progress")

    def test_exhaustion_names_the_first_spent_counter_in_order(self):
        """`exhaust_repair_attempts` names the first spent counter of
        `no_progress`, `work_total`, `repair_attempts_cluster`; the event
        lists every spent one, and each resume restarts only the named one."""
        rec = record("waiting_ci")
        failed = tick(rec, verdict="failed")
        observed = final(failed, rec)
        evidence = {e["evidence_id"]: e for e in staged(failed, "evidence")}
        cluster = next(iter(final(tick(observed, evidence=evidence, capacity_ok=True),
                                  observed)["budget"]["clusters"]))
        limits = {name: lifecycle.budget_limit(name, POLICY)
                  for name in ("no_progress", "work_total", "repair_attempts_cluster")}
        fields = {"no_progress": {"no_progress_streak": limits["no_progress"]},
                  "work_total": {"attempts_used": limits["work_total"]},
                  "repair_attempts_cluster": {
                      "clusters": {cluster: limits["repair_attempts_cluster"]}}}

        def spent(*names):
            out = copy.deepcopy(observed)
            for name in names:
                out["budget"].update(copy.deepcopy(fields[name]))
            return out

        def exhaust(rec):
            out = tick(rec, evidence=evidence)
            self.assertEqual(kinds(out), ["exhaust_repair_attempts"])
            event = staged(out, "events")[0]
            return final(out, rec), event

        for names, first in ((tuple(fields), "no_progress"),
                             (("work_total", "repair_attempts_cluster"), "work_total"),
                             (("repair_attempts_cluster",), "repair_attempts_cluster")):
            with self.subTest(spent=names):
                after, event = exhaust(spent(*names))
                self.assertEqual(engine.state_key(after), "upstream_review.waiting_human")
                self.assertEqual(after["pending_human_action"]["counter"], first)
                self.assertEqual(event["details"]["exhausted"], list(names))

        after, _event = exhaust(spent("work_total", "repair_attempts_cluster"))
        out = apply_command(after, commands.parse(
            f'/upstreaming resume revision={after["revision"]} reason="go"'))
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(engine.state_key(out.record), "upstream_review.fix_needed")
        self.assertEqual(out.record["budget"]["attempts_used"], 0)
        self.assertEqual(out.record["budget"]["clusters"][cluster],
                         limits["repair_attempts_cluster"])
        again, event = exhaust(out.record)
        self.assertEqual(again["pending_human_action"]["counter"], "repair_attempts_cluster")
        self.assertEqual(event["details"]["exhausted"], ["repair_attempts_cluster"])

    def test_no_park_from_upstream_review_without_a_command(self):
        for edge in lifecycle.table_edges():
            if lifecycle.state_phase(edge.source) == "upstream_review" \
                    and edge.target == "parked.idle":
                self.assertEqual(edge.transition.trigger, lifecycle.Trigger.COMMAND)
        park = commands.parse('/upstreaming park reason="x"')
        for activity in ("waiting_ci", "fix_needed", "agent_running"):
            with self.subTest(state=activity):
                out = apply_command(record(activity), park)
                self.assertFalse(out.accepted)
        waiting = record("waiting_human", pending_human_action={
            "kind": "answer_reviewer", "requested_at": NOW, "reminded_at": None,
            "escalated_at": None, "counter": None})
        self.assertTrue(apply_command(waiting, park).accepted)
        # Ticks over every observation and Result never park.
        for rec in (record(), record("fix_needed"), record("agent_running",
                                                           active_work=dict(REVISION_WORK))):
            for kwargs in (dict(verdict="failed"), dict(pr=pull(state="closed")),
                           dict(feedback=REVIEW), dict(pr=pull(head=SHA_B), branch=SHA_B)):
                pubs = [publication(rec, "rejected_validation")] if rec["active_work"] else []
                with self.subTest(state=engine.state_key(rec), **{k: str(v)[:20] for k, v in kwargs.items()}):
                    after = final(tick(rec, pubs, **kwargs), rec)
                    self.assertNotEqual(after["phase"], "parked")


class TestRelease(unittest.TestCase):
    CONTRACT = {"schema_version": 1, "evidence_id": "contract-1", "kind": "target_contract",
                "data": {"distributions": ["proj"],
                         "release_rows": [{"interpreter": "cp312", "abi": "cp312",
                                           "libc": "manylinux", "floor": [2, 39]}]}}

    def pypi(self, *files):
        return {"proj": {"releases": {"1.1.0": [
            {"filename": name, "yanked": False, "digests": {"sha256": "0" * 64},
             "upload_time_iso_8601": "2026-11-22T00:00:00Z"} for name in files]}}}

    def test_without_a_contract_an_operator_acts(self):
        rec = released_wait()
        out = tick(rec)
        self.assertEqual(kinds(out), ["require_release_intervention"])
        after = final(out, rec)
        self.assertEqual((engine.state_key(after), after["pending_human_action"]["kind"]),
                         ("release_wait.waiting_human", release.CONTRACT_MISSING))
        resumed = apply_command(after, commands.parse(
            f'/upstreaming resume revision={after["revision"]} reason="contract recorded"'))
        self.assertTrue(resumed.accepted, resumed.reason)
        self.assertEqual(engine.state_key(resumed.record), "release_wait.idle")

    def test_an_unknown_distribution_needs_an_operator(self):
        rec = released_wait(contract="contract-1")
        out = tick(rec, contracts=[self.CONTRACT], pypi={})
        self.assertEqual(final(out, rec)["pending_human_action"]["kind"],
                         release.DISTRIBUTION_MISSING)

    def test_incomplete_coverage_is_recorded_once_and_read_every_run(self):
        rec = released_wait(contract="contract-1")
        pypi = self.pypi("proj-1.1.0-cp312-cp312-manylinux_2_39_x86_64.whl")
        out = tick(rec, contracts=[self.CONTRACT], pypi=pypi, contains={"1.1.0": True})
        self.assertEqual(kinds(out), ["record_release_incomplete"])
        self.assertEqual(staged(out, "contributions"), [])
        after = final(out, rec)
        self.assertEqual(engine.state_key(after), "release_wait.idle")
        key = staged(out, "events")[0]["details"]["observation_key"]
        # Read on every run; recorded once per observation.
        self.assertEqual(kinds(tick(after, contracts=[self.CONTRACT], pypi=pypi,
                                    contains={"1.1.0": True},
                                    audited={rec["contribution_id"]: {key}})), [])
        again = tick(after, contracts=[self.CONTRACT], pypi=pypi, contains={"1.1.0": True},
                     audited={rec["contribution_id"]: {key}})
        self.assertEqual(staged(again, "contributions"), [])

    def test_a_release_without_the_merge_is_incomplete(self):
        rec = released_wait(contract="contract-1")
        pypi = self.pypi("proj-1.1.0-cp312-cp312-manylinux_2_39_riscv64.whl")
        for contains in ({"1.1.0": False}, {}):
            out = tick(rec, contracts=[self.CONTRACT], pypi=pypi, contains=contains)
            self.assertEqual(kinds(out), ["record_release_incomplete"])

    def test_verified_coverage_releases(self):
        rec = released_wait(contract="contract-1")
        pypi = self.pypi("proj-1.1.0-cp312-cp312-manylinux_2_39_riscv64.whl")
        out = tick(rec, contracts=[self.CONTRACT], pypi=pypi, contains={"1.1.0": True})
        self.assertEqual(kinds(out), ["verify_release"])
        after = final(out, rec)
        self.assertEqual(engine.state_key(after), "released.idle")
        [ev] = [e for e in staged(out, "evidence") if e["kind"] == "release_acceptance"]
        self.assertEqual(after["release_acceptance"]["evidence_id"], ev["evidence_id"])
        self.assertTrue(after["release_acceptance"]["complete"])

    def test_a_release_uploaded_before_the_merge_is_not_a_candidate(self):
        contract = release.acceptance_contract(self.CONTRACT)
        pypi = self.pypi("proj-1.1.0-cp312-cp312-manylinux_2_39_riscv64.whl")
        merge = dict(MERGE, merged_at="2026-11-22T12:00:00+00:00")
        decision = release.decide(contract, merge=merge, pypi=pypi, contains=lambda v: True)
        self.assertEqual(decision.verb, "record_release_incomplete")


class TestLedgerAndResponses(unittest.TestCase):
    def test_upstream_messages_and_quoted_comments(self):
        rec = record()
        out = tick(rec, feedback=INQUIRY, verdict="passed")
        [event] = staged(out, "events")
        event["sequence"] = 3
        body = status_ledger.render(event, final(out, rec), "https://github.com/run")
        self.assertIn("on the upstream PR", body)
        self.assertIn("`Why riscv64 @maintainer?`", body)
        for verb, kwargs in (("record_merge", dict(pr=pull(merged=True, state="closed"))),
                             ("record_pr_closed_unmerged", dict(pr=pull(state="closed"))),
                             ("record_upstream_ci_failure", dict(verdict="failed")),
                             ("record_head_change", dict(pr=pull(head=SHA_B), branch=SHA_B))):
            with self.subTest(verb=verb):
                out = tick(rec, **kwargs)
                [event] = [e for e in staged(out, "events") if e["kind"] == verb]
                event["sequence"] = 3
                text = status_ledger.render(event, final(out, rec), "https://github.com/run")
                self.assertNotIn(verb.replace("_", " ").capitalize(), text)

    def test_nothing_is_answered_on_the_upstream_pr(self):
        rec = record()
        self.assertEqual(review_response.apply(object(), record=rec, task={}, proposal={},
                                               outcome={"status": "published",
                                                        "candidate": SHA_B}), [])


if __name__ == "__main__":
    unittest.main()
