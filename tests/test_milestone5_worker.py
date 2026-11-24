# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Milestone 5 image, uncredentialed worker, and proposal gate contracts."""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import yaml

import capacity
import controller
import reconcile
import worker_gate
from tests.helpers import contribution, policy

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
IMAGE = "ghcr.io/riseproject-dev/porting-agent/ubuntu-24.04-riscv"


def source(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / f"{name}.md").read_text().split("---", 2)[1])


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def mbox(path=".github/workflows/build-wheels.yml", subject="add riscv64") -> bytes:
    return (
        "From " + "a" * 40 + " Mon Sep 17 00:00:00 2001\n"
        f"Subject: [PATCH] {subject}\n\n---\n"
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-old\n+new\n"
    ).encode()


def task():
    return {
        "task": "development_patch",
        "contribution_id": "closeio--ciso8601/1",
        "revision": 3,
        "expected_head": None,
        "ref": "riscv64",
        "allowed_operations": ["append", "rewrite"],
        "allowed_paths": [
            ".github/workflows/build-wheels.yml",
            "pyproject.toml",
            "CHANGELOG.md",
        ],
        "upstream_facing": False,
    }


def proposal(patch: bytes, path=".github/workflows/build-wheels.yml"):
    return {
        "schema_version": 1,
        "contribution_id": "closeio--ciso8601/1",
        "outcome": "patch",
        "expected_revision": 3,
        "expected_head": None,
        "reason_ids": ["plan-closeio--ciso8601--1-r2"],
        "patch_digest": hashlib.sha256(patch).hexdigest(),
        "changed_files": [path],
        "operation": "append",
        "diagnosis": "Add the bounded riscv64 wheel row after exact reproduction.",
        "validation_performed": ["direct riscv64 build and tests in the image"],
        "validation_not_performed": [],
        "progress": "acceptance_row_satisfied",
        "ref": "riscv64",
        "reconstruction_base": None,
        "reproduction": {
            "image": IMAGE + "@sha256:" + "b" * 64,
            "architecture": "riscv64",
            "commands": [
                {
                    "command": "docker run --rm --platform linux/riscv64 " + IMAGE + "@sha256:"
                    + "b" * 64 + " sh -c 'uname -m && make test'",
                    "exit_status": 0,
                    "log_sha256": "c" * 64,
                },
                {
                    "command": "gh act -j build -P ubuntu-24.04-riscv=<image>",
                    "exit_status": 0,
                    "log_sha256": "c" * 64,
                },
            ],
            "workflows": [
                {
                    "workflow": ".github/workflows/build-wheels.yml",
                    "jobs": ["build", "test"],
                    "runner_mappings": ["ubuntu-24.04-riscv=<image>"],
                }
            ],
            "limits": {
                "wall_seconds": 3600,
                "memory_bytes": 8589934592,
                "pids": 2048,
                "artifact_bytes": len(patch),
                "log_bytes": 4096,
            },
        },
    }


