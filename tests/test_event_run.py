# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The workflow run that records an event is stored on it."""

import tempfile
import unittest
from pathlib import Path

import schemas
import state
from tests.helpers import policy

EVENT = {"schema_version": 1, "contribution_id": "example--bravo/1", "sequence": 0,
         "at": "2026-11-20T01:02:03+00:00", "kind": "retry_dispatch", "from": {}, "to": {},
         "cause": {"type": "tick", "id": None}, "revision_bumped": False}


class TestEventRun(unittest.TestCase):
    def staged(self, run):
        with tempfile.TemporaryDirectory() as directory:
            store = state.StateStore(Path(directory), policy(), run=run)
            path = store.stage("events", dict(EVENT))
            return store._staged[path]

    def test_the_store_records_its_run_on_every_event(self):
        event = self.staged({"id": 45313564039, "attempt": 2})
        self.assertEqual(event["run"], {"id": 45313564039, "attempt": 2})
        schemas.validate("event", event)

    def test_without_a_run_nothing_is_added(self):
        self.assertNotIn("run", self.staged(None))

    def test_the_schema_refuses_a_malformed_run(self):
        for run in ({"id": 0, "attempt": 1}, {"id": 1}, {"id": "1", "attempt": 1},
                    {"id": 1, "attempt": 1, "extra": 1}):
            with self.subTest(run=run), self.assertRaises(Exception):
                schemas.validate("event", dict(EVENT, run=run))

    def test_the_run_is_read_from_the_github_environment(self):
        self.assertEqual(state.run_from_env({"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}),
                         {"id": 123, "attempt": 2})
        for env in ({}, {"GITHUB_RUN_ID": "123"}, {"GITHUB_RUN_ID": "x", "GITHUB_RUN_ATTEMPT": "1"},
                    {"GITHUB_RUN_ID": "0", "GITHUB_RUN_ATTEMPT": "1"}):
            with self.subTest(env=env):
                self.assertIsNone(state.run_from_env(env))


if __name__ == "__main__":
    unittest.main()
