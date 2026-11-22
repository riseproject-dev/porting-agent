# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Terminal command reactions are durable, exact, and self-healing."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import command_reactions
from status_ledger import LedgerError


class FakeApi:
    def __init__(self, reactions=None, fail_posts=False):
        self.reactions = {key: list(value) for key, value in (reactions or {}).items()}
        self.calls = []
        self.fail_posts = fail_posts
        self.next_id = 100

    @staticmethod
    def comment_id(path):
        return int(path.split("/comments/")[1].split("/")[0])

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        comment_id = self.comment_id(path)
        if method == "GET":
            return list(self.reactions.get(comment_id, []))
        if method == "DELETE":
            reaction_id = int(path.rsplit("/", 1)[1])
            self.reactions[comment_id] = [
                item for item in self.reactions.get(comment_id, []) if item["id"] != reaction_id
            ]
            return None
        if self.fail_posts:
            raise LedgerError("injected reaction failure")
        item = {
            "id": self.next_id,
            "content": body["content"],
            "user": {"login": "riseproject-dev[bot]"},
        }
        self.next_id += 1
        self.reactions.setdefault(comment_id, []).append(item)
        return item


class TestCommandReactions(unittest.TestCase):
    def root(self, directory, *, result7="accepted"):
        root = Path(directory)
        commands = root / ".state/commands"
        commands.mkdir(parents=True)
        receipts = (
            {"command_id": "comment-7", "comment_id": 7, "result": result7},
            {"command_id": "comment-8", "comment_id": 8, "result": "rejected"},
        )
        for receipt in receipts:
            (commands / f"{receipt['command_id']}.json").write_text(json.dumps(receipt))
        return root

    def test_backfills_accepted_and_rejected_terminal_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeApi()
            with patch("command_reactions.Api", return_value=api):
                result = command_reactions.publish(self.root(directory), "token")
        self.assertEqual([item["status"] for item in result], ["created", "created"])
        self.assertEqual(api.reactions[7][0]["content"], "+1")
        self.assertEqual(api.reactions[8][0]["content"], "-1")

    def test_existing_app_completion_is_idempotent(self):
        existing = {
            7: [{"id": 1, "content": "+1", "user": {"login": "riseproject-dev[bot]"}}],
            8: [{"id": 2, "content": "-1", "user": {"login": "riseproject-dev[bot]"}}],
        }
        with tempfile.TemporaryDirectory() as directory:
            api = FakeApi(existing)
            with patch("command_reactions.Api", return_value=api):
                result = command_reactions.publish(self.root(directory), "token")
        self.assertEqual([item["status"] for item in result], ["existing", "existing"])
        self.assertFalse(any(call[0] in {"POST", "DELETE"} for call in api.calls))

    def test_rejected_result_replaces_an_earlier_success_reaction(self):
        existing = {
            7: [{"id": 1, "content": "+1", "user": {"login": "riseproject-dev[bot]"}}]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory, result7="rejected")
            api = FakeApi(existing)
            with patch("command_reactions.Api", return_value=api):
                result = command_reactions.publish(root, "token")
        self.assertEqual(result[0]["status"], "updated")
        self.assertEqual(
            [item["content"] for item in api.reactions[7] if item["user"]["login"].endswith("[bot]")],
            ["-1"],
        )

    def test_durable_receipt_recovers_after_projection_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            failing = FakeApi(fail_posts=True)
            with patch("command_reactions.Api", return_value=failing), patch(
                "command_reactions._retry", side_effect=lambda call: call()
            ), self.assertRaises(LedgerError):
                command_reactions.publish(root, "token")
            recovered = FakeApi()
            with patch("command_reactions.Api", return_value=recovered):
                result = command_reactions.publish(root, "token")
        self.assertEqual([item["status"] for item in result], ["created", "created"])


if __name__ == "__main__":
    unittest.main()
