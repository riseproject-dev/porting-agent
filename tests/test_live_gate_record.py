# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The live closeio--ciso8601/1 record in the shape its one hand edit
leaves it, outside the pipeline: development on `riscv64-dev` at b29a8f4,
the development PR #2 that a human opened from it (no pipeline marker), and
PR #1 from the removed `riscv64` closed. The edit changes `fork.head_ref`,
`fork.pr_number`, and `updated_at` only, with no event. The pipeline has no
migration for it and processes it like any record at the submission
gate."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collect
import engine
import handoff
import lifecycle
import schemas
import state
from common import canonical_json
from tests.helpers import policy
from tests.test_handoff import World, at
from tests.test_live_handoff_migration import CI_EVIDENCE, HEAD, comment

FIXTURES = Path(__file__).parent / "fixtures/live-gate-dev"
FORK = "riseproject-upstreaming/ciso8601"
RECORD_PATH = ".state/contributions/closeio--ciso8601--1.json"


def record() -> dict:
    return json.loads((FIXTURES / "contribution.json").read_text())


class TestTheHandEditedRecord(unittest.TestCase):
    def test_it_is_valid_and_no_migration_touches_it(self):
        value = record()
        schemas.validate("contribution", value)
        self.assertEqual((engine.state_key(value), value["revision"], value["fork"]["head_ref"],
                          value["fork"]["head_sha"], value["fork"]["pr_number"],
                          value["last_ci_evidence_id"]),
                         ("handoff.awaiting_submission", 6, "riscv64-dev", HEAD, 2, CI_EVIDENCE))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / RECORD_PATH
            path.parent.mkdir(parents=True)
            path.write_text(canonical_json(value))
            self.assertEqual(state.pending_record_migrations(root, policy()), [])
            self.assertEqual(state.apply_pending_record_migrations(
                root, policy(), now="2026-11-23T14:00:00+00:00"), [])

    def test_the_state_checks_accept_the_edit_without_an_event(self):
        from tests.helpers import commit_files, git, init_repo
        before = dict(record(), fork=dict(record()["fork"], head_ref="riscv64", pr_number=1),
                      updated_at="2026-11-22T22:54:12.110885+00:00")
        with tempfile.TemporaryDirectory() as directory:
            repo = init_repo(Path(directory) / "repo")
            base = commit_files(repo, {RECORD_PATH: canonical_json(before)}, msg="before")
            edited = commit_files(repo, {RECORD_PATH: canonical_json(record())}, msg="edit")
            report = state.validate_state_diff(repo, base, edited, policy())
            self.assertTrue(report.ok, report.problems)
            self.assertEqual(report.changed, [RECORD_PATH])
            self.assertEqual(git(repo, "diff", "--name-only", base, edited), RECORD_PATH)

    def test_the_gate_reads_riscv64_dev_and_changes_nothing(self):
        world = World(record=record())
        world.gh.branches = {"riscv64-dev": HEAD, "riscv64": None}
        before = copy.deepcopy(world.record)
        out = world.tick(at(0))
        self.assertEqual((world.kinds(out), world.record), ([], before))
        [entry] = out.report["observation"]
        self.assertEqual((entry["ref"], entry["observed_head"]), ("riscv64-dev", HEAD))
        for key in ("requested_effects", "handoff_requests", "dev_pr_requests"):
            self.assertEqual(out.report[key], [], key)

    def test_the_review_reads_of_the_gate_read_pr_2(self):
        paths = []

        class Api:
            def paginate(self, path, **_kwargs):
                paths.append(path)
                return []

            def get(self, path):
                paths.append(path)
                raise AssertionError(path)

        self.assertIsNone(collect.development_review_feedback(Api(), record(), [1]))
        self.assertEqual(sorted(paths), sorted([
            f"/repos/{FORK}/pulls/2/reviews?per_page=100",
            f"/repos/{FORK}/pulls/2/comments?per_page=100",
            f"/repos/{FORK}/issues/2/comments?per_page=100"]))

    def test_the_submission_and_the_guard_proceed(self):
        world = World(record=record())
        world.gh.branches = {"riscv64-dev": HEAD, "riscv64": None}
        out = world.tick(at(0), comments=[comment(
            f"/upstreaming submit-upstream revision=6 head={HEAD}")])
        self.assertEqual(world.kinds(out), ["submit_upstream", "request_submission_rewrite"])
        self.assertEqual(lifecycle.work_ref(world.record), "riscv64")
        # `riscv64` is missing and no pull request of the fork is open from
        # it (PR #2 is from `riscv64-dev`, PR #1 is closed): the build of
        # `riscv64` may write it.
        with mock.patch("collect.branch_head",
                        lambda _a, _f, ref: {"head": HEAD} if ref == "riscv64-dev" else None), \
                mock.patch("collect.fork_pull_requests", lambda _a, _f, ref: []):
            self.assertIsNone(handoff.upstream_branch_blocked(object(), world.record))

    def test_the_fork_pr_inventory_asks_for_open_prs_from_riscv64_only(self):
        asked = []

        class Api:
            def get(self, path):
                asked.append(path)
                return []

        self.assertEqual(collect.fork_pull_requests(Api(), FORK, "riscv64"), [])
        self.assertEqual(asked, [f"/repos/{FORK}/pulls?state=open&head=riseproject-upstreaming"
                                 "%3Ariscv64&per_page=100"])


if __name__ == "__main__":
    unittest.main()
