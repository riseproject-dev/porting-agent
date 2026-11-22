# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Milestone 1 repository, migration, intake, and safety contracts."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import unittest
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SOURCE_COMMIT = "c21e458f67541af8b6e988eda7a20e30fbad201d"


def files() -> list[Path]:
    ignored = {".git", ".venv", ".ruff_cache", "__pycache__"}
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file() and ignored.isdisjoint(path.relative_to(ROOT).parts)
    ]


class TestRepositoryLayout(unittest.TestCase):
    def test_conventional_layout_and_forbidden_paths(self):
        for directory in (
            ".github/ISSUE_TEMPLATE",
            ".github/workflows",
            ".state",
            "docs",
            "image",
            "schemas",
            "scripts",
            "tests",
        ):
            self.assertTrue((ROOT / directory).is_dir(), directory)
        for forbidden in ("upstreaming", "upstreaming-state", "src"):
            self.assertFalse((ROOT / forbidden).exists(), forbidden)
        self.assertFalse((ROOT / "porting_agent.py").exists())
        self.assertFalse((ROOT / "__main__.py").exists())
        self.assertFalse(
            any(path.name.startswith("upstreaming-") for path in WORKFLOWS.iterdir())
        )

    def test_generated_workflow_files_are_expected_and_paired(self):
        manifest = json.loads((ROOT / "docs/workflow-migration.json").read_text())
        self.assertEqual(
            {
                path.relative_to(ROOT).as_posix()
                for path in (ROOT / ".github/aw").rglob("*")
                if path.is_file()
            },
            {".github/aw/actions-lock.json"},
        )
        expected_targets = {item["target"] for item in manifest["workflows"]}
        for lock in WORKFLOWS.glob("*.lock.yml"):
            self.assertIn(lock.name, expected_targets)
            self.assertTrue(lock.with_name(lock.name.removesuffix(".lock.yml") + ".md").is_file())
        for source in WORKFLOWS.glob("*.md"):
            self.assertTrue(source.with_name(source.stem + ".lock.yml").is_file())


class TestMigrationInventory(unittest.TestCase):
    def test_every_frozen_source_file_has_one_decision(self):
        inventory = json.loads((ROOT / "docs/migration-inventory.json").read_text())
        self.assertEqual(inventory["sourceCommit"], SOURCE_COMMIT)
        entries = inventory["entries"]
        sources = [entry["source"] for entry in entries]
        self.assertEqual(len(sources), 93)
        self.assertEqual(len(sources), len(set(sources)))
        self.assertTrue(all(source.startswith("upstreaming/") for source in sources))
        digest = hashlib.sha256(("\n".join(sorted(sources)) + "\n").encode()).hexdigest()
        self.assertEqual(digest, inventory["sourceTreeSha256"])
        for entry in entries:
            self.assertIn(entry["action"], {"move", "remove", "split", "defer"})
            self.assertIn(entry["milestone"], range(1, 11))
            self.assertTrue(entry["rationale"])
            if entry["action"] == "remove":
                self.assertEqual(entry["targets"], [])
            else:
                self.assertTrue(entry["targets"])
            if entry["action"] == "defer":
                self.assertTrue(entry["restoringMilestones"])
                self.assertEqual(entry["status"], "deferred")
            for target in entry["targets"]:
                self.assertNotRegex(target, r"(^|/)(upstreaming|.state|src)(/|$)")
        for entry in entries:
            if entry["status"] in {"migrated", "partially-migrated", "deferred"}:
                for target in entry["targets"]:
                    self.assertTrue((ROOT / target).exists(), target)

    def test_workflow_rename_and_ownership_is_one_to_one(self):
        manifest = json.loads((ROOT / "docs/workflow-migration.json").read_text())
        self.assertEqual(manifest["sourceCommit"], SOURCE_COMMIT)
        workflows = manifest["workflows"]
        sources = [item["source"] for item in workflows]
        targets = [item["target"] for item in workflows]
        self.assertEqual(len(sources), len(set(sources)))
        self.assertEqual(len(targets), len(set(targets)))
        self.assertFalse(any(target.startswith("upstreaming-") for target in targets))
        for item in workflows:
            self.assertTrue(item["owner"])
            self.assertIn(item["milestone"], range(1, 11))
            present = (WORKFLOWS / item["target"]).exists()
            self.assertEqual(present, item["milestone"] <= 5, item["target"])
        for item in workflows:
            if item["agentic"] and (WORKFLOWS / item["target"]).exists():
                stem = item["target"].removesuffix(".md")
                self.assertTrue((WORKFLOWS / f"{stem}.lock.yml").is_file())


