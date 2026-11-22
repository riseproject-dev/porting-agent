# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Milestone 3 controller workflow and projection security contracts."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


class TestWorkflowContracts(unittest.TestCase):
    def test_controller_pipeline_push_wakeup_excludes_state_commits(self):
        controller = workflow("controller.yml")
        trigger = controller.get("on", controller.get(True))["push"]
        self.assertEqual(trigger["branches"], ["main"])
        self.assertIn("*.py", trigger["paths"])
        self.assertIn(".github/workflows/**", trigger["paths"])
        self.assertNotIn(".state/**", trigger["paths"])
        self.assertIn("github.event_name == 'push'", controller["jobs"]["filter"]["if"])

    def test_controller_posts_append_only_app_authored_issue_ledger(self):
        controller = workflow("controller.yml")
        steps = controller["jobs"]["reconcile"]["steps"]
        token = next(step for step in steps if step.get("id") == "issue-token")
        self.assertEqual(token["with"]["owner"], "riseproject-dev")
        self.assertEqual(token["with"]["repositories"], "porting-agent")
        self.assertEqual(token["with"]["permission-issues"], "write")
        ledger = next(
            step
            for step in steps
            if step.get("name") == "Append lifecycle entries to tracking issues"
        )
        self.assertEqual(ledger["env"]["GH_TOKEN"], "${{ steps.issue-token.outputs.token }}")
        self.assertIn("python -m status_ledger", ledger["run"])

    def test_controller_explains_rejected_commands_with_issue_app_token(self):
        controller = workflow("controller.yml")
        steps = controller["jobs"]["reconcile"]["steps"]
        feedback = next(
            step
            for step in steps
            if step.get("name") == "Explain rejected commands on their tracking issues"
        )
        self.assertEqual(feedback["env"]["GH_TOKEN"], "${{ steps.issue-token.outputs.token }}")
        self.assertIn("python -m command_feedback", feedback["run"])

    def test_controller_reconciles_current_main_not_stale_event_sha(self):
        controller = workflow("controller.yml")
        for job_name in ("observe", "reconcile"):
            checkout = next(
                step
                for step in controller["jobs"][job_name]["steps"]
                if str(step.get("uses", "")).startswith("actions/checkout@")
            )
            self.assertEqual(checkout["with"]["ref"], "main")
            self.assertFalse(checkout["with"]["persist-credentials"])

    def test_command_acknowledgement_is_fast_idempotent_and_not_serialized(self):
        controller = workflow("controller.yml")
        self.assertNotIn("concurrency", controller)
        acknowledgement = controller["jobs"]["acknowledge-command"]
        self.assertNotIn("needs", acknowledgement)
        self.assertNotIn("concurrency", acknowledgement)
        self.assertEqual(
            acknowledgement["if"],
            "github.event_name == 'issue_comment' && github.event.issue.pull_request == null",
        )
        script = acknowledgement["steps"][-1]["run"]
        self.assertIn("awk 'NF { print; exit }'", script)
        self.assertIn("reactions?per_page=100", script)
        self.assertIn("riseproject-dev[bot]", script)
        self.assertIn("-f content='eyes'", script)
        reconcile = controller["jobs"]["reconcile"]
        self.assertEqual(reconcile["concurrency"]["group"], "porting-agent-controller")
        self.assertFalse(reconcile["concurrency"]["cancel-in-progress"])
        complete = next(
            step
            for step in reconcile["steps"]
            if step.get("name") == "Mark command receipts complete"
        )
        self.assertIn("python -m command_reactions", complete["run"])
        publish_index = next(
            index
            for index, step in enumerate(reconcile["steps"])
            if step.get("name") == "Publish one validated state-only commit on main"
        )
        self.assertGreater(reconcile["steps"].index(complete), publish_index)

    def test_canary_effect_uses_issue_token_then_state_token(self):
        steps = workflow("controller.yml")["jobs"]["reconcile"]["steps"]
        repair = next(
            step for step in steps if step.get("name") == "Repair the exact canonical canary request"
        )
        confirm = next(
            step for step in steps if step.get("name") == "Publish exact canary repair receipt state"
        )
        self.assertEqual(repair["env"]["GH_TOKEN"], "${{ steps.issue-token.outputs.token }}")
        self.assertIn("python -m canary_recovery", repair["run"])
        self.assertEqual(confirm["env"]["GH_TOKEN"], "${{ steps.state-token.outputs.token }}")
        self.assertIn("python -m state_commit", confirm["run"])
        self.assertLess(steps.index(repair), steps.index(confirm))

    def test_milestone_three_has_no_agent_or_destination_writer(self):
        self.assertEqual(
            {path.name for path in WORKFLOWS.iterdir()},
            {"validate.yml", "controller.yml", "website.yml"},
        )
        text = "\n".join(path.read_text() for path in WORKFLOWS.iterdir())
        self.assertNotIn("RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY", text)
        self.assertNotIn("RISEPROJECT_UPSTREAMING_APP_CLIENT_ID", text)
        self.assertNotIn("gh workflow run", text)
