# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Section 21: CI evidence, capacity, budgets, recovery, release, triggers, tick."""

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import capacity
import ci_evidence
import collect
import commands
import controller
import engine
import isolation
import lifecycle
import observe_ci
import recovery
import release
import triggers
from tests.helpers import (
    NOW,
    OWNER,
    SHA_A,
    SHA_B,
    SHA_C,
    STRANGER,
    apply_command,
    ci_record,
    comment,
    contribution,
    policy,
    runner_loss,
)

T = datetime.fromisoformat(NOW)


def run(i, sha=SHA_A, attempt=1, conclusion="success", wf=10, event="pull_request",
        status="completed"):
    return {"id": i, "head_sha": sha, "run_attempt": attempt, "status": status,
            "conclusion": conclusion, "workflow_id": wf, "event": event,
            "path": ".github/workflows/wheels.yml"}


def job(i, name, conclusion="success", status="completed", labels=("ubuntu-24.04-riscv",)):
    return {"id": i, "name": name, "status": status, "conclusion": conclusion,
            "started_at": NOW if conclusion not in ("skipped",) else None, "labels": list(labels)}


class TestCiEvidence(unittest.TestCase):
    def ev(self, runs, jobs, expected=("p-1-cp312-cp312-manylinux_2_39_riscv64.whl",), produced=None,
           required_runner_label=None):
        return ci_evidence.evaluate(runs=runs, jobs_by_run=jobs, head_sha=SHA_A,
                                    required_job_patterns=["riscv64"], expected_wheels=list(expected),
                                    produced_wheels=list(expected if produced is None else produced),
                                    required_runner_label=required_runner_label)

    def test_passed(self):
        self.assertEqual(self.ev([run(1)], {1: [job(1, "wheels riscv64")]}).state, "passed")

    def test_skipped_neutral_cancelled_not_success(self):
        for c in ("skipped", "neutral", "cancelled"):
            with self.subTest(c):
                v = self.ev([run(1)], {1: [job(1, "wheels riscv64", conclusion=c)]})
                self.assertEqual(v.state, "not_executed")

    def test_missing_job_and_wheel(self):
        self.assertEqual(self.ev([run(1)], {1: [job(1, "lint")]}).state, "missing")
        self.assertEqual(self.ev([run(1)], {1: [job(1, "wheels riscv64")]}, produced=[]).state, "missing")

    def test_other_head_and_approval(self):
        self.assertEqual(self.ev([run(1, sha=SHA_B)], {}).state, "missing")
        self.assertEqual(self.ev([run(1, conclusion="action_required")], {}).state, "access_wait")

    def test_any_executed_test_failure_blocks_green_but_publish_failure_does_not(self):
        jobs = {1: [job(1, "wheels riscv64"), job(2, "tests (3.12)", conclusion="failure")]}
        verdict = self.ev([run(1)], jobs)
        self.assertEqual(verdict.state, "failed")
        self.assertIn("tests (3.12)", " ".join(verdict.problems))
        publish_only = {1: [job(1, "wheels riscv64"),
                            job(2, "validate-release-request", conclusion="failure")]}
        self.assertEqual(self.ev([run(1)], publish_only).state, "passed")

    def test_rerun_uses_latest_attempt(self):
        runs = [run(1, attempt=1), run(1, attempt=2)]
        v = self.ev(runs, {1: [job(1, "wheels riscv64")]})
        self.assertEqual(v.state, "passed")
        self.assertEqual(len(ci_evidence.latest_attempts(runs)), 1)
        self.assertEqual(ci_evidence.latest_attempts(runs)[0]["run_attempt"], 2)

    def test_duplicate_workflow_registrations_remain_independently_relevant(self):
        runs = [run(45007971640, wf=426522356),
                run(45008094063, wf=426522356, status="in_progress", conclusion=None)]
        jobs = {
            45007971640: [job(1, "Build - ubuntu-24.04-riscv (riscv64)")],
            45008094063: [job(2, "Build - ubuntu-24.04-riscv (riscv64)",
                              status="in_progress", conclusion=None)],
        }
        self.assertEqual(len(ci_evidence.latest_attempts(runs)), 2)
        self.assertEqual(self.ev(runs, jobs).state, "pending")

    def test_incomplete_exact_head_waits_even_after_an_executed_failure(self):
        runs = [run(1, conclusion="failure"), run(2, status="in_progress", conclusion=None, wf=11)]
        jobs = {1: [job(1, "tests", conclusion="failure"),
                    job(2, "wheels riscv64")],
                2: []}
        self.assertEqual(self.ev(runs, jobs).state, "pending")
        runs[1] = run(2, wf=11)
        self.assertEqual(self.ev(runs, jobs).state, "failed")

    def test_absent_required_job_is_missing_evidence_not_failure(self):
        verdict = self.ev([run(1)], {1: [job(1, "lint")]})
        self.assertEqual(verdict.state, "missing")

    def test_pending_and_label(self):
        self.assertEqual(self.ev([run(1)], {1: [job(1, "wheels riscv64", status="in_progress",
                                                     conclusion=None)]}).state, "pending")
        qemu = self.ev([run(1)], {1: [job(1, "wheels riscv64", labels=("ubuntu-latest",))]})
        self.assertEqual(qemu.state, "passed")
        native_contract = self.ev([run(1)], {1: [job(1, "wheels riscv64", labels=("riscv-custom",))]},
                                  required_runner_label="ubuntu-24.04-riscv")
        self.assertEqual(native_contract.state, "failed")


USAGE = Path.home() / "notes/ecosystem-wg/python/python-wheels-upstreaming/research/2026-11-17-reassessment/runner-usage.json"


