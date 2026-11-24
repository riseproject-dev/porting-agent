# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A completed task graph is terminal without a usable Result only once its
Result is known to be missing: the worker run the queue bound to it ended
(or is gone) without a publication artifact, the queue binds no run to it,
or its artifact was not collected within the collection wait. While the run
has not ended, or was not read, nothing is decided and nothing is charged."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import capacity
import collect
import engine
import lifecycle as lc
import queue_result
import reconcile
import work
from tests.helpers import OWNER, SHA_A, contribution
from tests.test_work_identity import POLICY, T, requested, result

# One record in each state that holds a task, with its human wait.
STATES = (
    ("planning", "agent_running", "planning", {"fork": None}),
    ("development", "agent_running", "development_repair", {"plan_evidence_id": "plan-1"}),
    ("handoff", "agent_running", work.SUBMISSION_REWRITE, {}),
    ("upstream_review", "agent_running", "upstream_revision",
     {"upstream_pr": {"id": 9, "number": 9, "url": "https://github.com/example/proj/pull/9",
                      "head_repository_id": 2, "head_ref": "riscv64", "head_sha": SHA_A,
                      "base_ref": "main", "registered_by": OWNER}}),
)


def held(phase, activity, task, over):
    record = requested(contribution(phase=phase, generation=1, revision=5, **over), task)
    record["activity"] = activity
    return record


def graph(record):
    return work.graph_id(record["active_work"]["dedupe_key"])


def reading(status="completed", publication=False, ended=T - timedelta(minutes=5),
            run_id="4242"):
    return {"run_id": run_id, "status": status,
            "conclusion": "success" if status == "completed" else None,
            "ended_at": ended.isoformat() if ended else None,
            "publication": publication if status == "completed" else None}


def recover(record, state="completed", results=None, *, now=T, collected=frozenset()):
    relaunch, staged, report = reconcile.dispatch_recovery(
        {"c": copy.deepcopy(record)}, runs=[], queue_works={graph(record): state}, now=now,
        policy=POLICY, work_results=results, collected=collected)
    return relaunch, staged, report


def kinds(staged):
    return [e["kind"] for kind, e in staged if kind == "events"]


class TestWorkRuns(unittest.TestCase):
    """``queue_result.work_runs`` binds each completed graph to its run."""

    def projection(self, **work_over):
        item = {"work_id": "w1", "graph_id": "g1", "state": "completed", "claim_id": "c1"}
        item.update(work_over)
        return {"works": {"w1": item},
                "claims": {"c1": {"work_id": "w1", "dispatch_id": "d1"}},
                "dispatches": {"d1": {"run": {"run_id": 77, "run_attempt": 1,
                                              "workflow": ".github/workflows/worker.lock.yml"}}}}

    def test_the_dispatch_chain(self):
        self.assertEqual(queue_result.work_runs(self.projection()),
                         {"g1": {"run_id": "77", "run_attempt": 1,
                                 "workflow": ".github/workflows/worker.lock.yml"}})

    def test_the_result_names_the_run_without_a_dispatch(self):
        descriptor = {"outputs": [{"type": "upload_artifact",
                                   "resource": {"kind": "artifact", "target_run_id": 88}}]}
        for value in (descriptor, json.dumps(descriptor)):
            projection = self.projection(claim_id="gone", result=value)
            self.assertEqual(queue_result.work_runs(projection)["g1"]["run_id"], "88")

    def test_no_run(self):
        self.assertEqual(queue_result.work_runs(self.projection(claim_id=None)),
                         {"g1": {"run_id": None}})

    def test_only_completed_work(self):
        for state in ("available", "claimed", "cancelled"):
            self.assertEqual(queue_result.work_runs(self.projection(state=state)), {})

    def test_a_malformed_run_id_is_rejected(self):
        for bad in ("0", "-1", "12/../x", "1" * 21, True):
            projection = self.projection()
            projection["dispatches"]["d1"]["run"]["run_id"] = bad
            self.assertEqual(queue_result.work_runs(projection), {"g1": {"run_id": None}}, bad)

    def test_no_queue(self):
        self.assertEqual(queue_result.work_runs(None), {})


