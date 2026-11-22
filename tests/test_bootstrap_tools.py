# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Deterministic checks for bootstrap audit and pinned tooling utilities."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

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
