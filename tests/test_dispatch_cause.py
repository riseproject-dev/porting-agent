# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A dispatch that failed for the dispatch path is not a failure of the
contribution: what its allocation charged is recorded, the wait records the
version of the code it failed at, and the re-arm gives the budgets back."""

from __future__ import annotations

import unittest

import engine
import lifecycle
import reconcile
from tests import test_development_table as dev
from tests.helpers import contribution, policy

POLICY = policy()
NOW = "2026-11-23T10:00:00+00:00"


def waiting(**fields):
    rec = contribution(phase="development", activity="waiting_human", **fields)
    rec["pending_human_action"] = {"kind": "dispatch_failed", "requested_at": NOW,
                                   "reminded_at": None, "escalated_at": None,
                                   "counter": "dispatch_recovery"}
    return rec


def rearm(rec):
    return engine.try_apply_transition(rec, "rearm_dispatch", policy=POLICY, now=NOW,
                                       facts={"dispatch_cause_changed"})


class TestWhatAnAllocationCharged(unittest.TestCase):
    def test_a_repair_records_the_counter_of_its_failing_job_and_the_work_total(self):
        out = dev.tick(dev.record("waiting_ci"), verdict="failed",
                       evidence={"plan-1": {"schema_version": 1, "evidence_id": "plan-1",
                                            "kind": "plan", "policy_version": "p",
                                            "collected_at": NOW, "summary": "s",
                                            "data": {"proposal": {}}}})
        self.assertEqual(dev.kinds(out), ["record_ci_failure", "request_repair"])
        record = dev.final(out, None)
        [cluster] = record["budget"]["clusters"]
        self.assertEqual(record["active_work"]["charged"],
                         [f"repair_attempts_cluster:{cluster}", "work_total"])
        self.assertEqual(record["budget"]["attempts_used"], 1)

    def test_a_fresh_graph_after_a_terminal_one_charged_its_own_allocation_only(self):
        rec = dev.running(active_work=dict(dev.WORK, charged=["work_total"],
                                           dedupe_key="example--proj/12@g0.r0:development_repair"))
        import work
        out = dev.tick(rec, observe=False, queue={work.graph_id(
            rec["active_work"]["dedupe_key"]): "cancelled"},
            now=dev.T)
        self.assertEqual(dev.kinds(out), ["allocate_work_retry"])
        # `allocate_work_retry` charged `work_total` for its own graph: a
        # later re-arm gives back one, not the charge of the graph before.
        self.assertEqual(dev.final(out, rec)["active_work"]["charged"], ["work_total"])

    def test_a_graph_reallocated_after_a_failure_of_the_pipeline_keeps_the_allocation(self):
        import controller
        rec = dev.running(active_work=dict(dev.WORK, charged=["work_total"],
                                           dedupe_key="example--proj/12@g0.r0:development_repair"))
        fresh = controller._fresh_work(rec, rec["active_work"], __import__("datetime").datetime
                                       .fromisoformat(NOW))
        out = engine.try_apply_transition(
            rec, "reallocate_after_pipeline_failure", policy=POLICY, now=NOW,
            facts={"work_terminal_without_result", "pipeline_failure",
                   "pipeline_failure_cleared"}, values={"active_work": fresh})
        self.assertEqual(out.record["active_work"]["charged"], ["work_total"])
        self.assertEqual(out.record["budget"]["attempts_used"], rec["budget"]["attempts_used"])


class TestTheWaitRecordsWhatTheReArmNeeds(unittest.TestCase):
    def lost(self, aw, **kw):
        rec = dev.running(active_work=dict(dev.WORK, **aw))
        run = {"id": 9, "status": "completed", "created_at": "2026-11-23T09:30:00Z"}
        limit = lifecycle.budget_limit("dispatch_recovery", POLICY)
        return reconcile._lost_dispatch(rec, rec["active_work"], run, limit + 1, now=dev.T,
                                        policy=POLICY, **kw)

    def test_the_exhaustion_of_a_lost_dispatch_records_the_version_and_the_credit(self):
        new, event, _decision = self.lost(
            {"charged": ["repair_attempts_cluster:build", "work_total"]}, pipeline="v" * 64)
        self.assertEqual(new["pending_human_action"]["pipeline_version"], "v" * 64)
        self.assertEqual(new["pending_human_action"]["credit"],
                         ["repair_attempts_cluster:build", "work_total"])
        self.assertEqual(event["details"]["pipeline_version"], "v" * 64)

    def test_work_allocated_before_the_credit_was_recorded_records_none(self):
        new, _event, _decision = self.lost({})
        self.assertNotIn("credit", new["pending_human_action"])
        self.assertNotIn("pipeline_version", new["pending_human_action"])
        schema = __import__("schemas")
        schema.validate("contribution", new)


class TestTheReArmGivesTheBudgetsBack(unittest.TestCase):
    def test_the_credit_names_what_is_given_back(self):
        rec = waiting(budget=dict(contribution()["budget"], attempts_used=5,
                                  clusters={"build": 2}))
        rec["pending_human_action"]["credit"] = ["repair_attempts_cluster:build", "work_total"]
        out = rearm(rec)
        self.assertEqual((out.record["budget"]["attempts_used"], out.record["budget"]["clusters"]),
                         (4, {"build": 1}))
        self.assertEqual(out.event["details"]["credited"],
                         ["repair_attempts_cluster:build", "work_total"])

    def test_a_cluster_given_back_to_zero_is_removed(self):
        rec = waiting(budget=dict(contribution()["budget"], attempts_used=1,
                                  clusters={"build": 1}))
        rec["pending_human_action"]["credit"] = ["repair_attempts_cluster:build"]
        out = rearm(rec)
        self.assertEqual((out.record["budget"]["attempts_used"], out.record["budget"]["clusters"]),
                         (1, {}))

    def test_a_wait_without_a_credit_gives_back_the_work_total_alone(self):
        rec = waiting(budget=dict(contribution()["budget"], attempts_used=3,
                                  clusters={"build": 2}))
        out = rearm(rec)
        self.assertEqual((out.record["budget"]["attempts_used"], out.record["budget"]["clusters"]),
                         (2, {"build": 2}))

    def test_a_budget_is_never_given_back_below_zero(self):
        out = rearm(waiting(budget=dict(contribution()["budget"], attempts_used=0)))
        self.assertEqual(out.record["budget"]["attempts_used"], 0)

    def test_only_the_budgets_the_row_may_credit_are_given_back(self):
        rec = waiting(budget=dict(contribution()["budget"], attempts_used=3))
        rec["pending_human_action"]["credit"] = ["dispatch_recovery", "no_progress"]
        out = rearm(rec)
        self.assertEqual(out.record["budget"]["attempts_used"], 3)
        self.assertNotIn("credited", out.event["details"])

    def test_no_other_row_gives_a_budget_back(self):
        rows = [e for e in lifecycle.table_edges() if e.transition.effects.credits]
        self.assertTrue(rows)
        self.assertEqual({e.verb for e in rows}, {"rearm_dispatch"})
        self.assertTrue(all(set(e.transition.effects.credits) <= set(lifecycle.CREDITABLE)
                            for e in rows))

    def test_the_cause_must_have_changed(self):
        refused = engine.try_apply_transition(waiting(), "rearm_dispatch", policy=POLICY, now=NOW)
        self.assertEqual(refused.kind, engine.Refusal.FACT_MISSING)


if __name__ == "__main__":
    unittest.main()