class TestImageContract(unittest.TestCase):
    def test_image_build_is_exact_base_digest_and_trusted_publish_only(self):
        image = workflow("image.yml")
        self.assertEqual(image["env"]["IMAGE"], IMAGE)
        build = image["jobs"]["build-image"]
        self.assertEqual(build["permissions"], {"contents": "read", "packages": "write"})
        steps = build["steps"]
        qemu = next(step for step in steps if step.get("name") == "Setup QEMU")
        self.assertEqual(
            qemu["uses"],
            "docker/setup-qemu-action@99012661954931238ded8c8b007157a8430204e1",
        )
        base = next(step for step in steps if step.get("id") == "base")
        self.assertIn('.platform.os == "linux"', base["run"])
        self.assertIn('.platform.architecture == "riscv64"', base["run"])
        self.assertIn("expected one linux/riscv64 base manifest", base["run"])
        build_step = next(step for step in steps if step.get("id") == "build")
        self.assertEqual(build_step["with"]["platforms"], "linux/riscv64")
        self.assertIn("BASE=${{ steps.base.outputs.ref }}", build_step["with"]["build-args"])
        self.assertEqual(build_step["with"]["push"], "${{ needs.setup.outputs.push-images == 'true' }}")
        verify = next(
            step
            for step in steps
            if step.get("name") == "Verify anonymous exact-digest pull and riscv64 execution"
        )
        self.assertIn("docker logout ghcr.io", verify["run"])
        self.assertIn('ref="$IMAGE@$DIGEST"', verify["run"])
        self.assertIn('docker pull --platform linux/riscv64 "$ref"', verify["run"])
        self.assertIn(
            'docker image inspect --platform linux/riscv64 "$ref"', verify["run"]
        )
        self.assertIn("uname -m", verify["run"])

    def test_no_riscv64_only_image_path_uses_host_default_pull_or_inspection(self):
        sources = {
            "image workflow": (WORKFLOWS / "image.yml").read_text(),
            "worker": (WORKFLOWS / "worker.md").read_text(),
        }
        for source_name, text in sources.items():
            pull_lines = [line.strip() for line in text.splitlines() if "docker pull " in line]
            inspect_lines = [
                line.strip() for line in text.splitlines() if "docker image inspect " in line
            ]
            self.assertTrue(pull_lines, source_name)
            self.assertTrue(inspect_lines, source_name)
            for line in pull_lines + inspect_lines:
                with self.subTest(source=source_name, line=line):
                    self.assertIn("--platform linux/riscv64", line)
        host_default_examples = ('docker pull "$ref"', 'docker image inspect "$IMAGE"')
        for example in host_default_examples:
            self.assertTrue(any(example not in text for text in sources.values()))
            self.assertFalse(any(example in text for text in sources.values()))

    def test_image_context_removes_runner_credentials_and_bounds_local_reproduction(self):
        dockerfile = (ROOT / "image/Dockerfile").read_text()
        entrypoint = (ROOT / "image/entrypoint.sh").read_text()
        launcher = (ROOT / "image/run.sh").read_text()
        self.assertIn("rm -f /home/runner/.credentials /home/runner/.runner", dockerfile)
        self.assertIn("PORTING_REPRO=1", dockerfile)
        self.assertNotIn("FROM --platform=linux/riscv64", dockerfile)
        self.assertNotIn(
            "FROM --platform=linux/riscv64",
            (ROOT / "image/Dockerfile.filtered").read_text(),
        )
        for credential in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "ACTIONS_RUNTIME_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
            "RISEPROJECT_APP_PRIVATE_KEY",
            "RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY",
            "ANTHROPIC_API_KEY",
        ):
            self.assertIn(credential, entrypoint)
        self.assertIn("PORTING_DISPOSABLE_HOST", launcher)
        self.assertIn("^sha256:[0-9a-f]{64}$", launcher)
        self.assertIn("REPRO_TIMEOUT_SECONDS", launcher)
        self.assertIn("--memory", launcher)
        self.assertIn("--pids-limit", launcher)


class TestAutomaticProposalAdmission(unittest.TestCase):
    def test_no_patch_work_is_selected_before_the_fork_exists(self):
        """A record of an earlier controller that waits for its planned fork
        intent is migrated to `provisioning.idle`; the intent is launched by
        the table and no patch Work is requested before the fork."""
        record = json.loads(
            (ROOT / "tests/fixtures/canary-m5-provisioning-state.json").read_text()
        )
        operation = json.loads(
            (ROOT / "tests/fixtures/canary-m5-provisioning-operation.json").read_text()
        )
        recovery_operation = json.loads(
            (ROOT / "tests/fixtures/canary-recovery-operation.json").read_text()
        )
        project = json.loads(
            (ROOT / "tests/fixtures/canary-m5-project.json").read_text()
        )
        issue = {
            "id": record["tracking_issue"]["id"],
            "number": record["tracking_issue"]["number"],
            "title": "[upstreaming] ciso8601 riscv64 wheel support",
            "body": record["tracking_issue"]["description"],
            "labels": [{"name": "upstreaming-request"}],
        }
        result = controller.tick(
            contributions={"c": record},
            operations={
                operation["operation_id"]: operation,
                recovery_operation["operation_id"]: recovery_operation,
            },
            consumed={},
            projects={project["project_id"]: project},
            policy=policy(read_only=False),
            inputs=controller.Inputs(
                now=datetime.fromisoformat("2026-11-22T03:00:00+00:00"),
                pressure=capacity.Pressure(ok=True),
                tracking_issues={1: issue},
            ),
        )
        launched = next(
            item for kind, item in result.staged
            if kind == "operations" and item["operation_id"] == operation["operation_id"]
        )
        final = [item for kind, item in result.staged if kind == "contributions"][-1]
        self.assertEqual(launched["status"], "planned")
        self.assertEqual(result.report["provision_requests"][0]["operation_id"],
                         operation["operation_id"])
        self.assertEqual(result.report["selected"], [])
        self.assertEqual(result.requested_effects, [])
        self.assertEqual((final["phase"], final["activity"]), ("provisioning", "idle"))


