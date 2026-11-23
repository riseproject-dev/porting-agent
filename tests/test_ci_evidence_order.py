# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
import json
import unittest

import ci_evidence
import schemas

HEAD = "a" * 40


def _record(jobs):
    run = {"id": 7, "path": ".github/workflows/ci.yml", "run_attempt": 1, "head_sha": "a" * 40,
           "workflow_id": 3, "event": "push"}
    return ci_evidence.evidence_record(
        evidence_id="ci-1-7-1", contribution_id="o--r--1", policy_version="1",
        collected_at="2026-01-01T00:00:00Z", repository={"id": 1, "full_name": "o/r"},
        run=run, jobs=jobs, base_sha=None, test_merge_sha=None, expected_wheels=[],
        artifacts=[], verdict=ci_evidence.CiVerdict("passed"), debug_present=False,
        limitations=[])


class EvidenceJobOrder(unittest.TestCase):
    def test_jobs_are_recorded_in_id_order_whatever_the_api_order(self):
        jobs = [{"id": i, "name": f"job {i}", "status": "completed", "conclusion": "success",
                 "started_at": "2026-01-01T00:00:00Z"} for i in (5, 2, 9, 1)]
        first = _record(jobs)
        second = _record(list(reversed(jobs)))
        self.assertEqual(first, second)
        self.assertEqual([j["job_id"] for j in first["ci"]["jobs"]], [1, 2, 5, 9])


def _job(job_id, name, conclusion, **extra):
    return {"id": job_id, "run_id": 7, "name": name, "status": "completed",
            "conclusion": conclusion, "started_at": "2026-01-01T00:00:00Z", "labels": [],
            **extra}


def _verdict(jobs):
    run = {"id": 7, "head_sha": HEAD, "run_attempt": 2, "status": "completed"}
    return ci_evidence.evaluate(runs=[run], jobs_by_run={7: jobs}, head_sha=HEAD,
                                required_job_patterns=["riscv64"], expected_wheels=[],
                                produced_wheels=[])


class InterruptedJobs(unittest.TestCase):
    """A required job that was cancelled or lost its runner did not execute:
    CI on the head is `not_executed` and names it; an executed failure of
    another job still fails the head."""

    def test_cancelled_and_runner_lost_jobs_are_interrupted(self):
        for job in (_job(1, "build riscv64", "cancelled"),
                    _job(1, "build riscv64", "failure", runner_lost=True)):
            with self.subTest(conclusion=job["conclusion"]):
                v = _verdict([job])
                self.assertEqual((v.state, [j["id"] for j in v.interrupted_jobs], v.attempts),
                                 ("not_executed", [1], {7: 2}))
                record = _record([job])
                schemas.validate("evidence", record)
                self.assertEqual(record["ci"]["jobs"][0]["executed"], False)
                self.assertEqual(record["ci"]["jobs"][0].get("interrupted"),
                                 True if "runner_lost" in job else None)
        # A timed-out job executed: it is a failure.
        v = _verdict([_job(1, "build riscv64", "timed_out")])
        self.assertEqual((v.state, v.interrupted_jobs), ("failed", []))
        # A real failure wins over an interrupted job.
        v = _verdict([_job(1, "build riscv64", "cancelled"),
                      _job(2, "test riscv64", "failure", runner_lost=False)])
        self.assertEqual(v.state, "failed")

    def test_a_blocking_job_that_lost_its_runner_never_passes(self):
        """A job no required pattern names that failed after it lost its
        runner is interrupted: the head is not passed, so it is never
        nominated while the gate, reading the same CI unclassified, calls it
        failed. A publishing job the fork has no credentials for stays out."""
        lost = _job(2, "lint", "failure", runner_lost=True)
        v = _verdict([_job(1, "build riscv64", "success"), lost])
        self.assertEqual((v.state, [j["id"] for j in v.interrupted_jobs]), ("not_executed", [2]))
        self.assertEqual(_verdict([_job(1, "build riscv64", "success"),
                                   dict(lost, runner_lost=False)]).state, "failed")
        v = _verdict([_job(1, "build riscv64", "success"),
                      _job(2, "upload to pypi", "failure", runner_lost=True)])
        self.assertEqual(v.state, "passed")

    def test_without_classification_the_verdict_and_evidence_are_unchanged(self):
        """The gate, CI on the clean head, and upstream review read CI
        without classification: their verdicts and evidence are exactly
        what they were before the trusted rerun."""
        jobs = [_job(1, "build riscv64", "failure"), _job(2, "test riscv64", "cancelled"),
                _job(3, "lint", "success")]
        v = _verdict(jobs)
        self.assertEqual((v.state, v.problems),
                         ("failed", ["job 'build riscv64' failed (failure)",
                                     "job 'build riscv64' failed (failure)",
                                     "job 'test riscv64' did not execute (cancelled)"]))
        record = _record(jobs)
        self.assertEqual(json.dumps(record["ci"]["jobs"], sort_keys=True), json.dumps([
            {"job_id": 1, "name": "build riscv64", "status": "completed", "conclusion": "failure",
             "executed": True, "runner_labels": []},
            {"job_id": 2, "name": "test riscv64", "status": "completed",
             "conclusion": "cancelled", "executed": False, "runner_labels": []},
            {"job_id": 3, "name": "lint", "status": "completed", "conclusion": "success",
             "executed": True, "runner_labels": []}], sort_keys=True))


if __name__ == "__main__":
    unittest.main()
