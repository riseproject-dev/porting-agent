# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Development on the table's rows.

The Result of patch Work is folded by the rows of
``development.agent_running``; the published head is observed by the rows of
``development.waiting_ci`` only; requested Work is recovered by the dispatch
and Work recovery rows; a held publication waits for its retries in
``development.agent_running``. The development states the table does not
have, and the wait the removed Work deadline wrote, are rewritten by the
development record migration.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import capacity
import collect
import commands
import compat
import controller
import engine
import lifecycle
import observe_ci
import reconcile
import schemas
import state
from common import canonical_json
from tests.helpers import (
    NOW,
    OWNER,
    SHA_A,
    SHA_B,
    SHA_C,
    apply_command,
    ci_record,
    contribution,
    policy,
)
from tests.test_late_results import WORK, ci_snapshot, publication

T = datetime.fromisoformat("2026-11-23T09:00:00+00:00")
POLICY = policy()
PATH = ".state/contributions/example--proj--12.json"
REVIEW = {"review_ids": [77], "comment_ids": [], "conversation_comment_ids": [],
          "reviews": [{"id": 77, "body": "Please keep the aarch64 row."}], "comments": [],
          "conversation_comments": [], "inquiry_comment_ids": [],
          "inquiry_conversation_comment_ids": [], "inquiry_comments": [],
          "inquiry_conversation_comments": []}
HELD = {"worker_run_id": "555", "attempts": 1, "last_reason": "HTTP 502",
        "next_attempt_at": NOW, "exhausted": False}


def record(activity="waiting_ci", **fields):
    return contribution(activity=activity, dossier_evidence_id="dossier-1", scope_paths=["a"],
                        scope_coverage_complete=True, **fields)


def running(**fields):
    fields.setdefault("active_work", dict(WORK))
    return record("agent_running", **fields)


def tick(rec, publications=(), *, observe=True, verdict="pending", head=SHA_A, feedback=None,
         now=T, evidence=None, runs=(), queue=None, pull=None, ci=None):
    snapshot = reconcile.Snapshot({"c": copy.deepcopy(rec)}, {}, {}, {}, dict(evidence or {}))

    def review_feedback(_api, _record, _authority, check_pull=False):
        if check_pull and pull:
            return {"pr_mismatch": pull}
        return copy.deepcopy(feedback)

    with mock.patch("observe_ci.collect.branch_head", return_value={"head": head}), \
            mock.patch("observe_ci.collect.development_review_feedback",
                       side_effect=review_feedback), \
            mock.patch("observe_ci.collect.ci_snapshot",
                       return_value=ci or ci_snapshot(head, verdict)), \
            contextlib.redirect_stderr(io.StringIO()):
        return reconcile.tick(object() if observe else None, snapshot, policy=POLICY, now=now,
                              publications=list(publications),
                              pressure=capacity.Pressure(ok=True), observe=observe,
                              dispatcher_runs=list(runs), queue_works=queue or {})


def final(output, rec):
    staged = [r for kind, r in output.staged if kind == "contributions"]
    return staged[-1] if staged else rec


def kinds(output):
    return [e["kind"] for kind, e in output.staged if kind == "events"]


