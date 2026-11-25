# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Recover the exact Docker-blocked worker proposal into a bounded fresh task."""

import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import capacity
import engine
import reconcile
import tasks
import worker_recovery
from tests.helpers import NOW, contribution, policy

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/worker-docker-blocked"
RUN_ID = "44982902579"
ARTIFACT_ID = 14738278252


class TestDispatcherAcceptedLaunchRecovery(unittest.TestCase):
    def test_failed_dispatcher_run_does_not_duplicate_an_already_live_claim(self):
        record = contribution(phase="development", activity="agent_running", revision=3)
        record["active_work"] = {
            "dedupe_key": "example--proj/12@3:development_patch",
            "task": "development_patch",
            "revision": 3,
            "attempt": 1,
            "requested_at": NOW,
            "deadline_at": "2026-11-22T12:00:00+00:00",
            "dispatched_run_id": None,
            "reason_ids": ["plan-1"], "generation": 0, "feedback_evidence_ids": []
        }
        graph = tasks.graph_id(record["active_work"]["dedupe_key"])
        relaunch, staged, report = reconcile.dispatch_recovery(
            {"c": record},
            runs=[{
                "id": 44982720398,
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-11-22T04:00:00Z",
            }],
            queue_works={graph: "claimed"},
            now=datetime.fromisoformat("2026-11-22T04:05:00+00:00"),
            policy=policy(),
        )
        self.assertEqual((relaunch, staged, report), ([], [], []))


class TestLivePreAgentInfrastructureRecovery(unittest.TestCase):
    """Run 45005299790 failed before the agent and returned its task to available."""

    def setUp(self):
        self.record = contribution(phase="development", activity="agent_running", revision=4)
        self.record["active_work"] = {
            "dedupe_key": "closeio--ciso8601/1@4:development_repair",
            "task": "development_repair",
            "revision": 4,
            "attempt": 1,
            "requested_at": "2026-11-22T08:55:56.948866+00:00",
            "deadline_at": "2026-11-22T14:55:56.948866+00:00",
            # No dispatcher run is decided yet for this request.
            "dispatched_run_id": None,
            "reason_ids": [
                "rehearsal-closeio--ciso8601--1-r3-44982902579",
                "plan-closeio--ciso8601--1-r2",
            ], "generation": 0, "feedback_evidence_ids": []
        }
        self.graph = tasks.graph_id(self.record["active_work"]["dedupe_key"])
        self.runs = [
            {
                "id": 45005112315,
                "status": "completed",
                "conclusion": "success",
                "created_at": "2026-11-22T08:56:11Z",
            },
            {
                "id": 45005640838,
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-11-22T09:03:07Z",
            },
            {
                "id": 45006000000,
                "status": "completed",
                "conclusion": "failure",
                "created_at": "2026-11-22T09:08:00Z",
            },
        ]

    def recover(self, dispatcher_attempts):
        return reconcile.dispatch_recovery(
            {"c": copy.deepcopy(self.record)},
            runs=self.runs[:dispatcher_attempts],
            queue_works={self.graph: "available"},
            now=datetime.fromisoformat("2026-11-22T09:10:00+00:00"),
            policy=policy(),
        )

    def test_run_45005299790_launches_bounded_fresh_worker_attempts(self):
        for dispatcher_attempts, expected_attempt in ((1, 2), (2, 3)):
            with self.subTest(dispatcher_attempts=dispatcher_attempts):
                relaunch, staged, report = self.recover(dispatcher_attempts)
                self.assertEqual(
                    relaunch,
                    [{"type": "relaunch_dispatcher", "contribution_id": self.record["contribution_id"]}],
                )
                self.assertIn(f"dispatch attempt {expected_attempt} of 3", report[0]["decision"])
                staged_contributions = [item for kind, item in staged if kind == "contributions"]
                if staged_contributions:
                    self.assertEqual(
                        staged_contributions[-1]["active_work"]["dedupe_key"],
                        self.record["active_work"]["dedupe_key"],
                    )

        relaunch, staged, report = self.recover(3)
        self.assertEqual(relaunch, [])
        final = [item for kind, item in staged if kind == "contributions"][-1]
        self.assertEqual((final["activity"], final["active_work"]), ("waiting_human", None))
        self.assertEqual(final["pending_human_action"]["kind"], "dispatch_failed")
        self.assertEqual(final["pending_human_action"]["counter"], "dispatch_recovery")
        self.assertEqual([item["kind"] for kind, item in staged if kind == "events"],
                         ["exhaust_recovery"])
        self.assertEqual(report[-1]["decision"], "dispatch failed; waiting for a human")

    def test_each_ended_dispatcher_run_is_decided_once(self):
        relaunch, staged, _report = self.recover(1)
        decided = [item for kind, item in staged if kind == "contributions"][-1]
        self.assertEqual(decided["active_work"]["dispatched_run_id"], "45005112315")
        [event] = [item for kind, item in staged if kind == "events"]
        self.assertEqual((event["kind"], event["cause"]["id"]),
                         ("retry_dispatch", "dispatcher-45005112315"))
        # The row records the decided run: only `active_work.dispatched_run_id`
        # changes in the task, and no write beside the row records it.
        self.assertEqual(decided["active_work"],
                         dict(self.record["active_work"], dispatched_run_id="45005112315"))
        with self.assertRaises(engine.TransitionError):
            engine.record_beside(self.record, "dispatcher_run_decided", now=decided["updated_at"],
                                 active_work=decided["active_work"])
        # The relaunched run is not listed: the launch was lost, so it is
        # launched again, and no row is taken again.
        self.record = decided
        relaunch, staged, report = self.recover(1)
        self.assertEqual((len(relaunch), staged), (1, []))
        self.assertEqual(report[0]["decision"], "relaunch: the requested run did not start")


