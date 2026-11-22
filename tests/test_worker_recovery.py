# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Recover the exact Docker-blocked worker proposal into bounded fresh Work."""

import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import capacity
import reconcile
import work
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
            "reason_ids": ["dossier-1"],
        }
        graph = work.graph_id(record["active_work"]["dedupe_key"])
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
    """Run 45005299790 failed before the agent and returned its Work to available."""

    def setUp(self):
        self.record = contribution(phase="development", activity="agent_running", revision=4)
        self.record["active_work"] = {
            "dedupe_key": "closeio--ciso8601/1@4:development_repair",
            "task": "development_repair",
            "revision": 4,
            "attempt": 1,
            "requested_at": "2026-11-22T08:55:56.948866+00:00",
            "deadline_at": "2026-11-22T14:55:56.948866+00:00",
            "dispatched_run_id": "45005112315",
            "reason_ids": [
                "rehearsal-closeio--ciso8601--1-r3-44982902579",
                "dossier-closeio--ciso8601--1-r2",
            ],
        }
        self.graph = work.graph_id(self.record["active_work"]["dedupe_key"])
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
        self.assertEqual(report[-1]["decision"], "dispatch failed; waiting for a human")


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
            activity="waiting_capacity",
            active_work=None,
            active_operation_id=None,
            fork={
                "repository_id": 55,
                "name": "riseproject-upstreaming/ciso8601",
                "head_ref": "riscv64",
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

    def test_exact_blocker_is_reprocessed_despite_prior_artifact_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            publication = self.publication(temporary)
        record, operations, project = self.state()
        # The failed trusted publication may already have advanced the current
        # revision after rejecting operation:none; exact blocker recovery is
        # still bound by worker run and stored proposal identity.
        record["revision"] += 1
        output = reconcile.tick(
            None,
            reconcile.Snapshot(
                {"c": record}, operations, {}, {project["project_id"]: project}, {}
            ),
            policy=policy(read_only=False, destination_writes=True),
            now=datetime.fromisoformat("2026-11-22T05:30:00+00:00"),
            publications=[publication],
            pressure=capacity.Pressure(ok=True),
            issues=[],
            comments=[],
            observe=False,
        )
        evidence = [
            item for kind, item in output.staged
            if kind == "evidence" and item["kind"] == "rehearsal"
        ]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["data"]["worker_run_id"], RUN_ID)
        final = [item for kind, item in output.staged if kind == "contributions"][-1]
        self.assertEqual(final["revision"], 5)
        self.assertEqual(final["budget"]["attempts_used"], 1)
        self.assertIsNone(final["proposal_artifact"])
        self.assertEqual(final["active_work"]["task"], "development_repair")
        self.assertIn(evidence[0]["evidence_id"], final["active_work"]["reason_ids"])

        repeated = reconcile.tick(
            None,
            reconcile.Snapshot(
                {"c": final}, operations, {}, {project["project_id"]: project},
                {evidence[0]["evidence_id"]: evidence[0]},
            ),
            policy=policy(read_only=False, destination_writes=True),
            now=datetime.fromisoformat("2026-11-22T05:31:00+00:00"),
            publications=[publication],
            pressure=capacity.Pressure(ok=True),
            issues=[],
            comments=[],
            observe=False,
        )
        self.assertFalse(any(kind == "evidence" for kind, _item in repeated.staged))

    def test_no_progress_and_cluster_retry_are_bounded(self):
        record, _operations, _project = self.state()
        proposal = json.loads((FIXTURE / "proposal.json").read_text())
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
            record, _staged = reconcile.apply_publication(
                record,
                outcome,
                now=f"2026-11-22T05:3{attempt}:00+00:00",
                policy=policy(read_only=False, destination_writes=True),
                worker_run_id=str(100 + attempt),
            )
        self.assertEqual(record["budget"]["attempts_used"], 2)
        self.assertEqual(
            record["budget"]["clusters"]["reproduction-image-infrastructure-docker-unavailable"],
            2,
        )
        # Plan 3.3.3 development failure case 1: exhaustion enters parked.
        self.assertEqual((record["phase"], record["activity"]), ("parked", "idle"))
        self.assertEqual(record["previous_phase"], "development")
        self.assertIn("repair-attempt budget exhausted", record["parked_reason"])
        self.assertIn("worker reproduction blocked", record["parked_reason"])
        self.assertIsNone(record["pending_human_action"])
        self.assertIsNone(record["active_work"])
        self.assertEqual(
            [item["kind"] for kind, item in _staged if kind == "events"],
            ["repair_attempts_exhausted"],
        )
        # Parked contributions allocate no Work.
        _record, operations, project = self.state()
        output = reconcile.tick(
            None,
            reconcile.Snapshot({"c": record}, operations, {}, {project["project_id"]: project}, {}),
            policy=policy(read_only=False, destination_writes=True),
            now=datetime.fromisoformat("2026-11-22T05:40:00+00:00"),
            publications=[],
            pressure=capacity.Pressure(ok=True),
            issues=[],
            comments=[],
            observe=False,
        )
        final = ([item for kind, item in output.staged if kind == "contributions"] or [record])[-1]
        self.assertEqual(final["phase"], "parked")
        self.assertIsNone(final["active_work"])
        self.assertEqual(output.report.get("requested_effects", []), [])

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
