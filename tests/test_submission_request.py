# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The tracking issue asks for `submit-upstream` at the submission gate, and
says what the command does."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import command_feedback
import commands
import compat
import engine
import state
import status_ledger
from tests.helpers import OWNER, SHA_A, awaiting_submission, policy
from tests.test_live_handoff_migration import HEAD, NOW, SUBMIT, live, load
from tests.test_status_ledger import FakeApi


class TestLedgerAsksForSubmission(unittest.TestCase):
    def render(self):
        record, event = compat.migrate_handoff_wait(live(), evidence=load("evidence-ci.json"),
                                                    policy=policy(), now=NOW)
        return status_ledger.render(dict(event, sequence=23), record, "https://github.com/run")

    def test_migration_entry_asks_for_the_exact_command(self):
        body = self.render()
        self.assertTrue(body.startswith("**Upstreaming r6 · handoff/awaiting_submission** -- "))
        visible, _, folded = body.partition("<details>")
        self.assertIn("an authorized human posts the command below on this issue", visible)
        self.assertIn(f"```text\n{SUBMIT}\n```", visible)
        self.assertIn(HEAD, visible)
        self.assertIn("`approve-handoff` no longer applies", visible)
        # The command is accepted: nothing says it is refused.
        self.assertNotIn("refuse", body)
        self.assertNotIn("changes nothing", body)
        self.assertIn(SUBMIT, folded)
        self.assertNotIn("/upstreaming approve-handoff", body)
        self.assertNotIn("/upstreaming abandon", body)
        self.assertNotIn(chr(0x2014), body)
        self.assertIn("<!-- upstreaming-ledger:closeio--ciso8601/1:23 -->", body)

    def test_the_request_is_bound_to_the_event_not_to_a_later_record(self):
        record, event = compat.migrate_handoff_wait(live(), evidence=load("evidence-ci.json"),
                                                    policy=policy(), now=NOW)
        later = dict(record, revision=9, fork=dict(record["fork"], head_sha="c" * 40))
        body = status_ledger.render(dict(event, sequence=23), later, "https://github.com/run")
        self.assertIn(f"```text\n{SUBMIT}\n```", body.partition("<details>")[0])

    def test_publish_posts_one_app_authored_entry_for_the_migration_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record, event = compat.migrate_handoff_wait(
                live(), evidence=load("evidence-ci.json"), policy=policy(), now=NOW)
            store = state.StateStore(root, policy())
            store.stage("contributions", record)
            event_path = store.stage("events", event)
            store.write_staged()
            api = FakeApi()
            with mock.patch("status_ledger.Api", return_value=api):
                receipts = status_ledger.publish(root, {"written": [event_path]}, "token",
                                                 "https://github.com/run")
            self.assertEqual([r["status"] for r in receipts], ["created"])
            self.assertIn("/repos/riseproject-dev/porting-agent/issues/1/comments",
                          [call[1] for call in api.calls if call[0] == "POST"])
            self.assertIn(SUBMIT, api.comment["body"])

    def test_command_feedback_for_the_retired_command(self):
        self.assertEqual(engine.HELD_COMMANDS, {})
        retired = {"comment_id": 901, "actor": OWNER, "verb": "", "revision": None,
                   "result": "rejected", "reason": commands.RETIRED["approve-handoff"]}
        body = command_feedback.render(
            retired, {"body": f"/upstreaming approve-handoff revision=6 head={HEAD}", "user": OWNER})
        self.assertIn("`approve-handoff` is retired", body)
        self.assertIn(f"**Expected form:**\n```text\n{SUBMIT}\n```", body)

    def test_nomination_entry_asks_for_the_exact_command(self):
        before = awaiting_submission(phase="development", activity="waiting_ci", revision=5,
                                     handoff_request=None, pending_human_action=None)
        out = engine.apply_transition(
            before, "nominate_for_handoff", policy=policy(), now=NOW, cause_id="ci-2-7-1",
            facts={"ci_passed_on_head"},
            values={"last_ci_evidence_id": "ci-2-7-1",
                    "handoff_request": {"command_id": "observation-ci-2-7-1", "head": SHA_A,
                                        "revision": 5}},
            details={"head": SHA_A, "revision": 5})
        body = status_ledger.render(dict(out.event, sequence=4), out.record, "https://github.com/run")
        visible = body.partition("<details>")[0]
        self.assertTrue(body.startswith("**Upstreaming r5 · handoff/awaiting_submission** -- "))
        self.assertIn(f"Required CI passed on the exact head `{SHA_A}`", visible)
        self.assertIn(f"```text\n/upstreaming submit-upstream revision=5 head={SHA_A}\n```", visible)
        self.assertNotIn("refuse", body)
        self.assertNotIn("approve-handoff", body)

    def test_entries_at_the_gate_offer_the_table_commands(self):
        record = awaiting_submission(revision=7)
        event = dict(engine.apply_transition(
            dict(record, paused=True, paused_at=NOW, paused_by="human", revision=6),
            "resume_pause", policy=policy(), now=NOW,
            command=commands.parse("/upstreaming resume revision=6 reason=r")).event, sequence=30)
        body = status_ledger.render(event, record, "https://github.com/run")
        folded = body.partition("<details>")[2]
        self.assertEqual(
            [line for line in folded.splitlines() if line.startswith("/upstreaming ")],
            ['/upstreaming pause revision=7 reason="..."',
             f"/upstreaming submit-upstream revision=7 head={SHA_A}",
             '/upstreaming refresh revision=7 reason="..."',
             '/upstreaming park revision=7 reason="..."'])
        self.assertNotIn("refused", folded)
        paused = dict(record, paused=True, paused_at=NOW, paused_by="human")
        folded = status_ledger.render(event, paused, "https://github.com/run").partition("<details>")[2]
        self.assertEqual(
            [line for line in folded.splitlines() if line.startswith("/upstreaming ")],
            ['/upstreaming resume revision=7 reason="..."',
             '/upstreaming refresh revision=7 reason="..."',
             '/upstreaming park revision=7 reason="..."'])


if __name__ == "__main__":
    unittest.main()
