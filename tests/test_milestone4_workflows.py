# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Milestone 4 native queue, investigator, and automatic pickup contracts."""

import json
import unittest
from datetime import datetime
from pathlib import Path

import yaml

import capacity
import controller
import work
from common import load_policy

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def source(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / f"{name}.md").read_text().split("---", 2)[1])


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def manifest(name: str) -> dict:
    prefix = "# gh-aw-manifest: "
    line = next(
        line for line in (WORKFLOWS / f"{name}.lock.yml").read_text().splitlines()
        if line.startswith(prefix)
    )
    return json.loads(line.removeprefix(prefix))


class TestNativeQueueContracts(unittest.TestCase):
    def test_authored_sources_have_one_generated_v122_lock_each(self):
        for name in ("dispatcher", "investigator"):
            self.assertTrue((WORKFLOWS / f"{name}.md").is_file())
            self.assertTrue((WORKFLOWS / f"{name}.lock.yml").is_file())
            compiled = manifest(name)
            setup = [
                action
                for action in compiled["actions"]
                if action["repo"] == "github/gh-aw-actions/setup"
            ]
            self.assertEqual(len(setup), 1)
            self.assertEqual(setup[0]["version"], "v1.2.2")

    def test_dispatcher_policy_admits_only_current_profiles(self):
        dispatcher = source("dispatcher")
        dispatch = dispatcher["safe-outputs"]["dispatch-workflow"]
        self.assertEqual(dispatch["workflows"], ["investigator", "worker"])
        self.assertEqual(dispatch["max"], 1)
        lock = workflow("dispatcher.lock.yml")
        policies = []
        for job in lock["jobs"].values():
            for step in job.get("steps", []):
                raw = (step.get("env") or {}).get("GH_AW_WORK_QUEUE_POLICY")
                if raw:
                    policies.append(json.loads(raw))
        self.assertTrue(policies)
        for policy in policies:
            pool = policy["pools"]["default"]
            self.assertEqual(set(pool["profiles"]), {"investigator", "worker"})
            self.assertEqual(
                pool["profiles"]["investigator"]["workflow"],
                ".github/workflows/investigator.lock.yml",
            )
            self.assertEqual(
                pool["profiles"]["worker"]["workflow"],
                ".github/workflows/worker.lock.yml",
            )
            self.assertTrue(all(profile["max_claims"] == 1 for profile in pool["profiles"].values()))

    def test_current_queued_canary_is_selected_without_manual_intervention(self):
        contribution = json.loads(
            (ROOT / "tests/fixtures/canary-queued-state.json").read_text()
        )
        operation = json.loads(
            (ROOT / "tests/fixtures/canary-recovery-operation.json").read_text()
        )
        project = json.loads(
            (ROOT / "tests/fixtures/canary-project.json").read_text()
        )
        issue = {
            "id": contribution["tracking_issue"]["id"],
            "number": 1,
            "title": "[upstreaming] ciso8601 riscv64 wheel support",
            "body": contribution["tracking_issue"]["description"],
            "labels": [{"name": "upstreaming-request"}],
        }
        result = controller.tick(
            contributions={"c": contribution},
            operations={operation["operation_id"]: operation},
            consumed={},
            projects={project["project_id"]: project},
            policy=load_policy(),
            inputs=controller.Inputs(
                now=datetime.fromisoformat("2026-11-22T02:30:00+00:00"),
                pressure=capacity.Pressure(ok=True),
                tracking_issues={1: issue},
            ),
        )
        self.assertEqual(
            result.report["selected"],
            [{"contribution": "closeio--ciso8601/1", "task": "investigation"}],
        )
        self.assertEqual(len(result.requested_effects), 1)
        staged = [record for kind, record in result.staged if kind == "contributions"][-1]
        request = work.request_from_active(staged)
        node = work.node(
            request,
            staged,
            control_repo_id="1",
            allowed_paths=[".github/workflows/*"],
        )
        self.assertEqual(node["pool"], "default")
        self.assertEqual(node["worker_profile"], "investigator")
        self.assertEqual(
            node["payload"]["context"]["tracking_issue"]["description_digest"],
            "9f58bffa65d7e7df0206ae1130ce6eb5e218181b966839f2e609c05e8baa7ebb",
        )
        self.assertLess(
            len(json.dumps(node["payload"], separators=(",", ":")).encode()), 15_500
        )


