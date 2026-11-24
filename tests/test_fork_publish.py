# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Fork publisher end to end against local bare repositories."""

import hashlib
import tempfile
import time
import unittest
from pathlib import Path

import fork_publish
import publisher
from tests.helpers import commit_files, contribution, git, init_repo, policy

NOW = "2026-11-18T12:00:00+00:00"


class _ForkFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        # Upstream and fork as bare repos under root/<owner>/<name>.git
        up_work = init_repo(root / "up-work")
        self.base = commit_files(up_work, {".github/workflows/wheels.yml": "on: [push]\njobs: {}\n",
                                           "src/a.c": "int a;\n"})
        for name in ("example/proj", "riseproject-upstreaming/proj"):
            bare = root / f"{name}.git"
            bare.parent.mkdir(parents=True, exist_ok=True)
            git(root, "init", "-q", "--bare", "-b", "main", str(bare))
            git(up_work, "push", "-q", str(bare), "HEAD:refs/heads/main", "HEAD:refs/heads/riscv64-dev")
        self.fork_bare = root / "riseproject-upstreaming/proj.git"
        self._saved = (fork_publish.URL_TEMPLATE, list(fork_publish.TEST_EXTRA_CONFIG))
        fork_publish.URL_TEMPLATE = f"file://{root}/{{}}.git"
        fork_publish.TEST_EXTRA_CONFIG[:] = ["-c", "protocol.file.allow=always"]
        # The agent's checkout and its series.
        self.agent = root / "agent"
        git(root, "clone", "-q", "-b", "riscv64-dev", str(self.fork_bare), str(self.agent))
        commit_files(self.agent, {".github/workflows/wheels.yml": "on: [push]\njobs: {riscv64: {}}\n"},
                     msg="BLD: build riscv64 wheels")
        self.mbox = git(self.agent, "format-patch", "--stdout", self.base).encode() + b"\n"
        self.record = contribution()
        self.record["upstream"]["base_sha"] = self.base
        self.record["fork"]["head_sha"] = self.base
        self.task = {"contribution_id": self.record["contribution_id"], "revision": 0, "expected_head": self.base,
                     "fork": "riseproject-upstreaming/proj", "fork_repository_id": 2,
                     "allowed_operations": ["append", "rewrite"],
                     "allowed_paths": [".github/workflows/*"], "reason_ids": ["ci-1"], "ref": "riscv64-dev"}
        self.ops = []
        self.invocations = 0

    def tearDown(self):
        fork_publish.URL_TEMPLATE, extra = self._saved
        fork_publish.TEST_EXTRA_CONFIG[:] = extra
        self.tmp.cleanup()

    def proposal(self, **over):
        p = {"schema_version": 1, "contribution_id": self.record["contribution_id"], "outcome": "patch",
             "expected_revision": 0, "expected_head": self.base, "operation": "append",
             "diagnosis": "add riscv64 row", "progress": "requirement_fixed",
             "changed_files": [".github/workflows/wheels.yml"],
             "patch_digest": hashlib.sha256(self.mbox).hexdigest(),
             "ref": "riscv64-dev", "reconstruction_base": self.base, "fork_repository_id": 2}
        p.update(over)
        return p

    def test_repair_must_acknowledge_every_reason_evidence_record(self):
        self.task["task"] = "development_repair"
        with self.assertRaisesRegex(fork_publish.PublishRejected, "did not acknowledge"):
            fork_publish.check_proposal(self.proposal(reason_ids=[]), self.record, self.task)
        fork_publish.check_proposal(self.proposal(reason_ids=["ci-1"]), self.record, self.task)
        self.task["context"] = {"reason_evidence": [{"kind": "human_relay", "data": {
            "comments": [{"id": 20}], "conversation_comments": [{"id": 30}]}}]}
        with self.assertRaisesRegex(fork_publish.PublishRejected, "responses for feedback"):
            fork_publish.check_proposal(self.proposal(reason_ids=["ci-1"]), self.record, self.task)
        fork_publish.check_proposal(self.proposal(
            reason_ids=["ci-1"], feedback_responses=[
                {"comment_id": 20, "summary": "Updated the matrix."},
                {"comment_id": 30, "summary": "Enabled the test workflow."}]), self.record, self.task)

    def fork_head(self):
        return git(self.fork_bare, "rev-parse", f"refs/heads/{self.task['ref']}")

    def run_publish(self, proposal=None, mbox=None, pol=None, observe=None, scope=None):
        self.invocations += 1
        return fork_publish.publish(
            record=self.record, task=self.task, proposal=proposal or self.proposal(), mbox=self.mbox if mbox is None else mbox,
            token=None, observe_head=observe or self.fork_head,
            scope=scope or publisher.Scope(allowed_globs=[".github/workflows/*"]),
            policy=pol or policy(read_only=False), now=NOW, save_operation=self.ops.append,
            workdir=self.root / f"pub-{self.invocations}")

