# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A worker run that ends without a Result leaves its Work to the queue's
retry, which dispatch recovery neither charges nor mistakes for a lost
dispatch (luhenry--d2vm/3, worker run 45257915183 stopped by its timeout)."""

import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import queue_result
import reconcile
import work
from tests.helpers import contribution, policy

DEDUPE = "luhenry--d2vm/3@g1.r3:development_patch"
REQUESTED = "2026-11-23T20:13:29.927419+00:00"
# The cancelled Claim of worker run 45257915183 allows the next attempt from
# 22:20:43.206 (its `retry_not_before`).
RETRY_MS = 1795472443206
RETRY_AT = datetime.fromtimestamp(RETRY_MS / 1000, tz=timezone.utc)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def run(run_id: int, created: str, status: str = "completed", conclusion: str = "success") -> dict:
    return {"id": run_id, "status": status, "conclusion": conclusion, "created_at": created,
            "updated_at": created}


def projection(state: str = "available", attempts: int = 1, retry=RETRY_MS) -> dict:
    graph = work.graph_id(DEDUPE)
    item = {"kind": "Work", "work_id": "b7ec5153", "graph_id": graph, "pool": "default",
            "state": state, "attempts": attempts}
    if retry is not None:
        item["retry_not_before"] = retry
    return {"policy": {"pools": {"default": {"retry": {"max_attempts": 3, "backoff_ms": 30000}}}},
            "works": {"b7ec5153": item}}


class TestWorkRetries(unittest.TestCase):
    def test_a_cancelled_claim_leaves_available_work_to_retry(self):
        retries = queue_result.work_retries(projection())
        self.assertEqual(retries, {work.graph_id(DEDUPE): {
            "attempts": 1, "max_attempts": 3, "retry_at": RETRY_AT}})

    def test_only_available_work_after_a_claim_is_a_retry(self):
        for state, attempts, retry in (("claimed", 2, RETRY_MS), ("available", 0, None),
                                       ("cancelled", 3, RETRY_MS), ("available", 1, None),
                                       ("available", True, RETRY_MS)):
            with self.subTest(state=state, attempts=attempts, retry=retry):
                self.assertEqual(queue_result.work_retries(projection(state, attempts, retry)), {})
        self.assertEqual(queue_result.work_retries(None), {})


class TestDispatchRecoveryOfAQueueRetry(unittest.TestCase):
    def setUp(self):
        self.record = contribution(phase="development", activity="agent_running", revision=3,
                                   contribution_id="luhenry--d2vm/3")
        self.record["generation"] = 1
        self.record["active_work"] = {
            "dedupe_key": DEDUPE, "task": "development_patch", "revision": 3, "generation": 1,
            "attempt": 1, "requested_at": REQUESTED,
            "deadline_at": "2026-11-24T02:13:29.927419+00:00",
            "dispatched_run_id": None, "reason_ids": ["plan-luhenry--d2vm--3-r2"],
            "feedback_evidence_ids": []}
        # The dispatcher run that submitted the Work and launched worker run
        # 45257915183, which ran until its 120 minute timeout.
        self.runs = [run(45257187396, "2026-11-23T20:13:50Z")]

    def recover(self, now, runs=None, retries=True, record=None):
        graph = work.graph_id(DEDUPE)
        p = projection()
        return reconcile.dispatch_recovery(
            {"c": copy.deepcopy(record or self.record)}, runs=runs or self.runs,
            queue_works={graph: "available"}, now=at(now), policy=policy(),
            queue_retries=queue_result.work_retries(p) if retries else None)

    def test_the_retry_is_dispatched_at_once_and_charges_nothing(self):
        relaunch, staged, report = self.recover("2026-11-23T22:21:19+00:00")
        self.assertEqual(staged, [])
        self.assertEqual(relaunch, [{"type": "relaunch_dispatcher",
                                     "contribution_id": "luhenry--d2vm/3"}])
        self.assertIn("the queue retries the Work", report[0]["decision"])
        self.assertEqual(report[0]["queue_retry"]["attempts"], 1)
        self.assertEqual(report[0]["dispatcher_runs"], [])

    def test_without_the_retry_the_live_tick_charged_a_lost_dispatch(self):
        # What controller run 45278754335 decided: `retry_dispatch`, attempt
        # 2 of 3, for a Work the queue was about to retry anyway.
        _relaunch, staged, report = self.recover("2026-11-23T22:21:19+00:00", retries=False)
        self.assertEqual([r["kind"] for kind, r in staged if kind == "events"], ["retry_dispatch"])
        self.assertIn("dispatch attempt 2 of 3", report[0]["decision"])

    def test_nothing_is_launched_before_the_backoff(self):
        relaunch, staged, report = self.recover(
            (RETRY_AT - timedelta(seconds=10)).isoformat())
        self.assertEqual((relaunch, staged), ([], []))
        self.assertEqual(report[0]["decision"], "waiting for the queue's retry backoff")

    def test_runs_before_the_retry_are_not_counted(self):
        # Two more dispatcher runs ended before the next worker run was
        # stopped: they never count against the retry.
        runs = self.runs + [run(45278935001, "2026-11-23T22:21:46Z", conclusion="failure"),
                            run(45287416687, "2026-11-23T23:16:55Z", conclusion="failure")]
        later = projection(attempts=2, retry=int(at("2026-11-24T00:30:00+00:00").timestamp() * 1000))
        relaunch, staged, report = reconcile.dispatch_recovery(
            {"c": copy.deepcopy(self.record)}, runs=runs,
            queue_works={work.graph_id(DEDUPE): "available"},
            now=at("2026-11-24T00:31:00+00:00"), policy=policy(),
            queue_retries=queue_result.work_retries(later))
        self.assertEqual(staged, [])
        self.assertEqual(len(relaunch), 1)
        self.assertEqual(report[0]["queue_retry"]["attempts"], 2)
        # Without the retry those three runs would exhaust the recovery.
        _relaunch, staged, _report = reconcile.dispatch_recovery(
            {"c": copy.deepcopy(self.record)}, runs=runs,
            queue_works={work.graph_id(DEDUPE): "available"},
            now=at("2026-11-24T00:31:00+00:00"), policy=policy())
        self.assertEqual([r["kind"] for kind, r in staged if kind == "events"],
                         ["exhaust_dispatch_recovery"])

    def test_the_launched_dispatcher_is_awaited(self):
        runs = self.runs + [run(45278935001, "2026-11-23T22:21:46Z", status="in_progress")]
        relaunch, staged, report = self.recover("2026-11-23T22:22:30+00:00", runs=runs)
        self.assertEqual((relaunch, staged), ([], []))
        self.assertEqual(report[0]["decision"], "dispatcher running")

    def test_a_retry_no_dispatcher_claims_is_charged_and_explained(self):
        runs = self.runs + [run(45278935001, "2026-11-23T22:21:46Z", conclusion="failure")]
        relaunch, staged, report = self.recover("2026-11-23T22:40:00+00:00", runs=runs)
        events = [r for kind, r in staged if kind == "events"]
        self.assertEqual([e["kind"] for e in events], ["retry_dispatch"])
        self.assertEqual(events[0]["details"]["queue_retry"], {"attempts": 1, "max_attempts": 3})
        self.assertIn("ended without a Result", events[0]["details"]["why"])
        self.assertIn("dispatch attempt 2 of 3", report[0]["decision"])
        self.assertEqual(len(relaunch), 1)

    def test_a_claimed_retry_is_left_to_the_worker(self):
        relaunch, staged, report = reconcile.dispatch_recovery(
            {"c": copy.deepcopy(self.record)}, runs=self.runs,
            queue_works={work.graph_id(DEDUPE): "claimed"},
            now=at("2026-11-23T23:30:00+00:00"), policy=policy(),
            queue_retries=queue_result.work_retries(projection("claimed", 2)))
        self.assertEqual((relaunch, staged, report), ([], [], []))


class TestDispatcherStaging(unittest.TestCase):
    def test_the_dispatcher_stages_through_the_queue_check(self):
        # gh-aw refuses a second payload for one Work id: the plan is staged
        # by the step that leaves queued Work out (tested against gh-aw
        # v1.2.2 in scripts/gh-aw-compat/tests/work_queue_stage.test.cjs).
        root = Path(__file__).parents[1]
        for name in ("dispatcher.md", "dispatcher.lock.yml"):
            with self.subTest(name=name):
                text = (root / ".github/workflows" / name).read_text()
                self.assertIn("scripts/work_queue_stage.cjs", text)
                self.assertNotIn("stagePlanIntents", text)


if __name__ == "__main__":
    unittest.main()
