# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Lifecycle issue ledger entries are concise, append-only and App-authored."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import compat
import lifecycle
import status_ledger
from tests.helpers import NOW, contribution


class FakeApi:
    def __init__(self):
        self.calls = []
        self.comment = None

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("?per_page=100"):
            return []
        if method == "POST":
            self.comment = {"id": 7, "body": body["body"], "html_url": "https://github.com/x",
                            "user": {"login": "riseproject-dev[bot]"}}
        return self.comment


class TestStatusLedger(unittest.TestCase):
    def records(self, root: Path):
        old = contribution(phase="queued", activity="idle", revision=0)
        new = dict(old, phase="parked", activity="idle", revision=1, parked_reason="No patch", updated_at=NOW)
        event = lifecycle._event(old, new, "dossier_parked", "observation", "dossier-1", NOW, True)
        event["sequence"] = 3
        cpath = root / ".state/contributions/example--proj--12.json"
        epath = root / ".state/events/example--proj--12/00000003.json"
        cpath.parent.mkdir(parents=True); epath.parent.mkdir(parents=True)
        cpath.write_text(json.dumps(new)); epath.write_text(json.dumps(event))
        return new, event, str(epath.relative_to(root))

    def test_render_has_short_summary_and_folded_prefilled_commands(self):
        record = contribution(phase="parked", activity="idle", revision=4, parked_reason="No patch")
        event = lifecycle._event(record, record, "dossier_parked", "observation", "d", NOW, False)
        event["sequence"] = 2
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertTrue(body.startswith("**Upstreaming r4 · parked/idle**"))
        self.assertIn("<details>", body)
        self.assertIn("<summary>Possible next commands</summary>", body)
        self.assertIn("/upstreaming force-develop revision=4", body)
        self.assertIn("<!-- upstreaming-ledger:", body)

    def test_paused_feedback_ledger_does_not_claim_work_was_requested(self):
        record = contribution(phase="development", activity="fix_needed", revision=8, paused=True)
        event = lifecycle._event(record, record, "development_review_feedback", "observation",
                                 "review-1", NOW, True)
        event["sequence"] = 14
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertTrue(body.startswith("**Upstreaming r8 · development/paused**"))
        self.assertIn("Repair remains pending", body)
        self.assertIn("no Work was requested because the contribution is paused", body)
        self.assertNotIn("and requested repair", body)
        self.assertNotIn("/upstreaming pause revision=8", body)
        self.assertIn("/upstreaming resume revision=8", body)

    def test_pause_event_explains_retained_pending_state(self):
        record = contribution(phase="development", activity="fix_needed", revision=10, paused=True)
        event = lifecycle._event(record, record, "command:pause", "command", "comment-1", NOW, False)
        event["sequence"] = 15
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertTrue(body.startswith("**Upstreaming r10 · development/paused**"))
        self.assertIn("Paused automatic work", body)
        self.assertIn("Pending lifecycle activity `fix needed` is retained until resume", body)

    def test_unpaused_feedback_ledger_reports_repair_request(self):
        record = contribution(phase="development", activity="fix_needed", revision=8, paused=False)
        event = lifecycle._event(record, record, "development_review_feedback", "observation",
                                 "review-1", NOW, True)
        event["sequence"] = 14
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn("change feedback and requested repair", body)

    def test_explanation_only_ledger_does_not_claim_repair(self):
        record = contribution(phase="development", activity="fix_needed", revision=8, paused=True)
        event = lifecycle._event(record, record, "development_review_inquiry", "observation",
                                 "inquiry-1", NOW, False)
        event["sequence"] = 14
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn("explanation-only", body)
        self.assertIn("No repair was requested", body)
        self.assertNotIn("requested repair", body)

    def test_development_pr_ledger_offers_dossier_refresh(self):
        record = contribution(phase="development", activity="fix_needed", revision=7)
        event = lifecycle._event(record, record, "activity", "observation", None, NOW, False)
        event["sequence"] = 8
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn('/upstreaming refresh revision=7 reason="..."', body)

    def test_repair_exhaustion_park_renders_and_offers_resume(self):
        before = contribution(phase="development", activity="fix_needed", revision=7)
        record = compat.park(dict(before, revision=8), "repair-attempt budget exhausted")
        event = lifecycle._event(before, record, "repair_attempts_exhausted", "result", "1", NOW, True)
        event["sequence"] = 9
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn("r8 · parked/idle", body)
        self.assertIn("Exhausted the bounded repair-attempt budget and parked the contribution", body)
        self.assertIn('/upstreaming resume revision=8 reason="..."', body)

    def test_abandoned_ledger_offers_same_issue_restart(self):
        record = contribution(phase="abandoned", activity="idle", revision=8,
                              terminal_reason="restart requested")
        event = lifecycle._event(record, record, "command:abandon", "command", "c", NOW, False)
        event["sequence"] = 9
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn('/upstreaming restart revision=8 reason="..."', body)
        self.assertNotIn("/upstreaming resume", body)

    def test_publish_appends_one_app_authored_comment_for_written_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); _record, _event, path = self.records(root); api = FakeApi()
            with patch("status_ledger.Api", return_value=api):
                receipts = status_ledger.publish(root, {"written": [path]}, "token", "https://github.com/run")
            self.assertEqual(receipts[0]["status"], "created")
            self.assertEqual([call[0] for call in api.calls], ["GET", "POST", "GET"])
            self.assertIn("Upstreaming r1", api.comment["body"])

    def test_next_tick_backfills_event_after_post_commit_projection_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.records(root)
            api = FakeApi()
            with patch("status_ledger.Api", return_value=api):
                receipts = status_ledger.publish(
                    root, {"written": []}, "token", "https://github.com/run-2"
                )
        self.assertEqual(receipts[0]["status"], "created")
        self.assertIn("run-2", api.comment["body"])
