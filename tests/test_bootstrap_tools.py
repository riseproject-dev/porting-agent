# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Deterministic checks for bootstrap audit and pinned tooling utilities."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import audit_repository_settings, gh_aw_compat
from scripts.dependencies import sync

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "scripts/gh-aw-compat/tests/upload_artifact.v1.2.2.cjs.b64"


class TestRepositorySettingsAudit(unittest.TestCase):
    def test_normalized_bootstrap_snapshot_matches(self):
        issues = audit_repository_settings.audit(
            ROOT / "tests/fixtures/repository-settings.json"
        )
        self.assertEqual(issues, [])

    def test_missing_or_changed_security_setting_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = json.loads((ROOT / "tests/fixtures/repository-settings.json").read_text())
            snapshot["actions"]["defaultWorkflowPermissions"] = "write"
            snapshot["ruleset"]["rules"].remove("non_fast_forward")
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(snapshot))
            issues = audit_repository_settings.audit(path)
            self.assertTrue(any("defaultWorkflowPermissions" in issue for issue in issues))
            self.assertTrue(any("ruleset.rules" in issue for issue in issues))


    def test_a_gh_aw_secret_fails_even_when_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = json.loads((ROOT / "tests/fixtures/repository-settings.json").read_text())
            snapshot["selectedOrganizationSecrets"].append("GH_AW_GITHUB_TOKEN")
            snapshot["environments"]["state"]["secrets"].append("GH_AW_GITHUB_MCP_SERVER_TOKEN")
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(snapshot))
            issues = audit_repository_settings.audit(path, path)
            self.assertEqual(len(issues), 4)
            self.assertTrue(any(issue.startswith("$.selectedOrganizationSecrets: GH_AW_GITHUB_TOKEN")
                                for issue in issues))
            self.assertTrue(any(issue.startswith(
                "$.environments.state.secrets: GH_AW_GITHUB_MCP_SERVER_TOKEN") for issue in issues))

    def test_the_live_audit_reads_every_secret_the_repository_can_use(self):
        pages = {
            "repos/o/r/environments": [{"environments": [{"name": "state"}]}, {"environments": []}],
            "repos/o/r/actions/secrets": [{"secrets": []}],
            "repos/o/r/actions/organization-secrets": [
                {"secrets": [{"name": "ANTHROPIC_API_KEY"}]},
                {"secrets": [{"name": "GH_AW_GITHUB_TOKEN"}]}],
            "repos/o/r/environments/state/secrets": [{"secrets": []}],
        }
        settings = audit_repository_settings.live_settings("o/r", pages.__getitem__)
        self.assertEqual(settings["selectedOrganizationSecrets"],
                         ["ANTHROPIC_API_KEY", "GH_AW_GITHUB_TOKEN"])
        self.assertEqual(settings["environments"], {"state": {"secrets": []}})
        [issue] = audit_repository_settings.forbidden_secrets(settings)
        self.assertIn("GH_AW_GITHUB_TOKEN", issue)
        pages["repos/o/r/actions/organization-secrets"] = [{"secrets": [{"name": "ANTHROPIC_API_KEY"}]}]
        self.assertEqual(audit_repository_settings.forbidden_secrets(
            audit_repository_settings.live_settings("o/r", pages.__getitem__)), [])

    def test_a_failed_gh_api_call_fails_the_live_audit_with_its_stderr(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 403: Resource not accessible")
        stderr = io.StringIO()
        with mock.patch.object(audit_repository_settings.subprocess, "run", return_value=failed), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = audit_repository_settings.main(["--live", "o/r"])
        self.assertNotEqual(code, 0)
        self.assertIn("HTTP 403: Resource not accessible", stderr.getvalue())

    def test_a_page_without_the_expected_key_fails_the_live_audit_closed(self):
        pages = {
            "repos/o/r/environments": [{"environments": []}],
            "repos/o/r/actions/secrets": [{"secrets": []}],
            "repos/o/r/actions/organization-secrets": [
                {"secrets": [{"name": "ANTHROPIC_API_KEY"}]}, {"message": "Not Found"}],
        }
        with self.assertRaisesRegex(audit_repository_settings.LiveAuditError,
                                    "organization-secrets: a page has no 'secrets' list"):
            audit_repository_settings.live_settings("o/r", pages.__getitem__)
        pages["repos/o/r/actions/organization-secrets"] = []
        with self.assertRaises(audit_repository_settings.LiveAuditError):
            audit_repository_settings.live_settings("o/r", pages.__getitem__)


class TestDependencySynchronization(unittest.TestCase):
    def test_milestone_one_inventory_matches_without_agent_workflows(self):
        self.assertEqual(sync.synchronize(ROOT), [])

    def test_source_and_lock_must_move_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "scripts/dependencies", root / "scripts/dependencies")
            (root / "scripts").mkdir(exist_ok=True)
            shutil.copy(ROOT / "scripts/install_gh_aw.sh", root / "scripts/install_gh_aw.sh")
            workflow = root / ".github/workflows/worker.md"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("engine:\n  id: pi\n  version: 0.57.2\n")
            self.assertIn(
                "worker: authored workflow and generated lock must move together",
                sync.synchronize(root),
            )

    def test_prerelease_inventory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "scripts/dependencies", root / "scripts/dependencies")
            (root / "scripts").mkdir(exist_ok=True)
            shutil.copy(ROOT / "scripts/install_gh_aw.sh", root / "scripts/install_gh_aw.sh")
            package = root / "scripts/dependencies/package.json"
            data = json.loads(package.read_text())
            data["dependencies"]["pi-web-access"] = "0.48.1-beta.1"
            package.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                sync.synchronize(root)


class TestNativeArtifactPatch(unittest.TestCase):
    def test_exact_source_output_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            actions = Path(tmp)
            handler = actions / "upload_artifact.cjs"
            handler.write_bytes(base64.decodebytes(FIXTURE.read_bytes()))
            self.assertEqual(gh_aw_compat.apply(actions), "patched")
            self.assertEqual(
                hashlib.sha256(handler.read_bytes()).hexdigest(), gh_aw_compat.PATCHED_SHA256
            )
            self.assertEqual(gh_aw_compat.apply(actions), "already patched")

    def test_unknown_source_and_symlink_are_rejected_without_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            actions = Path(tmp)
            handler = actions / "upload_artifact.cjs"
            handler.write_text("unexpected")
            with self.assertRaises(ValueError):
                gh_aw_compat.apply(actions)
            self.assertEqual(handler.read_text(), "unexpected")
            handler.unlink()
            handler.symlink_to(FIXTURE)
            with self.assertRaises(ValueError):
                gh_aw_compat.apply(actions)


class TestImportIsolation(unittest.TestCase):
    def test_scripts_import_without_custom_pythonpath(self):
        for relative in (
            "scripts/audit_repository_settings.py",
            "scripts/dependencies/sync.py",
            "scripts/gh_aw_compat.py",
            "scripts/normalize_gh_aw_locks.py",
            "scripts/pi_provider_credential.py",
        ):
            path = ROOT / relative
            spec = importlib.util.spec_from_file_location(path.stem, path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