class TestWorkerContract(unittest.TestCase):
    def test_worker_agent_is_uncredentialed_and_trusted_publish_is_separate(self):
        worker = source("worker")
        self.assertFalse(worker["strict"])
        self.assertFalse(worker["sandbox"]["agent"])
        self.assertFalse(worker["safe-outputs"]["threat-detection"])
        self.assertEqual(
            worker["permissions"],
            {
                "contents": "read",
                "issues": "read",
                "pull-requests": "read",
                "actions": "read",
            },
        )
        self.assertEqual(set(worker["jobs"]), {"safe_outputs", "publish", "continuation"})
        for boundary in (worker["pre-agent-steps"], worker["jobs"]["safe_outputs"]):
            text = str(boundary)
            self.assertNotIn("${{ secrets.RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY }}", text)
            self.assertNotIn("${{ vars.RISEPROJECT_UPSTREAMING_APP_CLIENT_ID }}", text)
        publish = worker["jobs"]["publish"]
        self.assertNotIn("environment", publish)
        token = next(step for step in publish["steps"] if step.get("id") == "fork-token")
        self.assertEqual(token["with"]["repositories"], "${{ steps.result.outputs.fork_name }}")
        wrapper = next(
            step for step in worker["pre-agent-steps"]
            if "credential-free unsandboxed engine wrapper" in step.get("name", "")
        )["run"]
        self.assertIn("credential remained in model environment", wrapper)
        self.assertIn("exec \"$real\"", wrapper)
        lock = (WORKFLOWS / "worker.lock.yml").read_text()
        self.assertIn("GH_AW_PI_COMMAND: /tmp/gh-aw/agent/pi-scrubbed", lock)
        self.assertNotIn("awf --config", lock)
        self.assertIn("docker pull --platform linux/riscv64", lock)
        continuation = worker["jobs"]["continuation"]
        self.assertEqual(set(continuation["needs"]), {"safe_outputs", "publish"})
        self.assertEqual(continuation["if"], "always()")
        self.assertIn("gh workflow run controller.yml", str(continuation))

    def test_qemu_image_gh_act_and_stop_commands_precede_agent(self):
        worker = source("worker")
        steps = worker["pre-agent-steps"]
        self.assertTrue(steps[0]["uses"].startswith("docker/setup-qemu-action@"))
        setup = steps[1]["run"]
        self.assertIn(
            'docker pull --platform linux/riscv64 "$IMAGE"', setup
        )
        self.assertIn("set -euo pipefail", setup)
        self.assertNotIn("gh-act", setup)
        self.assertIn(
            'docker image inspect --platform linux/riscv64 "$IMAGE"', setup
        )
        self.assertIn("for attempt in 1 2 3 4 5 6 7 8 9 10", setup)
        self.assertIn("image pull failed after bounded retry", setup)
        self.assertIn("--platform linux/riscv64", setup)
        # The pinned gh act install is best effort: it never fails the run.
        act = steps[2]["run"]
        self.assertIn("gh release download v0.2.98", act)
        self.assertIn("34849b32013cb8ac63a1e74015e63602a3374ba3b43df952525739ad15abed7f", act)
        self.assertIn("gh-act.real", act)
        self.assertIn("unset GITHUB_TOKEN GH_TOKEN", act)
        self.assertIn("gh act --version", act)
        self.assertIn("gh-act-status", act)
        self.assertIn("printf unavailable", act)
        self.assertNotIn("set -e", act)
        self.assertNotIn("exit 1", act)
        suspend = next(step for step in steps if step.get("id") == "suspend_commands")
        self.assertIn("::stop-commands::%s", suspend["run"])
        self.assertEqual(worker["post-steps"][0]["if"], "always()")
        prompt = (WORKFLOWS / "worker.md").read_text()
        self.assertIn("## Mandatory riscv64 reproduction", prompt)
        self.assertIn("Select only plan-relevant build and test workflows/jobs", prompt)
        self.assertIn("Pass no secret", prompt)
        self.assertIn("is\noptional", prompt)
        self.assertIn("gh-act-status", prompt)
        self.assertIn("never a reason for a blocked\noutcome", prompt)

    def test_dispatcher_compiled_policy_contains_only_current_profiles(self):
        lock = workflow("dispatcher.lock.yml")
        policies = []
        for job in lock["jobs"].values():
            for step in job.get("steps", []):
                raw = (step.get("env") or {}).get("GH_AW_WORK_QUEUE_POLICY")
                if raw:
                    policies.append(json.loads(raw))
        self.assertTrue(policies)
        for queue_policy in policies:
            profiles = queue_policy["pools"]["default"]["profiles"]
            self.assertEqual(set(profiles), {"planner", "worker", "reviewer"})
            self.assertEqual(profiles["reviewer"]["workflow"],
                             ".github/workflows/reviewer.lock.yml")
            self.assertEqual(profiles["worker"]["workflow"], ".github/workflows/worker.lock.yml")
            self.assertEqual(profiles["worker"]["max_claims"], 1)