class TestExactHeadCiReconciliation(unittest.TestCase):
    HEAD = "b29a8f49374004d650cf312e9c31848953e5ae60"

    @staticmethod
    def snapshot(*, pending):
        runs = [
            run(45008094068, sha=TestExactHeadCiReconciliation.HEAD, wf=426522359),
            run(45007971640, sha=TestExactHeadCiReconciliation.HEAD, wf=426522356,
                status="in_progress" if pending else "completed",
                conclusion=None if pending else "success"),
            run(45008094063, sha=TestExactHeadCiReconciliation.HEAD, wf=426522356,
                status="in_progress" if pending else "completed",
                conclusion=None if pending else "success"),
        ]
        jobs = {
            45008094068: [job(137164080131, "clang-format")],
            45007971640: [job(137164226915, "Build - ubuntu-24.04-riscv (riscv64)",
                                status="in_progress" if pending else "completed",
                                conclusion=None if pending else "success")],
            45008094063: [job(137164226916, "Build - ubuntu-24.04-riscv (riscv64)",
                                status="in_progress" if pending else "completed",
                                conclusion=None if pending else "success")],
        }
        return collect.CiSnapshot(
            repository={"id": 1467208233, "full_name": "riseproject-upstreaming/ciso8601"},
            head_sha=TestExactHeadCiReconciliation.HEAD,
            runs=runs,
            jobs_by_run=jobs,
            artifacts_by_run={},
        )

    def record(self, activity="waiting_ci"):
        rec = contribution(phase="development", activity=activity, revision=5,
                           last_ci_evidence_id=None)
        rec["fork"] = dict(rec["fork"], repository_id=1467208233,
                           name="riseproject-upstreaming/ciso8601", head_sha=self.HEAD,
                           pr_number=1)
        rec["proposal_artifact"] = {
            "work_id": "work", "claim_id": "claim", "artifact_id": 14748243901,
            "name": "porting-out", "digest": "sha256:" + "0" * 64,
            "run_id": "45006566517", "result_commit": "result", "outcome": "published",
        }
        rec["plan_evidence_id"] = "plan-closeio--ciso8601--1-r2"
        rec["pending_feedback_evidence_ids"] = []
        return rec

    def observe(self, rec, snapshot, evidence=None, contracts=None):
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": self.HEAD}), \
             mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
             mock.patch("observe_ci.collect.ci_snapshot", return_value=snapshot), \
             mock.patch("observe_ci.collect.classify_runner_loss", side_effect=runner_loss()):
            return observe_ci.observe(object(), contributions={"c": rec}, operations={},
                                      contracts=contracts or {}, policy=policy(), now=NOW,
                                      evidence=evidence or {})

    @staticmethod
    def final(observation, original):
        records = [item for kind, item in observation.staged if kind == "contributions"]
        return records[-1] if records else original

    def test_run_45008094063_partial_snapshot_waits_without_requesting_repair(self):
        rec = self.record()
        observation = self.observe(rec, self.snapshot(pending=True))
        final = self.final(observation, rec)
        self.assertEqual(final["activity"], "waiting_ci")
        self.assertIsNotNone(final["next_poll_at"])
        self.assertIsNone(final["last_ci_evidence_id"])
        selected = controller.tick(
            contributions={"c": final}, operations={}, consumed={}, projects={}, policy=policy(),
            inputs=controller.Inputs(now=T, pressure=capacity.Pressure(ok=True)))
        self.assertEqual(selected.requested_effects, [])

    OBSOLETE_REASONS = {
        "rehearsal-closeio--ciso8601--1-r3-44982902579": {
            "kind": "rehearsal", "evidence_id": "rehearsal-closeio--ciso8601--1-r3-44982902579"},
        "plan-closeio--ciso8601--1-r2": {
            "kind": "plan", "evidence_id": "plan-closeio--ciso8601--1-r2"},
    }

    def undispatched_repair(self):
        rec = self.record(activity="agent_running")
        rec["active_work"] = {
            "dedupe_key": "closeio--ciso8601/1@5:development_repair",
            "task": "development_repair", "revision": 5, "attempt": 1,
            "requested_at": NOW, "deadline_at": "2026-11-18T18:00:00+00:00",
            "dispatched_run_id": None,
            "reason_ids": ["rehearsal-closeio--ciso8601--1-r3-44982902579",
                           "plan-closeio--ciso8601--1-r2"], "generation": 0, "feedback_evidence_ids": []
        }
        return rec

    def test_requested_work_is_not_observed_or_withdrawn(self):
        """`development.agent_running` has no observation row: green CI on
        the published head neither withdraws the requested Work nor
        nominates; the Work's Result is folded by the table's rows."""
        for pending in (False, True):
            with self.subTest(pending=pending):
                rec = self.undispatched_repair()
                observation = self.observe(rec, self.snapshot(pending=pending),
                                           evidence=self.OBSOLETE_REASONS)
                self.assertEqual(observation.staged, [])
                self.assertEqual(observation.report[0]["note"],
                                 "development.agent_running has no observation row")
                self.assertNotIn("obsolete_repair_withdrawn", observation.report[0])

    def test_a_moved_head_is_not_recorded_while_work_is_requested(self):
        """No state that requests Work or holds a Result for publication has
        a head row: a moved fork head is recorded from
        `development.fix_needed` once the stale Result is replanned."""
        held = self.undispatched_repair()
        held["active_work"] = None
        held["pending_publication"] = {"worker_run_id": "5", "attempts": 1, "last_reason": "x",
                                       "next_attempt_at": NOW, "exhausted": False}
        moved = "c" * 40
        for label, rec in (("requested Work", self.undispatched_repair()),
                           ("held publication", held)):
            with self.subTest(case=label):
                self.assertNotEqual(rec["fork"]["head_sha"], moved)
                with mock.patch("observe_ci.collect.branch_head", return_value={"head": moved}), \
                     mock.patch("observe_ci.collect.development_review_feedback",
                                return_value=None), \
                     mock.patch("observe_ci.collect.ci_snapshot",
                                return_value=self.snapshot(pending=False)), \
                     mock.patch("observe_ci.collect.classify_runner_loss",
                                side_effect=runner_loss()):
                    observation = observe_ci.observe(
                        object(), contributions={"c": rec}, operations={}, contracts={},
                        policy=policy(), now=NOW, evidence=self.OBSOLETE_REASONS)
                self.assertEqual([item for kind, item in observation.staged
                                  if kind in ("contributions", "events")], [])
                self.assertEqual(observation.report[0]["note"],
                                 "development.agent_running has no observation row")
                self.assertEqual(self.final(observation, rec)["fork"]["head_sha"],
                                 rec["fork"]["head_sha"])

    def test_pending_feedback_with_green_ci_returns_to_due_work(self):
        """Nomination requires that no actionable feedback is pending; with
        feedback pending, `return_for_feedback` makes Work due again."""
        rec = dict(self.record(), pending_feedback_evidence_ids=["review-1"])
        observation = self.observe(rec, self.snapshot(pending=False))
        final = self.final(observation, rec)
        self.assertEqual(engine.state_key(final), "development.fix_needed")
        self.assertEqual(final["revision"], rec["revision"])
        self.assertEqual(final["last_ci_evidence_id"], observation.report[0]["evidence"])
        self.assertEqual(final["pending_feedback_evidence_ids"], ["review-1"])
        self.assertEqual(observation.report[0]["feedback_due"], ["review-1"])
        self.assertNotIn("handoff_deferred", observation.report[0])
        self.assertEqual([e["kind"] for kind, e in observation.staged if kind == "events"],
                         ["return_for_feedback"])

    def test_new_head_in_handoff_clears_readiness_while_ci_pending(self):
        """A new head leaves the gate by the table's record_head_change."""
        rec = self.record()
        handoff = self.final(self.observe(rec, self.snapshot(pending=False)), rec)
        self.assertEqual((handoff["phase"], handoff["activity"]), ("handoff", "awaiting_submission"))
        new_head = "c" * 40
        snapshot = self.snapshot(pending=True)
        snapshot.head_sha = new_head
        for r in snapshot.runs:
            r["head_sha"] = new_head
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": new_head}), \
             mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
             mock.patch("observe_ci.collect.ci_snapshot", return_value=snapshot):
            observation = observe_ci.observe(object(), contributions={"c": handoff}, operations={},
                                             contracts={}, policy=policy(), now=NOW, evidence={})
        final = self.final(observation, handoff)
        self.assertTrue(observation.report[0].get("head_changed"))
        self.assertEqual(final["fork"]["head_sha"], new_head)
        self.assertEqual((final["phase"], final["activity"]), ("development", "waiting_ci"))
        self.assertEqual(final["revision"], handoff["revision"] + 1)
        self.assertIsNone(final["last_ci_evidence_id"])
        self.assertIsNone(final["handoff_request"])
        self.assertIsNone(final["pending_human_action"])
        events = [event for kind, event in observation.staged if kind == "events"]
        self.assertEqual([event["kind"] for event in events], ["record_head_change"])
        self.assertTrue(events[0]["revision_bumped"])
        self.assertNotEqual(final["activity"], "waiting_human")

    def test_live_green_pr_ci_without_uploaded_artifacts_enters_handoff(self):
        """Exact live shape: revision 5, no target contract, PR CI that never uploads."""
        rec = self.record()
        rec["next_poll_at"] = "2026-11-18T12:01:00+00:00"
        self.assertIsNone(rec["target_contract_id"])
        snapshot = self.snapshot(pending=False)
        self.assertEqual(snapshot.artifacts_by_run, {})
        observation = self.observe(rec, snapshot)
        final = self.final(observation, rec)
        evidence_id = "ci-1467208233-45008094068-1"
        self.assertEqual((final["phase"], final["activity"]), ("handoff", "awaiting_submission"))
        self.assertEqual(final["revision"], 5)
        self.assertEqual(final["last_ci_evidence_id"], evidence_id)
        # The clean head is set by the debug cleanup, after submission is requested.
        self.assertIsNone(final["clean_head_sha"])
        self.assertEqual(final["fork"]["head_sha"], self.HEAD)
        self.assertEqual(final["handoff_request"],
                         {"command_id": f"observation-{evidence_id}", "head": self.HEAD, "revision": 5})
        self.assertEqual(final["pending_human_action"]["kind"], "submit_upstream")
        self.assertIsNone(final["pending_human_action"]["counter"])
        self.assertIsNone(final["next_poll_at"])
        staged_evidence = [item for kind, item in observation.staged if kind == "evidence"]
        self.assertEqual([item["evidence_id"] for item in staged_evidence], [evidence_id])
        self.assertEqual(staged_evidence[0]["ci"]["head_sha"], self.HEAD)
        self.assertNotIn("no riscv64 wheel artifact was uploaded", staged_evidence[0]["limitations"])
        jobs = {job["name"] for job in staged_evidence[0]["ci"]["jobs"]}
        self.assertIn("Build - ubuntu-24.04-riscv (riscv64)", jobs)
        events = [event for kind, event in observation.staged if kind == "events"]
        self.assertEqual([event["kind"] for event in events], ["nominate_for_handoff"])
        self.assertEqual(events[0]["to"], {"phase": "handoff", "activity": "awaiting_submission"})
        self.assertEqual(events[0]["cause"], {"type": "observation", "id": evidence_id})
        self.assertFalse(events[0]["revision_bumped"])
        self.assertEqual(events[0]["details"]["head"], self.HEAD)
        self.assertEqual(events[0]["details"]["revision"], 5)
        # The ordinary controller requests no Work for the handoff wait.
        selected = controller.tick(
            contributions={"c": final}, operations={}, consumed={}, projects={}, policy=policy(),
            inputs=controller.Inputs(now=T, pressure=capacity.Pressure(ok=True)))
        self.assertEqual(selected.requested_effects, [])
        # Repeated observation of the candidate head keeps the gate,
        # including while a rerun of the same head is pending.
        for pending in (False, True):
            with self.subTest(pending=pending):
                again = self.observe(final, self.snapshot(pending=pending))
                repeated = self.final(again, final)
                self.assertEqual((repeated["phase"], repeated["activity"]),
                                 ("handoff", "awaiting_submission"))
                self.assertEqual(repeated["handoff_request"], final["handoff_request"])
                self.assertEqual(repeated["pending_human_action"], final["pending_human_action"])
                self.assertEqual([kind for kind, event in again.staged if kind == "events"], [])

    def test_live_tick_commits_handoff_and_posts_app_authored_ledger_entry(self):
        """reconcile.tick -> observe_ci -> durable state -> status_ledger.publish."""
        import tempfile

        import reconcile
        import state
        import status_ledger
        from tests.test_status_ledger import FakeApi

        rec = self.record()
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": self.HEAD}), \
             mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
             mock.patch("observe_ci.collect.ci_snapshot", return_value=self.snapshot(pending=False)):
            output = reconcile.tick(
                object(), reconcile.Snapshot({"c": rec}, {}, {}, {}, {}), policy=policy(), now=T,
                publications=[], pressure=capacity.Pressure(ok=True))
        self.assertTrue(output.report["observation"][0]["handoff_ready"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = state.StateStore(root, policy())
            for kind, item in output.staged:
                store.stage(kind, item)
            written = store.write_staged()
            events = [path for path in written if path.startswith(".state/events/")]
            self.assertEqual(len(events), 1)
            committed = json.loads(next(path for path in (root / ".state/contributions").glob("*.json")).read_text())
            self.assertEqual((committed["phase"], committed["activity"]),
                             ("handoff", "awaiting_submission"))
            self.assertEqual(committed["last_ci_evidence_id"], "ci-1467208233-45008094068-1")
            api = FakeApi()
            with mock.patch("status_ledger.Api", return_value=api):
                receipts = status_ledger.publish(root, {"written": written}, "token",
                                                 "https://github.com/run")
        self.assertEqual([receipt["status"] for receipt in receipts], ["created"])
        body = api.comment["body"]
        self.assertTrue(body.startswith("**Upstreaming r5 · handoff/awaiting_submission**"))
        self.assertNotIn("approve-handoff", body)
        self.assertEqual(api.comment["user"]["login"], "riseproject-dev[bot]")

    def test_terminal_executed_failure_records_reason_and_requests_repair(self):
        rec = self.record()
        snapshot = self.snapshot(pending=False)
        snapshot.jobs_by_run[45008094063][0] = job(
            137164226916, "Build - ubuntu-24.04-riscv (riscv64)", conclusion="failure")
        observation = self.observe(rec, snapshot)
        final = self.final(observation, rec)
        self.assertEqual(final["activity"], "fix_needed")
        self.assertIsNotNone(final["last_ci_evidence_id"])
        evidence = {e["evidence_id"]: e for kind, e in observation.staged if kind == "evidence"}
        selected = controller.tick(
            contributions={"c": final}, operations={}, consumed={}, projects={}, policy=policy(),
            inputs=controller.Inputs(now=T, pressure=capacity.Pressure(ok=True)),
            evidence=evidence)
        self.assertEqual(selected.requested_effects[0]["task"], "development_repair")
        self.assertIn(final["last_ci_evidence_id"], selected.requested_effects[0]["reason_ids"])
        # The failing job of the head is the cluster the repair charges.
        claimed = [r for kind, r in selected.staged if kind == "contributions"][-1]
        self.assertEqual(claimed["budget"]["clusters"],
                         {"Build - ubuntu-24.04-riscv (riscv64)": 1})
        # Evidence the record names but the store does not hold is an
        # unexpected error of the contribution, never a silent deferral.
        with self.assertRaises(isolation.ProcessingError) as raised:
            controller.tick(
                contributions={"c": final}, operations={}, consumed={}, projects={},
                policy=policy(), inputs=controller.Inputs(now=T, pressure=capacity.Pressure(ok=True)))
        self.assertIsInstance(raised.exception.error, controller.MissingEvidence)


class TestCapacityBudgets(unittest.TestCase):
    def test_stale_and_malformed(self):
        p = policy()
        self.assertFalse(capacity.observe({}, fetched_at=T, now=T, max_age_seconds=900).ok)
        old = capacity.observe({"jobs": []}, fetched_at=T - timedelta(hours=1), now=T, max_age_seconds=900)
        self.assertFalse(old.ok)
        ok, why = capacity.admit_expensive(old, reserved=0, estimated_jobs=1, policy=p)
        self.assertFalse(ok)
        self.assertIn("stale", why)

    def test_thresholds_and_reservations(self):
        p = policy()
        snap = {"jobs": [{"job_labels": ["ubuntu-24.04-riscv"], "status": "pending",
                          "entity_name": "riseproject-dev", "job_created_at": NOW}] * 90}
        pr = capacity.observe(snap, fetched_at=T, now=T, max_age_seconds=900)
        self.assertTrue(capacity.admit_expensive(pr, reserved=0, estimated_jobs=5, policy=p)[0])
        ops = [{"status": "applying", "capacity_reservation": {"estimated_jobs": 8, "observed_run_ids": []}},
               {"status": "confirmed", "capacity_reservation": {"estimated_jobs": 50, "observed_run_ids": [1]}},
               {"status": "not_launched", "capacity_reservation": {"estimated_jobs": 50}}]
        self.assertEqual(capacity.reserved_jobs(ops), 8)
        self.assertFalse(capacity.admit_expensive(pr, reserved=8, estimated_jobs=5, policy=p)[0])

    @unittest.skipUnless(USAGE.exists(), "historical usage snapshot not available")
    def test_historical_snapshot(self):
        raw = json.loads(USAGE.read_text())
        t = datetime.fromisoformat(raw["fetched_at"])
        pr = capacity.observe(raw["response"], fetched_at=t, now=t, max_age_seconds=900)
        self.assertEqual(pr.per_entity["riseproject-dev"], {"running": 22, "pending": 113})
        self.assertFalse(capacity.admit_expensive(pr, reserved=0, estimated_jobs=1, policy=policy())[0])


class TestRecovery(unittest.TestCase):
    def op(self, kind="branch_update"):
        return recovery.plan(contribution_id="c/1", revision=3, kind=kind, policy_version="p",
                             reason_ids=["fail-1"], target={"repository": "riseproject-upstreaming/proj",
                                                            "ref": "riscv64", "expected_head": SHA_A},
                             proposed={"commit": SHA_B, "tree": "1" * 40, "parents": [SHA_A]}, now=NOW)

    def test_deterministic_id(self):
        self.assertEqual(self.op()["operation_id"], self.op()["operation_id"])

    def test_lost_receipt_discovered_not_repeated(self):
        op = recovery.mark_applying(self.op(), NOW, facts=recovery.RECEIPT_FACTS)
        op = recovery.mark_result(op, response_ok=None, external_ids=None, now=NOW)
        self.assertEqual(op["status"], "uncertain")
        with self.assertRaises(recovery.RecoveryError):
            recovery.mark_applying(op, NOW, facts=recovery.RECEIPT_FACTS)  # no second effect
        r = recovery.discover_branch(op, {"head": SHA_B})
        self.assertEqual(r, "confirmed")
        r2 = recovery.discover_branch(op, {"head": "9" * 40, "tree": "1" * 40, "parents": [SHA_A]})
        self.assertEqual(r2, "confirmed")  # signed replay changed the SHA
        self.assertEqual(recovery.discover_branch(op, {"head": "8" * 40, "tree": "7" * 40}), "unrelated")
        op = recovery.record_discovery(op, r, "found", NOW)
        self.assertEqual(op["status"], "confirmed")

    def test_absence_is_not_nonlaunch(self):
        op = recovery.mark_result(recovery.mark_applying(self.op("ci_dispatch"), NOW, facts=recovery.RECEIPT_FACTS),
                                  response_ok=None, external_ids=None, now=NOW)
        for _ in range(6):
            op = recovery.record_discovery(op, "absent", "none", NOW)
        self.assertEqual(op["status"], "uncertain")
        self.assertFalse(recovery.releases_capacity(op))
        later = T + timedelta(hours=25)
        self.assertIn("owner_review_required", recovery.alerts(op, later))

    def test_schedule(self):
        op = recovery.mark_result(recovery.mark_applying(self.op(), NOW, facts=recovery.RECEIPT_FACTS), response_ok=None,
                                  external_ids=None, now=NOW)
        p = policy()
        self.assertEqual(recovery.next_discovery_at(op, p), T + timedelta(minutes=1))
        op = recovery.record_discovery(op, "absent", "", NOW)
        self.assertEqual(recovery.next_discovery_at(op, p), T + timedelta(minutes=5))

    def test_resolve_requires_evidence(self):
        op = recovery.mark_result(recovery.mark_applying(self.op("ci_dispatch"), NOW, facts=recovery.RECEIPT_FACTS),
                                  response_ok=None, external_ids=None, now=NOW)
        c = commands.parse(f"/upstreaming resolve-operation operation={op['operation_id']} "
                           'disposition=not-launched reason="the dispatch never ran"')
        with self.assertRaises(recovery.RecoveryError):
            recovery.resolve(op, c, actor=OWNER, command_id="x", evidence=None, now=NOW)
        receipt = {"command_id": "x", "comment_id": 3, "issue_number": 12,
                   "content_digest": "e" * 64, "actor": OWNER}
        ev = controller.resolution_evidence(op, c, receipt, None, policy(), NOW)
        done = recovery.resolve(op, c, actor=OWNER, command_id="x", evidence=ev, now=NOW)
        self.assertEqual(done["status"], "not_launched")
        self.assertTrue(recovery.releases_capacity(done))


def pyfile(name, yanked=False):
    return {"filename": name, "yanked": yanked, "digests": {"sha256": "0" * 64}}


class TestRelease(unittest.TestCase):
    rows = [release.Row("cp312", "cp312", "manylinux", (2, 39)),
            release.Row("cp313", "cp313", "manylinux", (2, 39)),
            release.Row("cp313", "cp313t", "manylinux", (2, 39)),
            release.Row("cp312", "cp312", "musllinux", (1, 2))]

    def test_abi3_compressed_and_free_threaded(self):
        files = [pyfile("p-1.0-cp312-abi3-manylinux_2_39_riscv64.whl"),
                 pyfile("p-1.0-cp313-cp313t-manylinux_2_39_riscv64.whl"),
                 pyfile("p-1.0-cp312-cp312-musllinux_1_2_riscv64.musllinux_1_2_x86_64.whl")]
        cov = release.coverage(files, "1.0", self.rows)
        self.assertEqual(cov.missing, [])
        self.assertTrue(cov.complete)

    def test_floor_yank_and_universal(self):
        files = [pyfile("p-1.0-cp312-cp312-manylinux_2_41_riscv64.whl"),
                 pyfile("p-1.0-cp313-cp313-manylinux_2_39_riscv64.whl", yanked=True),
                 pyfile("p-1.0-py3-none-any.whl")]
        cov = release.coverage(files, "1.0", self.rows)
        self.assertIn("cp312-cp312-manylinux_2_39", cov.missing)   # newer floor does not satisfy
        self.assertIn("cp313-cp313-manylinux_2_39", cov.missing)   # yanked
        self.assertFalse(cov.complete)
        uni = release.coverage(files, "1.0", self.rows, allow_universal=True)
        self.assertEqual(uni.missing, [])

    def test_select_release(self):
        rel = {"0.9": [pyfile("a")], "1.0rc1": [pyfile("b")], "1.0": [pyfile("c", yanked=True)],
               "1.1": [pyfile("d")]}
        self.assertEqual(release.select_release(rel, minimum="0.9"), ["0.9", "1.1"])


class TestDevelopmentReviewFeedback(unittest.TestCase):
    class Api:
        def paginate(self, path, **kw):
            if path.endswith("/reviews?per_page=100"):
                return [{"id": 10, "state": "CHANGES_REQUESTED", "body": "Please follow the existing QEMU architecture list.",
                         "html_url": "https://github.com/o/r/pull/1#review", "commit_id": SHA_A,
                         "user": {"id": OWNER["id"], "login": OWNER["login"]}},
                        {"id": 11, "state": "CHANGES_REQUESTED", "body": "untrusted",
                         "commit_id": SHA_A, "user": {"id": 999, "login": "other"}}]
            if "/issues/" in path:
                return [{"id": 30, "body": "The Test workflow is missing", "created_at": NOW,
                         "html_url": "https://github.com/o/r/pull/1#issuecomment-30",
                         "user": {"id": OWNER["id"], "login": OWNER["login"]}},
                        {"id": 31, "body": "old feedback", "created_at": "2020-01-01T00:00:00Z",
                         "user": {"id": OWNER["id"], "login": OWNER["login"]}}]
            return [{"id": 20, "body": "Use manylinux_2_39_riscv64", "html_url": "https://github.com/o/r/pull/1#c",
                     "commit_id": SHA_A, "pull_request_review_id": 10, "path": "build.yml", "line": 33,
                     "user": {"id": OWNER["id"], "login": OWNER["login"]}}]

        def get(self, path):
            date = "2030-01-01T00:00:00Z" if path.endswith(SHA_B) else "2026-01-01T00:00:00Z"
            return {"commit": {"committer": {"date": date}}}

    def test_explanation_only_observation_does_not_bump_or_request_repair(self):
        import observe_ci
        rec = contribution(phase="development", activity="waiting_ci", revision=8, paused=True,
                           paused_at=NOW, paused_by="human")
        inquiry = {"reviews": [], "comments": [], "conversation_comments": [],
                   "review_ids": [], "comment_ids": [], "conversation_comment_ids": [],
                   "inquiry_comments": [],
                   "inquiry_conversation_comments": [{"id": 32, "body": "Explain why it failed. Do not change anything."}],
                   "inquiry_comment_ids": [], "inquiry_conversation_comment_ids": [32]}
        pending_ci = collect.CiSnapshot(
            repository={"id": 2, "full_name": "riseproject-upstreaming/proj"}, head_sha=SHA_A,
            runs=[run(1, status="in_progress", conclusion=None)],
            jobs_by_run={1: [job(1, "wheels riscv64", status="in_progress", conclusion=None)]})
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": SHA_A}), \
             mock.patch("observe_ci.collect.development_review_feedback", return_value=inquiry), \
             mock.patch("observe_ci.collect.ci_snapshot", return_value=pending_ci):
            observed = observe_ci.observe(object(), contributions={"c": rec}, operations={}, contracts={},
                                          policy=policy(), now=NOW)
        final = [item for kind, item in observed.staged if kind == "contributions"][-1]
        event = [item for kind, item in observed.staged if kind == "events"][-1]
        evidence = [item for kind, item in observed.staged if kind == "evidence"][-1]
        self.assertEqual((final["revision"], final["activity"], final["paused"]),
                         (8, "waiting_ci", True))
        self.assertEqual(final.get("pending_feedback_evidence_ids"), rec.get("pending_feedback_evidence_ids"))
        self.assertEqual(final["processed_pr_conversation_comment_ids"], [32])
        self.assertEqual(event["kind"], "record_explanation")
        self.assertFalse(event["revision_bumped"])
        self.assertEqual(evidence["kind"], "human_inquiry")
        self.assertNotIn("review_feedback", observed.report[0])

    def test_only_review_feedback_is_read_while_development_work_is_due(self):
        """`development.fix_needed` has two observation rows: on the recorded
        head an actionable review takes `record_actionable_review` and keeps
        the state; a moved head takes `record_head_change` to
        `development.waiting_ci`. CI is never read on the recorded head."""
        import observe_ci
        rec = contribution(phase="development", activity="fix_needed", revision=8)
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": rec["fork"]["head_sha"]}), \
             mock.patch("observe_ci.collect.development_review_feedback", return_value=None) as feedback, \
             mock.patch("observe_ci.collect.ci_snapshot") as snapshot:
            observed = observe_ci.observe(object(), contributions={"c": rec}, operations={},
                                          contracts={}, policy=policy(), now=NOW)
        self.assertEqual(observed.staged, [])
        feedback.assert_called_once()
        self.assertNotIn("check_pull", feedback.call_args.kwargs)
        snapshot.assert_not_called()
        moved = SHA_B if rec["fork"]["head_sha"] != SHA_B else SHA_A
        pending_ci = collect.CiSnapshot(repository={"id": 2, "full_name": "riseproject-upstreaming/proj"},
                                        head_sha=moved, runs=[], jobs_by_run={})
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": moved}), \
             mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
             mock.patch("observe_ci.collect.ci_snapshot", return_value=pending_ci):
            observed = observe_ci.observe(object(), contributions={"c": rec}, operations={},
                                          contracts={}, policy=policy(), now=NOW)
        events = [item["kind"] for kind, item in observed.staged if kind == "events"]
        final = [item for kind, item in observed.staged if kind == "contributions"][-1]
        self.assertEqual(events, ["record_head_change"])
        self.assertEqual((final["activity"], final["revision"], final["fork"]["head_sha"]),
                         ("waiting_ci", 9, moved))

    def test_a_review_while_development_work_is_due_is_recorded_once(self):
        import observe_ci
        rec = contribution(phase="development", activity="fix_needed", revision=8,
                           last_ci_evidence_id="ci-2-777-1")
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": SHA_A}), \
             mock.patch("observe_ci.collect.ci_snapshot") as snapshot:
            observed = observe_ci.observe(self.Api(), contributions={"c": rec}, operations={},
                                          contracts={}, policy=policy(), now=NOW)
        snapshot.assert_not_called()
        events = [item for kind, item in observed.staged if kind == "events"]
        final = [item for kind, item in observed.staged if kind == "contributions"][-1]
        self.assertEqual([e["kind"] for e in events], ["record_actionable_review"])
        self.assertEqual((events[0]["details"]["from_state"], events[0]["details"]["to_state"]),
                         ("development.fix_needed", "development.fix_needed"))
        self.assertEqual((final["activity"], final["revision"]), ("fix_needed", 9))
        self.assertEqual(len(final["pending_feedback_evidence_ids"]), 1)
        self.assertEqual(final["last_ci_evidence_id"], "ci-2-777-1")
        self.assertEqual((final["processed_review_ids"], final["processed_review_comment_ids"],
                          final["processed_pr_conversation_comment_ids"]), ([10], [20], [30]))
        # The row records the fences: one write of the record, one event,
        # and nothing written beside the row.
        self.assertEqual(len([1 for kind, _ in observed.staged if kind == "contributions"]), 1)
        self.assertNotIn("review_fences", engine.BESIDE_ROW)
        self.assertFalse(any(set(lifecycle._PROCESSED_IDS) & set(fields)
                             for fields in engine.BESIDE_ROW.values()))
        # The fences keep the same comments from being recorded again.
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": SHA_A}):
            again = observe_ci.observe(self.Api(), contributions={"c": final}, operations={},
                                       contracts={}, policy=policy(), now=NOW)
        self.assertEqual(again.staged, [])

    def test_an_inquiry_while_development_work_is_due_is_not_recorded(self):
        import observe_ci
        rec = contribution(phase="development", activity="fix_needed", revision=8)
        inquiry = {"reviews": [], "comments": [], "conversation_comments": [],
                   "review_ids": [], "comment_ids": [], "conversation_comment_ids": [],
                   "inquiry_comments": [],
                   "inquiry_conversation_comments": [{"id": 32, "body": "Why riscv64 only?"}],
                   "inquiry_comment_ids": [], "inquiry_conversation_comment_ids": [32]}
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": SHA_A}), \
             mock.patch("observe_ci.collect.development_review_feedback", return_value=inquiry), \
             mock.patch("observe_ci.collect.ci_snapshot") as snapshot:
            observed = observe_ci.observe(object(), contributions={"c": rec}, operations={},
                                          contracts={}, policy=policy(), now=NOW)
        self.assertEqual(observed.staged, [])
        snapshot.assert_not_called()

    def test_no_feedback_is_read_while_work_is_due_without_a_development_pr(self):
        import observe_ci
        rec = contribution(phase="development", activity="fix_needed", revision=8)
        rec["fork"] = dict(rec["fork"], pr_number=None)
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": SHA_A}), \
             mock.patch("observe_ci.collect.development_review_feedback") as feedback, \
             mock.patch("observe_ci.collect.ci_snapshot") as snapshot:
            observed = observe_ci.observe(object(), contributions={"c": rec}, operations={},
                                          contracts={}, policy=policy(), now=NOW)
        self.assertEqual(observed.staged, [])
        feedback.assert_not_called()
        snapshot.assert_not_called()

    def test_explanation_only_comments_are_separated_from_repairs(self):
        self.assertEqual(collect.review_comment_intent(
            "Explain me why it failed. Do not change anything."), "inquiry")
        self.assertEqual(collect.review_comment_intent("Why did this CI job fail?"), "inquiry")
        self.assertEqual(collect.review_comment_intent("Why did this fail? Fix it."), "repair")
        self.assertEqual(collect.review_comment_intent("Disable this temporarily."), "repair")

        class InquiryApi(self.Api):
            def paginate(inner, path, **kw):
                if "/issues/" in path:
                    return [{"id": 32, "body": "Explain why it failed. Do not change anything.",
                             "created_at": NOW, "user": {"id": OWNER["id"], "login": OWNER["login"]}}]
                if path.endswith("/reviews?per_page=100"):
                    return []
                return []

        rec = contribution(phase="development", activity="fix_needed")
        rec["fork"] = dict(rec["fork"], head_sha=SHA_A, pr_number=1)
        result = collect.development_review_feedback(InquiryApi(), rec, [OWNER["id"]])
        self.assertEqual(result["inquiry_conversation_comment_ids"], [32])
        self.assertEqual(result["conversation_comment_ids"], [])
        self.assertEqual(result["inquiry_conversation_comments"][0]["intent"], "inquiry")

    def test_only_new_authorized_current_head_review_feedback_is_returned(self):
        rec = contribution(phase="development", activity="waiting_ci")
        rec["fork"] = dict(rec["fork"], head_sha=SHA_A, pr_number=1)
        feedback = collect.development_review_feedback(self.Api(), rec, [OWNER["id"]])
        self.assertEqual(feedback["review_ids"], [10])
        self.assertEqual(feedback["comment_ids"], [20])
        self.assertEqual(feedback["conversation_comment_ids"], [30])
        self.assertEqual(feedback["comments"][0]["path"], "build.yml")
        self.assertEqual(feedback["conversation_comments"][0]["commit_id"], SHA_A)
        rec["processed_review_ids"] = [10]; rec["processed_review_comment_ids"] = [20]
        rec["processed_pr_conversation_comment_ids"] = [30]
        self.assertIsNone(collect.development_review_feedback(self.Api(), rec, [OWNER["id"]]))
        rec["fork"]["head_sha"] = SHA_B
        rec["processed_review_ids"] = []; rec["processed_review_comment_ids"] = []
        rec["processed_pr_conversation_comment_ids"] = []
        self.assertIsNone(collect.development_review_feedback(self.Api(), rec, [OWNER["id"]]))


class TestTriggers(unittest.TestCase):
    def test_semantics(self):
        doc = {"on": {"push": {"branches": ["main"], "paths": ["docs/**", "!docs/x/**"]}}}
        self.assertTrue(triggers.push_triggers(doc, ["docs/a.md"])[0])
        self.assertFalse(triggers.push_triggers(doc, ["docs/x/a.md"])[0])
        self.assertFalse(triggers.push_triggers(doc, [".state/a.json"])[0])
        self.assertTrue(triggers.push_triggers({True: "push"}, ["a"])[0])
        self.assertFalse(triggers.push_triggers({True: {"push": {"tags": ["v*"]}}}, ["a"])[0])
        self.assertEqual(triggers.classify_commit([".state/a", "ci_scripts/b"]), "mixed")

    def test_repository_state_only_push_refreshes_only_the_website(self):
        root = Path(__file__).resolve().parents[1]
        findings = triggers.audit(root / ".github/workflows", [".state/contributions/x.json"])
        self.assertEqual([f.workflow for f in findings if f.triggered], ["website.yml"])
        self.assertEqual(
            triggers.workflow_run_consumers(root / ".github/workflows"),
            ["controller.yml"],
        )


class TestTick(unittest.TestCase):
    def test_removed_intake_label_cancels_only_pre_work_request(self):
        rec = contribution(phase="queued", activity="idle", fork=None, plan_evidence_id=None,
                           admission_class=None)
        raw = {"id": rec["tracking_issue"]["id"], "number": rec["tracking_issue"]["number"],
               "body": "request", "labels": []}
        tick = controller.tick(contributions={"c": rec}, operations={}, consumed={}, projects={},
                               policy=policy(), inputs=controller.Inputs(now=T,
                               tracking_issues={raw["number"]: raw}, pressure=capacity.Pressure(ok=True)))
        final = [value for kind, value in tick.staged if kind == "contributions"][-1]
        self.assertEqual((final["phase"], final["activity"]), ("closed", "idle"))
        self.assertEqual(final["terminal_reason"], "withdrawn")
        self.assertEqual([value["kind"] for kind, value in tick.staged if kind == "events"],
                         ["withdraw_request"])
        self.assertEqual(tick.requested_effects, [])
        self.assertEqual(tick.report["intake"][-1]["decision"], "cancelled")

        active = contribution(phase="planning", activity="agent_running")
        active["active_work"] = {"dedupe_key": "k", "task": "planning", "revision": 0,
                                 "requested_at": NOW, "deadline_at": NOW, "generation": 0, "attempt": 1, "dispatched_run_id": None, "reason_ids": [], "feedback_evidence_ids": []}
        tick = controller.tick(contributions={"c": active}, operations={}, consumed={}, projects={},
                               policy=policy(), inputs=controller.Inputs(now=T,
                               tracking_issues={raw["number"]: raw}, pressure=capacity.Pressure(ok=True)))
        self.assertFalse(any(kind == "contributions" and value["phase"] == "closed"
                             for kind, value in tick.staged))
        # A request with a plan is not withdrawn by the label: the table's
        # guard requires no Work, no plan, and no fork.
        planned = dict(rec, plan_evidence_id="plan-1")
        tick = controller.tick(contributions={"c": planned}, operations={}, consumed={},
                               projects={}, policy=policy(), inputs=controller.Inputs(now=T,
                               tracking_issues={raw["number"]: raw}, pressure=capacity.Pressure(ok=True)))
        self.assertFalse(any(kind == "contributions" and value["phase"] == "closed"
                             for kind, value in tick.staged))

    def test_unlabeled_issue_without_state_is_a_noop(self):
        tick = controller.tick(contributions={}, operations={}, consumed={}, projects={}, policy=policy(),
                               inputs=controller.Inputs(now=T, issues=[], tracking_issues={},
                                                        pressure=capacity.Pressure(ok=True)))
        self.assertFalse(any(kind == "contributions" for kind, _ in tick.staged))
        self.assertEqual(tick.requested_effects, [])

    def test_idle_tick_selects_nothing(self):
        rec = contribution(activity="waiting_ci")
        res = controller.tick(contributions={"a": rec}, operations={}, consumed={}, projects={},
                              policy=policy(), inputs=controller.Inputs(now=T))
        self.assertEqual(res.requested_effects, [])
        self.assertEqual(res.staged, [])

    def test_pause_processed_before_work_and_unauthorized_ignored(self):
        rec = contribution(activity="fix_needed")
        cid = rec["contribution_id"]
        pressure = capacity.Pressure(ok=True)
        inputs = controller.Inputs(now=T, pressure=pressure, comments=[
            {"issue_number": 12, "comment": comment(
                f"/upstreaming pause contribution={cid} revision=0 reason=r", cid=5)},
        ])
        res = controller.tick(contributions={"a": rec}, operations={}, consumed={}, projects={},
                              policy=policy(), inputs=inputs)
        self.assertEqual(res.requested_effects, [])
        self.assertTrue(any(k == "contributions" and r["paused"] for k, r in res.staged))

    def test_repair_selected_with_capacity_only(self):
        rec = contribution(activity="fix_needed")
        none = controller.tick(contributions={"a": rec}, operations={}, consumed={}, projects={},
                               policy=policy(), inputs=controller.Inputs(now=T))
        self.assertEqual(none.requested_effects, [])
        self.assertIn("no expensive admission", none.report["deferred"][0]["why"])
        res = controller.tick(contributions={"a": rec}, operations={}, consumed={}, projects={},
                              policy=policy(), inputs=controller.Inputs(now=T, pressure=capacity.Pressure(ok=True)))
        self.assertEqual(len(res.requested_effects), 1)
        eff = res.requested_effects[0]
        self.assertEqual(eff["effect_contract"]["ref"], "riscv64-dev")
        self.assertEqual(eff["expected_head"], SHA_A)

    def test_repair_carries_pending_review_and_latest_ci_evidence(self):
        rec = contribution(activity="fix_needed")
        rec["pending_feedback_evidence_ids"] = ["review-1", "review-2"]
        rec["last_ci_evidence_id"] = "ci-3"
        res = controller.tick(contributions={"a": rec}, operations={}, consumed={}, projects={},
                              policy=policy(), inputs=controller.Inputs(
                                  now=T, pressure=capacity.Pressure(ok=True)),
                              evidence={"ci-3": ci_record("ci-3", SHA_A, failing="build")})
        self.assertEqual(res.requested_effects[0]["reason_ids"], ["review-1", "review-2", "ci-3"])
        self.assertEqual(res.requested_effects[0]["feedback_evidence_ids"],
                         ["review-1", "review-2"])

    def test_uncertain_operation_holds_work(self):
        rec = contribution(activity="fix_needed")
        op = recovery.plan(contribution_id=rec["contribution_id"], revision=0, kind="branch_update",
                           policy_version="p", reason_ids=[], target={"repository": "r"}, now=NOW)
        op["status"] = "uncertain"
        res = controller.tick(contributions={"a": rec}, operations={op["operation_id"]: op}, consumed={},
                              projects={}, policy=policy(),
                              inputs=controller.Inputs(now=T, pressure=capacity.Pressure(ok=True)))
        self.assertEqual(res.requested_effects, [])

    def test_dispatch_once_per_task_and_no_deadline(self):
        from datetime import timedelta

        import reconcile
        rec = contribution(phase="queued", activity="idle")
        contribs = {"c": rec}
        dispatched = []
        for i in range(4):
            snap = reconcile.Snapshot(contributions=contribs, operations={}, consumed={}, projects={}, evidence={})
            out = reconcile.tick(None, snap, policy=policy(read_only=False), now=T + timedelta(minutes=15 * i),
                                 publications=[], pressure=capacity.Pressure(ok=True), observe=False)
            dispatched += [e["task"] for e in out.report["requested_effects"]]
            for k, r in out.staged:
                if k == "contributions":
                    contribs = {"c": r}
        self.assertEqual(dispatched, ["planning"])
        self.assertEqual(contribs["c"]["phase"], "planning")
        self.assertEqual(contribs["c"]["activity"], "agent_running")
        # No outcome past the recorded deadline: no row reads the deadline,
        # so nothing changes and nothing is dispatched again; a terminal
        # graph is recovered by the table's Work recovery rows.
        snap = reconcile.Snapshot(contributions=contribs, operations={}, consumed={}, projects={}, evidence={})
        out = reconcile.tick(None, snap, policy=policy(read_only=False), now=T + timedelta(hours=7),
                             publications=[], pressure=capacity.Pressure(ok=True), observe=False)
        self.assertEqual(out.report["requested_effects"], [])
        self.assertNotIn("timed_out_work", out.report)
        self.assertEqual([r for k, r in out.staged if k == "contributions"], [])

    def test_planning_outcome_auto_admits_development(self):
        import reconcile
        from tests.test_plan_gate import plan
        rec = contribution(phase="planning", activity="agent_running")
        rec["active_work"] = {"dedupe_key": "k", "task": "planning", "revision": 0,
                              "requested_at": NOW, "deadline_at": NOW, "generation": 0, "attempt": 1, "dispatched_run_id": None, "reason_ids": [], "feedback_evidence_ids": []}
        pub = {"status": "planning", "task": {"contribution_id": rec["contribution_id"], "revision": 0,
                                                "generation": 0, "dedupe_key": "k"},
               "worker_run_id": "7", "publication_artifact_id": 3,
               "plan": plan(), "proposal": {"diagnosis": "Bounded wheel integration work is actionable."},
               "artifact": {"digest": "sha256:" + "0" * 64}}
        legacy = dict(pub, publication_artifact_id=2, worker_run_id="6", plan={"legacy": "missing decision"})
        snap = reconcile.Snapshot(contributions={"c": rec}, operations={}, consumed={}, projects={}, evidence={})
        out = reconcile.tick(
            None,
            snap,
            policy=policy(read_only=False),
            now=T,
            publications=[legacy, pub],
            pressure=capacity.Pressure(ok=True),
            observe=False,
        )
        ev = [r for k, r in out.staged if k == "evidence"]
        self.assertEqual(ev[0]["kind"], "plan")
        self.assertEqual(ev[0]["data"], plan())
        final = [r for k, r in out.staged if k == "contributions"][-1]
        self.assertEqual(final["plan_evidence_id"], ev[0]["evidence_id"])
        # `admit_development`: the fork is provisioned before any patch Work.
        self.assertEqual(
            (final["phase"], final["activity"], final["revision"]),
            ("provisioning", "idle", 1),
        )
        self.assertEqual(final["admission_class"], "configuration_integration")
        self.assertEqual(final["scope_paths"], [".github/workflows/*", "README.rst"])
        self.assertTrue(final["scope_coverage_complete"])
        self.assertIsNone(final["pending_human_action"])
        self.assertIsNone(final["proposal_artifact"])
        self.assertEqual(len(out.report["provision_requests"]), 1)
        self.assertEqual(final["active_operation_id"],
                         out.report["provision_requests"][0]["operation_id"])
        self.assertTrue(any(item.get("ignored") == "superseded publication artifact"
                            for item in out.report["publications"]))
        self.assertEqual(out.report["requested_effects"], [])
        kinds = [r["kind"] for k, r in out.staged if k == "events"]
        self.assertIn("admit_development", kinds)
        self.assertIn("start_operation", kinds)

    def test_refresh_plan_returns_to_existing_pr_and_requires_worker_pass(self):
        import commands
        import reconcile
        from tests.test_plan_gate import plan
        rec = contribution(activity="waiting_ci", revision=7, plan_evidence_id="old-plan",
                           scope_paths=[".github/workflows/wheels.yml"],
                           scope_coverage_complete=True)
        refreshed = apply_command(
            rec, commands.parse('/upstreaming refresh revision=7 reason="audit dependencies"'),
            command_id="comment-9", actor=OWNER, now=NOW).record
        refreshed["phase"] = "planning"
        refreshed["activity"] = "agent_running"
        refreshed["active_work"] = {"dedupe_key": "refresh", "task": "planning",
                                    "revision": 8, "requested_at": NOW, "deadline_at": NOW, "generation": 0, "attempt": 1, "dispatched_run_id": None, "reason_ids": [], "feedback_evidence_ids": []}
        pub = {"status": "planning",
               "task": {"contribution_id": rec["contribution_id"], "revision": 8,
                        "generation": 0, "dedupe_key": "refresh"},
               "worker_run_id": "9", "publication_artifact_id": 5,
               "plan": plan(), "proposal": {"diagnosis": "Fresh complete plan."},
               "artifact": {"digest": "sha256:" + "2" * 64}}
        snap = reconcile.Snapshot(contributions={"c": refreshed}, operations={}, consumed={},
                                  projects={}, evidence={})
        out = reconcile.tick(None, snap, policy=policy(read_only=False), now=T,
                             publications=[pub], pressure=capacity.Pressure(ok=True), observe=False)
        evidence = [r for k, r in out.staged if k == "evidence"][0]
        final = [r for k, r in out.staged if k == "contributions"][-1]
        self.assertEqual((final["phase"], final["activity"], final["revision"]),
                         ("development", "agent_running", 9))
        self.assertEqual(final["fork"], rec["fork"])
        self.assertEqual(final["scope_paths"], [".github/workflows/*", "README.rst"])
        self.assertIsNone(final["refresh"])
        self.assertEqual(final["plan_evidence_id"], evidence["evidence_id"])
        # The refresh cleared the CI evidence and no feedback is pending:
        # `request_patch` asks for a patch against the refreshed plan.
        self.assertEqual(out.report["controller"]["selected"][0]["task"], "development_patch")
        self.assertIn(evidence["evidence_id"], out.report["requested_effects"][0]["reason_ids"])
        self.assertEqual(out.report["provision_requests"], [])

    def test_planning_can_park_without_human_gate(self):
        import reconcile
        from tests.test_plan_gate import plan
        rec = contribution(phase="planning", activity="agent_running")
        rec["active_work"] = {"dedupe_key": "k", "task": "planning", "revision": 0,
                              "requested_at": NOW, "deadline_at": NOW, "generation": 0, "attempt": 1, "dispatched_run_id": None, "reason_ids": [], "feedback_evidence_ids": []}
        research = plan()
        research["proposal"].update(decision="park", admission_class=None, allowed_paths=[],
                                    rationale="No concrete upstream patch is currently actionable; retain verified evidence and wait for a new prerequisite.")
        pub = {"status": "planning", "task": {"contribution_id": rec["contribution_id"], "revision": 0,
                                                "generation": 0, "dedupe_key": "k"},
               "worker_run_id": "8", "publication_artifact_id": 4, "plan": research,
               "proposal": {"diagnosis": "No actionable patch."}, "artifact": {"digest": "sha256:" + "1" * 64}}
        snap = reconcile.Snapshot(contributions={"c": rec}, operations={}, consumed={}, projects={}, evidence={})
        out = reconcile.tick(None, snap, policy=policy(read_only=False), now=T, publications=[pub],
                             pressure=capacity.Pressure(ok=True), observe=False)
        final = [r for k, r in out.staged if k == "contributions"][-1]
        # `park_planning` ends the generation without a revision bump.
        self.assertEqual((final["phase"], final["activity"], final["active_work"], final["revision"]),
                         ("parked", "idle", None, 0))
        self.assertIn("No concrete upstream patch", final["parked_reason"])
        self.assertEqual(out.report["provision_requests"], [])

    def test_command_resolves_contribution_from_issue(self):
        rec = contribution(activity="waiting_ci")
        res = controller.tick(contributions={"a": rec}, operations={}, consumed={}, projects={}, policy=policy(),
                              inputs=controller.Inputs(now=T, comments=[
                                  {"issue_number": 12, "comment": comment('/upstreaming pause reason="test"', cid=7)},
                                  {"issue_number": 999, "comment": comment("/upstreaming pause", cid=8)},
                                  {"issue_number": 12, "comment": comment(
                                      "/upstreaming pause contribution=other--x/1", cid=9)}]))
        by = {c["comment"]: c for c in res.report["commands"]}
        self.assertEqual(by[7]["result"], "accepted")
        self.assertEqual(by[8]["result"], "rejected")
        self.assertIn("tracks no contribution", by[8]["reason"])
        self.assertIn("this issue tracks", by[9]["reason"])
        receipt = [r for k, r in res.staged if k == "commands" and r["comment_id"] == 7][0]
        self.assertEqual((receipt["contribution_id"], receipt["revision"]), (rec["contribution_id"], 0))
        self.assertTrue([r for k, r in res.staged if k == "contributions"][-1]["paused"])

    def test_intake_creates_queued_contribution(self):
        from tests.test_commands_intake import issue
        res = controller.tick(contributions={}, operations={}, consumed={}, projects={},
                              policy=policy(), inputs=controller.Inputs(
                                  now=T, issues=[issue()], acceptances={77: OWNER["id"]},
                                  upstream_snapshots={"example/proj": {
                                      "repository_id": 99, "base_ref": "main", "base_sha": "a" * 40}}))
        kinds = [k for k, _ in res.staged]
        self.assertIn("contributions", kinds)
        self.assertIn("projects", kinds)
        # Planning is selected; it needs no write admission.
        self.assertEqual(res.requested_effects[0]["effect_contract"], {"kind": "none"})

    def test_work_waits_for_a_bound_upstream_identity(self):
        # Every row that allocates Work is guarded by the bound upstream
        # identity and base: without a snapshot no Work is requested and the
        # record is deferred with that guard; a later tick binds them and
        # requests it.
        from tests.test_commands_intake import issue
        res = controller.tick(contributions={}, operations={}, consumed={}, projects={},
                              policy=policy(), inputs=controller.Inputs(
                                  now=T, issues=[issue()], acceptances={77: OWNER["id"]}))
        self.assertEqual(res.requested_effects, [])
        why = [d["why"] for d in res.report["deferred"] if d["contribution"] == "example--proj/12"]
        self.assertEqual(len(why), 1)
        self.assertIn("upstream identity and base not bound", why[0])
        queued = [r for k, r in res.staged if k == "contributions"][-1]
        self.assertEqual((queued["phase"], queued["active_work"]), ("queued", None))
        again = controller.tick(contributions={queued["contribution_id"]: queued}, operations={},
                                consumed={}, projects={}, policy=policy(), inputs=controller.Inputs(
                                    now=T, upstream_snapshots={"example/proj": {
                                        "repository_id": 99, "base_ref": "main",
                                        "base_sha": "a" * 40}}))
        self.assertEqual(len(again.requested_effects), 1)


class TestRefreshFromParkedBindsIdentity(unittest.TestCase):
    """A refresh from `parked.idle` clears the upstream identity and base;
    the tick that restarts the generation binds them again and requests the
    planning Work, or keeps the record waiting when the upstream cannot be
    read."""

    SNAPSHOT = {"repository_id": 1, "base_ref": "trunk", "base_sha": SHA_B}

    def parked(self):
        return contribution(phase="parked", activity="idle", generation=1, revision=4,
                            parked_reason="old attempt", previous_phase="development")

    def tick(self, rec, snapshots):
        cid = rec["contribution_id"]
        return controller.tick(
            contributions={cid: rec}, operations={}, consumed={}, projects={}, policy=policy(),
            inputs=controller.Inputs(now=T, upstream_snapshots=snapshots, comments=[
                {"issue_number": 12, "comment": comment(
                    f'/upstreaming refresh contribution={cid} revision=4 reason="clean start"',
                    cid=31)}]))

    def final(self, res):
        return [r for k, r in res.staged if k == "contributions"][-1]

    def test_refresh_requests_planning_with_identity_in_the_same_tick(self):
        res = self.tick(self.parked(), {"example/proj": dict(self.SNAPSHOT)})
        self.assertEqual(res.report["commands"][0]["result"], "accepted")
        self.assertEqual([e["kind"] for k, e in res.staged if k == "events"],
                         ["restart_generation", "begin_planning", "dispatch_planning"])
        final = self.final(res)
        self.assertEqual((final["phase"], final["activity"], final["generation"]),
                         ("planning", "agent_running", 2))
        self.assertEqual(final["upstream"], {"repository_id": 1, "full_name": "example/proj",
                                             "base_ref": "trunk", "base_sha": SHA_B})
        self.assertEqual(len(res.requested_effects), 1)
        self.assertEqual(res.requested_effects[0]["task"], "planning")
        self.assertEqual(res.requested_effects[0]["dedupe_key"],
                         final["active_work"]["dedupe_key"])

    def test_refresh_without_snapshot_waits_unbound(self):
        for snapshots in ({}, {"example/proj": {"error": "upstream unavailable"}}):
            with self.subTest(snapshots=snapshots):
                res = self.tick(self.parked(), snapshots)
                self.assertEqual(res.report["commands"][0]["result"], "accepted")
                final = self.final(res)
                self.assertEqual((final["phase"], final["activity"]), ("queued", "idle"))
                self.assertIsNone(final["active_work"])
                self.assertEqual(final["upstream"]["repository_id"], None)
                self.assertEqual(res.requested_effects, [])
                self.assertTrue(any(d["contribution"] == final["contribution_id"]
                                    for d in res.report["deferred"]))
                # A later tick with the snapshot binds them and requests the Work.
                later = controller.tick(
                    contributions={final["contribution_id"]: final}, operations={}, consumed={},
                    projects={}, policy=policy(), inputs=controller.Inputs(
                        now=T, upstream_snapshots={"example/proj": dict(self.SNAPSHOT)}))
                self.assertEqual(len(later.requested_effects), 1)
                self.assertEqual(self.final(later)["upstream"]["base_sha"], SHA_B)

    def test_a_parked_record_is_not_bound_or_paused_without_a_restart(self):
        rec = self.parked()
        res = controller.tick(
            contributions={"a": rec}, operations={}, consumed={}, projects={}, policy=policy(),
            inputs=controller.Inputs(now=T, upstream_snapshots={"example/proj": dict(
                self.SNAPSHOT, repository_id=77)}))
        self.assertEqual(res.staged, [])
        self.assertEqual(res.requested_effects, [])

    def test_the_collector_reads_a_parked_upstream_only_for_a_refresh(self):
        class Api:
            def __init__(self):
                self.paths = []

            def get(self, path):
                self.paths.append(path)
                if path == "/repos/example/proj":
                    return {"id": 1, "full_name": "example/proj", "default_branch": "trunk"}
                return {"name": "trunk", "commit": {"sha": SHA_B, "commit": {"tree": {"sha": SHA_A}},
                                                   "parents": []}}

        rec = self.parked()
        api = Api()
        self.assertEqual(collect.upstream_snapshots(api, [rec], [], policy()), {})
        self.assertEqual(api.paths, [])
        refresh = {"issue_number": 12, "comment": comment(
            "/upstreaming refresh contribution=example--proj/12 revision=4 reason=r")}
        other = {"issue_number": 12, "comment": comment("/upstreaming pause reason=r")}
        self.assertEqual(collect.upstream_snapshots(api, [rec], [], policy(), [other]), {})
        self.assertEqual(collect.upstream_snapshots(api, [rec], [], policy(), [refresh]),
                         {"example/proj": dict(self.SNAPSHOT)})

    def test_the_collector_counts_only_a_refresh_the_controller_can_take(self):
        class Api:
            def __init__(self):
                self.paths = []

            def get(self, path):
                self.paths.append(path)
                if path == "/repos/example/proj":
                    return {"id": 1, "full_name": "example/proj", "default_branch": "trunk"}
                return {"name": "trunk", "commit": {"sha": SHA_B, "commit": {"tree": {"sha": SHA_A}},
                                                   "parents": []}}

        rec = self.parked()
        body = "/upstreaming refresh contribution=example--proj/12 revision=4 reason=r"
        read = {"example/proj": dict(self.SNAPSHOT)}
        cases = (
            # commands.parse accepts runs of spaces between the prefix, the
            # verb, and the fields: the collector reads what it accepts.
            ("spaced", comment("/upstreaming   refresh  revision=4   reason=r", cid=41), (), read),
            ("consumed", comment(body, cid=42), (42,), {}),
            ("unauthorized", comment(body, actor=STRANGER, cid=43), (), {}),
            ("quoted", comment("> " + body, cid=44), (), {}),
            ("malformed", comment("/upstreaming refresh revision=four", cid=45), (), {}),
        )
        for name, item, consumed, expected in cases:
            with self.subTest(name):
                api = Api()
                got = collect.upstream_snapshots(api, [rec], [], policy(),
                                                 [{"issue_number": 12, "comment": item}], consumed)
                self.assertEqual(got, expected)
                self.assertEqual(bool(api.paths), bool(expected))

    def test_the_collector_reads_a_parked_upstream_at_the_requested_branch(self):
        class Api:
            def __init__(self):
                self.paths = []

            def get(self, path):
                self.paths.append(path)
                if path == "/repos/example/proj":
                    return {"id": 1, "full_name": "example/proj", "default_branch": "trunk"}
                name = path.rsplit("/", 1)[1]
                return {"name": name, "commit": {"sha": SHA_B, "commit": {"tree": {"sha": SHA_A}},
                                                 "parents": []}}

        refresh = {"issue_number": 12, "comment": comment(
            "/upstreaming refresh revision=4 reason=r", cid=46)}
        for branch, expected in (("release-2", "release-2"), (None, "trunk")):
            with self.subTest(branch=branch):
                api = Api()
                got = collect.upstream_snapshots(api, [self.parked_from(branch)], [], policy(),
                                                 [refresh])
                self.assertEqual(got, {"example/proj": dict(self.SNAPSHOT, base_ref=expected)})
                self.assertEqual(api.paths[-1], f"/repos/example/proj/branches/{expected}")

    def parked_from(self, branch):
        """A parked record whose request asked for ``branch`` (None: no
        branch) at intake, bound to it in the generation that parked."""
        body = ("### Upstream repository\n\nhttps://github.com/example/proj\n\n"
                "### Upstream target branch\n\n" + (branch or "_No response_") + "\n")
        rec = self.parked()
        return dict(rec, tracking_issue=dict(rec["tracking_issue"], description=body),
                    upstream=dict(rec["upstream"], base_ref=branch or "main"))

    def test_a_restart_keeps_the_requested_target_branch(self):
        rec = self.parked_from("release-2")
        res = self.tick(rec, {"example/proj": dict(self.SNAPSHOT, base_ref="release-2")})
        final = self.final(res)
        self.assertEqual(final["upstream"], {"repository_id": 1, "full_name": "example/proj",
                                             "base_ref": "release-2", "base_sha": SHA_B})
        self.assertEqual(final["activity"], "agent_running")
        self.assertEqual(len(res.requested_effects), 1)
        # A snapshot of another branch binds nothing: the record waits.
        res = self.tick(rec, {"example/proj": dict(self.SNAPSHOT)})
        final = self.final(res)
        self.assertEqual(final["upstream"], {"repository_id": None, "full_name": "example/proj",
                                             "base_ref": "release-2", "base_sha": None})
        self.assertEqual(res.requested_effects, [])
        self.assertIn({"contribution": "example--proj/12",
                       "why": "upstream snapshot is not of the requested base branch"},
                      res.report["deferred"])

    def test_a_restart_without_a_requested_branch_binds_the_default_branch(self):
        res = self.tick(self.parked_from(None), {"example/proj": dict(self.SNAPSHOT)})
        final = self.final(res)
        self.assertEqual(final["upstream"]["base_ref"], "trunk")
        self.assertEqual(len(res.requested_effects), 1)

    def test_a_restart_while_paused_binds_the_identity_and_allocates_nothing(self):
        rec = dict(self.parked(), paused=True, paused_at=NOW, paused_by="human")
        res = self.tick(rec, {"example/proj": dict(self.SNAPSHOT)})
        self.assertEqual(res.report["commands"][0]["result"], "accepted")
        self.assertEqual([e["kind"] for k, e in res.staged if k == "events"],
                         ["restart_generation"])
        final = self.final(res)
        self.assertEqual((final["phase"], final["activity"], final["paused"]),
                         ("queued", "idle", True))
        self.assertEqual(final["upstream"], {"repository_id": 1, "full_name": "example/proj",
                                             "base_ref": "trunk", "base_sha": SHA_B})
        self.assertIsNone(final["active_work"])
        self.assertEqual(res.requested_effects, [])


class TestBaseBranchUnavailable(unittest.TestCase):
    """An upstream base branch that cannot be read where Work is due: a
    definitive answer (missing, renamed) leads to the human wait of the
    phase at once, any other failure only once `source_reads` is exhausted
    (a count of consecutive failed reads and a minimum time); the collector
    stops reading while the contribution waits, and a resume or a restart on
    another branch continues. A branch renamed while the upstream PR is open
    is followed without a human."""

    SNAPSHOT = {"repository_id": 1, "base_ref": "main", "base_sha": SHA_B}
    MISSING = {"error": "upstream base branch 'main' does not exist", "ref": "main",
               "base_ref": "main", "missing": True, "repository_id": 1}
    RENAMED = {"error": "upstream base branch 'master' was renamed", "ref": "master",
               "base_ref": "master", "missing": True, "renamed_to": "main", "repository_id": 1}
    TRANSIENT = {"error": "GET /repos/example/proj/branches/main: HTTP 503", "ref": "main",
                 "scope": "branch"}
    REPOSITORY = {"error": "GET /repos/example/proj: HTTP 404", "ref": "main",
                  "scope": "repository"}
    MOVED = {"error": "GitHub names the upstream repository 'other/proj' now", "ref": "main",
             "scope": "repository", "renamed_repository": "other/proj",
             "renamed_repository_id": 1}
    PR = {"id": 77, "number": 5, "url": "https://github.com/example/proj/pull/5",
          "head_repository_id": 2, "head_ref": "riscv64", "head_sha": SHA_A,
          "base_ref": "master", "registered_by": OWNER}

    class Api:
        """The upstream as GitHub answers it: ``branches`` maps a branch
        to the name its response carries (another name for a rename);
        a branch it lacks is HTTP 404; ``fail`` answers every read with it,
        ``repo_fail`` the read of the repository; ``pr`` is the upstream PR
        (``None``: its read fails); ``compare`` the status of a comparison."""

        def __init__(self, branches=(("main", "main"),), fail=None, repo_fail=None,
                     full_name="example/proj", pr=None, compare="ahead"):
            self.branches, self.fail, self.repo_fail = dict(branches), fail, repo_fail
            self.full_name, self.pr, self.compare, self.paths = full_name, pr, compare, []
            self.base = "https://api.github.test"

        def get(self, path):
            self.paths.append(path)
            if self.fail:
                raise collect.CollectError(f"GET {path}: {self.fail}")
            if path == "/repos/example/proj":
                if self.repo_fail:
                    raise collect.CollectError(f"GET {path}: {self.repo_fail}")
                return {"id": 1, "full_name": self.full_name, "default_branch": "main"}
            if "/pulls/" in path:
                if self.pr is None:
                    raise collect.CollectError(f"GET {path}: HTTP 502")
                return self.pr
            name = self.branches.get(path.rsplit("/", 1)[1])
            if name is None:
                raise collect.CollectError(f"GET {path}: HTTP 404")
            return {"name": name, "commit": {"sha": SHA_B, "commit": {"tree": {"sha": SHA_A}},
                                             "parents": []}}

        def _request(self, url):
            self.paths.append(url)
            return {"status": self.compare}, {}

    def queued(self, **over):
        rec = contribution(phase="queued", activity="idle", fork=None,
                           upstream={"repository_id": None, "full_name": "example/proj",
                                     "base_ref": "main", "base_sha": None})
        rec.update(over)
        return rec

    def reviewing(self, activity="fix_needed", **over):
        """A record in upstream review on `master`, its PR open."""
        rec = contribution(phase="upstream_review", activity=activity,
                           upstream={"repository_id": 1, "full_name": "example/proj",
                                     "base_ref": "master", "base_sha": SHA_A},
                           upstream_pr=dict(self.PR))
        rec.update(over)
        return rec

    def waiting(self, state, kind="base_branch_unavailable", counter=None, **over):
        phase, activity = state.split(".")
        rec = contribution(phase=phase, activity=activity, pending_human_action={
            "kind": kind, "requested_at": NOW, "reminded_at": None, "escalated_at": None,
            "counter": counter})
        rec.update(over)
        return rec

    def pull(self, **over):
        """The upstream PR as GitHub reports it after the rename."""
        pr = {"id": 77, "number": 5, "state": "open", "merged": False,
              "base": {"ref": "main", "repo": {"id": 1}},
              "head": {"ref": "riscv64", "sha": SHA_A, "repo": {"id": 2}}}
        pr.update(over)
        return pr

    def tick(self, rec, snapshot, comments=(), consumed=None, now=T):
        return controller.tick(
            contributions={rec["contribution_id"]: rec}, operations={},
            consumed={} if consumed is None else consumed, projects={}, policy=policy(),
            inputs=controller.Inputs(now=now, comments=list(comments),
                                     upstream_snapshots={"example/proj": snapshot}
                                     if snapshot is not None else {}))

    def final(self, res, rec):
        staged = [r for k, r in res.staged if k == "contributions"]
        return staged[-1] if staged else rec

    def events(self, res):
        return [e for k, e in res.staged if k == "events"]

    def render(self, event, record):
        import status_ledger
        return status_ledger.render(dict(event, sequence=1), record,
                                    "https://github.com/r/actions/runs/1")

    # The collector

    def test_the_collector_tells_a_definitive_answer_from_a_failed_read(self):
        rec = contribution(phase="development", activity="fix_needed")
        cases = (
            ("missing", self.Api(branches=()), dict(self.MISSING)),
            ("renamed", self.Api(branches={"main": "trunk"}),
             {"error": "upstream base branch 'main' was renamed", "ref": "main",
              "base_ref": "main", "missing": True, "renamed_to": "trunk",
              "repository_id": 1}),
            ("repository unavailable", self.Api(fail="HTTP 503"),
             {"error": "GET /repos/example/proj: HTTP 503", "ref": "main",
              "scope": "repository"}),
            ("read", self.Api(), dict(self.SNAPSHOT)),
        )
        for name, api, expected in cases:
            with self.subTest(name):
                self.assertEqual(collect.upstream_snapshots(api, [rec], [], policy()),
                                 {"example/proj": expected})

    def test_a_failed_branch_read_is_not_a_definitive_answer(self):
        rec = contribution(phase="development", activity="fix_needed")

        class Flaky(self.Api):
            def get(self, path):
                if "/branches/" in path:
                    raise collect.CollectError(f"GET {path}: HTTP 503")
                return super().get(path)

        self.assertEqual(collect.upstream_snapshots(Flaky(), [rec], [], policy()),
                         {"example/proj": dict(self.TRANSIENT)})

    def test_a_repository_that_is_gone_or_forbidden_stays_a_failed_read(self):
        # HTTP 404 and 403 for the repository say nothing definitive about the
        # branch: the token may have lost access. The read is charged, and
        # the contribution waits only once `source_reads` is exhausted.
        for status in ("HTTP 404", "HTTP 403"):
            with self.subTest(status):
                got = collect.upstream_snapshots(self.Api(repo_fail=status),
                                                 [self.queued()], [], policy())
                self.assertEqual(got, {"example/proj": {
                    "error": f"GET /repos/example/proj: {status}", "ref": "main",
                    "scope": "repository"}})
                res = self.tick(self.queued(), got["example/proj"])
                final = self.final(res, self.queued())
                self.assertEqual(engine.state_key(final), "queued.idle")
                self.assertEqual([e["kind"] for e in self.events(res)],
                                 ["record_source_unavailable"])
        moved = collect.upstream_snapshots(self.Api(full_name="other/proj"),
                                           [self.queued()], [], policy())
        self.assertEqual(moved, {"example/proj": dict(self.MOVED)})

    def test_the_collector_does_not_read_while_the_contribution_waits(self):
        resume = {"issue_number": 12, "comment": comment(
            "/upstreaming resume revision=0 reason=restored", cid=61)}
        # Every `upstream_unavailable` wait is read on every tick: with the
        # counter `source_reads` failed reads caused it, and with none a
        # renamed repository did (below).
        for kind in ("base_branch_unavailable",):
            rec = self.waiting("development.waiting_human", kind)
            cases = (("no command", [], (), False), ("resume", [resume], (), True),
                     ("consumed", [resume], (61,), False))
            for name, comments, consumed, read in cases:
                with self.subTest(kind=kind, case=name):
                    api = self.Api()
                    got = collect.upstream_snapshots(api, [rec], [], policy(), comments, consumed)
                    self.assertEqual(bool(api.paths), read)
                    self.assertEqual(got, {"example/proj": dict(self.SNAPSHOT)} if read else {})

    def test_a_duplicate_intake_request_does_not_read_a_waiting_upstream(self):
        rec = self.waiting("planning.waiting_human")
        body = "### Upstream repository\n\nhttps://github.com/example/proj\n\n### Packages\n\n_No response_\n"
        duplicate = {"id": 900, "number": 90, "body": body, "user": dict(STRANGER),
                     "labels": [{"name": "upstreaming-request"}]}
        api = self.Api()
        self.assertEqual(collect.upstream_snapshots(api, [rec], [duplicate], policy()), {})
        self.assertEqual(api.paths, [])
        # Without a contribution, the request is read as before.
        self.assertEqual(collect.upstream_snapshots(self.Api(), [], [duplicate], policy()),
                         {"example/proj": dict(self.SNAPSHOT)})

    def test_the_collector_reads_a_parked_upstream_at_the_branch_of_the_refresh(self):
        rec = contribution(phase="parked", activity="idle", parked_reason="r",
                           previous_phase="development")
        api = self.Api(branches={"trunk": "trunk"})
        refresh = {"issue_number": 12, "comment": comment(
            "/upstreaming refresh revision=0 branch=trunk reason=renamed", cid=62)}
        got = collect.upstream_snapshots(api, [rec], [], policy(), [refresh])
        self.assertEqual(got, {"example/proj": dict(self.SNAPSHOT, base_ref="trunk")})
        self.assertEqual(api.paths[-1], "/repos/example/proj/branches/trunk")

    # The controller

    def test_a_missing_branch_waits_for_a_human_in_planning_at_once(self):
        import status_ledger
        rec = self.queued()
        res = self.tick(rec, dict(self.MISSING))
        final = self.final(res, rec)
        self.assertEqual(engine.state_key(final), "planning.waiting_human")
        self.assertEqual(final["pending_human_action"]["kind"], "base_branch_unavailable")
        self.assertIsNone(final["pending_human_action"]["counter"])
        self.assertEqual(final["revision"], rec["revision"])
        self.assertEqual(res.requested_effects, [])
        [event] = self.events(res)
        self.assertEqual(event["kind"], "require_base_branch")
        self.assertEqual(event["details"]["base_ref"], "main")
        self.assertEqual([d for d in res.report["deferred"]
                          if d["contribution"] == rec["contribution_id"]],
                         [{"contribution": rec["contribution_id"],
                           "why": "source verification unavailable"}])
        self.assertTrue(status_ledger.notifies(dict(event, sequence=1), "queued.idle",
                                               "planning.waiting_human"))
        text = self.render(event, final)
        self.assertIn("Cause: the upstream says the base branch `main` does not exist.", text)
        self.assertNotIn("pending_human_action.counter", text)
        self.assertIn("To continue on another branch, park first; then, once it is parked, "
                      f"post `/upstreaming refresh revision={final['revision']} branch=NAME", text)
        self.assertNotIn("parked.idle", text)

    def test_a_renamed_branch_of_a_bound_record_waits_in_its_phase(self):
        import status_ledger
        rec = contribution(phase="development", activity="fix_needed",
                           upstream={"repository_id": 1, "full_name": "example/proj",
                                     "base_ref": "master", "base_sha": SHA_B})
        res = self.tick(rec, dict(self.RENAMED))
        final = self.final(res, rec)
        self.assertEqual(engine.state_key(final), "development.waiting_human")
        self.assertEqual(final["pending_human_action"]["kind"], "base_branch_unavailable")
        self.assertEqual(final["upstream"], rec["upstream"])
        self.assertEqual(res.requested_effects, [])
        [event] = self.events(res)
        self.assertEqual((event["details"]["base_ref"], event["details"]["renamed_to"]),
                         ("master", "main"))
        self.assertTrue(status_ledger.notifies(dict(event, sequence=1), "development.fix_needed",
                                               "development.waiting_human"))
        text = self.render(event, final)
        self.assertIn("Cause: the upstream renamed the base branch `master` to `main`.", text)
        self.assertIn(f"/upstreaming resume revision={final['revision']}", text)
        self.assertIn(f"/upstreaming park revision={final['revision']}", text)
        # The restart on the new name is named after a park, never as a
        # command of this state, which would refuse it.
        commands_block = text.split("<summary>Possible next commands</summary>", 1)[1]
        self.assertNotIn("branch=", commands_block)
        self.assertIn("park first; then, once it is parked, post "
                      f"`/upstreaming refresh revision={final['revision']} branch=main", text)

    def test_the_bound_needs_both_the_count_and_the_time(self):
        import status_ledger
        limit, minutes = 3, 60
        slot = timedelta(minutes=minutes) / limit
        start = T + timedelta(seconds=20)
        rec = self.queued()
        # A burst: one counted read, and nothing written for the others.
        for i in range(8 * limit):
            res = self.tick(rec, dict(self.TRANSIENT), now=start + timedelta(seconds=10 * i))
            rec = self.final(res, rec)
            self.assertEqual(engine.state_key(rec), "queued.idle", i)
            self.assertEqual(rec["budget"]["source_unavailable"], 1)
            self.assertEqual(res.requested_effects, [])
            if i == 0:
                self.assertEqual([e["kind"] for e in self.events(res)],
                                 ["record_source_unavailable"])
                [event] = self.events(res)
                self.assertFalse(status_ledger.notifies(dict(event, sequence=1), "queued.idle",
                                                        "queued.idle"))
            else:
                self.assertEqual(res.staged, [])
                # One deferred entry for the contribution, which names why.
                self.assertEqual([d for d in res.report["deferred"]
                                  if d["contribution"] == rec["contribution_id"]],
                                 [{"contribution": rec["contribution_id"],
                                   "why": "upstream read failed; counted at most once per "
                                          "spacing"}])
        # One counted read per spacing slot reaches the count before the
        # time: still no human.
        for n in range(1, limit):
            res = self.tick(rec, dict(self.TRANSIENT), now=start + n * slot)
            rec = self.final(res, rec)
            self.assertEqual([e["kind"] for e in self.events(res)], ["record_source_unavailable"])
            self.assertEqual(rec["budget"]["source_unavailable"], n + 1)
            self.assertEqual(engine.state_key(rec), "queued.idle")
        self.assertEqual(rec["budget"]["source_unavailable_since"], start.isoformat())
        self.assertEqual(rec["budget"]["source_unavailable_at"],
                         (start + (limit - 1) * slot).isoformat())
        # A read just before the minimum time, in the slot already counted,
        # writes nothing.
        res = self.tick(rec, dict(self.TRANSIENT),
                        now=start + timedelta(minutes=minutes) - timedelta(seconds=30))
        self.assertEqual(res.staged, [])
        # Once the minimum time has passed too, the next failed read waits.
        res = self.tick(rec, dict(self.TRANSIENT), now=start + timedelta(minutes=minutes))
        final = self.final(res, rec)
        self.assertEqual(engine.state_key(final), "planning.waiting_human")
        self.assertEqual(final["pending_human_action"],
                         dict(final["pending_human_action"], kind="base_branch_unavailable",
                              counter="source_reads"))
        [event] = self.events(res)
        self.assertEqual((event["kind"], event["details"]["count"], event["details"]["limit"],
                          event["details"]["repository_failures"]),
                         ("exhaust_source_reads", limit + 1, limit, 0))
        text = self.render(event, final)
        self.assertIn("Cause: the upstream base branch `main` could not be read. Reads have "
                      f"failed since {start.isoformat()}: {limit + 1} counted failures, 0 of "
                      "them for the repository itself.", text)
        self.assertIn("continues on its own once it can be read again", text)
        self.assertNotIn("pending_human_action", text)

    def _entered(self, times, burst=0):
        """The time at which failed reads at ``times`` (each followed by
        ``burst`` more ticks 15 seconds apart) lead to the wait, with the
        record that waits; every tick of a burst writes nothing."""
        rec = self.queued()
        for at in times:
            for b in range(burst + 1):
                res = self.tick(rec, dict(self.TRANSIENT), now=at + timedelta(seconds=15 * b))
                rec = self.final(res, rec)
                if engine.state_key(rec) == "planning.waiting_human":
                    return at + timedelta(seconds=15 * b), rec
                if b:
                    self.assertEqual(res.staged, [], (at, b))
        return None, rec

    def test_late_ticks_still_count_and_bursts_count_once(self):
        # The scheduled tick comes up to a minute late, and each is followed
        # by a burst of event ticks: no tick of a burst is counted, and the
        # wait begins at the first scheduled tick once the minimum time has
        # passed, not later with each late tick.
        import random
        rng = random.Random(5)
        start = T + timedelta(seconds=20)
        times = [start] + [start + timedelta(minutes=5 * k, seconds=rng.uniform(0, 59))
                           for k in range(1, 20)]
        expected = next(t for t in times if t - start >= timedelta(minutes=60))
        self.assertLess(expected - start, timedelta(minutes=66))
        entered, rec = self._entered(times, burst=2)
        self.assertEqual(entered, expected)
        self.assertEqual(rec["pending_human_action"]["counter"], "source_reads")

    def test_jittered_late_and_frequent_ticks_wait_at_the_minimum_time(self):
        # Spacing slots are counted from the first failed read, not from the
        # last counted one: whatever the jitter of a regular tick, the wait
        # begins at the first tick once the minimum time has passed.
        import random
        rng = random.Random(7)
        start = T + timedelta(seconds=20)
        schedules = {
            "every 5 minutes, +-1 minute": [start] + sorted(
                start + timedelta(minutes=5 * k + rng.uniform(-1, 1)) for k in range(1, 30)),
            "every 5 minutes, up to 3 minutes late": [start] + [
                start + timedelta(minutes=5 * k + rng.uniform(0, 3)) for k in range(1, 30)],
            "every 3 minutes": [start + timedelta(minutes=3 * k) for k in range(40)],
        }
        for name, times in schedules.items():
            with self.subTest(name):
                expected = next(t for t in times if t - start >= timedelta(minutes=60))
                entered, rec = self._entered(times)
                self.assertEqual(entered, expected)
                self.assertLess(entered - start, timedelta(minutes=65))
                self.assertEqual(rec["pending_human_action"]["counter"], "source_reads")

    def test_reads_hours_apart_each_count_once(self):
        # GitHub's schedule may skip runs for hours: each read after a long
        # pause counts once, a burst after it still counts once, and the
        # wait begins at the third spaced read, the minimum time having
        # passed long before.
        start = T + timedelta(seconds=20)
        rec = self.queued()
        for n in range(3):
            at = start + timedelta(hours=8 * n)
            rec = self.final(self.tick(rec, dict(self.TRANSIENT), now=at), rec)
            if n < 2:
                self.assertEqual(engine.state_key(rec), "queued.idle", n)
                self.assertEqual(rec["budget"]["source_unavailable"], n + 1)
                res = self.tick(rec, dict(self.TRANSIENT), now=at + timedelta(seconds=30))
                self.assertEqual(res.staged, [])
        self.assertEqual(engine.state_key(rec), "planning.waiting_human")
        self.assertEqual(rec["pending_human_action"]["counter"], "source_reads")

    def test_the_count_alone_reached_after_a_long_run_waits(self):
        rec = self.queued()
        rec["budget"] = dict(rec["budget"], source_unavailable=3,
                             source_unavailable_since=(T - timedelta(hours=2)).isoformat())
        res = self.tick(rec, dict(self.TRANSIENT))
        self.assertEqual([e["kind"] for e in self.events(res)], ["exhaust_source_reads"])
        # The read that exhausts the budget is not held back by the spacing:
        # the last counted read is in the same slot.
        rec["budget"].update(source_unavailable_since=(T - timedelta(minutes=130)).isoformat(),
                             source_unavailable_at=(T - timedelta(minutes=1)).isoformat())
        self.assertFalse(engine.source_read_counted(rec, policy(), T.isoformat()))
        res = self.tick(rec, dict(self.TRANSIENT))
        [event] = self.events(res)
        self.assertEqual((event["kind"], event["details"]["count"]), ("exhaust_source_reads", 3))
        # A count recorded without its start time starts its clock first.
        rec["budget"] = dict(rec["budget"], source_unavailable=3)
        del rec["budget"]["source_unavailable_since"]
        res = self.tick(rec, dict(self.TRANSIENT))
        final = self.final(res, rec)
        self.assertEqual([e["kind"] for e in self.events(res)], ["record_source_unavailable"])
        self.assertEqual(final["budget"]["source_unavailable_since"], T.isoformat())

    def test_a_repository_that_cannot_be_read_waits_with_its_own_cause(self):
        import status_ledger
        spent = self.queued()
        spent["budget"] = dict(spent["budget"], source_unavailable=12,
                               source_unavailable_repository=12,
                               source_unavailable_since=(T - timedelta(hours=2)).isoformat())
        for rec, snapshot, verb, counter, cause, guidance in (
                (spent, dict(self.REPOSITORY), "require_upstream_access", "source_reads",
                 "Cause: the upstream repository `example/proj` could not be read",
                 "a resume reads it again at once"),
                # GitHub names the same repository otherwise: at once, and no
                # failed read is counted for it.
                (self.queued(), dict(self.MOVED), "require_renamed_upstream", None,
                 "Cause: GitHub now names the upstream repository `example/proj` `other/proj`",
                 "while GitHub names it otherwise, a resume only waits again")):
            with self.subTest(cause):
                res = self.tick(rec, snapshot)
                final = self.final(res, rec)
                self.assertEqual(engine.state_key(final), "planning.waiting_human")
                self.assertEqual((final["pending_human_action"]["kind"],
                                  final["pending_human_action"]["counter"]),
                                 ("upstream_unavailable", counter))
                [event] = self.events(res)
                self.assertEqual(event["kind"], verb)
                self.assertTrue(status_ledger.notifies(dict(event, sequence=1), "queued.idle",
                                                       "planning.waiting_human"))
                text = self.render(event, final)
                self.assertIn(cause, text)
                self.assertIn(guidance, text)
                self.assertNotIn("does the same at once", text)
                self.assertIn("park, then abandon", text)
                self.assertNotIn("branch=", text)
                self.assertNotIn("base branch", text.split("Cause:", 1)[1].split("[Workflow", 1)[0])
                # Its resume continues once the repository is read again.
                again = self.tick(final, dict(self.SNAPSHOT), self.resume(final))
                self.assertEqual(again.report["commands"][0]["result"], "accepted")
                self.assertEqual(engine.state_key(self.final(again, final)),
                                 "planning.agent_running")

    def test_a_read_starts_the_count_again(self):
        rec = self.queued()
        for n in range(3):
            rec = self.final(self.tick(rec, dict(self.TRANSIENT), now=T + timedelta(minutes=20 * n)),
                             rec)
        self.assertEqual(rec["budget"]["source_unavailable"], 3)
        res = self.tick(rec, dict(self.SNAPSHOT))
        final = self.final(res, rec)
        self.assertEqual(final["budget"]["source_unavailable"], 0)
        for key in engine.SOURCE_READ_FIELDS:
            self.assertNotIn(key, final["budget"])
        self.assertEqual(final["upstream"]["base_sha"], SHA_B)
        self.assertEqual(len(res.requested_effects), 1)

    def test_a_snapshot_of_another_branch_never_escalates(self):
        rec = self.queued(upstream={"repository_id": None, "full_name": "example/proj",
                                    "base_ref": "release-2", "base_sha": None})
        rec["budget"] = dict(rec["budget"], source_unavailable=12,
                             source_unavailable_since=(T - timedelta(hours=2)).isoformat())
        for snapshot in (dict(self.MISSING), dict(self.TRANSIENT), dict(self.REPOSITORY)):
            with self.subTest(snapshot["error"]):
                res = self.tick(rec, snapshot)
                self.assertEqual(self.events(res), [])
                self.assertEqual(res.staged, [])
                self.assertIn({"contribution": rec["contribution_id"],
                               "why": "upstream snapshot is not of the recorded base branch"},
                              res.report["deferred"])

    def test_a_paused_record_stays_deferred(self):
        rec = self.queued(paused=True, paused_at=NOW, paused_by="human")
        res = self.tick(rec, dict(self.MISSING))
        self.assertEqual(res.staged, [])

    def resume(self, rec, cid=63):
        return [{"issue_number": 12, "comment": comment(
            f"/upstreaming resume revision={rec['revision']} reason=restored", cid=cid)}]

    def test_a_resume_once_the_branch_exists_allocates_the_work(self):
        rec = self.final(self.tick(self.queued(), dict(self.MISSING)), self.queued())
        res = self.tick(rec, dict(self.SNAPSHOT), self.resume(rec))
        self.assertEqual(res.report["commands"][0]["result"], "accepted")
        final = self.final(res, rec)
        self.assertEqual(engine.state_key(final), "planning.agent_running")
        self.assertIsNone(final["pending_human_action"])
        self.assertEqual(final["upstream"]["base_sha"], SHA_B)
        self.assertEqual(len(res.requested_effects), 1)

    def test_the_resume_of_every_phase_leaves_the_wait(self):
        cases = (("provisioning.waiting_human", "resume_provisioning", "provisioning.idle",
                  {"fork": None}),
                 ("development.waiting_human", "resume_development", "development.fix_needed", {}),
                 ("handoff.waiting_human", "resume_submission_review", "handoff.review_due", {}),
                 ("upstream_review.waiting_human", "resume_upstream_revision",
                  "upstream_review.fix_needed", {"upstream_pr": dict(self.PR, base_ref="main")}))
        for state, verb, target, over in cases:
            for kind, counter in (("base_branch_unavailable", None),
                                  ("base_branch_unavailable", "source_reads"),
                                  ("upstream_unavailable", "source_reads")):
                with self.subTest(state=state, kind=kind, counter=counter):
                    rec = self.waiting(state, kind, counter, **over)
                    rec["budget"] = dict(rec["budget"], source_unavailable=12,
                                         source_unavailable_since=NOW)
                    res = self.tick(rec, None, self.resume(rec))
                    self.assertEqual(res.report["commands"][0]["result"], "accepted",
                                     res.report["commands"])
                    [event] = [e for e in self.events(res) if e["kind"] == verb]
                    self.assertEqual(event["details"]["to_state"], target)
                    final = [r for k, r in res.staged if k == "contributions"][0]
                    self.assertIsNone(final["pending_human_action"])
                    if counter:
                        self.assertEqual(final["budget"]["source_unavailable"], 0)
                        self.assertNotIn("source_unavailable_since", final["budget"])

    def test_a_paused_wait_needs_two_resumes(self):
        rec = self.waiting("development.waiting_human", paused=True, paused_at=NOW,
                           paused_by="human")
        api = self.Api()
        self.assertEqual(collect.upstream_snapshots(api, [rec], [], policy()), {})
        self.assertEqual(api.paths, [])
        res = self.tick(rec, None, self.resume(rec))
        first = self.final(res, rec)
        self.assertEqual([e["kind"] for e in self.events(res)], ["resume_pause"])
        self.assertFalse(first["paused"])
        self.assertEqual(engine.state_key(first), "development.waiting_human")
        self.assertEqual(first["pending_human_action"]["kind"], "base_branch_unavailable")
        res = self.tick(first, dict(self.SNAPSHOT), self.resume(first, cid=64))
        self.assertEqual([e["kind"] for e in self.events(res)][:1], ["resume_development"])
        self.assertIsNone(self.final(res, first)["pending_human_action"])

    def test_a_resume_while_the_branch_is_missing_waits_again(self):
        rec = self.final(self.tick(self.queued(), dict(self.MISSING)), self.queued())
        res = self.tick(rec, dict(self.MISSING), self.resume(rec))
        self.assertEqual(res.report["commands"][0]["result"], "accepted")
        final = self.final(res, rec)
        self.assertEqual(engine.state_key(final), "planning.waiting_human")
        self.assertEqual(final["revision"], rec["revision"] + 1)
        self.assertEqual([e["kind"] for e in self.events(res)],
                         ["resume_planning", "require_base_branch"])

    def test_a_resume_after_the_bound_starts_the_count_again(self):
        rec = self.queued()
        rec["budget"] = dict(rec["budget"], source_unavailable=12,
                             source_unavailable_since=(T - timedelta(hours=2)).isoformat())
        rec = self.final(self.tick(rec, dict(self.TRANSIENT)), rec)
        self.assertEqual(rec["pending_human_action"]["counter"], "source_reads")
        res = self.tick(rec, dict(self.SNAPSHOT), self.resume(rec))
        final = self.final(res, rec)
        self.assertEqual(final["budget"]["source_unavailable"], 0)
        # The read of this tick already started the count again, before the
        # resume restarts the counter its wait names.
        resumed = [e for e in self.events(res) if e["kind"] == "resume_planning"]
        self.assertEqual(resumed[0]["details"]["restarted"],
                         {"counter": "source_reads", "count": 0})
        self.assertEqual(engine.state_key(final), "planning.agent_running")

    def test_a_restart_from_parked_on_another_branch(self):
        rec = contribution(phase="parked", activity="idle", parked_reason="base branch renamed",
                           previous_phase="development", revision=4)
        body = "/upstreaming refresh revision=4 branch=trunk reason=renamed"
        res = self.tick(rec, dict(self.SNAPSHOT, base_ref="trunk"),
                        [{"issue_number": 12, "comment": comment(body, cid=64)}])
        self.assertEqual(res.report["commands"][0]["result"], "accepted")
        final = self.final(res, rec)
        self.assertEqual(final["upstream"], {"repository_id": 1, "full_name": "example/proj",
                                             "base_ref": "trunk", "base_sha": SHA_B})
        self.assertEqual(len(res.requested_effects), 1)
        for body, cid in (("/upstreaming refresh revision=4 branch=a..b reason=r", 65),):
            with self.subTest(body=body):
                res = self.tick(rec, dict(self.SNAPSHOT),
                                [{"issue_number": 12, "comment": comment(body, cid=cid)}])
                self.assertEqual(res.report["commands"][0]["result"], "rejected")
                self.assertEqual(res.report["commands"][0]["reason"],
                                 "invalid upstream target branch")

    def test_branch_applies_only_to_a_restart(self):
        rec = contribution(phase="development", activity="waiting_ci")
        res = self.tick(rec, dict(self.SNAPSHOT), [{"issue_number": 12, "comment": comment(
            "/upstreaming refresh revision=0 branch=trunk reason=r", cid=66)}])
        self.assertEqual(res.report["commands"][0]["result"], "rejected")
        self.assertIn("`branch` applies only to a refresh from `parked.idle`",
                      res.report["commands"][0]["reason"])
        with self.assertRaises(commands.CommandError):
            commands.parse("/upstreaming resume revision=0 branch=trunk reason=r")

    def test_the_refresh_help_names_branch(self):
        for text in (commands.PARKED_RESUME, commands.RETIRED["restart"],
                     commands.RETIRED["approve-scope"]):
            self.assertIn("branch=", text)

    def test_a_snapshot_of_another_branch_is_deferred_once(self):
        rec = self.queued(upstream={"repository_id": None, "full_name": "example/proj",
                                    "base_ref": "release-2", "base_sha": None})
        res = self.tick(rec, dict(self.SNAPSHOT))
        self.assertEqual([d for d in res.report["deferred"]
                          if d["contribution"] == rec["contribution_id"]],
                         [{"contribution": rec["contribution_id"],
                           "why": "upstream snapshot is not of the requested base branch"}])
        self.assertEqual(res.staged, [])

    # A base branch renamed while the upstream PR is open

    def renamed_api(self, **over):
        return self.Api(branches={"master": "main", "main": "main"}, pr=self.pull(**over))

    def test_the_collector_verifies_the_pr_that_follows_a_rename(self):
        rec = self.reviewing()
        api = self.renamed_api()
        got = collect.upstream_snapshots(api, [rec], [], policy())
        self.assertEqual(got, {"example/proj": dict(self.RENAMED, pr_base={
            "pr_id": 77, "pr_number": 5, "base_ref": "main", "base_sha": SHA_A})})
        self.assertIn(f"{api.base}/repos/example/proj/compare/{SHA_A}...{SHA_B}?per_page=100",
                      api.paths)
        for name, kwargs, compare in (
                ("closed", {"state": "closed"}, "ahead"),
                ("merged", {"merged": True}, "ahead"),
                ("another base", {"base": {"ref": "release", "repo": {"id": 1}}}, "ahead"),
                ("another repository", {"base": {"ref": "main", "repo": {"id": 9}}}, "ahead"),
                ("base commit not contained", {}, "diverged")):
            with self.subTest(name):
                api = self.renamed_api(**kwargs)
                api.compare = compare
                self.assertEqual(collect.upstream_snapshots(api, [rec], [], policy()),
                                 {"example/proj": dict(self.RENAMED)})
        # A PR that cannot be read is a failed read, never a definitive answer.
        api = self.Api(branches={"master": "main", "main": "main"}, pr=None)
        got = collect.upstream_snapshots(api, [rec], [], policy())["example/proj"]
        self.assertEqual((got.get("missing"), got["scope"], got["ref"]), (None, "branch", "master"))

    def test_the_collector_reads_a_review_that_waits_for_a_rename(self):
        rec = self.reviewing("waiting_human", pending_human_action={
            "kind": "base_branch_unavailable", "requested_at": NOW, "reminded_at": None,
            "escalated_at": None, "counter": None})
        api = self.renamed_api()
        got = collect.upstream_snapshots(api, [rec], [], policy())
        self.assertIn("pr_base", got["example/proj"])

    def test_an_open_pr_follows_the_renamed_base(self):
        import status_ledger
        for activity in ("fix_needed", "waiting_ci"):
            with self.subTest(activity):
                rec = self.reviewing(activity)
                snapshot = collect.upstream_snapshots(self.renamed_api(), [rec], [],
                                                      policy())["example/proj"]
                res = self.tick(rec, snapshot)
                final = self.final(res, rec)
                self.assertEqual(engine.state_key(final), f"upstream_review.{activity}")
                self.assertEqual(final["upstream"], dict(rec["upstream"], base_ref="main"))
                self.assertEqual(final["upstream_pr"], dict(self.PR, base_ref="main"))
                self.assertEqual(final["revision"], rec["revision"])
                [event] = self.events(res)
                self.assertEqual(event["kind"], "follow_renamed_base")
                self.assertTrue(status_ledger.notifies(dict(event, sequence=1),
                                                       f"upstream_review.{activity}",
                                                       f"upstream_review.{activity}"))
                text = self.render(event, final)
                self.assertIn("The upstream renamed the base branch `master` to `main` while the "
                              "upstream pull request is open. The upstream pull request #5 "
                              "targets `main`, so the contribution now follows it.", text)
                # The next tick reads the new name and continues.
                again = self.tick(final, dict(self.SNAPSHOT))
                self.assertEqual([e["kind"] for e in self.events(again)
                                  if e["kind"].startswith(("require", "follow"))], [])

    def test_a_wait_the_rename_caused_ends_without_a_human(self):
        import status_ledger
        rec = self.reviewing()
        stuck = self.final(self.tick(rec, dict(self.RENAMED)), rec)
        self.assertEqual(engine.state_key(stuck), "upstream_review.waiting_human")
        self.assertIn("The upstream pull request is open, so a restart on another branch is "
                      "refused", self.render(self.events(self.tick(rec, dict(self.RENAMED)))[0],
                                             stuck))
        snapshot = collect.upstream_snapshots(self.renamed_api(), [stuck], [],
                                              policy())["example/proj"]
        res = self.tick(stuck, snapshot)
        final = self.final(res, stuck)
        self.assertEqual(engine.state_key(final), "upstream_review.fix_needed")
        self.assertIsNone(final["pending_human_action"])
        self.assertEqual(final["revision"], stuck["revision"] + 1)
        self.assertEqual(final["upstream"]["base_ref"], "main")
        self.assertEqual(final["upstream"]["base_sha"], SHA_A)
        [event] = self.events(res)
        self.assertEqual(event["kind"], "resume_renamed_base")
        self.assertTrue(status_ledger.notifies(dict(event, sequence=1),
                                               "upstream_review.waiting_human",
                                               "upstream_review.fix_needed"))
        self.assertEqual(status_ledger.NOTICE_VERBS,
                         {"follow_renamed_base", "resume_renamed_base", "resume_upstream_read",
                          "resume_renamed_upstream"})

    def test_a_pr_that_does_not_follow_the_rename_still_waits(self):
        rec = self.reviewing()
        snapshot = dict(self.RENAMED, pr_base={"pr_id": 999, "pr_number": 5, "base_ref": "main",
                                               "base_sha": SHA_A})
        res = self.tick(rec, snapshot)
        self.assertEqual([e["kind"] for e in self.events(res)], ["require_base_branch"])
        self.assertEqual(engine.state_key(self.final(res, rec)), "upstream_review.waiting_human")

    # A paused record, an open operation, and another pending action keep
    # the rename for later.

    def test_a_paused_record_follows_the_rename_once_resumed(self):
        rec = self.reviewing("waiting_ci", paused=True, paused_at=NOW, paused_by="human")
        snapshot = collect.upstream_snapshots(self.renamed_api(), [rec], [],
                                              policy())["example/proj"]
        self.assertIn("pr_base", snapshot)
        res = self.tick(rec, snapshot)
        self.assertEqual(self.events(res), [])
        self.assertEqual(self.final(res, rec)["upstream"]["base_ref"], "master")
        # The resume clears the flag first; the rename is followed in the same tick.
        res = self.tick(rec, snapshot, self.resume(rec))
        self.assertEqual([e["kind"] for e in self.events(res)],
                         ["resume_pause", "follow_renamed_base"])
        final = self.final(res, rec)
        self.assertFalse(final["paused"])
        self.assertEqual((final["upstream"]["base_ref"], final["upstream_pr"]["base_ref"]),
                         ("main", "main"))

    def test_an_open_operation_holds_the_rename(self):
        rec = self.reviewing("waiting_ci", active_operation_id="op-" + "d" * 24)
        snapshot = collect.upstream_snapshots(self.renamed_api(), [rec], [],
                                              policy())["example/proj"]
        res = self.tick(rec, snapshot)
        self.assertNotIn("follow_renamed_base", [e["kind"] for e in self.events(res)])
        self.assertEqual(self.final(res, rec)["upstream"]["base_ref"], "master")
        self.assertNotIn("base_branch_renamed", res.report)

    def test_a_failed_comparison_is_a_failed_read_through_the_tick(self):
        class NoCompare(self.Api):
            def _request(self, url):
                raise collect.CollectError(f"GET {url}: HTTP 502")

        rec = self.reviewing("fix_needed")
        api = NoCompare(branches={"master": "main", "main": "main"}, pr=self.pull())
        snapshot = collect.upstream_snapshots(api, [rec], [], policy())["example/proj"]
        self.assertEqual((snapshot.get("missing"), snapshot.get("pr_base"), snapshot["scope"]),
                         (None, None, "branch"))
        res = self.tick(rec, snapshot)
        self.assertEqual([e["kind"] for e in self.events(res)], ["record_source_unavailable"])
        final = self.final(res, rec)
        self.assertEqual(engine.state_key(final), "upstream_review.fix_needed")
        self.assertEqual(final["upstream"], rec["upstream"])
        self.assertEqual(final["budget"]["source_unavailable"], 1)

    def test_a_wait_with_another_pending_action_does_not_follow_the_rename(self):
        rec = self.reviewing("waiting_human", pending_human_action={
            "kind": "dispatch_failed", "requested_at": NOW, "reminded_at": None,
            "escalated_at": None, "counter": "dispatch_recovery"})
        snapshot = collect.upstream_snapshots(self.renamed_api(), [rec], [],
                                              policy())["example/proj"]
        self.assertIn("pr_base", snapshot)
        res = self.tick(rec, snapshot)
        self.assertEqual(self.events(res), [])
        final = self.final(res, rec)
        self.assertEqual(engine.state_key(final), "upstream_review.waiting_human")
        self.assertEqual(final["pending_human_action"]["kind"], "dispatch_failed")
        self.assertEqual(final["upstream"]["base_ref"], "master")

    def test_the_handoff_wait_with_a_recorded_pr_offers_no_restart(self):
        rec = contribution(phase="handoff", activity="review_due",
                           upstream_pr=dict(self.PR, base_ref="main"))
        res = self.tick(rec, dict(self.MISSING))
        final = self.final(res, rec)
        self.assertEqual(engine.state_key(final), "handoff.waiting_human")
        [event] = self.events(res)
        text = self.render(event, final)
        self.assertIn("Once the branch can be read again, resume. The upstream pull request is "
                      "open, so a restart on another branch is refused", text)
        self.assertNotIn("park first", text)
        self.assertNotIn("branch=", text)

    # A renamed repository, and the cause of most of the run

    def test_a_renamed_repository_of_the_same_id_waits_at_once(self):
        rec = contribution(phase="development", activity="fix_needed")
        res = self.tick(rec, dict(self.MOVED))
        final = self.final(res, rec)
        self.assertEqual(engine.state_key(final), "development.waiting_human")
        self.assertEqual(final["pending_human_action"]["kind"], "upstream_unavailable")
        self.assertIsNone(final["pending_human_action"]["counter"])
        [event] = self.events(res)
        self.assertEqual((event["kind"], event["details"]["renamed_repository"]),
                         ("require_renamed_upstream", "other/proj"))
        self.assertNotIn("count", event["details"])
        self.assertNotIn("restarted", event["details"])
        # Another repository id under the old name is no definitive answer.
        res = self.tick(rec, dict(self.MOVED, renamed_repository_id=9))
        self.assertEqual([e["kind"] for e in self.events(res)], ["record_source_unavailable"])

    def test_the_exhaustion_names_the_cause_of_most_reads(self):
        for repository_failures, snapshot, verb in (
                (3, self.REPOSITORY, "exhaust_source_reads"),
                (10, self.TRANSIENT, "require_upstream_access"),
                (6, self.REPOSITORY, "require_upstream_access"),
                (6, self.TRANSIENT, "exhaust_source_reads")):
            with self.subTest(repository_failures=repository_failures, snapshot=snapshot["scope"]):
                rec = self.queued()
                rec["budget"] = dict(rec["budget"], source_unavailable=12,
                                     source_unavailable_repository=repository_failures,
                                     source_unavailable_since=(T - timedelta(hours=2)).isoformat())
                res = self.tick(rec, dict(snapshot))
                [event] = self.events(res)
                current = 1 if snapshot["scope"] == "repository" else 0
                self.assertEqual((event["kind"], event["details"]["count"],
                                  event["details"]["repository_failures"]),
                                 (verb, 13, repository_failures + current))

    def test_a_counted_read_records_its_cause(self):
        rec = self.queued()
        rec = self.final(self.tick(rec, dict(self.REPOSITORY)), rec)
        rec = self.final(self.tick(rec, dict(self.TRANSIENT), now=T + timedelta(minutes=20)), rec)
        self.assertEqual((rec["budget"]["source_unavailable"],
                          rec["budget"]["source_unavailable_repository"]), (2, 1))

    # A wait that failed reads caused heals itself

    def test_a_wait_failed_reads_caused_ends_once_the_upstream_is_read(self):
        import status_ledger
        cases = (("planning.waiting_human", "planning.idle", {}),
                 ("provisioning.waiting_human", "provisioning.idle", {"fork": None}),
                 ("development.waiting_human", "development.fix_needed", {}),
                 ("handoff.waiting_human", "handoff.review_due", {}),
                 ("upstream_review.waiting_human", "upstream_review.fix_needed",
                  {"upstream_pr": dict(self.PR, base_ref="main")}))
        for state, target, over in cases:
            for kind in ("base_branch_unavailable", "upstream_unavailable"):
                with self.subTest(state=state, kind=kind):
                    rec = self.waiting(state, kind, "source_reads", **over)
                    rec["budget"] = dict(rec["budget"], source_unavailable=12,
                                         source_unavailable_since=NOW)
                    api = self.Api()
                    snapshot = collect.upstream_snapshots(api, [rec], [], policy())["example/proj"]
                    self.assertTrue(api.paths)
                    res = self.tick(rec, snapshot)
                    [event] = [e for e in self.events(res) if e["kind"] == "resume_upstream_read"]
                    self.assertEqual(event["details"]["to_state"], target)
                    self.assertEqual(event["details"]["restarted"],
                                     {"counter": "source_reads", "count": 0})
                    resumed = [r for k, r in res.staged if k == "contributions"
                               and r["revision"] == rec["revision"] + 1][0]
                    self.assertIsNone(resumed["pending_human_action"])
                    self.assertTrue(status_ledger.notifies(dict(event, sequence=1), state, target))
                    text = self.render(event, resumed)
                    self.assertIn("can be read again, so the wait for it ends without a command",
                                  text)

    def test_the_wait_a_renamed_repository_caused_ends_once_it_is_read_again(self):
        import status_ledger
        rec = contribution(phase="development", activity="fix_needed")
        wait = self.final(self.tick(rec, dict(self.MOVED)), rec)
        self.assertEqual((wait["pending_human_action"]["kind"],
                          wait["pending_human_action"]["counter"]), ("upstream_unavailable", None))
        # The reads row does not apply: reads did not cause this wait.
        self.assertTrue(engine.refusal(wait, "resume_upstream_read", policy=policy(), now=NOW,
                                       facts={"upstream_read_again"}, open_operations=()))
        # Still named otherwise: it is read, and the wait stays.
        api = self.Api(full_name="other/proj")
        snapshot = collect.upstream_snapshots(api, [wait], [], policy())["example/proj"]
        self.assertTrue(api.paths)
        self.assertEqual(snapshot["renamed_repository"], "other/proj")
        res = self.tick(wait, snapshot)
        self.assertEqual(self.events(res), [])
        # A resume while it is still named otherwise waits again at once.
        res = self.tick(wait, dict(snapshot), self.resume(wait))
        self.assertEqual([e["kind"] for e in self.events(res)],
                         ["resume_development", "require_renamed_upstream"])
        # Another repository under the recorded name does not end it.
        res = self.tick(wait, dict(self.SNAPSHOT, repository_id=9))
        self.assertNotIn("resume_renamed_upstream", [e["kind"] for e in self.events(res)])
        # GitHub answers for the same repository under its recorded name.
        api = self.Api()
        snapshot = collect.upstream_snapshots(api, [wait], [], policy())["example/proj"]
        res = self.tick(wait, snapshot)
        [event] = [e for e in self.events(res) if e["kind"] == "resume_renamed_upstream"]
        self.assertEqual(event["details"]["to_state"], "development.fix_needed")
        resumed = [r for k, r in res.staged if k == "contributions"
                   and r["revision"] == wait["revision"] + 1][0]
        self.assertIsNone(resumed["pending_human_action"])
        self.assertTrue(status_ledger.notifies(dict(event, sequence=1),
                                               "development.waiting_human",
                                               "development.fix_needed"))
        self.assertIn("can be read again under its recorded name", self.render(event, resumed))

    def _rename_waits(self):
        """Every state that reads the upstream base branch, with a record in
        it that GitHub answers for under another name, and that answer."""
        for state in lifecycle.BASE_BRANCH_WAITS:
            phase, activity = state.split(".")
            if state == "queued.idle":
                rec = self.queued()
            elif phase == "upstream_review":
                rec = self.reviewing(activity)
            else:
                rec = contribution(phase=phase, activity=activity)
            yield state, rec, dict(self.MOVED, ref=rec["upstream"]["base_ref"])

    def test_a_human_resume_of_the_rename_wait_waits_again_in_every_state(self):
        # The entry of the wait offers a resume: in every wait it is
        # accepted, provisioning included, and while GitHub still names the
        # repository otherwise the contribution waits again at once.
        for state, rec, moved in self._rename_waits():
            with self.subTest(state):
                wait = self.final(self.tick(rec, moved), rec)
                self.assertEqual(engine.state_key(wait), lifecycle.BASE_BRANCH_WAITS[state])
                self.assertEqual((wait["pending_human_action"]["kind"],
                                  wait["pending_human_action"]["counter"]),
                                 ("upstream_unavailable", None))
                self.assertIn(f"/upstreaming resume revision={wait['revision']}",
                              self.render(self.events(self.tick(rec, moved))[-1], wait))
                res = self.tick(wait, dict(moved), self.resume(wait))
                self.assertEqual(res.report["commands"][0]["result"], "accepted",
                                 res.report["commands"])
                kinds = [e["kind"] for e in self.events(res)]
                self.assertEqual(len(kinds), 2, kinds)
                self.assertEqual(kinds[1], "require_renamed_upstream")
                again = self.final(res, wait)
                self.assertEqual(engine.state_key(again), engine.state_key(wait))
                self.assertEqual((again["pending_human_action"]["kind"],
                                  again["pending_human_action"]["counter"]),
                                 ("upstream_unavailable", None))

    def test_the_rename_wait_of_a_request_heals_only_on_the_same_repository(self):
        # A request not bound yet records the id GitHub answered for, so that
        # another repository created later under the recorded name is never
        # bound in its place.
        rec = self.queued()
        wait = self.final(self.tick(rec, dict(self.MOVED)), rec)
        self.assertEqual(wait["upstream"]["repository_id"], 1)
        [event] = self.events(self.tick(rec, dict(self.MOVED)))
        self.assertEqual(event["details"]["repository_id"], 1)
        res = self.tick(wait, dict(self.SNAPSHOT, repository_id=9))
        kinds = [e["kind"] for e in self.events(res)]
        self.assertNotIn("resume_renamed_upstream", kinds)
        self.assertEqual(self.final(res, wait)["upstream"]["repository_id"], 1)
        self.assertIsNone(self.final(res, wait)["upstream"]["base_sha"])
        res = self.tick(wait, dict(self.SNAPSHOT))
        self.assertIn("resume_renamed_upstream", [e["kind"] for e in self.events(res)])
        healed = self.final(res, wait)
        self.assertEqual((healed["upstream"]["repository_id"], healed["upstream"]["base_sha"]),
                         (1, SHA_B))
        # Without an id from GitHub, nothing is bound and the wait stays
        # until a human resumes it; its entry says so.
        unknown = self.final(self.tick(rec, dict(self.MOVED, renamed_repository_id=None)), rec)
        self.assertEqual(engine.state_key(unknown), "planning.waiting_human")
        self.assertIsNone(unknown["upstream"]["repository_id"])
        [event] = self.events(self.tick(rec, dict(self.MOVED, renamed_repository_id=None)))
        self.assertIn("does not continue on its own", self.render(event, unknown))
        for snapshot in (dict(self.SNAPSHOT), dict(self.SNAPSHOT, repository_id=9),
                         dict(self.MISSING)):
            res = self.tick(unknown, snapshot)
            self.assertEqual(res.staged, [], snapshot)
            if snapshot.get("error"):
                continue
            self.assertIn({"contribution": unknown["contribution_id"],
                           "why": "the renamed upstream repository has no recorded id; a "
                                  "resume continues"}, res.report["deferred"])
        res = self.tick(unknown, dict(self.SNAPSHOT), self.resume(unknown))
        self.assertEqual(res.report["commands"][0]["result"], "accepted")
        self.assertEqual(engine.state_key(self.final(res, unknown)), "planning.idle")

    def test_a_renamed_repository_back_without_its_branch_waits_for_the_branch(self):
        import status_ledger
        rec = contribution(phase="development", activity="fix_needed")
        wait = self.final(self.tick(rec, dict(self.MOVED)), rec)
        api = self.Api(branches=())
        missing = collect.upstream_snapshots(api, [wait], [], policy())["example/proj"]
        self.assertEqual(missing, dict(self.MISSING))
        renamed = dict(self.MISSING, error="upstream base branch 'main' was renamed",
                       renamed_to="trunk")
        for snapshot, cause in (
                (missing, "Cause: the upstream says the base branch `main` does not exist."),
                (renamed, "Cause: the upstream renamed the base branch `main` to `trunk`.")):
            with self.subTest(cause):
                res = self.tick(wait, dict(snapshot))
                [event] = self.events(res)
                self.assertEqual(event["kind"], "require_base_branch")
                final = self.final(res, wait)
                self.assertEqual(engine.state_key(final), "development.waiting_human")
                self.assertEqual((final["pending_human_action"]["kind"],
                                  final["pending_human_action"]["counter"]),
                                 ("base_branch_unavailable", None))
                self.assertEqual(final["revision"], wait["revision"])
                self.assertTrue(status_ledger.notifies(dict(event, sequence=1),
                                                       "development.waiting_human",
                                                       "development.waiting_human"))
                text = self.render(event, final)
                self.assertIn(cause, text)
                self.assertIn("Once the branch can be read again, resume.", text)
                # The usual wait of the base branch: it is not read again
                # until a resume is posted.
                api = self.Api()
                self.assertEqual(collect.upstream_snapshots(api, [final], [], policy()), {})
        # Another repository under the recorded name says nothing about it.
        res = self.tick(wait, dict(self.MISSING, repository_id=9))
        self.assertNotIn("require_base_branch", [e["kind"] for e in self.events(res)])

    def test_the_rename_wait_stays_while_paused_or_an_operation_is_open(self):
        rec = contribution(phase="development", activity="fix_needed")
        wait = self.final(self.tick(rec, dict(self.MOVED)), rec)
        paused = dict(wait, paused=True, paused_at=NOW, paused_by="human",
                      paused_reason="hold")
        busy = dict(wait, active_operation_id="op-1")
        for name, held in (("paused", paused), ("open operation", busy)):
            for snapshot in (dict(self.SNAPSHOT), dict(self.MISSING), dict(self.MOVED)):
                with self.subTest(name, snapshot=snapshot.get("error", "read")):
                    res = self.tick(held, snapshot)
                    kinds = [e["kind"] for e in self.events(res)]
                    self.assertFalse({"resume_renamed_upstream", "require_base_branch",
                                      "require_renamed_upstream"} & set(kinds), kinds)
                    final = self.final(res, held)
                    self.assertEqual(engine.state_key(final), "development.waiting_human")
                    self.assertEqual(final["pending_human_action"]["kind"],
                                     "upstream_unavailable")

    def test_a_human_resume_with_a_readable_snapshot_is_taken_as_posted(self):
        # The resume is applied before the automatic end, which then has
        # nothing to end: one transition, the one the human posted.
        for state, rec, moved in self._rename_waits():
            with self.subTest(state):
                wait = self.final(self.tick(rec, moved), rec)
                snapshot = dict(self.SNAPSHOT, base_ref=wait["upstream"]["base_ref"] or "main")
                res = self.tick(wait, snapshot, self.resume(wait))
                self.assertEqual(res.report["commands"][0]["result"], "accepted",
                                 res.report["commands"])
                kinds = [e["kind"] for e in self.events(res)]
                self.assertNotIn("resume_renamed_upstream", kinds)
                self.assertNotIn("require_renamed_upstream", kinds)
                self.assertTrue(kinds[0].startswith("resume_"), kinds)
                self.assertIsNone(self.final(res, wait)["pending_human_action"])

    def test_a_queued_request_on_the_default_branch_heals_through_the_tick(self):
        rec = self.queued(upstream={"repository_id": None, "full_name": "example/proj",
                                    "base_ref": None, "base_sha": None})
        rec["budget"] = dict(rec["budget"], source_unavailable=12,
                             source_unavailable_since=(T - timedelta(hours=2)).isoformat())
        failed = collect.upstream_snapshots(self.Api(fail="HTTP 503"), [rec], [],
                                            policy())["example/proj"]
        wait = self.final(self.tick(rec, failed), rec)
        self.assertEqual(engine.state_key(wait), "planning.waiting_human")
        self.assertEqual(wait["pending_human_action"]["counter"], "source_reads")
        self.assertEqual(wait["upstream"], rec["upstream"])
        api = self.Api()
        snapshot = collect.upstream_snapshots(api, [wait], [], policy())["example/proj"]
        self.assertTrue(api.paths)
        res = self.tick(wait, snapshot, now=T + timedelta(minutes=1))
        [event] = [e for e in self.events(res) if e["kind"] == "resume_upstream_read"]
        self.assertEqual(event["details"]["to_state"], "planning.idle")
        final = self.final(res, wait)
        self.assertIsNone(final["pending_human_action"])
        self.assertEqual(final["upstream"], dict(rec["upstream"], repository_id=1,
                                                 base_ref="main", base_sha=SHA_B))
        self.assertEqual(final["budget"]["source_unavailable"], 0)

    def test_a_paused_reads_wait_ends_once_the_pause_is_resumed(self):
        rec = self.waiting("development.waiting_human", "base_branch_unavailable",
                           "source_reads", paused=True, paused_at=NOW, paused_by="human")
        api = self.Api()
        snapshot = collect.upstream_snapshots(api, [rec], [], policy())["example/proj"]
        self.assertTrue(api.paths)
        res = self.tick(rec, snapshot)
        self.assertEqual(self.events(res), [])
        res = self.tick(rec, dict(snapshot), self.resume(rec))
        self.assertEqual([e["kind"] for e in self.events(res)][:2],
                         ["resume_pause", "resume_upstream_read"])
        final = [r for k, r in res.staged if k == "contributions"][-1]
        self.assertFalse(final["paused"])
        self.assertIsNone(final["pending_human_action"])

    def test_a_wait_the_upstream_answer_caused_needs_a_resume(self):
        for counter, over, snapshot in (
                (None, {}, dict(self.SNAPSHOT)),
                ("source_reads", {"paused": True, "paused_at": NOW, "paused_by": "human"},
                 dict(self.SNAPSHOT)),
                ("source_reads", {}, dict(self.SNAPSHOT, base_ref="trunk")),
                ("source_reads", {}, dict(self.TRANSIENT)),
                ("source_reads", {}, dict(self.MOVED))):
            with self.subTest(counter=counter, over=over, snapshot=snapshot):
                rec = self.waiting("development.waiting_human", "base_branch_unavailable",
                                   counter, **over)
                res = self.tick(rec, snapshot)
                self.assertNotIn("resume_upstream_read", [e["kind"] for e in self.events(res)])
                self.assertEqual(engine.state_key(self.final(res, rec)),
                                 "development.waiting_human")

    def test_a_posted_resume_in_the_same_tick_is_taken_as_posted(self):
        rec = self.waiting("development.waiting_human", "base_branch_unavailable", "source_reads")
        res = self.tick(rec, dict(self.SNAPSHOT), self.resume(rec))
        self.assertEqual(res.report["commands"][0]["result"], "accepted")
        kinds = [e["kind"] for e in self.events(res)]
        self.assertIn("resume_development", kinds)
        self.assertNotIn("resume_upstream_read", kinds)

    # A base renamed before the upstream PR is opened

    def locked(self):
        """A record in the locked wait for the upstream PR, on `master`."""
        return contribution(
            phase="handoff", activity="waiting_upstream_pr", clean_head_sha=SHA_A,
            upstream={"repository_id": 1, "full_name": "example/proj", "base_ref": "master",
                      "base_sha": SHA_C},
            handoff_request={"command_id": "op-" + "e" * 24, "head": SHA_A, "revision": 0},
            pending_human_action={"kind": "open_upstream_pr", "requested_at": NOW,
                                  "reminded_at": None, "escalated_at": None, "counter": None})

    def opened(self, base="main"):
        return {"id": 77, "number": 5, "state": "open", "merged_at": None,
                "html_url": "https://github.com/example/proj/pull/5",
                "base": {"ref": base, "repo": {"id": 1}},
                "head": {"ref": "riscv64", "sha": SHA_A, "repo": {"id": 2}},
                "user": {"id": OWNER["id"], "login": OWNER["login"], "type": "User"}}

    def observe_locked(self, api, rec):
        import status_ledger
        obs, entry = observe_ci.Observation(), {}
        observe_ci._observe_upstream_pr(api, obs, entry, rec, SHA_A, policy=policy(), now=NOW,
                                        audited={})
        events = [r for k, r in obs.staged if k == "events"]
        records = [r for k, r in obs.staged if k == "contributions"]
        return events, (records[-1] if records else rec), entry, status_ledger

    class Pulls(Api):
        def __init__(self, pulls, **kwargs):
            super().__init__(**kwargs)
            self.pulls = pulls

        def get(self, path):
            if "/pulls?" in path:
                self.paths.append(path)
                return self.pulls
            return super().get(path)

    def test_a_pr_on_a_base_renamed_before_it_was_opened_is_verified(self):
        rec = self.locked()
        api = self.Pulls([self.opened()], branches={"master": "main", "main": "main"})
        events, final, entry, status_ledger = self.observe_locked(api, rec)
        [event] = events
        self.assertEqual(event["kind"], "verify_upstream_pr")
        self.assertEqual(engine.state_key(final), "upstream_review.waiting_ci")
        self.assertEqual(final["upstream"], dict(rec["upstream"], base_ref="main"))
        self.assertEqual(final["upstream_pr"]["base_ref"], "main")
        self.assertEqual((event["details"]["base_ref"], event["details"]["renamed_to"]),
                         ("master", "main"))
        self.assertEqual(entry["base_branch_renamed"], "main")
        self.assertIn(f"{api.base}/repos/example/proj/compare/{SHA_C}...{SHA_B}?per_page=100",
                      api.paths)
        self.assertTrue(status_ledger.notifies(dict(event, sequence=1),
                                               "handoff.waiting_upstream_pr",
                                               "upstream_review.waiting_ci"))
        self.assertIn("The upstream renamed the base branch `master` to `main` before the pull "
                      "request was opened.", self.render(event, final))

    def test_a_candidate_on_another_branch_does_not_make_the_rename_ambiguous(self):
        pulls = [dict(self.opened("trunk"), id=78, number=6), self.opened()]
        api = self.Pulls(pulls, branches={"master": "main", "main": "main", "trunk": "trunk"})
        events, final, entry, _ledger = self.observe_locked(api, self.locked())
        self.assertEqual([e["kind"] for e in events], ["verify_upstream_pr"])
        self.assertEqual(final["upstream_pr"]["number"], 5)
        self.assertEqual(final["upstream"]["base_ref"], "main")
        self.assertEqual(entry["base_branch_renamed"], "main")

    def test_a_pr_on_another_base_stays_unverified(self):
        for name, pulls, branches, compare in (
                ("no rename", [self.opened()], {"master": "master", "main": "main"}, "ahead"),
                ("renamed elsewhere", [self.opened()], {"master": "trunk", "main": "main"},
                 "ahead"),
                ("base commit not contained", [self.opened()],
                 {"master": "main", "main": "main"}, "diverged"),
                ("two candidates", [self.opened(), dict(self.opened(), id=78, number=6)],
                 {"master": "main", "main": "main"}, "ahead")):
            with self.subTest(name):
                rec = self.locked()
                api = self.Pulls(pulls, branches=branches, compare=compare)
                events, final, entry, _ledger = self.observe_locked(api, rec)
                self.assertEqual([e["kind"] for e in events], ["record_audit_observation"])
                self.assertEqual(engine.state_key(final), "handoff.waiting_upstream_pr")
                self.assertEqual(final["upstream"]["base_ref"], "master")
        # The PR on the frozen base is verified as it is, and the base is kept.
        api = self.Pulls([self.opened("master")], branches={"master": "master"})
        events, final, _entry, _ledger = self.observe_locked(api, self.locked())
        self.assertEqual([e["kind"] for e in events], ["verify_upstream_pr"])
        self.assertEqual(final["upstream"]["base_ref"], "master")
        self.assertNotIn("renamed_to", events[0]["details"])

    # The entries name no internal identifier

    def test_the_entries_about_an_unavailable_upstream_are_plain_words(self):
        import re

        import status_ledger
        entries = []

        def collect_entries(res, rec):
            final = self.final(res, rec)
            for event in self.events(res):
                if status_ledger.plain_message(dict(event, sequence=1), final) is not None:
                    entries.append((event, final))

        spent = {"source_unavailable": 12, "source_unavailable_repository": 1,
                 "source_unavailable_since": (T - timedelta(hours=2)).isoformat()}
        for rec, snapshot in (
                (self.queued(), self.MISSING),
                (contribution(phase="development", activity="fix_needed",
                              upstream={"repository_id": 1, "full_name": "example/proj",
                                        "base_ref": "master", "base_sha": SHA_B}), self.RENAMED),
                (self.queued(budget=dict(self.queued()["budget"], **spent)), self.TRANSIENT),
                (self.queued(budget=dict(self.queued()["budget"], **dict(
                    spent, source_unavailable_repository=12))), self.REPOSITORY),
                (self.queued(), self.MOVED),
                (self.reviewing(), self.RENAMED),
                (contribution(phase="handoff", activity="review_due",
                              upstream_pr=dict(self.PR, base_ref="main")), self.MISSING)):
            collect_entries(self.tick(rec, dict(snapshot)), rec)
        for kind, counter in (("base_branch_unavailable", None),
                              ("base_branch_unavailable", "source_reads"),
                              ("upstream_unavailable", "source_reads"),
                              ("upstream_unavailable", None)):
            rec = self.waiting("development.waiting_human", kind, counter)
            rec["pending_human_action"]["requested_at"] = (T - timedelta(days=30)).isoformat()
            collect_entries(self.tick(rec, None), rec)
            if counter or kind == "upstream_unavailable":
                collect_entries(self.tick(rec, dict(self.SNAPSHOT)), rec)
        for activity in ("fix_needed", "waiting_ci"):
            rec = self.reviewing(activity)
            snapshot = collect.upstream_snapshots(self.renamed_api(), [rec], [],
                                                  policy())["example/proj"]
            collect_entries(self.tick(rec, snapshot), rec)
        stuck = self.final(self.tick(self.reviewing(), dict(self.RENAMED)), self.reviewing())
        snapshot = collect.upstream_snapshots(self.renamed_api(), [stuck], [],
                                              policy())["example/proj"]
        collect_entries(self.tick(stuck, snapshot), stuck)
        # The upstream PR verified on a base renamed before it was opened.
        api = self.Pulls([self.opened()], branches={"master": "main", "main": "main"})
        events, final, _entry, _ledger = self.observe_locked(api, self.locked())
        entries.extend((event, final) for event in events
                       if status_ledger.plain_message(dict(event, sequence=1), final) is not None)
        kinds = {e["kind"] for e, _r in entries}
        self.assertEqual(kinds, {"require_base_branch", "exhaust_source_reads",
                                 "require_upstream_access", "require_renamed_upstream",
                                 "record_reminder", "resume_upstream_read",
                                 "resume_renamed_upstream", "follow_renamed_base",
                                 "resume_renamed_base", "verify_upstream_pr"})
        command_words = set(commands.GRAMMAR)
        identifiers = ({v for v in engine.table_verbs() if v not in command_words}
                       | set(lifecycle.BUDGETS) | set(lifecycle.LIFECYCLE)
                       | {lifecycle.BASE_BRANCH_UNAVAILABLE, lifecycle.UPSTREAM_UNAVAILABLE,
                          "pending_human_action", "upstream_pr", "base_ref", "base_sha",
                          "repository_id", "source_unavailable", "budget", "counter",
                          "retries"})
        for event, record in entries:
            with self.subTest(kind=event["kind"], state=event["details"]["from_state"]):
                text = self.render(event, record)
                visible = text.split("<!-- upstreaming-ledger:", 1)[0]
                for name in identifiers:
                    self.assertIsNone(re.search(rf"(?<![\w/-]){re.escape(name)}(?![\w-])",
                                                visible), (name, visible))
                self.assertNotIn("\u2014", text)


class _Stop(Exception):
    pass


class TestEntryPointsPassCommandsToTheCollector(unittest.TestCase):
    """`controller observe` and `reconcile` hand the command comments and
    the command ledger they read to the upstream snapshot collector, which
    reads a parked upstream for a refresh the controller can still take."""

    COMMENTS = [{"issue_number": 12, "comment": comment(
        "/upstreaming refresh revision=4 reason=r", cid=51)}]

    def setUp(self):
        import tempfile

        import state
        from common import canonical_json
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        rec = contribution(phase="parked", activity="idle", generation=1, revision=4,
                           parked_reason="old attempt", previous_phase="development")
        receipt = {"actor": dict(OWNER), "args": {"reason": "r", "revision": "3"},
                   "command_id": "comment-50", "command_version": 1, "comment_id": 50,
                   "content_digest": "0" * 64, "contribution_id": rec["contribution_id"],
                   "issue_number": 12, "reason": None, "received_at": NOW,
                   "result": "accepted", "revision": 3, "schema_version": 1, "verb": "refresh"}
        for kind, value in (("contributions", rec), ("commands", receipt)):
            path = self.root / state.record_path(kind, value)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canonical_json(value))

    def tearDown(self):
        self.tmp.cleanup()

    def run_entry(self, entry, args, load_policy):
        calls = []

        def snapshots(*a, **k):
            calls.append((a, k))
            raise _Stop()

        with mock.patch(load_policy, return_value=policy()), \
                mock.patch("collect.Api"), \
                mock.patch("collect.intake_issues", return_value=[]), \
                mock.patch("collect.command_comments", return_value=list(self.COMMENTS)), \
                mock.patch("collect.tracking_issues", return_value={}), \
                mock.patch("collect.usage", side_effect=RuntimeError("offline")), \
                mock.patch("collect.upstream_snapshots", side_effect=snapshots):
            with self.assertRaises(_Stop):
                entry(args)
        self.assertEqual(len(calls), 1)
        a, k = calls[0]
        self.assertEqual(a[4], self.COMMENTS)
        self.assertEqual(set(a[5]), {50})

    def test_observe(self):
        from types import SimpleNamespace
        self.run_entry(controller.cmd_observe,
                       SimpleNamespace(root=str(self.root), offline=False),
                       "controller.load_policy")

    def test_reconcile(self):
        from types import SimpleNamespace

        import reconcile
        self.run_entry(reconcile.cmd_reconcile,
                       SimpleNamespace(root=str(self.root), report=None, publications=None,
                                       provisions=None, development_prs=None, since=None,
                                       offline=False, gh_aw=None),
                       "reconcile.load_policy")


if __name__ == "__main__":
    unittest.main()


class TestPauseOnFreshRequest(unittest.TestCase):
    def test_pause_posted_with_the_request_stops_the_first_dispatch(self):
        from tests.test_commands_intake import issue
        iss = issue(actor={"id": 1001, "login": "owner"}, number=4935)
        res = controller.tick(contributions={}, operations={}, consumed={}, projects={}, policy=policy(),
                              inputs=controller.Inputs(now=T, issues=[iss], pressure=capacity.Pressure(ok=True),
                                                       comments=[{"issue_number": 4935, "comment": comment(
                                                           '/upstreaming pause reason="test"', cid=5)}]))
        self.assertEqual(res.report["commands"][0]["result"], "accepted")
        self.assertEqual(res.requested_effects, [])
        self.assertTrue([r for k, r in res.staged if k == "contributions"][-1]["paused"])
