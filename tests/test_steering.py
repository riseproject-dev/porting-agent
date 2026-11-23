# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Human steering: who steers, what is recorded, and that it is append-only."""

from __future__ import annotations

import copy
import unittest

import collect
import steering
from common import CONTROL_REPO, sha256_text
from tests.helpers import NOW, OWNER, STRANGER, contribution, policy

POLICY = policy()
MAINTAINER = {"id": 4242, "login": "maintainer"}
BOT = {"id": 77, "login": "ci[bot]", "type": "Bot"}


def comment(cid, user, body, *, at="2026-11-20T10:00:00Z", updated=None, association="NONE",
            **extra):
    return {"id": cid, "user": dict(user), "body": body, "created_at": at,
            "updated_at": updated or at, "html_url": f"https://github.com/x/y/issues/1#c{cid}",
            "author_association": association, **extra}


class Api:
    def __init__(self, lists=None):
        self.lists = lists or {}
        self.paths = []

    def paginate(self, path, **kwargs):
        self.paths.append((path, kwargs.get("max_pages")))
        return copy.deepcopy(self.lists.get(path.split("?")[0], []))


def record(**over):
    rec = contribution(phase="development", activity="fix_needed")
    rec["fork"]["pr_number"] = 5
    rec.update(over)
    return rec


class TestCollect(unittest.TestCase):
    def test_only_the_command_authority_steers(self):
        rec = record()
        issue_comments = f"/repos/{CONTROL_REPO}/issues/{rec['tracking_issue']['number']}/comments"
        api = Api({issue_comments: [comment(1, OWNER, "Please keep the QEMU topology."),
                                    comment(2, STRANGER, "Do something else."),
                                    comment(3, OWNER, "   ")]})
        out = collect.steering(api, rec, [OWNER["id"]], issue=None)
        self.assertEqual([(i["id"], i["authority"]) for i in out["items"]], [(1, True)])
        self.assertEqual(out["omitted"], {"tracking_issue_comment": 2})
        self.assertTrue(all(pages == collect.STEERING_PAGES for _p, pages in api.paths))

    def test_upstream_maintainers_are_feedback_not_steering(self):
        rec = record(phase="upstream_review", activity="fix_needed",
                     upstream_pr={"number": 9, "head_sha": "a" * 40})
        base = f"/repos/{rec['upstream']['full_name']}"
        api = Api({f"{base}/pulls/9/reviews": [
                       dict(comment(10, MAINTAINER, "Please add a changelog entry.",
                                    association="MEMBER", state="CHANGES_REQUESTED"),
                            submitted_at="2026-11-21T00:00:00Z")],
                   f"{base}/issues/9/comments": [
                       comment(11, STRANGER, "+1", association="NONE"),
                       comment(12, BOT, "coverage report", association="MEMBER")],
                   f"{base}/pulls/9/comments": [
                       comment(13, OWNER, "Rename this job.", path="ci.yml", line=4,
                               in_reply_to_id=10, commit_id="a" * 40)]})
        out = collect.steering(api, rec, [OWNER["id"]], issue=None)
        items = {i["id"]: i for i in out["items"]}
        self.assertEqual((items[10]["authority"], items[10]["source"], items[10]["review_state"],
                          items[10]["created_at"]),
                         (False, "upstream_pr_review", "CHANGES_REQUESTED", "2026-11-21T00:00:00Z"))
        self.assertEqual((items[13]["authority"], items[13]["path"], items[13]["line"],
                          items[13]["in_reply_to"]), (True, "ci.yml", 4, 10))
        self.assertEqual(sorted(items), [10, 13])
        self.assertEqual(out["omitted"], {"upstream_pr_comment": 2})

    def test_an_edited_description_steers_only_from_an_authorized_author(self):
        rec = record()
        rec["tracking_issue"].update(description="The original request.",
                                     description_digest=sha256_text("The original request."))
        issue = {"id": rec["tracking_issue"]["id"], "number": rec["tracking_issue"]["number"],
                 "body": "Please target the 2.x branch too.", "user": dict(OWNER),
                 "created_at": "2026-11-18T00:00:00Z", "updated_at": "2026-11-22T00:00:00Z",
                 "html_url": "https://github.com/x/y/issues/12"}
        out = collect.steering(Api(), rec, [OWNER["id"]], issue=issue)
        [item] = out["items"]
        self.assertEqual((item["source"], item["version"]),
                         ("tracking_issue_description", sha256_text(issue["body"])))
        out = collect.steering(Api(), rec, [OWNER["id"]], issue=dict(issue, user=dict(STRANGER)))
        self.assertEqual((out["items"], out["omitted"]), ([], {"tracking_issue_description": 1}))
        # The frozen description itself is not steering.
        frozen = dict(issue, body=rec["tracking_issue"]["description"])
        self.assertEqual(collect.steering(Api(), rec, [OWNER["id"]], issue=frozen)["items"], [])

    def test_a_failed_read_raises(self):
        class Failing(Api):
            def paginate(self, path, **kwargs):
                raise collect.CollectError("GET: HTTP 502")

        with self.assertRaises(collect.CollectError):
            collect.steering(Failing(), record(), [OWNER["id"]], issue=None)


