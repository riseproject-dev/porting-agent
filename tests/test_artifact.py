# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Untrusted proposal-artifact extraction."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import artifact
from common import repository_root


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def zip(self, entries, symlink=None):
        p = self.d / "a.zip"
        with zipfile.ZipFile(p, "w") as z:
            for n, data in entries.items():
                z.writestr(n, data)
            if symlink:
                info = zipfile.ZipInfo(symlink)
                info.external_attr = 0o120777 << 16
                z.writestr(info, "/etc/passwd")
        return p

    def test_ok(self):
        got = artifact.extract(self.zip({"porting-out/proposal.json": "{}", "porting-out/patches.mbox": "From x"}), self.d / "o")
        self.assertEqual(got, ["patches.mbox", "proposal.json"])

    def test_flattened_cli_resolves_control_state_at_module_root(self):
        out = self.d / "live-layout"
        out.mkdir()
        (out / "task.json").write_text(
            json.dumps({"contribution_id": "closeio--ciso8601/1"})
        )
        archive = self.zip({"porting-out/proposal.json": "{}"})
        self.assertEqual(
            repository_root(), Path(artifact.__file__).resolve().parent
        )
        self.assertEqual(artifact.main(["artifact", "extract", str(archive), str(out)]), 0)
        record = json.loads((out / "record.json").read_text())
        self.assertEqual(record["contribution_id"], "closeio--ciso8601/1")

    def test_rejects(self):
        for entries, sym in [({"../proposal.json": "{}"}, None), ({"proposal.json": "{}", "run.sh": "x"}, None),
                             ({"x/proposal.json": "{}"}, None), ({"patches.mbox": "x"}, None),
                             ({"proposal.json": "not json"}, None), ({"proposal.json": "{}"}, "patches.mbox"),
                             ({"proposal.json": "{}", "patches.mbox": "x" * (5 * 1024 * 1024)}, None)]:
            with self.subTest(entries=list(entries), sym=sym), self.assertRaises(Exception):
                artifact.extract(self.zip(entries, sym), self.d / "o")


if __name__ == "__main__":
    unittest.main()
