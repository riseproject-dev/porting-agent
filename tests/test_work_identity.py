# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The Work identity (generation, revision, Work id) and the budgets of Work.

A Result is folded only when the generation, the revision, and the Work id it
carries back match the record; every other Result takes
``record_late_effect``. Work is allocated by the rows of the table, which
charge its budgets once, and an exhausted budget takes the exhaustion row
that names it, so the resume starts exactly that counter again.
"""

from __future__ import annotations

import copy
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import capacity
import ci_evidence
import commands
import controller
import engine
import isolation
import lifecycle as lc
import reconcile
import schemas
import state
import work
from tests import helpers
from tests.helpers import NOW, SHA_A, SHA_B, SHA_C, apply_command, ci_record, contribution, policy
from tests.test_engine import scenario
from tests.test_late_results import UPSTREAM_PR, events, final, publication, tick

T = datetime.fromisoformat("2026-11-23T09:00:00+00:00")
POLICY = policy()


def requested(record, task="development_repair"):
    """``record`` holding Work allocated at its generation and revision."""
    generation, revision = record["generation"], record["revision"]
    return dict(record, activity="agent_running", active_work={
        "dedupe_key": work.work_key(record["contribution_id"], generation, revision, task),
        "task": task, "generation": generation, "revision": revision, "attempt": 1,
        "requested_at": NOW, "deadline_at": "2026-11-24T09:00:00+00:00",
        "dispatched_run_id": None, "reason_ids": [], "feedback_evidence_ids": []})


def result(record, name="published", **task):
    """A Result of the Work ``record`` holds, with ``task`` overriding its payload."""
    pub = publication(record, name)
    pub["task"].update(task)
    return pub


def select(record, *, evidence=None, now=T, slots=1):
    res = controller.tick(contributions={"c": record}, operations={}, consumed={}, projects={},
                          policy=policy(concurrency=dict(POLICY["concurrency"],
                                                         coding_workers=slots)),
                          inputs=controller.Inputs(now=now, pressure=capacity.Pressure(ok=True)),
                          evidence=evidence or {})
    staged = [r for kind, r in res.staged if kind == "contributions"]
    return res, (staged[-1] if staged else record)


def kinds(res):
    return [e["kind"] for kind, e in res.staged if kind == "events"]


class TestLateResultMatrix(unittest.TestCase):
    """Generation, revision, and Work id: all three must match."""

    def record(self, generation=1, revision=5):
        return requested(contribution(generation=generation, revision=revision,
                                      plan_evidence_id="plan-1"))

    def test_only_the_exact_identity_is_folded(self):
        record = self.record()
        key = record["active_work"]["dedupe_key"]
        other_key = work.work_key(record["contribution_id"], 1, 5, "development_patch")
        cases = {
            "exact": ({}, None),
            "older generation": ({"generation": 0}, "late Result of generation 0, current is 1"),
            "later generation": ({"generation": 2}, "late Result of generation 2, current is 1"),
            "older revision": ({"revision": 4}, "late Result of revision 4, current is 5"),
            "later revision": ({"revision": 6}, "late Result of revision 6, current is 5"),
            "other Work": ({"dedupe_key": other_key},
                           "late Result without matching requested Work"),
            "no Work id": ({"dedupe_key": None}, "late Result: its task names no Work identity"),
        }
        for name, (task, why) in cases.items():
            for status in ("published", "rejected_validation", "no_change", "worker_blocked"):
                with self.subTest(case=name, status=status):
                    pub = result(record, status, **task)
                    if task.get("dedupe_key", key) is None:
                        del pub["task"]["dedupe_key"]
                    self.assertEqual(reconcile.result_mismatch(record, pub), why)
                    output = tick(record, [pub], capacity_ok=False)
                    after = final(output, record)
                    if why is None:
                        self.assertNotIn("record_late_effect", [e["kind"] for e in events(output)])
                        self.assertIsNone(after["active_work"])
                        continue
                    self.assertEqual([e["kind"] for e in events(output)], ["record_late_effect"])
                    late = events(output)[0]["details"]
                    self.assertEqual(late["why"], why)
                    self.assertEqual(dict(after, processed_publications=[]),
                                     dict(record, processed_publications=[]))

    def test_a_payload_without_the_identity_names_no_work(self):
        """Only an integer generation and a string Work id name a Work
        identity; every other payload, also one that names neither, names
        none, and its Result is late in every generation."""
        bare = {"contribution_id": "example--proj/12", "revision": 5}
        self.assertEqual(work.task_identity(dict(bare, generation=0, dedupe_key="k")), (0, "k"))
        for task in (bare, dict(bare, generation=0), dict(bare, dedupe_key="k"),
                     dict(bare, generation=True, dedupe_key="k"),
                     dict(bare, generation="0", dedupe_key="k"),
                     dict(bare, generation=0, dedupe_key=7),
                     dict(bare, generation=0, dedupe_key=None)):
            with self.subTest(task=task):
                self.assertEqual(work.task_identity(task), (None, None))
        for generation in (0, 1):
            with self.subTest(generation=generation):
                record = self.record(generation=generation, revision=5)
                pub = dict(publication(record, "published"), task=dict(bare))
                self.assertEqual(reconcile.result_mismatch(record, pub),
                                 "late Result: its task names no Work identity")
                output = tick(record, [pub], capacity_ok=False)
                self.assertEqual([e["kind"] for e in events(output)], ["record_late_effect"])
                late = events(output)[0]["details"]
                self.assertEqual((late["task_generation"], late["task_work"]), (None, None))
                after = final(output, record)
                self.assertEqual(dict(after, processed_publications=[]),
                                 dict(record, processed_publications=[]))
                self.assertEqual(after["processed_publications"], [999])

    def test_every_payload_carries_the_identity(self):
        """Every Work the dispatcher submits names its generation and Work
        id, and the pending feedback ids it answers."""
        tasks = sorted(work.WRITE_TASKS) + list(work.SUBMISSION_TASKS) + [
            "planning", work.DEVELOPMENT_REVIEW]
        for task in tasks:
            for generation in (0, 2):
                with self.subTest(task=task, generation=generation):
                    record = requested(contribution(generation=generation, revision=5), task)
                    payload = work.node(work.request_from_active(record), record,
                                        control_repo_id="1", allowed_paths=["a"])["payload"]
                    for name in ("generation", "dedupe_key", "feedback_evidence_ids"):
                        self.assertIn(name, payload)
                    self.assertEqual(work.task_identity(payload),
                                     (generation, record["active_work"]["dedupe_key"]))

    def test_work_of_no_or_another_generation_is_not_dispatched(self):
        record = self.record(generation=1, revision=5)
        self.assertIsNotNone(work.request_from_active(record))
        for generation in (None, 0, 2, "1"):
            with self.subTest(generation=generation):
                other = copy.deepcopy(record)
                if generation is None:
                    del other["active_work"]["generation"]
                else:
                    other["active_work"]["generation"] = generation
                self.assertIsNone(work.request_from_active(other))
        # The field migration carries Work recorded without a generation as
        # generation 0: it is then dispatched in generation 0 only.
        for generation in (0, 1):
            with self.subTest(migrated=generation):
                old = copy.deepcopy(self.record(generation=generation, revision=5))
                del old["active_work"]["generation"]
                migrated, _event = state.migrate_record_fields(old, now=NOW)
                self.assertEqual(migrated["active_work"]["generation"], 0)
                self.assertEqual(work.request_from_active(migrated) is not None,
                                 generation == 0)

    def test_work_left_by_a_result_without_identity_is_retried_then_waits(self):
        """The Work an identity-less Result leaves requested is terminal
        without a usable Result once its graph completed: it takes
        ``allocate_work_retry`` within ``work_recovery``, then
        ``exhaust_work_recovery`` to the human wait of the phase."""
        record = self.record(generation=0, revision=5)
        pub = dict(publication(record, "published"),
                   task={"contribution_id": record["contribution_id"], "revision": 5})
        after = final(tick(record, [pub], capacity_ok=False), record)
        self.assertEqual(after["active_work"], record["active_work"])
        graph = work.graph_id(after["active_work"]["dedupe_key"])
        _, staged, _ = reconcile.dispatch_recovery(
            {"c": copy.deepcopy(after)}, runs=[], queue_works={graph: "completed"}, now=T,
            policy=POLICY, work_results=None, collected=frozenset({graph}))
        self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                         ["allocate_work_retry"])
        retried = [r for kind, r in staged if kind == "contributions"][-1]
        self.assertNotEqual(retried["active_work"]["dedupe_key"],
                            after["active_work"]["dedupe_key"])
        # `work_recovery` counts the attempts of the Work, `work_total` the
        # attempts of the contribution: each is charged once.
        self.assertEqual(retried["active_work"]["attempt"], after["active_work"]["attempt"] + 1)
        self.assertEqual(retried["budget"]["attempts_used"],
                         after["budget"]["attempts_used"] + 1)
        exhausted = copy.deepcopy(after)
        exhausted["active_work"]["attempt"] = lc.budget_limit("work_recovery", POLICY)
        _, staged, _ = reconcile.dispatch_recovery(
            {"c": exhausted}, runs=[], queue_works={graph: "completed"}, now=T,
            policy=POLICY, work_results=None, collected=frozenset({graph}))
        self.assertEqual([e["kind"] for kind, e in staged if kind == "events"],
                         ["exhaust_work_recovery"])
        waiting = [r for kind, r in staged if kind == "contributions"][-1]
        self.assertEqual(engine.state_key(waiting), "development.waiting_human")

    def test_no_pre_identity_reading_is_left(self):
        self.assertFalse(hasattr(work, "work_generation"))
        root = Path(__file__).resolve().parents[1]
        tracked = subprocess.run(["git", "ls-files", "*.py"], cwd=root, capture_output=True,
                                 text=True, check=True).stdout.split()
        phrases = ("before the Work identity " + "carried", "before the identity " + "carried")
        for name in tracked:
            text = (root / name).read_text(encoding="utf-8")
            for phrase in phrases:
                with self.subTest(file=name, phrase=phrase):
                    self.assertNotIn(phrase, text)

    def test_an_older_artifact_of_the_same_work_is_late_once_the_newest_is_folded(self):
        record = self.record()
        newest = dict(result(record, "published"), publication_artifact_id=1001)
        older = dict(result(record, "infra_error"), publication_artifact_id=1000)
        output = tick(record, [older, newest], capacity_ok=False)
        self.assertEqual(final(output, record)["activity"], "waiting_ci")
        late = [e for e in events(output) if e["kind"] == "record_late_effect"]
        self.assertEqual([e["details"]["why"] for e in late], ["superseded publication artifact"])
        self.assertEqual(sorted(final(output, record)["processed_publications"]), [1000, 1001])
        again = tick(final(output, record), [older, newest], capacity_ok=False)
        self.assertEqual(events(again), [])

    def test_the_publisher_checks_the_identity(self):
        record = self.record()
        key = record["active_work"]["dedupe_key"]
        check = dict(expected_revision=5, expected_head=SHA_A, observed_head=SHA_A,
                     ref="riscv64-dev", repository="riseproject-upstreaming/proj")
        self.assertIsNone(lc.publication_check(record, expected_generation=1, work_key=key, **check))
        self.assertEqual(lc.publication_check(record, expected_generation=0, work_key=key, **check),
                         "work generation changed")
        self.assertEqual(lc.publication_check(record, expected_generation=1, work_key="other",
                                              **check), "the requested Work is other Work")
        self.assertEqual(lc.publication_check(dict(record, active_work=None), expected_generation=1,
                                              work_key=key, **check),
                         "no requested Work or pending publication")
        held = dict(record, active_work=None, pending_publication={
            "worker_run_id": "555", "attempts": 1, "last_reason": "x", "next_attempt_at": NOW})
        self.assertIsNone(lc.publication_check(held, expected_generation=1, work_key=key, **check))
        self.assertEqual(lc.publication_check(dict(record, revision=6), expected_generation=1,
                                              work_key=key, **check), "work revision changed")

    def test_a_changed_head_waits_for_the_requested_work(self):
        record = self.record()
        output = tick(record, observe=True, head=SHA_B)
        after = final(output, record)
        self.assertEqual((after["revision"], after["fork"]["head_sha"]), (5, SHA_A))
        # `development.agent_running` has no observation row.
        self.assertEqual(output.report["observation"][0]["note"],
                         "development.agent_running has no observation row")
        # The Result of that Work is then folded at its revision.
        folded = final(tick(record, [result(record, "published")], capacity_ok=False), record)
        self.assertEqual(folded["activity"], "waiting_ci")
        observed = final(tick(folded, observe=True, head=SHA_B), folded)
        self.assertEqual((observed["revision"], observed["fork"]["head_sha"]), (6, SHA_B))


class TestAllocation(unittest.TestCase):
    """Work is allocated by the table's rows; each allocation charges once."""

    def test_planning_begins_in_a_commit_of_its_own(self):
        record = contribution(phase="queued", activity="idle", fork=None)
        res, begun = select(record)
        self.assertEqual(kinds(res), ["begin_planning"])
        self.assertEqual(engine.state_key(begun), "planning.idle")
        self.assertIsNone(begun["active_work"])
        self.assertEqual(begun["budget"]["attempts_used"], 0)
        self.assertEqual(res.requested_effects, [])
        # The Work is allocated from `planning.idle` in a later tick.
        res, after = select(begun)
        self.assertEqual(kinds(res), ["dispatch_planning"])
        self.assertEqual(engine.state_key(after), "planning.agent_running")
        self.assertEqual(after["budget"]["attempts_used"], 1)
        self.assertEqual(after["active_work"]["dedupe_key"], "example--proj/12@g0.r0:planning")
        self.assertEqual(res.requested_effects[0]["generation"], 0)
        # Without a worker slot planning still begins, and waits in `planning.idle`.
        res, waiting = select(record, slots=0)
        self.assertEqual((kinds(res), engine.state_key(waiting)),
                         (["begin_planning"], "planning.idle"))
        res, still = select(waiting, slots=0)
        self.assertEqual((kinds(res), still), ([], waiting))
        # A paused request changes nothing.
        paused = dict(record, paused=True, paused_at=NOW, paused_reason="x", paused_by="u")
        res, same = select(paused)
        self.assertEqual((kinds(res), same), ([], paused))

    def test_work_total_spent_waits_after_planning_begins(self):
        limit = lc.budget_limit("work_total", POLICY)
        record = contribution(phase="queued", activity="idle", fork=None)
        record = dict(record, budget=dict(record["budget"], attempts_used=limit))
        res, begun = select(record)
        self.assertEqual(kinds(res), ["begin_planning"])
        res, waiting = select(begun)
        self.assertEqual(kinds(res), ["exhaust_work_total"])
        self.assertEqual(waiting["activity"], "waiting_human")

    def test_the_task_follows_the_row_and_the_pending_feedback(self):
        # A refreshed plan that returns to the fork with no failing CI job and
        # no pending feedback is answered by a patch.
        base = contribution(phase="planning", activity="agent_running", plan_evidence_id="plan-1",
                            refresh={"command_id": "c", "reason": "r", "from_phase": "development",
                                     "head_sha": SHA_A, "pr_number": 7})
        fix = dict(base, phase="development", activity="fix_needed", refresh=None)
        res, after = select(fix)
        self.assertEqual(kinds(res), ["request_patch"])
        self.assertEqual(after["active_work"]["task"], "development_patch")
        # Pending feedback to answer: a repair, still by `request_patch`.
        feedback = dict(fix, pending_feedback_evidence_ids=["review-1"])
        res, after = select(feedback, evidence={"review-1": {"evidence_id": "review-1",
                                                             "kind": "human_relay"}})
        self.assertEqual(kinds(res), ["request_patch"])
        self.assertEqual(after["active_work"]["task"], "development_repair")
        # A failing job: `request_repair`.
        failing = dict(fix, last_ci_evidence_id="ci-9")
        res, after = select(failing, evidence={"ci-9": ci_record("ci-9", SHA_A,
                                                                 failing="build riscv64")})
        self.assertEqual(kinds(res), ["request_repair"])
        self.assertEqual(after["active_work"]["task"], "development_repair")

    def test_a_rejected_outcome_charges_no_progress_whatever_progress_it_reports(self):
        for name in ("rejected_validation", "worker_blocked"):
            for phase in ("development", "upstream_review"):
                with self.subTest(result=name, phase=phase):
                    extra = {"upstream_pr": copy.deepcopy(UPSTREAM_PR)} \
                        if phase == "upstream_review" else {}
                    record = requested(contribution(phase=phase, activity="fix_needed",
                                                    plan_evidence_id="plan-1", **extra),
                                       "upstream_revision" if phase == "upstream_review"
                                       else "development_patch")
                    pub = publication(record, name)
                    pub = dict(pub, proposal=dict(pub.get("proposal") or {},
                                                  progress="new_causal_evidence"),
                               task={"contribution_id": record["contribution_id"],
                                     "revision": record["revision"], "generation": 0,
                                     "dedupe_key": record["active_work"]["dedupe_key"]})
                    folded, _staged = reconcile.apply_publication(record, pub, now=NOW,
                                                                  policy=POLICY)
                    self.assertEqual(folded["budget"]["no_progress_streak"],
                                     record["budget"]["no_progress_streak"] + 1)

    def test_repair_charges_its_failing_job_once_at_allocation(self):
        record = contribution(activity="fix_needed", last_ci_evidence_id="ci-9",
                              plan_evidence_id="plan-1")
        evidence = {"ci-9": ci_record("ci-9", SHA_A, failing="build riscv64")}
        res, after = select(record, evidence=evidence)
        self.assertEqual(kinds(res), ["request_repair"])
        self.assertEqual(after["budget"]["clusters"], {"build riscv64": 1})
        self.assertEqual(after["budget"]["attempts_used"], 1)
        details = [e for k, e in res.staged if k == "events"][0]["details"]
        self.assertEqual((details["cluster"], details["charged"]),
                         ("build riscv64", ["repair_attempts_cluster", "work_total"]))
        # The next tick holds the Work: nothing is charged again.
        res, again = select(after, evidence=evidence)
        self.assertEqual((kinds(res), again["budget"]), ([], after["budget"]))
        # The rejected outcome charges `no_progress` only.
        rejected = dict(publication(after, "rejected_validation"),
                        task={"contribution_id": after["contribution_id"],
                              "revision": after["revision"], "generation": 0,
                              "dedupe_key": after["active_work"]["dedupe_key"]})
        folded, _staged = reconcile.apply_publication(after, rejected, now=NOW, policy=POLICY)
        self.assertEqual(folded["budget"], dict(after["budget"], no_progress_streak=1))

    def test_a_review_only_repair_charges_no_cluster(self):
        record = contribution(activity="fix_needed", last_ci_evidence_id="review-1",
                              pending_feedback_evidence_ids=["review-1"])
        review = {"evidence_id": "review-1", "kind": "human_relay"}
        res, after = select(record, evidence={"review-1": review})
        self.assertEqual(kinds(res), ["request_patch"])
        self.assertEqual(after["active_work"]["task"], "development_repair")
        self.assertEqual(after["budget"]["clusters"], {})

    def test_the_first_patch_after_a_pause_is_a_patch(self):
        record = requested(contribution(plan_evidence_id="plan-1"), "development_patch")
        paused = apply_command(record, commands.parse("/upstreaming pause")).record
        resumed = apply_command(paused, commands.parse(
            f"/upstreaming resume revision={paused['revision']} reason=go")).record
        res, after = select(resumed)
        self.assertEqual(kinds(res), ["request_patch"])
        self.assertEqual(after["active_work"]["task"], "development_patch")
        self.assertEqual(after["active_work"]["reason_ids"], ["plan-1"])

    def test_feedback_ids_are_carried_exactly(self):
        record = contribution(activity="fix_needed", pending_feedback_evidence_ids=["r-1", "r-2"])
        res, after = select(record)
        self.assertEqual(after["active_work"]["feedback_evidence_ids"], ["r-1", "r-2"])
        self.assertEqual(res.requested_effects[0]["feedback_evidence_ids"], ["r-1", "r-2"])
        # Feedback recorded while the Work runs is not carried.
        later = dict(after, pending_feedback_evidence_ids=["r-1", "r-2", "r-3"])
        request = work.request_from_active(later)
        self.assertEqual(request["feedback_evidence_ids"], ["r-1", "r-2"])
        node = work.node(request, later, control_repo_id="1", allowed_paths=["a"])
        self.assertEqual((node["payload"]["feedback_evidence_ids"], node["payload"]["generation"],
                          node["payload"]["dedupe_key"]),
                         (["r-1", "r-2"], 0, after["active_work"]["dedupe_key"]))
        # A fresh graph for the same Work carries the same ids.
        out, event, _decision = reconcile._terminal_work(later, later["active_work"], "cancelled",
                                                         now=T, policy=POLICY)
        self.assertEqual(out["active_work"]["feedback_evidence_ids"], ["r-1", "r-2"])
        self.assertEqual(out["pending_feedback_evidence_ids"], ["r-1", "r-2", "r-3"])
        # A pause clears the Work and keeps the pending feedback.
        paused = apply_command(later, commands.parse("/upstreaming pause")).record
        self.assertIsNone(paused["active_work"])
        self.assertEqual(paused["pending_feedback_evidence_ids"], ["r-1", "r-2", "r-3"])

    def test_work_of_another_generation_is_not_dispatched(self):
        record = requested(contribution(generation=1, revision=3))
        self.assertIsNotNone(work.request_from_active(record))
        stale = copy.deepcopy(record)
        stale["active_work"]["generation"] = 0
        self.assertIsNone(work.request_from_active(stale))