class TestPatchResults(unittest.TestCase):
    """Every Result of patch Work takes a row of `development.agent_running`."""

    def test_each_publisher_result_takes_its_row(self):
        expected = {
            "published": ("record_publish_success", "development.waiting_ci", False),
            "uncertain": ("record_publish_success", "development.waiting_ci", False),
            "proposal": ("reject_patch_proposal", "development.fix_needed", True),
            "no_change": ("request_ci_rerun", "development.waiting_ci", False),
            "rejected_validation": ("reject_patch_proposal", "development.fix_needed", True),
            "worker_blocked": ("reject_patch_proposal", "development.fix_needed", True),
            "rejected_stale": ("replan_development", "development.fix_needed", True),
            "infra_error": ("schedule_publish_retry", "development.agent_running", False),
        }
        for name, (verb, target, bumped) in expected.items():
            with self.subTest(result=name):
                rec = running()
                new, staged = reconcile.apply_publication(
                    rec, dict(publication(rec, name)), now=NOW, policy=POLICY,
                    worker_run_id="555")
                events = [e for kind, e in staged if kind == "events"]
                self.assertEqual([e["kind"] for e in events], [verb])
                self.assertEqual(engine.state_key(new), target)
                self.assertEqual(events[0]["revision_bumped"], bumped)
                self.assertIsNone(new["active_work"])
                self.assertIsNone(compat.state_problem(new))
                charged = events[0]["details"].get("charged", [])
                self.assertEqual("no_progress" in charged, verb == "reject_patch_proposal")
                self.assertEqual("flaky_reruns" in charged, verb == "request_ci_rerun")
        # A blocked Result records its rehearsal evidence and names it in the
        # event; no row records it as pending feedback.
        rec = running()
        new, staged = reconcile.apply_publication(
            rec, dict(publication(rec, "worker_blocked")), now=NOW, policy=POLICY,
            worker_run_id="555")
        evidence = [e for kind, e in staged if kind == "evidence"]
        self.assertEqual([e["kind"] for e in evidence], ["rehearsal"])
        self.assertEqual([e["details"]["evidence_id"] for kind, e in staged if kind == "events"],
                         [evidence[0]["evidence_id"]])
        self.assertEqual(new.get("pending_feedback_evidence_ids") or [], [])

    def test_a_no_change_report_reruns_ci_within_flaky_reruns_without_feedback(self):
        """The repair worker classifies: a `no_change` Result reports a failure
        not caused by the patch. Without carried feedback and within
        `flaky_reruns` it takes `request_ci_rerun`; CI on the head is read
        again in `development.waiting_ci`. With carried feedback, or once
        `flaky_reruns` is spent for the head, it counts as
        `reject_patch_proposal`."""
        rec = running(last_ci_evidence_id="ci-0")
        new, staged = reconcile.apply_publication(
            rec, dict(publication(rec, "no_change")), now=NOW, policy=POLICY,
            worker_run_id="555")
        self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                         ["request_ci_rerun"])
        self.assertEqual((engine.state_key(new), new["revision"], new["last_ci_evidence_id"],
                          new["active_work"]),
                         ("development.waiting_ci", rec["revision"], None, None))
        self.assertEqual(new["budget"]["reruns"], {"head_sha": SHA_A, "flaky": 1, "interrupted": 0,
                                                   "answered_ci_evidence_id": "ci-0"})
        self.assertEqual(new["budget"]["no_progress_streak"], rec["budget"]["no_progress_streak"])
        # Passing CI on the head nominates; a failed run is a failure again.
        self.assertEqual(kinds(tick(new, verdict="passed")), ["nominate_for_handoff"])
        self.assertEqual(kinds(tick(new, verdict="failed"))[:1], ["record_ci_failure"])
        # `flaky_reruns` is spent for this head: the next report is rejected.
        again = dict(running(), budget=new["budget"])
        rejected, staged = reconcile.apply_publication(
            again, dict(publication(again, "no_change")), now=NOW, policy=POLICY,
            worker_run_id="555")
        self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                         ["reject_patch_proposal"])
        self.assertEqual(engine.state_key(rejected), "development.fix_needed")
        self.assertEqual(rejected["budget"]["no_progress_streak"],
                         again["budget"]["no_progress_streak"] + 1)
        # Work that carries feedback never takes the rerun.
        carried = running(pending_feedback_evidence_ids=["review-1"])
        carried["active_work"]["feedback_evidence_ids"] = ["review-1"]
        rejected, staged = reconcile.apply_publication(
            carried, dict(publication(carried, "no_change")), now=NOW, policy=POLICY,
            worker_run_id="555")
        self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                         ["reject_patch_proposal"])
        self.assertEqual(rejected["pending_feedback_evidence_ids"], ["review-1"])

    def test_a_published_patch_clears_exactly_the_feedback_its_work_carried(self):
        rec = running(pending_feedback_evidence_ids=["review-1", "review-2"])
        rec["active_work"]["feedback_evidence_ids"] = ["review-1"]
        new, staged = reconcile.apply_publication(
            rec, dict(publication(rec, "published"),
                      operation={"operation_id": "op-1", "proposed": {"changed_files": ["b"]}}),
            now=NOW, policy=POLICY, worker_run_id="555")
        self.assertEqual(new["pending_feedback_evidence_ids"], ["review-2"])
        self.assertEqual(new["publication_operation_id"], "op-1")
        self.assertEqual(new["scope_paths"], ["a", "b"])
        self.assertEqual(new["budget"]["reruns"],
                         {"head_sha": SHA_A, "flaky": 0, "interrupted": 0})
        # Feedback recorded after the allocation stays pending: no nomination.
        out = tick(new, verdict="passed")
        self.assertEqual(engine.state_key(final(out, new)), "development.waiting_ci")
        self.assertIn("review-2", out.report["observation"][0]["handoff_deferred"])
        # Without it, passing CI nominates.
        cleared = dict(new, pending_feedback_evidence_ids=[])
        out = tick(cleared, verdict="passed")
        self.assertEqual(kinds(out), ["nominate_for_handoff"])

    def test_publish_retries_then_the_human_wait_then_fresh_work(self):
        rec = running()
        out = tick(rec, [dict(publication(rec, "infra_error"), publication_artifact_id=1)],
                   observe=False)
        held = final(out, rec)
        self.assertEqual(kinds(out), ["schedule_publish_retry"])
        self.assertEqual((engine.state_key(held), held["active_work"],
                          held["pending_publication"]["attempts"]),
                         ("development.agent_running", None, 1))
        # The retry is launched once it is due, without a new charge.
        self.assertEqual(held["pending_publication"]["next_attempt_at"],
                         (T + timedelta(minutes=15)).isoformat())
        later = T + timedelta(hours=1)
        out = tick(held, observe=False, now=later)
        self.assertEqual(out.report["publication_retries"],
                         [{"type": "retry_publication", "contribution_id": held["contribution_id"],
                           "worker_run_id": "555", "attempt": 2}])
        launched = final(out, held)
        self.assertEqual(launched["pending_publication"]["attempts"], 1)
        self.assertEqual(launched["pending_publication"]["launched_at"], later.isoformat())
        self.assertEqual(kinds(out), [])
        limit = lifecycle.budget_limit("publish_recovery", POLICY)
        current = launched
        for artifact in range(2, limit + 2):
            # Each retry is dispatched with the attempt after the charged ones.
            attempt = current["pending_publication"]["attempts"] + 1
            out = tick(current, [dict(publication(current, "infra_error"), attempt=attempt,
                                      publication_artifact_id=artifact)], observe=False, now=later)
            current = final(out, current)
        self.assertEqual(kinds(out), ["exhaust_publish_recovery"])
        self.assertEqual((engine.state_key(current), current["pending_human_action"]["kind"],
                          current["pending_human_action"]["counter"]),
                         ("development.waiting_human", "publication_infrastructure_failure",
                          "publish_recovery"))
        self.assertTrue(current["pending_publication"]["exhausted"])
        self.assertEqual(tick(current, observe=False, now=later).report["publication_retries"], [])
        # The exhausted publication launches nothing more and does not keep
        # the resume from applying: fresh Work is due.
        resumed = apply_command(current, commands.parse(
            f"/upstreaming resume revision={current['revision']} reason=fixed"))
        self.assertTrue(resumed.accepted, resumed.reason)
        self.assertEqual(resumed.event["kind"], "resume_development")
        self.assertEqual((engine.state_key(resumed.record), resumed.record["pending_publication"]),
                         ("development.fix_needed", None))
        self.assertEqual(resumed.event["details"]["restarted"]["counter"], "publish_recovery")

    def test_a_retry_without_a_result_is_a_failed_publication(self):
        """A launched publish-only retry whose Result is not folded within its
        wait counts as a failed publication: `publish_recovery` is charged
        each time, so `exhaust_publish_recovery` is reached and the human wait
        and its resume follow."""
        rec = running()
        current = final(tick(rec, [dict(publication(rec, "infra_error"),
                                        publication_artifact_id=1)], observe=False), rec)
        now = T
        launches, charged = [], []
        for _ in range(200):
            now += timedelta(minutes=10)
            out = tick(current, observe=False, now=now)
            current = final(out, current)
            launches += [(now, r["attempt"]) for r in out.report["publication_retries"]]
            charged += [(now, k) for k in kinds(out)]
            if engine.state_key(current) != "development.agent_running":
                break
        limit = lifecycle.budget_limit("publish_recovery", POLICY)
        self.assertEqual([k for _at, k in charged],
                         ["schedule_publish_retry"] * (limit - 1) + ["exhaust_publish_recovery"])
        self.assertEqual([attempt for _at, attempt in launches], list(range(2, limit + 2)))
        # Each lost launch is charged once its wait is over, and the next one
        # follows the backoff.
        for (launched, _attempt), (charge, _kind) in zip(launches, charged):
            self.assertEqual(charge - launched,
                             timedelta(minutes=reconcile.PUBLISH_RESULT_WAIT_MINUTES))
        for (charge, _kind), (launched, _attempt), attempts in zip(
                charged, launches[1:], range(2, limit + 1)):
            self.assertGreaterEqual(launched - charge,
                                    timedelta(minutes=reconcile.publish_retry_delay(attempts)))
        self.assertEqual((engine.state_key(current), current["pending_human_action"]["kind"],
                          current["pending_human_action"]["counter"]),
                         ("development.waiting_human", "publication_infrastructure_failure",
                          "publish_recovery"))
        self.assertTrue(current["pending_publication"]["exhausted"])
        self.assertEqual(tick(current, observe=False, now=now + timedelta(days=1))
                         .report["publication_retries"], [])
        resumed = apply_command(current, commands.parse(
            f"/upstreaming resume revision={current['revision']} reason=fixed"))
        self.assertTrue(resumed.accepted, resumed.reason)
        self.assertEqual(engine.state_key(resumed.record), "development.fix_needed")

    def test_the_result_of_a_launched_retry_is_folded_before_its_wait_ends(self):
        rec = running()
        held = final(tick(rec, [dict(publication(rec, "infra_error"),
                                     publication_artifact_id=1)], observe=False), rec)
        launched = final(tick(held, observe=False, now=T + timedelta(minutes=20)), held)
        self.assertIn("launched_at", launched["pending_publication"])
        # The retry run's artifact names the worker run it publishes.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "9001"
            directory.mkdir()
            (directory / "outcome.json").write_text(json.dumps(
                {"status": "published", "failure_class": None, "reason": None}))
            (directory / "task.json").write_text(json.dumps(
                {"contribution_id": rec["contribution_id"], "revision": rec["revision"]}))
            (directory / "worker-run-id").write_text("555")
            (directory / "artifact-id").write_text("2")
            publications = reconcile._publication_outcomes(tmp)
        self.assertEqual(publications[0]["worker_run_id"], "555")
        out = tick(launched, publications, observe=False, now=T + timedelta(minutes=30))
        self.assertEqual(kinds(out), ["record_publish_success"])
        self.assertEqual((engine.state_key(final(out, launched)),
                          final(out, launched)["pending_publication"]),
                         ("development.waiting_ci", None))

    def test_a_pause_clears_the_held_publication_and_no_retry_is_launched(self):
        held = record("agent_running", pending_publication=dict(HELD))
        paused = apply_command(held, commands.parse('/upstreaming pause reason="hold"'))
        self.assertTrue(paused.accepted)
        self.assertEqual((engine.state_key(paused.record), paused.record["pending_publication"]),
                         ("development.fix_needed", None))
        out = tick(paused.record, observe=False, now=T + timedelta(hours=5))
        self.assertEqual((out.report["publication_retries"], out.report["requested_effects"],
                          out.report["dispatch_relaunch"]), ([], [], []))