class TestWorkerBlockedRecovery(unittest.TestCase):
    def publication(self, temporary):
        directory = Path(temporary) / RUN_ID
        shutil.copytree(FIXTURE, directory)
        outcome = worker_recovery.normalize(directory, RUN_ID)
        (directory / "artifact-id").write_text(str(ARTIFACT_ID))
        publications = reconcile._publication_outcomes(Path(temporary))
        self.assertEqual(outcome["status"], "worker_blocked")
        return publications[0]

    def state(self):
        record = json.loads(
            (FIXTURE / "contribution.json").read_text()
        )
        record = copy.deepcopy(record)
        record.update(
            phase="development",
            activity="waiting_ci",
            active_work=None,
            active_operation_id=None,
            fork={
                "repository_id": 55,
                "name": "riseproject-upstreaming/ciso8601",
                "head_ref": "riscv64-dev",
                "head_sha": record["upstream"]["base_sha"],
                "pr_number": None,
            },
        )
        operations = {
            "op-e6b6689f44d1434a6adcabcd": json.loads(
                (FIXTURE / "canary-operation.json").read_text()
            ),
            "op-eedfbd35ac6db9c814795d66": json.loads(
                (FIXTURE / "fork-operation.json").read_text()
            ),
        }
        fork_op = operations["op-eedfbd35ac6db9c814795d66"]
        operations[fork_op["operation_id"]] = dict(fork_op, status="confirmed")
        project = json.loads((FIXTURE / "project.json").read_text())
        return record, operations, project

    def test_a_blocker_without_requested_task_is_a_late_result(self):
        """The record names the blocked run as its proposal, but holds no task
        for it and is at a later revision: the Result matches nothing and
        takes `record_late_effect`, once."""
        with tempfile.TemporaryDirectory() as temporary:
            publication = self.publication(temporary)
        record, operations, project = self.state()
        record["revision"] += 1
        # `development.waiting_ci` requires the development PR: a record
        # without one is healed first (``reconcile._heal_invariant``).
        record["fork"] = dict(record["fork"], pr_number=12)

        def tick(record, now):
            return reconcile.tick(
                None,
                reconcile.Snapshot(
                    {"c": record}, operations, {}, {project["project_id"]: project}, {}
                ),
                policy=policy(read_only=False),
                now=datetime.fromisoformat(now),
                publications=[publication],
                pressure=capacity.Pressure(ok=True),
                issues=[],
                comments=[],
                observe=False,
            )

        # Its artifact was received before: nothing is folded or recorded again.
        self.assertIn(ARTIFACT_ID, record["processed_publications"])
        output = tick(record, "2026-11-22T05:30:00+00:00")
        self.assertFalse(any(kind in ("evidence", "events") for kind, _item in output.staged))
        # A first receipt of the same Result is recorded as late, once.
        fresh = dict(record, processed_publications=[
            pid for pid in record["processed_publications"] if pid != ARTIFACT_ID])
        output = tick(fresh, "2026-11-22T05:31:00+00:00")
        self.assertFalse(any(kind == "evidence" for kind, _item in output.staged))
        self.assertEqual([item["kind"] for kind, item in output.staged if kind == "events"],
                         ["record_late_effect"])
        final = [item for kind, item in output.staged if kind == "contributions"][-1]
        self.assertEqual((final["revision"], final["budget"]), (fresh["revision"], fresh["budget"]))
        repeated = tick(final, "2026-11-22T05:32:00+00:00")
        self.assertFalse(any(kind == "events" for kind, _item in repeated.staged))

    def test_blocked_outcomes_charge_no_progress_and_its_exhaustion_refreshes_then_parks(self):
        import controller

        record = contribution(phase="development", activity="agent_running")
        proposal = json.loads((FIXTURE / "proposal.json").read_text())
        before = copy.deepcopy(record["budget"])
        for attempt in (1, 2):
            outcome = {
                "status": "worker_blocked",
                "failure_class": "infrastructure",
                "reason": "Docker unavailable",
                "proposal": proposal,
                "artifact": {
                    "digest": "sha256:" + str(attempt) * 64,
                    "run_id": str(100 + attempt),
                },
            }
            # Each blocked Result answers a task requested in agent_running.
            record = dict(record, activity="agent_running",
                          active_work={"dedupe_key": f"example--proj/12@g0.r{record['revision']}"
                                                     ":development_patch",
                                       "task": "development_patch",
                                       "revision": record["revision"], "attempt": 1,
                                       "requested_at": "2026-11-22T05:00:00+00:00",
                                       "deadline_at": "2026-11-22T23:00:00+00:00",
                                       "generation": 0, "dispatched_run_id": None,
                                       "reason_ids": [], "feedback_evidence_ids": []})
            record, _staged = reconcile.apply_publication(
                record,
                outcome,
                now=f"2026-11-22T05:3{attempt}:00+00:00",
                policy=policy(read_only=False),
                worker_run_id=str(100 + attempt),
            )
        # Each blocked outcome charges `no_progress` only; `work_total` and
        # the clusters are charged when a task is allocated.
        self.assertEqual(record["budget"]["no_progress_streak"],
                         before["no_progress_streak"] + 2)
        self.assertEqual(record["budget"]["attempts_used"], before["attempts_used"])
        self.assertEqual(record["budget"]["clusters"], before["clusters"])
        self.assertEqual((record["phase"], record["activity"]), ("development", "fix_needed"))

        def select(record):
            res = controller.tick(contributions={"c": record}, operations={}, consumed={},
                                  projects={}, policy=policy(),
                                  inputs=controller.Inputs(now=datetime.fromisoformat(
                                      "2026-11-22T06:00:00+00:00"),
                                      pressure=capacity.Pressure(ok=True)))
            return res, [item for kind, item in res.staged if kind == "contributions"][-1]

        # `no_progress` is exhausted where the next task is due: the one
        # automatic refresh of the generation runs complete planning.
        res, refreshed = select(record)
        self.assertEqual([e["kind"] for k, e in res.staged if k == "events"][:1],
                         ["refresh_on_exhaustion"])
        self.assertTrue(refreshed["automatic_refresh_used"])
        self.assertEqual(refreshed["budget"]["no_progress_streak"], 0)
        # After it, the same exhaustion parks.
        again = dict(record, automatic_refresh_used=True)
        res, parked = select(again)
        self.assertEqual([e["kind"] for k, e in res.staged if k == "events"],
                         ["exhaust_repair_attempts"])
        self.assertEqual((parked["phase"], parked["activity"]), ("parked", "idle"))
        self.assertEqual(parked["previous_phase"], "development")
        self.assertIn("no_progress", parked["parked_reason"])
        self.assertIsNone(parked["active_work"])

    def test_other_blocked_or_mutated_proposals_are_not_reclassified(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / RUN_ID
            shutil.copytree(FIXTURE, directory)
            proposal_path = directory / "proposal.json"
            proposal = json.loads(proposal_path.read_text())
            proposal["failure_cluster"] = "dependency-missing"
            proposal_path.write_text(json.dumps(proposal))
            outcome = worker_recovery.normalize(directory, RUN_ID)
        self.assertEqual(outcome["status"], "proposal")


if __name__ == "__main__":
    unittest.main()
