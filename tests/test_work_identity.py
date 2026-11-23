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
import unittest
from datetime import datetime, timedelta

import capacity
import ci_evidence
import commands
import controller
import engine
import lifecycle as lc
import reconcile
import work
from tests.helpers import NOW, SHA_A, SHA_B, apply_command, ci_record, contribution, policy
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
    work_request = record["active_work"]
    pub["task"] = dict(dict(pub["task"], generation=work_request["generation"],
                            dedupe_key=work_request["dedupe_key"]), **task)
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
                                      dossier_evidence_id="dossier-1"))

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

    def test_a_payload_without_the_identity_is_generation_zero(self):
        """A worker started before the identity carried the generation."""
        old = {"contribution_id": "example--proj/12", "revision": 5}
        self.assertEqual(work.task_identity(old), (0, None))
        self.assertEqual(work.task_identity(dict(old, generation=0)), (None, None))
        self.assertEqual(work.task_identity(dict(old, dedupe_key="k")), (None, None))
        # In generation 0 its Work is recognized by the revision; in-flight
        # Work recorded without a generation is generation 0 too.
        zero = requested(contribution(revision=5))
        del zero["active_work"]["generation"]
        self.assertEqual(work.work_generation(zero["active_work"]), 0)
        pub = dict(publication(zero, "published"), task=dict(old))
        self.assertIsNone(reconcile.result_mismatch(zero, pub))
        output = tick(zero, [pub], capacity_ok=False)
        self.assertEqual(final(output, zero)["activity"], "waiting_ci")
        # It is never folded into a later generation, whatever its revision.
        later = self.record(generation=1, revision=5)
        self.assertEqual(reconcile.result_mismatch(later, dict(pub)),
                         "late Result of generation 0, current is 1")
        output = tick(later, [dict(pub)], capacity_ok=False)
        self.assertEqual([e["kind"] for e in events(output)], ["record_late_effect"])
        self.assertEqual(final(output, later)["active_work"], later["active_work"])

    def test_work_in_flight_keeps_the_payload_it_was_submitted_with(self):
        """A replay of Work requested before the identity existed submits the
        same payload, so the queue sees the same Work."""
        old = requested(contribution(revision=5))
        for name in ("generation", "feedback_evidence_ids"):
            del old["active_work"][name]
        old["active_work"]["dedupe_key"] = "example--proj/12@5:development_repair"
        payload = work.node(work.request_from_active(old), old, control_repo_id="1",
                            allowed_paths=["a"])["payload"]
        for name in ("generation", "dedupe_key", "feedback_evidence_ids"):
            self.assertNotIn(name, payload)
        self.assertEqual(work.task_identity(payload), (0, None))
        new = requested(contribution(revision=5))
        payload = work.node(work.request_from_active(new), new, control_repo_id="1",
                            allowed_paths=["a"])["payload"]
        self.assertEqual(work.task_identity(payload), (0, new["active_work"]["dedupe_key"]))

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
                     ref="riscv64", repository="riseproject-upstreaming/proj")
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
        self.assertEqual(output.report["observation"][0]["note"],
                         "head change is recorded once the requested Work ends")
        # The Result of that Work is then folded at its revision.
        folded = final(tick(record, [result(record, "published")], capacity_ok=False), record)
        self.assertEqual(folded["activity"], "waiting_ci")
        observed = final(tick(folded, observe=True, head=SHA_B), folded)
        self.assertEqual((observed["revision"], observed["fork"]["head_sha"]), (6, SHA_B))