class TestForkPublish(_ForkFixture, unittest.TestCase):
    def test_publishes_with_lease_and_confirms(self):
        out = self.run_publish()
        self.assertEqual(out.status, "published", out.reason)
        self.assertEqual(self.fork_head(), out.candidate)
        self.assertEqual([o["status"] for o in self.ops], ["planned", "applying", "confirmed"])
        self.assertEqual(out.operation["proposed"]["parents"], [self.base])
        identity = git(self.fork_bare, "show", "-s", "--format=%an;%ae;%cn;%ce", out.candidate)
        expected = f"{fork_publish.APP_COMMIT_NAME};{fork_publish.APP_COMMIT_EMAIL}"
        self.assertEqual(identity, f"{expected};{expected}")

    def test_authorized_review_repair_may_add_exact_changed_path_to_scope(self):
        commit_files(self.agent, {"src/a.c": "int riscv64;\n"}, msg="BLD: follow review feedback")
        self.mbox = git(self.agent, "format-patch", "--stdout", self.base).encode() + b"\n"
        self.task["task"] = "development_repair"
        self.task["review_scope_extension"] = True
        proposal = self.proposal(reason_ids=["ci-1"],
                                 changed_files=[".github/workflows/wheels.yml", "src/a.c"])
        out = self.run_publish(proposal=proposal)
        self.assertEqual(out.status, "published", out.reason)
        self.assertEqual(out.operation["proposed"]["changed_files"],
                         [".github/workflows/wheels.yml", "src/a.c"])

    def test_non_review_task_cannot_expand_scope(self):
        commit_files(self.agent, {"src/a.c": "int riscv64;\n"}, msg="BLD: unrelated scope")
        self.mbox = git(self.agent, "format-patch", "--stdout", self.base).encode() + b"\n"
        proposal = self.proposal(changed_files=[".github/workflows/wheels.yml", "src/a.c"])
        out = self.run_publish(proposal=proposal)
        self.assertEqual((out.status, out.failure_class), ("rejected", "validation"))
        self.assertIn("outside admitted scope", out.reason)

    def test_read_only_validates_but_does_not_push(self):
        out = self.run_publish(pol=policy(read_only=True))
        self.assertEqual(out.status, "rejected")
        self.assertIn("read-only", out.reason)
        self.assertEqual(self.fork_head(), self.base)

    def test_rejects_out_of_scope_and_lies(self):
        out = self.run_publish(scope=publisher.Scope(allowed_globs=["src/*"]))
        self.assertIn("outside admitted scope", out.reason)
        out = self.run_publish(self.proposal(changed_files=[".github/workflows/other.yml"]))
        self.assertIn("proposal claims files", out.reason)
        out = self.run_publish(self.proposal(patch_digest="0" * 64))
        self.assertIn("digest", out.reason)
        self.assertEqual(self.fork_head(), self.base)

    def test_rejects_stale_head_and_wrong_task(self):
        commit_files(self.agent, {"src/a.c": "int b;\n"}, msg="manual")
        git(self.agent, "push", "-q", "origin", "HEAD:riscv64-dev")
        out = self.run_publish()
        self.assertIn("publication check", out.reason)
        out = self.run_publish(self.proposal(expected_revision=3))
        self.assertIn("does not answer", out.reason)

    def test_head_moves_between_validation_and_push(self):
        heads = iter([self.base, "f" * 40])
        out = self.run_publish(observe=lambda: next(heads))
        self.assertIn("moved before push", out.reason)
        # The final check did not pass: no effect is begun and no intent is
        # recorded.
        self.assertEqual(self.ops, [])
        self.assertIsNone(out.operation)
        self.assertEqual(self.fork_head(), self.base)

    def test_lease_rejects_concurrent_push(self):
        # Fork moves *after* our last read: the lease, not the read, stops us.
        real = self.fork_head
        calls = {"n": 0}

        def observe():
            calls["n"] += 1
            if calls["n"] == 2:
                h = real()
                other = self.root / "other"
                git(self.root, "clone", "-q", "-b", "riscv64-dev", str(self.fork_bare), str(other))
                commit_files(other, {"src/a.c": "int z;\n"}, msg="race")
                git(other, "push", "-q", "origin", "HEAD:riscv64-dev")
                return h
            return real()
        out = self.run_publish(observe=observe)
        self.assertEqual(out.status, "rejected")
        self.assertIn("push rejected", out.reason)

    def test_garbage_and_binary_series(self):
        self.assertEqual(self.run_publish(mbox=b"not a patch\n",
                                          proposal=self.proposal(patch_digest=None)).status, "rejected")
        self.assertIn("binary", self.run_publish(mbox=b"x\nGIT binary patch\nliteral 1\n",
                                                 proposal=self.proposal(patch_digest=None)).reason)

    def test_debug_commit_rejected_once_upstream_facing(self):
        self.record["upstream_pr"] = {"id": 1, "number": 1, "url": "https://github.com/example/proj/pull/1",
                                      "head_repository_id": 2, "head_ref": "riscv64", "head_sha": self.base,
                                      "base_ref": "main", "registered_by": {"id": 1, "login": "x"}}
        self.record["phase"] = "upstream_review"
        self.task["ref"] = "riscv64"
        git(self.fork_bare, "branch", "riscv64", "riscv64-dev")
        git(self.agent, "reset", "-q", "--hard", self.base)
        commit_files(self.agent, {".github/workflows/wheels.yml": "on: [push]\n# fork\n"}, msg="{debug} enable")
        mbox = git(self.agent, "format-patch", "--stdout", self.base).encode() + b"\n"
        out = self.run_publish(self.proposal(patch_digest=None), mbox=mbox)
        self.assertIn("{debug}", out.reason)

    def test_proposal_cannot_substitute_fork_or_base(self):
        out = self.run_publish(self.proposal(fork_repository_id=999))
        self.assertIn("another fork", out.reason)
        out = self.run_publish(self.proposal(reconstruction_base="e" * 40))
        self.assertIn("reconstruction base", out.reason)
        self.task["fork_repository_id"] = 3
        self.assertIn("repository ID", self.run_publish().reason)

    def test_preflight(self):
        ok = fork_publish.preflight(record=self.record, task=self.task, proposal=self.proposal(),
                                    fork_lookup=lambda n: {"id": 2, "full_name": n, "archived": False})
        self.assertEqual(ok, {"publish": True, "status": "publish", "reason": None,
                              "fork_name": "proj", "failure_class": None})
        moved = fork_publish.preflight(record=self.record, task=self.task, proposal=self.proposal(),
                                       fork_lookup=lambda n: {"id": 77, "full_name": n})
        self.assertFalse(moved["publish"])
        blocked = fork_publish.preflight(record=self.record, task=self.task,
                                         proposal=self.proposal(outcome="blocked", operation="none"),
                                         fork_lookup=lambda n: {"id": 2, "full_name": n})
        self.assertFalse(blocked["publish"])
        unchanged = fork_publish.preflight(
            record=self.record, task=self.task,
            proposal=self.proposal(outcome="no_change", operation="none", patch_digest=None, changed_files=[]),
            fork_lookup=lambda _n: self.fail("no-change must not mint or inspect a fork token"))
        self.assertEqual(unchanged["status"], "no_change")
        self.assertFalse(unchanged["publish"])

    def test_rewrite_squash_and_empty_removal(self):
        # First land two commits, then squash them via a rewrite onto the base.
        commit_files(self.agent, {"src/a.c": "int a = 1;\n"}, msg="second")
        mbox2 = git(self.agent, "format-patch", "--stdout", self.base).encode() + b"\n"
        scope = publisher.Scope(allowed_globs=[".github/workflows/*", "src/*"])
        self.task["allowed_paths"] = scope.allowed_globs
        out = self.run_publish(self.proposal(changed_files=[".github/workflows/wheels.yml", "src/a.c"],
                                             patch_digest=None), mbox=mbox2, scope=scope)
        self.assertEqual(out.status, "published", out.reason)
        head2 = self.fork_head()
        self.record["fork"]["head_sha"] = head2
        self.task["expected_head"] = head2
        self.task["revision"] = 1
        self.record["revision"] = 1
        git(self.agent, "reset", "-q", "--soft", self.base)
        git(self.agent, "commit", "-q", "-m", "BLD: build riscv64 wheels (squashed)")
        squash = git(self.agent, "format-patch", "--stdout", self.base).encode() + b"\n"
        out = self.run_publish(self.proposal(operation="rewrite", expected_revision=1, expected_head=head2,
                                             changed_files=[], patch_digest=None), mbox=squash, scope=scope)
        self.assertEqual(out.status, "published", out.reason)
        self.assertEqual(git(self.fork_bare, "rev-list", "--count", f"{self.base}..riscv64-dev"), "1")
        self.assertEqual(self.ops[-1]["kind"], "branch_rewrite")
        # Empty rewrite = remove every contribution commit.
        head3 = self.fork_head()
        self.record["fork"]["head_sha"] = head3
        self.task.update(expected_head=head3, revision=2)
        self.record["revision"] = 2
        out = self.run_publish(self.proposal(operation="rewrite", expected_revision=2, expected_head=head3,
                                             changed_files=[], patch_digest=None), mbox=b"", scope=scope)
        self.assertEqual(out.status, "published", out.reason)
        self.assertEqual(self.fork_head(), self.base)
        # Empty append is refused.
        self.record["fork"]["head_sha"] = self.base
        self.task.update(expected_head=self.base, revision=3)
        self.record["revision"] = 3
        out = self.run_publish(self.proposal(expected_revision=3, changed_files=[], patch_digest=None), mbox=b"")
        self.assertIn("empty series", out.reason)

    def test_merge_topology_rejected(self):
        git(self.agent, "checkout", "-q", "-b", "side", self.base)
        commit_files(self.agent, {"src/a.c": "int side;\n"}, msg="side")
        git(self.agent, "checkout", "-q", "riscv64-dev")
        git(self.agent, "merge", "-q", "--no-ff", "-m", "merge", "side")
        mbox = git(self.agent, "format-patch", "--stdout", self.base).encode() + b"\n"
        # format-patch flattens merges: the rebuilt history is linear and must
        # still pass scope checks; a merge cannot be expressed at all.
        out = self.run_publish(self.proposal(changed_files=[], patch_digest=None), mbox=mbox,
                               scope=publisher.Scope(allowed_globs=[".github/workflows/*"]))
        self.assertEqual(out.status, "rejected")
        self.assertIn("outside the approved patch surface", out.reason)