class FakeApi:
    def __init__(self, run=None, artifacts=(), error=None):
        self.run, self.artifacts, self.error = run, list(artifacts), error
        self.calls: list[str] = []

    def get(self, path):
        self.calls.append(path)
        if self.error:
            raise self.error
        return self.run

    def paginate(self, path, **_kwargs):
        self.calls.append(path)
        return list(self.artifacts)


class TestWorkerRunResult(unittest.TestCase):
    RUN = {"id": 7, "status": "completed", "conclusion": "success",
           "created_at": "2026-11-23T07:00:00Z", "updated_at": "2026-11-23T08:00:00Z"}

    def test_a_run_in_progress(self):
        api = FakeApi(dict(self.RUN, status="in_progress", conclusion=None))
        got = collect.worker_run_result(api, "7")
        self.assertEqual((got["status"], got["publication"]), ("in_progress", None))
        self.assertEqual(len(api.calls), 1)

    def test_an_ended_run_with_and_without_the_artifact(self):
        for artifacts, expected in (([{"name": "porting-publication", "expired": False}], True),
                                    ([{"name": "porting-publication", "expired": True}], False),
                                    ([{"name": "other", "expired": False}], False),
                                    ([], False)):
            got = collect.worker_run_result(FakeApi(self.RUN, artifacts), "7")
            self.assertEqual(got, {"run_id": "7", "status": "completed", "conclusion": "success",
                                   "created_at": "2026-11-23T07:00:00Z",
                                   "ended_at": "2026-11-23T08:00:00Z",
                                   "publication": expected})

    def test_a_run_that_is_gone(self):
        got = collect.worker_run_result(FakeApi(error=collect.CollectError("x", status=404)), "7")
        self.assertEqual((got["status"], got["publication"]), ("gone", False))

    def test_another_failure_raises(self):
        with self.assertRaises(collect.CollectError):
            collect.worker_run_result(FakeApi(error=collect.CollectError("x", status=500)), "7")
        with self.assertRaises(collect.CollectError):
            collect.worker_run_result(FakeApi(self.RUN), "7/../x")