class TestAllocation(unittest.TestCase):
    """Work is allocated by the table's rows; each allocation charges once."""

    def test_investigation_begins_and_is_dispatched_in_one_tick(self):
        record = contribution(phase="queued", activity="idle", fork=None)
        res, after = select(record)
        self.assertEqual(kinds(res), ["begin_investigation", "dispatch_investigation"])
        self.assertEqual(engine.state_key(after), "investigation.agent_running")
        self.assertEqual(after["budget"]["attempts_used"], 1)
        self.assertEqual(after["active_work"]["dedupe_key"], "example--proj/12@g0.r0:investigation")
        self.assertEqual(res.requested_effects[0]["generation"], 0)
        # Without a worker slot nothing begins.
        res, waiting = select(record, slots=0)
        self.assertEqual((kinds(res), waiting), ([], record))

    def test_repair_charges_its_failing_job_once_at_allocation(self):
        record = contribution(activity="fix_needed", last_ci_evidence_id="ci-9",
                              dossier_evidence_id="dossier-1")
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
        record = requested(contribution(dossier_evidence_id="dossier-1"), "development_patch")
        paused = apply_command(record, commands.parse("/upstreaming pause")).record
        resumed = apply_command(paused, commands.parse(
            f"/upstreaming resume revision={paused['revision']} reason=go")).record
        res, after = select(resumed)
        self.assertEqual(kinds(res), ["request_patch"])
        self.assertEqual(after["active_work"]["task"], "development_patch")
        self.assertEqual(after["active_work"]["reason_ids"], ["dossier-1"])

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
        for record in (contribution(phase="investigation", activity="idle", fork=None),
                       contribution(phase="queued", activity="idle", fork=None),
                       contribution(activity="fix_needed")):
            with self.subTest(state=engine.state_key(record)):
                record = self.spent(record, attempts_used=limit, no_progress_streak=1,
                                    investigation_attempts=1)
                res, waiting = select(record)
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
                                                 "completed"}, now=T, policy=POLICY)
        self.assertEqual([e["kind"] for k, e in staged if k == "events"], ["allocate_work_retry"])
        self.assertEqual(len(relaunch), 1)

    def test_investigation_without_a_usable_dossier(self):
        record = requested(contribution(phase="investigation", fork=None), "investigation")
        limit = lc.budget_limit("investigation_attempts", POLICY)
        for decision in (None, "maybe"):
            with self.subTest(decision=decision):
                name = "investigation_without_dossier" if decision is None else "investigation_develop"
                pub = result(record, name)
                if decision:
                    pub["dossier"] = {"proposal": {"decision": decision}}
                new, staged = reconcile.apply_publication(record, pub, now=NOW, policy=POLICY,
                                                          worker_run_id="555")
                self.assertEqual([e["kind"] for k, e in staged if k == "events"], ["reject_dossier"])
                self.assertEqual(engine.state_key(new), "investigation.idle")
                self.assertEqual(new["budget"]["investigation_attempts"], 1)
                self.assertEqual(new["revision"], record["revision"] + 1)
                exhausted = self.spent(record, investigation_attempts=limit)
                new, staged = reconcile.apply_publication(exhausted, pub, now=NOW, policy=POLICY,
                                                          worker_run_id="555")
                self.assertEqual([e["kind"] for k, e in staged if k == "events"],
                                 ["exhaust_investigation_recovery"])
                self.assertEqual((new["pending_human_action"]["kind"],
                                  new["pending_human_action"]["counter"]),
                                 ("investigation_failed", "investigation_attempts"))
                out = self.resume(new)
                self.assertEqual(out.record["budget"],
                                 dict(new["budget"], investigation_attempts=0))

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
        # Unknown evidence decides nothing.
        res, same = select(record)
        self.assertEqual((kinds(res), same), ([], record))
        self.assertIn(engine.UNKNOWN_EVIDENCE, res.report["deferred"][0]["why"])

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


class TestLiveRecord(unittest.TestCase):
    def test_the_submission_gate_allocates_nothing(self):
        from tests.test_live_handoff_migration import migrated

        record = migrated()
        for at in (T, T + timedelta(days=1)):
            res, after = select(record, now=at)
            self.assertEqual((res.requested_effects, after), ([], record))


if __name__ == "__main__":
    unittest.main()
