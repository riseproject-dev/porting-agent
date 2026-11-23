# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Live closeio--ciso8601/1 repair loop on green exact-head CI (plan 3.4, 8.6, 12.3, 14.1).

Fixtures are the exact live shapes:

* ``task.json`` .. ``worker-gate.json``: the ``porting-publication`` artifact
  14750552358 of worker run 45010139289 (development_repair at revision 5),
  whose valid ``no_change`` proposal the worker gate misclassified as a
  validation rejection;
* ``contribution-withdrawn-r5.json``: state at e96fae5, after the r5 repair
  was withdrawn although its Work was claimed and its worker was running;
* ``contribution-r6.json``: state at 9a6c7d6, after the late r5 Result bumped
  the revision and requested the r6 repair whose Work is claimed by worker run
  45013412209.
"""

import copy
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import capacity
import commands
import controller
import reconcile
import work
import worker_gate
import worker_recovery
from tests.helpers import apply_command, policy
from tests.test_observation import TestExactHeadCiReconciliation

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/worker-no-change-rejected"
PUBLICATION_FILES = ("assignment.json", "task.json", "result.json", "outcome.json",
                     "proposal.json", "record.json", "worker-gate.json")
R5_RUN, R5_ARTIFACT = "45010139289", 14750552358
R6_RUN = "45013412209"
HEAD = "b29a8f49374004d650cf312e9c31848953e5ae60"
CI_EVIDENCE = "ci-1467208233-45008094068-1"
R5_KEY = "closeio--ciso8601/1@5:development_repair"
R6_KEY = "closeio--ciso8601/1@6:development_repair"
REASONS = {
    "rehearsal-closeio--ciso8601--1-r3-44982902579": {
        "kind": "rehearsal", "evidence_id": "rehearsal-closeio--ciso8601--1-r3-44982902579"},
    "dossier-closeio--ciso8601--1-r2": {
        "kind": "dossier", "evidence_id": "dossier-closeio--ciso8601--1-r2"},
}
DISPATCHERS = [
    {"id": 45009921961, "created_at": "2026-11-22T09:55:57Z", "status": "completed"},
    {"id": 45013187668, "created_at": "2026-11-22T10:35:26Z", "status": "completed"},
]


def load(name):
    return json.loads((FIXTURE / name).read_text())


def publication_dir(temporary, run_id, mutate=None):
    directory = Path(temporary) / run_id
    directory.mkdir(parents=True)
    for name in PUBLICATION_FILES:
        shutil.copy(FIXTURE / name, directory / name)
    if mutate:
        files = {name: json.loads((directory / name).read_text()) for name in PUBLICATION_FILES}
        mutate(files)
        for name, value in files.items():
            (directory / name).write_text(json.dumps(value))
    return directory


def as_r6_result(files):
    """The same no_change answer, delivered for the claimed r6 Work."""
    files["task.json"]["revision"] = 6
    files["proposal.json"]["expected_revision"] = 6
    files["result.json"]["run_id"] = R6_RUN
    files["record.json"] = load("contribution-r6.json")


def collected(temporary, artifact_ids):
    for run_id, artifact_id in artifact_ids.items():
        (Path(temporary) / run_id / "artifact-id").write_text(str(artifact_id))
    return reconcile._publication_outcomes(temporary)


def tick(record, *, publications=(), queue_works=None, runs=None, now="2026-11-22T10:50:00+00:00"):
    observed = TestExactHeadCiReconciliation.snapshot(pending=False)
    with mock.patch("observe_ci.collect.branch_head", return_value={"head": HEAD}), \
         mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
         mock.patch("observe_ci.collect.ci_snapshot", return_value=observed):
        return reconcile.tick(
            object(), reconcile.Snapshot({"c": copy.deepcopy(record)}, {}, {}, {}, dict(REASONS)),
            policy=policy(read_only=False, destination_writes=True),
            now=datetime.fromisoformat(now), publications=list(publications),
            pressure=capacity.Pressure(ok=True), issues=[], comments=[],
            dispatcher_runs=runs, queue_works=queue_works)


def final(output, original):
    records = [item for kind, item in output.staged if kind == "contributions"]
    return records[-1] if records else original


def events(output):
    return [item["kind"] for kind, item in output.staged if kind == "events"]


class TestNoChangeIsPreserved(unittest.TestCase):
    """Plan 8.6: a valid exact-task no_change Result is not a validation failure."""

    def test_live_no_change_proposal_passes_the_worker_gate(self):
        report = worker_gate.validate(load("task.json"), load("proposal.json"),
                                      FIXTURE / "patches.mbox")
        self.assertEqual((report["status"], report["failure_class"]), ("accepted", None))
        self.assertEqual(report["changed_files"], [])
        # The live artifact recorded the old misclassification.
        self.assertEqual((load("worker-gate.json")["status"], load("outcome.json")["status"]),
                         ("blocked", "rejected"))

    def test_no_change_must_not_carry_a_write_operation(self):
        proposal = dict(load("proposal.json"), operation="append")
        with self.assertRaisesRegex(worker_gate.WorkerGateError, "operation none"):
            worker_gate.validate(load("task.json"), proposal, FIXTURE / "patches.mbox")

    def test_misclassified_live_artifact_is_normalized_by_trusted_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = publication_dir(temporary, R5_RUN)
            outcome = worker_recovery.normalize(directory, R5_RUN)
            self.assertEqual((outcome["status"], outcome["failure_class"], outcome["stage"]),
                             ("no_change", None, "preflight"))
            self.assertEqual(json.loads((directory / "outcome.json").read_text()), outcome)
            self.assertTrue((directory / "recovered-worker-no-change").is_file())

    def test_genuine_rejections_are_not_reclassified(self):
        def unacknowledged(files):
            files["proposal.json"]["reason_ids"] = ["dossier-closeio--ciso8601--1-r2"]

        def blocked(files):
            files["proposal.json"]["outcome"] = "blocked"

        def other_head(files):
            files["proposal.json"]["expected_head"] = "c" * 40

        for mutate in (unacknowledged, blocked, other_head):
            with self.subTest(mutate=mutate.__name__), tempfile.TemporaryDirectory() as temporary:
                directory = publication_dir(temporary, R5_RUN, mutate)
                outcome = worker_recovery.normalize(directory, R5_RUN)
                self.assertEqual((outcome["status"], outcome["failure_class"]),
                                 ("rejected", "validation"))
                self.assertFalse((directory / "recovered-worker-no-change").exists())


class TestLiveWithdrawalNeedsQueueProof(unittest.TestCase):
    """Run 45012008778 withdrew r5 although its Work was claimed (plan 12.3)."""

    def r5_requested(self):
        record = load("contribution-withdrawn-r5.json")
        r6 = load("contribution-r6.json")
        record.update(activity="agent_running", active_work=dict(
            r6["active_work"], dedupe_key=R5_KEY, revision=5,
            requested_at="2026-11-22T09:55:37.891318+00:00",
            deadline_at="2026-11-22T15:55:37.891318+00:00"))
        return record

    def test_claimed_work_with_null_dispatched_run_id_is_not_withdrawn(self):
        record = self.r5_requested()
        self.assertIsNone(record["active_work"]["dispatched_run_id"])
        output = tick(record, queue_works={work.graph_id(R5_KEY): "claimed"}, runs=DISPATCHERS,
                      now="2026-11-22T10:21:30+00:00")
        result = final(output, record)
        self.assertEqual((result["phase"], result["activity"]), ("development", "agent_running"))
        self.assertEqual(result["active_work"]["dedupe_key"], R5_KEY)
        self.assertEqual(output.report["observation"][0]["handoff_deferred"],
                         "requested Work is still active")
        self.assertNotIn("obsolete_ci_repair_withdrawn", events(output))
        self.assertEqual(output.report["requested_effects"], [])

    def test_unknown_queue_or_running_dispatcher_is_not_proof(self):
        record = self.r5_requested()
        running = DISPATCHERS[:1] + [dict(DISPATCHERS[0], id=1, status="in_progress")]
        for queue_works, runs in ((None, DISPATCHERS), ({}, None), ({}, running)):
            with self.subTest(queue=queue_works, runs=runs):
                self.assertIn(reconcile.provably_unadmitted_work(
                    {"c": record}, runs=runs, queue_works=queue_works), (None, set()))
                output = tick(record, queue_works=queue_works, runs=runs,
                              now="2026-11-22T10:21:30+00:00")
                self.assertEqual(final(output, record)["active_work"]["dedupe_key"], R5_KEY)

    def test_work_absent_from_queue_is_cleared_at_handoff(self):
        record = self.r5_requested()
        self.assertEqual(reconcile.provably_unadmitted_work(
            {"c": record}, runs=DISPATCHERS[:1], queue_works={}), {R5_KEY})
        output = tick(record, queue_works={}, runs=DISPATCHERS[:1],
                      now="2026-11-22T10:21:30+00:00")
        result = final(output, record)
        self.assertEqual((result["phase"], result["activity"]), ("handoff", "awaiting_submission"))
        self.assertIsNone(result["active_work"])
        self.assertEqual(events(output), ["obsolete_ci_repair_withdrawn", "nominate_for_handoff"])
        self.assertEqual(output.report["dispatch_relaunch"], [])


class TestLateResultOfWithdrawnWork(unittest.TestCase):
    """c338f21 folded the late r5 Result into the withdrawn state (plan 8.6)."""

    def test_late_rejection_answers_no_request_and_requests_no_repair(self):
        record = load("contribution-withdrawn-r5.json")
        self.assertEqual((record["revision"], record["activity"], record["active_work"]),
                         (5, "waiting_ci", None))
        with tempfile.TemporaryDirectory() as temporary:
            publication_dir(temporary, R5_RUN)
            # As delivered: the controller of that time had no no_change recovery.
            publications = collected(temporary, {R5_RUN: R5_ARTIFACT})
        self.assertEqual(publications[0]["status"], "rejected")
        output = tick(record, publications=publications, queue_works={}, runs=DISPATCHERS)
        result = final(output, record)
        self.assertEqual(output.report["publications"],
                         [{"contribution": "closeio--ciso8601/1", "ignored": "late Result without matching requested Work"}])
        self.assertEqual([e["kind"] for k, e in output.staged if k == "events"][:1],
                         ["record_late_effect"])
        self.assertIn(R5_ARTIFACT, result["processed_publications"])
        self.assertEqual(result["revision"], 5)
        self.assertEqual((result["phase"], result["activity"]), ("handoff", "awaiting_submission"))
        self.assertEqual(output.report["requested_effects"], [])


class TestLiveR6SelfHeals(unittest.TestCase):
    """From the 9a6c7d6 state: r6 is claimed and running; nothing is rewritten."""

    QUEUE = {work.graph_id(R5_KEY): "completed", work.graph_id(R6_KEY): "claimed"}
    RUNS = DISPATCHERS

    def test_while_r6_runs_green_ci_is_recorded_and_handoff_waits(self):
        record = load("contribution-r6.json")
        output = tick(record, queue_works=self.QUEUE, runs=self.RUNS)
        result = final(output, record)
        self.assertEqual((result["revision"], result["activity"]), (6, "agent_running"))
        self.assertEqual(result["active_work"], record["active_work"])
        self.assertEqual(result["last_ci_evidence_id"], CI_EVIDENCE)
        self.assertEqual(output.report["requested_effects"], [])
        self.assertEqual(output.report["dispatch_relaunch"], [])
        self.assertEqual(events(output), [])

    def test_processed_r5_artifact_stays_processed_after_normalization(self):
        record = load("contribution-r6.json")
        with tempfile.TemporaryDirectory() as temporary:
            worker_recovery.normalize(publication_dir(temporary, R5_RUN), R5_RUN)
            publications = collected(temporary, {R5_RUN: R5_ARTIFACT})
        self.assertEqual(publications[0]["status"], "no_change")
        output = tick(record, publications=publications, queue_works=self.QUEUE, runs=self.RUNS)
        self.assertEqual(output.report["publications"], [])
        result = final(output, record)
        self.assertEqual((result["revision"], result["active_work"]), (6, record["active_work"]))

    def test_r6_no_change_result_enters_handoff_without_new_work(self):
        record = load("contribution-r6.json")
        with tempfile.TemporaryDirectory() as temporary:
            directory = publication_dir(temporary, R6_RUN, as_r6_result)
            # The r6 worker runs trusted code from c338f21, so it delivers the
            # same misclassification; the controller's recovery normalizes it.
            self.assertEqual(json.loads((directory / "outcome.json").read_text())["status"], "rejected")
            worker_recovery.normalize(directory, R6_RUN)
            publications = collected(temporary, {R6_RUN: 14760000000})
        output = tick(record, publications=publications,
                      queue_works=dict(self.QUEUE, **{work.graph_id(R6_KEY): "completed"}),
                      runs=self.RUNS, now="2026-11-22T11:20:00+00:00")
        result = final(output, record)
        self.assertEqual(result["revision"], 6)
        self.assertEqual((result["phase"], result["activity"]), ("handoff", "awaiting_submission"))
        self.assertIsNone(result["active_work"])
        self.assertIsNone(result["clean_head_sha"])
        self.assertEqual(result["fork"]["head_sha"], HEAD)
        self.assertEqual(result["pending_human_action"]["kind"], "submit_upstream")
        self.assertEqual(result["last_ci_evidence_id"], CI_EVIDENCE)
        self.assertEqual(result["handoff_request"],
                         {"command_id": f"observation-{CI_EVIDENCE}", "head": HEAD, "revision": 6})
        self.assertEqual(result["proposal_artifact"]["outcome"], "no_change")
        self.assertEqual(result["budget"], record["budget"])
        self.assertEqual(events(output), ["worker_no_change", "nominate_for_handoff"])
        no_change = next(item for kind, item in output.staged
                         if kind == "events" and item["kind"] == "worker_no_change")
        self.assertEqual(no_change["cause"], {"type": "result", "id": R6_RUN})
        self.assertFalse(no_change["revision_bumped"])
        self.assertEqual(output.report["requested_effects"], [])
        self.assertEqual(output.report["dispatch_relaunch"], [])

    def test_genuine_r6_rejection_charges_no_progress_only(self):
        record = load("contribution-r6.json")
        self.assertEqual(record["budget"]["no_progress_streak"], 1)
        rejected = {"status": "rejected", "failure_class": "validation", "reason": "patch digest mismatch",
                    "stage": "worker_gate", "proposal": dict(load("proposal.json"), outcome="patch")}
        new, staged = reconcile.apply_publication(
            record, rejected, now="2026-11-22T11:20:00+00:00",
            policy=policy(read_only=False, destination_writes=True), worker_run_id=R6_RUN)
        self.assertEqual(new["revision"], 7)
        # `work_total` was charged when the Work was allocated; the rejected
        # outcome charges `no_progress`, now exhausted. Its exhaustion is
        # taken where the next Work is due.
        self.assertEqual(new["budget"]["attempts_used"], 1)
        self.assertEqual(new["budget"]["no_progress_streak"], 2)
        self.assertEqual(new["budget"]["clusters"], record["budget"]["clusters"])
        self.assertEqual((new["phase"], new["activity"]), ("development", "fix_needed"))
        self.assertIsNone(new["active_work"])
        self.assertEqual([item["kind"] for kind, item in staged if kind == "events"],
                         ["worker_proposal_rejected"])

    def test_rejection_exhaustion_refreshes_then_parks_and_resumes_clean(self):
        record = load("contribution-r6.json")
        rejected = {"status": "rejected", "failure_class": "validation", "reason": "patch digest mismatch",
                    "stage": "worker_gate", "proposal": dict(load("proposal.json"), outcome="patch")}
        fix_needed, _staged = reconcile.apply_publication(
            record, rejected, now="2026-11-22T11:20:00+00:00",
            policy=policy(read_only=False, destination_writes=True), worker_run_id=R6_RUN)

        def select(rec):
            # Outside the canary's own recovery, which holds this record.
            rec = dict(rec, contribution_id="example--proj/12")
            res = controller.tick(contributions={"c": rec}, operations={}, consumed={},
                                  projects={}, policy=policy(), inputs=controller.Inputs(
                                      now=datetime.fromisoformat("2026-11-22T11:30:00+00:00"),
                                      pressure=capacity.Pressure(ok=True)))
            return res, [item for kind, item in res.staged if kind == "contributions"][-1]

        res, refreshed = select(fix_needed)
        self.assertEqual(res.requested_effects, [])
        self.assertEqual([e["kind"] for k, e in res.staged if k == "events"],
                         ["refresh_on_exhaustion"])
        self.assertEqual((refreshed["phase"], refreshed["activity"]), ("queued", "idle"))
        self.assertEqual(refreshed["refresh"]["from_phase"], "development")
        res, parked = select(dict(fix_needed, automatic_refresh_used=True))
        self.assertEqual(res.requested_effects, [])
        self.assertEqual((parked["phase"], parked["activity"]), ("parked", "idle"))
        self.assertIsNone(parked["active_work"])
        parked = dict(parked, contribution_id=record["contribution_id"])
        output = tick(parked, queue_works={}, runs=self.RUNS, now="2026-11-22T11:30:00+00:00")
        result = final(output, parked)
        self.assertEqual((result["phase"], result["activity"]), ("parked", "idle"))
        self.assertEqual(output.report["requested_effects"], [])
        # Plan 3.3.4 failure case 2 and 3.5: exact-revision resume starts a
        # clean generation at queued:idle with a fresh retry budget.
        resumed = apply_command(
            parked, commands.parse(f"/upstreaming resume revision={parked['revision']} reason=r"),
            command_id="comment-1", actor={"login": "owner", "id": 1}, now="2026-11-22T11:40:00+00:00")
        self.assertTrue(resumed.accepted)
        out = resumed.record
        self.assertEqual((out["phase"], out["activity"]), ("queued", "idle"))
        self.assertEqual(out["generation"], parked["generation"] + 1)
        self.assertEqual(out["revision"], parked["revision"] + 1)
        self.assertEqual(out["budget"]["attempts_used"], 0)
        self.assertIsNone(out["parked_reason"])
        self.assertIsNone(out["fork"])
        self.assertIsNone(out["dossier_evidence_id"])
        self.assertEqual(out["processed_publications"], parked["processed_publications"])


if __name__ == "__main__":
    unittest.main()
