# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Fail-closed installation of the approved native artifact compatibility fix."""
import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts import gh_aw_compat, normalize_gh_aw_locks

# Base64 preserves exact upstream bytes, including non-ASCII comments, for
# the pinned input hash without adding those comments to project prose.
FIXTURE = Path(__file__).parents[1] / "scripts/gh-aw-compat/tests/upload_artifact.v1.2.2.cjs.b64"


class TestNativeArtifactPatch(unittest.TestCase):
    def test_exact_source_and_output_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            actions = Path(tmp)
            handler = actions / "upload_artifact.cjs"
            handler.write_bytes(base64.decodebytes(FIXTURE.read_bytes()))
            self.assertEqual(gh_aw_compat.apply(actions), "patched")
            self.assertEqual(hashlib.sha256(handler.read_bytes()).hexdigest(), gh_aw_compat.PATCHED_SHA256)
            self.assertEqual(gh_aw_compat.apply(actions), "already patched")

    def test_compiler_lock_text_normalization_is_narrow_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "planner.lock.yml"
            lock.write_text(
                '# gh-aw-manifest: {"version":1}\nmessage: "one '
                + normalize_gh_aw_locks.EM_DASH
                + ' two"\n'
            )
            self.assertEqual(normalize_gh_aw_locks.normalize(lock), 1)
            self.assertEqual(
                lock.read_text(),
                '# gh-aw-manifest: {"version":1}\nmessage: "one -- two"\n',
            )
            self.assertEqual(normalize_gh_aw_locks.normalize(lock), 0)
            invalid = Path(tmp) / "authored.yml"
            invalid.write_text("message: no manifest\n")
            with self.assertRaises(ValueError):
                normalize_gh_aw_locks.normalize(invalid)

    def test_unknown_source_and_symlink_rejected_without_changes(self):
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
