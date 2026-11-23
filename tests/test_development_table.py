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
import ci_rerun
import collect
import commands
import controller
import engine
import lifecycle
import observe_ci
import reconcile
import recovery
import schemas
from tests import test_ci_rerun
from tests.helpers import (
    NOW,
    OWNER,
    SHA_A,
    SHA_B,
    apply_command,
    ci_record,
    contribution,
    policy,
    runner_loss,
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
    return contribution(activity=activity, plan_evidence_id="plan-1", scope_paths=["a"],
                        scope_coverage_complete=True, **fields)


def running(**fields):
    fields.setdefault("active_work", dict(WORK))
    return record("agent_running", **fields)


def tick(rec, publications=(), *, observe=True, verdict="pending", head=SHA_A, feedback=None,
         now=T, evidence=None, runs=(), queue=None, pull=None, ci=None, operations=None,
         lost=(), api=None, ci_reruns=None):
    snapshot = reconcile.Snapshot({"c": copy.deepcopy(rec)}, dict(operations or {}), {}, {},
                                  dict(evidence or {}))

    def review_feedback(_api, _record, _authority, check_pull=False):
        if check_pull and pull:
            return {"pr_mismatch": pull}
        return copy.deepcopy(feedback)

    with mock.patch("observe_ci.collect.branch_head", return_value={"head": head}), \
            mock.patch("observe_ci.collect.development_review_feedback",
                       side_effect=review_feedback), \
            mock.patch("observe_ci.collect.ci_snapshot",
                       return_value=ci or ci_snapshot(head, verdict)), \
            mock.patch("observe_ci.collect.classify_runner_loss", side_effect=runner_loss(lost)), \
            contextlib.redirect_stderr(io.StringIO()):
        return reconcile.tick(api or (object() if observe else None), snapshot, policy=POLICY,
                              now=now, publications=list(publications),
                              pressure=capacity.Pressure(ok=True), observe=observe,
                              dispatcher_runs=list(runs), queue_works=queue or {},
                              ci_reruns=ci_reruns)


def final(output, rec):
    staged = [r for kind, r in output.staged if kind == "contributions"]
    return staged[-1] if staged else rec


def kinds(output):
    return [e["kind"] for kind, e in output.staged if kind == "events"]


def rerun_outcome(rec, status="rerun", runs=((777, 1, 2),)):
    """The outcome of a `no_change` Result whose trusted publication job
    reran the failed jobs (``ci_rerun.publish``): ``rerun`` with a confirmed
    operation, or ``superseded`` or ``nothing_to_rerun`` without one."""
    out = dict(publication(rec, "no_change"), stage="ci_rerun",
               rerun={"status": status, "runs": []})
    if status == "rerun":
        out["rerun"]["runs"] = [{"run_id": r, "from_attempt": f, "run_attempt": a}
                                for r, f, a in runs]
        op = ci_rerun.operation(rec, rec["fork"]["head_sha"],
                                [{"run_id": r, "from_attempt": f} for r, f, _a in runs],
                                ["ci-0", "work:x", "publish-attempt:1"], POLICY, NOW)
        out["operation"] = recovery.fold_receipt(
            op, external_ids=ci_rerun.attempt_ids(out["rerun"]["runs"]), detail="read back",
            now=NOW)
    return out


class TestPatchResults(unittest.TestCase):
    """Every Result of patch Work takes a row of `development.agent_running`."""

    def test_each_publisher_result_takes_its_row(self):
        expected = {
            "published": ("record_publish_success", "development.waiting_ci", False),
            "uncertain": ("record_publish_success", "development.waiting_ci", False),
            "proposal": ("reject_patch_proposal", "development.fix_needed", True),
            # A `no_change` report without the receipt of the trusted rerun
            # is a failed publication; with it, see the next test.
            "no_change": ("schedule_publish_retry", "development.agent_running", False),
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
                self.assertIsNone(engine.state_problem(new))
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
            rec, rerun_outcome(rec), now=NOW, policy=POLICY, worker_run_id="555")
        events = [e for kind, e in staged if kind == "events"]
        self.assertEqual([e["kind"] for e in events], ["request_ci_rerun"])
        self.assertEqual(events[0]["details"]["rerun"]["status"], "rerun")
        self.assertEqual(events[0]["details"]["rerun"]["runs"],
                         [{"run_id": 777, "from_attempt": 1, "run_attempt": 2}])
        [op] = [o for kind, o in staged if kind == "operations"]
        self.assertEqual((op["kind"], op["status"], op["external_ids"]["attempt:777"]),
                         ("ci_rerun", "confirmed", 2))
        self.assertEqual((engine.state_key(new), new["revision"], new["last_ci_evidence_id"],
                          new["active_work"], new["pending_human_action"]),
                         ("development.waiting_ci", rec["revision"], None, None, None))
        self.assertEqual(new["budget"]["reruns"], {"head_sha": SHA_A, "flaky": 1, "interrupted": 0})
        self.assertEqual(new["budget"]["no_progress_streak"], rec["budget"]["no_progress_streak"])
        # Passing CI on the head nominates; a failed run is a failure again.
        self.assertEqual(kinds(tick(new, verdict="passed")), ["nominate_for_handoff"])
        self.assertEqual(kinds(tick(new, verdict="failed"))[:1], ["record_ci_failure"])
        # `flaky_reruns` is spent for this head: the next report is rejected.
        again = dict(running(), budget=new["budget"])
        rejected, staged = reconcile.apply_publication(
            again, rerun_outcome(again), now=NOW, policy=POLICY, worker_run_id="555")
        self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                         ["reject_patch_proposal"])
        self.assertEqual(engine.state_key(rejected), "development.fix_needed")
        self.assertEqual(rejected["budget"]["no_progress_streak"],
                         again["budget"]["no_progress_streak"] + 1)
        # Work that carries feedback never takes the rerun.
        carried = running(pending_feedback_evidence_ids=["review-1"])
        carried["active_work"]["feedback_evidence_ids"] = ["review-1"]
        rejected, staged = reconcile.apply_publication(
            carried, rerun_outcome(carried), now=NOW, policy=POLICY, worker_run_id="555")
        self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                         ["reject_patch_proposal"])
        self.assertEqual(rejected["pending_feedback_evidence_ids"], ["review-1"])

    def test_a_no_change_result_of_patch_work_is_published_by_the_rerun(self):
        """`development_patch` Work (requested without pending feedback) may
        take `request_ci_rerun` like repair Work: the trusted publication job
        admits its `no_change` Result (`ci_rerun.preflight`), and the
        reconciler folds the receipt by the row, never as a failed
        publication."""
        work = dict(WORK, task="development_patch",
                    dedupe_key=WORK["dedupe_key"].replace("development_repair",
                                                          "development_patch"))
        rec = running(active_work=work, last_ci_evidence_id="ci-0")
        record_ = copy.deepcopy(rec)
        record_["active_work"] = dict(test_ci_rerun.WORK, task="development_patch")
        pre = ci_rerun.preflight(
            record=record_, task=test_ci_rerun.task(task="development_patch"),
            proposal=test_ci_rerun.no_change_proposal(record_), policy=POLICY, now=NOW,
            fork_lookup=lambda _n: {"id": 2, "full_name": rec["fork"]["name"]})
        self.assertEqual(pre, {"rerun": True, "fork_name": "proj", "reason": None})
        for status in ("nothing_to_rerun", "rerun"):
            with self.subTest(receipt=status):
                new, staged = reconcile.apply_publication(
                    rec, rerun_outcome(rec, status), now=NOW, policy=POLICY,
                    worker_run_id="555")
                self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                                 ["request_ci_rerun"])
                self.assertEqual(engine.state_key(new), "development.waiting_ci")
                self.assertIsNone(engine.state_problem(new))
                self.assertEqual(kinds(tick(new, verdict="passed")), ["nominate_for_handoff"])

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
    """`development.waiting_ci` is observed by its rows; `development.fix_needed`
    only for a moved head and for review feedback on the recorded head."""

    def test_a_review_while_work_is_due_is_carried_by_the_next_work(self):
        failed = next(e for kind, e in tick(record(), verdict="failed").staged
                      if kind == "evidence")
        for evidence, verb in ((None, "request_patch"), (failed["evidence_id"], "request_repair")):
            with self.subTest(verb=verb):
                rec = record("fix_needed", last_ci_evidence_id=evidence)
                ev = {evidence: failed} if evidence else {}
                out = tick(rec, feedback=REVIEW, verdict="failed", evidence=ev)
                after = final(out, rec)
                self.assertEqual(kinds(out)[0], "record_actionable_review")
                review = next(e for kind, e in out.staged if kind == "events")
                self.assertEqual((review["details"]["from_state"], review["details"]["to_state"]),
                                 ("development.fix_needed", "development.fix_needed"))
                self.assertEqual(review["revision_bumped"], True)
                self.assertEqual(after["processed_review_ids"], [77])
                self.assertEqual(after["last_ci_evidence_id"], evidence)
                self.assertNotIn("record_ci_failure", kinds(out))
                self.assertEqual(kinds(out)[1:], [verb])
                self.assertIn("review-2-1-77", after["active_work"]["feedback_evidence_ids"])
                self.assertEqual(after["revision"], rec["revision"] + 1)

    def test_a_review_on_a_paused_record_while_work_is_due_is_recorded(self):
        """Observation rows are not masked by a pause: the review is recorded
        in `development.fix_needed` and no Work is requested while paused."""
        paused = apply_command(record("fix_needed"),
                               commands.parse('/upstreaming pause reason="hold"'))
        self.assertTrue(paused.accepted)
        rec = paused.record
        out = tick(rec, feedback=REVIEW, verdict="failed")
        after = final(out, rec)
        self.assertEqual(kinds(out), ["record_actionable_review"])
        self.assertEqual((engine.state_key(after), after["paused"], after["revision"]),
                         ("development.fix_needed", True, rec["revision"] + 1))
        self.assertEqual(after["pending_feedback_evidence_ids"], ["review-2-1-77"])
        self.assertIsNone(after["active_work"])

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