class TestObservation(unittest.TestCase):
    """`development.waiting_ci` is the only development state observed."""

    def test_review_feedback_is_not_ci_evidence(self):
        rec = record(last_ci_evidence_id=None)
        out = tick(rec, feedback=REVIEW, verdict="passed")
        after = final(out, rec)
        # The review is answered by patch Work that carries it.
        self.assertEqual(kinds(out), ["record_actionable_review", "request_patch"])
        self.assertEqual(final(out, rec)["active_work"]["feedback_evidence_ids"],
                         ["review-2-1-77"])
        review = next(e for kind, e in out.staged if kind == "events")
        self.assertEqual(review["to"]["activity"], "fix_needed")
        self.assertIsNone(after["last_ci_evidence_id"])
        self.assertEqual(after["pending_feedback_evidence_ids"], ["review-2-1-77"])
        self.assertEqual(after["processed_review_ids"], [77])
        self.assertEqual(after["revision"], rec["revision"] + 1)

    def test_a_review_with_a_ci_failure_is_recorded_first(self):
        rec = record()
        out = tick(rec, feedback=REVIEW, verdict="failed")
        self.assertEqual(kinds(out)[:1], ["record_actionable_review"])
        self.assertNotIn("record_ci_failure", kinds(out))

    def test_a_ci_failure_returns_to_development_without_a_revision_bump(self):
        rec = record()
        out = tick(rec, verdict="failed")
        after = final(out, rec)
        self.assertEqual(kinds(out)[0], "record_ci_failure")
        self.assertEqual(after["revision"], rec["revision"])
        self.assertTrue(after["last_ci_evidence_id"].startswith("ci-2-777-1"))
        # The same tick requests the repair for the failing job.
        self.assertEqual(kinds(out)[1:], ["request_repair"])

    def test_a_new_head_restarts_the_rerun_counts(self):
        rec = record(budget=dict(contribution()["budget"],
                                 reruns={"head_sha": SHA_A, "flaky": 1, "interrupted": 2}))
        out = tick(rec, head=SHA_B)
        after = final(out, rec)
        self.assertEqual(kinds(out), ["record_head_change"])
        self.assertEqual(after["budget"]["reruns"],
                         {"head_sha": SHA_B, "flaky": 0, "interrupted": 0})
        self.assertEqual(after["revision"], rec["revision"] + 1)

    def test_a_development_pr_that_does_not_match_waits_for_a_human(self):
        rec = record()
        out = tick(rec, pull="development PR 1 has its head in another repository")
        after = final(out, rec)
        self.assertEqual(kinds(out), ["record_pr_mismatch"])
        self.assertEqual((engine.state_key(after), after["pending_human_action"]["kind"]),
                         ("development.waiting_human", "development_pr_mismatch"))
        # The wait is not observed; the resume returns to fix_needed.
        self.assertEqual(tick(after, verdict="failed").staged, [])
        resumed = apply_command(after, commands.parse(
            f"/upstreaming resume revision={after['revision']} reason=fixed"))
        self.assertEqual((resumed.event["kind"], engine.state_key(resumed.record)),
                         ("resume_development", "development.fix_needed"))

    def test_a_pull_request_mismatch_is_read_from_github(self):
        class Api:
            def __init__(self, pr):
                self.pr = pr

            def get(self, path):
                if self.pr is None:
                    raise collect.CollectError("HTTP 404")
                return self.pr

        rec = record()
        good = {"number": 1, "head": {"ref": "riscv64-dev", "repo": {"id": 2}}, "base": {"ref": "main"}}
        self.assertIsNone(collect.development_pull_problem(Api(good), rec))
        for pr in (None, dict(good, number=2), dict(good, head={"ref": "x", "repo": {"id": 2}}),
                   dict(good, head={"ref": "riscv64-dev", "repo": {"id": 3}}),
                   dict(good, base={"ref": "other"})):
            with self.subTest(pr=pr):
                self.assertTrue(collect.development_pull_problem(Api(pr), rec))
        self.assertEqual(collect.development_review_feedback(Api(None), rec, [OWNER["id"]],
                                                             check_pull=True),
                         {"pr_mismatch": "development PR 1 does not exist"})

    def test_ci_waiting_for_approval_is_not_a_state(self):
        rec = record()
        snapshot = ci_snapshot(SHA_A, "passed")
        snapshot.runs[0]["conclusion"] = "action_required"
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": SHA_A}), \
                mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
                mock.patch("observe_ci.collect.ci_snapshot", return_value=snapshot):
            observed = observe_ci.observe(object(), contributions={"c": rec}, operations={},
                                          contracts={}, policy=POLICY, now=NOW)
        after = [r for kind, r in observed.staged if kind == "contributions"]
        self.assertEqual(engine.state_key(after[-1]), "development.waiting_ci")
        self.assertIsNotNone(after[-1]["next_poll_at"])
        self.assertEqual([kind for kind, _ in observed.staged], ["contributions"])

    def test_a_head_that_moved_while_work_is_due_waits_for_its_ci(self):
        """A push while Work is due takes `record_head_change` from
        `development.fix_needed` to `development.waiting_ci`: the new head
        is recorded at a new revision, the CI evidence of the old head is
        cleared, the rerun counts start again, and no Work is requested in
        the tick. The CI of the new head is then read."""
        rec = record("fix_needed", last_ci_evidence_id="ci-old",
                     budget=dict(record()["budget"],
                                 reruns={"head_sha": SHA_A, "flaky": 1,
                                         "interrupted": 0}))
        out = tick(rec, head=SHA_B)
        after = final(out, rec)
        self.assertEqual(kinds(out), ["record_head_change"])
        self.assertEqual(out.report["requested_effects"], [])
        self.assertEqual((engine.state_key(after), after["revision"], after["fork"]["head_sha"],
                          after["last_ci_evidence_id"]),
                         ("development.waiting_ci", rec["revision"] + 1, SHA_B, None))
        self.assertEqual(after["budget"]["reruns"],
                         {"head_sha": SHA_B, "flaky": 0, "interrupted": 0})
        self.assertIsNotNone(after["next_poll_at"])
        # A failure on the new head is recorded in the same tick, and the
        # repair is requested for the new head.
        failed = tick(rec, head=SHA_B, verdict="failed")
        self.assertEqual(kinds(failed), ["record_head_change", "record_ci_failure", "request_repair"])
        self.assertEqual(final(failed, rec)["fork"]["head_sha"], SHA_B)
        self.assertEqual(failed.report["requested_effects"][0]["expected_head"], SHA_B)
        # An unchanged head is not read: Work is requested for it.
        out = tick(dict(rec, last_ci_evidence_id=None), head=SHA_A)
        self.assertEqual(kinds(out), ["request_patch"])

    def test_green_ci_at_the_gate_changes_nothing(self):
        from tests.helpers import awaiting_submission
        gate = awaiting_submission()
        out = tick(gate, verdict="passed")
        self.assertEqual([kind for kind, _ in out.staged], ["evidence"])
        self.assertEqual(kinds(out), [])


