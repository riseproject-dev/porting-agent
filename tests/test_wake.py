# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The wake of the controller: every wait a tick decides names what ends it."""

from __future__ import annotations

import contextlib
import copy
import io
import unittest
from datetime import datetime, timedelta, timezone

import engine
import lifecycle
import patch_review
import reconcile
import wake
import work
from tests import test_queue_retry as queue_retry
from tests.helpers import contribution
from tests.test_result_collection import STATES, graph, held, reading, recover
from tests.test_work_identity import POLICY, T

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


class TestWake(unittest.TestCase):
    def test_a_timed_wait_is_noted_and_the_earliest_is_reported(self):
        report = {}
        for minutes in (30, 5, 90):
            self.assertTrue(wake.note(report, NOW + timedelta(minutes=minutes), "x", now=NOW))
        wake.note_on(report, "dispatcher run completion", "y")
        wake.finish(report, NOW)
        self.assertEqual(report["wake_at"], "2026-11-24T12:05:00Z")
        self.assertEqual(len(report["waits"]), 4)

    def test_the_wake_of_the_controller_report_counts(self):
        report = {"controller": {"alerts": []}}
        wake.note(report["controller"], NOW + timedelta(minutes=7), "reminder", now=NOW)
        wake.finish(report, NOW)
        self.assertEqual(report["wake_at"], "2026-11-24T12:07:00Z")

    def test_a_fractional_second_is_rounded_up(self):
        self.assertEqual(wake.stamp(NOW + timedelta(seconds=1, microseconds=1)),
                         "2026-11-24T12:00:02Z")

    def test_a_time_that_is_not_later_than_now_is_an_alert_not_a_wake(self):
        report = {"controller": {"alerts": []}}
        for at in (NOW, NOW - timedelta(minutes=3), "garbage"):
            self.assertFalse(wake.note(report, at, "already due", now=NOW, contribution="c"))
        wake.finish(report, NOW)
        self.assertIsNone(report["wake_at"])
        self.assertEqual(len(report["controller"]["alerts"]), 3)
        self.assertIn("already due", report["controller"]["alerts"][0]["alert"])
        self.assertEqual(report["controller"]["alerts"][0]["contribution"], "c")

    def test_no_wait_means_no_wake(self):
        report = {}
        wake.note_on(report, "push", "a human")
        wake.finish(report, NOW)
        self.assertIsNone(report["wake_at"])

    def test_sleep_seconds_is_clamped(self):
        for at, expected in (
                (NOW + timedelta(seconds=5), wake.MIN_SLEEP),
                (NOW + timedelta(minutes=10), 600),
                (NOW + timedelta(seconds=600, microseconds=1), 601),
                (NOW + timedelta(days=3), wake.MAX_SLEEP),
                (NOW - timedelta(hours=1), wake.MIN_SLEEP)):
            self.assertEqual(wake.seconds(wake.stamp(at) if at.microsecond == 0 else at, NOW),
                             expected)
        self.assertLess(wake.MAX_SLEEP, 355 * 60)

    def test_the_cli_prints_the_sleep_and_refuses_what_is_no_time(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wake.main(["sleep-seconds", "--at", "2026-11-24T12:10:00Z",
                                        "--now", NOW.isoformat()]), 0)
        self.assertEqual(out.getvalue().strip(), "600")
        with contextlib.redirect_stderr(io.StringIO()):
            for bad in ("tomorrow", "2026-11-24T12:10:00+00:00", "2026-11-24 12:10:00Z"):
                self.assertEqual(wake.main(["sleep-seconds", "--at", bad]), 2)

    def test_a_report_of_a_migration_has_the_key(self):
        self.assertIn("wake_at", reconcile.migration_report("x", []))
        self.assertIsNone(reconcile.state_text_migration_report([])["wake_at"])


