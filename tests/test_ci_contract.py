# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The CI rule of a contribution with no recorded job pattern.

``luhenry--d2vm/3`` waited in ``development.waiting_ci`` at head b393cc1
with CI that passed: its record names no target contract, and the default
required job pattern ``riscv64`` matched none of the project's jobs
(Tests, Test Templates, End to end Tests, Docs up to date, Build), so CI
was ``missing`` and nothing recorded it. Without a recorded pattern CI
passes when every job of every run on the head executed and passed; a
pattern applies only when a recorded contract names one, and CI that is
complete and still ``missing`` asks a human (``require_head_ci``).
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import ci_evidence
import collect
import commands
import engine
import lifecycle
import observe_ci
from tests.helpers import apply_command
from tests.test_development_table import POLICY, T, final, kinds, tick

FIXTURE = Path(__file__).parent / "fixtures/live-d2vm-ci-contract/contribution.json"
HEAD = "b393cc111205dd693b669458942646c53ed7311a"
RUN = 45588297911
JOBS = ("Tests", "Test Templates (quay.io/centos/centos:stream)",
        "End to end Tests (ubuntu-24.04)", "Docs up to date", "Build")


def live() -> dict:
    """The live record of ``luhenry--d2vm/3``: ``development.waiting_ci``,
    no target contract."""
    return engine.with_current_names(json.loads(FIXTURE.read_text()))[0]


def job(number: int, name: str, conclusion="success", status="completed") -> dict:
    return {"id": 1000 + number, "run_id": RUN, "name": name, "status": status,
            "conclusion": conclusion, "started_at": "2026-11-25T14:00:00Z",
            "labels": ["ubuntu-24.04"]}


def d2vm_ci(*, conclusions=None, names=JOBS, status="completed", head=HEAD) -> collect.CiSnapshot:
    """Run 45588297911, attempt 2, on the head of d2vm: its five jobs."""
    conclusions = conclusions or {}
    run = {"id": RUN, "head_sha": head, "run_attempt": 2, "status": status,
           "conclusion": "success" if status == "completed" else None, "workflow_id": 1,
           "event": "pull_request", "path": ".github/workflows/ci.yaml"}
    jobs = [job(i, name, conclusions.get(name, "success"), status if status != "completed"
                else "completed") for i, name in enumerate(names)]
    return collect.CiSnapshot(
        repository={"id": 1469259163, "full_name": "riseproject-upstreaming/d2vm"},
        head_sha=head, runs=[run], jobs_by_run={RUN: jobs}, artifacts_by_run={})


def evaluate(snap: collect.CiSnapshot, patterns=()) -> ci_evidence.CiVerdict:
    return collect.evaluate_snapshot(snap, required_job_patterns=list(patterns),
                                     expected_wheels=[])


def contract(patterns) -> dict:
    return {"evidence_id": "target-1", "kind": "target_contract",
            "data": {"required_job_patterns": patterns}}


class TestEveryJobRule(unittest.TestCase):
    def test_the_shape_of_d2vm_passes_without_a_contract(self):
        verdict = evaluate(d2vm_ci())
        self.assertEqual((verdict.state, verdict.problems), ("passed", []))
        self.assertEqual(len(verdict.executed_jobs), 5)
        self.assertEqual(observe_ci.contract_for(live(), {})["required_job_patterns"], [])

    def test_a_contract_without_a_pattern_is_the_same_rule(self):
        record = dict(live(), target_contract_id="target-1")
        self.assertEqual(observe_ci.contract_for(record, {"target-1": contract([])})
                         ["required_job_patterns"], [])

    def test_a_failing_job_still_fails(self):
        verdict = evaluate(d2vm_ci(conclusions={"Docs up to date": "failure"}))
        self.assertEqual(verdict.state, "failed")
        self.assertIn("job 'Docs up to date' failed (failure)", verdict.problems)

    def test_a_publishing_job_that_fails_does_not_fail_the_head(self):
        snap = d2vm_ci(names=JOBS + ("Publish to PyPI",),
                       conclusions={"Publish to PyPI": "failure"})
        self.assertEqual(evaluate(snap).state, "passed")

    def test_no_job_at_all_is_missing(self):
        snap = d2vm_ci(names=())
        self.assertEqual((evaluate(snap).state, evaluate(snap).problems),
                         ("missing", ["no job executed on this head"]))

    def test_only_skipped_jobs_is_missing(self):
        snap = d2vm_ci(names=("Build",), conclusions={"Build": "skipped"})
        self.assertEqual(evaluate(snap).state, "missing")

    def test_a_skipped_job_is_ignored_when_another_job_passed(self):
        snap = d2vm_ci(conclusions={"Docs up to date": "skipped"})
        self.assertEqual(evaluate(snap).state, "passed")

    def test_a_cancelled_job_did_not_execute_and_is_interrupted(self):
        verdict = evaluate(d2vm_ci(conclusions={"Build": "cancelled"}))
        self.assertEqual(verdict.state, "not_executed")
        self.assertEqual([j["name"] for j in verdict.interrupted_jobs], ["Build"])

    def test_a_run_in_progress_is_pending(self):
        self.assertEqual(evaluate(d2vm_ci(status="in_progress")).state, "pending")

    def test_a_recorded_pattern_applies_and_must_match_a_job(self):
        self.assertEqual(evaluate(d2vm_ci(), ["Build"]).state, "passed")
        verdict = evaluate(d2vm_ci(), ["riscv64"])
        self.assertEqual((verdict.state, verdict.problems),
                         ("missing", ["required job 'riscv64' absent"]))