class TestDispatchAndWorkRecovery(unittest.TestCase):
    def test_lost_dispatch_is_retried_then_waits_for_a_human(self):
        rec = running(active_work=dict(WORK, dedupe_key="example--proj/12@g0.r0:development_repair",
                                       requested_at="2026-11-23T08:00:00+00:00"))
        limit = lifecycle.budget_limit("dispatch_recovery", POLICY)
        runs = [{"id": 100 + i, "status": "completed",
                 "created_at": f"2026-11-23T08:{10 + i:02d}:00Z"} for i in range(limit)]
        current = rec
        for count in range(1, limit + 1):
            out = tick(current, observe=False, runs=runs[:count])
            current = final(out, current)
        self.assertEqual(kinds(out), ["exhaust_dispatch_recovery"])
        self.assertEqual((engine.state_key(current), current["active_work"],
                          current["pending_human_action"]["counter"]),
                         ("development.waiting_human", None, "dispatch_recovery"))

    def test_terminal_work_is_reallocated_within_its_budget(self):
        rec = running(active_work=dict(WORK, dedupe_key="example--proj/12@g0.r0:development_repair"))
        import work
        out = tick(rec, observe=False, queue={work.graph_id(rec["active_work"]["dedupe_key"]):
                                              "completed"})
        self.assertEqual(kinds(out), ["allocate_work_retry"])
        self.assertEqual(final(out, rec)["active_work"]["attempt"], 2)

    def test_no_deadline_ends_requested_work(self):
        rec = running(active_work=dict(WORK, deadline_at="2026-11-01T00:00:00+00:00"))
        out = tick(rec, observe=False)
        self.assertEqual((out.staged, out.report.get("timed_out_work")), ([], None))


