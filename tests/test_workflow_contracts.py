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

    def test_worker_fetches_and_verifies_complete_committed_dossier(self):
        worker = source("worker")
        checkout = next(
            step
            for step in worker["pre-agent-steps"]
            if step.get("name") == "Check out the contribution fork at the expected head (read-only)"
        )
        script = checkout["run"]
        self.assertEqual(checkout["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertIn(".state/evidence/", script)
        self.assertIn("application/vnd.github.raw+json", script)
        self.assertIn("sha256sum /tmp/gh-aw/agent/dossier.json", script)
        self.assertIn("dossier digest mismatch", script)
        self.assertIn('.kind == "dossier" and .evidence_id == $id', script)
        prompt = (WORKFLOWS / "worker.md").read_text()
        self.assertIn("identifies the latest verified dossier", prompt)
        self.assertIn("data` is only its queue-safe index", prompt)
        self.assertIn("read the latest verified dossier", prompt)

    def test_worker_brackets_untrusted_output_with_stop_commands(self):
        worker = source("worker")
        before = next(
            step
            for step in worker["pre-agent-steps"]
            if step.get("id") == "suspend_commands"
        )
        self.assertIn("openssl rand -hex 32", before["run"])
        self.assertIn('echo "token=$token" >> "$GITHUB_OUTPUT"', before["run"])
        self.assertIn("::stop-commands::%s", before["run"])
        after = worker["post-steps"][0]
        self.assertEqual(after["if"], "always()")
        self.assertEqual(
            after["env"]["STOP_COMMANDS_TOKEN"],
            "${{ steps.suspend_commands.outputs.token }}",
        )
        self.assertIn("printf '::%s::", after["run"])

    def test_prompts_require_rise_wheels_for_missing_build_and_test_dependencies(self):
        investigator = (WORKFLOWS / "investigator.md").read_text()
        worker = (WORKFLOWS / "worker.md").read_text()
        for text in (investigator, worker):
            self.assertIn("PIP_EXTRA_INDEX_URL=https://pypi.riseproject.dev/simple", text)
            self.assertIn("PIP_PREFER_BINARY=true", text)
            self.assertIn("public PyPI", text)
        self.assertIn("CIBW_ENVIRONMENT", worker)
        self.assertIn("CIBW_TEST_ENVIRONMENT", worker)
        skill = (ROOT / "skills/python-project-porting/SKILL.md").read_text()
        self.assertIn(
            "Do not source-build a dependency when a compatible RISE wheel exists",
            skill,
        )
        self.assertIn("PIP_PREFER_BINARY=true", skill)

    def test_worker_forbids_unrelated_fork_ci_debug_changes(self):
        worker = source("worker")
        github = worker["mcp-servers"]["github-authenticated"]
        self.assertEqual(github["env"]["GITHUB_READ_ONLY"], "1")
        self.assertTrue(github["required"])
        text = (WORKFLOWS / "worker.md").read_text()
        for required in (
            "do not add `push: riscv64`",
            "do not broaden conditions",
            "`inputs.upload`",
            "rejected/superseded approaches",
            "RISC-V GitHub Runners [1]",
            "credentialless",
            "configured external OIDC trust",
            "Do not change or",
            "disable `id-token: write`",
            "publication should fail out of the box",
            "add `riscv64`",
            "to that same list",
        ):
            self.assertIn(required, text)
        record = worker["jobs"]["record"]
        self.assertNotIn("environment", record)
        self.assertNotIn(
            "${{ secrets.RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY }}", str(worker)
        )

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
                "worker.md",
                "worker.lock.yml",
                "image.yml",
            },
        )
        text = "\n".join(path.read_text() for path in WORKFLOWS.iterdir() if path.is_file())
        self.assertNotIn("${{ secrets.RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY }}", text)
        self.assertNotIn("${{ vars.RISEPROJECT_UPSTREAMING_APP_CLIENT_ID }}", text)
        launches = set(re.findall(r"gh workflow run ([A-Za-z0-9_.-]+)", text))
        self.assertLessEqual(launches, {"controller.yml", "dispatcher.lock.yml"})
