# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Recover the exact verified Result from live investigator run 44974390332."""

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import capacity
import investigation_recovery
import reconcile
from tests.helpers import policy

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/investigation-record-failure"
RUN_ID = "44974390332"


class TestInvestigationRecordRecovery(unittest.TestCase):
    def directory(self, temporary):
        directory = Path(temporary) / RUN_ID
        shutil.copytree(FIXTURE, directory)
        return directory

    def test_live_verified_result_is_revalidated_and_reintroduced(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary)
            outcome = investigation_recovery.recover(directory, RUN_ID)
            self.assertEqual(
                outcome,
                {
                    "status": "investigation",
                    "failure_class": None,
                    "reason": None,
                    "stage": "result",
                },
            )
            self.assertEqual(json.loads((directory / "outcome.json").read_text()), outcome)
            self.assertIn("run_id=44974390332", (directory / "recovered-investigation").read_text())
            publications = reconcile._publication_outcomes(Path(temporary))
        self.assertEqual(len(publications), 1)
        self.assertEqual(publications[0]["status"], "investigation")
        self.assertEqual(publications[0]["worker_run_id"], RUN_ID)
        self.assertEqual(publications[0]["artifact"]["run_id"], RUN_ID)
        self.assertEqual(
            publications[0]["dossier"]["tracking_issue_description_digest"],
            "9f58bffa65d7e7df0206ae1130ce6eb5e218181b966839f2e609c05e8baa7ebb",
        )

    def test_ordinary_reconciliation_folds_recovered_result_without_replay(self):
        contribution = json.loads(
            (ROOT / "tests/fixtures/canary-investigation-active-state.json").read_text()
        )
        operation = json.loads(
            (ROOT / "tests/fixtures/canary-recovery-operation.json").read_text()
        )
        project = json.loads((ROOT / "tests/fixtures/canary-project.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary)
            investigation_recovery.recover(directory, RUN_ID)
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
            policy=policy(read_only=False, destination_writes=False),
            now=datetime.fromisoformat("2026-11-22T02:20:00+00:00"),
            publications=publications,
            pressure=capacity.Pressure(ok=False, reason="offline"),
            issues=[],
            comments=[],
            observe=False,
        )
        dossier = next(
            record
            for kind, record in output.staged
            if kind == "evidence" and record["kind"] == "dossier"
        )
        final = [record for kind, record in output.staged if kind == "contributions"][-1]
        self.assertEqual(dossier["contribution_id"], "closeio--ciso8601/1")
        self.assertEqual(final["dossier_evidence_id"], dossier["evidence_id"])
        self.assertEqual((final["phase"], final["activity"]), ("development", "idle"))
        self.assertIsNone(final["active_work"])
        self.assertEqual(output.report["controller"]["selected"], [])

    def test_wrong_run_task_result_or_dossier_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary)
            with self.assertRaisesRegex(
                investigation_recovery.InvestigationRecoveryError, "run attempt"
            ):
                investigation_recovery.recover(directory, "44974390333")

            task_path = directory / "task.json"
            task = json.loads(task_path.read_text())
            task["task"] = "development_patch"
            task_path.write_text(json.dumps(task))
            with self.assertRaisesRegex(
                investigation_recovery.InvestigationRecoveryError, "only investigation"
            ):
                investigation_recovery.recover(directory, RUN_ID)

        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary)
            result_path = directory / "result.json"
            result = json.loads(result_path.read_text())
            result["digest"] = "bad"
            result_path.write_text(json.dumps(result))
            with self.assertRaisesRegex(
                investigation_recovery.InvestigationRecoveryError, "digest"
            ):
                investigation_recovery.recover(directory, RUN_ID)

        with tempfile.TemporaryDirectory() as temporary:
            directory = self.directory(temporary)
            dossier_path = directory / "dossier.json"
            dossier = json.loads(dossier_path.read_text())
            del dossier["research"]["maintainer_reviews"]
            dossier_path.write_text(json.dumps(dossier))
            with self.assertRaises(ValueError):
                investigation_recovery.recover(directory, RUN_ID)

    def test_controller_recovery_is_bounded_to_trusted_investigator_artifacts(self):
        text = (ROOT / ".github/workflows/controller.yml").read_text()
        self.assertIn("python -m investigation_recovery", text)
        self.assertIn("trusted investigation recovery rejected", text)
        self.assertIn("publications/*/recovered-investigation", text)
        self.assertIn("PORTING_DOSSIER_RECOVERY: '1'", text)
        self.assertNotIn("work-queue reset", text)


if __name__ == "__main__":
    unittest.main()