JOB = "Build - ubuntu-24.04-riscv (riscv64)"


def observed_ci_id(attempt=1):
    """The evidence id observation records for the fixture's CI run."""
    return observe_ci._evidence_id(2, {"id": 777, "run_attempt": attempt})


def attempt_tick(rec, attempt, verdict, **kw):
    """``tick`` with the fixture's CI run at ``attempt``."""
    snapshot = ci_snapshot(SHA_A, verdict)
    snapshot.runs[0]["run_attempt"] = attempt
    return tick(rec, verdict=verdict, ci=snapshot, **kw)


def submitted(output):
    return [e["dedupe_key"] for e in output.report["requested_effects"]
            if e["type"] == "submit_work"]


class TestCiRerunWithoutTrustedRerun(unittest.TestCase):
    """While no trusted rerun exists, `request_ci_rerun` leads to
    `development.waiting_ci`, where only CI evidence of another run or
    attempt than the one the classified Result answered is acted on."""

    def setUp(self):
        failing = observed_ci_id()
        self.evidence = {failing: ci_record(failing, SHA_A, failing=JOB)}
        due = record("fix_needed", last_ci_evidence_id=failing)
        out = tick(due, observe=False, evidence=self.evidence)
        self.assertEqual(kinds(out), ["request_repair"])
        self.running = final(out, due)
        self.work = self.running["active_work"]["dedupe_key"]
        self.assertEqual(submitted(out), [self.work])

    def test_the_answered_run_is_not_recorded_again(self):
        import work
        result = dict(publication(self.running, "no_change"),
                      task={"contribution_id": self.running["contribution_id"],
                            "revision": self.running["revision"], "generation": 0,
                            "dedupe_key": self.work, "task": "development_repair"})
        out = tick(self.running, [result], verdict="failed", evidence=self.evidence)
        self.assertEqual(kinds(out), ["request_ci_rerun"])
        waiting = final(out, self.running)
        self.assertEqual(engine.state_key(waiting), "development.waiting_ci")
        self.assertEqual(waiting["budget"]["reruns"]["answered_ci_evidence_id"],
                         observed_ci_id())
        self.assertIn("request_ci_rerun answered", out.report["observation"][0]["note"])
        self.assertEqual(submitted(out), [])
        # The queue now holds the Work as completed. The same failed run on
        # later ticks changes nothing: no Work is requested or recovered.
        queue = {work.graph_id(self.work): "completed"}
        runs = [{"id": 5, "status": "completed", "created_at": "2026-11-23T09:05:00Z"}]
        for minutes in (10, 20, 60):
            later = tick(waiting, verdict="failed", evidence=self.evidence, queue=queue,
                         runs=runs, now=T + timedelta(minutes=minutes))
            self.assertEqual((kinds(later), submitted(later)), ([], []))
            waiting = final(later, waiting)
        # A newer attempt of the run fails: repair Work is requested under an
        # id other than the Work that returned the Result.
        out = attempt_tick(waiting, 2, "failed", evidence=self.evidence, queue=queue, runs=runs,
                           now=T + timedelta(minutes=70))
        self.assertEqual(kinds(out), ["record_ci_failure", "request_repair"])
        [repair] = submitted(out)
        self.assertNotEqual(repair, self.work)
        self.assertEqual(repair, self.work.replace(".r0:", ".r0.rerun1:"))
        self.assertEqual(final(out, waiting)["revision"], self.running["revision"])
        # A passing run on the head still nominates.
        out = tick(waiting, verdict="passed", evidence=self.evidence)
        self.assertEqual(kinds(out), ["nominate_for_handoff"])

    def test_work_that_returned_a_result_is_never_allocated_again(self):
        """An allocation that would name a Work graph the queue holds as
        terminal, or that returned a Result in this tick, is an error of the
        contribution, not a resubmission."""
        import work
        due = record("fix_needed", last_ci_evidence_id=observed_ci_id())
        key = work.work_key(due["contribution_id"], 0, due["revision"], "development_repair")
        out = tick(due, observe=False, evidence=self.evidence,
                   queue={work.graph_id(key): "completed"})
        self.assertEqual(kinds(out), ["pause_on_processing_error"])
        self.assertEqual(submitted(out), [])
        self.assertIn("never submitted again",
                      out.report["processing_errors"][0]["message"])
        out = tick(due, [dict(publication(due, "published"),
                              task={"contribution_id": due["contribution_id"],
                                    "revision": due["revision"], "generation": 0,
                                    "dedupe_key": key, "task": "development_repair"})],
                   observe=False, evidence=self.evidence)
        self.assertIn("pause_on_processing_error", kinds(out))
        self.assertEqual(submitted(out), [])


