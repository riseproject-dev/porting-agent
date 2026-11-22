# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Milestone 6 destination intent, token, publication, PR, and CI contracts."""

import copy
import json
import unittest
from datetime import datetime
from pathlib import Path

import yaml

import capacity
import controller
import fork_publish
import publisher
from tests.helpers import policy

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
NOW = "2026-11-22T05:00:00+00:00"


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def source(name):
    return yaml.safe_load((WORKFLOWS / f"{name}.md").read_text().split("---", 2)[1])


def fixtures():
    record = json.loads(
        (ROOT / "tests/fixtures/canary-m5-provisioning-state.json").read_text()
    )
    operation = json.loads(
        (ROOT / "tests/fixtures/canary-m5-provisioning-operation.json").read_text()
    )
    recovery = json.loads(
        (ROOT / "tests/fixtures/canary-recovery-operation.json").read_text()
    )
    project = json.loads((ROOT / "tests/fixtures/canary-m5-project.json").read_text())
    return record, operation, recovery, project


class TestDurableProvisioningLaunch(unittest.TestCase):
    def tick(self, record, operation, recovery, project):
        issue = {
            "id": record["tracking_issue"]["id"],
            "number": 1,
            "title": "[upstreaming] ciso8601 riscv64 wheel support",
            "body": record["tracking_issue"]["description"],
            "labels": [{"name": "upstreaming-request"}],
        }
        return controller.tick(
            contributions={"c": record},
            operations={
                operation["operation_id"]: operation,
                recovery["operation_id"]: recovery,
            },
            consumed={},
            projects={project["project_id"]: project},
            policy=policy(read_only=False, destination_writes=True),
            inputs=controller.Inputs(
                now=datetime.fromisoformat(NOW),
                pressure=capacity.Pressure(ok=True),
                tracking_issues={1: issue},
            ),
        )

    def test_existing_planned_operation_is_launched_without_replanning(self):
        record, operation, recovery, project = fixtures()
        result = self.tick(record, operation, recovery, project)
        self.assertEqual(
            result.report["provision_requests"],
            [{
                "operation_id": operation["operation_id"],
                "contribution_id": record["contribution_id"],
                "attempt": 1,
            }],
        )
        staged = next(
            value for kind, value in result.staged
            if kind == "operations" and value["operation_id"] == operation["operation_id"]
        )
        self.assertEqual(staged["status"], "planned")
        self.assertEqual(staged["external_ids"]["launch_attempts"], 1)
        self.assertEqual(result.report["selected"], [])

    def test_pre_fork_proposal_rearms_exact_not_launched_operation(self):
        record, operation, recovery, project = fixtures()
        record = copy.deepcopy(record)
        operation = dict(operation, status="not_launched")
        record.update(
            phase="development",
            activity="waiting_capacity",
            active_operation_id=None,
            active_work=None,
            proposal_artifact={
                "outcome": "proposal",
                "run_id": "44990000000",
                "artifact_id": 7,
                "work_id": "w",
                "claim_id": "c",
                "name": "claim-x-porting-out",
                "digest": "sha256:" + "a" * 64,
                "result_commit": "q_" + "b" * 64,
            },
        )
        result = self.tick(record, operation, recovery, project)
        final = [value for kind, value in result.staged if kind == "contributions"][-1]
        self.assertEqual(final["phase"], "provisioning")
        self.assertEqual(final["active_operation_id"], operation["operation_id"])
        self.assertEqual(result.report["provision_requests"][0]["attempt"], 1)
        self.assertTrue(any(
            kind == "events" and value["kind"] == "destination_operation_rearmed"
            for kind, value in result.staged
        ))


    def test_verified_pre_fork_proposal_launch_is_durably_fenced(self):
        record, operation, recovery, project = fixtures()
        record = copy.deepcopy(record)
        operation = dict(operation, status="confirmed")
        record.update(
            phase="development",
            activity="waiting_capacity",
            active_operation_id=None,
            active_work=None,
            fork={
                "repository_id": 55,
                "name": "riseproject-upstreaming/ciso8601",
                "head_ref": "riscv64",
                "head_sha": record["upstream"]["base_sha"],
                "pr_number": None,
            },
            proposal_artifact={
                "outcome": "proposal",
                "run_id": "44990000000",
                "artifact_id": 7,
                "work_id": "w",
                "claim_id": "c",
                "name": "claim-x-porting-out",
                "digest": "sha256:" + "a" * 64,
                "result_commit": "q_" + "b" * 64,
            },
        )
        result = self.tick(record, operation, recovery, project)
        request = result.report["proposal_publications"][0]
        self.assertEqual(request["worker_run_id"], "44990000000")
        self.assertEqual(request["attempt"], 1)
        final = [value for kind, value in result.staged if kind == "contributions"][-1]
        self.assertEqual(final["pending_publication"]["attempts"], 1)
        self.assertEqual(final["activity"], "waiting_capacity")
        self.assertEqual(result.report["selected"], [])


