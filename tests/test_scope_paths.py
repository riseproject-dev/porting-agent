# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The bounds of a patch scope are the same where scope paths enter and where
the record stores them, and a published patch widens the scope only by the
files no scope path matches.

The live ``luhenry--d2vm/3`` record paused with a ``SchemaError`` when its
publication was folded: the fold appended every published file to the 30
admitted scope paths, also the files a glob of the scope already matched
(``pkg/qemu/qemu.go`` under ``pkg/qemu/*``), and the record schema refused the
40 paths that resulted.
"""

from __future__ import annotations

import json
import unittest

import common
import reconcile
import schemas
from common import SCOPE_PATH_MAX_LENGTH, SCOPE_PATHS_MAX, validate_scope_paths, widen_scope
from tests.test_development_table import NOW, POLICY, publication
from tests.test_development_table import running as _running

# The admitted scope and the published files of luhenry--d2vm/3, revision 3.
D2VM_SCOPE = [
    ".github/workflows/*", ".goreleaser.yaml", "Makefile", "Dockerfile", "README.md",
    ".gitignore", "builder.go", "bootloader.go", "grub.go", "grub_bios.go", "grub_efi.go",
    "grub_common.go", "syslinux.go", "config.go", "config_test.go", "dockerfile.go",
    "docker_image_test.go", "version.go", "convert.go", "convert_options.go",
    "container_disk.go", "qemu.sh", "cmd/d2vm/*", "cmd/d2vm/run/*", "pkg/qemu/*",
    "pkg/docker/*", "templates/*", "e2e/*", "docs/content/*", "docs/content/reference/*",
]
D2VM_CHANGED = [
    ".github/workflows/ci.yaml", ".goreleaser.yaml", "Makefile", "README.md", "builder.go",
    "cmd/d2vm/flags.go", "cmd/d2vm/run/qemu.go", "docs/content/reference/d2vm_build.md",
    "docs/content/reference/d2vm_convert.md", "docs/content/reference/d2vm_run_qemu.md",
    "e2e/e2e_test.go", "pkg/qemu/qemu.go", "templates/debian.Dockerfile",
    "templates/ubuntu.Dockerfile",
]


def running(scope_paths=("a",)):
    """A record in `development.agent_running` with ``scope_paths`` admitted."""
    return dict(_running(), scope_paths=list(scope_paths))


def _schema_arrays():
    schema = json.loads((common.SCHEMA_DIR / "contribution.schema.json").read_text())
    scope = next(b for b in schema["properties"]["scope_paths"]["anyOf"] if b["type"] == "array")
    override = next(b for b in schema["properties"]["admission_override"]["anyOf"]
                    if b.get("type") == "object")["properties"]["paths"]
    return {"scope_paths": scope, "admission_override.paths": override}


class TestBoundsAgree(unittest.TestCase):
    def test_the_record_schema_states_the_bounds_the_gate_checks(self):
        for name, array in _schema_arrays().items():
            with self.subTest(field=name):
                self.assertEqual(array["maxItems"], SCOPE_PATHS_MAX)
                self.assertEqual(array["items"]["maxLength"], SCOPE_PATH_MAX_LENGTH)
                self.assertEqual(array["items"]["minLength"], 1)

    def test_a_scope_the_gate_admits_is_a_scope_the_record_stores(self):
        rec = running(D2VM_SCOPE)
        for size in (1, SCOPE_PATHS_MAX):
            paths = [f"dir{i}/*" for i in range(size)]
            validate_scope_paths(paths)
            schemas.validate("contribution", dict(rec, scope_paths=paths))
        with self.assertRaises(ValueError):
            validate_scope_paths([f"f{i}" for i in range(SCOPE_PATHS_MAX + 1)])
        with self.assertRaises(schemas.SchemaError):
            schemas.validate("contribution",
                             dict(rec, scope_paths=[f"f{i}" for i in range(SCOPE_PATHS_MAX + 1)]))
        long = "a" * (SCOPE_PATH_MAX_LENGTH + 1)
        with self.assertRaises(ValueError):
            validate_scope_paths([long])
        with self.assertRaises(schemas.SchemaError):
            schemas.validate("contribution", dict(rec, scope_paths=[long]))

    def test_the_d2vm_scope_is_a_valid_scope(self):
        self.assertEqual(validate_scope_paths(D2VM_SCOPE), D2VM_SCOPE)


class TestWidening(unittest.TestCase):
    def test_files_the_scope_matches_add_nothing(self):
        self.assertEqual(widen_scope(D2VM_SCOPE, D2VM_CHANGED), D2VM_SCOPE)

    def test_a_glob_matches_below_its_directory_as_everywhere(self):
        # `fnmatchcase`, the rule of the worker gate, the plan gate, and the
        # publisher, lets `*` match `/`.
        self.assertTrue(common.scope_covers("cmd/d2vm/run/qemu.go", ["cmd/d2vm/*"]))
        self.assertFalse(common.scope_covers("cmd/other.go", ["cmd/d2vm/*"]))

    def test_unmatched_files_join_as_exact_paths_once(self):
        self.assertEqual(widen_scope(["a/*", "b"], ["a/x", "c", "b", "c", "d/e"]),
                         ["a/*", "b", "c", "d/e"])

    def test_a_widening_beyond_the_bounds_is_refused(self):
        scope = [f"f{i}" for i in range(SCOPE_PATHS_MAX)]
        self.assertEqual(widen_scope(scope, ["f0"]), scope)
        with self.assertRaises(ValueError):
            widen_scope(scope, ["new"])


class TestTheFold(unittest.TestCase):
    def publish(self, rec, changed):
        return reconcile.apply_publication(
            rec, dict(publication(rec, "published"),
                      operation={"operation_id": "op-1", "proposed": {"changed_files": changed}}),
            now=NOW, policy=POLICY, worker_run_id="555")

    def test_the_d2vm_publication_folds_with_its_scope_unchanged(self):
        rec = running(D2VM_SCOPE)
        new, staged = self.publish(rec, D2VM_CHANGED)
        self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                         ["record_publish_success"])
        self.assertEqual(new["scope_paths"], D2VM_SCOPE)
        self.assertEqual(new["activity"], "waiting_ci")

    def test_a_scope_that_cannot_widen_is_kept_and_the_event_says_why(self):
        full = [f"f{i}" for i in range(SCOPE_PATHS_MAX)]
        rec = running(full)
        new, staged = self.publish(rec, ["f1", "outside"])
        [event] = [e for kind, e in staged if kind == "events"]
        self.assertEqual(event["kind"], "record_publish_success")
        self.assertEqual(new["scope_paths"], full)
        self.assertIn(f"1..{SCOPE_PATHS_MAX}", event["details"]["scope_extension_not_recorded"])


def _changed_files_arrays():
    """Every ``changed_files`` array a schema states, by schema and path."""
    found = {}

    def walk(node, where):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "changed_files" and isinstance(value, dict) and value.get("type") == "array":
                    found[where + "/" + key] = value
                walk(value, f"{where}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{where}/{index}")

    for path in sorted(common.SCHEMA_DIR.glob("*.schema.json")):
        walk(json.loads(path.read_text()), path.name)
    return found


class TestChangedFilesBounds(unittest.TestCase):
    """The files one patch changes become scope paths once it is published,
    so the worker gate (``validate_scope_paths``), the proposal, the held
    patch, the push operation and the publisher all bound them by
    ``SCOPE_PATHS_MAX``: a patch the gate passes is a patch the publisher
    can record."""

    def test_every_schema_bounds_changed_files_by_the_scope_bound(self):
        arrays = _changed_files_arrays()
        for where in ("proposal.schema.json/properties/changed_files",
                      "operation.schema.json/properties/proposed/properties/changed_files"):
            self.assertIn(where, arrays)
        self.assertTrue(any(where.startswith("contribution.schema.json/properties/held_patch")
                            for where in arrays))
        for where, array in arrays.items():
            with self.subTest(schema=where):
                self.assertEqual(array["maxItems"], SCOPE_PATHS_MAX)
                self.assertGreaterEqual(array["items"]["maxLength"], SCOPE_PATH_MAX_LENGTH)

    def test_the_gate_passes_exactly_the_patches_the_bound_admits(self):
        import hashlib
        import tempfile
        from pathlib import Path

        import worker_gate
        from tests.test_milestone5_worker import proposal, task

        def run(count):
            files = [f"src/f{i:02d}.c" for i in range(count)]
            patch = ("From " + "a" * 40 + " Mon Sep 17 00:00:00 2001\n"
                     "Subject: [PATCH] add riscv64\n\n---\n" + "".join(
                         f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
                         "@@ -1 +1 @@\n-old\n+new\n" for path in files)).encode()
            value = proposal(patch) | {"changed_files": files,
                                       "patch_digest": hashlib.sha256(patch).hexdigest()}
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "patches.mbox"
                path.write_bytes(patch)
                return worker_gate.validate(task() | {"allowed_paths": ["src/*"]}, value, path)

        for count in (33, SCOPE_PATHS_MAX):
            with self.subTest(files=count):
                self.assertEqual(len(run(count)["changed_files"]), count)
        with self.assertRaises((worker_gate.WorkerGateError, ValueError)):
            run(SCOPE_PATHS_MAX + 1)


if __name__ == "__main__":
    unittest.main()