class TestCompletedWorkWaitsForItsResult(unittest.TestCase):

    def test_a_run_that_has_not_ended_decides_nothing(self):
        for spec in STATES:
            with self.subTest(state=spec[:2]):
                record = held(*spec)
                for results, decision in (
                        ({graph(record): reading("in_progress")},
                         "waiting for worker run 4242 to publish its Result"),
                        ({}, "waiting: the worker run of the completed task was not read"),
                        (None, "waiting: the worker run of the completed task was not read")):
                    relaunch, staged, report = recover(record, results=results)
                    self.assertEqual((relaunch, staged), ([], []))
                    self.assertEqual(report[0]["decision"], decision)

    def test_a_run_that_ended_without_the_artifact_is_retried(self):
        for spec in STATES:
            with self.subTest(state=spec[:2]):
                record = held(*spec)
                relaunch, staged, _ = recover(record, results={graph(record): reading()})
                self.assertEqual(kinds(staged), ["allocate_work_retry"])
                [event] = [e for kind, e in staged if kind == "events"]
                self.assertEqual((event["details"]["worker_run"], event["details"]["result"],
                                  event["details"]["run_conclusion"]),
                                 ("4242", "missing", "success"))
                self.assertEqual(len(relaunch), 1)
                # With `work_recovery` spent, the phase's human wait.
                spent = copy.deepcopy(record)
                spent["active_work"]["attempt"] = lc.budget_limit("work_recovery", POLICY)
                _, staged, _ = recover(spent, results={graph(record): reading()})
                self.assertEqual(kinds(staged), ["exhaust_recovery"])
                new = [r for kind, r in staged if kind == "contributions"][-1]
                self.assertEqual(engine.state_key(new), f"{spec[0]}.waiting_human")

    def test_an_uncollected_artifact_is_waited_for(self):
        record = held(*STATES[1])
        ended = T - timedelta(minutes=reconcile.RESULT_COLLECTION_GRACE_MINUTES - 1)
        results = {graph(record): reading(publication=True, ended=ended)}
        _, staged, report = recover(record, results=results)
        self.assertEqual(staged, [])
        self.assertIn("to be collected", report[0]["decision"])
        _, staged, _ = recover(record, results=results, now=T + timedelta(minutes=1))
        self.assertEqual(kinds(staged), ["allocate_work_retry"])
        self.assertEqual([e for k, e in staged if k == "events"][0]["details"]["result"],
                         "uncollected")

    def test_an_artifact_that_will_never_be_collected_is_not_waited_for(self):
        record = held(*STATES[1])
        ended = T - timedelta(minutes=1)
        for never in ("rejected", "ambiguous", "outside the run inventory"):
            with self.subTest(never=never):
                results = {graph(record): dict(reading(publication=True, ended=ended),
                                               never_collected=never)}
                _, staged, _ = recover(record, results=results)
                self.assertEqual(kinds(staged), ["allocate_work_retry"])
                details = [e for k, e in staged if k == "events"][0]["details"]
                self.assertEqual((details["result"], details["never_collected"]),
                                 ("uncollected", never))

    def test_unbound_gone_and_collected_are_retried_at_once(self):
        record = held(*STATES[1])
        for results, collected, why in (
                ({graph(record): {"status": "unbound"}}, frozenset(), "unbound"),
                ({graph(record): reading("gone", ended=None)}, frozenset(), "missing"),
                (None, frozenset({graph(record)}), "collected")):
            _, staged, _ = recover(record, results=results, collected=collected)
            self.assertEqual(kinds(staged), ["allocate_work_retry"], why)
            self.assertEqual([e for k, e in staged if k == "events"][0]["details"]["result"], why)

    def test_a_cancelled_graph_is_retried_without_a_reading(self):
        for spec in STATES:
            record = held(*spec)
            _, staged, _ = recover(record, "cancelled", None)
            self.assertEqual(kinds(staged), ["allocate_work_retry"], spec)

    def test_a_paused_record_is_unchanged(self):
        record = dict(held(*STATES[1]), paused=True)
        _, staged, _ = recover(record, results={graph(record): reading()})
        self.assertEqual(staged, [])

    def test_the_result_of_a_slow_publish_job_is_folded(self):
        """The regression of the gap: a tick between the completion of the
        graph and the upload of its publication charges nothing, and the
        Result that arrives later is folded by its success row."""
        record = held(*STATES[1])
        queue = {graph(record): "completed"}

        def tick(publications, results):
            snapshot = reconcile.Snapshot({"c": copy.deepcopy(record)}, {}, {}, {}, {})
            with contextlib.redirect_stderr(io.StringIO()):
                return reconcile.tick(None, snapshot, policy=POLICY, now=T,
                                      publications=publications,
                                      pressure=capacity.Pressure(ok=False, reason="none"),
                                      observe=False, dispatcher_runs=[], queue_works=queue,
                                      work_results=results)

        out = tick([], {graph(record): reading("in_progress")})
        self.assertEqual(out.staged, [])
        out = tick([result(record, "published")], {graph(record): reading(publication=True)})
        found = [e["kind"] for kind, e in out.staged if kind == "events"]
        self.assertNotIn("allocate_work_retry", found)
        self.assertNotIn("record_late_effect", found)
        after = [r for kind, r in out.staged if kind == "contributions"][-1]
        self.assertEqual(engine.state_key(after), "development.waiting_ci")
        # `work_recovery` was never charged: the task was never reallocated.
        self.assertEqual(after["revision"], record["revision"])
        self.assertEqual(dict(after["budget"], reruns=None), dict(record["budget"], reruns=None))


