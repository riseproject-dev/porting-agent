# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""luhenry--d2vm/3 stalled: worker run 45397386038 failed at "Start MCP
Gateway", before its agent, and the controller neither relaunched nor woke.
The record is the one committed at 9a2e00e (`development.agent_running`,
a task `luhenry--d2vm/3@g1.r6:development_repair`, 4 attempts used); the queue
holds the task available for attempt 2 of 3 after the cancelled Claim of
that run."""

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import capacity
import engine
import lifecycle
import pipeline_failure
import queue_result
import reconcile
import tasks
from tests.helpers import policy, take

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "live-d2vm-gateway"
RECORD = json.loads((FIXTURE / "contribution.json").read_text())
GRAPH = tasks.graph_id(RECORD["active_work"]["dedupe_key"])
RETRY_AT = datetime(2026, 11, 24, 12, 54, 34, tzinfo=timezone.utc)
# The code at the failed run, and the code after a push.
V1, V2, V3 = "1" * 64, "2" * 64, "3" * 64
ENDED = datetime(2026, 11, 24, 12, 54, 51, tzinfo=timezone.utc)
FAILED = {"run_id": "45397386038", "pipeline": True, "job": "agent",
          "step": "Start MCP Gateway", "version": V1, "count": 1,
          "ended_at": "2026-11-24T12:54:51Z"}
# The free relaunches of one version: after 30 minutes, 2 hours and 8 hours.
FIRST, SECOND, THIRD = (ENDED + timedelta(minutes=30), ENDED + timedelta(hours=2),
                        ENDED + timedelta(hours=8))


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def projection(state="available"):
    item = {"work_id": "w", "graph_id": GRAPH, "pool": "default", "state": state,
            "attempts": 1, "retry_not_before": int(RETRY_AT.timestamp() * 1000)}
    return {"policy": {"pools": {"default": {"retry": {"max_attempts": 3}}}},
            "works": {"w": item},
            "claims": {"c1": {"work_id": "w", "dispatch_id": "d1"}},
            "dispatches": {"d1": {"run": {"run_id": "45397386038"}}}}


def dispatcher(run_id, created, conclusion="success", status="completed"):
    return {"id": run_id, "status": status, "conclusion": conclusion, "created_at": created,
            "updated_at": created}


# The dispatcher run that submitted the task, long before.
RUNS = [dispatcher(45396818686, "2026-11-24T12:46:30Z")]


def recover(now, *, state="available", failures=None, pipeline=V1, manual=False, runs=None,
            record=None):
    graph = tasks.graph_id((record or RECORD)["active_work"]["dedupe_key"])
    return reconcile.dispatch_recovery(
        {"c": copy.deepcopy(record or RECORD)}, runs=RUNS if runs is None else runs,
        queue_works={graph: state}, now=at(now), policy=policy(),
        queue_retries=queue_result.work_retries(projection()) if state == "available" else None,
        pipeline_failures=failures, pipeline=pipeline, manual=manual)


class TestTheStall(unittest.TestCase):
    def test_the_fixture_is_the_incident(self):
        self.assertEqual(RECORD["active_work"]["dedupe_key"],
                         "luhenry--d2vm/3@g1.r6:development_repair")
        self.assertEqual(RECORD["budget"]["attempts_used"], 4)
        self.assertEqual(queue_result.work_claim_runs(projection()), {GRAPH: ["45397386038"]})
        run = json.loads((FIXTURE / "worker-run-45397386038.json").read_text())
        found = pipeline_failure.classify(run["run"], run["jobs"])
        self.assertEqual((found["job"], found["step"]), ("agent", "Start MCP Gateway"))

    def test_a_retry_after_the_gateway_failure_is_held_at_the_same_version(self):
        relaunch, staged, report = recover("2026-11-24T13:01:00+00:00", failures={GRAPH: FAILED})
        self.assertEqual((relaunch, staged), ([], []))
        [entry] = report
        self.assertTrue(entry["decision"].startswith(
            "held: worker run 45397386038 failed before its agent (agent/Start MCP Gateway), "
            "a failure of the pipeline at version 111111111111"), entry["decision"])
        self.assertIn("alert", entry)
        # The alert names when its first free relaunch is due.
        self.assertIn(FIRST.isoformat(), entry["alert"])

    def test_another_contributions_dispatcher_run_does_not_charge_the_held_task(self):
        # A dispatcher run for other tasks, created after the retry time and
        # ended: it neither relaunches this task nor is a lost dispatch of it.
        other = dispatcher(45398000000, "2026-11-24T12:56:00Z")
        relaunch, staged, report = recover("2026-11-24T13:01:00+00:00",
                                           failures={GRAPH: FAILED}, runs=RUNS + [other])
        self.assertEqual((relaunch, staged), ([], []))
        self.assertTrue(report[0]["decision"].startswith("held: worker run 45397386038"))
        self.assertEqual(report[0]["dispatcher_runs"], [45398000000])
        # Without the failure that run is what it always was: a lost dispatch.
        _relaunch, staged, _report = recover("2026-11-24T13:01:00+00:00",
                                             failures={GRAPH: {"run_id": "5", "pipeline": False}},
                                             runs=RUNS + [other])
        self.assertEqual([e["kind"] for k, e in staged if k == "events"], ["retry_dispatch"])

    def test_the_same_failure_by_a_lost_runner_is_relaunched_at_once(self):
        relaunch, staged, report = recover("2026-11-24T13:01:00+00:00", failures={
            GRAPH: {"run_id": "45397386038", "pipeline": False}})
        self.assertEqual(staged, [])
        self.assertEqual(len(relaunch), 1)
        self.assertIn("the queue retries the task", report[0]["decision"])

    def test_a_read_that_failed_decides_nothing_and_looks_again_soon(self):
        now = at("2026-11-24T13:01:00+00:00")
        for state in ("available", "cancelled"):
            with self.subTest(state):
                relaunch, staged, report = recover(now.isoformat(), failures={}, state=state)
                self.assertEqual((relaunch, staged), ([], []))
                self.assertTrue(report[0]["decision"].startswith("waiting: the last Claim"))
                self.assertIn("alert", report[0])

    def test_the_backoff_is_waited_when_the_failure_is_not_a_failure_of_the_pipeline(self):
        relaunch, staged, report = recover(
            "2026-11-24T12:54:20+00:00", failures={GRAPH: {"run_id": "1", "pipeline": False}})
        self.assertEqual((relaunch, staged), ([], []))
        self.assertEqual(report[0]["decision"], "waiting for the queue's retry backoff")

    def test_a_changed_version_or_a_human_releases_the_hold(self):
        for label, kwargs in (("a push", {"pipeline": V2}), ("a human", {"manual": True})):
            with self.subTest(label):
                relaunch, staged, report = recover("2026-11-24T13:01:00+00:00",
                                                   failures={GRAPH: FAILED}, **kwargs)
                self.assertEqual((len(relaunch), staged), (1, []))
                self.assertIn("relaunch", report[0]["decision"])

    def test_a_dispatcher_that_ends_unclaimed_after_a_release_is_charged_not_looped(self):
        # After a code change the task is launched again; the dispatcher runs
        # that end without claiming it are decided and charged by the normal
        # lost-dispatch path, bounded by `dispatch_recovery`, never launched
        # again for ever without a charge.
        for pipeline, manual in ((V2, False), (V1, True)):
            with self.subTest(pipeline=pipeline, manual=manual):
                kwargs = {"failures": {GRAPH: FAILED}, "pipeline": pipeline, "manual": manual}
                relaunch, staged, _report = recover("2026-11-24T13:01:00+00:00", **kwargs)
                self.assertEqual((len(relaunch), staged), (1, []))
                ended = [dispatcher(45400000000 + i, f"2026-11-24T13:0{2 + i}:00Z")
                         for i in range(3)]
                kinds = []
                for count, expected in ((1, "retry_dispatch"), (2, "retry_dispatch"),
                                        (3, "exhaust_recovery")):
                    relaunch, staged, report = recover(
                        "2026-11-24T14:00:00+00:00", runs=RUNS + ended[:count], **kwargs)
                    kinds.append([e["kind"] for k, e in staged if k == "events"])
                    self.assertEqual(kinds[-1], [expected], (count, report))
                    self.assertEqual(len(report[0]["dispatcher_runs"]), count)
                # Dispatcher runs that ended before the failed run did are
                # not the task's.
                older = dispatcher(45390000000, "2026-11-24T12:50:00Z")
                relaunch, staged, _report = recover(
                    "2026-11-24T14:00:00+00:00", runs=RUNS + [older], **kwargs)
                self.assertEqual((len(relaunch), staged), (1, []))

    def test_the_free_relaunches_are_not_carried_to_any_other_fresh_graph(self):
        record = copy.deepcopy(RECORD)
        record["active_work"]["pipeline_free"] = {"version": V1, "count": 2}
        failures = {GRAPH: {"run_id": "5", "pipeline": False}}
        _relaunch, staged, _report = recover("2026-11-24T14:41:00+00:00", state="cancelled",
                                             failures=failures, record=record)
        [new] = [r for kind, r in staged if kind == "contributions"]
        self.assertEqual([e["kind"] for k, e in staged if k == "events"],
                         ["allocate_work_retry"])
        self.assertNotIn("pipeline_free", new["active_work"])
        # Nor is the count of a changed version or of a human's release.
        for kwargs in ({"pipeline": V2}, {"manual": True}):
            _relaunch, staged, _report = recover(
                "2026-11-24T14:41:00+00:00", state="cancelled",
                failures={GRAPH: dict(FAILED, ended_at="2026-11-24T14:00:00Z")},
                record=record, **kwargs)
            [new] = [r for kind, r in staged if kind == "contributions"]
            self.assertNotIn("pipeline_free", new["active_work"])

    def test_the_version_of_the_code_unknown_decides_nothing(self):
        now = at("2026-11-24T13:01:00+00:00")
        for state in ("available", "cancelled"):
            for failure in (FAILED, dict(FAILED, version=None)):
                with self.subTest(state=state, version=failure["version"]):
                    relaunch, staged, report = recover(
                        now.isoformat(), failures={GRAPH: failure}, state=state,
                        pipeline=V1 if failure["version"] is None else None)
                    self.assertEqual((relaunch, staged), ([], []))
                    self.assertTrue(report[0]["decision"].startswith("waiting:"))
                    self.assertIn("not known", report[0]["decision"])
                    self.assertIn("alert", report[0])

    def test_a_transient_failure_is_relaunched_free_after_each_backoff_then_held(self):
        free = [(FIRST, 1), (SECOND, 2), (THIRD, 3)]
        for due, used in free:
            failures = {GRAPH: dict(FAILED, count=used)}
            with self.subTest(used=used):
                before = recover((due - timedelta(seconds=1)).isoformat(), failures=failures)
                self.assertEqual((before[0], before[1]), ([], []))
                self.assertIn(due.isoformat(), before[2][0]["alert"])
                self.assertIn(f"free relaunch {used} of 3", before[2][0]["decision"])
                relaunch, staged, report = recover(due.isoformat(), failures=failures)
                self.assertEqual((len(relaunch), staged), (1, []))
                self.assertEqual(report[0]["pipeline_release"], "timed")
        # After the third the task stays held, with its alert, until the code
        # changes or a human launches the controller.
        spent = {GRAPH: dict(FAILED, count=4)}
        relaunch, staged, report = recover("2026-12-30T00:00:00+00:00", failures=spent)
        self.assertEqual((relaunch, staged), ([], []))
        self.assertIn("spent", report[0]["decision"])
        self.assertIn("alert", report[0])
        relaunch, _staged, _report = recover("2026-12-30T00:00:00+00:00", failures=spent,
                                             pipeline=V2)
        self.assertEqual(len(relaunch), 1)

    def test_a_dispatcher_run_in_flight_since_the_release_is_not_charged_for_older_runs(self):
        # The free relaunch counts only the dispatcher runs created since it
        # was due: the run that ended before it charges nothing.
        other = dispatcher(45398000000, "2026-11-24T12:56:00Z")
        relaunch, staged, _report = recover(FIRST.isoformat(), failures={GRAPH: FAILED},
                                            runs=RUNS + [other])
        self.assertEqual((len(relaunch), staged), (1, []))

    def test_a_cancelled_graph_is_held_then_reallocated_without_a_charge(self):
        failures = {GRAPH: dict(FAILED, run_id="45413173475", ended_at="2026-11-24T14:26:33Z")}
        now = "2026-11-24T14:41:00+00:00"
        relaunch, staged, report = recover(now, state="cancelled", failures=failures)
        # Held: no row, so the record keeps the cancelled graph.
        self.assertEqual((relaunch, staged), ([], []))
        self.assertTrue(report[0]["decision"].startswith("held: worker run 45413173475"))
        # A fix pushed after the failure: a fresh graph, nothing charged.
        relaunch, staged, report = recover(now, state="cancelled", failures=failures,
                                           pipeline=V2)
        [event] = [e for kind, e in staged if kind == "events"]
        [new] = [r for kind, r in staged if kind == "contributions"]
        self.assertEqual(event["kind"], "reallocate_after_pipeline_failure")
        self.assertEqual(event["details"]["pipeline_failure"]["step"], "Start MCP Gateway")
        self.assertEqual(event["details"]["pipeline_failure"]["release"], "changed")
        self.assertEqual(len(relaunch), 1)
        self.assertEqual(new["revision"], RECORD["revision"] + 1)
        self.assertEqual(new["active_work"]["dedupe_key"],
                         "luhenry--d2vm/3@g1.r7:development_repair")
        # The budgets are those of the record, and so are the attempts used.
        self.assertEqual(new["budget"], RECORD["budget"])
        self.assertEqual(new["active_work"]["attempt"], RECORD["active_work"]["attempt"])
        self.assertNotIn("pipeline_free", new["active_work"])
        self.assertEqual(engine.state_key(new), "development.agent_running")
        # The same release by a human, at the same version.
        _relaunch, staged, _report = recover(now, state="cancelled", failures=failures,
                                             manual=True)
        self.assertEqual([e["kind"] for k, e in staged if k == "events"],
                         ["reallocate_after_pipeline_failure"])

    def test_a_cancelled_graph_is_reallocated_free_after_the_backoff_and_counts_it(self):
        failures = {GRAPH: dict(FAILED, run_id="45413173475", ended_at="2026-11-24T14:00:00Z")}
        due = at("2026-11-24T14:30:00+00:00")
        _relaunch, staged, _report = recover((due - timedelta(seconds=1)).isoformat(),
                                             state="cancelled", failures=failures)
        self.assertEqual(staged, [])
        relaunch, staged, _report = recover(due.isoformat(), state="cancelled",
                                            failures=failures)
        [new] = [r for kind, r in staged if kind == "contributions"]
        [event] = [e for kind, e in staged if kind == "events"]
        self.assertEqual((event["kind"], len(relaunch)),
                         ("reallocate_after_pipeline_failure", 1))
        self.assertEqual(event["details"]["pipeline_failure"]["release"], "timed")
        self.assertEqual(new["active_work"]["pipeline_free"], {"version": V1, "count": 1})
        self.assertEqual(new["budget"], RECORD["budget"])
        # The fresh graph fails the same way: the count of the task carries
        # over, so the next backoff is the second (2 hours).
        again = dict(RECORD, **{k: new[k] for k in ("revision", "active_work", "budget")})
        graph = tasks.graph_id(new["active_work"]["dedupe_key"])
        later = {graph: dict(FAILED, ended_at="2026-11-24T15:00:00Z")}
        _r, staged, report = recover("2026-11-24T16:59:00+00:00", state="cancelled",
                                     failures=later, record=again)
        self.assertEqual(staged, [])
        self.assertIn("free relaunch 2 of 3", report[0]["decision"])
        # The count belongs to one version of the code.
        _r, staged, _report = recover("2026-11-24T15:01:00+00:00", state="cancelled",
                                      failures=later, record=again, pipeline=V2)
        [new] = [r for kind, r in staged if kind == "contributions"]
        self.assertNotIn("pipeline_free", new["active_work"])
        # Three free relaunches of this version are spent.
        spent = copy.deepcopy(again)
        spent["active_work"]["pipeline_free"] = {"version": V1, "count": 3}
        _r, staged, report = recover("2027-01-30T00:00:00+00:00", state="cancelled",
                                     failures=later, record=spent)
        self.assertEqual(staged, [])
        self.assertIn("spent", report[0]["decision"])

    def test_a_second_failure_at_a_new_version_holds_again_and_a_third_releases(self):
        now = "2026-11-24T15:00:00+00:00"
        again = {GRAPH: dict(FAILED, run_id="45420000000", version=V2,
                             ended_at="2026-11-24T14:59:00Z")}
        _relaunch, staged, report = recover(now, state="cancelled", failures=again, pipeline=V2)
        self.assertEqual(staged, [])
        self.assertTrue(report[0]["decision"].startswith("held:"))
        _relaunch, staged, _report = recover(now, state="cancelled", failures=again, pipeline=V3)
        self.assertEqual([e["kind"] for k, e in staged if k == "events"],
                         ["reallocate_after_pipeline_failure"])

    def test_a_cancelled_graph_without_a_pipeline_failure_is_charged_as_before(self):
        for failures in ({GRAPH: {"run_id": "5", "pipeline": False}}, None):
            _relaunch, staged, _report = recover("2026-11-24T14:41:00+00:00", state="cancelled",
                                                 failures=failures)
            self.assertEqual([e["kind"] for k, e in staged if k == "events"],
                             ["allocate_work_retry"])

    def test_a_dispatcher_run_that_failed_before_its_agent_is_not_charged(self):
        failed = dispatcher(45420000001, "2026-11-24T13:10:00Z", conclusion="failure")
        reading = {"run_id": "45420000001", "pipeline": True, "job": "agent",
                   "step": "Start MCP Gateway", "version": V1, "count": 1,
                   "ended_at": "2026-11-24T13:12:00Z"}
        # The task's own last Claim did not fail for the pipeline.
        failures = {GRAPH: {"run_id": "5", "pipeline": False}, "45420000001": reading}
        now = "2026-11-24T13:20:00+00:00"
        for state in ("available", None):
            with self.subTest(state=state):
                # The same code: held, with no `retry_dispatch`.
                relaunch, staged, report = recover(now, state=state, failures=failures,
                                                   runs=RUNS + [failed])
                self.assertEqual((relaunch, staged), ([], []))
                self.assertTrue(report[0]["decision"].startswith("held: dispatcher run 45420000001"))
                # The code changed, or its backoff passed: launched again, free.
                for kwargs, when in (({"pipeline": V2}, now),
                                     ({}, "2026-11-24T13:43:00+00:00")):
                    relaunch, staged, _report = recover(when, state=state, failures=failures,
                                                        runs=RUNS + [failed], **kwargs)
                    self.assertEqual((len(relaunch), staged), (1, []))
                # A read that failed decides nothing.
                relaunch, staged, report = recover(now, state=state, runs=RUNS + [failed],
                                                   failures={GRAPH: failures[GRAPH]})
                self.assertEqual((relaunch, staged), ([], []))
                self.assertIn("alert", report[0])
        # Without the readings the run is a lost dispatch, charged as ever.
        _relaunch, staged, _report = recover(now, failures=None, runs=RUNS + [failed])
        self.assertEqual([e["kind"] for k, e in staged if k == "events"], ["retry_dispatch"])


class TestTheTick(unittest.TestCase):
    def tick(self, failures, pipeline, manual=False, now="2026-11-24T13:01:00+00:00"):
        snapshot = reconcile.Snapshot({"c": copy.deepcopy(RECORD)}, {}, {}, {}, {})
        return reconcile.tick(
            None, snapshot, policy=policy(), now=at(now), publications=[],
            pressure=capacity.Pressure(ok=True), observe=False, dispatcher_runs=list(RUNS),
            queue_works={GRAPH: "available"},
            queue_retries=queue_result.work_retries(projection()),
            pipeline_failures=failures, pipeline=pipeline, manual=manual)

    def test_a_hold_is_alerted_with_the_time_of_its_free_relaunch(self):
        held = self.tick({GRAPH: FAILED}, V1)
        self.assertEqual((held.staged, held.report["dispatch_relaunch"]), ([], []))
        self.assertTrue(any("failed before its agent (agent/Start MCP Gateway)" in a["alert"]
                            for a in held.report["controller"]["alerts"]))
        self.assertTrue(any(FIRST.isoformat() in a["alert"]
                            for a in held.report["controller"]["alerts"]))
        for label, out in (("push", self.tick({GRAPH: FAILED}, V2)),
                           ("human", self.tick({GRAPH: FAILED}, V1, manual=True)),
                           ("backoff", self.tick({GRAPH: FAILED}, V1,
                                                 now="2026-11-24T13:30:00+00:00"))):
            with self.subTest(label):
                self.assertEqual(len(out.report["dispatch_relaunch"]), 1)
                self.assertFalse(any("Start MCP Gateway" in a.get("alert", "")
                                     for a in out.report["controller"]["alerts"]))

    def test_the_manual_flag_is_an_argument_of_the_reconciler(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as root, \
                contextlib.redirect_stdout(io.StringIO()):
            (Path(root) / ".state").mkdir()
            self.assertEqual(reconcile.main(["--root", root, "--offline", "--manual"]), 0)


class TestTheRow(unittest.TestCase):
    FACTS = {"task_terminal_without_result", "pipeline_failure", "pipeline_failure_cleared"}

    def fresh(self):
        aw = dict(RECORD["active_work"], dedupe_key="luhenry--d2vm/3@g1.r7:development_repair",
                  revision=7)
        return {"active_work": aw}

    def test_the_row_charges_nothing_and_gives_a_new_dedupe_key(self):
        out = take(copy.deepcopy(RECORD), "reallocate_after_pipeline_failure", policy=policy(),
                   now=datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc).isoformat(),
                   cause_id="queue-cancelled", facts=self.FACTS, values=self.fresh())
        self.assertTrue(out.accepted, out.reason)
        self.assertNotIn("charged", out.event["details"])
        self.assertEqual(out.record["revision"], RECORD["revision"] + 1)
        self.assertEqual(out.record["budget"], RECORD["budget"])
        self.assertEqual(out.record["active_work"]["dedupe_key"],
                         "luhenry--d2vm/3@g1.r7:development_repair")
        self.assertEqual(out.record["active_work"]["feedback_evidence_ids"],
                         RECORD["active_work"]["feedback_evidence_ids"])

    def test_it_needs_the_failure_and_that_it_was_cleared(self):
        for missing in ("pipeline_failure", "pipeline_failure_cleared",
                        "task_terminal_without_result"):
            out = take(copy.deepcopy(RECORD), "reallocate_after_pipeline_failure",
                       policy=policy(), now=datetime(2026, 11, 24, 15, 0,
                                                     tzinfo=timezone.utc).isoformat(),
                       cause_id="x", facts=self.FACTS - {missing}, values=self.fresh())
            self.assertFalse(out.accepted, missing)

    def test_the_other_rows_of_a_terminal_graph_are_refused_for_a_pipeline_failure(self):
        now = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc).isoformat()
        spent = copy.deepcopy(RECORD)
        spent["active_work"]["attempt"] = lifecycle.budget_limit("work_recovery", policy())
        for verb, record in (("allocate_work_retry", RECORD), ("exhaust_recovery", spent)):
            facts = {"task_terminal_without_result"}
            values = self.fresh() if verb == "allocate_work_retry" else {}
            out = take(copy.deepcopy(record), verb, policy=policy(), now=now, cause_id="x",
                       facts=facts | {"pipeline_failure"}, values=values)
            self.assertFalse(out.accepted, verb)
            self.assertIn("failed before its agent for a failure of the pipeline",
                          out.reason or "", verb)
            # Without the failure each is the row it always was.
            out = take(copy.deepcopy(record), verb, policy=policy(), now=now, cause_id="x",
                       facts=facts, values=values)
            self.assertTrue(out.accepted, (verb, out.reason))
        charged = take(copy.deepcopy(RECORD), "allocate_work_retry", policy=policy(), now=now,
                       cause_id="x", facts={"task_terminal_without_result"}, values=self.fresh())
        self.assertEqual(charged.event["details"]["charged"], ["work_recovery", "work_total"])

    def test_the_pair_is_exclusive_in_the_table(self):
        self.assertIn(((lifecycle.NOT_PIPELINE_FAILURE,), (lifecycle.PIPELINE_FAILURE,)),
                      lifecycle.EXCLUSIVE_GUARDS)
        verbs = {e.verb for e in lifecycle.table_edges()}
        self.assertIn("reallocate_after_pipeline_failure", verbs)
        self.assertEqual(engine.PIPELINE_VERBS["reallocate_after_pipeline_failure"], "controller")


if __name__ == "__main__":
    unittest.main()
