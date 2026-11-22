# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Milestone 2 root-module, CLI, golden, and deferral contracts."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import state
from common import canonical_json
from tests.helpers import contribution

ROOT = Path(__file__).parents[1]
GOLDEN = json.loads((ROOT / "tests/fixtures/cli-golden.json").read_text())
PYTHON = sys.executable


def run_module(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return subprocess.run(
        [PYTHON, "-m", *args],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def normalized_without_at(value: dict) -> dict:
    value = json.loads(json.dumps(value))
    value.pop("at", None)
    if isinstance(value.get("controller"), dict):
        value["controller"].pop("at", None)
    return value


class TestRootModuleMigration(unittest.TestCase):
    def test_import_every_root_module_without_custom_pythonpath(self):
        modules = sorted(path.stem for path in ROOT.glob("*.py"))
        self.assertGreaterEqual(len(modules), 30)
        command = "import " + ", ".join(modules)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [PYTHON, "-c", command],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_removed_package_and_aggregate_are_not_reintroduced(self):
        self.assertFalse((ROOT / "upstreaming").exists())
        self.assertFalse((ROOT / "src").exists())
        self.assertFalse((ROOT / "porting_agent.py").exists())
        self.assertFalse((ROOT / "__main__.py").exists())
        for path in [*ROOT.glob("*.py"), *ROOT.glob("scripts/**/*.py")]:
            source = path.read_text()
            self.assertNotRegex(source, r"(?m)^\s*from\s+\.")
            self.assertNotIn("python -m upstreaming", source)
            self.assertNotRegex(source, r"(?m)^\s*(from|import)\s+upstreaming\b")
            self.assertNotRegex(source, r"[\"']upstreaming/")

    def test_schema_policy_and_state_resolution_ignore_process_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            state_root = cwd / "empty-state"
            state_root.mkdir()
            result = subprocess.run(
                [PYTHON, str(ROOT / "state.py"), "validate", str(state_root)],
                cwd=cwd,
                env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("projects: 0 valid", result.stdout)


class TestCliContracts(unittest.TestCase):
    def test_help_for_every_new_entry_point(self):
        commands = (
            ("state", "validate", "--help"),
            ("state", "check-diff", "--help"),
            ("triggers", "audit", "--help"),
            ("commands", "parse", "--help"),
            ("capacity", "observe", "--help"),
            ("controller", "observe", "--help"),
            ("work", "plan", "--help"),
            ("reconcile", "--help"),
        )
        for command in commands:
            with self.subTest(command=command):
                result = run_module(*command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_frozen_old_cli_captures_match_new_commands(self):
        captures = GOLDEN["captures"]
        self.assertEqual(GOLDEN["sourceCommit"], "c21e458f67541af8b6e988eda7a20e30fbad201d")
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            workflows = fixture / ".github/workflows"
            workflows.mkdir(parents=True)

            result = run_module("state", "validate", str(fixture))
            expected = captures["state.validate"]
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (expected["exit"], expected["stdout"], expected["stderr"]))

            result = run_module("triggers", "audit", "--root", str(fixture),
                                "--paths", "README.md")
            expected = captures["triggers.audit"]
            self.assertEqual(result.returncode, expected["exit"])
            self.assertEqual(json.loads(result.stdout), expected["json"])
            self.assertEqual(result.stderr, expected["stderr"])

            command_file = fixture / "command.txt"
            command_file.write_text('/upstreaming pause reason="maintenance"\n')
            result = run_module("commands", "parse", str(command_file))
            expected = captures["commands.parse"]
            self.assertEqual(result.returncode, expected["exit"])
            self.assertEqual(json.loads(result.stdout), expected["json"])
            self.assertEqual(result.stderr, expected["stderr"])

            result = run_module(
                "capacity",
                "observe",
                "--file",
                str(ROOT / "tests/fixtures/capacity-usage.json"),
            )
            expected = captures["capacity.observe"]
            self.assertEqual(result.returncode, expected["exit"])
            self.assertEqual(json.loads(result.stdout), expected["json"])
            self.assertEqual(result.stderr, expected["stderr"])

            result = run_module("controller", "observe", str(fixture), "--offline")
            expected = captures["controller.observe"]
            self.assertEqual(result.returncode, expected["exit"])
            self.assertEqual(normalized_without_at(json.loads(result.stdout)),
                             expected["jsonWithoutAt"])
            self.assertEqual(result.stderr, expected["stderr"])

            subprocess.run(["git", "init", "-q", str(fixture)], check=True)
            subprocess.run(
                ["git", "-C", str(fixture), "-c", "user.name=test", "-c",
                 "user.email=test@example.com", "commit", "--allow-empty", "-qm", "initial"],
                check=True,
            )
            plan_file = fixture / "plan.json"
            result = run_module(
                "work", "plan", "--root", str(fixture), "--out", str(plan_file),
                "--control-repo-id", "1",
            )
            expected = captures["work.plan"]
            self.assertEqual(result.returncode, expected["exit"])
            self.assertEqual(json.loads(result.stdout), expected["json"])
            self.assertEqual(result.stderr, expected["stderr"])
            self.assertFalse(plan_file.exists())

            result = run_module("reconcile", "--root", str(fixture), "--offline")
            expected = captures["reconcile"]
            self.assertEqual(result.returncode, expected["exit"])
            self.assertEqual(normalized_without_at(json.loads(result.stdout)),
                             expected["jsonWithoutAt"])
            self.assertEqual(result.stderr, expected["stderr"])

    def test_state_check_diff_argument_and_output_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            git = ["git", "-C", str(repository), "-c", "user.name=test", "-c",
                   "user.email=test@example.com"]
            subprocess.run([*git, "commit", "--allow-empty", "-qm", "base"], check=True)
            base = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            record = contribution()
            relative = state.record_path("contributions", record)
            path = repository / relative
            path.parent.mkdir(parents=True)
            payload = canonical_json(record)
            path.write_text(payload)
            subprocess.run(["git", "-C", str(repository), "add", relative], check=True)
            subprocess.run([*git, "commit", "-qm", "state"], check=True)
            candidate = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.strip()
            result = run_module(
                "state", "check-diff", base, candidate, "--repo", str(repository)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {"ok": True, "changed": 1, "bytes": len(payload.encode())},
            )
            self.assertEqual(result.stderr, "")


class TestDeferredAndWorkflowContracts(unittest.TestCase):
    def test_every_deferred_source_test_has_a_restoring_owner(self):
        deferrals = json.loads((ROOT / "docs/test-deferrals.json").read_text())["deferrals"]
        identifiers = {item["test"] for item in deferrals}
        for item in deferrals:
            self.assertIn(item["restoringMilestone"], (4, 5, 6))
            self.assertTrue(item["prerequisite"])
        reference = ROOT / "tests/deferred/test_workflow_contracts.py.reference"
        tree = ast.parse(reference.read_text())
        methods = {
            node.name
            for item in tree.body
            if isinstance(item, ast.ClassDef) and item.name == "TestWorkflowContracts"
            for node in item.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        }
        deferred_methods = {
            identifier.rsplit(".", 1)[-1]
            for identifier in identifiers
            if "test_workflow_contracts.py.reference" in identifier
        }
        restored_tree = ast.parse((ROOT / "tests/test_workflow_contracts.py").read_text())
        restored_methods = {
            node.name
            for item in restored_tree.body
            if isinstance(item, ast.ClassDef) and item.name == "TestWorkflowContracts"
            for node in item.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        }
        self.assertEqual(methods, deferred_methods | (methods & restored_methods))
        self.assertNotIn(
            "tests.test_observation.TestTriggers.test_repository_state_only_push_refreshes_only_the_website",
            identifiers,
        )

    def test_workflow_python_commands_use_root_modules_or_scripts(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text()
            self.assertNotIn("python -m upstreaming", text)
            self.assertNotRegex(text, r"python\s+upstreaming/")
            approved_modules = {path.stem for path in ROOT.glob("*.py")} | {"unittest"}
            for invocation in re.findall(r"python\s+-m\s+([A-Za-z0-9_.-]+)", text):
                self.assertIn(invocation, approved_modules)
            for script in re.findall(r"python\s+([^\s]+\.py)", text):
                self.assertTrue(script.startswith("scripts/"), script)