class TestFlakyRerun(unittest.TestCase):
    """`request_ci_rerun` is taken once the trusted publication job reran the
    failed jobs on the exact head and read the new attempt back (or found
    nothing to rerun); a `no_change` Result without that receipt is a
    failed publication, which the publish-only retry performs."""

    def setUp(self):
        failing = observed_ci_id()
        self.evidence = {failing: ci_record(failing, SHA_A, failing=JOB)}
        due = record("fix_needed", last_ci_evidence_id=failing)
        out = tick(due, observe=False, evidence=self.evidence)
        self.assertEqual(kinds(out), ["request_repair"])
        self.running = final(out, due)
        self.work = self.running["active_work"]["dedupe_key"]
        self.assertEqual(submitted(out), [self.work])

    def result(self, outcome):
        return dict(outcome, task={"contribution_id": self.running["contribution_id"],
                                   "revision": self.running["revision"], "generation": 0,
                                   "dedupe_key": self.work, "task": "development_repair"})

    def test_a_confirmed_rerun_is_awaited_then_acted_on(self):
        import work
        out = tick(self.running, [self.result(rerun_outcome(self.running))], verdict="failed",
                   evidence=self.evidence)
        self.assertEqual(kinds(out), ["request_ci_rerun"])
        waiting = final(out, self.running)
        [op] = [o for kind, o in out.staged if kind == "operations"]
        self.assertEqual((op["kind"], op["status"]), ("ci_rerun", "confirmed"))
        self.assertEqual((engine.state_key(waiting), waiting["pending_human_action"]),
                         ("development.waiting_ci", None))
        self.assertIn("behind a confirmed rerun", out.report["observation"][0]["note"])
        self.assertEqual(submitted(out), [])
        operations = {op["operation_id"]: op}
        # The snapshot still shows the failed attempt 1: nothing is decided.
        queue = {work.graph_id(self.work): "completed"}
        for minutes in (10, 20):
            later = tick(waiting, verdict="failed", evidence=self.evidence, queue=queue,
                         operations=operations, now=T + timedelta(minutes=minutes))
            self.assertEqual((kinds(later), submitted(later)), ([], []))
            waiting = final(later, waiting)
        # The new attempt fails: repair Work under an id other than the Work
        # that returned the Result.
        out = attempt_tick(waiting, 2, "failed", evidence=self.evidence, queue=queue,
                           operations=operations, now=T + timedelta(minutes=70))
        self.assertEqual(kinds(out), ["record_ci_failure", "request_repair"])
        [repair] = submitted(out)
        self.assertEqual(repair, self.work.replace(".r0:", ".r0.rerun1:"))
        # The new attempt passes: the head is nominated.
        out = attempt_tick(waiting, 2, "passed", evidence=self.evidence, operations=operations)
        self.assertEqual(kinds(out), ["nominate_for_handoff"])

    def test_nothing_to_rerun_records_the_same_failure_again(self):
        """No stall: with nothing rerun, the failing evidence of the head is
        recorded again, bounded by `flaky_reruns` and `no_progress`."""
        out = tick(self.running, [self.result(rerun_outcome(self.running, "nothing_to_rerun"))],
                   verdict="failed", evidence=self.evidence)
        self.assertEqual(kinds(out)[:2], ["request_ci_rerun", "record_ci_failure"])
        self.assertEqual(engine.state_key(final(out, self.running)), "development.agent_running")

    def test_a_result_without_a_receipt_is_a_failed_publication(self):
        rec = running(last_ci_evidence_id="ci-0")
        new, staged = reconcile.apply_publication(
            rec, dict(publication(rec, "no_change")), now=NOW, policy=POLICY,
            worker_run_id="555")
        events = [e for kind, e in staged if kind == "events"]
        self.assertEqual([e["kind"] for e in events], ["schedule_publish_retry"])
        self.assertEqual(events[0]["details"]["rerun"], "the Result carries no CI rerun receipt")
        self.assertEqual((engine.state_key(new), new["pending_publication"]["attempts"]),
                         ("development.agent_running", 1))
        # Each launched retry that reruns nothing is charged, until the
        # human wait.
        verbs = []
        for attempt in range(2, 6):
            held = new["pending_publication"]
            new = dict(new, pending_publication=dict(held, launched_at=NOW))
            new, staged = reconcile.apply_publication(
                new, dict(publication(new, "no_change"), attempt=attempt), now=NOW,
                policy=POLICY, worker_run_id="555")
            verbs += [e["kind"] for kind, e in staged if kind == "events"]
            if engine.state_key(new) == "development.waiting_human":
                break
        self.assertEqual(verbs[-1], "exhaust_publish_recovery")
        self.assertEqual(new["pending_human_action"]["kind"], "publication_infrastructure_failure")
        # A retry that reruns the jobs ends the held publication.
        rec = running(last_ci_evidence_id="ci-0")
        held, _ = reconcile.apply_publication(rec, dict(publication(rec, "no_change")),
                                              now=NOW, policy=POLICY, worker_run_id="555")
        held = dict(held, pending_publication=dict(held["pending_publication"], launched_at=NOW))
        new, staged = reconcile.apply_publication(
            held, dict(rerun_outcome(held), attempt=2), now=NOW, policy=POLICY,
            worker_run_id="555")
        self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                         ["request_ci_rerun"])
        self.assertIsNone(new["pending_publication"])

    def test_a_receipt_for_another_head_is_not_a_receipt(self):
        rec = running(last_ci_evidence_id="ci-0")
        outcome = rerun_outcome(rec)
        outcome["operation"]["target"]["expected_head"] = SHA_B
        new, staged = reconcile.apply_publication(rec, outcome, now=NOW, policy=POLICY,
                                                  worker_run_id="555")
        self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                         ["schedule_publish_retry"])

    def test_work_that_returned_a_result_is_never_allocated_again(self):
        """An allocation that would name a Work graph the queue holds as
        terminal, or that returned a Result in this tick, is terminal Work
        without a usable Result: a fresh graph is allocated, never a
        resubmission."""
        import work
        due = record("fix_needed", last_ci_evidence_id=observed_ci_id())
        key = work.work_key(due["contribution_id"], 0, due["revision"], "development_repair")
        fresh = work.work_key(due["contribution_id"], 0, due["revision"] + 1, "development_repair")
        out = tick(due, observe=False, evidence=self.evidence,
                   queue={work.graph_id(key): "completed"})
        self.assertEqual(kinds(out), ["request_repair", "allocate_work_retry"])
        self.assertEqual(submitted(out), [fresh])
        out = tick(due, [dict(publication(due, "published"),
                              task={"contribution_id": due["contribution_id"],
                                    "revision": due["revision"], "generation": 0,
                                    "dedupe_key": key, "task": "development_repair"})],
                   observe=False, evidence=self.evidence)
        self.assertEqual(kinds(out)[-2:], ["request_repair", "allocate_work_retry"])
        self.assertNotIn("pause_on_processing_error", kinds(out))
        self.assertEqual(submitted(out), [fresh])


