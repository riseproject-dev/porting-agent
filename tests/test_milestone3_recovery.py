# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Milestone 3 deterministic recovery, trigger isolation, and security gates."""

import copy
import unittest
from datetime import datetime
from pathlib import Path

import yaml

import capacity
import collect
import controller
import triggers
from common import CONTROL_REPO
from scripts import generate_doc
from tests.helpers import NOW, contribution, policy

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"


class TestDeterministicController(unittest.TestCase):
    def test_identical_fixture_inputs_produce_identical_state_effects_and_report(self):
        record = contribution(
            phase="investigation",
            activity="fix_needed",
            fork=None,
            active_work=None,
        )
        inputs = controller.Inputs(
            now=datetime.fromisoformat(NOW),
            pressure=capacity.Pressure(ok=True),
        )
        arguments = {
            "contributions": {"record": record},
            "operations": {},
            "consumed": {},
            "projects": {},
            "policy": policy(read_only=False),
            "inputs": inputs,
        }
        first = controller.tick(**copy.deepcopy(arguments))
        second = controller.tick(**copy.deepcopy(arguments))
        self.assertEqual(first.staged, second.staged)
        self.assertEqual(first.requested_effects, second.requested_effects)
        self.assertEqual(first.report, second.report)


class TestCanonicalTrackingRepository(unittest.TestCase):
    def test_intake_and_tracking_urls_are_control_repository_only(self):
        class Api:
            def __init__(self):
                self.paths = []

            def paginate(self, path, **_kwargs):
                self.paths.append(path)
                return []

        api = Api()
        self.assertEqual(collect.intake_issues(api), [])
        self.assertEqual(
            api.paths,
            [
                "/repos/riseproject-dev/porting-agent/issues?labels="
                "upstreaming-request&state=open&per_page=50"
            ],
        )
        context = collect.tracking_issue_context(
            {"id": 1, "number": 1, "body": "request"},
            {"id": 1, "number": 1},
        )
        self.assertEqual(
            context["url"], "https://github.com/riseproject-dev/porting-agent/issues/1"
        )


class TestTriggerIsolation(unittest.TestCase):
    def workflows_for(self, paths):
        return [
            finding.workflow
            for finding in triggers.audit(WORKFLOWS, paths)
            if finding.triggered
        ]

    def test_state_only_commit_wakes_only_website(self):
        self.assertEqual(
            self.workflows_for([".state/commands/comment-1.json"]),
            ["website.yml"],
        )

    def test_code_and_workflow_deployment_wake_controller_not_status_recursion(self):
        code = self.workflows_for(["controller.py"])
        self.assertIn("controller.yml", code)
        self.assertIn("validate.yml", code)
        self.assertNotIn("website.yml", code)
        workflow = self.workflows_for([".github/workflows/controller.yml"])
        self.assertIn("controller.yml", workflow)
        self.assertIn("validate.yml", workflow)
        self.assertNotIn("website.yml", workflow)


class TestTrustedPublicationWorkflow(unittest.TestCase):
    def setUp(self):
        self.controller_text = (WORKFLOWS / "controller.yml").read_text()
        self.controller = yaml.safe_load(self.controller_text)

    def test_state_writer_is_control_repo_only_and_plain_fast_forward(self):
        reconcile = self.controller["jobs"]["reconcile"]
        self.assertEqual(reconcile["environment"], "state")
        steps = reconcile["steps"]
        token = next(step for step in steps if step.get("id") == "state-token")
        self.assertEqual(token["with"]["repositories"], "porting-agent")
        self.assertEqual(token["with"]["permission-contents"], "write")
        self.assertNotIn("permission-actions", token["with"])
        publish = next(
            step
            for step in steps
            if step.get("name") == "Publish one validated state-only commit on main"
        )
        self.assertIn("python -m state_commit", publish["run"])
        state_source = (ROOT / "state_commit.py").read_text()
        self.assertNotIn("--force", state_source)
        self.assertNotIn("force-with-lease", state_source)
        self.assertIn('f"{commit}:refs/heads/{branch}"', state_source)

    def test_control_jobs_have_no_model_or_destination_credential(self):
        all_workflows = "\n".join(
            path.read_text() for path in WORKFLOWS.iterdir() if path.is_file()
        )
        control_workflows = "\n".join(
            (WORKFLOWS / name).read_text()
            for name in ("controller.yml", "validate.yml", "website.yml")
        )
        self.assertNotIn(
            "${{ secrets.RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY }}", all_workflows
        )
        self.assertNotIn(
            "${{ vars.RISEPROJECT_UPSTREAMING_APP_CLIENT_ID }}", all_workflows
        )
        self.assertNotIn("ANTHROPIC_API_KEY", control_workflows)
        self.assertEqual(CONTROL_REPO, "riseproject-dev/porting-agent")

    def test_human_projections_follow_state_publication_and_are_retryable(self):
        steps = self.controller["jobs"]["reconcile"]["steps"]
        names = [step.get("name") for step in steps]
        publication = names.index("Publish one validated state-only commit on main")
        for projection in (
            "Mark command receipts complete",
            "Append lifecycle entries to tracking issues",
            "Explain rejected commands on their tracking issues",
        ):
            index = names.index(projection)
            self.assertGreater(index, publication)
            self.assertTrue(steps[index]["continue-on-error"])


class TestStatusSite(unittest.TestCase):
    def test_empty_tree_is_stable_and_project_page_is_rooted(self):
        rendered = generate_doc.render([])
        self.assertIn("# Porting status", rendered)
        self.assertIn("permalink: /", rendered)
        self.assertIn("| Project | Tracking issue |", rendered)
        self.assertNotIn("None", rendered)

    def test_generated_cells_escape_untrusted_markdown(self):
        state = contribution(updated_at=NOW)
        state["upstream"]["full_name"] = "example/proj|escape"
        rendered = generate_doc.render([state])
        self.assertIn("proj\\|escape", rendered)
        self.assertNotIn("proj|escape", rendered)

    def test_pages_uses_actions_deployment_without_contents_write(self):
        website = yaml.safe_load((WORKFLOWS / "website.yml").read_text())
        self.assertEqual(
            website["permissions"],
            {
                "actions": "read",
                "contents": "read",
                "pages": "write",
                "id-token": "write",
            },
        )
        deploy = website["jobs"]["deploy"]
        self.assertEqual(deploy["environment"]["name"], "github-pages")
        uses = [step.get("uses", "") for step in deploy["steps"]]
        self.assertEqual(
            sum(item.startswith("actions/deploy-pages@") for item in uses), 2
        )
        first = next(step for step in deploy["steps"] if step.get("id") == "deployment")
        retry = next(
            step for step in deploy["steps"] if step.get("id") == "deployment-retry"
        )
        self.assertTrue(first["continue-on-error"])
        self.assertEqual(retry["if"], "steps.deployment.outcome == 'failure'")
        discovery = next(
            step for step in deploy["steps"] if step.get("id") == "pages-artifact"
        )
        self.assertIn("pages_artifact.py discover", discovery["run"])
        self.assertIn("--attempts 6", discovery["run"])
        self.assertFalse(any("peaceiris" in item for item in uses))


if __name__ == "__main__":
    unittest.main()