class TestIssueFormAndCanary(unittest.TestCase):
    def test_issue_form_structure_and_setup_label(self):
        form = yaml.safe_load((ROOT / ".github/ISSUE_TEMPLATE/porting-request.yml").read_text())
        self.assertEqual(form["name"], "Upstream RISC-V Python wheels")
        self.assertEqual(form["title"], "[upstreaming] ")
        self.assertEqual(form["labels"], ["upstreaming-request"])
        body = form["body"]
        self.assertEqual(
            [field["id"] for field in body],
            ["upstream-repository", "distributions", "target-branch", "additional-notes"],
        )
        required = {
            field["id"]: field.get("validations", {}).get("required", False) for field in body
        }
        self.assertEqual(
            required,
            {
                "upstream-repository": True,
                "distributions": True,
                "target-branch": False,
                "additional-notes": False,
            },
        )
        labels = yaml.safe_load((ROOT / ".github/labels.yml").read_text())
        self.assertIn(
            {
                "name": "upstreaming-request",
                "color": "5fb86d",
                "description": "Starts a porting-agent request",
            },
            labels,
        )

    def test_ciso8601_frozen_request_contract(self):
        canary = json.loads((ROOT / "tests/fixtures/ciso8601-request.json").read_text())
        self.assertEqual(canary["title"], "[upstreaming] ciso8601 riscv64 wheel support")
        self.assertEqual(canary["label"], "upstreaming-request")
        self.assertEqual(canary["canonicalRepository"], "riseproject-dev/porting-agent")
        self.assertEqual(
            canary["issueUrl"],
            "https://github.com/riseproject-dev/porting-agent/issues/1",
        )
        self.assertEqual(canary["issueNumber"], 1)
        self.assertEqual(
            canary["fields"],
            {
                "upstream-repository": "https://github.com/closeio/ciso8601",
                "distributions": "ciso8601 (import: ciso8601)",
                "target-branch": "master",
                "additional-notes": canary["fields"]["additional-notes"],
            },
        )
        self.assertIn(canary["sourceBacklink"], canary["renderedBody"])
        self.assertIn("python-wheels#625", canary["renderedBody"])
        self.assertIn("9339ec54ae594458b0b9c6039bb55e9b2b13eff7", canary["renderedBody"])
        self.assertEqual(
            hashlib.sha256(canary["renderedBody"].encode()).hexdigest(),
            "9f58bffa65d7e7df0206ae1130ce6eb5e218181b966839f2e609c05e8baa7ebb",
        )
        self.assertEqual(
            canary["renderedBodySha256"],
            "9f58bffa65d7e7df0206ae1130ce6eb5e218181b966839f2e609c05e8baa7ebb",
        )
        self.assertEqual(canary["verifiedBodySha256"], canary["renderedBodySha256"])


class TestDocumentationLinks(unittest.TestCase):
    def test_current_and_historical_links_are_distinct(self):
        markdown = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
        link_pattern = re.compile(r"https://github\.com/[^)\s`]+")
        current_seen = False
        historical_seen = False
        for path in markdown:
            for line in path.read_text().splitlines():
                for url in link_pattern.findall(line):
                    parsed = urlparse(url)
                    self.assertEqual(parsed.scheme, "https")
                    if "/riseproject-dev/python-wheels" in parsed.path:
                        historical_seen = True
                        self.assertIn("Earlier implementation evidence:", line)
                    if "/riseproject-dev/porting-agent" in parsed.path:
                        current_seen = True
        self.assertTrue(current_seen)
        self.assertTrue(historical_seen)


class TestRepositoryHygiene(unittest.TestCase):
    def test_license_secret_symlink_case_size_and_executable_contracts(self):
        license_text = (ROOT / "LICENSE").read_text()
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        self.assertIn("Copyright 2026 The RISE Project", license_text)
        repository_files = files()
        ignored = {".git", ".venv", ".ruff_cache", "__pycache__"}
        self.assertFalse(
            any(
                path.is_symlink()
                for path in ROOT.rglob("*")
                if ignored.isdisjoint(path.relative_to(ROOT).parts)
            )
        )
        folded: dict[str, Path] = {}
        for path in repository_files:
            relative = path.relative_to(ROOT).as_posix()
            key = relative.casefold()
            self.assertNotIn(key, folded, f"case collision: {relative} and {folded.get(key)}")
            folded[key] = path
            self.assertLessEqual(path.stat().st_size, 5 * 1024 * 1024, relative)
            data = path.read_bytes()
            private_key_marker = b"-----BEGIN " + b"PRIVATE KEY-----"
            self.assertNotIn(private_key_marker, data, relative)
            self.assertIsNone(re.search(rb"gh[pousr]_[A-Za-z0-9]{30,}", data), relative)
            self.assertNotIn("\u2014".encode(), data, relative)
        executable = {
            path.relative_to(ROOT).as_posix()
            for path in repository_files
            if path.stat().st_mode & stat.S_IXUSR
        }
        self.assertEqual(
            executable,
            {
                "image/entrypoint.sh",
                "image/run.sh",
                "scripts/check_gh_aw.sh",
                "scripts/compile_gh_aw.sh",
                "scripts/gh-aw-compat/check.sh",
                "scripts/git_identity.sh",
                "scripts/install_gh_aw.sh",
            },
        )

    def test_python_and_shell_implementation_has_spdx(self):
        for path in files():
            if path.suffix == ".py" or (path.suffix == ".sh" and "fixtures" not in path.parts):
                head = path.read_text(errors="ignore")[:300]
                self.assertIn("SPDX-License-Identifier:", head, str(path.relative_to(ROOT)))


class TestValidationWorkflowSecurity(unittest.TestCase):
    def test_pull_request_validation_has_no_remote_writer_inputs(self):
        workflow = (WORKFLOWS / "validate.yml").read_text()
        lowered = workflow.lower()
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertNotIn("environment:", lowered)
        for forbidden in (
            "riseproject_app_private_key",
            "riseproject_upstreaming_app_private_key",
            "anthropic_api_key",
            "pypi",
            "package credential",
            "state-publisher",
            "contents: write",
            "issues: write",
            "pull-requests: write",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotRegex(workflow, r"\b(gh api|git push|gh issue|gh pr)\b")