def interrupted_ci(conclusion="cancelled", attempt=1, failed_too=False):
    """The fixture's CI run with its required job interrupted: cancelled, or
    failed after it lost its runner (``conclusion="failure"`` with job 1 in
    ``lost``). ``failed_too`` adds a required job that failed on its own."""
    snapshot = ci_snapshot(SHA_A, "failed")
    snapshot.runs[0].update(run_attempt=attempt, conclusion=conclusion)
    snapshot.jobs_by_run[777][0]["conclusion"] = conclusion
    if failed_too:
        snapshot.jobs_by_run[777].append(dict(snapshot.jobs_by_run[777][0], id=2,
                                              name="Test - riscv64", conclusion="failure"))
    return snapshot


class RunsApi:
    """The collector's reads of the runs a CI rerun names."""

    def __init__(self, attempt=2, head=SHA_A):
        self.attempt, self.head = attempt, head

    def get(self, path):
        assert path == "/repos/riseproject-upstreaming/proj/actions/runs/777", path
        return {"id": 777, "head_sha": self.head, "run_attempt": self.attempt,
                "status": "queued"}


def rerun_receipt(op, status="rerun"):
    return {"schema_version": 1, "status": status, "operation_id": op["operation_id"],
            "contribution_id": op["contribution_id"], "revision": op["revision"],
            "repository": op["target"]["repository"], "repository_id": 2,
            "head_sha": op["target"]["expected_head"], "ci_rerun_run_id": 99,
            "runs": [{"run_id": 777, "from_attempt": 1, "run_attempt": 2}]}


