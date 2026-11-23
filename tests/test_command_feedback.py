# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Rejected commands receive actionable App-authored tracking replies."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import command_feedback


class FakeApi:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.created = None
        self.methods = []

    def request(self, method, path, body=None):
        self.methods.append(method)
        if path.endswith("?per_page=100"):
            return self.existing
        if path.endswith("/issues/comments/42"):
            return {"id": 42, "body": "/upstreaming force-develop revision=1",
                    "user": {"login": "luhenry", "id": 660779}}
        if method in {"POST", "PATCH"}:
            comment_id = 99 if method == "POST" else int(path.rsplit("/", 1)[1])
            self.created = {"id": comment_id, "body": body["body"],
                            "user": {"login": "riseproject-dev[bot]"}}
            return self.created
        return self.created


class TestCommandFeedback(unittest.TestCase):
    def test_rejected_retired_command_shows_its_replacement(self):
        import commands
        for verb, replacement in command_feedback.REPLACEMENTS.items():
            with self.subTest(verb=verb):
                self.assertIn(verb, commands.RETIRED)
                body_text = f"/upstreaming {verb} revision=7 reason=r"
                try:
                    commands.parse(body_text)
                except commands.CommandError as error:
                    reason = str(error)
                receipt = {"comment_id": 42, "actor": {"login": "luhenry"}, "reason": reason}
                body = command_feedback.render(receipt, {"body": body_text,
                                                         "user": {"login": "luhenry"}})
                self.assertIn(f"`{verb}` is retired", body)
                form = " ".join(command_feedback.TEMPLATES[replacement].split()[:2])
                self.assertIn(f"{form} revision=7", body)
                self.assertNotIn(f"**Expected form:**\n```text\n/upstreaming {verb} ", body)
        self.assertEqual(set(command_feedback.REPLACEMENTS), set(commands.RETIRED))

    def test_repeated_pause_explains_that_pause_remains_effective(self):
        receipt = {"comment_id": 42, "actor": {"login": "luhenry"}, "verb": "pause",
                   "reason": "already paused", "revision": 8}
        original = {"body": '/upstreaming pause revision=8 reason="saving tokens"',
                    "user": {"login": "luhenry"}}
        body = command_feedback.render(receipt, original)
        self.assertIn("the contribution remains paused", body)
        self.assertIn('/upstreaming resume revision=8 reason="..."', body)
        self.assertNotIn("**Expected form:**", body)

    def test_rejected_command_is_quoted_pings_author_and_shows_expected_form(self):
        receipt = {"schema_version": 1, "command_id": "comment-42", "command_version": 1,
                   "comment_id": 42, "issue_number": 1138, "actor": {"id": 660779, "login": "luhenry"},
                   "received_at": "2026-11-20T00:00:00+00:00", "content_digest": "0" * 64,
                   "verb": "", "args": {}, "contribution_id": None, "revision": None,
                   "result": "rejected", "reason": "missing fields: class, paths, reason"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); directory = root / ".state/commands"; directory.mkdir(parents=True)
            (directory / "comment-42.json").write_text(json.dumps(receipt))
            api = FakeApi()
            with patch("command_feedback.Api", return_value=api):
                out = command_feedback.publish(root, "token")
        self.assertEqual(out[0]["status"], "created")
        self.assertIn("@luhenry, this command was rejected", api.created["body"])
        self.assertIn("> /upstreaming force-develop revision=1", api.created["body"])
        self.assertIn("missing fields: class, paths, reason", api.created["body"])
        self.assertIn("\n\n---\n\n**Reason:**", api.created["body"])
        self.assertIn("/upstreaming resume revision=1 class=configuration_integration",
                      api.created["body"])
        self.assertNotIn("Deprecated", api.created["body"])
        self.assertIn("<!-- upstreaming-command-feedback:42 -->", api.created["body"])

    def test_retry_edits_a_stale_marked_reply_instead_of_duplicating(self):
        receipt = {
            "schema_version": 1,
            "command_id": "comment-42",
            "command_version": 1,
            "comment_id": 42,
            "issue_number": 1138,
            "actor": {"id": 660779, "login": "luhenry"},
            "received_at": "2026-11-20T00:00:00+00:00",
            "content_digest": "0" * 64,
            "verb": "force-develop",
            "args": {},
            "contribution_id": None,
            "revision": 1,
            "result": "rejected",
            "reason": "updated deterministic reason",
        }
        existing = [{
            "id": 77,
            "body": "stale\n\n<!-- upstreaming-command-feedback:42 -->",
            "user": {"login": "riseproject-dev[bot]"},
        }]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipts = root / ".state/commands"
            receipts.mkdir(parents=True)
            (receipts / "comment-42.json").write_text(json.dumps(receipt))
            api = FakeApi(existing)
            with patch("command_feedback.Api", return_value=api):
                result = command_feedback.publish(root, "token")
        self.assertEqual(result[0], {"command_id": "comment-42", "comment_id": 77,
                                     "status": "updated"})
        self.assertIn("PATCH", api.methods)
        self.assertNotIn("POST", api.methods)
        self.assertIn("updated deterministic reason", api.created["body"])