class TestDestinationTokenBoundaries(unittest.TestCase):
    def test_provisioning_creation_and_one_fork_tokens_are_separate(self):
        steps = workflow("provision.yml")["jobs"]["provision"]["steps"]
        creator = next(step for step in steps if step.get("id") == "creator")
        fork = next(step for step in steps if step.get("id") == "fork-token")
        self.assertEqual(creator["with"]["owner"], "riseproject-upstreaming")
        self.assertNotIn("repositories", creator["with"])
        self.assertEqual(
            creator["env"]["INPUT_PERMISSION-REPOSITORY-CREATION"], "write"
        )
        self.assertEqual(fork["with"]["repositories"], "${{ steps.create.outputs.fork_name }}")
        self.assertEqual(fork["with"]["permission-administration"], "write")
        self.assertEqual(fork["with"]["permission-actions"], "write")
        self.assertEqual(fork["with"]["permission-contents"], "write")
        self.assertEqual(fork["with"]["permission-workflows"], "write")

    def test_worker_model_boundary_has_no_destination_secret(self):
        worker = source("worker")
        for boundary in (
            worker["pre-agent-steps"],
            worker["jobs"]["safe_outputs"],
        ):
            text = str(boundary)
            self.assertNotIn("${{ secrets.RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY }}", text)
            self.assertNotIn("${{ vars.RISEPROJECT_UPSTREAMING_APP_CLIENT_ID }}", text)
        publish = worker["jobs"]["publish"]
        token = next(step for step in publish["steps"] if step.get("id") == "fork-token")
        self.assertEqual(token["with"]["repositories"], "${{ steps.result.outputs.fork_name }}")
        self.assertEqual(token["with"]["owner"], "riseproject-upstreaming")


class TestPublicationAndPRContracts(unittest.TestCase):
    def test_pre_fork_proposal_binds_only_to_verified_base_fork(self):
        from tests.test_milestone5_worker import mbox, proposal, task

        task_value = task()
        patch = mbox()
        proposal_value = proposal(patch)
        record, _operation, _recovery, _project = fixtures()
        record = copy.deepcopy(record)
        record.update(
            phase="development",
            active_operation_id=None,
            active_work=None,
            proposal_artifact={"outcome": "proposal", "run_id": "9"},
            fork={
                "repository_id": 55,
                "name": "riseproject-upstreaming/ciso8601",
                "head_ref": "riscv64",
                "head_sha": record["upstream"]["base_sha"],
                "pr_number": None,
            },
        )
        task_value["base_sha"] = record["upstream"]["base_sha"]
        task_value["fork"] = None
        task_value["fork_repository_id"] = None
        proposal_value["reconstruction_base"] = record["upstream"]["base_sha"]
        result = fork_publish.preflight(
            record=record,
            task=task_value,
            proposal=proposal_value,
            fork_lookup=lambda _name: {
                "id": 55,
                "full_name": "riseproject-upstreaming/ciso8601",
                "archived": False,
            },
        )
        self.assertTrue(result["publish"])
        moved = copy.deepcopy(record)
        moved["fork"]["head_sha"] = "f" * 40
        rejected = fork_publish.preflight(
            record=moved,
            task=task_value,
            proposal=proposal_value,
            fork_lookup=lambda _name: {},
        )
        self.assertFalse(rejected["publish"])

    def test_publication_is_exact_head_leased_and_recoverable(self):
        self.assertEqual(
            publisher.lease_refspec("b" * 40, "a" * 40),
            ["--force-with-lease=refs/heads/riscv64:" + "a" * 40,
             "b" * 40 + ":refs/heads/riscv64"],
        )
        retry = (WORKFLOWS / "publish-retry.yml").read_text()
        self.assertIn("python -m queue_result", retry)
        self.assertIn("python -m fork_publish preflight", retry)
        self.assertIn("python -m fork_publish publish", retry)
        self.assertIn("--previous-candidate", retry)
        self.assertIn("porting-publication", retry)

    def test_development_pr_is_same_fork_and_continuations_are_explicit(self):
        dev = workflow("dev-pr.yml")
        self.assertEqual(dev["concurrency"]["group"], "porting-fork-writer")
        create_text = str(dev["jobs"]["create"])
        self.assertIn("python -m dev_pr", create_text)
        for name in ("provision.yml", "dev-pr.yml", "publish-retry.yml"):
            continuation = workflow(name)["jobs"]["continuation"]
            self.assertEqual(continuation["if"], "always()")
            self.assertIn("gh workflow run controller.yml", str(continuation))

    def test_controller_collects_receipts_and_launches_only_durable_requests(self):
        text = (WORKFLOWS / "controller.yml").read_text()
        for required in (
            "porting-provisioning",
            "porting-development-pr",
            "provision_requests",
            "proposal_publications",
            "publication_retries",
            "dev_pr_requests",
        ):
            self.assertIn(required, text)
        self.assertIn("gh workflow run provision.yml", text)
        self.assertIn("gh workflow run publish-retry.yml", text)
        self.assertIn("gh workflow run dev-pr.yml", text)


if __name__ == "__main__":
    unittest.main()