class TestDispatchRecoveryWakes(unittest.TestCase):
    """Every dispatch recovery decision that waits says what ends the wait."""

    def check(self, report, *, at=None, on=None):
        [entry] = report
        self.assertTrue(entry.get("wake_at") or entry.get("wake_on"), entry)
        if at is not None:
            self.assertEqual(wake.parse(entry["wake_at"]), at)
        if on is not None:
            self.assertEqual(entry["wake_on"], on)
        return entry

    def test_the_queue_retry_backoff_wakes_at_the_retry_time(self):
        case = queue_retry.TestDispatchRecoveryOfAQueueRetry("test_nothing_is_launched_before_the_backoff")
        case.setUp()
        _relaunch, _staged, report = case.recover(
            (queue_retry.RETRY_AT - timedelta(seconds=10)).isoformat())
        self.assertEqual(report[0]["decision"], "waiting for the queue's retry backoff")
        self.check(report, at=queue_retry.RETRY_AT)

    def test_a_dispatcher_in_flight_wakes_on_its_completion(self):
        case = queue_retry.TestDispatchRecoveryOfAQueueRetry("test_the_launched_dispatcher_is_awaited")
        case.setUp()
        runs = case.runs + [queue_retry.run(45278935001, "2026-11-23T22:21:46Z",
                                            status="in_progress")]
        _relaunch, _staged, report = case.recover("2026-11-23T22:22:30+00:00", runs=runs)
        self.check(report, on="dispatcher run completion")

    def test_the_wait_for_the_history_wakes_when_it_ends(self):
        from tests import test_development_table as dev
        rec = dev.running(active_work=dict(dev.WORK, requested_at=dev.T.isoformat()))
        out = dev.tick(rec, observe=False, evidence={}, now=dev.T + timedelta(minutes=5))
        [entry] = [e for e in out.report["dispatch_recovery"]
                   if e["decision"] == "waiting for the history of the Work"]
        self.assertEqual(wake.parse(entry["wake_at"]), dev.T + reconcile.HISTORY_WAIT)
        self.assertEqual(out.report["wake_at"], wake.stamp(dev.T + reconcile.HISTORY_WAIT))

    def test_every_wait_over_a_completed_work_graph_names_its_wake(self):
        record = held(*STATES[1])
        ended = T - timedelta(minutes=reconcile.RESULT_COLLECTION_GRACE_MINUTES - 1)
        for label, results in (
                ("not read", None),
                ("run in flight", {graph(record): reading("in_progress", ended=None)}),
                ("uncollected", {graph(record): reading(publication=True, ended=ended)})):
            with self.subTest(label):
                _, staged, report = recover(record, results=results)
                self.assertEqual(staged, [])
                self.assertTrue(report[0]["decision"].startswith("waiting"), report)
                entry = self.check(report)
                if label == "not read":
                    self.assertEqual(wake.parse(entry["wake_at"]), T + reconcile.READ_RETRY)
                elif label == "run in flight":
                    self.assertEqual(entry["wake_on"], "worker run completion")
                else:
                    self.assertEqual(
                        wake.parse(entry["wake_at"]),
                        ended + timedelta(minutes=reconcile.RESULT_COLLECTION_GRACE_MINUTES))

    def test_the_queue_drain_wakes_on_the_dispatcher(self):
        case = queue_retry.TestDispatchRecoveryOfAQueueRetry("test_the_launched_dispatcher_is_awaited")
        case.setUp()
        graph_id = work.graph_id(queue_retry.DEDUPE)
        _relaunch, _staged, report = reconcile.dispatch_recovery(
            {"c": copy.deepcopy(case.record)}, runs=case.runs,
            queue_works={graph_id: "available"},
            now=queue_retry.at("2026-11-24T00:46:00+00:00"), policy=queue_retry.policy(),
            queue_policy={"preserved": False, "quiescent": False})
        self.assertTrue(report[0]["decision"].startswith("waiting for the queue to drain"))
        self.check(report, on="dispatcher run completion")

    def test_the_policy_reinstall_throttle_wakes_when_it_ends(self):
        policy = {"preserved": False, "quiescent": True, "dropped": ["x"]}
        record = contribution(phase="development", activity="agent_running",
                              active_work={"dedupe_key": "k"})
        created = NOW - timedelta(minutes=10)
        for status, expected in (("completed", ("wake_at", created + timedelta(
                minutes=reconcile.POLICY_REINSTALL_LAUNCH_MINUTES))),
                                 ("in_progress", ("wake_on", "dispatcher run completion"))):
            with self.subTest(status=status):
                _launch, _staged, report = reconcile.queue_policy_recovery(
                    {"c": record}, queue_policy=policy, now=NOW, policy={},
                    runs=[{"status": status, "created_at": created.isoformat()}])
                [entry] = report
                key, value = expected
                self.assertEqual(wake.parse(entry[key]) if key == "wake_at" else entry[key],
                                 value)


