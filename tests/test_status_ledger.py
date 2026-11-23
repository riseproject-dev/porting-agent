# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Lifecycle issue ledger entries are concise, append-only and App-authored."""
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import compat
import lifecycle
import status_ledger
from tests.helpers import NOW, SHA_A, contribution


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
        self.assertIn("/upstreaming resume revision=4 class=configuration_integration", body)
        self.assertNotIn("force-develop", body)
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

    def test_gate_review_rows_render_what_they_did(self):
        import engine
        from tests.helpers import awaiting_submission, policy
        gate = awaiting_submission()
        explained = engine.apply_transition(
            gate, "record_explanation", policy=policy(), now=NOW, cause_id="inquiry-1",
            facts={"explanation_inquiry"},
            values={"processed_review_ids": [], "processed_review_comment_ids": [],
                    "processed_pr_conversation_comment_ids": [88]},
            details={"comments": [{"url": "https://github.com/o/r/pull/1#issuecomment-88",
                                   "body": "Why `riscv64` only? <!-- x -->"}]})
        body = status_ledger.render(dict(explained.event, sequence=24), explained.record,
                                    "https://github.com/run")
        self.assertTrue(body.startswith("**Upstreaming r0 · handoff/awaiting_submission**"))
        self.assertIn("explanation-only", body)
        self.assertIn("nothing is answered automatically", body)
        self.assertIn("> Why 'riscv64' only?", body)
        self.assertIn("([comment](https://github.com/o/r/pull/1#issuecomment-88))", body)
        self.assertEqual(body.count("<!--"), 1)   # only the ledger marker
        self.assertIn("/upstreaming submit-upstream revision=0", body)
        reviewed = engine.apply_transition(
            gate, "record_actionable_review", policy=policy(), now=NOW, cause_id="review-1",
            facts={"actionable_review", "candidate_head_not_failed"},
            values={"pending_feedback_evidence_ids": ["review-1"]})
        body = status_ledger.render(dict(reviewed.event, sequence=25), reviewed.record,
                                    "https://github.com/run")
        self.assertTrue(body.startswith("**Upstreaming r1 · development/fix_needed**"))
        self.assertIn("Recorded authorized change feedback", body)
        self.assertNotIn(chr(0x2014), body)

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

    def test_closed_ledger_offers_no_command(self):
        parked = contribution(phase="parked", activity="idle", revision=8, parked_reason="x")
        record = dict(parked, phase="closed", terminal_reason="abandoned")
        event = lifecycle._event(parked, record, "abandon", "command", "c", NOW, False)
        event["sequence"] = 9
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn("r8 · closed/idle", body)
        self.assertIn("Abandoned the parked contribution", body)
        self.assertNotIn("Possible next commands", body)
        self.assertNotIn("/upstreaming", body)

    def test_ledger_never_offers_a_retired_command(self):
        """In every admitted state, paused or not, the offered commands are
        commands of the grammar, never retired, and each parses."""
        import commands
        import engine
        states = sorted(set(lifecycle.LIFECYCLE) | compat.LEGACY_STATES)
        for state, paused in itertools.product(states, (False, True)):
            phase, activity = state.split(".")
            record = contribution(phase=phase, activity=activity, revision=3,
                                  clean_head_sha=SHA_A, paused=paused,
                                  handoff_request={"command_id": "x", "head": SHA_A,
                                                   "revision": 3})
            with self.subTest(state=state, paused=paused):
                for line in status_ledger._command_lines(record):
                    verb = line.split()[1]
                    self.assertNotIn(verb, commands.RETIRED)
                    self.assertIn(verb, commands.GRAMMAR)
                    parsed = commands.parse(line.replace("...", "x"))
                    if verb not in ("pause", "park", "resume") or state in lifecycle.LIFECYCLE:
                        # Every other offer is a command the table takes here.
                        self.assertTrue(engine.command_verbs(record, parsed), line)
                if state == "closed.idle" and not paused:
                    self.assertEqual(status_ledger._command_lines(record), [])

    def test_resume_with_an_admission_override_states_what_it_does(self):
        parked = contribution(phase="parked", activity="idle", revision=8, parked_reason="x")
        record = dict(parked, phase="queued", revision=9, admission_override={
            "class": "optimized_backend", "paths": ["setup.py"], "reason": "r"})
        event = lifecycle._event(parked, record, "restart_generation", "command", "c", NOW, True)
        event["sequence"] = 9
        event["details"] = {"admission_override": record["admission_override"]}
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn("The admission override is recorded (class `optimized_backend`, paths "
                      "`setup.py`)", body)
        self.assertIn("a dossier that decides park then admits development with the override's "
                      "class and paths", body)
        self.assertNotIn("force-develop", body)

    def test_the_dossier_admission_states_the_override_it_applied(self):
        record = contribution(phase="provisioning", activity="idle", revision=10, fork=None)
        before = dict(record, phase="investigation", activity="agent_running", revision=9)
        override = {"class": "optimized_backend", "paths": ["setup.py"], "reason": "r"}
        event = lifecycle._event(before, record, "admit_development", "result", "1", NOW, True)
        event["sequence"] = 10
        event["details"] = {"admission_override": override}
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn("the recorded admission override (class `optimized_backend`, paths "
                      "`setup.py`) admitted development instead", body)
        closed = contribution(phase="closed", activity="idle", terminal_reason="no_work")
        event = lifecycle._event(before, closed, "close_investigation", "result", "1", NOW, False)
        event["sequence"] = 11
        event["details"] = {"admission_override_not_applied": override}
        body = status_ledger.render(event, closed, "https://github.com/run")
        self.assertIn("closed the contribution", body)
        self.assertIn("The recorded admission override was not applied", body)

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