class TestInvestigatorSecurityAndContinuation(unittest.TestCase):
    def test_investigator_is_authenticated_read_only_and_complete(self):
        investigator = source("investigator")
        self.assertEqual(
            investigator["permissions"],
            {
                "contents": "read",
                "issues": "read",
                "pull-requests": "read",
                "actions": "read",
            },
        )
        github = investigator["mcp-servers"]["github-authenticated"]
        self.assertEqual(github["env"]["GITHUB_READ_ONLY"], "1")
        self.assertEqual(
            github["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"], "${{ github.token }}"
        )
        text = (WORKFLOWS / "investigator.md").read_text()
        for required in (
            "Read the ENTIRE issue description",
            "full upstream issue and PR corpus",
            "actual PR reviews and review threads",
            "project_graph_query",
            "public PyPI",
            "https://pypi.riseproject.dev/simple",
            "complete multi-path coverage",
            "work_queue_claim_finish",
        ):
            self.assertIn(required, text)
        self.assertNotIn("RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY", text)
        self.assertNotIn("RISEPROJECT_UPSTREAMING_APP_CLIENT_ID", text)
        for trusted_boundary in (
            investigator.get("pre-agent-steps", []),
            investigator["jobs"]["safe_outputs"],
            investigator["jobs"]["record"],
        ):
            self.assertNotIn("RISEPROJECT_APP_PRIVATE_KEY", str(trusted_boundary))

    def test_completeness_gate_and_result_resolution_are_trusted(self):
        investigator = source("investigator")
        pre_steps = investigator["jobs"]["safe_outputs"]["pre-steps"]
        gate = next(
            step
            for step in pre_steps
            if step.get("name") == "Require complete research before native queue Completion"
        )
        self.assertIn("python -m dossier_gate", gate["run"])
        record = investigator["jobs"]["record"]
        self.assertEqual(record["if"], "needs.safe_outputs.outputs.work_queue_outcome == 'completed'")
        record_text = str(record)
        self.assertIn("python -m queue_result", record_text)
        self.assertIn("python -m artifact", record_text)
        self.assertIn("from dossier_gate import validate", record_text)
        self.assertIn("porting-publication", record_text)

    def test_dispatcher_and_investigator_continue_controller_explicitly(self):
        expectations = {
            "dispatcher": {"safe_outputs"},
            "investigator": {"safe_outputs", "record", "notify"},
        }
        for name, needs in expectations.items():
            continuation = source(name)["jobs"]["continuation"]
            self.assertEqual(set(continuation["needs"]), needs)
            self.assertEqual(continuation["if"], "always()")
            token = next(
                step for step in continuation["steps"] if step.get("id") == "control-token"
            )
            self.assertEqual(token["with"]["repositories"], "porting-agent")
            self.assertEqual(token["with"]["permission-contents"], "read")
            self.assertEqual(token["with"]["permission-actions"], "write")
            self.assertIn("gh workflow run controller.yml", str(continuation))

    def test_controller_has_artifact_and_schedule_fallback_without_destination_write(self):
        controller_text = (WORKFLOWS / "controller.yml").read_text()
        controller_workflow = workflow("controller.yml")
        triggers = controller_workflow.get("on", controller_workflow.get(True))
        self.assertEqual(
            set(triggers["workflow_run"]["workflows"]),
            {
                "Dispatcher",
                "Investigator",
                "Worker",
                "Reproduction Image",
                "Fork Provisioning",
                "Development PR",
                "Publish Retry",
            },
        )
        self.assertIn("for workflow in investigator.lock.yml worker.lock.yml", controller_text)
        self.assertIn("actions/workflows/$workflow/runs?per_page=20", controller_text)
        self.assertIn('name == "porting-publication"', controller_text)
        self.assertIn("gh workflow run dispatcher.lock.yml", controller_text)
        self.assertNotIn("RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY", controller_text)
        self.assertNotIn("RISEPROJECT_UPSTREAMING_APP_CLIENT_ID", controller_text)


if __name__ == "__main__":
    unittest.main()
