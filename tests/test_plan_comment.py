# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Only an exact verified Result becomes an idempotent issue comment."""
import unittest
from unittest.mock import patch

import plan_comment
from tests.test_plan_gate import plan, proposal, task


class TestPlanComment(unittest.TestCase):
    def result(self):
        return {"work_id": "w" * 64, "claim_id": "c" * 64, "artifact_id": 42,
                "digest": "sha256:" + "0" * 64}

    def task(self):
        value = task()
        value["tracking_issue"] = 12
        return value

    def test_render_is_readable_bounded_and_exact_result_marked(self):
        body = plan_comment.render(plan(), self.task(), self.result())
        self.assertIn("## Porting planning result", body)
        self.assertIn("### Research coverage", body)
        self.assertIn("Verified native Result", body)
        self.assertIn(plan_comment.marker(self.task(), self.result()), body)
        self.assertLess(len(body.encode()), 60_000)

    def test_the_project_and_the_path_coverage_are_rendered_bounded(self):
        value = plan()
        body = plan_comment.render(value, self.task(), self.result())
        self.assertIn("### Project", body)
        self.assertIn("- **Languages:** Python, C", body)
        self.assertIn("  - pypi `proj`: riscv64 absent (https://pypi.org/project/proj/)", body)
        self.assertIn("  - build: `.github/workflows/wheels.yml` (The wheel workflow owns", body)
        self.assertNotIn("{'area'", body)
        value["project"]["languages"] = ["<!-- x", "Go\nInjected"]
        value["project"]["release_channels"][0]["name"] = "a`b<!--"
        body = plan_comment.render(value, self.task(), self.result())
        self.assertNotIn("<!-- x", body)
        self.assertIn("&lt;!-- x, Go Injected", body)
        self.assertIn("`ab&lt;!--`", body)
        older = plan()
        del older["project"]
        self.assertNotIn("### Project", plan_comment.render(older, self.task(), self.result()))

    def test_trusted_worker_ref_is_accepted_without_weakening_workflow_identity(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "riseproject-dev/porting-agent",
               "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_WORKFLOW": "Planner",
               "GITHUB_REF": "refs/heads/work-queue"}
        self.assertTrue(plan_comment.trusted_runtime(env))
        env["GITHUB_WORKFLOW"] = "Untrusted workflow"
        self.assertFalse(plan_comment.trusted_runtime(env))
        env["GITHUB_WORKFLOW"] = "Controller"
        self.assertFalse(plan_comment.trusted_runtime(env))
        env["PORTING_PLAN_RECOVERY"] = "1"
        self.assertTrue(plan_comment.trusted_runtime(env))

    def test_missing_result_identity_is_rejected(self):
        result = self.result(); del result["claim_id"]
        with self.assertRaises(plan_comment.CommentError):
            plan_comment.publish(plan(), proposal(), self.task(), result, "token", "9")

    def test_new_comment_requires_app_author_but_existing_comment_is_not_rewritten(self):
        result = self.result(); body = plan_comment.render(plan(), self.task(), result)
        new_comment = {"id": 45, "body": body, "html_url": "https://github.com/x", "user": {"login": "riseproject-dev[bot]"}}
        def create(method, path, token, payload=None):
            return [] if path.endswith("?per_page=100") else new_comment
        with patch("plan_comment.request", side_effect=create):
            receipt = plan_comment.publish(plan(), proposal(), self.task(), result, "token", "99",
                                              "riseproject-dev[bot]")
        self.assertEqual(receipt["comment_id"], 45)
        old_comment = dict(new_comment, id=44, user={"login": "github-actions[bot]"})
        calls = []
        def existing(method, path, token, payload=None):
            calls.append(method)
            return [old_comment] if path.endswith("?per_page=100") else old_comment
        with patch("plan_comment.request", side_effect=existing):
            plan_comment.publish(plan(), proposal(), self.task(), result, "token", "99",
                                    "riseproject-dev[bot]")
        self.assertNotIn("POST", calls)

    def test_existing_exact_result_comment_is_reused(self):
        result = self.result()
        body = plan_comment.render(plan(), self.task(), result)
        comment = {"id": 44, "body": body,
                   "html_url": "https://github.com/riseproject-dev/porting-agent/issues/12#issuecomment-44"}
        calls = []
        def fake(method, path, token, payload=None):
            calls.append((method, path))
            return [comment] if path.endswith("?per_page=100") else comment
        with patch("plan_comment.request", side_effect=fake):
            receipt = plan_comment.publish(plan(), proposal(), self.task(), result, "token", "99")
        self.assertEqual(receipt["comment_id"], 44)
        self.assertFalse(any(method == "POST" for method, _ in calls))
