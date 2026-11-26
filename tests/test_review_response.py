# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import review_response


class FakeApi:
    def __init__(self, existing=None, resolved=False):
        self.existing = existing or []
        self.resolved = resolved
        self.calls = []

    def call(self, method, path, data=None, attempts=5):
        self.calls.append((method, path, data))
        if method == "GET" and "/pulls/1/comments" in path:
            return list(self.existing)
        if method == "GET" and "/issues/1/comments" in path:
            return []
        return {}

    def graphql(self, query, variables):
        self.calls.append(("GRAPHQL", query, variables))
        if "reviewThreads" in query:
            return {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{
                "id": "PRRT_1", "isResolved": self.resolved,
                "comments": {"nodes": [{"databaseId": 20, "body": "change this"}]}
            }]}}}}
        self.resolved = True
        return {"resolveReviewThread": {"thread": {"id": "PRRT_1", "isResolved": True}}}


class TestReviewResponse(unittest.TestCase):
    def values(self):
        task = {"context": {"reason_evidence": [{"kind": "human_relay", "data": {
            "comments": [{"id": 20, "reviewer": "maintainer"}],
            "conversation_comments": [{"id": 30, "reviewer": "maintainer"}],
            "reviews": []}}]}}
        proposal = {"feedback_responses": [
            {"comment_id": 20, "summary": "Added riscv64 to the existing test matrix."},
            {"comment_id": 30, "summary": "Enabled the Test workflow for the pull request."}]}
        record = {"fork": {"name": "riseproject-upstreaming/proj", "pr_number": 1}}
        outcome = {"status": "published", "candidate": "a" * 40}
        return record, task, proposal, outcome

    def test_replies_then_resolves_inline_thread(self):
        api = FakeApi()
        actions = review_response.apply(api, record=self.values()[0], task=self.values()[1],
                                        proposal=self.values()[2], outcome=self.values()[3])
        self.assertEqual(actions, [{"comment_id": 20, "action": "replied"},
                                   {"comment_id": 20, "action": "resolved"},
                                   {"comment_id": 30, "action": "replied"}])
        reply = next(call for call in api.calls if "/comments/20/replies" in call[1])
        self.assertIn("@maintainer Added riscv64", reply[2]["body"])
        self.assertTrue(any(call[0] == "GRAPHQL" and "resolveReviewThread" in call[1] for call in api.calls))

    def test_marker_makes_reply_idempotent_but_unresolved_thread_is_retried(self):
        record, task, proposal, outcome = self.values()
        tag = review_response.marker(20, outcome["candidate"])
        api = FakeApi(existing=[{"body": tag}])
        actions = review_response.apply(api, record=record, task=task, proposal=proposal, outcome=outcome)
        self.assertNotIn(("POST", "/repos/riseproject-upstreaming/proj/pulls/1/comments/20/replies"),
                         [(method, path) for method, path, _ in api.calls])
        self.assertIn({"comment_id": 20, "action": "resolved"}, actions)

    def test_non_published_outcome_has_no_effect(self):
        record, task, proposal, outcome = self.values()
        api = FakeApi()
        self.assertEqual(review_response.apply(api, record=record, task=task, proposal=proposal,
                                               outcome={"status": "uncertain"}), [])
        self.assertEqual(api.calls, [])

    def test_a_record_in_upstream_review_is_not_answered(self):
        record, task, proposal, outcome = self.values()
        record["phase"] = "upstream_review"
        api = FakeApi()
        self.assertEqual(review_response.apply(api, record=record, task=task, proposal=proposal,
                                               outcome=outcome), [])
        self.assertEqual(api.calls, [])

    def test_comments_are_posted_once_without_a_retry(self):
        class Spy(FakeApi):
            attempts = []

            def call(self, method, path, data=None, attempts=5):
                if method == "POST":
                    self.attempts.append(attempts)
                return super().call(method, path, data)

        spy = Spy()
        review_response.apply(spy, record=self.values()[0], task=self.values()[1],
                              proposal=self.values()[2], outcome=self.values()[3])
        self.assertEqual(spy.attempts, [1, 1])

    def test_missing_responses_raise_before_anything_is_posted(self):
        record, task, proposal, outcome = self.values()
        proposal["feedback_responses"].pop()
        api = FakeApi()
        with self.assertRaises(review_response.ResponseError):
            review_response.apply(api, record=record, task=task, proposal=proposal, outcome=outcome)
        self.assertEqual(api.calls, [])

    def run_main(self, values, *, token="t", threads_missing=False):
        record, task, proposal, outcome = values
        with tempfile.TemporaryDirectory() as tmp:
            paths = {}
            for name, value in (("record", record), ("task", task), ("proposal", proposal), ("outcome", outcome)):
                paths[name] = Path(tmp) / f"{name}.json"
                paths[name].write_text(json.dumps(value))
            out = Path(tmp) / "out" / "feedback-responses.json"
            api = FakeApi()
            if threads_missing:
                api.graphql = lambda query, variables: (
                    {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}
                    if "reviewThreads" in query else {})
            env = {"UPSTREAMING_FORK_TOKEN": token} if token else {}
            argv = [f"--{name}={path}" for name, path in paths.items()] + [f"--out={out}"]
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(review_response, "Api", return_value=api):
                rc = review_response.main(argv)
            return rc, json.loads(out.read_text())

    def test_main_succeeds_and_records_the_actions(self):
        rc, written = self.run_main(self.values())
        self.assertEqual(rc, 0)
        self.assertEqual(len(written["actions"]), 3)
        self.assertNotIn("error", written)

    def test_main_never_raises_and_keeps_the_actions_taken_before_an_error(self):
        rc, written = self.run_main(self.values(), threads_missing=True)
        self.assertEqual(rc, 1)
        self.assertIn("was not found", written["error"])
        self.assertEqual(written["actions"], [{"comment_id": 20, "action": "replied"}])
        rc, written = self.run_main(self.values(), token="")
        self.assertEqual(rc, 1)
        self.assertEqual(written["actions"], [])
        self.assertIn("UPSTREAMING_FORK_TOKEN is missing", written["error"])


if __name__ == "__main__":
    unittest.main()