class TestLateRetryResult(unittest.TestCase):
    def test_a_late_result_of_a_charged_launch_charges_nothing_again(self):
        """A retry whose Result is not folded within its wait is charged by
        the wait; its own `infra_error` Result arriving later is recorded
        once and charges nothing again."""
        rec = running()
        held = final(tick(rec, [dict(publication(rec, "infra_error"),
                                     publication_artifact_id=1)], observe=False), rec)
        launched = final(tick(held, observe=False, now=T + timedelta(minutes=20)), held)
        lost = final(tick(launched, observe=False, now=T + timedelta(minutes=80)), launched)
        self.assertEqual(lost["pending_publication"]["attempts"], 2)
        self.assertNotIn("launched_at", lost["pending_publication"])
        for late in ({}, {"attempt": 2}):
            out = tick(lost, [dict(publication(lost, "infra_error"), publication_artifact_id=2,
                                   **late)], observe=False, now=T + timedelta(minutes=90))
            after = final(out, lost)
            self.assertEqual(kinds(out), ["record_late_effect"])
            self.assertEqual(after["pending_publication"], lost["pending_publication"])
            self.assertEqual(after["processed_publications"][-1], 2)
        # The next launch's own Result is charged, and a late Result of the
        # charged launch is not, although a retry is launched now.
        relaunched = final(tick(lost, observe=False, now=T + timedelta(hours=3)), lost)
        self.assertIn("launched_at", relaunched["pending_publication"])
        out = tick(relaunched, [dict(publication(relaunched, "infra_error"), attempt=2,
                                     publication_artifact_id=3)],
                   observe=False, now=T + timedelta(hours=3, minutes=10))
        self.assertEqual(kinds(out), ["record_late_effect"])
        out = tick(relaunched, [dict(publication(relaunched, "infra_error"), attempt=3,
                                     publication_artifact_id=4)],
                   observe=False, now=T + timedelta(hours=3, minutes=10))
        self.assertEqual(kinds(out), ["schedule_publish_retry"])
        self.assertEqual(final(out, relaunched)["pending_publication"]["attempts"], 3)