class TestWorkResultReadings(unittest.TestCase):

    def test_only_held_completed_graphs_are_read_and_failures_are_isolated(self):
        one, two = held(*STATES[1]), held(*STATES[2])
        two["contribution_id"] = "example--other/3"
        two["active_work"]["dedupe_key"] = work.work_key("example--other/3", 1, 5,
                                                         work.SUBMISSION_REWRITE)
        idle = contribution(phase="handoff", activity="awaiting_submission", active_work=None)
        contributions = {"a": one, "b": two, "c": idle}
        queue = {graph(one): "completed", graph(two): "completed", "other": "completed"}
        runs = {graph(one): {"run_id": "11"}, graph(two): {"run_id": "22"}}
        calls = []

        def read(_api, run_id):
            calls.append(run_id)
            if run_id == "11":
                raise collect.CollectError("boom")
            return reading(run_id=run_id)

        with mock.patch("reconcile.collect.worker_run_result", side_effect=read), \
                contextlib.redirect_stderr(io.StringIO()):
            got = reconcile.work_result_readings(None, contributions, queue, runs)
        self.assertEqual(sorted(calls), ["11", "22"])
        self.assertEqual(list(got), [graph(two)])
        # A graph the queue does not report completed is not read; one the
        # queue binds no run to is unbound.
        with mock.patch("reconcile.collect.worker_run_result", side_effect=read):
            got = reconcile.work_result_readings(None, contributions,
                                                 {graph(one): "claimed", graph(two): "completed"},
                                                 {})
        self.assertEqual(got, {graph(two): {"status": "unbound"}})
        self.assertEqual(sorted(calls), ["11", "22"])

    def test_the_collector_marks_what_it_cannot_collect(self):
        record = held(*STATES[1])
        queue = {graph(record): "completed"}
        runs = {graph(record): {"run_id": "4242"}}
        listed = T
        before, after = (T - timedelta(minutes=5)).isoformat(), (T + timedelta(minutes=1)).isoformat()
        for uncollected, inventory, created, expected in (
                ({"4242": "rejected"}, frozenset({"4242"}), before, "rejected"),
                ({"4242": "ambiguous"}, frozenset({"4242"}), before, "ambiguous"),
                ({}, frozenset({"1"}), before, "outside the run inventory"),
                # A run created after the listing started is not out of reach.
                ({}, frozenset({"1"}), after, None),
                ({}, frozenset({"1"}), None, None),
                ({"4242": "download_failed"}, frozenset({"4242"}), before, None),
                ({}, frozenset({"4242"}), before, None),
                ({}, None, before, None)):
            with self.subTest(uncollected=uncollected, inventory=inventory, created=created):
                with mock.patch("reconcile.collect.worker_run_result",
                                return_value=dict(reading(publication=True), created_at=created)):
                    got = reconcile.work_result_readings(
                        None, {"a": record}, queue, runs, (uncollected, inventory, listed))
                # Without the time of the listing nothing is out of reach.
                if expected == "outside the run inventory":
                    with mock.patch("reconcile.collect.worker_run_result",
                                    return_value=dict(reading(publication=True),
                                                      created_at=created)):
                        unknown = reconcile.work_result_readings(
                            None, {"a": record}, queue, runs, (uncollected, inventory, None))
                    self.assertNotIn("never_collected", unknown[graph(record)])
                self.assertEqual(got[graph(record)].get("never_collected"), expected)

    def test_the_collection_files_are_read(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(reconcile.publication_collection(root), ({}, None, None))
            (Path(root) / ".uncollected").write_text(
                "11 rejected\n12 download_failed\n13 unknown\nx ambiguous\n14 ambiguous\n")
            (Path(root) / ".inventory").write_text("11\n\n12\n")
            (Path(root) / ".listed-at").write_text("2026-11-24T12:00:00Z\n")
            self.assertEqual(reconcile.publication_collection(root),
                             ({"11": "rejected", "12": "download_failed", "14": "ambiguous"},
                              frozenset({"11", "12"}),
                              datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)))
            (Path(root) / ".listed-at").write_text("not a time")
            self.assertIsNone(reconcile.publication_collection(root)[2])
        self.assertEqual(reconcile.publication_collection(None), ({}, None, None))


if __name__ == "__main__":
    unittest.main()
