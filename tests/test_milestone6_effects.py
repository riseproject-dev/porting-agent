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
import compat
import controller
import engine
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
            policy=policy(read_only=False),
            inputs=controller.Inputs(
                now=datetime.fromisoformat(NOW),
                pressure=capacity.Pressure(ok=True),
                tracking_issues={1: issue},
            ),
        )

    def test_existing_planned_operation_is_launched_without_replanning(self):
        record, operation, recovery, project = fixtures()
        # The record waits in the provisioning shape of an earlier
        # controller; the record migration rewrites it as `provisioning.idle`
        # with the planned operation, which the table's row then launches.
        self.assertEqual(engine.state_key(record), "provisioning.waiting_capacity")
        record, event = compat.migrate_provisioning(record, operation=operation, now=NOW)
        self.assertEqual(engine.state_key(record), "provisioning.idle")
        self.assertEqual(event["revision_bumped"], False)
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
        self.assertIn("start_operation", [value["kind"] for kind, value in result.staged
                                          if kind == "events"])

    def test_a_deferred_fork_intent_is_planned_again_at_a_new_revision(self):
        """Development without a fork (a fork intent an earlier controller
        retired while destination writes were disabled, and a proposal made
        before the fork) returns to provisioning; the new intent never reuses
        the retired operation."""
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
        migrated, event = compat.migrate_provisioning(record, operation=None, now=NOW)
        self.assertEqual((engine.state_key(migrated), migrated["revision"],
                          migrated["proposal_artifact"]),
                         ("provisioning.idle", record["revision"] + 1, None))
        self.assertTrue(event["revision_bumped"])
        result = self.tick(migrated, operation, recovery, project)
        final = [value for kind, value in result.staged if kind == "contributions"][-1]
        self.assertEqual(engine.state_key(final), "provisioning.idle")
        self.assertNotEqual(final["active_operation_id"], operation["operation_id"])
        self.assertEqual(result.report["provision_requests"][0]["attempt"], 1)
        self.assertEqual(result.report["provision_requests"][0]["operation_id"],
                         final["active_operation_id"])
        self.assertEqual(result.report["selected"], [])

    def test_a_proposal_held_on_the_provisioned_fork_is_discarded(self):
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
                "head_ref": "riscv64-dev",
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
            pending_publication={"worker_run_id": "44990000000", "attempts": 1,
                                 "last_reason": "x", "next_attempt_at": NOW,
                                 "exhausted": False},
        )
        migrated, _event = compat.migrate_provisioning(record, operation=None, now=NOW)
        self.assertEqual((engine.state_key(migrated), migrated["revision"],
                          migrated["proposal_artifact"], migrated["pending_publication"]),
                         ("development.fix_needed", record["revision"] + 1, None, None))
        result = self.tick(migrated, operation, recovery, project)
        # A fresh patch is requested on the fork; nothing held is published.
        self.assertEqual(result.report["selected"][0]["task"], "development_patch")
        self.assertEqual(result.requested_effects[0]["effect_contract"]["kind"], "fork_branch")
        self.assertNotIn("proposal_publications", result.report)


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
    def test_a_task_without_the_recorded_fork_is_never_published(self):
        """Patch Work always names the provisioned fork and its head: a task
        that names none is rejected before any token exists."""
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
            fork={
                "repository_id": 55,
                "name": "riseproject-upstreaming/ciso8601",
                "head_ref": "riscv64-dev",
                "head_sha": record["upstream"]["base_sha"],
                "pr_number": None,
            },
        )
        task_value["base_sha"] = record["upstream"]["base_sha"]
        task_value["fork"] = None
        task_value["fork_repository_id"] = None
        task_value["expected_head"] = None
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
        self.assertFalse(result["publish"])
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(hasattr(fork_publish, "bind_provisioned_task"))

    def test_destination_writes_have_no_policy_switch(self):
        """Patch Work always writes the provisioned fork: the policy has no
        switch that defers destination writes, and no module reads one."""
        import schemas
        from common import load_policy
        current = load_policy()
        self.assertNotIn("destination_writes", current)
        with self.assertRaises(schemas.SchemaError):
            schemas.validate("policy", dict(current, destination_writes=False))
        for module in ROOT.glob("*.py"):
            with self.subTest(module=module.name):
                self.assertNotIn("destination_writes", module.read_text())

    def test_publication_is_exact_head_leased_and_recoverable(self):
        self.assertEqual(
            publisher.lease_refspec("b" * 40, "a" * 40),
            ["--force-with-lease=refs/heads/riscv64-dev:" + "a" * 40,
             "b" * 40 + ":refs/heads/riscv64-dev"],
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
            "publication_retries",
            "dev_pr_requests",
        ):
            self.assertIn(required, text)
        self.assertIn("gh workflow run provision.yml", text)
        self.assertIn("gh workflow run publish-retry.yml", text)
        self.assertIn("gh workflow run dev-pr.yml", text)
        self.assertNotIn("proposal_publications", text)


if __name__ == "__main__":
    unittest.main()