class TestInterruptedRerun(unittest.TestCase):
    """A required job that was cancelled or lost its runner, with no job
    failed on its own, takes `rerun_interrupted_ci` in
    `development.waiting_ci`: a `ci_rerun` operation is recorded before the
    effect and launched through the development PR workflow, within
    `interrupted_reruns`; `exhaust_interrupted_reruns` waits for a human."""

    def planned(self, rec=None, **kw):
        rec = rec or record()
        out = tick(rec, ci=interrupted_ci(**kw), **({"lost": [1]} if kw.get("conclusion")
                                                    == "failure" else {}))
        self.assertEqual(kinds(out), ["rerun_interrupted_ci"])
        [op] = [o for kind, o in out.staged if kind == "operations" and o["status"] == "planned"]
        return out, final(out, rec), op

    def test_the_row_plans_the_operation_and_launches_it_once(self):
        out, rec, op = self.planned()
        self.assertEqual((engine.state_key(rec), rec["active_operation_id"],
                          rec["budget"]["reruns"]["interrupted"]),
                         ("development.waiting_ci", op["operation_id"], 1))
        self.assertEqual((op["kind"], op["proposed"]["marker"], op["target"]["expected_head"]),
                         ("ci_rerun", "ci-rerun:777@1", SHA_A))
        self.assertEqual(op["external_ids"]["launch_attempts"], 1)
        self.assertEqual(out.report["dev_pr_requests"],
                         [{"operation_id": op["operation_id"], "contribution_id":
                           rec["contribution_id"], "attempt": 1}])
        [event] = [e for kind, e in out.staged if kind == "events"]
        self.assertEqual(event["details"]["charged"], ["interrupted_reruns"])
        # While the operation is open, interrupted CI changes nothing and the
        # operation is not launched again before the launch interval.
        ops = {op["operation_id"]: op}
        later = tick(rec, ci=interrupted_ci(), operations=ops, now=T + timedelta(minutes=2))
        self.assertEqual((kinds(later), later.report["dev_pr_requests"]), ([], []))
        self.assertIn("open operation", later.report["observation"][0]["note"])

    def test_a_runner_loss_is_interrupted_and_a_real_failure_wins(self):
        self.planned(conclusion="failure")
        out = tick(record(), ci=interrupted_ci(failed_too=True))
        self.assertEqual(kinds(out)[:1], ["record_ci_failure"])
        # Without the runner-loss annotation the failed job is a failure.
        out = tick(record(), ci=interrupted_ci(conclusion="failure"))
        self.assertEqual(kinds(out)[:1], ["record_ci_failure"])

    def test_a_lost_runner_on_a_job_that_is_not_required_is_rerun_not_nominated(self):
        """The gate reads CI unclassified and calls a job that lost its
        runner failed, so development never nominates such a head: the job
        is interrupted and rerun within `interrupted_reruns`."""
        snapshot = ci_snapshot(SHA_A, "passed")
        snapshot.jobs_by_run[777].append(dict(snapshot.jobs_by_run[777][0], id=2,
                                              name="Lint", conclusion="failure"))
        out = tick(record(), ci=snapshot, lost=[2])
        self.assertEqual(kinds(out), ["rerun_interrupted_ci"])
        [op] = [o for kind, o in out.staged if kind == "operations" and o["status"] == "planned"]
        self.assertEqual(op["proposed"]["marker"], "ci-rerun:777@1")
        # Without the runner-loss annotation the same CI is a failure.
        out = tick(record(), ci=snapshot)
        self.assertEqual(kinds(out)[:1], ["record_ci_failure"])

    def test_a_confirmed_rerun_releases_the_operation_and_is_awaited(self):
        _out, rec, op = self.planned()
        ops = {op["operation_id"]: op}
        # The receipt is read back: not yet a new attempt, nothing decided.
        out = tick(rec, ci=interrupted_ci(), operations=ops, api=RunsApi(attempt=1),
                   ci_reruns=[rerun_receipt(op)], now=T + timedelta(minutes=1))
        self.assertEqual(out.report["ci_reruns"][0]["outcome"], "new attempt not read back yet")
        self.assertEqual(kinds(out), [])
        # Read back: the operation is confirmed and released, no row fired,
        # and the snapshot behind the new attempt is awaited.
        out = tick(rec, ci=interrupted_ci(), operations=ops, api=RunsApi(),
                   ci_reruns=[rerun_receipt(op)], now=T + timedelta(minutes=2))
        self.assertEqual(kinds(out), ["ci_rerun_recorded"])
        after = final(out, rec)
        self.assertIsNone(after["active_operation_id"])
        [closed] = [o for kind, o in out.staged if kind == "operations"]
        self.assertEqual((closed["status"], closed["external_ids"]["attempt:777"]),
                         ("confirmed", 2))
        self.assertIn("behind a confirmed rerun", out.report["observation"][0]["note"])
        # The new attempt passes: nominated.
        ops = {op["operation_id"]: closed}
        out = tick(after, ci=interrupted_ci(attempt=2), operations=ops)
        self.assertEqual(kinds(out), ["rerun_interrupted_ci"])
        passing = ci_snapshot(SHA_A, "passed")
        passing.runs[0]["run_attempt"] = 2
        out = tick(after, ci=passing, operations=ops)
        self.assertEqual(kinds(out), ["nominate_for_handoff"])

    def test_a_run_that_cannot_be_read_back_leaves_the_tick_to_the_launch_budget(self):
        """A run of the marker that can no longer be read (deleted in the
        fork) does not skip the contribution: the receipt stays unread, the
        observation runs, and the spent launches close the operation."""
        _out, rec, op = self.planned()
        launched = recovery.charge_launch(op, NOW)
        ops = {op["operation_id"]: launched}

        class Gone:
            def get(self, path):
                raise collect.CollectError(f"GET {path}: HTTP 404")

        out = tick(rec, ci=interrupted_ci(), operations=ops, api=Gone(),
                   ci_reruns=[rerun_receipt(op)], now=T + timedelta(minutes=1))
        self.assertEqual(out.report["ci_reruns"][0]["outcome"], "new attempt not read back yet")
        self.assertNotIn("c", out.report.get("skipped") or {})
        limit = lifecycle.budget_limit("operation_launch", POLICY)
        spent = launched
        for _ in range(limit - 1):
            spent = recovery.charge_launch(spent, NOW)
        out = tick(rec, ci=interrupted_ci(), operations={op["operation_id"]: spent}, api=Gone(),
                   ci_reruns=[rerun_receipt(op)], now=T + timedelta(hours=1))
        self.assertEqual(out.report["controller"]["ci_rerun_closures"][0]["closed"],
                         "operation_launch is spent without a receipt")

    def test_failed_reruns_exhaust_the_budget_and_resume_ci_reruns_again(self):
        _out, rec, op = self.planned()
        ops = {op["operation_id"]: op}
        # The workflow refuses: the operation closes failed, is released,
        # and the same observation plans the next rerun, charged.
        out = tick(rec, ci=interrupted_ci(), operations=ops, api=RunsApi(),
                   ci_reruns=[rerun_receipt(op, "refused")], now=T + timedelta(minutes=1))
        self.assertEqual(kinds(out), ["rerun_interrupted_ci"])
        statuses = {o["operation_id"]: o["status"] for kind, o in out.staged
                    if kind == "operations"}
        self.assertEqual(statuses[op["operation_id"]], "failed")
        rec = final(out, rec)
        self.assertEqual(rec["budget"]["reruns"]["interrupted"], 2)
        second = rec["active_operation_id"]
        self.assertNotEqual(second, op["operation_id"])
        ops = {o["operation_id"]: o for kind, o in out.staged if kind == "operations"}
        # Its launches are spent without a receipt: closed and released, no
        # row; the next observation exhausts the budget.
        now = T + timedelta(minutes=1)
        for _ in range(4):
            now += timedelta(minutes=6)
            out = tick(rec, ci=interrupted_ci(), operations=ops, now=now)
            rec = final(out, rec)
            ops.update({o["operation_id"]: o for kind, o in out.staged if kind == "operations"})
            if engine.state_key(rec) == "development.waiting_human":
                break
        self.assertEqual(ops[second]["status"], "failed")
        self.assertEqual(kinds(out), ["exhaust_interrupted_reruns"])
        self.assertEqual((engine.state_key(rec), rec["pending_human_action"]["kind"],
                          rec["pending_human_action"]["counter"]),
                         ("development.waiting_human", "ci_interrupted", "interrupted_reruns"))
        # `resume_ci` restarts the counter, and the next observation reruns.
        out = apply_command(rec, commands.parse(
            f"/upstreaming resume revision={rec['revision']} reason=fixed"), policy_=POLICY)
        self.assertEqual(out.event["kind"], "resume_ci")
        resumed = out.record
        self.assertEqual(engine.state_key(resumed), "development.waiting_ci")
        self.assertEqual(resumed["budget"]["reruns"]["interrupted"], 0)
        out, after, _op = self.planned(resumed)
        self.assertEqual(after["budget"]["reruns"]["interrupted"], 1)

    def test_a_paused_contribution_is_not_rerun(self):
        rec = record(paused=True, paused_reason="maintenance", paused_by="human",
                     paused_at=NOW)
        out = tick(rec, ci=interrupted_ci())
        self.assertEqual(kinds(out), [])
        self.assertIn("paused", out.report["observation"][0]["note"])

    def test_a_pause_closes_the_planned_rerun_so_park_and_a_late_resume_apply(self):
        """The workflow refuses a paused record, so a planned rerun cannot
        get a receipt while paused: it is closed as failed and released
        instead of fencing `park` and a stale `resume` forever."""
        _out, planned, op = self.planned()
        ops = {op["operation_id"]: op}
        # While the operation is open, the pause holds it and park is refused.
        paused = apply_command(planned, commands.parse("/upstreaming pause reason=maintenance"),
                               policy_=POLICY, operations=ops).record
        self.assertTrue(paused["paused"])
        refused = apply_command(paused, commands.parse(
            f"/upstreaming park revision={paused['revision']} reason=later"),
            policy_=POLICY, operations=ops)
        self.assertIsNone(refused.event)
        self.assertIn("is open", refused.reason)
        out = tick(paused, ci=interrupted_ci(), operations=ops, now=T + timedelta(minutes=10))
        self.assertEqual(out.report["dev_pr_requests"], [])
        self.assertEqual(kinds(out), [])
        self.assertEqual(out.report["controller"]["ci_rerun_closures"][0]["closed"],
                         "the contribution is paused")
        [closed] = [o for kind, o in out.staged if kind == "operations"]
        self.assertEqual(closed["status"], "failed")
        after = final(out, paused)
        self.assertIsNone(after["active_operation_id"])
        self.assertTrue(after["paused"])
        self.assertEqual(engine.state_key(after), "development.waiting_ci")
        self.assertEqual(after["revision"], paused["revision"])
        schemas.validate("contribution", after)
        ops = {closed["operation_id"]: closed}
        # Nothing is planned again while paused.
        again = tick(after, ci=interrupted_ci(), operations=ops, now=T + timedelta(minutes=20))
        self.assertEqual((kinds(again), [o for kind, o in again.staged if kind == "operations"]),
                         ([], []))
        # Park is accepted.
        parked = apply_command(after, commands.parse(
            f"/upstreaming park revision={after['revision']} reason=later"),
            policy_=POLICY, operations=ops, now=NOW)
        self.assertIsNotNone(parked.event, parked)
        self.assertEqual(engine.state_key(parked.record).split(".")[0], "parked")
        # Resume after more than refresh.stale_after_days is accepted (the
        # stale stop restarts from planning).
        late = (datetime.fromisoformat(NOW) + timedelta(days=8)).isoformat()
        resumed = apply_command(after, commands.parse(
            f"/upstreaming resume revision={after['revision']} reason=back"),
            policy_=POLICY, operations=ops, now=late)
        self.assertEqual(resumed.event["kind"], "resume_stale")
        self.assertFalse(resumed.record.get("paused"))
        # A resume within it keeps the state, and the first observation after
        # it reruns within the budget.
        resumed = apply_command(after, commands.parse(
            f"/upstreaming resume revision={after['revision']} reason=back"),
            policy_=POLICY, operations=ops, now=NOW)
        self.assertEqual(resumed.event["kind"], "resume_pause")
        out = tick(resumed.record, ci=interrupted_ci(), operations=ops,
                   now=T + timedelta(minutes=30))
        self.assertEqual(kinds(out), ["rerun_interrupted_ci"])
        self.assertEqual(final(out, resumed.record)["budget"]["reruns"]["interrupted"], 2)

    def test_a_moved_head_closes_the_operation(self):
        _out, rec, op = self.planned()
        out = tick(rec, ci=ci_snapshot(SHA_B, "pending"), head=SHA_B,
                   operations={op["operation_id"]: op}, now=T + timedelta(minutes=1))
        self.assertEqual(kinds(out), ["record_head_change"])
        closed = [o for kind, o in out.staged if kind == "operations"]
        self.assertEqual([o["status"] for o in closed], ["failed"])
        self.assertIsNone(final(out, rec)["active_operation_id"])
        self.assertEqual(out.report["controller"]["ci_rerun_closures"][0]["closed"],
                         "the operation no longer matches the record")