if __name__ == "__main__":
    unittest.main()


class TestFailureClasses(_ForkFixture, unittest.TestCase):
    def test_infra_failures_are_retryable_not_validation(self):
        import subprocess
        def http500(w, a, c):
            return subprocess.CompletedProcess(a, 128, b"", b"error: RPC failed; HTTP 500")
        out = fork_publish.publish(record=self.record, task=self.task, proposal=self.proposal(), mbox=self.mbox,
                                   token=None, observe_head=self.fork_head,
                                   scope=publisher.Scope(allowed_globs=[".github/workflows/*"]),
                                   policy=policy(read_only=False), now=NOW, save_operation=self.ops.append,
                                   workdir=self.root / "w500", push=http500)
        self.assertEqual((out.status, out.failure_class), ("infra_error", "infrastructure"))
        def broken_api():
            raise RuntimeError("HTTP 502")
        out = self.run_publish(observe=broken_api)
        self.assertEqual((out.status, out.failure_class), ("infra_error", "infrastructure"))
        self.assertEqual(self.fork_head(), self.base)

    def test_validation_and_stale_classes(self):
        out = self.run_publish(scope=publisher.Scope(allowed_globs=["src/*"]))
        self.assertEqual(out.failure_class, "validation")
        out = self.run_publish(self.proposal(expected_revision=9))
        self.assertEqual(out.failure_class, "validation")
        commit_files(self.agent, {"src/a.c": "int b;\n"}, msg="manual")
        git(self.agent, "push", "-q", "origin", "HEAD:riscv64-dev")
        self.assertEqual(self.run_publish().failure_class, "stale")

    def test_retry_after_lost_receipt_confirms_instead_of_pushing(self):
        first = self.run_publish()
        self.assertEqual(first.status, "published")
        # Pretend the receipt was lost: the retry still names the old expected head.
        pushes = []
        out = fork_publish.publish(record=self.record, task=self.task, proposal=self.proposal(), mbox=self.mbox,
                                   token=None, observe_head=self.fork_head,
                                   scope=publisher.Scope(allowed_globs=[".github/workflows/*"]),
                                   policy=policy(read_only=False), now=NOW, save_operation=self.ops.append,
                                   workdir=self.root / "retry", previous_candidate=first.candidate,
                                   push=lambda *a: pushes.append(a))
        self.assertEqual(out.status, "published")
        self.assertIn("previous attempt", out.reason)
        self.assertEqual(pushes, [])


