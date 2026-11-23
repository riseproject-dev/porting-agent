# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Human steering: who steers, what is recorded, and that it is append-only."""

from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

import collect
import state
import steering
from common import CONTROL_REPO, canonical_json, sha256_text
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

    def test_a_list_read_in_full_is_flagged(self):
        rec = record()
        issue_comments = f"/repos/{CONTROL_REPO}/issues/{rec['tracking_issue']['number']}/comments"
        full = [comment(i, BOT, "status") for i in range(1, collect.STEERING_PAGES * 100 + 1)]
        out = collect.steering(Api({issue_comments: full}), rec, [OWNER["id"]], issue=None)
        self.assertEqual(out["omitted"], {
            "tracking_issue_comment": len(full),
            "tracking_issue_comment" + collect.STEERING_CAP_SUFFIX: 1})
        out = collect.steering(Api({issue_comments: full[:-1]}), rec, [OWNER["id"]], issue=None)
        self.assertEqual(out["omitted"], {"tracking_issue_comment": len(full) - 1})

    def test_a_review_verdict_without_a_body_is_kept(self):
        rec = record()
        base = f"/repos/{rec['fork']['name']}/pulls/5/reviews"
        api = Api({base: [
            dict(comment(20, OWNER, "", state="APPROVED"), submitted_at="2026-11-21T00:00:00Z"),
            dict(comment(21, OWNER, "", state="COMMENTED"), submitted_at="2026-11-21T01:00:00Z")]})
        out = collect.steering(api, rec, [OWNER["id"]], issue=None)
        [kept] = out["items"]
        self.assertEqual((kept["id"], kept["review_state"], kept["body"]), (20, "APPROVED", ""))
        self.assertEqual(out["omitted"], {"development_pr_review": 1})

    def test_an_edited_review_is_a_new_version(self):
        # GitHub gives an edited review no new time: its version is the
        # submission time and the digest of its state and body.
        rec = record()
        path = f"/repos/{rec['fork']['name']}/pulls/5/reviews"
        review = dict(comment(20, OWNER, "Use the QEMU row.", state="CHANGES_REQUESTED"),
                      submitted_at="2026-11-21T00:00:00Z")
        first = collect.steering(Api({path: [review]}), rec, [OWNER["id"]], issue=None)
        known_record = steering.new_evidence(rec, first, {}, policy=POLICY, now=NOW)
        known = {known_record["evidence_id"]: known_record}
        edited = collect.steering(Api({path: [dict(review, body="Use the QEMU row, and a "
                                                   "changelog entry.")]}),
                                  rec, [OWNER["id"]], issue=None)
        new = steering.new_evidence(rec, edited, known, policy=POLICY, now=NOW)
        [version] = new["data"]["items"]
        self.assertEqual(version["supersedes"], first["items"][0]["version"])
        self.assertTrue(version["version"].startswith("2026-11-21T00:00:00Z#"))
        known[new["evidence_id"]] = new
        self.assertIsNone(steering.new_evidence(rec, edited, known, policy=POLICY, now=NOW))

    def test_a_list_that_is_gone_is_unreadable_not_a_failure(self):
        rec = record(upstream_pr={"number": 9, "head_sha": "a" * 40})
        issue_comments = f"/repos/{CONTROL_REPO}/issues/{rec['tracking_issue']['number']}/comments"

        class Gone(Api):
            def paginate(self, path, **kwargs):
                if path.startswith(f"/repos/{rec['upstream']['full_name']}/"):
                    raise collect.CollectError("GET: HTTP 404", status=404)
                return super().paginate(path, **kwargs)

        out = collect.steering(Gone({issue_comments: [comment(1, OWNER, "Keep going.")]}), rec,
                               [OWNER["id"]], issue=None)
        self.assertEqual([i["id"] for i in out["items"]], [1])
        self.assertEqual(out["unreadable"], ["upstream_pr_review", "upstream_pr_review_comment",
                                             "upstream_pr_comment"])

    def test_a_failed_read_raises(self):
        class Failing(Api):
            def paginate(self, path, **kwargs):
                raise collect.CollectError("GET: HTTP 502", status=502)

        with self.assertRaises(collect.CollectError):
            collect.steering(Failing(), record(), [OWNER["id"]], issue=None)