class TestTickWakes(unittest.TestCase):
    def test_the_earliest_wait_of_a_tick_is_reported(self):
        from tests import test_development_table as dev
        rec = dev.running(active_work=dict(dev.WORK, requested_at=dev.T.isoformat()))
        out = dev.tick(rec, observe=False, evidence={}, now=dev.T + timedelta(minutes=5))
        waits = out.report["waits"]
        self.assertTrue(any(w["why"].startswith("dispatch recovery") for w in waits))
        self.assertEqual(out.report["wake_at"], min(
            w["wake_at"] for w in waits if "wake_at" in w))

    def test_a_publication_retry_wakes_when_it_is_due_and_after_its_launch(self):
        from tests import test_development_table as dev
        rec = dev.running()
        out = dev.tick(rec, [dict(dev.publication(rec, "infra_error"), publication_artifact_id=1)],
                       observe=False)
        held = dev.final(out, rec)
        # The failed publication of this tick schedules its retry: woken
        # when it is due.
        waiting = dev.tick(held, observe=False, now=dev.T + timedelta(minutes=1))
        self.assertIn(wake.stamp(dev.T + timedelta(minutes=15)),
                      [w.get("wake_at") for w in waiting.report["waits"]])
        later = dev.T + timedelta(hours=1)
        out = dev.tick(held, observe=False, now=later)
        self.assertEqual(len(out.report["publication_retries"]), 1)
        # The launched retry is waited for, then it is a failed publication.
        self.assertEqual(out.report["wake_at"], wake.stamp(
            later + timedelta(minutes=reconcile.PUBLISH_RESULT_WAIT_MINUTES)))


class TestReviewCommentAndOperationWakes(unittest.TestCase):
    def tick(self, record, minutes=0, **over):
        import capacity
        from tests import test_development_review as review
        snap = reconcile.Snapshot(contributions={"c": record}, operations={}, consumed={},
                                  projects={}, evidence={})
        return reconcile.tick(None, snap, policy=dict(review.POLICY, **over),
                              now=datetime.fromisoformat(review.NOW) + timedelta(minutes=minutes),
                              publications=[], pressure=capacity.Pressure(ok=True), observe=False,
                              review_comment_receipts=[])

    def test_a_review_comment_wakes_at_its_receipt_wait_and_at_its_next_attempt(self):
        from tests import test_development_review as review
        now = datetime.fromisoformat(review.NOW)
        due = {"evidence_id": "patch-review-x", "head": review.CANDIDATE, "repository": None,
               "pr_number": None, "attempts": 0, "next_attempt_at": review.NOW,
               "launched_at": None, "last_reason": "", "exhausted": False}
        rec = contribution(activity="waiting_ci", review_comments=[due])
        out = self.tick(rec)
        self.assertEqual(out.report["wake_at"], wake.stamp(
            now + timedelta(minutes=patch_review.COMMENT_RECEIPT_WAIT_MINUTES)))
        launched = review.final(out)
        # A comment due later is woken at its time; a read-only policy and a
        # held comment launch nothing, so wake nothing.
        later = dict(launched, review_comments=[dict(
            launched["review_comments"][0], launched_at=None,
            next_attempt_at=(now + timedelta(minutes=45)).isoformat())])
        self.assertEqual(self.tick(later).report["wake_at"], wake.stamp(now + timedelta(minutes=45)))
        self.assertIsNone(self.tick(later, read_only=True).report["wake_at"])
        self.assertIsNone(self.tick(dict(later, paused=True)).report["wake_at"])

    def test_a_launched_operation_wakes_at_its_relaunch_time(self):
        import controller
        import recovery
        rep = {}
        base = {"status": "planned", "kind": "dev_pr_create", "contribution_id": "c",
                "updated_at": NOW.isoformat()}
        operations = {
            "launched": dict(base, external_ids={recovery.LAUNCH_COUNTER: 1}),
            "never launched": dict(base, external_ids={}),
            "due": dict(base, updated_at=(NOW - timedelta(minutes=6)).isoformat(),
                        external_ids={recovery.LAUNCH_COUNTER: 1}),
            "closed": dict(base, status="failed", external_ids={recovery.LAUNCH_COUNTER: 1})}
        controller._note_launch_waits(operations, NOW, rep)
        self.assertEqual([w["wake_at"] for w in rep["waits"]],
                         [wake.stamp(NOW + timedelta(seconds=controller.LAUNCH_INTERVAL_SECONDS))])
        self.assertNotIn("overdue_waits", rep)