class TestSpentWork(unittest.TestCase):
    """A row that keeps the revision can name Work whose graph already
    returned a Result or is terminal in the queue. That Work is terminal
    without a usable Result: a fresh graph is allocated in the same tick
    (`allocate_work_retry`), or the contribution waits for a human
    (`exhaust_work_recovery`), and the spent graph is never submitted."""

    def setUp(self):
        failing = observed_ci_id()
        self.evidence = {failing: ci_record(failing, SHA_A, failing=JOB)}

    def check(self, output):
        for kind, staged in output.staged:
            schemas.validate({"contributions": "contribution", "events": "event"}.get(kind, kind),
                             staged)
        self.assertEqual(output.report.get("failures") or [], [])

    def uncertain_publication(self):
        """Repair Work whose uncertain publication is recorded at the same
        revision, and the graph of that Work, completed in the queue."""
        import work
        due = record("fix_needed", last_ci_evidence_id=observed_ci_id())
        out = tick(due, observe=False, evidence=self.evidence)
        running_ = final(out, due)
        key = running_["active_work"]["dedupe_key"]
        out = tick(running_, [publication(running_, "uncertain")], observe=False,
                   evidence=self.evidence)
        self.assertEqual(kinds(out), ["record_publish_success"])
        waiting = final(out, running_)
        self.assertEqual(waiting["revision"], running_["revision"])
        return waiting, key, {work.graph_id(key): "completed"}

    def test_an_uncertain_publication_that_never_landed_is_repaired_on_a_fresh_graph(self):
        waiting, key, queue = self.uncertain_publication()
        # The push never landed: the same head fails again, and the repair
        # Work at the kept revision names the spent graph.
        out = tick(waiting, verdict="failed", evidence=self.evidence, queue=queue)
        self.check(out)
        self.assertEqual(kinds(out), ["record_ci_failure", "request_repair", "allocate_work_retry"])
        rec = final(out, waiting)
        self.assertEqual((engine.state_key(rec), rec["paused"], rec["revision"]),
                         ("development.agent_running", False, waiting["revision"] + 1))
        self.assertEqual(rec["active_work"]["attempt"], 2)
        self.assertEqual(submitted(out), [rec["active_work"]["dedupe_key"]])
        self.assertNotIn(key, submitted(out))
        self.assertEqual(out.report["controller"]["spent_work"][0]["work"], key)

    def test_spent_work_without_work_recovery_waits_for_a_human(self):
        waiting, key, queue = self.uncertain_publication()
        limit = lifecycle.budget_limit("work_total", POLICY)
        waiting = dict(waiting, budget=dict(waiting["budget"], attempts_used=limit - 1))
        out = tick(waiting, verdict="failed", evidence=self.evidence, queue=queue)
        self.check(out)
        self.assertEqual(kinds(out), ["record_ci_failure", "request_repair",
                                      "exhaust_work_recovery"])
        rec = final(out, waiting)
        self.assertEqual((engine.state_key(rec), rec["active_work"],
                          rec["pending_human_action"]["kind"],
                          rec["pending_human_action"]["counter"]),
                         ("development.waiting_human", None, "work_failed", "work_total"))
        self.assertEqual(submitted(out), [])

    def test_a_stale_revision_result_in_upstream_review_is_revised_on_a_fresh_graph(self):
        # A revision Result that left the revision unchanged (`no_change` or
        # stale) is followed by failing CI at the same revision: the revision
        # Work names the graph that already returned that Result.
        import work
        rec = record("fix_needed", phase="upstream_review", revision=3,
                     last_ci_evidence_id=observed_ci_id())
        rec["upstream_pr"] = {"id": 9, "number": 9, "url": "https://github.com/example/proj/pull/9",
                              "head_repository_id": 2, "head_ref": "riscv64", "head_sha": SHA_A,
                              "base_ref": "main", "registered_by": OWNER}
        key = work.work_key(rec["contribution_id"], 0, 3, "upstream_revision")
        out = tick(rec, observe=False, evidence=self.evidence,
                   queue={work.graph_id(key): "completed"})
        self.check(out)
        self.assertEqual(kinds(out), ["request_upstream_revision", "allocate_work_retry"])
        new = final(out, rec)
        self.assertEqual((engine.state_key(new), new["paused"], new["revision"]),
                         ("upstream_review.agent_running", False, 4))
        self.assertEqual(submitted(out), ["example--proj/12@g0.r4:upstream_revision"])


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
    """Both refresh verbs keep the pending feedback; the planning that
    follows carries none, and only patch and revision Work carry it."""

    def assert_planning_follows(self, refreshed):
        self.assertEqual(engine.state_key(refreshed), "queued.idle")
        self.assertEqual(refreshed["pending_feedback_evidence_ids"], ["review-1"])
        out = tick(refreshed, observe=False, now=T + timedelta(minutes=10))
        self.assertEqual(kinds(out), ["begin_planning", "dispatch_planning"])
        after = final(out, refreshed)
        self.assertEqual(after["active_work"]["task"], "planning")
        self.assertEqual(after["active_work"].get("feedback_evidence_ids") or [], [])
        self.assertEqual(after["pending_feedback_evidence_ids"], ["review-1"])
        self.assertNotIn("processing_errors", out.report)

    def test_refresh_on_exhaustion_then_planning(self):
        limit = lifecycle.budget_limit("no_progress", POLICY)
        due = record("fix_needed", pending_feedback_evidence_ids=["review-1"],
                     budget=dict(contribution()["budget"], no_progress_streak=limit))
        out = tick(due, observe=False)
        self.assertEqual(kinds(out), ["refresh_on_exhaustion"])
        self.assert_planning_follows(final(out, due))

    def test_refresh_planning_then_planning(self):
        waiting = record(pending_feedback_evidence_ids=["review-1"])
        out = apply_command(waiting, commands.parse(
            f'/upstreaming refresh revision={waiting["revision"]} reason="again"'))
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.event["kind"], "refresh_planning")
        self.assert_planning_follows(out.record)

    def test_patch_work_carries_the_pending_feedback(self):
        due = record("fix_needed", pending_feedback_evidence_ids=["review-1"])
        out = tick(due, observe=False)
        self.assertEqual(kinds(out), ["request_patch"])
        self.assertEqual(final(out, due)["active_work"]["feedback_evidence_ids"], ["review-1"])


class TestNoLegacyDevelopmentWrites(unittest.TestCase):
    def test_development_has_no_legacy_state_or_write_site(self):
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
    def test_a_development_pr_mismatch_is_quoted(self):
        import status_ledger
        rec = record()
        out = tick(rec, pull="development PR 1 does not exist")
        event = next(e for kind, e in out.staged if kind == "events")
        body = status_ledger.render(event, final(out, rec), "https://github.com/run")
        self.assertIn("development PR 1 does not exist", body)
        self.assertIn("/upstreaming resume revision=0", body)


if __name__ == "__main__":
    unittest.main()