class TestRefreshKeepsFeedback(unittest.TestCase):
    """Both refresh verbs keep the pending feedback; the investigation that
    follows carries none, and only patch and revision Work carry it."""

    def assert_investigation_follows(self, refreshed):
        self.assertEqual(engine.state_key(refreshed), "queued.idle")
        self.assertEqual(refreshed["pending_feedback_evidence_ids"], ["review-1"])
        out = tick(refreshed, observe=False, now=T + timedelta(minutes=10))
        self.assertEqual(kinds(out), ["begin_investigation", "dispatch_investigation"])
        after = final(out, refreshed)
        self.assertEqual(after["active_work"]["task"], "investigation")
        self.assertEqual(after["active_work"].get("feedback_evidence_ids") or [], [])
        self.assertEqual(after["pending_feedback_evidence_ids"], ["review-1"])
        self.assertNotIn("processing_errors", out.report)

    def test_refresh_on_exhaustion_then_investigation(self):
        limit = lifecycle.budget_limit("no_progress", POLICY)
        due = record("fix_needed", pending_feedback_evidence_ids=["review-1"],
                     budget=dict(contribution()["budget"], no_progress_streak=limit))
        out = tick(due, observe=False)
        self.assertEqual(kinds(out), ["refresh_on_exhaustion"])
        self.assert_investigation_follows(final(out, due))

    def test_refresh_investigation_then_investigation(self):
        waiting = record(pending_feedback_evidence_ids=["review-1"])
        out = apply_command(waiting, commands.parse(
            f'/upstreaming refresh revision={waiting["revision"]} reason="again"'))
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.event["kind"], "refresh_investigation")
        self.assert_investigation_follows(out.record)

    def test_patch_work_carries_the_pending_feedback(self):
        due = record("fix_needed", pending_feedback_evidence_ids=["review-1"])
        out = tick(due, observe=False)
        self.assertEqual(kinds(out), ["request_patch"])
        self.assertEqual(final(out, due)["active_work"]["feedback_evidence_ids"], ["review-1"])


def migrate(rec):
    return compat.migrate_development(rec, now="2026-11-23T10:00:00+00:00")


class TestDevelopmentMigration(unittest.TestCase):
    def legacy(self, activity, **fields):
        rec = record(activity)
        rec.update(fields)
        schemas.validate("contribution", rec)
        return rec

    def test_each_legacy_shape_becomes_the_state_of_the_table(self):
        base_head = {"fork": dict(contribution()["fork"], head_sha=SHA_C)}
        cases = {
            "idle on the provisioned base": (self.legacy("idle", **base_head),
                                             "development.fix_needed"),
            "idle with CI evidence": (self.legacy("idle", last_ci_evidence_id="ci-1"),
                                      "development.waiting_ci"),
            "idle on a published head": (self.legacy("idle"), "development.waiting_ci"),
            "paused idle": (self.legacy("idle", paused=True, paused_at=NOW, paused_by="human",
                                        **base_head), "development.fix_needed"),
            "planned development PR": (self.legacy("waiting_capacity",
                                                   active_operation_id="op-pr"),
                                       "development.waiting_ci"),
            "held publication": (self.legacy("waiting_capacity",
                                             pending_publication=dict(HELD)),
                                 "development.agent_running"),
            "CI access wait": (self.legacy("waiting_human"), "development.waiting_ci"),
        }
        for name, (rec, target) in cases.items():
            with self.subTest(name):
                self.assertTrue(compat.needs_development_migration(rec))
                self.assertTrue(compat.awaits_development_migration(rec))
                new, event = migrate(rec)
                self.assertEqual(engine.state_key(new), target)
                self.assertIsNone(engine.state_problem(new))
                self.assertEqual(new["revision"], rec["revision"])
                changed = {k for k in rec if rec[k] != new[k]}
                self.assertEqual(changed - {"activity", "updated_at"}, set())
                self.assertEqual((event["kind"], event["cause"]),
                                 (compat.DEVELOPMENT_MIGRATED,
                                  {"type": "migration", "id": compat.DEVELOPMENT_MIGRATION}))
                self.assertFalse(event["revision_bumped"])
                self.assertFalse(compat.needs_development_migration(new))

    def test_the_work_deadline_wait_becomes_work_failed(self):
        for phase in ("investigation", "development", "upstream_review"):
            with self.subTest(phase=phase):
                rec = contribution(phase=phase, activity="waiting_human", pending_human_action={
                    "kind": "task_without_outcome", "requested_at": NOW, "reminded_at": None,
                    "escalated_at": None})
                new, event = migrate(rec)
                self.assertEqual((engine.state_key(new), new["pending_human_action"]["kind"],
                                  new["pending_human_action"]["counter"],
                                  new["pending_human_action"]["requested_at"]),
                                 (f"{phase}.waiting_human", "work_failed", None, NOW))
                self.assertEqual(event["details"]["pending_human_action"],
                                 {"from": "task_without_outcome", "to": "work_failed"})
        resumed = apply_command(new, commands.parse(
            f"/upstreaming resume revision={new['revision']} reason=r"))
        self.assertEqual(resumed.event["kind"], "resume_upstream_revision")

    def test_other_shapes_fail_closed(self):
        refused = {
            "requested Work": self.legacy("idle", active_work=dict(WORK)),
            "exhausted publication": self.legacy(
                "waiting_capacity", pending_publication=dict(HELD, exhausted=True)),
            "paused with a publication": self.legacy(
                "waiting_capacity", pending_publication=dict(HELD), paused=True,
                paused_at=NOW, paused_by="human"),
            "pending action in idle": self.legacy("idle", pending_human_action={
                "kind": "work_failed", "requested_at": NOW, "reminded_at": None,
                "escalated_at": None}),
            "publication in idle": self.legacy("idle", pending_publication=dict(HELD)),
        }
        for name, rec in refused.items():
            with self.subTest(name):
                self.assertTrue(compat.needs_development_migration(rec))
                self.assertFalse(compat.awaits_development_migration(rec))
                with self.assertRaises(lifecycle.TransitionError):
                    migrate(rec)
        # Records of the table need none.
        for rec in (record(), running(), record("fix_needed"),
                    record("waiting_human", pending_human_action={
                        "kind": "work_failed", "requested_at": NOW, "reminded_at": None,
                        "escalated_at": None})):
            self.assertFalse(compat.needs_development_migration(rec))

    def test_the_controller_migrates_in_its_own_commit_and_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / PATH
            path.parent.mkdir(parents=True)
            legacy = self.legacy("idle")
            path.write_text(canonical_json(legacy))
            # The store admits the record only as the migration reads it,
            # and never writes the old shape.
            self.assertEqual(state.check_blob(PATH, path.read_bytes(), POLICY), [])
            self.assertEqual(state.pending_record_migrations(root, POLICY), [PATH])
            written = state.apply_pending_record_migrations(root, POLICY, now=NOW)
            self.assertEqual(sorted(written),
                             [PATH, ".state/events/example--proj--12/00000000.json"])
            migrated = json.loads(path.read_text())
            self.assertEqual(engine.state_key(migrated), "development.waiting_ci")
            self.assertEqual(state.pending_record_migrations(root, POLICY), [])
            self.assertEqual(state.apply_pending_record_migrations(root, POLICY, now=NOW), [])
            path.write_text(canonical_json(self.legacy("idle", active_work=dict(WORK))))
            self.assertTrue(state.check_blob(PATH, path.read_bytes(), POLICY))
            with self.assertRaises(state.StateError):
                state.pending_record_migrations(root, POLICY)

    def test_the_live_record_needs_no_migration(self):
        live = Path(".state/contributions/closeio--ciso8601--1.json")
        if live.exists():
            self.assertFalse(compat.needs_development_migration(json.loads(live.read_text())))


