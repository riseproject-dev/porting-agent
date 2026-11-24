# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A work-queue Policy that refuses every dispatch heals without a human.

The fixtures are the replayed ledger and the waiting record of the live
case: the planner profile was renamed from `investigator`, so gh-aw refused
every dispatch of `luhenry--d2vm/3` and dispatch recovery waited for a human.
"""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path

import engine
import queue_policy
import reconcile
import work
import work_history
from tests.helpers import NOW, contribution, policy

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures/queue-policy"
REPOSITORY = "riseproject-dev/porting-agent"


def projection() -> dict:
    return json.loads((FIXTURES / "projection.json").read_text())


def waiting() -> dict:
    return json.loads((FIXTURES / "waiting-dispatch-failed.json").read_text())


def compiled() -> dict:
    return queue_policy.load_compiled(ROOT, sha="f" * 40, repository=REPOSITORY)


def reinstalled(at: datetime) -> dict:
    """The replay once the dispatcher has published the compiled policy at
    ``at``: the projection of the reinstall test of the dispatcher module."""
    p = projection()
    epoch = "epoch_" + "e" * 64
    p["policy"] = compiled()
    p["policy_epoch"] = epoch
    p["requests"]["policy-reinstall"] = {
        "at": int(at.timestamp() * 1000), "policy_epoch": epoch,
        "actor": {"role": "administrator"}, "request": {"id": "policy-reinstall", "kind": "policy"},
        "operations": [{"kind": "Policy", "epoch": epoch, "policy": p["policy"]}]}
    return p


class TestCompiledPolicy(unittest.TestCase):
    def test_the_dispatcher_lock_carries_one_compiled_policy(self):
        p = compiled()
        self.assertEqual(p["authorization"], "aw")
        profiles = p["pools"]["default"]["profiles"]
        self.assertEqual(sorted(profiles), ["planner", "reviewer", "worker"])
        self.assertEqual(p["pools"]["default"]["default_profile"], "planner")
        for name, profile in profiles.items():
            self.assertEqual(profile["workflow"], f".github/workflows/{name}.lock.yml")
            self.assertEqual(profile["ref"], "f" * 40)
            self.assertEqual(profile["effect_scope"], REPOSITORY)
        self.assertEqual(p["pools"]["default"]["allowed_repositories"], [REPOSITORY])

    def test_the_proposal_command_writes_the_run_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "proposal.json"
            self.assertEqual(queue_policy.main([
                "proposal", "--lock", str(ROOT / queue_policy.DISPATCHER_LOCK),
                "--sha", "f" * 40, "--repository", REPOSITORY, "--out", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text()), compiled())

    def test_an_unknown_expression_or_two_policies_are_refused(self):
        lock = (ROOT / queue_policy.DISPATCHER_LOCK).read_text()
        with self.assertRaises(queue_policy.PolicyConfigError):
            queue_policy.compiled_policy(lock.replace("${{ github.sha }}", "${{ github.ref }}"),
                                         sha="f" * 40, repository=REPOSITORY)
        text = queue_policy.compiled_text(lock)
        changed = lock.replace(text.replace('"', '\\"'), text.replace("planner", "other").replace(
            '"', '\\"'), 1)
        self.assertNotEqual(changed, lock)
        with self.assertRaisesRegex(queue_policy.PolicyConfigError, "found 2"):
            queue_policy.compiled_text(changed)


class TestStatus(unittest.TestCase):
    def test_the_live_ledger_drops_the_renamed_profile(self):
        status = queue_policy.status(projection(), compiled())
        self.assertEqual(status["dropped"], ["default/investigator"])
        self.assertFalse(status["preserved"])
        self.assertTrue(status["quiescent"])
        self.assertEqual(status["installed_at"], "2026-11-22T02:04:43.546000+00:00")

    def test_refs_and_added_profiles_never_drop_a_profile(self):
        p = projection()
        p["policy"] = copy.deepcopy(compiled())
        moved = copy.deepcopy(compiled())
        for profile in moved["pools"]["default"]["profiles"].values():
            profile["ref"] = "0" * 40
            profile["logical_contract"] = "1" * 64
        moved["pools"]["default"]["profiles"]["extra"] = dict(
            moved["pools"]["default"]["profiles"]["worker"], workflow=".github/workflows/extra.lock.yml")
        self.assertTrue(queue_policy.status(p, moved)["preserved"])

    def test_the_added_reviewer_profile_installs_without_a_reinstall(self):
        """The queue installed before the review of development patches holds
        the planner and worker profiles only; the compiled policy adds the
        reviewer, which gh-aw installs on demand: no profile is dropped, so
        nothing refuses a dispatch and the dispatcher reinstalls nothing,
        whether or not the queue is quiescent."""
        p = projection()
        before = copy.deepcopy(compiled())
        del before["pools"]["default"]["profiles"]["reviewer"]
        p["policy"] = before
        self.assertEqual(sorted(compiled()["pools"]["default"]["profiles"]),
                         ["planner", "reviewer", "worker"])
        status = queue_policy.status(p, compiled())
        self.assertEqual((status["preserved"], status["dropped"]), (True, []))
        next(iter(p["works"].values()))["state"] = "available"
        self.assertTrue(queue_policy.status(p, compiled())["preserved"])
        # A rollback that drops the reviewer again is a dropped profile,
        # which the dispatcher reinstalls on a quiescent queue.
        p = projection()
        p["policy"] = copy.deepcopy(compiled())
        self.assertEqual(queue_policy.status(p, before)["dropped"], ["default/reviewer"])

    def test_a_queue_with_live_work_or_a_held_dispatch_is_not_quiescent(self):
        p = projection()
        next(iter(p["works"].values()))["state"] = "available"
        self.assertFalse(queue_policy.quiescent(p))
        p = projection()
        next(iter(p["works"].values()))["barrier"] = "pending"
        self.assertFalse(queue_policy.quiescent(p))
        p = projection()
        next(iter(p["dispatches"].values()))["released"] = False
        self.assertFalse(queue_policy.quiescent(p))

    def test_no_queue_yet_is_preserved(self):
        self.assertEqual(queue_policy.status({}, compiled())["preserved"], True)


class TestPolicyRecovery(unittest.TestCase):
    def setUp(self):
        self.policy = policy()
        self.record = waiting()
        self.failed = datetime.fromisoformat(self.record["pending_human_action"]["requested_at"])

    def recover(self, projection_, *, runs=(), now=None):
        contributions = {"c": copy.deepcopy(self.record)}
        status = queue_policy.status(projection_, compiled())
        launch, staged, report = reconcile.queue_policy_recovery(
            contributions, queue_policy=status, runs=list(runs),
            now=now or self.failed + timedelta(hours=2), policy=self.policy)
        return contributions["c"], launch, staged, report

    def test_the_stuck_record_is_re_armed_once_the_policy_is_reinstalled(self):
        record, launch, staged, report = self.recover(
            reinstalled(self.failed + timedelta(hours=1)))
        self.assertEqual(engine.state_key(record), "planning.idle")
        self.assertIsNone(record["pending_human_action"])
        self.assertEqual(record["revision"], self.record["revision"] + 1)
        self.assertEqual(launch, [])
        event = [r for kind, r in staged if kind == "events"]
        self.assertEqual([e["kind"] for e in event], ["rearm_dispatch"])
        self.assertEqual(event[0]["details"]["restarted"]["counter"], "dispatch_recovery")
        self.assertIn("re-armed", report[0]["decision"])

    def test_the_re_armed_work_gets_a_new_history_with_the_steering_of_the_wait(self):
        """The re-arm bumps the revision, so the Work allocated after it has
        a new Work id and its own history, built with the steering written
        while the dispatch waited; the history of the Work that failed is
        not reused."""
        record, _launch, _staged, _report = self.recover(
            reinstalled(self.failed + timedelta(hours=1)))
        cid, generation = record["contribution_id"], int(record.get("generation", 0))
        failed_key = work.work_key(cid, generation, self.record["revision"], "planning")
        key = work.work_key(cid, generation, record["revision"], "planning")
        self.assertNotEqual(key, failed_key)
        allocated = copy.deepcopy(record)
        allocated["activity"] = "agent_running"
        allocated["active_work"] = {
            "dedupe_key": key, "task": "planning", "generation": generation,
            "revision": record["revision"], "attempt": 1, "requested_at": NOW,
            "deadline_at": NOW, "dispatched_run_id": None, "reason_ids": [],
            "feedback_evidence_ids": []}
        old = work_history.evidence_id(failed_key)
        snap = reconcile.Snapshot({"c": allocated}, {}, {}, {}, {old: {"kind": "work_history"}})
        out = reconcile.TickOutput(report={"controller": {"alerts": []}})
        written = {"source": "tracking_issue_comment", "id": 77, "author": "owner",
                   "author_id": 1001, "authority": True, "created_at": NOW,
                   "updated_at": NOW, "url": "u", "body": "Target the 2.x branch."}
        with unittest.mock.patch("collect.steering",
                                 return_value={"items": [written], "omitted": {}}):
            report = reconcile.work_histories(None, snap.contributions, out, snap,
                                              policy=self.policy, now=NOW,
                                              tracking_issues={}, skip=frozenset())
        self.assertEqual(report[0]["evidence_id"], work_history.evidence_id(key))
        [history] = [r for kind, r in out.staged if r.get("kind") == "work_history"]
        self.assertEqual([i["body"] for i in history["data"]["steering"]],
                         ["Target the 2.x branch."])

    def test_a_reinstall_before_the_failure_re_arms_nothing(self):
        record, _launch, staged, report = self.recover(
            reinstalled(self.failed - timedelta(minutes=1)))
        self.assertEqual(record, self.record)
        self.assertEqual(staged, [])
        self.assertIn("no Policy reinstall", report[0]["decision"])

    def test_a_paused_record_keeps_waiting(self):
        self.record.update(paused=True, paused_at=NOW, paused_by="human", paused_reason="hold")
        record, _launch, staged, _report = self.recover(
            reinstalled(self.failed + timedelta(hours=1)))
        self.assertEqual(staged, [])
        self.assertEqual(engine.state_key(record), "planning.waiting_human")

    def test_a_refused_policy_on_a_quiescent_queue_launches_the_dispatcher(self):
        now = self.failed + timedelta(hours=2)
        record, launch, staged, _report = self.recover(projection(), now=now)
        self.assertEqual(record, self.record)
        self.assertEqual(staged, [])
        self.assertEqual(launch, [{"type": "reinstall_queue_policy"}])
        recent = {"id": 1, "status": "completed",
                  "created_at": (now - timedelta(minutes=5)).isoformat()}
        self.assertEqual(self.recover(projection(), runs=[recent], now=now)[1], [])
        running = {"id": 2, "status": "in_progress",
                   "created_at": (now - timedelta(hours=5)).isoformat()}
        self.assertEqual(self.recover(projection(), runs=[running], now=now)[1], [])

    def test_a_busy_queue_launches_nothing(self):
        p = projection()
        next(iter(p["works"].values()))["state"] = "available"
        self.assertEqual(self.recover(p)[1], [])


class TestDispatchWhilePolicyRefuses(unittest.TestCase):
    def setUp(self):
        self.record = contribution(phase="planning", activity="agent_running", revision=1)
        self.record["active_work"] = {
            "dedupe_key": "example--proj/12@1:planning", "task": "planning", "revision": 1,
            "attempt": 1, "requested_at": NOW, "deadline_at": "2026-11-19T12:00:00+00:00",
            "dispatched_run_id": None, "reason_ids": [], "generation": 0,
            "feedback_evidence_ids": []}
        self.runs = [{"id": n, "status": "completed", "conclusion": "failure",
                      "created_at": f"2026-11-18T12:0{n}:00Z"} for n in (1, 2, 3)]

    def recover(self, status):
        contributions = {"c": copy.deepcopy(self.record)}
        return reconcile.dispatch_recovery(
            contributions, runs=self.runs, queue_works={},
            now=datetime.fromisoformat("2026-11-18T13:00:00+00:00"), policy=policy(),
            queue_policy=status)

    def test_nothing_is_charged_while_the_queue_drains(self):
        p = projection()
        next(iter(p["works"].values()))["state"] = "available"
        relaunch, staged, report = self.recover(queue_policy.status(p, compiled()))
        self.assertEqual((relaunch, staged), ([], []))
        self.assertIn("waiting for the queue to drain", report[0]["decision"])

    def test_a_quiescent_queue_decides_as_before(self):
        _relaunch, staged, _report = self.recover(queue_policy.status(projection(), compiled()))
        self.assertEqual([r["kind"] for kind, r in staged if kind == "events"],
                         ["exhaust_recovery"])


class TestPlanSkipsAnUnboundRecord(unittest.TestCase):
    def test_a_record_without_identity_never_blocks_another(self):
        def requested(cid, issue, *, bound):
            record = contribution(phase="planning", activity="agent_running", revision=1,
                                  contribution_id=cid)
            record["tracking_issue"] = {"id": issue, "number": issue}
            if not bound:
                record["upstream"] = dict(record["upstream"], repository_id=None, base_sha=None)
            record["active_work"] = {
                "dedupe_key": f"{cid}@1:planning", "task": "planning", "revision": 1,
                "attempt": 1, "requested_at": NOW, "deadline_at": NOW,
                "dispatched_run_id": None, "reason_ids": [], "generation": 0,
                "feedback_evidence_ids": [], "tracking_issue": {"url": "https://x"}}
            return record
        histories = {work_history.evidence_id(f"{cid}@1:planning"): {}
                     for cid in ("example--a/1", "example--b/2")}
        snapshot = type("S", (), {"contributions": {
            "a": requested("example--a/1", 1, bound=False),
            "b": requested("example--b/2", 2, bound=True)}, "evidence": histories})
        with tempfile.TemporaryDirectory() as directory, \
                unittest.mock.patch("reconcile.load_snapshot", return_value=(None, snapshot)), \
                unittest.mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout="c" * 40)), \
                unittest.mock.patch("builtins.print") as printed:
            out = Path(directory) / "plan.json"
            (Path(directory) / ".state" / "evidence").mkdir(parents=True)
            for history_id in histories:
                (Path(directory) / ".state" / "evidence" / f"{history_id}.json").write_text("{}")
            args = type("A", (), {"root": directory, "out": str(out), "control_repo_id": "1"})
            self.assertEqual(work.cmd_plan(args), 0)
            nodes = json.loads(out.read_text())["nodes"]
        self.assertEqual([n["payload"]["contribution_id"] for n in nodes], ["example--b/2"])
        report = json.loads(printed.call_args.args[0])
        self.assertEqual([s["contribution"] for s in report["selected"]], ["example--b/2"])
        self.assertEqual(report["skipped"], [{
            "contribution": "example--a/1", "task": "planning",
            "reason": "the upstream identity or base is not bound"}])


if __name__ == "__main__":
    unittest.main()
