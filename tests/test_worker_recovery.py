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
        self.assertEqual(record["activity"], "waiting_human")
        self.assertEqual(
            record["pending_human_action"]["kind"], "worker_attempt_budget_exhausted"
        )

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
