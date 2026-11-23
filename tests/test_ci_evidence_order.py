# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
import unittest

import ci_evidence


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


if __name__ == "__main__":
    unittest.main()