class TestWorkerFailureClassification(unittest.TestCase):
    def test_reproduction_failures_remain_distinct(self):
        self.assertEqual(capacity.classify_failure("image pull failed: manifest unknown"),
                         "image_unavailable")
        self.assertEqual(capacity.classify_failure("qemu missing: exec format error"), "qemu")
        self.assertEqual(capacity.classify_failure("gh act does not support this command"),
                         "act_unsupported")
        self.assertEqual(capacity.classify_failure("model gateway unavailable"),
                         "model_infrastructure")
        self.assertEqual(capacity.classify_failure("pytest failed"), "code")

    def test_a_gh_act_failure_is_advisory(self):
        self.assertEqual(capacity.ADVISORY_FAILURE_CLASSES, {"act_unsupported"})
        self.assertFalse(capacity.blocks_outcome("act_unsupported"))
        for failure_class in ("image_unavailable", "qemu", "code", "dependency"):
            self.assertTrue(capacity.blocks_outcome(failure_class), failure_class)


class TestProposalOnlyReconciliation(unittest.TestCase):
    def test_a_proposal_without_a_fork_write_is_rejected(self):
        """No patch is held without a write to the provisioned fork: an
        outcome ``proposal`` is rejected in trusted validation, at a new
        revision, and nothing is held for publication."""
        record = contribution(
            phase="development",
            activity="agent_running",
            active_work={"task": "development_patch", "revision": 3},
            revision=3,
        )
        outcome = {
            "status": "proposal",
            "failure_class": None,
            "artifact": {
                "work_id": "w",
                "claim_id": "c",
                "artifact_id": 7,
                "name": "claim-x-porting-out",
                "digest": "sha256:" + "a" * 64,
                "run_id": "9",
                "result_commit": "q_" + "b" * 64,
            },
        }
        updated, staged = reconcile.apply_publication(
            record,
            outcome,
            now="2026-11-22T03:00:00+00:00",
            policy=policy(read_only=False),
            worker_run_id="9",
        )
        self.assertIsNone(updated["active_work"])
        self.assertEqual((updated["activity"], updated["revision"]), ("fix_needed", 4))
        # `reject_patch_proposal` sets no proposal artifact: only a published
        # Result is recorded there.
        self.assertIsNone(updated["proposal_artifact"])
        self.assertIsNone(updated["pending_publication"])
        self.assertEqual([r["kind"] for kind, r in staged if kind == "events"],
                         ["reject_patch_proposal"])
        self.assertFalse(any(kind == "operations" for kind, _record in staged))