class TestExhaustion(unittest.TestCase):
    """Each exhausted budget leads to the wait that names it, and the resume
    starts exactly that counter again."""

    def spent(self, record, **counts):
        budget = dict(record["budget"])
        for name, value in counts.items():
            budget[name] = value
        return dict(record, budget=budget)

    def resume(self, record):
        return apply_command(record, commands.parse(
            f"/upstreaming resume revision={record['revision']} reason=go"))

    def test_work_total_waits_for_a_human_and_resume_restarts_it(self):
        limit = lc.budget_limit("work_total", POLICY)
        for record in (contribution(phase="planning", activity="idle", fork=None),
                       contribution(phase="queued", activity="idle", fork=None),
                       contribution(activity="fix_needed")):
            with self.subTest(state=engine.state_key(record)):
                record = self.spent(record, attempts_used=limit, no_progress_streak=1,
                                    planning_attempts=1)
                res, waiting = select(record)
                if engine.state_key(record) == "queued.idle":
                    # Planning begins first, in a commit of its own.
                    self.assertEqual(kinds(res), ["begin_planning"])
                    res, waiting = select(waiting)
                self.assertEqual(res.requested_effects, [])
                self.assertEqual(kinds(res)[-1], "exhaust_work_total")
                self.assertEqual(waiting["activity"], "waiting_human")
                self.assertEqual((waiting["pending_human_action"]["kind"],
                                  waiting["pending_human_action"]["counter"]),
                                 ("work_budget_exhausted", "work_total"))
                details = [e for k, e in res.staged if k == "events"][-1]["details"]
                self.assertEqual((details["count"], details["limit"]), (limit, limit))
                out = self.resume(waiting)
                self.assertTrue(out.accepted, out.reason)
                self.assertEqual(out.event["details"]["restarted"],
                                 {"counter": "work_total", "count": limit})
                self.assertEqual(out.record["budget"],
                                 dict(waiting["budget"], attempts_used=0))

    def test_terminal_work_recovery_waits_naming_its_counter(self):
        record = requested(contribution(revision=2))
        attempts = []
        for _ in range(lc.budget_limit("work_recovery", POLICY)):
            new, event, decision = reconcile._terminal_work(record, record["active_work"],
                                                            "completed", now=T, policy=POLICY)
            attempts.append(event["kind"])
            record = new
        self.assertEqual(attempts, ["allocate_work_retry", "allocate_work_retry",
                                    "exhaust_work_recovery"])
        self.assertEqual((record["pending_human_action"]["kind"],
                          record["pending_human_action"]["counter"]), ("work_failed", "work_recovery"))
        self.assertEqual(record["budget"]["attempts_used"], 2)
        self.assertEqual(record["revision"], 4)
        # A fresh graph each time: generation and revision in the Work id.
        out = self.resume(record)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(engine.state_key(out.record), "development.fix_needed")
        # With `work_total` exhausted the wait names `work_total`.
        spent = self.spent(requested(contribution(revision=2)),
                           attempts_used=lc.budget_limit("work_total", POLICY))
        new, _event, _decision = reconcile._terminal_work(spent, spent["active_work"], "cancelled",
                                                          now=T, policy=POLICY)
        self.assertEqual(new["pending_human_action"]["counter"], "work_total")

    def test_exhausted_recovery_names_work_total_when_both_are_spent(self):
        total = lc.budget_limit("work_total", POLICY)
        recovery = lc.budget_limit("work_recovery", POLICY)
        cases = (("both", total, recovery, "work_total", ["work_total", "work_recovery"]),
                 ("work_recovery", 1, recovery, "work_recovery", ["work_recovery"]),
                 ("work_total", total, 1, "work_total", ["work_total"]))
        for label, used, attempt, counter, exhausted in cases:
            with self.subTest(spent=label):
                record = self.spent(requested(contribution(revision=2)), attempts_used=used)
                record["active_work"]["attempt"] = attempt
                new, event, _decision = controller.terminal_work(
                    record, record["active_work"], "completed", now=T, policy=POLICY)
                self.assertEqual(event["kind"], "exhaust_work_recovery")
                self.assertEqual(new["pending_human_action"]["counter"], counter)
                self.assertEqual(sorted(event["details"]["exhausted"]), sorted(exhausted))
        # One resume from the wait with both spent makes Work possible again.
        record = self.spent(requested(contribution(revision=2)), attempts_used=total)
        record["active_work"]["attempt"] = recovery
        waiting, _event, _decision = controller.terminal_work(
            record, record["active_work"], "completed", now=T, policy=POLICY)
        out = self.resume(waiting)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.event["details"]["restarted"]["counter"], "work_total")
        self.assertEqual(out.record["budget"]["attempts_used"], 0)
        res, allocated = select(out.record)
        self.assertEqual(kinds(res), ["request_patch"])
        self.assertEqual(allocated["active_work"]["attempt"], 1)
        _new, event, _decision = controller.terminal_work(
            allocated, allocated["active_work"], "completed", now=T, policy=POLICY)
        self.assertEqual(event["kind"], "allocate_work_retry")

    def test_terminal_work_retry_is_refused_without_the_bound_identity(self):
        # A record holding Work always has the identity bound (see
        # `controller.terminal_work`); were it unbound, the controller
        # neither allocates a fresh graph nor exhausts the recovery.
        record = requested(contribution(revision=2))
        for missing in ("repository_id", "base_ref", "base_sha"):
            with self.subTest(missing=missing):
                unbound = dict(record, upstream=dict(record["upstream"], **{missing: None}))
                out = engine.apply_transition(
                    unbound, "allocate_work_retry", policy=POLICY, now=T.isoformat(),
                    facts={"work_terminal_without_result"},
                    values={"active_work": dict(unbound["active_work"])})
                self.assertFalse(out.accepted)
                self.assertIn("upstream identity and base not bound", out.reason)
                new, event, decision = controller.terminal_work(
                    unbound, unbound["active_work"], "completed", now=T, policy=POLICY)
                self.assertEqual((new, event), (None, None))
                self.assertTrue(decision.startswith("terminal Work: "), decision)

    def test_terminal_work_retry_is_a_new_graph(self):
        record = requested(contribution(generation=2, revision=7))
        new, event, _decision = reconcile._terminal_work(record, record["active_work"], "cancelled",
                                                         now=T, policy=POLICY)
        self.assertEqual(new["active_work"]["dedupe_key"],
                         "example--proj/12@g2.r8:development_repair")
        self.assertEqual((new["active_work"]["attempt"], new["active_work"]["generation"]), (2, 2))
        self.assertNotEqual(work.graph_id(new["active_work"]["dedupe_key"]),
                            work.graph_id(record["active_work"]["dedupe_key"]))
        relaunch, staged, report = reconcile.dispatch_recovery(
            {"c": record}, runs=[], queue_works={work.graph_id(record["active_work"]["dedupe_key"]):
                                                 "completed"}, now=T, policy=POLICY,
            work_results=helpers.result_missing(
                work.graph_id(record["active_work"]["dedupe_key"])))
        self.assertEqual([e["kind"] for k, e in staged if k == "events"], ["allocate_work_retry"])
        self.assertEqual(len(relaunch), 1)

    def test_planning_without_a_usable_plan(self):
        record = requested(contribution(phase="planning", fork=None), "planning")
        limit = lc.budget_limit("planning_attempts", POLICY)
        for decision in (None, "maybe"):
            with self.subTest(decision=decision):
                name = "planning_without_plan" if decision is None else "planning_develop"
                pub = result(record, name)
                if decision:
                    pub["plan"] = {"proposal": {"decision": decision}}
                new, staged = reconcile.apply_publication(record, pub, now=NOW, policy=POLICY,
                                                          worker_run_id="555")
                self.assertEqual([e["kind"] for k, e in staged if k == "events"], ["reject_plan"])
                self.assertEqual(engine.state_key(new), "planning.idle")
                self.assertEqual(new["budget"]["planning_attempts"], 1)
                self.assertEqual(new["revision"], record["revision"] + 1)
                exhausted = self.spent(record, planning_attempts=limit)
                new, staged = reconcile.apply_publication(exhausted, pub, now=NOW, policy=POLICY,
                                                          worker_run_id="555")
                self.assertEqual([e["kind"] for k, e in staged if k == "events"],
                                 ["exhaust_planning_recovery"])
                self.assertEqual((new["pending_human_action"]["kind"],
                                  new["pending_human_action"]["counter"]),
                                 ("planning_failed", "planning_attempts"))
                out = self.resume(new)
                self.assertEqual(out.record["budget"],
                                 dict(new["budget"], planning_attempts=0))

    def test_repair_budgets_refresh_once_then_park(self):
        evidence = {"ci-9": ci_record("ci-9", SHA_A, failing="build riscv64")}
        limit = lc.budget_limit("repair_attempts_cluster", POLICY)
        record = contribution(activity="fix_needed", last_ci_evidence_id="ci-9")
        record["budget"]["clusters"] = {"build riscv64": limit, "other job": 1}
        res, refreshed = select(record, evidence=evidence)
        self.assertEqual(kinds(res), ["refresh_on_exhaustion"])
        self.assertEqual(engine.state_key(refreshed), "queued.idle")
        self.assertEqual((refreshed["budget"]["clusters"], refreshed["automatic_refresh_used"]),
                         ({}, True))
        self.assertEqual(refreshed["refresh"]["head_sha"], SHA_A)
        res, parked = select(dict(record, automatic_refresh_used=True), evidence=evidence)
        self.assertEqual(kinds(res), ["exhaust_repair_attempts"])
        self.assertEqual(engine.state_key(parked), "parked.idle")
        self.assertIn("repair_attempts_cluster", parked["parked_reason"])
        # Another failing job has its own count: Work is requested.
        other = dict(record, last_ci_evidence_id="ci-10")
        res, after = select(other, evidence={"ci-10": ci_record("ci-10", SHA_A, failing="other job")})
        self.assertEqual(kinds(res), ["request_repair"])
        self.assertEqual(after["budget"]["clusters"]["other job"], 2)
        # Evidence missing from the store is an error of the contribution.
        with self.assertRaises(isolation.ProcessingError) as raised:
            select(record)
        self.assertIsInstance(raised.exception.error, controller.MissingEvidence)

    def test_upstream_revision_exhaustion_names_the_counter(self):
        record = contribution(phase="upstream_review", activity="fix_needed",
                              upstream_pr=copy.deepcopy(UPSTREAM_PR),
                              pending_feedback_evidence_ids=["review-1"])
        record["budget"]["no_progress_streak"] = lc.budget_limit("no_progress", POLICY)
        res, waiting = select(record)
        self.assertEqual(kinds(res), ["exhaust_repair_attempts"])
        self.assertEqual((waiting["pending_human_action"]["kind"],
                          waiting["pending_human_action"]["counter"]),
                         ("repair_budget_exhausted", "no_progress"))
        out = self.resume(waiting)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.record["budget"], dict(waiting["budget"], no_progress_streak=0))
        fresh = dict(record, budget=dict(record["budget"], no_progress_streak=0))
        res, after = select(fresh)
        self.assertEqual(kinds(res), ["request_review_revision"])
        self.assertEqual(after["budget"]["attempts_used"], 1)

    def test_a_published_patch_starts_no_progress_again(self):
        record = requested(contribution(revision=3))
        record["budget"]["no_progress_streak"] = 1
        new, _staged = reconcile.apply_publication(record, result(record, "published"), now=NOW,
                                                   policy=POLICY)
        self.assertEqual(new["budget"]["no_progress_streak"], 0)

    def test_every_published_row_starts_the_streaks_again_as_a_row_effect(self):
        for source, verb in (("development.agent_running", "record_publish_success"),
                            ("development.publishing", "record_publish_success"),
                            ("upstream_review.agent_running", "record_revision_publish_success")):
            with self.subTest(state=source, verb=verb):
                edge = lc.find_edge(source, verb)
                rec, kwargs = scenario(source, verb, edge.transition)
                rec["budget"] = dict(rec["budget"], no_progress_streak=2, review_rejections=1)
                out = engine.apply_transition(rec, verb, **kwargs)
                self.assertTrue(out.accepted, out.reason)
                self.assertEqual((out.record["budget"]["no_progress_streak"],
                                  out.record["budget"]["review_rejections"]), (0, 0))
        # The write beside the row keeps to the admitted scope.
        with self.assertRaises(lc.TransitionError):
            engine.record_beside(contribution(), "patch_published", now=NOW,
                                 budget=dict(contribution()["budget"], no_progress_streak=0))


