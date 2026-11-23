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
import observe_ci
import recovery
import release
import triggers
from tests.helpers import (
    NOW,
    OWNER,
    SHA_A,
    SHA_B,
    apply_command,
    ci_record,
    comment,
    contribution,
    policy,
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
             mock.patch("observe_ci.collect.ci_snapshot", return_value=snapshot):
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

    def test_pending_feedback_keeps_green_ci_in_waiting_ci(self):
        """Nomination requires that no actionable feedback is pending."""
        rec = dict(self.record(), pending_feedback_evidence_ids=["review-1"])
        observation = self.observe(rec, self.snapshot(pending=False))
        final = self.final(observation, rec)
        self.assertEqual(engine.state_key(final), "development.waiting_ci")
        self.assertIsNone(final["last_ci_evidence_id"])
        self.assertIn("actionable feedback is pending", observation.report[0]["handoff_deferred"])
        self.assertEqual([kind for kind, _ in observation.staged if kind == "events"], [])

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
                           "disposition=not-launched evidence=ev-9")
        with self.assertRaises(recovery.RecoveryError):
            recovery.resolve(op, c, actor=OWNER, command_id="x", evidence=None, now=NOW)
        ev = {"evidence_id": "ev-9", "kind": "resolution"}
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
        pub = {"status": "planning", "task": {"contribution_id": rec["contribution_id"], "revision": 0},
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
               "task": {"contribution_id": rec["contribution_id"], "revision": 8},
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
        pub = {"status": "planning", "task": {"contribution_id": rec["contribution_id"], "revision": 0},
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
                                  now=T, issues=[issue()], acceptances={77: OWNER["id"]}))
        kinds = [k for k, _ in res.staged]
        self.assertIn("contributions", kinds)
        self.assertIn("projects", kinds)
        # Planning is selected; it needs no write admission.
        self.assertEqual(res.requested_effects[0]["effect_contract"], {"kind": "none"})


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