class TestNoLegacyDevelopmentWrites(unittest.TestCase):
    def test_development_has_no_legacy_state_or_write_site(self):
        self.assertFalse(any(s.startswith("development.") for s in compat.LEGACY_STATES))
        self.assertFalse(any(site.startswith(("controller.", "observe_ci."))
                             for site in compat.LEGACY_SITES))
        for verb in ("record_publish_success", "reject_patch_proposal", "replan_development",
                     "schedule_publish_retry", "exhaust_publish_recovery", "retry_dispatch",
                     "exhaust_dispatch_recovery", "record_ci_failure", "record_pr_mismatch",
                     "record_actionable_review", "record_explanation", "record_head_change"):
            self.assertIn(verb, engine.PIPELINE_VERBS)

    def test_nomination_reads_pending_feedback_from_the_record(self):
        self.assertEqual(engine.PREDICATES["no pending actionable feedback"].kind, "record")
        rec = record(pending_feedback_evidence_ids=["review-1"])
        out = engine.apply_transition(
            rec, "nominate_for_handoff", policy=POLICY, now=NOW, facts={"ci_passed_on_head"},
            values={"last_ci_evidence_id": "ci-1",
                    "handoff_request": {"command_id": "c", "head": SHA_A, "revision": 0}})
        self.assertFalse(out.accepted)
        self.assertIn("review-1", out.reason)

    def test_the_selection_requests_no_work_outside_the_table(self):
        legacy = record("idle")
        res = controller.tick(contributions={"c": legacy}, operations={}, consumed={},
                              projects={}, policy=POLICY,
                              inputs=controller.Inputs(now=T, pressure=capacity.Pressure(ok=True)))
        self.assertEqual((res.requested_effects, res.staged), ([], []))


class TestLedger(unittest.TestCase):
    def test_every_development_verb_has_a_message(self):
        import status_ledger
        for verb in ("record_publish_success", "reject_patch_proposal", "replan_development",
                     "schedule_publish_retry", "exhaust_publish_recovery", "retry_dispatch",
                     "exhaust_dispatch_recovery", "record_ci_failure", "record_pr_mismatch",
                     compat.DEVELOPMENT_MIGRATED):
            self.assertIn(verb, status_ledger.MESSAGES)
        rec = record()
        out = tick(rec, pull="development PR 1 does not exist")
        event = next(e for kind, e in out.staged if kind == "events")
        body = status_ledger.render(event, final(out, rec), "https://github.com/run")
        self.assertIn("development PR 1 does not exist", body)
        self.assertIn("/upstreaming resume revision=0", body)


if __name__ == "__main__":
    unittest.main()