class TestObservation(unittest.TestCase):
    def test_d2vm_leaves_waiting_ci_by_the_passing_path(self):
        rec = live()
        self.assertEqual(engine.state_key(rec), "development.waiting_ci")
        out = tick(rec, head=HEAD, ci=d2vm_ci())
        self.assertEqual(kinds(out), ["nominate_for_handoff"])
        after = final(out, rec)
        self.assertEqual(engine.state_key(after), "handoff.awaiting_submission")
        self.assertEqual(out.report["observation"][0]["ci"]["state"], "passed")

    def test_a_failing_job_records_the_failure(self):
        out = tick(live(), head=HEAD, ci=d2vm_ci(conclusions={"Build": "failure"}))
        self.assertEqual(kinds(out)[:1], ["record_ci_failure"])

    def test_a_missing_pattern_of_a_recorded_contract_waits_for_a_human(self):
        rec = dict(live(), target_contract_id="target-1")
        out = tick(rec, head=HEAD, ci=d2vm_ci(), evidence={"target-1": contract(["riscv64"])})
        self.assertEqual(kinds(out), ["require_head_ci"])
        after = final(out, rec)
        self.assertEqual((engine.state_key(after), after["pending_human_action"]["kind"]),
                         ("development.waiting_human", lifecycle.HEAD_CI_MISSING_ACTION))
        [event] = [e for kind, e in out.staged if kind == "events"]
        self.assertEqual(event["details"]["problems"], ["required job 'riscv64' absent"])
        self.assertEqual(out.report["observation"][0]["waiting_for_human"], "head_ci_missing")
        # A resume observes CI again: the pattern is still absent.
        resumed = apply_command(after, commands.parse(
            f"/upstreaming resume revision={after['revision']} reason=fixed"), policy_=POLICY)
        self.assertEqual(resumed.event["kind"], "resume_ci")
        again = tick(resumed.record, head=HEAD, ci=d2vm_ci(),
                     evidence={"target-1": contract(["riscv64"])}, now=T)
        self.assertEqual(kinds(again), ["require_head_ci"])
        # Once a job of the pattern ran, the contribution is nominated.
        ci = d2vm_ci(names=JOBS + ("Build (riscv64)",))
        done = tick(resumed.record, head=HEAD, ci=ci, now=T,
                    evidence={"target-1": contract(["riscv64"])})
        self.assertEqual(kinds(done), ["nominate_for_handoff"])

    def test_a_head_with_no_job_executed_waits_for_a_human(self):
        out = tick(live(), head=HEAD, ci=d2vm_ci(names=()))
        self.assertEqual(kinds(out), ["require_head_ci"])

    def test_missing_evidence_keeps_the_wait_while_a_run_is_incomplete(self):
        # A completed run with a job still running is not complete.
        snap = d2vm_ci(names=JOBS)
        snap.jobs_by_run[RUN][0]["status"] = "in_progress"
        snap.jobs_by_run[RUN][0]["conclusion"] = None
        out = tick(live(), head=HEAD, ci=snap)
        self.assertEqual(kinds(out), [])
        # No run on the head yet.
        empty = d2vm_ci()
        empty.runs, empty.jobs_by_run = [], {}
        self.assertEqual(kinds(tick(live(), head=HEAD, ci=empty)), [])


if __name__ == "__main__":
    unittest.main()
