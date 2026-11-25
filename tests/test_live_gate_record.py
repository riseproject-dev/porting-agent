# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The live closeio--ciso8601/1 record in the shape its one hand edit
leaves it, outside the pipeline: development on `riscv64-dev` at b29a8f4,
the development PR #2 that a human opened from it (no pipeline marker), and
PR #1 from the removed `riscv64` closed. The edit changes `fork.head_ref`,
`fork.pr_number`, and `updated_at` only, with no event. The pipeline has no
migration for it and processes it like any record at the submission
gate, once the record migration of the strict schema added its missing
fields."""

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


def committed() -> dict:
    return json.loads((FIXTURES / "contribution.json").read_text())


def record() -> dict:
    """The record after the record migration that adds the fields of the
    strict schema; it changes nothing this file checks."""
    return state.migrate_record_fields(committed(), now="2026-11-23T13:00:00+00:00")[0]


class TestTheHandEditedRecord(unittest.TestCase):
    def test_it_is_valid_once_the_record_migration_adds_the_strict_fields(self):
        self.assertTrue(state.needs_field_migration(committed()))
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


class TestTheLiveRecordAndItsHistory(unittest.TestCase):
    """The committed live record takes the history step without change."""

    def live(self) -> dict:
        path = Path(__file__).parents[1] / RECORD_PATH
        if not path.is_file():
            self.skipTest("the live record is not in this checkout")
        value = json.loads(path.read_text())
        if state.needs_field_migration(value):
            value = state.migrate_record_fields(value, now="2026-11-23T13:00:00+00:00")[0]
        return value

    def world(self) -> World:
        world = World(record=self.live())
        world.gh.branches = {"riscv64-dev": HEAD, "riscv64": None}
        # The committed evidence of the contribution, as the store holds it.
        for path in (Path(__file__).parents[1] / ".state/evidence").glob("*.json"):
            value = json.loads(path.read_text())
            if value.get("contribution_id") == world.record["contribution_id"]:
                world.evidence[value["evidence_id"]] = value
        return world

    def test_a_tick_stages_nothing_new_and_reads_no_steering(self):
        world = self.world()
        before = copy.deepcopy(world.record)
        with mock.patch("collect.steering") as steering:
            out = world.tick(at(0))
        steering.assert_not_called()
        self.assertEqual(out.report["work_history"], [])
        self.assertEqual([r["kind"] for k, r in out.staged if k == "evidence"
                          and r["kind"] in ("work_history", "human_steering", "worker_report")],
                         [])
        self.assertEqual((world.kinds(out), world.record), ([], before))

    def test_the_rewrite_task_gets_its_history_in_the_submitting_tick(self):
        world = self.world()
        paths = []

        def paginate(path, **_kwargs):
            paths.append(path)
            return []

        world.gh.paginate = paginate
        out = world.tick(at(0), comments=[comment(
            f"/upstreaming submit-upstream revision=6 head={HEAD}")])
        self.assertEqual(world.kinds(out), ["submit_upstream", "request_submission_rewrite"])
        [history] = [r for k, r in out.staged if k == "evidence" and r["kind"] == "work_history"]
        self.assertEqual(history["data"]["work"]["task"], "submission_rewrite")
        # An earlier task left no report: the timeline starts with the CI runs.
        self.assertEqual({t["kind"] for t in history["data"]["timeline"]}, {"ci"})
        self.assertGreaterEqual(len(history["data"]["timeline"]), 4)
        self.assertEqual(sorted(p.split("?")[0] for p in paths), sorted([
            "/repos/riseproject-dev/porting-agent/issues/1/comments",
            f"/repos/{FORK}/pulls/2/reviews", f"/repos/{FORK}/pulls/2/comments",
            f"/repos/{FORK}/issues/2/comments"]))