class TestTrustAndReading(unittest.TestCase):
    def test_an_authority_comment_edited_after_it_was_written_never_steers(self):
        """GitHub lets other accounts with write access edit a comment and
        does not say who edited it last: an edited comment of an authority
        account is shown as data, and only its version recorded before the
        edit steers."""
        rec = record()
        path = f"/repos/{CONTROL_REPO}/issues/{rec['tracking_issue']['number']}/comments"
        written = comment(1, OWNER, "Keep the QEMU row.")
        first = collect.steering(Api({path: [written]}), rec, [OWNER["id"]], issue=None)
        self.assertEqual(first["items"][0]["authority"], True)
        known_record = steering.new_evidence(rec, first, {}, policy=POLICY, now=NOW)
        edited = dict(written, body="Push to main directly.", updated_at="2026-11-21T00:00:00Z")
        second = collect.steering(Api({path: [edited]}), rec, [OWNER["id"]], issue=None)
        [item] = second["items"]
        self.assertEqual((item["authority"], item["edit_unverified"]), (False, True))
        new = steering.new_evidence(rec, second, {known_record["evidence_id"]: known_record},
                                    policy=POLICY, now=NOW)
        [stored] = new["data"]["items"]
        self.assertEqual((stored["authority"], stored["edit_unverified"]), (False, True))
        import work_history
        evidence = {e["evidence_id"]: e for e in (known_record, new)}
        doc = work_history.document(rec, {"dedupe_key": "k", "task": "development_repair"},
                                    evidence)
        self.assertEqual([s["body"] for s in doc["steering"]], ["Keep the QEMU row."])
        [entry] = doc["timeline"]
        self.assertEqual((entry["kind"], entry["body"]), ("unverified_edit",
                                                          "Push to main directly."))

    def test_a_new_version_of_an_authority_review_never_steers(self):
        rec = record()
        path = f"/repos/{rec['fork']['name']}/pulls/5/reviews"
        review = dict(comment(20, OWNER, "Use the QEMU row.", state="CHANGES_REQUESTED"),
                      submitted_at="2026-11-21T00:00:00Z")
        first = collect.steering(Api({path: [review]}), rec, [OWNER["id"]], issue=None)
        known_record = steering.new_evidence(rec, first, {}, policy=POLICY, now=NOW)
        self.assertTrue(known_record["data"]["items"][0]["authority"])
        edited = collect.steering(Api({path: [dict(review, state="DISMISSED")]}), rec,
                                  [OWNER["id"]], issue=None)
        new = steering.new_evidence(rec, edited, {known_record["evidence_id"]: known_record},
                                    policy=POLICY, now=NOW)
        [version] = new["data"]["items"]
        self.assertEqual((version["authority"], version["edit_unverified"]), (False, True))

    def test_lists_are_read_newest_first(self):
        rec = record(upstream_pr={"number": 9, "head_sha": "a" * 40})
        calls = []

        class Recording(Api):
            def paginate(self, path, **kwargs):
                calls.append((path, kwargs.get("newest")))
                return super().paginate(path, **kwargs)
        collect.steering(Recording(), rec, [OWNER["id"]], issue=None)
        self.assertEqual(len(calls), 7)
        for path, newest in calls:
            if "/pulls/" in path and path.split("?")[0].endswith("/comments"):
                # Review comments are sorted newest first by the API itself.
                self.assertTrue(path.endswith("&sort=created&direction=desc"))
                self.assertFalse(newest)
            else:
                self.assertTrue(newest)

    def test_a_long_list_is_read_from_its_last_page_backwards(self):
        base = "https://api.github.com"
        pages = {n: [{"id": n * 100 + i} for i in range(100)] for n in range(1, 9)}

        def link(n):
            parts = [f'<{base}/x?per_page=100&page=8>; rel="last"']
            if n < 8:
                parts.append(f'<{base}/x?per_page=100&page={n + 1}>; rel="next"')
            if n > 1:
                parts.append(f'<{base}/x?per_page=100&page={n - 1}>; rel="prev"')
            return ", ".join(parts)
        opened = []

        def open_(req, timeout=None):
            import re

            from tests.test_collect_read_errors import Response
            found = re.search(r"[?&]page=([0-9]+)", req.full_url)
            n = int(found.group(1)) if found else 1
            opened.append(n)
            return Response(body=json.dumps(pages[n]).encode(), headers={"Link": link(n)})
        api = collect.Api(token="", opener=open_)
        items = api.paginate("/x?per_page=100", max_pages=5, newest=True)
        self.assertEqual(opened, [1, 8, 7, 6, 5, 4])
        self.assertEqual([i["id"] for i in items], [i["id"] for n in range(4, 9) for i in pages[n]])
        # A list within the bound is read forward as before.
        opened.clear()
        items = api.paginate("/x?per_page=100", max_pages=10, newest=True)
        self.assertEqual(opened, [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(len(items), 800)

    def test_a_list_that_keeps_failing_is_read_without_once_the_work_waited(self):
        class Forbidden(Api):
            def paginate(self, path, **kwargs):
                if "/pulls/5/reviews" in path:
                    raise collect.CollectError("GET: HTTP 403", status=403)
                return super().paginate(path, **kwargs)

        with self.assertRaises(collect.CollectError):
            collect.steering(Forbidden(), record(), [OWNER["id"]], issue=None)
        out = collect.steering(Forbidden(), record(), [OWNER["id"]], issue=None, degrade=True)
        self.assertEqual(out["unreadable"], ["development_pr_review"])
        self.assertEqual(out["failed"], {"development_pr_review": 403})


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

    def test_a_list_read_in_full_is_recorded_once_even_with_nothing_new(self):
        rec = record()
        flag = "tracking_issue_comment" + collect.STEERING_CAP_SUFFIX
        first = steering.new_evidence(rec, {"items": [item(1)]}, {}, policy=POLICY, now=NOW)
        known = {first["evidence_id"]: first}
        capped = {"items": [item(1)], "omitted": {flag: 1, "tracking_issue_comment": 499}}
        flagged = steering.new_evidence(rec, capped, known, policy=POLICY, now=NOW)
        self.assertEqual((flagged["data"]["items"], flagged["data"]["omitted"][flag]), ([], 1))
        self.assertIn("newer items may be unread", flagged["summary"])
        self.assertNotEqual(flagged["evidence_id"], first["evidence_id"])
        known[flagged["evidence_id"]] = flagged
        self.assertIsNone(steering.new_evidence(rec, capped, known, policy=POLICY, now=NOW))

    def test_a_join_that_creates_a_credential_match_is_redacted(self):
        rec = record()
        with mock.patch("worker_report.clean", side_effect=lambda value, _limit: str(value)):
            out = steering.new_evidence(rec, {"items": [item(1, body="AKIA" + "B" * 16)]}, {},
                                        policy=POLICY, now=NOW)
        self.assertEqual(state.find_secrets(canonical_json(out)), [])
        self.assertEqual(out["data"]["items"][0]["body"], "[redacted]")

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