class TestUnreadInputsKeepTheEarliestWake(unittest.TestCase):
    def tick(self, **inputs):
        from tests import test_development_table as dev
        rec = dev.running()
        snap = reconcile.Snapshot({"c": rec}, {}, {}, {}, {})
        import capacity
        return reconcile.tick(None, snap, policy=dev.POLICY, now=dev.T, publications=[],
                              pressure=capacity.Pressure(ok=True), observe=False, **inputs), dev

    def test_a_tick_that_read_no_dispatcher_runs_or_queue_wakes_soon(self):
        for inputs in ({"dispatcher_runs": None, "queue_works": {}},
                       {"dispatcher_runs": [], "queue_works": None}, {}):
            with self.subTest(inputs=inputs):
                out, dev = self.tick(**inputs)
                self.assertEqual(out.report["wake_at"],
                                 wake.stamp(dev.T + reconcile.READ_RETRY))

    def test_a_tick_that_read_everything_adds_no_such_wake(self):
        out, _dev = self.tick(dispatcher_runs=[], queue_works={})
        self.assertNotIn("dispatch recovery read nothing this tick",
                         [w["why"] for w in out.report.get("waits", [])])

    def test_a_skipped_contribution_wakes_soon(self):
        from tests import test_development_table as dev
        rec = dev.running()
        snap = reconcile.Snapshot({"c": rec}, {}, {}, {}, {})
        import capacity
        out = reconcile._tick(None, snap, policy=dev.POLICY, now=dev.T, publications=[],
                              pressure=capacity.Pressure(ok=True), observe=False,
                              dispatcher_runs=[], queue_works={},
                              skip=frozenset({rec["contribution_id"]}))
        self.assertEqual(out.report["wake_at"], wake.stamp(dev.T + reconcile.READ_RETRY))


class TestReminderWake(unittest.TestCase):
    def test_the_reminder_is_due_when_the_business_days_have_passed(self):
        policy = copy.deepcopy(POLICY)
        limit = int(lifecycle.policy_value(policy, lifecycle.REMINDER_KEY))
        # A Monday: the business days pass day by day.
        requested = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
        record = {"pending_human_action": {"requested_at": requested.isoformat()}}
        at = engine.reminder_due_at(record, policy)
        self.assertEqual(engine.business_days_between(requested, at), limit)
        self.assertLess(engine.business_days_between(requested, at - timedelta(seconds=1)), limit)
        self.assertIsNone(engine.reminder_due_at({}, policy))


if __name__ == "__main__":
    unittest.main()
