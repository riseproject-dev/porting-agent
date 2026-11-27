# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The record migration that returns a contribution without a PyPI contract
from the release wait for a human to the observation.

The live ``luhenry--d2vm/3`` (Go, merged as luhenry/d2vm#1, event 73) took
`require_release_intervention` with the pending action
`release_contract_missing` (event 74): it names no target contract, and the
observation then read PyPI only. The migration returns it to
`release_wait.idle`; the GitHub release observation takes over.
"""

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import engine
import state
import status_ledger
from tests.helpers import policy

FIXTURE = Path(__file__).parent / "fixtures/live-d2vm-release"
POLICY = policy()
NOW = "2026-11-27T09:00:00+00:00"
CID = "luhenry--d2vm/3"
PATH = ".state/contributions/luhenry--d2vm--3.json"


class TestReleaseResumeMigration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.live = json.loads((FIXTURE / "contribution.json").read_text())
        shutil.copytree(FIXTURE / "events", self.root / ".state/events/luhenry--d2vm--3")
        self.write(self.live)

    def write(self, record):
        path = self.root / PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True))

    def read(self):
        return json.loads((self.root / PATH).read_text())

    def apply(self):
        return state.apply_pending_record_migrations(self.root, POLICY, now=NOW)

    def test_the_live_record_is_the_one_that_waits(self):
        self.assertEqual(engine.state_key(self.live), "release_wait.waiting_human")
        self.assertEqual(self.live["pending_human_action"]["kind"], "release_contract_missing")
        self.assertIsNone(self.live["target_contract_id"])
        self.assertEqual(self.live["upstream"]["full_name"], "luhenry/d2vm")
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [PATH])

    def test_it_returns_to_the_observation_with_an_event(self):
        written = self.apply()
        self.assertEqual(len(written), 2)
        record = self.read()
        self.assertEqual(engine.state_key(record), "release_wait.idle")
        self.assertIsNone(record["pending_human_action"])
        self.assertEqual(record["revision"], self.live["revision"])
        self.assertEqual(record["merge"], self.live["merge"])
        changed = {k for k in record if record[k] != self.live[k]}
        self.assertEqual(changed, {"activity", "pending_human_action", "updated_at"})
        [event] = [e for e in state.contribution_events(self.root, CID)
                   if e["kind"] == state.RELEASE_MIGRATED]
        self.assertEqual(event["sequence"], 75)
        self.assertEqual(event["cause"], {"type": "migration", "id": state.RELEASE_MIGRATION})
        self.assertEqual(event["details"], {"from_state": "release_wait.waiting_human",
                                            "to_state": "release_wait.idle",
                                            "pending_human_action": "release_contract_missing"})
        self.assertFalse(event["revision_bumped"])
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [])

    def test_it_is_idempotent(self):
        self.apply()
        before = self.read()
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.read(), before)
        self.assertEqual(len([e for e in state.contribution_events(self.root, CID)
                              if e["kind"] == state.RELEASE_MIGRATED]), 1)

    def test_a_wait_for_a_named_contract_is_kept(self):
        record = copy.deepcopy(self.live)
        record["target_contract_id"] = "contract-1"
        self.write(record)
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [])
        with self.assertRaises(state.StateError):
            state.migrate_release_resume(record, now=NOW)

    def test_a_record_with_distributions_takes_the_pypi_path_and_migrates(self):
        """The ciso8601 shape: the observation can decide it as well."""
        record = copy.deepcopy(self.live)
        record["distributions"] = ["ciso8601"]
        self.write(record)
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [PATH])
        self.apply()
        self.assertEqual(engine.state_key(self.read()), "release_wait.idle")

    def test_another_pending_action_is_kept(self):
        record = copy.deepcopy(self.live)
        record["pending_human_action"]["kind"] = "release_distribution_missing"
        self.write(record)
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [])

    def test_the_ledger_words_the_event(self):
        self.apply()
        [event] = [e for e in state.contribution_events(self.root, CID)
                   if e["kind"] == state.RELEASE_MIGRATED]
        text = status_ledger.render(event, self.read(), "https://github.com/run")
        self.assertIn("linux riscv64", text)
        self.assertIn("no human action", text)


if __name__ == "__main__":
    unittest.main()
