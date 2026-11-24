# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The record migration that refunds the rejections of the `changed_files`
contradiction.

The worker gate and the fork publisher once required different sets for the
`changed_files` of a rewrite, so the live ``luhenry--d2vm/3`` was rejected
twice (events 44 and 46), charged `no_progress` both times, and parked with
"repair budgets exhausted: no_progress". The migration returns the charges
and the record, once.
"""

import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import capacity
import engine
import lifecycle
import reconcile
import state
from tests.helpers import policy

FIXTURE = Path(__file__).parent / "fixtures/live-d2vm-gate"
POLICY = policy()
NOW = "2026-11-25T12:00:00+00:00"
CID = "luhenry--d2vm/3"


def load(name: str) -> dict:
    return json.loads((FIXTURE / name).read_text())


class _Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # The fixture is the record as committed before the task rename; the
        # refund is tested on it under the current names, and its composition
        # with the name migration in `TestComposition`.
        self.record = state.migrate_names("contributions", load("contribution.json"))[0]
        self.events = self.root / ".state/events/luhenry--d2vm--3"
        shutil.copytree(FIXTURE / "events", self.events)
        self.write(self.record)

    def write(self, record):
        path = self.root / ".state/contributions/luhenry--d2vm--3.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True))

    def read(self) -> dict:
        return json.loads((self.root / ".state/contributions/luhenry--d2vm--3.json").read_text())

    def event(self, sequence: int, at=None, **details):
        base = load("events/00000044.json") | {"sequence": sequence}
        base["details"] = dict(base["details"], **details)
        if at:
            base["at"] = at
        (self.events / f"{sequence:08d}.json").write_text(json.dumps(base))

    def other_refresh(self):
        """The rejections that spent the refresh are of another reason."""
        for sequence in (31, 33):
            self.event(sequence, reason="the series breaks the project's commit conventions: x")

    def apply(self):
        return state.apply_pending_record_migrations(self.root, POLICY, now=NOW)

    def refunds(self) -> list[dict]:
        return [e for e in state.contribution_events(self.root, CID)
                if e["kind"] == state.REFUND_MIGRATED]


class TestComposition(_Store):
    def test_the_refund_follows_the_name_migration_of_the_same_record(self):
        self.write(load("contribution.json"))
        self.assertEqual(state.pending_record_migrations(self.root, POLICY),
                         [".state/contributions/luhenry--d2vm--3.json"])
        written = self.apply()
        self.assertEqual(len(written), 3)
        self.assertEqual(self.apply(), [])
        record = self.read()
        self.assertNotIn("active_work", record)
        self.assertEqual((record["phase"], record["activity"]), ("development", "fix_needed"))
        names, refund = (state.contribution_events(self.root, CID)[-2:])
        self.assertEqual((names["sequence"], names["kind"], names["cause"]["id"]),
                         (48, state.NAMES_MIGRATED, "task-rename"))
        self.assertEqual((refund["sequence"], refund["kind"]), (49, state.REFUND_MIGRATED))


class TestRefund(_Store):
    def test_the_parked_record_returns_to_repair_with_its_charges_refunded(self):
        self.assertEqual(self.record["phase"], "parked")
        self.assertEqual(self.record["budget"]["no_progress_streak"], 2)
        written = self.apply()
        self.assertEqual(len(written), 2)
        record = self.read()
        self.assertEqual((record["phase"], record["activity"]), ("development", "fix_needed"))
        self.assertEqual(record["budget"]["no_progress_streak"], 0)
        self.assertEqual(record["budget"]["attempts_used"], self.record["budget"]["attempts_used"])
        self.assertIsNone(record["parked_reason"])
        self.assertIsNone(record["previous_phase"])
        self.assertIsNone(record["active_task"])
        self.assertEqual(record["revision"], self.record["revision"])
        [event] = self.refunds()
        self.assertEqual(event["details"]["refunded"], [44, 46])
        self.assertEqual(event["details"]["returned"], {"no_progress": 2})
        self.assertTrue(event["details"]["unparked"])
        self.assertEqual(event["cause"], {"type": "migration", "id": state.REFUND_MIGRATION})
        self.assertEqual(event["sequence"], 48)
        # The refresh that events 31 and 33 used up is returned with them.
        self.assertFalse(record["automatic_refresh_used"])
        self.assertTrue(self.record["automatic_refresh_used"])
        self.assertTrue(event["details"]["refresh_returned"])
        self.assertEqual(event["details"]["refresh_event"], 34)

    def test_the_invariants_of_development_fix_needed_hold(self):
        self.apply()
        record = self.read()
        self.assertEqual(lifecycle.requirement_problems("development.fix_needed", record), [])
        self.assertIsNone(engine.state_problem(record))
        self.assertIsNone(record["pending_publication"])
        self.assertTrue(record["upstream"]["base_sha"] and record["upstream"]["repository_id"])

    def test_it_is_idempotent(self):
        self.apply()
        first = self.read()
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [])
        self.assertEqual(self.apply(), [])
        self.assertEqual(self.read(), first)
        self.assertEqual(len(self.refunds()), 1)

    def test_a_charge_that_a_later_publication_started_again_is_not_refunded(self):
        # The streak started again after the rejections: nothing counts.
        record = copy.deepcopy(self.record)
        record["budget"]["no_progress_streak"] = 0
        self.write(record)
        self.other_refresh()
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [])

    def test_only_the_rejections_still_counted_are_refunded(self):
        record = copy.deepcopy(self.record)
        record["budget"]["no_progress_streak"] = 1
        self.write(record)
        self.apply()
        [event] = self.refunds()
        self.assertEqual(event["details"]["refunded"], [46])
        self.assertEqual(self.read()["budget"]["no_progress_streak"], 0)

    def test_a_rejection_for_another_reason_is_not_refunded(self):
        self.other_refresh()
        self.event(44, reason="the series breaks the project's commit conventions: x")
        self.event(46, reason="the series breaks the project's commit conventions: y")
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [])

    def test_a_record_still_exhausted_after_the_refund_stays_parked(self):
        # Two other rejections of the streak are not of the contradiction.
        for sequence in (50, 51):
            self.event(sequence, reason="the series breaks the project's commit conventions: y")
        record = copy.deepcopy(self.record)
        record["budget"]["no_progress_streak"] = 4
        self.write(record)
        self.apply()
        record = self.read()
        self.assertEqual(record["budget"]["no_progress_streak"], 2)
        self.assertEqual(record["phase"], "parked")
        [event] = self.refunds()
        self.assertEqual(event["details"]["refunded"], [44, 46])
        self.assertFalse(event["details"]["unparked"])
        # The refunded sequences are not refunded again.
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [])

    def test_a_rejection_after_the_cutoff_is_a_genuine_mistake_and_is_not_refunded(self):
        after = "2026-11-25T14:00:01+00:00"
        self.other_refresh()
        self.event(44, at=after)
        self.event(46, at=after)
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [])
        # A reason of the publisher at a time before the cutoff is refunded.
        self.event(44, at="2026-11-25T13:59:59+00:00")
        self.event(46, at=after)
        self.apply()
        [event] = self.refunds()
        self.assertEqual(event["details"]["refunded"], [44])

    def test_a_refresh_spent_by_other_rejections_is_not_returned(self):
        self.event(33, reason="the series breaks the project's commit conventions: y")
        self.apply()
        [event] = self.refunds()
        self.assertNotIn("refresh_returned", event["details"])
        self.assertTrue(self.read()["automatic_refresh_used"])
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [])

    def test_a_refresh_is_returned_once_and_a_later_one_is_not_confused_with_it(self):
        self.apply()
        record = self.read()
        # A later refresh in the same generation, spent by something else.
        record["automatic_refresh_used"] = True
        self.write(record)
        self.assertEqual(state.pending_record_migrations(self.root, POLICY), [])

    def test_task_total_is_not_refunded(self):
        self.event(46, charged=["no_progress", "task_total"])
        self.apply()
        record = self.read()
        self.assertEqual(record["budget"]["attempts_used"], self.record["budget"]["attempts_used"])
        self.assertEqual(self.refunds()[0]["details"]["returned"], {"no_progress": 2})

    def test_a_record_created_after_the_cutoff_reads_no_events(self):
        record = copy.deepcopy(self.record)
        record["created_at"] = "2026-11-25T14:00:00+00:00"
        self.assertFalse(state.needs_rejection_refund(record))

    def test_a_record_that_holds_a_publication_is_not_returned(self):
        record = copy.deepcopy(self.record)
        record["pending_publication"] = {"held": True}
        new, facts = engine.refund_contradicted(
            record, state.contribution_events(self.root, CID), set(), policy=POLICY, now=NOW)
        self.assertFalse(facts["unparked"])
        self.assertEqual(new["phase"], "parked")
        self.assertEqual(new["budget"]["no_progress_streak"], 0)

    def test_the_next_tick_requests_the_patch_again(self):
        self.apply()
        snapshot = reconcile.Snapshot({"c": self.read()}, {}, {}, {}, {})
        with contextlib.redirect_stderr(io.StringIO()):
            out = reconcile.tick(None, snapshot, policy=POLICY,
                                 now=datetime.fromisoformat("2026-11-25T12:05:00+00:00"),
                                 publications=[], pressure=capacity.Pressure(ok=True),
                                 observe=False, pipeline="a" * 64)
        verbs = [e["kind"] for kind, e in out.staged if kind == "events"]
        self.assertEqual(verbs, ["request_patch"])
        [record] = [r for kind, r in out.staged if kind == "contributions"]
        self.assertEqual(record["activity"], "agent_running")
        self.assertEqual(record["budget"]["no_progress_streak"], 0)

    def test_the_status_ledger_words_the_event(self):
        import status_ledger
        self.assertIn(state.REFUND_MIGRATED, status_ledger.RECORDED_KINDS)


if __name__ == "__main__":
    unittest.main()
