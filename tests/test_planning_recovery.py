# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Recover the exact verified Result from live planner run 44974390332."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import capacity
import plan_gate
import planning_recovery
import reconcile
from tests.helpers import policy
from tests.test_plan_gate import guidelines, project

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/planning-record-failure"
RUN_ID = "44974390332"


class TestPlanningRecordRecovery(unittest.TestCase):
    def directory(self, temporary, *, version=4):
        """The live run's partial publication. Its plan is of schema
        version 1; with ``version=4`` it records the contribution guidelines
        and the project a plan must now record."""
        directory = Path(temporary) / RUN_ID
        shutil.copytree(FIXTURE, directory)
        if version == 4:
            path = directory / "plan.json"
            plan = json.loads(path.read_text())
            plan.update(schema_version=4, contribution_guidelines=guidelines(
                plan["proposal"]["allowed_paths"]), project=project())
            path.write_text(json.dumps(plan))
        return directory

    def test_a_plan_without_the_contribution_guidelines_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary, version=1)
            with self.assertRaisesRegex(plan_gate.PlanError, "schema version 4"):
                planning_recovery.recover(directory, RUN_ID)
            self.assertFalse((directory / "outcome.json").exists())

    def test_live_verified_result_is_revalidated_and_reintroduced(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary)
            outcome = planning_recovery.recover(directory, RUN_ID)
            self.assertEqual(
                outcome,
                {
                    "status": "planning",
                    "failure_class": None,
                    "reason": None,
                    "stage": "result",
                },
            )
            self.assertEqual(json.loads((directory / "outcome.json").read_text()), outcome)
            self.assertIn("run_id=44974390332", (directory / "recovered-planning").read_text())
            publications = reconcile._publication_outcomes(Path(temporary))
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0]["status"], "planning")
        self.assertEqual(publications[0]["worker_run_id"], RUN_ID)
        self.assertEqual(publications[0]["artifact"]["run_id"], RUN_ID)
        self.assertEqual(
            publications[0]["plan"]["tracking_issue_description_digest"],
            "9f58bffa65d7e7df0206ae1130ce6eb5e218181b966839f2e609c05e8baa7ebb",
        )

    def test_ordinary_reconciliation_folds_recovered_result_without_replay(self):
        contribution = json.loads(
            (ROOT / "tests/fixtures/canary-planning-active-state.json").read_text()
        )
        operation = json.loads(
            (ROOT / "tests/fixtures/canary-recovery-operation.json").read_text()
        )
        project = json.loads((ROOT / "tests/fixtures/canary-project.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary)
            planning_recovery.recover(directory, RUN_ID)
            publications = reconcile._publication_outcomes(Path(temporary))
        output = reconcile.tick(
            None,
            reconcile.Snapshot(
                {"c": contribution},
                {operation["operation_id"]: operation},
                {},
                {project["project_id"]: project},
                {},
            ),
            policy=policy(read_only=False),
            now=datetime.fromisoformat("2026-11-22T02:20:00+00:00"),
            publications=publications,
            pressure=capacity.Pressure(ok=False, reason="offline"),
            issues=[],
            comments=[],
            observe=False,
        )
        plan = next(
            record
            for kind, record in output.staged
            if kind == "evidence" and record["kind"] == "plan"
        )
        final = [record for kind, record in output.staged if kind == "contributions"][-1]
        self.assertEqual(plan["contribution_id"], "closeio--ciso8601/1")
        self.assertEqual(final["plan_evidence_id"], plan["evidence_id"])
        # The plan admits development: the fork is provisioned first
        # (without capacity, no launch yet), and no patch Work is requested.
        self.assertEqual((final["phase"], final["activity"]), ("provisioning", "idle"))
        self.assertIsNone(final["active_work"])
        self.assertEqual(output.report["controller"]["selected"], [])
        self.assertEqual(output.report["provision_requests"], [])

    def test_wrong_run_task_result_or_plan_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary)
            with self.assertRaisesRegex(
                planning_recovery.PlanningRecoveryError, "run attempt"
            ):
                planning_recovery.recover(directory, "44974390333")

            task_path = directory / "task.json"
            task = json.loads(task_path.read_text())
            task["task"] = "development_patch"
            task_path.write_text(json.dumps(task))
            with self.assertRaisesRegex(
                planning_recovery.PlanningRecoveryError, "only planning"
            ):
                planning_recovery.recover(directory, RUN_ID)

        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary)
            result_path = directory / "result.json"
            result = json.loads(result_path.read_text())
            result["digest"] = "bad"
            result_path.write_text(json.dumps(result))
            with self.assertRaisesRegex(
                planning_recovery.PlanningRecoveryError, "digest"
            ):
                planning_recovery.recover(directory, RUN_ID)

        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary)
            plan_path = directory / "plan.json"
            plan = json.loads(plan_path.read_text())
            del plan["research"]["maintainer_reviews"]
            plan_path.write_text(json.dumps(plan))
            with self.assertRaises(ValueError):
                planning_recovery.recover(directory, RUN_ID)

    def test_controller_recovery_is_bounded_to_trusted_planner_artifacts(self):
        text = (ROOT / ".github/workflows/controller.yml").read_text()
        self.assertIn("python -m planning_recovery", text)
        self.assertIn("trusted planning recovery rejected", text)
        self.assertIn("publications/*/recovered-planning", text)
        self.assertIn("PORTING_PLAN_RECOVERY: '1'", text)
        self.assertNotIn("work-queue reset", text)


if __name__ == "__main__":
    unittest.main()
