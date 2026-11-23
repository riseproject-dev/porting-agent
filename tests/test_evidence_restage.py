# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
import json
import tempfile
import unittest
from pathlib import Path

import state
from tests.helpers import policy

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / ".state/evidence/ci-1467208233-45150934812-1.json"


@unittest.skipUnless(LIVE.is_file(), "live CI evidence not present")
class EvidenceRestage(unittest.TestCase):
    def store(self, directory):
        store = state.StateStore(Path(directory), policy())
        store.stage("evidence", json.loads(LIVE.read_text()))
        store.write_staged()
        return state.StateStore(Path(directory), policy())

    def test_the_same_run_observed_again_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            again = dict(json.loads(LIVE.read_text()), collected_at="2027-01-01T00:00:00+00:00")
            store.stage("evidence", again)
            self.assertEqual(store.write_staged(), [])

    def test_changed_evidence_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            changed = json.loads(LIVE.read_text())
            changed["limitations"] = ["see problems"]
            store.stage("evidence", changed)
            self.assertEqual(len(store.write_staged()), 1)


if __name__ == "__main__":
    unittest.main()
