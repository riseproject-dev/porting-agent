# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Records written while patches and submission rewrites were reviewed by
a separate task: the record migration moves them out of the removed states and
drops the retired fields, instead of refusing the whole store."""

import json
import tempfile
import unittest
from pathlib import Path

import engine
import state
from common import canonical_json
from tests.helpers import policy

LIVE = Path(__file__).parent / "fixtures" / "live-gate-dev" / "contribution.json"
NOW = "2026-11-23T09:00:00+00:00"
WORK = {"dedupe_key": "closeio--ciso8601/1@g0.r7:submission_review", "task": "submission_review",
        "generation": 0, "revision": 7, "attempt": 1, "requested_at": NOW, "deadline_at": NOW,
        "dispatched_run_id": None, "reason_ids": [], "feedback_evidence_ids": []}


def live():
    return json.loads(LIVE.read_text())


def old_handoff(activity, **over):
    record = state.migrate_record_fields(live(), now=NOW)[0]
    record.update(activity=activity, pending_human_action=None, submission_review_id="review-1",
                  submission_rewrite_id="rewrite-1", **over)
    return record


class TestRetiredStates(unittest.TestCase):
    def migrate(self, record):
        self.assertTrue(state.needs_field_migration(record))
        new, event = state.migrate_record_fields(record, now=NOW)
        self.assertIsNone(engine.state_problem(new))
        self.assertFalse(state.needs_field_migration(new))
        return new, event

    def test_the_handoff_reviews_go_back_to_the_rewrite(self):
        for activity in ("review_pending", "reviewing"):
            with self.subTest(activity=activity):
                work = dict(WORK) if activity == "reviewing" else None
                new, event = self.migrate(old_handoff(activity, active_work=work))
                self.assertEqual(engine.state_key(new), "handoff.review_due")
                self.assertEqual((new["active_work"], new["submission_rewrite_id"]), (None, None))
                self.assertNotIn("submission_review_id", new)
                self.assertEqual((event["details"]["from_state"], event["details"]["to_state"]),
                                 (f"handoff.{activity}", "handoff.review_due"))
                self.assertIn("submission_review_id", event["details"]["removed"])

    def test_a_later_handoff_record_only_drops_the_review_id(self):
        """`submission_review_id` stays set from the accepted review until the
        handoff ends: such a record must not stop the store."""
        for activity in ("building_upstream_branch", "waiting_ci"):
            with self.subTest(activity=activity):
                new, event = self.migrate(old_handoff(activity))
                self.assertEqual(engine.state_key(new), f"handoff.{activity}")
                self.assertEqual(new["submission_rewrite_id"], "rewrite-1")
                self.assertEqual(event["details"]["removed"], ["submission_review_id"])

    def test_held_patch_and_review_comments_are_dropped_whatever_they_hold(self):
        record = state.migrate_record_fields(live(), now=NOW)[0]
        record.update(held_patch={"candidate": "c" * 40}, review_comments=[{"id": 1}])
        new, event = self.migrate(record)
        self.assertNotIn("held_patch", new)
        self.assertNotIn("review_comments", new)
        self.assertEqual(sorted(event["details"]["removed"]), ["held_patch", "review_comments"])

    def test_the_development_reviews_go_back_to_a_patch(self):
        for activity in ("review_pending", "reviewing", "publishing"):
            with self.subTest(activity=activity):
                record = state.migrate_record_fields(live(), now=NOW)[0]
                record.update(phase="development", activity=activity, handoff_approval=None,
                              pending_human_action=None, clean_head_sha=None,
                              held_patch={"candidate": "c" * 40}, submission_rewrite_id=None,
                              active_work=dict(WORK, task="patch_review") if activity == "reviewing"
                              else None)
                new, event = self.migrate(record)
                self.assertEqual(engine.state_key(new), "development.fix_needed")
                self.assertEqual(new["active_work"], None)
                self.assertNotIn("held_patch", new)
                self.assertEqual(event["details"]["from_state"], f"development.{activity}")

    def test_a_publishing_record_with_its_held_result_is_requested_again(self):
        """Every old `development.publishing` entry held the Result of its
        publication: it is dropped with the patch, or nothing would ever run
        in `development.fix_needed` for it."""
        from datetime import datetime

        import capacity
        import reconcile

        record = state.migrate_record_fields(live(), now=NOW)[0]
        record.update(phase="development", activity="publishing", handoff_approval=None,
                      pending_human_action=None, clean_head_sha=None, submission_rewrite_id=None,
                      held_patch={"candidate": "c" * 40}, active_work=None,
                      pending_publication={"worker_run_id": "777", "attempts": 1,
                                           "last_reason": "infra", "next_attempt_at": NOW})
        new, event = self.migrate(record)
        self.assertEqual(engine.state_key(new), "development.fix_needed")
        self.assertIsNone(new["pending_publication"])
        self.assertIn("pending_publication", event["details"]["removed"])
        fixtures = Path(__file__).parent / "fixtures" / "live-handoff-r6"
        evidence = {item["evidence_id"]: item for item in (
            json.loads((fixtures / name).read_text()) for name in ("evidence-ci.json", "plan.json"))}
        snap = reconcile.Snapshot(contributions={"c": new}, operations={}, consumed={},
                                  projects={}, evidence=evidence)
        out = reconcile.tick(None, snap, policy=policy(),
                             now=datetime.fromisoformat("2026-11-23T10:00:00+00:00"),
                             publications=[], pressure=capacity.Pressure(ok=True), observe=False)
        kinds = [e["kind"] for k, e in out.staged if k == "events"]
        self.assertIn("request_patch", kinds)

    def test_fix_needed_never_holds_a_publication(self):
        import lifecycle as lc

        record = state.migrate_record_fields(live(), now=NOW)[0]
        record.update(phase="development", activity="fix_needed", handoff_approval=None,
                      clean_head_sha=None, submission_rewrite_id=None, active_work=None)
        self.assertIsNone(engine.state_problem(record))
        self.assertIn("holds the publication",
                      engine.state_problem(dict(record, pending_publication={"worker_run_id": "7"})))
        # Every row that leaves a running task for a state that requests a task
        # again drops the held Result with it.
        for edge in lc.table_edges():
            if edge.source in ("development.agent_running", "upstream_review.agent_running") \
                    and edge.target.endswith(".fix_needed"):
                self.assertIn(lc.HELD_RESULT, edge.transition.effects.clears,
                              f"{edge.source} --{edge.verb}--> {edge.target}")

    def test_a_review_that_could_not_run_becomes_a_failed_task(self):
        record = state.migrate_record_fields(live(), now=NOW)[0]
        record.update(phase="development", activity="waiting_human", handoff_approval=None,
                      clean_head_sha=None, submission_rewrite_id=None,
                      held_patch={"candidate": "c" * 40},
                      pending_human_action={"kind": "patch_review_unavailable", "requested_at": NOW,
                                            "reminded_at": None, "escalated_at": None,
                                            "counter": "work_recovery"})
        new, event = self.migrate(record)
        self.assertEqual(new["pending_human_action"]["kind"], "work_failed")
        self.assertIn("pending_human_action.kind patch_review_unavailable",
                      event["details"]["removed"])

    def test_the_store_is_migrated_not_refused(self):
        record = state.migrate_record_fields(live(), now=NOW)[0]
        record.update(phase="development", activity="reviewing", handoff_approval=None,
                      clean_head_sha=None, submission_rewrite_id=None,
                      held_patch={"candidate": "c" * 40}, active_work=dict(WORK))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".state/contributions/closeio--ciso8601--1.json"
            path.parent.mkdir(parents=True)
            path.write_text(canonical_json(record))
            pending = state.pending_record_migrations(Path(tmp), policy())
        self.assertEqual(pending, [".state/contributions/closeio--ciso8601--1.json"])


if __name__ == "__main__":
    unittest.main()
