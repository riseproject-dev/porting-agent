# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The tracking issue asks for `submit-upstream` at the submission gate, and
says what the command does."""

import unittest

import command_feedback
import commands
import engine
import status_ledger
from tests.helpers import OWNER, SHA_A, awaiting_submission, policy
from tests.test_live_handoff_migration import HEAD, NOW, SUBMIT


class TestLedgerAsksForSubmission(unittest.TestCase):
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