class TestWorkerGate(unittest.TestCase):
    def test_accepts_exact_bounded_patch_and_reproduction(self):
        patch = mbox()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "patches.mbox"
            path.write_bytes(patch)
            report = worker_gate.validate(task(), proposal(patch), path)
        self.assertEqual(report["status"], "accepted")
        self.assertEqual(report["changed_files"], [".github/workflows/build-wheels.yml"])
        self.assertTrue(report["image"].startswith(IMAGE + "@sha256:"))

    def test_scope_digest_debug_and_workflow_injection_fail_closed(self):
        cases = []
        outside = mbox("setup.py")
        cases.append((task(), proposal(outside, "setup.py"), outside, "outside approved scope"))
        mismatch = mbox("pyproject.toml")
        cases.append((task(), proposal(mismatch), mismatch, "mbox paths"))
        debug = mbox(subject="{debug} enable fork")
        upstream_task = task() | {"upstream_facing": True}
        cases.append((upstream_task, proposal(debug), debug, "debug commit"))
        injected = mbox()
        injected = injected.replace(b"+new", b"+pull_request_target: {}")
        cases.append((task(), proposal(injected), injected, "forbidden workflow"))
        for task_value, proposal_value, patch, error in cases:
            proposal_value["patch_digest"] = hashlib.sha256(patch).hexdigest()
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "patches.mbox"
                path.write_bytes(patch)
                with self.assertRaisesRegex(worker_gate.WorkerGateError, error):
                    worker_gate.validate(task_value, proposal_value, path)

    def test_reproduction_digest_architecture_and_evidence_are_required(self):
        patch = mbox()
        for mutate, error in (
            (lambda value: value["reproduction"].update(image=IMAGE + ":latest"), "exact-digest"),
            (lambda value: value["reproduction"].update(architecture="x86_64"), "schema"),
            (lambda value: value["reproduction"].update(commands=[]), "schema"),
        ):
            candidate = proposal(patch)
            mutate(candidate)
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "patches.mbox"
                path.write_bytes(patch)
                with self.assertRaises((worker_gate.WorkerGateError, ValueError)):
                    worker_gate.validate(task(), candidate, path)

    def _validate(self, candidate, patch=None):
        patch = mbox() if patch is None else patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "patches.mbox"
            path.write_bytes(patch)
            return worker_gate.validate(task(), candidate, path)

    def _no_change(self, candidate):
        candidate.update(outcome="no_change", patch_digest=None, changed_files=[],
                         operation="none")
        return candidate

    def test_reproduction_without_gh_act_is_accepted(self):
        for drop in (True, False):
            candidate = proposal(mbox())
            candidate["reproduction"]["commands"] = candidate["reproduction"]["commands"][:1]
            if drop:
                del candidate["reproduction"]["workflows"]
            else:
                candidate["reproduction"]["workflows"] = []
            with self.subTest(dropped=drop):
                report = self._validate(candidate)
                self.assertEqual(report["status"], "accepted")
                self.assertEqual(report["workflows"], 0)
                self.assertGreaterEqual(report["direct_commands"], 1)

    def test_failed_gh_act_does_not_block_passing_direct_commands(self):
        for outcome in ("patch", "no_change"):
            candidate = proposal(mbox())
            candidate["reproduction"]["commands"][1]["exit_status"] = 1
            patch = mbox()
            if outcome == "no_change":
                self._no_change(candidate)
                patch = b""
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "patches.mbox"
                if patch:
                    path.write_bytes(patch)
                report = worker_gate.validate(task(), candidate, path)
                self.assertEqual(report["status"], "accepted")

    def test_accepted_outcome_requires_a_passing_direct_image_command(self):
        exact = IMAGE + "@sha256:" + "b" * 64
        for name, commands in (
            ("gh act only", ["gh act -j build -P ubuntu-24.04-riscv=" + exact]),
            ("direct failed", ["docker run --rm --platform linux/riscv64 " + exact + " make test"]),
            ("no platform", ["docker run --rm " + exact + " make test"]),
            ("other digest", ["docker run --rm --platform linux/riscv64 " + IMAGE + "@sha256:"
                              + "d" * 64 + " make test"]),
        ):
            candidate = proposal(mbox())
            candidate["reproduction"]["commands"] = [
                {"command": command, "exit_status": 1 if name == "direct failed" else 0,
                 "log_sha256": "c" * 64} for command in commands]
            with self.subTest(case=name):
                with self.assertRaisesRegex(worker_gate.WorkerGateError, "passing direct command"):
                    self._validate(candidate)

    def test_recorded_gh_act_evidence_must_be_complete(self):
        for name, item in (
            ("no mappings", {"workflow": "ci.yml", "jobs": ["build"]}),
            ("empty jobs", {"workflow": "ci.yml", "jobs": [], "runner_mappings": ["a=b"]}),
            ("empty mappings", {"workflow": "ci.yml", "jobs": ["build"], "runner_mappings": []}),
        ):
            candidate = proposal(mbox())
            candidate["reproduction"]["workflows"] = [item]
            with self.subTest(case=name):
                with self.assertRaises((worker_gate.WorkerGateError, ValueError)):
                    self._validate(candidate)

    def _blocked(self, commands):
        candidate = self._no_change(proposal(mbox()))
        candidate.update(outcome="blocked",
                         failure_cluster="reproduction-image-infrastructure-gh-act")
        candidate["reproduction"]["commands"] = [
            {"command": command, "exit_status": status, "log_sha256": "c" * 64}
            for command, status in commands]
        with tempfile.TemporaryDirectory() as directory:
            return worker_gate.validate(task(), candidate, Path(directory) / "patches.mbox")

    def test_gh_act_failure_alone_is_not_an_infrastructure_blocker(self):
        exact = IMAGE + "@sha256:" + "b" * 64
        direct = "docker run --rm --platform linux/riscv64 " + exact + " make test"
        with self.assertRaisesRegex(worker_gate.WorkerGateError, "gh act failure alone"):
            self._blocked([(direct, 0), ("timeout 600 gh act -j build -P x=" + exact, 1)])
        report = self._blocked([("docker info", 1), (direct, 1),
                                ("gh act -j build -P x=" + exact, 1)])
        self.assertEqual(report["status"], "infrastructure_blocked")

    def test_stored_evidence_with_runner_mappings_stays_valid(self):
        import schemas
        fixtures = ROOT / "tests/fixtures"
        stored = {name: json.loads((fixtures / name / "proposal.json").read_text())
                  for name in ("worker-docker-blocked", "worker-no-change-rejected")}
        for name, value in stored.items():
            with self.subTest(fixture=name):
                schemas.validate("proposal", value)
                self.assertTrue(value["reproduction"]["workflows"][0]["runner_mappings"])
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "patches.mbox"
            no_change = stored["worker-no-change-rejected"]
            accepted = worker_gate.validate(
                json.loads((fixtures / "worker-no-change-rejected/task.json").read_text()),
                no_change, empty)
            self.assertEqual(accepted["status"], "accepted")
            blocked = worker_gate.validate(
                json.loads((fixtures / "worker-docker-blocked/task.json").read_text()),
                stored["worker-docker-blocked"], empty)
            self.assertEqual(blocked["status"], "infrastructure_blocked")
        record = contribution(phase="development", activity="agent_running", revision=3)
        evidence = reconcile._rehearsal_evidence(
            record, {"proposal": stored["worker-docker-blocked"], "reason": "docker unavailable",
                     "failure_class": "infrastructure", "artifact": {"digest": "sha256:" + "e" * 64}},
            "44982902579", policy(), "2026-11-22T08:55:56+00:00")
        schemas.validate("evidence", evidence)
        self.assertEqual(evidence["data"]["reproduction"]["workflows"][0]["runner_mappings"],
                         stored["worker-docker-blocked"]["reproduction"]["workflows"][0]["runner_mappings"])


if __name__ == "__main__":
    unittest.main()