class TestPublishRetryIdentity(_ForkFixture, unittest.TestCase):
    """A publish-only retry of the same Result records its own operation:
    the operation an earlier attempt closed is never written again."""

    def attempt(self, n, push=None):
        ops = []
        out = fork_publish.publish(
            record=self.record, task=self.task, proposal=self.proposal(), mbox=self.mbox,
            token=None, observe_head=self.fork_head,
            scope=publisher.Scope(allowed_globs=[".github/workflows/*"]),
            policy=policy(read_only=False), now=NOW,
            save_operation=ops.append, workdir=self.root / f"attempt-{n}-{len(self.ops)}",
            push=push, attempt=n)
        self.ops.append(ops[-1])
        return out, ops[-1]

    def commit_all(self):
        """Commit the operations one after the other, as the state publisher
        does, and return the problems of each diff."""
        import json
        import subprocess

        import state
        repo = self.root / "store"
        git(self.root, "init", "-q", "-b", "main", str(repo))
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "t")
        (repo / "README").write_text("store\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "base")
        pol = json.loads((Path(__file__).resolve().parents[1] / "policy.json").read_text())
        problems = []
        for op in self.ops:
            base = git(repo, "rev-parse", "HEAD").strip()
            path = repo / state.record_path("operations", op)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(op, indent=2, sort_keys=True) + "\n")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", op["status"]], check=True)
            problems += state.validate_state_diff(repo, base, "HEAD", pol).problems
        return problems

    @staticmethod
    def http500(w, a, c):
        import subprocess
        return subprocess.CompletedProcess(a, 128, b"", b"error: RPC failed; HTTP 500")

    def test_a_retry_after_a_definite_failure_publishes_a_new_operation(self):
        out1, failed = self.attempt(1, push=self.http500)
        self.assertEqual((out1.status, failed["status"]), ("infra_error", "failed"))
        out2, confirmed = self.attempt(2)
        self.assertEqual((out2.status, confirmed["status"]), ("published", "confirmed"))
        self.assertNotEqual(failed["operation_id"], confirmed["operation_id"])
        self.assertIn("publish-attempt:2", confirmed["reason_ids"])
        self.assertNotIn("publish-attempt:1", failed["reason_ids"])
        self.assertEqual(self.commit_all(), [])

    def test_an_uncertain_retry_after_a_definite_failure_commits_cleanly(self):
        import subprocess
        _out, failed = self.attempt(1, push=self.http500)
        out, uncertain = self.attempt(
            3, push=lambda w, a, c: subprocess.CompletedProcess(a, 0, b"", b""))
        self.assertEqual((out.status, uncertain["status"]), ("uncertain", "uncertain"))
        self.assertEqual(len({failed["operation_id"], uncertain["operation_id"]}), 2)
        self.assertEqual(self.commit_all(), [])

    def test_the_attempt_is_a_positive_number(self):
        for bad in (0, -1, True, "2"):
            with self.subTest(attempt=bad), self.assertRaises(ValueError):
                self.attempt(bad)


