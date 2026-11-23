# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A wait for a human is left only by the rows the table lists for it.

Multi-tick loops through ``reconcile.tick``: the development human wait that
``exhaust_work_total`` and ``exhaust_work_recovery`` enter stays as it is
under failing, pending, and passing CI, posts nothing again, and is left by
``resume_development``, ``park_contribution``, or ``refresh_investigation``.
"""

from __future__ import annotations

import contextlib
import copy
import io
import unittest
from datetime import datetime, timedelta
from unittest import mock

import capacity
import compat
import engine
import lifecycle
import observe_ci
import reconcile
import schemas
import work
from tests.helpers import OWNER, SHA_A, ci_record, contribution, policy
from tests.test_late_results import ci_snapshot

P = policy()
T0 = datetime.fromisoformat("2026-11-23T09:00:00+00:00")
FAILING = "Build - ubuntu-24.04-riscv (riscv64)"


class World:
    """One contribution ticked again and again with the staged records
    folded back into its snapshot."""

    def __init__(self, record: dict, evidence: dict | None = None):
        self.snap = reconcile.Snapshot({"c": record}, {}, {}, {}, dict(evidence or {}))
        self.n = 0
        self.comment_id = 900

    @property
    def rec(self) -> dict:
        return self.snap.contributions["c"]

    def command(self, body: str) -> dict:
        self.comment_id += 1
        return {"issue_number": self.rec["tracking_issue"]["number"],
                "comment": {"id": self.comment_id, "body": body, "user": dict(OWNER)}}

    def tick(self, verdict: str = "failed", *, comments=(), queue_works=None, runs=None):
        now = T0 + timedelta(minutes=10 * self.n)
        self.n += 1
        head = self.rec["fork"]["head_sha"]
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": head}), \
                mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
                mock.patch("observe_ci.collect.ci_snapshot",
                           return_value=ci_snapshot(head, verdict)), \
                contextlib.redirect_stderr(io.StringIO()):
            out = reconcile.tick(object(), copy.deepcopy(self.snap), policy=P, now=now,
                                 publications=[], comments=list(comments), issues=[],
                                 pressure=capacity.Pressure(ok=True),
                                 dispatcher_runs=runs if runs is not None else [],
                                 queue_works=queue_works if queue_works is not None else {})
        assert out.failures == [], out.failures
        events = []
        for kind, record in out.staged:
            if kind == "contributions":
                schemas.validate("contribution", record)
                self.snap.contributions["c"] = record
            elif kind == "evidence":
                self.snap.evidence[record["evidence_id"]] = record
            elif kind == "events":
                events.append(record)
        submitted = [e for e in out.report.get("requested_effects", [])
                     if e.get("type") == "submit_work"]
        return events, submitted


def failing_record(**fields) -> tuple[dict, dict]:
    record = contribution(activity="fix_needed", last_ci_evidence_id="ci-0",
                          dossier_evidence_id="dossier-1", scope_paths=["a"],
                          scope_coverage_complete=True, **fields)
    return record, {"ci-0": ci_record("ci-0", SHA_A, failing=FAILING)}


def wait_from_work_total() -> World:
    """``exhaust_work_total`` from ``development.fix_needed``."""
    record, evidence = failing_record()
    record["budget"]["attempts_used"] = lifecycle.budget_limit("work_total", P)
    world = World(record, evidence)
    events, _ = world.tick()
    assert [e["kind"] for e in events] == ["exhaust_work_total"], events
    return world


def wait_from_work_recovery() -> World:
    """``exhaust_work_recovery`` after the Work graph ended without a Result
    on every attempt ``work_recovery`` allows."""
    record, evidence = failing_record()
    world = World(record, evidence)
    runs = [{"id": 1, "status": "completed", "created_at": "2026-12-30T00:00:00Z"}]
    for _ in range(lifecycle.budget_limit("work_recovery", P) + 2):
        aw = world.rec.get("active_work")
        queue = {work.graph_id(aw["dedupe_key"]): "completed"} if aw else {}
        events, _ = world.tick(queue_works=queue, runs=runs)
        if engine.state_key(world.rec) == "development.waiting_human":
            assert "exhaust_work_recovery" in [e["kind"] for e in events], events
            return world
    raise AssertionError(f"never exhausted: {engine.state_key(world.rec)}")


WAITS = {"exhaust_work_total": wait_from_work_total,
         "exhaust_work_recovery": wait_from_work_recovery}


class TestDevelopmentHumanWaitIsStable(unittest.TestCase):
    def test_observation_never_leaves_the_wait(self):
        for verb, enter in WAITS.items():
            for verdict in ("failed", "pending", "passed"):
                with self.subTest(entered_by=verb, ci=verdict):
                    world = enter()
                    held = copy.deepcopy(world.rec)
                    for _ in range(6):
                        events, submitted = world.tick(verdict)
                        self.assertEqual((events, submitted), ([], []))
                    # Nothing about the record changed: state, revision,
                    # pending action, head, CI evidence, and budgets.
                    self.assertEqual(world.rec, held)

    def test_the_wait_is_left_by_its_rows_only(self):
        for verb, enter in WAITS.items():
            exits = {
                "resume": ("/upstreaming resume revision={r} reason=fixed", "resume_development",
                           {"development.fix_needed", "development.agent_running"}),
                "park": ("/upstreaming park reason=done", "park_contribution", {"parked.idle"}),
                "refresh": ("/upstreaming refresh revision={r} reason=again",
                            "refresh_investigation",
                            # `work_total` is not started again by a refresh: once
                            # it is spent, the investigation takes `exhaust_work_total`.
                            {"queued.idle", "investigation.agent_running",
                             "investigation.waiting_human"}),
            }
            for name, (body, row, targets) in exits.items():
                with self.subTest(entered_by=verb, command=name):
                    world = enter()
                    world.tick("failed")
                    events, _ = world.tick(
                        "failed", comments=[world.command(body.format(r=world.rec["revision"]))])
                    self.assertIn(row, [e["kind"] for e in events])
                    self.assertIn(engine.state_key(world.rec), targets)

    def test_resume_starts_the_counter_and_requests_fresh_work(self):
        world = wait_from_work_total()
        events, submitted = world.tick(
            "failed", comments=[world.command(
                f"/upstreaming resume revision={world.rec['revision']} reason=go")])
        kinds = [e["kind"] for e in events]
        self.assertIn("resume_development", kinds)
        self.assertIn("request_repair", kinds)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(world.rec["active_work"]["attempt"], 1)
        self.assertEqual(world.rec["budget"]["attempts_used"], 1)


class TestLegacyWritersKeepTheWait(unittest.TestCase):
    def record(self, **fields):
        record, _ = failing_record()
        record.update({"activity": "waiting_human",
                       "pending_human_action": {"kind": "work_failed",
                                                "requested_at": record["updated_at"],
                                                "reminded_at": None, "escalated_at": None,
                                                "counter": "work_recovery"}, **fields})
        return record

    def test_a_legacy_write_cannot_leave_a_wait_of_the_table(self):
        record = self.record()
        for site in ("observe_ci.ci_passed", "observe_ci.review_feedback",
                     "reconcile.publication_outcome", "controller.plan_provisioning"):
            with self.subTest(site=site), self.assertRaises(lifecycle.TransitionError):
                compat.legacy_update(record, site, activity="waiting_ci")
        with self.assertRaises(lifecycle.TransitionError):
            compat.observe_status(record, activity="fix_needed", now=record["updated_at"])
        # The same write in the wait's own state, and every non-state field, is kept.
        self.assertEqual(compat.legacy_update(record, "observe_ci.ci_passed",
                                              activity="waiting_human")["activity"],
                         "waiting_human")

    def test_every_wait_without_an_observation_row_is_not_observed(self):
        for key, spec in lifecycle.LIFECYCLE.items():
            if spec.kind not in lifecycle.REMINDER_KINDS:
                continue
            phase, activity = key.split(".")
            record = self.record(phase=phase, activity=activity)
            observed = any(e.transition.trigger == lifecycle.Trigger.OBSERVATION
                           for e in lifecycle.state_edges(key))
            with self.subTest(state=key):
                self.assertTrue(compat.waits_for_human(record))
                self.assertEqual(observe_ci.human_wait_without_observation(record), not observed)
        # The legacy shape without a pending action is not a wait of the table.
        self.assertFalse(compat.waits_for_human(self.record(pending_human_action=None)))

    def test_review_feedback_and_a_new_head_are_not_folded_in_the_wait(self):
        from tests.test_late_results import FEEDBACK
        record = self.record()
        world = World(record, {"ci-0": ci_record("ci-0", SHA_A, failing=FAILING)})
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": "f" * 40}), \
                mock.patch("observe_ci.collect.development_review_feedback",
                           return_value=FEEDBACK), \
                mock.patch("observe_ci.collect.ci_snapshot",
                           return_value=ci_snapshot("f" * 40, "passed")):
            obs = observe_ci.observe(object(), contributions={"c": record}, operations={},
                                     contracts={}, policy=P, now=record["updated_at"],
                                     evidence=world.snap.evidence)
        self.assertEqual(obs.staged, [])
        self.assertIn("waits for a human", obs.report[0]["note"])

    def test_a_held_proposal_is_not_published_from_the_wait(self):
        record = self.record(proposal_artifact={
            "work_id": "w", "claim_id": "c", "artifact_id": 5, "name": "n",
            "digest": "sha256:" + "0" * 64, "run_id": "555", "result_commit": "d" * 40,
            "outcome": "proposal"})
        world = World(record, {"ci-0": ci_record("ci-0", SHA_A, failing=FAILING)})
        for verdict in ("passed", "failed"):
            events, submitted = world.tick(verdict)
            self.assertEqual((events, submitted), ([], []))
        self.assertEqual(world.rec, record)

    def test_a_result_in_the_wait_is_late(self):
        record = self.record()
        record["active_work"] = {
            "dedupe_key": work.work_key(record["contribution_id"], 0, record["revision"],
                                        "development_repair"),
            "task": "development_repair", "generation": 0, "revision": record["revision"],
            "attempt": 1, "requested_at": record["updated_at"],
            "deadline_at": "2026-11-30T00:00:00+00:00", "dispatched_run_id": None,
            "reason_ids": []}
        aw = record["active_work"]
        pub = {"status": "published", "worker_run_id": "5",
               "task": {"contribution_id": record["contribution_id"], "revision": aw["revision"],
                        "generation": 0, "dedupe_key": aw["dedupe_key"],
                        "task": aw["task"]}}
        self.assertIn("waits for a human", reconcile.result_mismatch(record, pub))


if __name__ == "__main__":
    unittest.main()
