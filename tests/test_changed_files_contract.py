# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""``changed_files`` of a proposal is the net delta from ``expected_head`` to
the series applied, for an append and for a rewrite, and the worker gate and
the fork publisher agree on it.

The live ``luhenry--d2vm/3`` contribution was rejected twice for the same
rewrite: listing the paths of the series passed the worker gate (which
compared them with the mbox) and failed the publisher (which compares them
with ``git diff --name-only expected_head candidate``), and listing the delta
failed the gate.
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

import publisher
import worker_gate
from tests import helpers
from tests.helpers import commit_files, git
from tests.test_fork_publish import _ForkFixture
from tests.test_milestone5_worker import proposal as gate_proposal
from tests.test_milestone5_worker import task as gate_task

WORKFLOW = ".github/workflows/wheels.yml"
SCOPE = [".github/workflows/*", "src/*"]


class TestChangedFilesContract(_ForkFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        helpers.without_review_loop(self)
        self.scope = publisher.Scope(allowed_globs=SCOPE)
        self.task["allowed_paths"] = list(SCOPE)

    def gate(self, operation, changed, mbox):
        value = gate_proposal(mbox) | {"operation": operation, "changed_files": changed,
                                       "patch_digest": hashlib.sha256(mbox).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "patches.mbox"
            path.write_bytes(mbox)
            return worker_gate.validate(gate_task() | {"allowed_paths": list(SCOPE)}, value, path)

    def series(self, *steps):
        """The mbox of commits made on the agent's checkout from the base."""
        git(self.agent, "reset", "-q", "--hard", self.base)
        for files in steps:
            commit_files(self.agent, files, msg="BLD: step")
        return git(self.agent, "format-patch", "--stdout", self.base).encode() + b"\n"

    def land(self, mbox, files):
        out = self.run_publish(self.proposal(changed_files=files, patch_digest=None),
                               mbox=mbox, scope=self.scope)
        self.assertEqual(out.status, "published", out.reason)
        head = self.fork_head()
        self.record["fork"]["head_sha"] = head
        self.task.update(expected_head=head, revision=1)
        self.record["revision"] = 1
        return head

    def test_a_rewrite_that_retouches_a_file_on_the_branch_passes_both_gates_with_the_delta(self):
        head = self.land(self.series({WORKFLOW: "on: [push]\njobs: {a: {}}\n"},
                                     {"src/a.c": "int a = 1;\n"}), [WORKFLOW, "src/a.c"])
        # The rewrite touches both files again, but its tree differs from the
        # branch in `src/a.c` alone.
        rewrite = self.series({WORKFLOW: "on: [push]\njobs: {a: {}}\n"}, {"src/a.c": "int a = 2;\n"})
        delta = ["src/a.c"]
        self.assertEqual(self.gate("rewrite", delta, rewrite)["changed_files"], delta)
        out = self.run_publish(self.proposal(operation="rewrite", expected_revision=1, expected_head=head,
                                             changed_files=delta, patch_digest=None),
                               mbox=rewrite, scope=self.scope)
        self.assertEqual(out.status, "published", out.reason)
        self.assertEqual(out.operation["proposed"]["changed_files"], delta)

    def test_the_paths_of_the_series_are_not_the_delta_of_a_rewrite(self):
        head = self.land(self.series({WORKFLOW: "on: [push]\njobs: {a: {}}\n"},
                                     {"src/a.c": "int a = 1;\n"}), [WORKFLOW, "src/a.c"])
        rewrite = self.series({WORKFLOW: "on: [push]\njobs: {a: {}}\n"}, {"src/a.c": "int a = 2;\n"})
        every = [WORKFLOW, "src/a.c"]
        self.gate("rewrite", every, rewrite)
        out = self.run_publish(self.proposal(operation="rewrite", expected_revision=1, expected_head=head,
                                             changed_files=every, patch_digest=None),
                               mbox=rewrite, scope=self.scope)
        self.assertEqual(out.status, "rejected")
        self.assertIn("proposal claims files", out.reason)
        self.assertEqual(self.fork_head(), head)

    def test_a_rewrite_that_drops_a_file_names_it_in_the_delta(self):
        head = self.land(self.series({WORKFLOW: "on: [push]\njobs: {a: {}}\n"},
                                     {"src/a.c": "int a = 1;\n"}), [WORKFLOW, "src/a.c"])
        # The series keeps the workflow only: `src/a.c` returns to the base,
        # which no patch of the mbox shows.
        rewrite = self.series({WORKFLOW: "on: [push]\njobs: {a: {}}\n"})
        delta = ["src/a.c"]
        self.assertEqual(self.gate("rewrite", delta, rewrite)["series_paths"], [WORKFLOW])
        out = self.run_publish(self.proposal(operation="rewrite", expected_revision=1, expected_head=head,
                                             changed_files=delta, patch_digest=None),
                               mbox=rewrite, scope=self.scope)
        self.assertEqual(out.status, "published", out.reason)

    def test_an_append_with_a_file_changed_then_reverted_passes_with_the_delta(self):
        mbox = self.series({"src/a.c": "int a = 5;\n"}, {"src/a.c": "int a;\n"},
                           {WORKFLOW: "on: [push]\njobs: {b: {}}\n"})
        delta = [WORKFLOW]
        report = self.gate("append", delta, mbox)
        self.assertEqual(report["changed_files"], delta)
        self.assertEqual(sorted(report["series_paths"]), sorted([WORKFLOW, "src/a.c"]))
        out = self.run_publish(self.proposal(changed_files=delta, patch_digest=None),
                               mbox=mbox, scope=self.scope)
        self.assertEqual(out.status, "published", out.reason)
        self.assertEqual(out.operation["proposed"]["changed_files"], delta)

    def test_a_changed_file_that_is_not_in_the_series_is_rejected(self):
        mbox = self.series({WORKFLOW: "on: [push]\njobs: {b: {}}\n"})
        with self.assertRaisesRegex(worker_gate.WorkerGateError, "mbox paths do not include"):
            self.gate("append", [WORKFLOW, "src/a.c"], mbox)
        out = self.run_publish(self.proposal(changed_files=[WORKFLOW, "src/a.c"], patch_digest=None),
                               mbox=mbox, scope=self.scope)
        self.assertEqual(out.status, "rejected")
        self.assertEqual(self.fork_head(), self.base)

    def test_every_path_of_the_series_stays_in_scope(self):
        mbox = self.series({WORKFLOW: "on: [push]\njobs: {b: {}}\n"}, {"other/x.c": "int x;\n"})
        for operation in ("append", "rewrite"):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                    worker_gate.WorkerGateError, "mbox paths outside approved scope"):
                self.gate(operation, [WORKFLOW], mbox)

    def test_a_rewrite_that_only_rewords_passes_both_checks_with_an_empty_claim(self):
        content = {WORKFLOW: "on: [push]\njobs: {a: {}}\n"}
        head = self.land(self.series(content), [WORKFLOW])
        git(self.agent, "reset", "-q", "--hard", self.base)
        commit_files(self.agent, content, msg="BLD: build riscv64 wheels, reworded")
        reword = git(self.agent, "format-patch", "--stdout", self.base).encode() + b"\n"
        self.assertEqual(self.gate("rewrite", [], reword)["changed_files"], [])
        out = self.run_publish(self.proposal(operation="rewrite", expected_revision=1, expected_head=head,
                                             changed_files=[], patch_digest=None),
                               mbox=reword, scope=self.scope)
        self.assertEqual(out.status, "published", out.reason)
        self.assertNotEqual(out.candidate, head)
        self.assertEqual(out.operation["proposed"]["changed_files"], [])

    def test_an_empty_claim_cannot_skip_the_exact_check(self):
        head = self.land(self.series({WORKFLOW: "on: [push]\njobs: {a: {}}\n"},
                                     {"src/a.c": "int a = 1;\n"}), [WORKFLOW, "src/a.c"])
        rewrite = self.series({WORKFLOW: "on: [push]\njobs: {a: {}}\n"}, {"src/a.c": "int a = 2;\n"})
        out = self.run_publish(self.proposal(operation="rewrite", expected_revision=1, expected_head=head,
                                             changed_files=[], patch_digest=None),
                               mbox=rewrite, scope=self.scope)
        self.assertEqual(out.status, "rejected")
        self.assertIn("proposal claims files", out.reason)
        self.assertEqual(self.fork_head(), head)


if __name__ == "__main__":
    unittest.main()
