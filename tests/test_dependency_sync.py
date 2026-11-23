# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Dependency proposals cannot leave authored workflow pins unchanged."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.dependencies import sync

ROOT = Path(__file__).parents[1]


class TestDependencySynchronization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(ROOT / "scripts/dependencies", self.root / "scripts/dependencies")
        (self.root / "scripts").mkdir(exist_ok=True)
        shutil.copy(ROOT / "scripts/install_gh_aw.sh", self.root / "scripts/install_gh_aw.sh")
        (self.root / "queue_result.py").write_text('GH_AW_VERSION = "v1.2.2"\n')
        workflows = self.root / ".github/workflows"
        workflows.mkdir(parents=True)
        for name in ("planner", "worker", "dispatcher"):
            source = 'engine:\n  id: pi\n  version: "1.5.0"\n'
            if name == "planner":
                source += "tools:\n  - npm:pi-web-access@0.48.0\n  - npm:pi-subagents@0.89.0\n"
            (workflows / f"{name}.md").write_text(source)
            (workflows / f"{name}.lock.yml").write_text("generated\n")

    def tearDown(self):
        self.tmp.cleanup()

    def update(self, package: str, version: str) -> None:
        path = self.root / "scripts/dependencies/package.json"
        manifest = json.loads(path.read_text())
        manifest["dependencies"][package] = version
        path.write_text(json.dumps(manifest))

    def test_pins_match(self):
        self.assertEqual(sync.synchronize(self.root), [])

    def test_runtime_proposal_needs_source_sync(self):
        self.update("@earendil-works/pi-coding-agent", "1.6.0")
        self.update("pi-web-access", "0.49.0")
        self.assertEqual(len(sync.synchronize(self.root)), 3)
        self.assertEqual(sync.synchronize(self.root, write=True), [])
        self.assertEqual(sync.synchronize(self.root), [])
        source = (self.root / ".github/workflows/planner.md").read_text()
        self.assertIn('version: "1.6.0"', source)
        self.assertIn("npm:pi-web-access@0.49.0", source)

    def test_prerelease_and_compiler_upgrade_do_not_silently_merge(self):
        self.update("pi-web-access", "0.49.0-beta.1")
        with self.assertRaises(ValueError):
            sync.synchronize(self.root)
        self.update("pi-web-access", "0.49.0")
        module = self.root / "scripts/dependencies/go.mod"
        module.write_text(module.read_text().replace("v1.2.2", "v1.3.0"))
        errors = sync.synchronize(self.root, write=True)
        self.assertTrue(any("reviewed installer hashes" in error for error in errors))