class TestFailingJob(unittest.TestCase):
    def test_the_first_executed_blocking_job_of_the_head(self):
        passing = ci_record("ci-1", SHA_A)
        self.assertIsNone(ci_evidence.failing_job(passing, SHA_A))
        failing = ci_record("ci-1", SHA_A, failing="build riscv64")
        self.assertEqual(ci_evidence.failing_job(failing, SHA_A), "build riscv64")
        self.assertIsNone(ci_evidence.failing_job(failing, SHA_B))
        self.assertIsNone(ci_evidence.failing_job(None, SHA_A))
        self.assertIsNone(ci_evidence.failing_job({"kind": "human_relay"}, SHA_A))
        jobs = failing["ci"]["jobs"]
        two = copy.deepcopy(failing)
        two["ci"]["jobs"] = [
            dict(jobs[0], name="publish wheels", conclusion="failure"),
            dict(jobs[0], name="lint", conclusion="cancelled", executed=False),
            dict(jobs[0], name="test", conclusion="failure"),
            dict(jobs[0], name="build", conclusion="failure")]
        self.assertEqual(ci_evidence.failing_job(two, SHA_A), "test")
        label = copy.deepcopy(passing)
        label["limitations"] = [
            "job 'build riscv64' failed runner-label check 'ubuntu-24.04-riscv': labels []"]
        self.assertEqual(ci_evidence.failing_job(label, SHA_A), "build riscv64")

    def test_a_charge_keeps_the_counts_of_other_jobs(self):
        limit = lc.budget_limit("repair_attempts_cluster", POLICY)
        evidence = {"ci-9": ci_record("ci-9", SHA_A, failing="build riscv64")}
        record = contribution(activity="fix_needed", last_ci_evidence_id="ci-9")
        record["budget"]["clusters"] = {"lint": 2}
        res, after = select(record, evidence=evidence)
        self.assertEqual(kinds(res), ["request_repair"])
        self.assertEqual(after["budget"]["clusters"], {"lint": 2, "build riscv64": 1})
        # Another job at its limit does not stop the repair of this one.
        record["budget"]["clusters"] = {"lint": limit}
        res, after = select(record, evidence=evidence)
        self.assertEqual(kinds(res), ["request_repair"])
        self.assertEqual(after["budget"]["clusters"], {"lint": limit, "build riscv64": 1})
        # This job at its limit takes the exhaustion row.
        record["budget"]["clusters"] = {"lint": 1, "build riscv64": limit}
        res, _after = select(record, evidence=evidence)
        self.assertEqual(kinds(res), ["refresh_on_exhaustion"])

    def test_an_executed_failure_comes_before_a_runner_label_limitation(self):
        failing = ci_record("ci-1", SHA_A, failing="test")
        failing["limitations"] = [
            "job 'build riscv64' failed runner-label check 'ubuntu-24.04-riscv': labels []"]
        self.assertEqual(ci_evidence.failing_job(failing, SHA_A), "test")

    def test_a_job_named_by_a_limitation_is_a_key_only(self):
        name = "build riscv64; rm -rf /"
        label = ci_record("ci-9", SHA_A)
        label["limitations"] = [
            f"job '{name}' failed runner-label check 'ubuntu-24.04-riscv': labels []"]
        record = contribution(activity="fix_needed", last_ci_evidence_id="ci-9")
        res, after = select(record, evidence={"ci-9": label})
        self.assertEqual(kinds(res), ["request_repair"])
        self.assertEqual(after["budget"]["clusters"], {name: 1})
        details = [e for k, e in res.staged if k == "events"][0]["details"]
        self.assertEqual(details["cluster"], name)
        # The name is a counter key and an event detail; the Work request
        # carries no job to run.
        self.assertNotIn(name, repr(res.requested_effects))

    def test_the_engine_decides_it_from_the_recorded_evidence(self):
        rec = contribution(activity="fix_needed", last_ci_evidence_id="ci-1")
        self.assertEqual(engine.recorded_failing_job(rec, None), (False, None))
        self.assertEqual(engine.recorded_failing_job(rec, ci_record("ci-2", SHA_A, failing="x")),
                         (False, None))
        self.assertEqual(engine.recorded_failing_job(rec, ci_record("ci-1", SHA_A, failing="x")),
                         (True, "x"))
        self.assertEqual(engine.recorded_failing_job(dict(rec, last_ci_evidence_id=None), None),
                         (True, None))
        upstream = dict(rec, upstream_pr=dict(UPSTREAM_PR, head_sha=SHA_B))
        self.assertEqual(engine.recorded_failing_job(upstream, ci_record("ci-1", SHA_A,
                                                                         failing="x")),
                         (True, None))

    def test_failing_evidence_of_a_clean_head_is_evidence_of_its_candidate(self):
        failing = ci_record("ci-1", SHA_C, failing="build riscv64")
        same = ci_evidence.for_clean_head(failing, candidate=SHA_C, ref="riscv64",
                                          dev_ref="riscv64-dev")
        self.assertIs(same, failing)
        before = copy.deepcopy(failing)
        annotated = ci_evidence.for_clean_head(failing, candidate=SHA_A, ref="riscv64",
                                               dev_ref="riscv64-dev")
        self.assertEqual(failing, before)
        self.assertEqual((annotated["ci"]["head_sha"], annotated["ci"]["candidate_head_sha"]),
                         (SHA_C, SHA_A))
        self.assertEqual(annotated["limitations"][0],
                         f"CI ran on the clean head {SHA_C} of `riscv64`: the series of {SHA_A} "
                         "on `riscv64-dev` without its debug commits")
        schemas.validate("evidence", annotated)
        self.assertEqual(ci_evidence.failing_job(annotated, SHA_C), "build riscv64")
        self.assertEqual(ci_evidence.failing_job(annotated, SHA_A), "build riscv64")
        self.assertIsNone(ci_evidence.failing_job(annotated, SHA_B))
        self.assertIsNone(ci_evidence.failing_job(failing, SHA_A))
        rec = contribution(activity="fix_needed", last_ci_evidence_id="ci-1")
        rec["fork"]["head_sha"] = SHA_A
        self.assertEqual(engine.recorded_failing_job(rec, annotated), (True, "build riscv64"))
        rec["fork"]["head_sha"] = SHA_B
        self.assertEqual(engine.recorded_failing_job(rec, annotated), (True, None))


class TestLiveRecord(unittest.TestCase):
    def test_the_submission_gate_allocates_nothing(self):
        from tests.test_live_handoff_migration import migrated

        record = migrated()
        for at in (T, T + timedelta(days=1)):
            res, after = select(record, now=at)
            self.assertEqual((res.requested_effects, after), ([], record))


if __name__ == "__main__":
    unittest.main()
