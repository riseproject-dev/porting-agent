# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The trusted check of a review of a held development patch, and the
comment posted on the development PR for a patch published after
successive failed reviews."""

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import handoff
import patch_review
from tests.helpers import NOW, SHA_A, SHA_B, contribution, policy

DIGEST = "d" * 64
TASK = {"task": "development_review", "contribution_id": "example--proj/12", "revision": 4,
        "expected_head": SHA_A, "base_sha": SHA_B, "dedupe_key": "example--proj/12@g0.r4:development_review",
        "context": {"patch": {"worker_run_id": "11", "artifact_id": 21,
                              "work": "example--proj/12@g0.r3:development_patch", "revision": 3,
                              "patch_digest": DIGEST, "candidate": "c" * 40, "tree": "e" * 40,
                              "expected_head": SHA_A, "operation": "append",
                              "changed_files": [".github/workflows/wheels.yml"],
                              "report_evidence_id": "report-example--proj--12-r3-11"}}}


def review(**over):
    value = {"schema_version": 1, "contribution_id": "example--proj/12", "expected_revision": 4,
             "expected_head": SHA_A, "patch_digest": DIGEST, "candidate": "c" * 40,
             "verdict": "fail", "summary": "The riscv64 job never runs the tests.",
             "findings": [finding()]}
    value.update(over)
    return value


def finding(**over):
    value = {"id": "f1", "severity": "major", "category": "tests",
             "file": ".github/workflows/wheels.yml", "line": 12, "end_line": None,
             "title": "Tests are skipped", "detail": "CIBW_TEST_SKIP skips riscv64 entirely."}
    value.update(over)
    return value


class TestCheck(unittest.TestCase):
    def test_a_blocker_or_major_finding_fails_the_review(self):
        for severity in ("blocker", "major"):
            with self.subTest(severity=severity):
                out = patch_review.check(TASK, review(findings=[finding(severity=severity)]))
                self.assertEqual((out["status"], out["verdict"]), ("patch_review", "fail"))
                self.assertEqual((out["patch_digest"], out["candidate"]), (DIGEST, "c" * 40))

    def test_minor_findings_and_nits_pass_and_are_recorded(self):
        out = patch_review.check(TASK, review(verdict="pass", findings=[
            finding(severity="minor"), finding(id="f2", severity="nit", file=None, line=None)]))
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual([f["severity"] for f in out["findings"]], ["minor", "nit"])

    def test_the_verdict_is_derived_whatever_the_reviewer_declares(self):
        out = patch_review.check(TASK, review(verdict="pass"))
        self.assertEqual((out["verdict"], out["declared_verdict"]), ("fail", "pass"))
        out = patch_review.check(TASK, review(verdict="fail", findings=[finding(severity="nit")]))
        self.assertEqual(out["status"], "rejected")
        self.assertIn("names no blocker or major finding", out["reason"])

    def test_a_review_of_another_patch_or_work_is_not_usable(self):
        for change, why in ((dict(patch_digest="0" * 64), "another patch"),
                            (dict(candidate="0" * 40), "another patch"),
                            (dict(expected_revision=3), "expected_revision"),
                            (dict(contribution_id="other/1"), "contribution_id"),
                            (dict(expected_head=SHA_B), "another patch")):
            with self.subTest(change=change):
                out = patch_review.check(TASK, review(**change))
                self.assertEqual(out["status"], "rejected")
                self.assertIn(why, out["reason"])
        self.assertEqual(patch_review.check(dict(TASK, task="development_patch"), review())["status"],
                         "rejected")
        self.assertEqual(patch_review.check(dict(TASK, context={}), review())["status"], "rejected")

    def test_a_finding_on_a_file_the_patch_does_not_change_is_not_usable(self):
        out = patch_review.check(TASK, review(findings=[finding(file="setup.py")]))
        self.assertEqual(out["status"], "rejected")
        self.assertIn("setup.py", out["reason"])

    def test_a_malformed_review_is_not_usable(self):
        for value in ([], {"verdict": "pass"}, review(findings=[finding(severity="critical")]),
                      review(findings=[finding(), finding()]),
                      review(findings=[finding(detail="x" * 1501)])):
            with self.subTest(value=str(value)[:60]):
                self.assertEqual(patch_review.check(TASK, value)["status"], "rejected")

    def test_texts_are_bounded_and_redacted(self):
        secret = "ghp_" + "A" * 36
        out = patch_review.check(TASK, review(summary=f"token {secret} leaked",
                                              findings=[finding(detail=f"see {secret} here")]))
        self.assertNotIn(secret, json.dumps(out))
        self.assertIn("[redacted]", out["summary"])

    def test_the_command_line_writes_the_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "task.json").write_text(json.dumps(TASK))
            (tmp / "proposal.json").write_text(json.dumps(review()))
            self.assertEqual(patch_review.main(["check", "--task", str(tmp / "task.json"),
                                                "--result", str(tmp / "proposal.json"),
                                                "--out", str(tmp / "outcome.json")]), 0)
            self.assertEqual(json.loads((tmp / "outcome.json").read_text())["verdict"], "fail")
            (tmp / "proposal.json").write_text("{not json")
            patch_review.main(["check", "--task", str(tmp / "task.json"), "--result",
                               str(tmp / "proposal.json"), "--out", str(tmp / "outcome.json")])
            self.assertEqual(json.loads((tmp / "outcome.json").read_text())["status"], "rejected")


def held(**over):
    value = {"worker_run_id": "11", "artifact_run_id": "11", "artifact_id": 21,
             "work": "example--proj/12@g0.r3:development_patch", "generation": 0, "revision": 3,
             "patch_digest": DIGEST, "candidate": "c" * 40, "tree": "e" * 40,
             "expected_head": SHA_A, "operation": "append",
             "changed_files": [".github/workflows/wheels.yml"], "reason_ids": [],
             "feedback_evidence_ids": [], "report_evidence_id": None, "held_at": NOW,
             "expires_at": "2026-12-18T12:00:00+00:00"}
    value.update(over)
    return value


class TestHeldPatch(unittest.TestCase):
    def test_a_held_patch_expires_a_day_before_its_artifact(self):
        rec = contribution(activity="review_pending", held_patch=held())
        at = datetime.fromisoformat(NOW)
        self.assertFalse(patch_review.held_patch_expired(rec, at + timedelta(days=28)))
        self.assertTrue(patch_review.held_patch_expired(rec, at + timedelta(days=29)))
        self.assertFalse(patch_review.held_patch_expired(contribution(), at))

    def test_the_expiry_is_that_of_the_artifacts_the_publication_reads(self):
        at = datetime.fromisoformat(NOW)
        outcome = {"publication_run_id": "11",
                   "publication_artifact_expires_at": "2026-11-25T12:00:00Z"}
        # The expiry GitHub reported for the artifact, not the hold.
        self.assertEqual(patch_review.held_patch_expires_at(
            outcome, run="11", requested_at=NOW, now=NOW), "2026-11-25T12:00:00+00:00")
        # Without it, the retention after the Work was requested, before
        # any of its runs began.
        early = (at - timedelta(days=10)).isoformat()
        self.assertEqual(patch_review.held_patch_expires_at(
            {"publication_run_id": "11"}, run="11", requested_at=early, now=NOW),
            (at + timedelta(days=20)).isoformat())
        # A retry's artifact: the worker run's own artifact, which the
        # publication reads, may expire first.
        late = dict(outcome, publication_run_id="12",
                    publication_artifact_expires_at="2027-01-01T00:00:00+00:00")
        self.assertEqual(patch_review.held_patch_expires_at(
            late, run="11", requested_at=early, now=NOW), (at + timedelta(days=20)).isoformat())
        rec = contribution(activity="publishing",
                           held_patch=held(expires_at="2026-11-25T12:00:00+00:00"))
        self.assertFalse(patch_review.held_patch_expired(rec, at + timedelta(days=5)))
        self.assertTrue(patch_review.held_patch_expired(rec, at + timedelta(days=6)))

    def test_a_verified_patch_without_its_pin_cannot_be_held(self):
        rec = contribution(activity="agent_running")
        outcome = {"status": "verified", "candidate": "c" * 40, "tree": "e" * 40,
                   "patch_digest": DIGEST, "operation_kind": "append", "changed_files": [],
                   "publication_artifact_id": 21, "publication_run_id": "12",
                   "publication_series_digest": DIGEST,
                   "task": {"dedupe_key": "w", "revision": 0, "expected_head": SHA_A}}
        self.assertEqual(patch_review.held_patch(rec, outcome, run="11", now=NOW,
                                                 report_evidence_id=None)["candidate"], "c" * 40)
        for broken in ({"publication_artifact_id": None}, {"patch_digest": None},
                       {"operation_kind": "none"}, {"candidate": "x"},
                       {"publication_run_id": None}, {"publication_series_digest": "f" * 64}):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    patch_review.held_patch(rec, dict(outcome, **broken), run="11", now=NOW,
                                            report_evidence_id=None)


class FakeApi:
    """The comments of one PR, served a hundred to a page."""

    def __init__(self, existing=()):
        self.existing = list(existing)
        self.posted = []
        self.pages = []

    def call(self, method, path, data=None):
        if method == "GET":
            page = int(path.rsplit("page=", 1)[1])
            self.pages.append(page)
            return self.existing[(page - 1) * 100:page * 100]
        self.posted.append((path, data["body"]))
        return {"id": 77}


class TestComment(unittest.TestCase):
    def evidence(self, root: Path) -> dict:
        rec = contribution(activity="reviewing", revision=5, held_patch=held())
        outcome = patch_review.check(dict(TASK, revision=5),
                                     review(expected_revision=5, findings=[
                                         finding(), finding(id="f2", severity="nit",
                                                            detail="Use @someone's style.")]))
        ev = patch_review.evidence(rec, outcome, now=NOW, policy=policy(), run="31",
                                   disposition="publish_anyway", rejections=2)
        path = root / ".state/evidence"
        path.mkdir(parents=True)
        (path / f"{ev['evidence_id']}.json").write_text(json.dumps(ev))
        return ev

    def due(self, eid, **over):
        """A published record whose comment is due at its first attempt."""
        comment = {"evidence_id": eid, "head": "c" * 40, "repository": None, "pr_number": None,
                   "attempts": 1, "next_attempt_at": NOW, "launched_at": NOW, "last_reason": "",
                   "exhausted": False}
        comment.update(over)
        return contribution(activity="waiting_ci", review_comments=[comment])

    def test_the_latest_review_and_why_it_failed_is_posted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ev = self.evidence(root)
            rec = self.due(ev["evidence_id"])
            api = FakeApi()
            result = patch_review.comment(api, rec, ev["evidence_id"], 1, policy=policy(read_only=False),
                                          root=root)
            self.assertEqual((result["status"], result["attempt"]), ("posted", 1))
            [(path, body)] = api.posted
            self.assertEqual(path, "/repos/riseproject-upstreaming/proj/issues/1/comments")
            self.assertIn("published although its latest review did not pass", body)
            self.assertIn("second review in a row", body)
            self.assertIn("1 blocking problem", body)
            self.assertIn("CIBW_TEST_SKIP skips riscv64 entirely.", body)
            self.assertIn(f"`{'c' * 40}`", body)
            self.assertIn(patch_review.COMMENT_MARKER.format(ev["evidence_id"]), body)
            self.assertNotIn("@someone", body)
            self.assertIn("@" + handoff.WORD_JOINER + "someone", body)
            self.assertNotIn(chr(0x2014), body)
            # A retry finds the comment of the App by its marker, beyond the
            # first page of comments, and posts nothing; the marker in a
            # comment of anyone else is ignored.
            app = {"login": patch_review.COMMENT_AUTHOR}
            again = FakeApi([{"id": n, "body": "other", "user": app} for n in range(150)]
                            + [{"id": 76, "body": body, "user": {"login": "someone"}},
                               {"id": 77, "body": body, "user": app}])
            found = patch_review.comment(again, rec, ev["evidence_id"], 1,
                                         policy=policy(read_only=False), root=root)
            self.assertEqual((found["status"], found["comment_id"]), ("found", 77))
            self.assertEqual((again.posted, again.pages), ([], [1, 2]))
            forged = FakeApi([{"id": 76, "body": body, "user": {"login": "someone"}}])
            self.assertEqual(patch_review.comment(forged, rec, ev["evidence_id"], 1,
                                                  policy=policy(read_only=False),
                                                  root=root)["status"], "posted")
            # The PR recorded with the comment is the one it is posted on.
            pinned = self.due(ev["evidence_id"], repository="riseproject-upstreaming/old",
                              pr_number=4)
            other = FakeApi()
            patch_review.comment(other, dict(pinned, fork=None), ev["evidence_id"], 1,
                                 policy=policy(read_only=False), root=root)
            self.assertEqual(other.posted[0][0],
                             "/repos/riseproject-upstreaming/old/issues/4/comments")

    def test_a_long_review_is_bounded_and_ends_with_its_marker(self):
        findings = [{"id": f"f{i}", "severity": "major", "category": "tests", "file": "a/b.yml",
                     "line": 3, "title": "t" * 300, "detail": "word " * 300 + "```",
                     "suggestion": "@sugg " * 200} for i in range(30)]
        rev = {"evidence_id": "patch-review-x-r1-1",
               "data": {"findings": findings, "summary": "s " * 1500 + "````",
                        "failed_in_succession": 2}}
        body = patch_review.comment_body(rev, "c" * 40)
        self.assertLessEqual(len(body), patch_review.COMMENT_MAX)
        self.assertTrue(body.endswith(patch_review.COMMENT_MARKER.format(rev["evidence_id"])))
        self.assertIn("further findings of the review are recorded", body)
        # Every fence it opens is closed: the lines that open a fence pair
        # up with the lines that close it.
        fences = [line.strip() for line in body.splitlines()
                  if line.strip().startswith("```")]
        opened = []
        for fence in fences:
            if opened and fence == opened[-1][0]:
                opened.pop()
            else:
                opened.append((fence.rstrip("text"), fence))
        self.assertEqual(opened, [])

    def test_nothing_is_posted_unless_the_comment_is_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ev = self.evidence(root)
            eid = ev["evidence_id"]
            api = FakeApi()
            writable = policy(read_only=False)
            for rec, attempt, pol in (
                    (self.due(eid), 2, writable),
                    (self.due("patch-review-other"), 1, writable),
                    (dict(self.due(eid), paused=True), 1, writable),
                    (self.due(eid), 1, policy(read_only=True)),
                    (dict(self.due(eid), fork=dict(self.due(eid)["fork"], pr_number=None)), 1,
                     writable),
                    (dict(self.due(eid), review_comments=[]), 1, writable)):
                result = patch_review.comment(api, rec, eid, attempt, policy=pol, root=root)
                self.assertEqual(result["status"], "skipped", result)
            self.assertEqual(api.posted, [])

    def test_a_comment_is_due_only_for_a_patch_published_anyway(self):
        rec = contribution(activity="publishing", held_patch=held(), pending_publication={
            "worker_run_id": "11", "attempts": 0, "last_reason": "", "next_attempt_at": NOW,
            "patch_review": {"evidence_id": "patch-review-x", "verdict": "fail",
                             "published_anyway": True}})
        new = {"evidence_id": "patch-review-x", "head": "c" * 40,
               "repository": rec["fork"]["name"], "pr_number": rec["fork"]["pr_number"],
               "attempts": 0, "next_attempt_at": NOW, "launched_at": None, "last_reason": "",
               "exhausted": False}
        def due(record):
            due, dropped = patch_review.comment_due(
                record, patch_review.pending_published_anyway(record), now=NOW)
            self.assertEqual(dropped, [])
            return due

        self.assertEqual(patch_review.pending_published_anyway(rec), [("patch-review-x", "c" * 40)])
        self.assertEqual(due(rec), [new])
        passed = copy.deepcopy(rec)
        passed["pending_publication"]["patch_review"]["published_anyway"] = False
        self.assertEqual(due(passed), [])
        # The comments still due are kept, whatever is published, and a
        # review already due is not added again.
        older = dict(new, evidence_id="patch-review-old", attempts=3, exhausted=True)
        self.assertEqual(due(dict(passed, review_comments=[older])), [older])
        self.assertEqual(due(dict(rec, review_comments=[older])), [older, new])
        self.assertEqual(due(dict(rec, review_comments=[older, new])), [older, new])
        # Without a development PR, none is recorded: the launch resolves it.
        no_pr = dict(rec, fork=dict(rec["fork"], pr_number=None))
        self.assertEqual(due(no_pr), [dict(new, repository=None, pr_number=None)])

    def test_a_published_result_names_the_review_that_published_it_anyway(self):
        rec = contribution(activity="fix_needed")
        pin = {"worker_run_id": "11", "work": "w", "candidate": "c" * 40}
        stored = {"patch-review-x": {"kind": "patch_review", "contribution_id": rec["contribution_id"],
                                     "data": {"disposition": "publish_anyway", "patch": pin}}}
        result = {"status": "published", "worker_run_id": "11", "candidate": "c" * 40,
                  "task": {"dedupe_key": "w"}}
        self.assertEqual(patch_review.published_anyway(rec, result, stored),
                         [("patch-review-x", "c" * 40)])
        for name, other, store in (
                ("not published", dict(result, status="uncertain"), stored),
                ("another run", dict(result, worker_run_id="12"), stored),
                ("another Work", dict(result, task={"dedupe_key": "v"}), stored),
                ("another commit", dict(result, candidate="d" * 40), stored),
                ("a passed review", result, {"patch-review-x": dict(
                    stored["patch-review-x"], data={"disposition": "publish", "patch": pin})}),
                ("another contribution", result, {"patch-review-x": dict(
                    stored["patch-review-x"], contribution_id="other/1")})):
            with self.subTest(name):
                self.assertEqual(patch_review.published_anyway(rec, other, store), [])

    def test_trusted_branch_evidence_names_the_review_published_anyway(self):
        rec = contribution(activity="fix_needed")
        pin = {"worker_run_id": "11", "work": "w", "candidate": "c" * 40}
        stored = {"patch-review-x": {"kind": "patch_review", "contribution_id": rec["contribution_id"],
                                     "data": {"disposition": "publish_anyway", "patch": pin}}}
        self.assertEqual(patch_review.landed_anyway(rec, {"c" * 40}, stored),
                         [("patch-review-x", "c" * 40)])
        for name, heads, store in (
                ("another commit", {"d" * 40}, stored),
                ("no commit", {None, "not a sha"}, stored),
                ("a review that passed", {"c" * 40}, {"patch-review-x": dict(
                    stored["patch-review-x"], data={"disposition": "publish", "patch": pin})}),
                ("a review returned for repair", {"c" * 40}, {"patch-review-x": dict(
                    stored["patch-review-x"],
                    data={"disposition": "return_for_repair", "patch": pin})}),
                ("another contribution", {"c" * 40}, {"patch-review-x": dict(
                    stored["patch-review-x"], contribution_id="other/1")})):
            with self.subTest(name):
                self.assertEqual(patch_review.landed_anyway(rec, heads, store), [])
        op = {"contribution_id": rec["contribution_id"], "kind": "branch_update",
              "status": "confirmed", "proposed": {"commit": "c" * 40}}
        self.assertEqual(patch_review.confirmed_heads(rec, [op]), {"c" * 40: "c" * 40})
        # Discovery that confirmed it by its tree and parents read another
        # commit (a signed replay): the comment names the commit on the
        # branch, matched by the commit the review pinned.
        replayed = patch_review.confirmed_heads(rec, [op], "e" * 40)
        self.assertEqual(replayed, {"c" * 40: "e" * 40})
        self.assertEqual(patch_review.landed_anyway(rec, replayed, stored),
                         [("patch-review-x", "e" * 40)])
        for name, other in (("uncertain", dict(op, status="uncertain")),
                            ("another kind", dict(op, kind="pr_update")),
                            ("another contribution", dict(op, contribution_id="other/1"))):
            with self.subTest(name):
                self.assertEqual(patch_review.confirmed_heads(rec, [other]), {})

    def test_a_comment_due_beyond_the_record_drops_the_oldest_exhausted_one(self):
        rec = contribution(activity="fix_needed")
        cap = patch_review.RECORDED_DUE_COMMENTS
        def item(n, **kw):
            return {"evidence_id": f"patch-review-{n}", "head": "c" * 40,
                    "repository": rec["fork"]["name"], "pr_number": rec["fork"]["pr_number"],
                    "attempts": 1, "next_attempt_at": NOW, "launched_at": None,
                    "last_reason": "", "exhausted": False} | kw
        full = [item(n) for n in range(cap)]
        # The oldest exhausted comment that is not launched goes first.
        mixed = [dict(x) for x in full]
        mixed[3]["exhausted"] = True
        mixed[5]["exhausted"] = True
        mixed[1].update(exhausted=True, launched_at=NOW)
        due, dropped = patch_review.comment_due(dict(rec, review_comments=mixed),
                                                [("patch-review-new", "d" * 40)], now=NOW)
        self.assertEqual(len(due), cap)
        self.assertEqual([d["evidence_id"] for d in dropped], ["patch-review-3"])
        self.assertEqual(due[-1]["evidence_id"], "patch-review-new")
        # None exhausted: the oldest that is not launched, and it is returned.
        launched = [dict(x) for x in full]
        launched[0]["launched_at"] = NOW
        due, dropped = patch_review.comment_due(dict(rec, review_comments=launched),
                                                [("patch-review-new", "d" * 40)], now=NOW)
        self.assertEqual([d["evidence_id"] for d in dropped], ["patch-review-1"])
        self.assertEqual(len(due), cap)
        # Within the bound nothing is dropped, and a review already due is
        # not added again.
        due, dropped = patch_review.comment_due(dict(rec, review_comments=full),
                                                [("patch-review-0", "c" * 40)], now=NOW)
        self.assertEqual((due, dropped), (full, []))

    def test_the_command_line_checks_without_a_token_and_writes_a_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ev = self.evidence(root)
            rec = self.due(ev["evidence_id"])
            (root / ".state/contributions").mkdir(parents=True)
            (root / ".state/contributions/example--proj--12.json").write_text(json.dumps(rec))
            args = ["--contribution", "example--proj--12", "--evidence", ev["evidence_id"]]
            with mock.patch.object(patch_review, "repository_root", return_value=root), \
                    mock.patch("common.load_policy", return_value=policy(read_only=False)), \
                    mock.patch.dict("os.environ", {}, clear=True):
                patch_review.main(["comment-due", *args, "--attempt", "1",
                                   "--out", str(root / "out/due.json")])
                due = json.loads((root / "out/due.json").read_text())
                self.assertEqual((due["due"], due["fork_name"]), (True, "proj"))
                self.assertFalse((root / "out/review-comment.json").exists())
                patch_review.main(["comment-due", *args, "--attempt", "2",
                                   "--out", str(root / "out/due.json")])
                self.assertFalse(json.loads((root / "out/due.json").read_text())["due"])
                receipt = json.loads((root / "out/review-comment.json").read_text())
                self.assertEqual((receipt["status"], receipt["attempt"]), ("skipped", 2))
                # Without its token the post is a failed attempt, never a crash.
                patch_review.main(["comment", *args, "--attempt", "1",
                                   "--out", str(root / "out/review-comment.json")])
                receipt = json.loads((root / "out/review-comment.json").read_text())
                self.assertEqual(receipt["status"], "failed")


class TestBounds(unittest.TestCase):
    def test_the_schema_prompt_evidence_and_history_share_the_bounds(self):
        import work_history
        from common import repository_root

        schema = json.loads((repository_root() / "schemas/patch_review.schema.json").read_text())
        props = schema["properties"]
        item = props["findings"]["items"]["properties"]
        self.assertEqual(
            (props["findings"]["maxItems"], props["previous_findings"]["maxItems"],
             props["summary"]["maxLength"], item["title"]["maxLength"],
             item["detail"]["maxLength"], item["suggestion"]["maxLength"]),
            (patch_review.MAX_FINDINGS, patch_review.MAX_PREVIOUS_FINDINGS,
             patch_review.MAX_SUMMARY, patch_review.MAX_TITLE, patch_review.MAX_DETAIL,
             patch_review.MAX_SUGGESTION))
        prompt = " ".join((repository_root() / ".github/workflows/reviewer.md").read_text().split())
        for text in (f"`findings` (at most {patch_review.MAX_FINDINGS};",
                     f"`detail` {patch_review.MAX_DETAIL}",
                     f"`previous_findings` (at most {patch_review.MAX_PREVIOUS_FINDINGS})"):
            self.assertIn(text, prompt)
        # The full entry of the history keeps every finding of a review whole.
        long = "x" * patch_review.MAX_DETAIL
        ev = {"evidence_id": "patch-review-x", "collected_at": NOW, "data": {
            "findings": [finding(id=f"f{n}", detail=long, suggestion="y" * 1000)
                         for n in range(patch_review.MAX_FINDINGS)],
            "previous_findings": [{"ref": f"r{n}", "status": "resolved"}
                                  for n in range(patch_review.MAX_PREVIOUS_FINDINGS)]}}
        entry = work_history._patch_review_entry(ev)
        self.assertEqual(len(entry["findings"]), patch_review.MAX_FINDINGS)
        self.assertEqual(len(entry["previous_findings"]), patch_review.MAX_PREVIOUS_FINDINGS)
        self.assertEqual(len(entry["findings"][0]["detail"]), patch_review.MAX_DETAIL)
        self.assertEqual(len(entry["findings"][0]["suggestion"]), 1000)


if __name__ == "__main__":
    unittest.main()