def item(cid, *, body="Keep the QEMU row.", updated=None, source="tracking_issue_comment",
         authority=True):
    return {"source": source, "id": cid, "author": "owner", "author_id": OWNER["id"],
            "authority": authority, "created_at": f"2026-11-2{cid % 10}T00:00:00Z",
            "updated_at": updated or f"2026-11-2{cid % 10}T00:00:00Z",
            "url": f"https://github.com/x/y/issues/1#c{cid}", "body": body}


class TestEvidence(unittest.TestCase):
    def test_only_new_items_are_recorded(self):
        rec = record()
        first = steering.new_evidence(rec, {"items": [item(1), item(2)], "omitted": {"x": 3}},
                                      {}, policy=POLICY, now=NOW)
        self.assertEqual([i["id"] for i in first["data"]["items"]], [1, 2])
        self.assertEqual(first["data"]["omitted"], {"x": 3})
        known = {first["evidence_id"]: first}
        self.assertIsNone(steering.new_evidence(rec, {"items": [item(1), item(2)]}, known,
                                                policy=POLICY, now=NOW))
        second = steering.new_evidence(rec, {"items": [item(1), item(2), item(3)]}, known,
                                       policy=POLICY, now=NOW)
        self.assertEqual([i["id"] for i in second["data"]["items"]], [3])
        self.assertNotEqual(first["evidence_id"], second["evidence_id"])

    def test_an_edit_is_a_new_version_and_a_deletion_keeps_the_item(self):
        rec = record()
        first = steering.new_evidence(rec, {"items": [item(1), item(2)]}, {}, policy=POLICY,
                                      now=NOW)
        known = {first["evidence_id"]: first}
        edited = steering.new_evidence(
            rec, {"items": [item(1, body="Keep it, and add musllinux.",
                                 updated="2026-11-29T00:00:00Z")]},
            known, policy=POLICY, now=NOW)
        [version] = edited["data"]["items"]
        self.assertEqual((version["id"], version["supersedes"]),
                         (1, "2026-11-21T00:00:00Z"))
        known[edited["evidence_id"]] = edited
        # Comment 2 was deleted: nothing is new, and the record keeps it.
        self.assertIsNone(steering.new_evidence(rec, {"items": []}, known, policy=POLICY,
                                                now=NOW))
        self.assertEqual(sorted({(i["id"], i["version"]) for i in
                                 steering.recorded(rec["contribution_id"], known)}),
                         [(1, "2026-11-21T00:00:00Z"), (1, "2026-11-29T00:00:00Z"),
                          (2, "2026-11-22T00:00:00Z")])

    def test_bodies_are_bounded_and_cleaned(self):
        rec = record()
        token = "ghp_" + "Z" * 36
        out = steering.new_evidence(rec, {"items": [item(1, body=f"{token} " + "y" * 9000)]},
                                    {}, policy=POLICY, now=NOW)
        body = out["data"]["items"][0]["body"]
        self.assertNotIn(token, body)
        self.assertIn("[truncated:", body)
        self.assertLess(len(body), steering.MAX_BODY + 60)

    def test_many_items_are_recorded_in_bounded_records(self):
        rec = record()
        many = [item(i, body="z" * 3900) for i in range(1, 300)]
        out = steering.new_evidence(rec, {"items": many}, {}, policy=POLICY, now=NOW)
        self.assertGreater(out["data"]["deferred"], 0)
        self.assertLessEqual(len(out["data"]["items"]), steering.MAX_ITEMS)
        import json
        self.assertLess(len(json.dumps(out)), POLICY["storage"]["max_record_bytes"])

    def test_steering_changes_no_record_field(self):
        rec = record()
        before = copy.deepcopy(rec)
        steering.new_evidence(rec, {"items": [item(1)]}, {}, policy=POLICY, now=NOW)
        self.assertEqual(rec, before)


if __name__ == "__main__":
    unittest.main()