class TestReviewedPublication(_ForkFixture, unittest.TestCase):
    """A development patch held for its review is verified without a push in
    its worker run, then published later, in another run, only when it
    rebuilds to exactly the verified and reviewed commit."""

    def setUp(self):
        super().setUp()
        self.task.update(task="development_patch", review=fork_publish.REVIEW_REQUIRED)
        self.record.update(activity="agent_running")

    def verify(self):
        self.invocations += 1
        return fork_publish.verify(
            record=self.record, task=self.task, proposal=self.proposal(), mbox=self.mbox,
            observe_head=self.fork_head,
            scope=publisher.Scope(allowed_globs=[".github/workflows/*"]),
            policy=policy(read_only=False), now=NOW, workdir=self.root / f"pub-{self.invocations}")

    def publishing(self, candidate):
        self.record.update(activity="publishing", pending_publication={
            "worker_run_id": "7", "attempts": 0, "last_reason": "", "next_attempt_at": NOW})
        self.record["held_patch"] = {
            "worker_run_id": "7", "artifact_id": 8, "work": None, "revision": 0,
            "patch_digest": hashlib.sha256(self.mbox).hexdigest(), "candidate": candidate,
            "expected_head": self.base}

    def test_the_verified_commit_is_rebuilt_exactly_when_it_is_published(self):
        verified = self.verify()
        self.assertEqual(verified.status, "verified", verified.reason)
        self.assertEqual(verified.patch_digest, hashlib.sha256(self.mbox).hexdigest())
        self.assertEqual((self.fork_head(), self.ops), (self.base, []))
        # The worker's own run never pushes a patch held for its review.
        self.assertIn("published only from development.publishing", self.run_publish().reason)
        self.assertEqual(self.fork_head(), self.base)
        # Later, in another run and at another time, the reviewed patch
        # rebuilds to the very commit that was verified and is pushed.
        time.sleep(1.1)
        self.publishing(verified.candidate)
        out = self.run_publish()
        self.assertEqual(out.status, "published", out.reason)
        self.assertEqual((out.candidate, self.fork_head()), (verified.candidate, verified.candidate))

    def test_a_rebuild_other_than_the_reviewed_commit_is_never_pushed(self):
        verified = self.verify()
        self.publishing("f" * 40)
        out = self.run_publish()
        self.assertEqual((out.status, out.failure_class), ("rejected", "validation"))
        self.assertIn("not to the reviewed commit", out.reason)
        self.assertEqual(self.fork_head(), self.base)
        self.publishing(verified.candidate)
        self.record["held_patch"]["patch_digest"] = "0" * 64
        self.assertIn("not the held patch", self.run_publish().reason)
        self.assertEqual(self.fork_head(), self.base)
