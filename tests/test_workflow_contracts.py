# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Cumulative controller, native queue, and workflow security contracts."""

import re
import unittest
from pathlib import Path

import yaml

import reconcile

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def source(name: str) -> dict:
    return yaml.safe_load(
        (WORKFLOWS / f"{name}.md").read_text().split("---", 2)[1]
    )


def run_scripts(value):
    if isinstance(value, dict):
        if isinstance(value.get("run"), str):
            yield value["run"]
        for child in value.values():
            yield from run_scripts(child)
    elif isinstance(value, list):
        for child in value:
            yield from run_scripts(child)


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
        publish = worker["jobs"]["publish"]
        self.assertNotIn("environment", publish)
        for boundary in (worker["pre-agent-steps"], worker["jobs"]["safe_outputs"]):
            self.assertNotIn(
                "${{ secrets.RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY }}", str(boundary)
            )

    def test_development_pr_writer_is_restricted_to_one_verified_fork(self):
        workflow_data = workflow("dev-pr.yml")
        token = next(
            step for step in workflow_data["jobs"]["create"]["steps"]
            if step.get("id") == "fork-token"
        )
        self.assertEqual(token["with"]["permission-pull-requests"], "write")
        self.assertEqual(token["with"]["permission-contents"], "read")
        self.assertEqual(token["with"]["permission-actions"], "write")
        self.assertEqual(token["with"]["permission-administration"], "write")
        self.assertEqual(token["with"]["repositories"], "${{ steps.target.outputs.fork_name }}")

    def test_handoff_tokens_are_minimal_and_never_face_upstream(self):
        handoff = workflow("handoff.yml")
        self.assertEqual(handoff["permissions"], {"contents": "read"})
        self.assertEqual(handoff["concurrency"]["group"], "porting-fork-writer")
        jobs = handoff["jobs"]
        self.assertEqual(jobs["target"]["if"], "github.ref == 'refs/heads/main'")
        expected = {
            "debug-cleanup": ("riseproject-upstreaming", "${{ needs.target.outputs.fork_name }}",
                              {"permission-contents": "write", "permission-workflows": "write"}),
            "internal-pr-closure": ("riseproject-upstreaming",
                                    "${{ needs.target.outputs.fork_name }}",
                                    {"permission-pull-requests": "write"}),
            "submission-comment": ("riseproject-dev", "porting-agent",
                                   {"permission-issues": "write"}),
        }
        for name, (owner, repositories, permissions) in expected.items():
            with self.subTest(job=name):
                job = jobs[name]
                self.assertEqual(job["needs"], ["target"])
                self.assertIn("needs.target.outputs.kind ==", job["if"])
                self.assertNotIn("permissions", job)
                tokens = [step for step in job["steps"]
                          if "create-github-app-token" in str(step.get("uses"))]
                self.assertEqual(len(tokens), 1)
                given = tokens[0]["with"]
                self.assertEqual((given["owner"], given["repositories"]), (owner, repositories))
                self.assertEqual({k: v for k, v in given.items() if k.startswith("permission-")},
                                 permissions)
                for step in job["steps"]:
                    if step.get("uses"):
                        self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
                runs = [step["run"] for step in job["steps"] if "run" in step]
                self.assertTrue(all("python -m handoff" in run for run in runs))
                # The job checks out the head of main and checks it, and the
                # operation, before it mints its token.
                steps = job["steps"]
                checkout = next(s for s in steps if "actions/checkout" in str(s.get("uses")))
                self.assertEqual(checkout["with"]["ref"], "main")
                check = next(i for i, s in enumerate(steps) if "python -m handoff check" in str(s.get("run")))
                mint = next(i for i, s in enumerate(steps) if "create-github-app-token" in str(s.get("uses")))
                self.assertLess(check, mint)
                self.assertIn(f"--kind {job['if'].split(chr(39))[1]}", steps[check]["run"])
        self.assertEqual(next(s for s in jobs["target"]["steps"]
                              if "actions/checkout" in str(s.get("uses")))["with"]["ref"], "main")
        # Only the target job reads the operation before a token exists, and
        # no job holds a token for any repository outside the fork and the
        # control repository: nothing can write upstream.
        text = (WORKFLOWS / "handoff.yml").read_text()
        self.assertNotIn("closeio", text)
        self.assertEqual(text.count("RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY"), 2)
        self.assertNotIn("permission-administration", text)
        continuation = jobs["continuation"]
        self.assertEqual(continuation["if"], "always()")
        self.assertIn("gh workflow run controller.yml", str(continuation))

    def test_controller_collects_handoff_receipts_from_main_only(self):
        text = (WORKFLOWS / "controller.yml").read_text()
        self.assertIn("handoff.yml/runs?status=completed&branch=main&event=workflow_dispatch",
                      text)
        self.assertIn('name == "porting-handoff"', text)
        self.assertIn("--handoffs handoffs", text)
        self.assertIn("jq -r '.handoff_requests[].operation_id' report.json", text)

    def test_provisioning_administration_is_restricted_to_one_verified_fork(self):
        provision = workflow("provision.yml")
        token = next(
            step for step in provision["jobs"]["provision"]["steps"]
            if step.get("id") == "fork-token"
        )
        self.assertEqual(token["with"]["permission-administration"], "write")
        self.assertEqual(token["with"]["repositories"], "${{ steps.create.outputs.fork_name }}")
        self.assertEqual(token["with"]["owner"], "riseproject-upstreaming")

    def test_native_workers_explicitly_dispatch_controller_after_terminal_jobs(self):
        expectations = {
            "worker": {"safe_outputs", "publish"},
            "investigator": {"safe_outputs", "record", "notify"},
            "dispatcher": {"safe_outputs"},
        }
        for profile, needs in expectations.items():
            continuation = source(profile)["jobs"]["continuation"]
            self.assertEqual(set(continuation["needs"]), needs)
            self.assertEqual(continuation["if"], "always()")
            token = next(
                step for step in continuation["steps"] if step.get("id") == "control-token"
            )
            self.assertEqual(token["with"]["owner"], "riseproject-dev")
            self.assertEqual(token["with"]["repositories"], "porting-agent")
            self.assertEqual(token["with"]["permission-contents"], "read")
            self.assertEqual(token["with"]["permission-actions"], "write")
            self.assertIn("gh workflow run controller.yml", str(continuation))

    def test_destination_tokens_use_only_destination_app_credentials(self):
        paths = [
            WORKFLOWS / "provision.yml",
            WORKFLOWS / "dev-pr.yml",
            WORKFLOWS / "publish-retry.yml",
            WORKFLOWS / "worker.md",
            WORKFLOWS / "handoff.yml",
        ]
        for path in paths:
            text = path.read_text()
            destination_blocks = re.findall(
                r"client-id: .*?\n\s+private-key: .*?\n\s+owner: riseproject-upstreaming",
                text,
            )
            self.assertTrue(destination_blocks, path.name)
            for block in destination_blocks:
                self.assertIn("RISEPROJECT_UPSTREAMING_APP_CLIENT_ID", block)
                self.assertIn("RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY", block)
                self.assertNotIn("vars.RISEPROJECT_APP_CLIENT_ID", block)

    def test_controller_uses_publication_artifact_not_overall_run_status(self):
        text = (WORKFLOWS / "controller.yml").read_text()
        self.assertIn("actions/workflows/$workflow/runs?per_page=20", text)
        self.assertNotIn("actions/workflows/$workflow/runs?status=completed", text)
        self.assertIn('name == "porting-publication"', text)

    def test_controller_chains_every_pipeline_completion_and_has_schedule_fallback(self):
        controller = workflow("controller.yml")
        triggers = controller.get("on", controller.get(True))
        self.assertEqual(triggers["schedule"], [{"cron": "*/5 * * * *"}])
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
                "Handoff",
            },
        )
        self.assertIn("workflow_run", controller["jobs"]["filter"]["if"])
        for name in ("provision.yml", "dev-pr.yml", "publish-retry.yml", "handoff.yml"):
            continuation = workflow(name)["jobs"]["continuation"]
            self.assertEqual(continuation["if"], "always()")
            self.assertIn("gh workflow run controller.yml", str(continuation))

    def test_development_writers_do_not_wait_for_environment_review(self):
        worker = source("worker")
        self.assertNotIn("environment", worker["jobs"]["publish"])
        for name in ("provision.yml", "publish-retry.yml", "dev-pr.yml", "handoff.yml"):
            data = workflow(name)
            for job in data["jobs"].values():
                self.assertNotIn("environment", job, name)

    def test_authored_python_ci_invocations_use_uv(self):
        paths = [
            *(
                path for path in WORKFLOWS.glob("*.yml")
                if not path.name.endswith(".lock.yml")
            ),
            *WORKFLOWS.glob("*.md"),
        ]
        for path in paths:
            text = path.read_text()
            data = yaml.safe_load(
                text.split("---", 2)[1] if path.suffix == ".md" else text
            )
            for script in run_scripts(data):
                joined = script.replace("\\\n", " ")
                direct = re.search(r"(?:^|[\n;&|])\s*python(?:3)?(?:\s|$)", joined)
                if direct is not None:
                    self.assertEqual(path.name, "worker.md")
                    self.assertEqual(
                        script,
                        'python3 scripts/pi_provider_credential.py "$RUNNER_TEMP/gh-aw/actions"',
                    )

    def test_controller_pipeline_push_wakeup_excludes_state_commits(self):
        controller = workflow("controller.yml")
        trigger = controller.get("on", controller.get(True))["push"]
        self.assertEqual(trigger["branches"], ["main"])
        self.assertIn("*.py", trigger["paths"])
        self.assertIn(".github/workflows/**", trigger["paths"])
        self.assertNotIn(".state/**", trigger["paths"])
        self.assertIn("github.event_name == 'push'", controller["jobs"]["filter"]["if"])

    def test_migration_only_report_safely_noops_all_controller_jq_consumers(self):
        report = reconcile.state_text_migration_report([".state/evidence/example.json"])
        steps = workflow("controller.yml")["jobs"]["reconcile"]["steps"]
        consumers = {
            "Repair the exact canonical canary request": [
                (".controller.canary_recovery_requests", report["controller"]["canary_recovery_requests"]),
            ],
            "Launch dispatcher for durable selected Work": [
                (".requested_effects", report["requested_effects"]),
                (".dispatch_relaunch", report["dispatch_relaunch"]),
            ],
            "Launch durable fork provisioning operations": [
                (".provision_requests[]", report["provision_requests"]),
            ],
            "Launch due publication retries": [
                (".publication_retries[]", report["publication_retries"]),
            ],
            "Launch durable development PR operations": [
                (".dev_pr_requests[]", report["dev_pr_requests"]),
            ],
        }
        by_name = {step.get("name"): step for step in steps}
        # No patch is proposed before the fork exists, so nothing publishes one.
        self.assertFalse(any("proposal_publications" in (step.get("run") or "")
                             for step in steps))
        for name, fields in consumers.items():
            script = by_name[name]["run"]
            self.assertIn("report.json", script)
            for jq_path, value in fields:
                self.assertIn(jq_path, script)
                self.assertEqual(value, [], f"{name}: {jq_path}")

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

    def test_current_milestone_has_only_reviewed_destination_writers(self):
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
                "provision.yml",
                "dev-pr.yml",
                "publish-retry.yml",
                "handoff.yml",
            },
        )
        text = "\n".join(path.read_text() for path in WORKFLOWS.iterdir() if path.is_file())
        non_destination = "\n".join(
            (WORKFLOWS / name).read_text()
            for name in (
                "controller.yml", "dispatcher.md", "investigator.md", "image.yml",
                "validate.yml", "website.yml",
            )
        )
        self.assertNotIn(
            "${{ secrets.RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY }}", non_destination
        )
        launches = set(re.findall(r"gh workflow run ([A-Za-z0-9_.-]+)", text))
        self.assertLessEqual(
            launches,
            {
                "controller.yml", "dispatcher.lock.yml", "provision.yml",
                "dev-pr.yml", "publish-retry.yml", "handoff.yml",
            },
        )
