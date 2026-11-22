# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Cumulative controller, native queue, and workflow security contracts."""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def source(name: str) -> dict:
    return yaml.safe_load(
        (WORKFLOWS / f"{name}.md").read_text().split("---", 2)[1]
    )


class TestWorkflowContracts(unittest.TestCase):
    def test_only_investigator_unsandboxed(self):
        investigator = source("investigator")
        self.assertFalse(investigator["strict"])
        self.assertFalse(investigator["sandbox"]["agent"])
        self.assertFalse(investigator["safe-outputs"]["threat-detection"])
        self.assertEqual(investigator["permissions"]["contents"], "read")
        self.assertNotIn("publish", investigator["jobs"])
        self.assertEqual(
            investigator["engine"]["extensions"],
            ["npm:pi-web-access@0.48.0", "npm:pi-subagents@0.89.0"],
        )
        github = investigator["mcp-servers"]["github-authenticated"]
        self.assertTrue(github["required"])
        self.assertEqual(
            github["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"], "${{ github.token }}"
        )
        self.assertEqual(github["env"]["GITHUB_READ_ONLY"], "1")
        self.assertIn("issue_read", github["allowed"])
        self.assertIn("pull_request_read", github["allowed"])
        graph = investigator["mcp-servers"]["project-graph"]
        self.assertTrue(graph["required"])
        self.assertEqual(graph["allowed"], ["project_graph_query"])
        dispatcher = source("dispatcher")
        self.assertTrue(dispatcher["strict"])
        self.assertNotEqual(dispatcher.get("sandbox", {}).get("agent"), False)

    def test_completeness_gate_precedes_durable_completion(self):
        lock = workflow("investigator.lock.yml")
        steps = lock["jobs"]["safe_outputs"]["steps"]
        names = [step.get("name") for step in steps]
        gate = names.index("Require complete research before native queue Completion")
        self.assertLess(gate, names.index("Reconcile work queue claim"))
        self.assertLess(gate, names.index("Process Safe Outputs"))
        self.assertNotIn("detection", lock["jobs"])
        notify = lock["jobs"]["notify"]
        self.assertEqual(set(notify["needs"]), {"safe_outputs", "record"})
        self.assertNotIn("issues", notify["permissions"])
        self.assertIn("python -m dossier_comment", str(notify))
        self.assertIn("porting-publication", str(notify))

    def test_dossier_comments_use_the_control_app_identity(self):
        notify = source("investigator")["jobs"]["notify"]
        self.assertNotIn("issues", notify["permissions"])
        token = next(step for step in notify["steps"] if step.get("id") == "comment-token")
        self.assertEqual(token["with"]["owner"], "riseproject-dev")
        self.assertEqual(token["with"]["repositories"], "porting-agent")
        self.assertEqual(token["with"]["permission-issues"], "write")
        publish = next(
            step for step in notify["steps"] if "dossier comment" in step.get("name", "")
        )
        self.assertEqual(
            publish["env"]["GH_TOKEN"], "${{ steps.comment-token.outputs.token }}"
        )
        self.assertEqual(publish["env"]["PORTING_COMMENT_AUTHOR"], "riseproject-dev[bot]")

    def test_investigation_retry_also_publishes_verified_comment(self):
        investigator = source("investigator")
        notify = investigator["jobs"]["notify"]
        self.assertEqual(set(notify["needs"]), {"safe_outputs", "record"})
        self.assertEqual(notify["if"], "needs.safe_outputs.outputs.work_queue_outcome == 'completed'")
        self.assertIn("porting-publication", str(notify))
        self.assertIn("dossier_comment", str(notify))
        self.assertIn("exact-Result marker makes retries idempotent",
                      (WORKFLOWS / "investigator.md").read_text())

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

    def test_current_milestone_has_no_destination_writer(self):
        self.assertEqual(
            {path.name for path in WORKFLOWS.iterdir() if path.is_file()},
            {
                "validate.yml",
                "controller.yml",
                "website.yml",
                "dispatcher.md",
                "dispatcher.lock.yml",
                "investigator.md",
                "investigator.lock.yml",
            },
        )
        text = "\n".join(path.read_text() for path in WORKFLOWS.iterdir() if path.is_file())
        self.assertNotIn("RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY", text)
        self.assertNotIn("RISEPROJECT_UPSTREAMING_APP_CLIENT_ID", text)
        launches = set(re.findall(r"gh workflow run ([A-Za-z0-9_.-]+)", text))
        self.assertLessEqual(launches, {"controller.yml", "dispatcher.lock.yml"})
