# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The dossier's measurable contribution rules and the trusted checks that
apply them: the worker gate and the Work payload that carries them."""

import copy
import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import commit_rules
import worker_gate
from tests.test_dossier_gate import dossier
from tests.test_milestone5_worker import proposal, task

RULES = {"title_max_length": 50, "title_prefixes": ["ci:", "build(wheels):"],
         "title_prefix_required": True, "title_capitalization": "upper",
         "title_trailing_period": "forbidden", "body_required": True, "body_line_max_length": 72,
         "signed_off_by_required": True, "max_commits": 1}
GOOD = ("ci: Add riscv64 to the wheel matrix\n\nBuild riscv64 wheels with the existing QEMU "
        "job.\n\nSigned-off-by: " + commit_rules.APP_IDENTITY)
APP_SIGNOFF = "Signed-off-by: " + commit_rules.APP_IDENTITY


def series(*messages: str, path=".github/workflows/build-wheels.yml") -> bytes:
    """A ``git format-patch`` mbox of one change per message."""
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo)]
        subprocess.run([*git, "init", "-q"], check=True)
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("old\n")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
        for i, message in enumerate(messages):
            (repo / path).write_text(f"new {i}\n")
            subprocess.run([*git, "commit", "-q", "-a", "-F", "-"], input=message.encode(),
                           check=True)
        return subprocess.run([*git, "format-patch", "--stdout", "HEAD~" + str(len(messages))],
                              check=True, capture_output=True).stdout


class TestRules(unittest.TestCase):
    def test_a_message_that_follows_every_rule(self):
        self.assertEqual(commit_rules.message_problems(GOOD, commit_rules.normalize(RULES)), [])

    def test_each_rule(self):
        rules = commit_rules.normalize(RULES)
        cases = {
            "ci: Add riscv64 to the wheel matrix of every supported platform\n\nBody.\n\n"
            "Signed-off-by: a <a@b.c>": "more than 50",
            "Add riscv64\n\nBody.\n\nSigned-off-by: a <a@b.c>": "none of the prefixes",
            "ci: add riscv64\n\nBody.\n\nSigned-off-by: a <a@b.c>": "capital letter",
            "ci: Add riscv64.\n\nBody.\n\nSigned-off-by: a <a@b.c>": "ends with a period",
            "ci: Add riscv64\n\nSigned-off-by: a <a@b.c>": "no body",
            "ci: Add riscv64\n\n" + "word " * 20 + "\n\nSigned-off-by: a <a@b.c>": "longer than 72",
            "ci: Add riscv64\n\nBody.": "no Signed-off-by",
        }
        for message, problem in cases.items():
            with self.subTest(problem=problem):
                found = commit_rules.message_problems(message, rules)
                self.assertEqual(len(found), 1, found)
                self.assertIn(problem, found[0])

    def test_urls_and_unbroken_tokens_may_exceed_the_wrap_width(self):
        rules = commit_rules.normalize(RULES)
        message = GOOD + "\n[1]: https://ci.riseproject.dev/" + "x" * 80
        self.assertEqual(commit_rules.message_problems(message, rules), [])

    def test_debug_commits_are_exempt_and_the_series_is_counted(self):
        rules = commit_rules.normalize(RULES)
        self.assertEqual(commit_rules.series_problems([GOOD, "{debug} lift the fork guard"],
                                                      rules), [])
        self.assertIn("more than 1", commit_rules.series_problems([GOOD, GOOD], rules)[0])
        self.assertEqual(commit_rules.series_problems([GOOD, GOOD], rules,
                                                      count_commits=False), [])
        self.assertEqual(commit_rules.series_problems(["anything"], None), [])

    def test_malformed_rules_are_refused(self):
        for change in ({"title_max_length": 5}, {"title_max_length": True},
                       {"title_prefixes": ["a\nb"]}, {"title_capitalization": "title"},
                       {"unknown": 1}, {"title_prefixes": [], "title_prefix_required": True},
                       {"body_required": "yes"}):
            with self.subTest(change=change), self.assertRaises(commit_rules.RuleError):
                commit_rules.normalize(dict(RULES, **change))

    def test_prefixes_are_literal(self):
        rules = commit_rules.normalize(dict(RULES, title_prefixes=["(a+)+$"]))
        found = commit_rules.message_problems("a" * 40 + "!\n\nBody.\n\nSigned-off-by: a <a@b.c>",
                                              rules)
        self.assertIn("none of the prefixes", found[0])

    def test_the_mbox_reads_as_git_am_does(self):
        long = "ci: Add riscv64 " + "x" * 90
        data = series(GOOD, long + "\n\nA body\n---\nnot part of it")
        messages = commit_rules.mbox_messages(data)
        self.assertEqual(messages[0], GOOD)
        self.assertEqual(messages[1], long + "\n\nA body")

    def test_template_sections(self):
        d = dossier()
        self.assertEqual(commit_rules.sections_from_dossier(d), ["Summary", "Testing"])
        body = "## Summary\n\nText.\n\n### **testing**\n\n- [x] Tests added"
        self.assertEqual(commit_rules.body_problems(body, ["Summary", "Testing"]), [])
        fenced = "## Summary\n\n```\n## Testing\n```"
        self.assertIn("'Testing'", commit_rules.body_problems(fenced, ["Summary", "Testing"])[0])
        v1 = dict(d, schema_version=1)
        self.assertIsNone(commit_rules.sections_from_dossier(v1))
        self.assertIsNone(commit_rules.from_dossier(v1))
        self.assertEqual(commit_rules.from_dossier(d)["title_max_length"], 72)


class TestWorkerGate(unittest.TestCase):
    def gate(self, patch: bytes, rules: dict | None):
        value = task()
        if rules is not None:
            value["context"] = {"commit_rules": rules}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "patches.mbox"
            path.write_bytes(patch)
            return worker_gate.validate(value, proposal(patch), path)

    def test_the_gate_checks_every_non_debug_commit(self):
        rules = dict(RULES, max_commits=None)
        self.assertEqual(self.gate(series(GOOD, "{debug} lift the guard"), rules)["status"],
                         "accepted")
        with self.assertRaisesRegex(worker_gate.WorkerGateError,
                                    "commit conventions: .*none of the prefixes"):
            self.gate(series(GOOD, "Add riscv64\n\nBody.\n\n" + APP_SIGNOFF), rules)
        # The number of commits is checked at submission only.
        self.assertEqual(self.gate(series(GOOD, GOOD), RULES)["status"], "accepted")

    def test_attribution_trailers_name_the_app_only(self):
        for trailer in ("Signed-off-by: Linus Torvalds <torvalds@linux-foundation.org>",
                        "Co-authored-by: Real Maintainer <m@example.org>",
                        "Reviewed-by: a <a@b.c>", "acked-BY: a <a@b.c>", "Tested-by: x",
                        "Reported-by: x", "Suggested-by: x", "Helped-by: x",
                        "Co-authored-by: " + commit_rules.APP_IDENTITY + " extra"):
            with self.subTest(trailer=trailer), self.assertRaisesRegex(
                    worker_gate.WorkerGateError, "attributes work to someone else"):
                self.gate(series("Add riscv64\n\nBody.\n\n" + trailer), None)
        for trailer in (APP_SIGNOFF, "Co-authored-by: " + commit_rules.APP_IDENTITY):
            self.assertEqual(self.gate(series("Add riscv64\n\nBody.\n\n" + trailer), None)
                             ["status"], "accepted")
        # A trailer is admitted when it is byte-identical to one of the
        # approved commits, and only then.
        approved = ["Build\n\nSigned-off-by: Ann <ann@example.org>"]
        self.assertEqual(commit_rules.attribution_problems(
            ["Rebuild\n\nSigned-off-by: Ann <ann@example.org>"], approved), [])
        self.assertEqual(len(commit_rules.attribution_problems(
            ["Rebuild\n\nSigned-off-by: Ann  <ann@example.org>",
             "Rebuild\n\nAcked-by: Ann <ann@example.org>"], approved)), 2)

    def test_an_underscore_key_is_not_an_attribution_trailer(self):
        for line in ("order_by: x", "  group_by: name", "sort_by:"):
            with self.subTest(line=line):
                self.assertEqual(commit_rules.attribution_problems(
                    ["Add riscv64\n\nBody.\n\n" + line], []), [])

    def test_a_lookalike_trailer_is_an_attribution_trailer(self):
        """Every lookalike of a key or colon that GitHub or a reader could
        take for an attribution trailer is one: case, white space before the
        colon, Unicode hyphens, fullwidth and other
        colon forms, compatibility characters, another script, and a title
        that is itself a trailer."""
        evil = "Evil <e@x>"
        for line in ("co-AUTHORED-by: " + evil, "Co-authored-by  : " + evil,
                     "Co‐authored‐by: " + evil, "Co‑authored‑by: " + evil,
                     "Co‒authored–by: " + evil, "Co―authored−by: " + evil,
                     "Co﹘authored﹣by: " + evil, "Co－authored－by: " + evil,
                     "Co-authored-by： " + evil,
                     "Co-authored-by﹕ " + evil, "Co-authored-by︓ " + evil,
                     "Co-authored-by∶ " + evil, "Co-authored-by꞉ " + evil,
                     "Ｃｏ-authored-ｂｙ: " + evil, "Cо-authored-by: " + evil,
                     "Co-authored-by:\t" + evil, " Co-authored-by: " + evil,
                     "Co-authored-by: " + commit_rules.APP_IDENTITY + " " + evil):
            with self.subTest(line=line):
                self.assertEqual(len(commit_rules.attribution_problems(
                    ["Add riscv64\n\nBody.\n\n" + line], [])), 1)
        title = commit_rules.attribution_problems(["Co-authored-by: " + evil], [])
        self.assertEqual(len(title), 1)
        self.assertIn("Co-authored-by: Evil", title[0])
        # A lookalike is admitted only when it is byte-identical to an
        # approved trailer, never by its folded form.
        approved = ["Build\n\nCo-authored-by: Ann <ann@example.org>"]
        self.assertEqual(len(commit_rules.attribution_problems(
            ["Rebuild\n\nCo‐authored-by: Ann <ann@example.org>"], approved)), 1)
        self.assertEqual(len(commit_rules.attribution_problems(
            ["Rebuild\n\nCo-authored-by： " + commit_rules.APP_IDENTITY], [])), 1)

    def test_a_continued_attribution_trailer_is_refused(self):
        """``git interpret-trailers`` folds a white-space-led line into the
        trailer before it, so a continuation could name someone else."""
        for after in ("  Co-authored-by: Evil <e@x>", " and Evil <e@x>", "\tEvil <e@x>",
                      " Evil <e@x>"):
            with self.subTest(after=after):
                problems = commit_rules.attribution_problems(
                    ["Add riscv64\n\nBody.\n\n" + APP_SIGNOFF + "\n" + after], [])
                self.assertTrue(any("continues the attribution trailer" in p for p in problems),
                                problems)
        self.assertEqual(commit_rules.attribution_problems(
            ["Add riscv64\n\nBody.\n\n" + APP_SIGNOFF + "\n\n  indented text"], []), [])
        self.assertEqual(commit_rules.attribution_problems(
            ["Add riscv64\n\n  indented body\n\n" + APP_SIGNOFF], []), [])

    def test_without_rules_nothing_is_checked(self):
        self.assertEqual(self.gate(series("anything goes"), None)["status"], "accepted")

    def test_invalid_rules_in_the_work_are_refused(self):
        with self.assertRaisesRegex(worker_gate.WorkerGateError, "invalid commit rules"):
            self.gate(series(GOOD), dict(RULES, title_max_length=1))


class TestPayload(unittest.TestCase):
    def test_the_payload_carries_the_rules_of_a_version_2_dossier_only(self):
        import work
        from tests.helpers import contribution
        record = contribution(phase="development", activity="agent_running", revision=3)
        record.update(dossier_evidence_id="dossier-x", upstream=dict(
            record["upstream"], repository_id=1, base_sha="a" * 40))
        record["active_work"] = {"dedupe_key": "k", "task": "development_patch", "generation": 0,
                                 "revision": 3, "attempt": 1, "requested_at": "2026-11-23T00:00:00+00:00",
                                 "deadline_at": "2026-11-23T06:00:00+00:00", "dispatched_run_id": None,
                                 "reason_ids": [], "tracking_issue": {"url": "https://x"}, "feedback_evidence_ids": []}
        for data, expected in ((dossier(), commit_rules.from_dossier(dossier())),
                               (dict(dossier(), schema_version=1), None)):
            evidence = {"evidence_id": "dossier-x", "kind": "dossier", "data": copy.deepcopy(data)}
            snapshot = type("S", (), {"contributions": {"c": record}, "evidence": {"dossier-x": evidence}})
            with tempfile.TemporaryDirectory() as directory, \
                    unittest.mock.patch("reconcile.load_snapshot", return_value=(None, snapshot)), \
                    unittest.mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout="c" * 40)):
                out = Path(directory) / "plan.json"
                args = type("A", (), {"root": directory, "out": str(out), "control_repo_id": "1"})
                work.cmd_plan(args)
                context = json.loads(out.read_text())["nodes"][0]["payload"]["context"]
            self.assertEqual(context.get("commit_rules"), expected)
            self.assertEqual("commit_rules" in context, expected is not None)



class TestHeadings(unittest.TestCase):
    def test_atx_headings_and_their_closing_sequence(self):
        self.assertEqual(commit_rules.headings(
            "## Summary\n# Testing ##\n   ### A #b ###  \n#NoSpace\n####### seven\n"
            "    ## indented code\n```\n## fenced\n```\n## *Checklist*\t#"),
            ["summary", "testing", "a #b", "checklist"])

    def test_an_adversarial_description_is_read_in_linear_time(self):
        import time
        bodies = ["#" + " " * 12000 + "x", "# " + "#" * 6000 + " " * 6000 + "x",
                  "##" + "\t " * 6000 + "#", ("# " + " #" * 6000 + "\n") * 2]
        start = time.monotonic()
        for body in bodies:
            commit_rules.headings(body)
            commit_rules.body_problems(body, ["Summary"])
        self.assertLess(time.monotonic() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
