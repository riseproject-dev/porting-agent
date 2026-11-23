# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The trusted CI rerun (``ci_rerun``): what is rerun, the request and its
readback, the operation and its discovery, the checks before any token, and
the outcome the publication job delivers."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import ci_evidence
import ci_rerun
import collect
import engine
import recovery
import schemas
from tests.helpers import NOW, SHA_A, SHA_B, contribution, policy

FIXTURES = Path(__file__).parent / "fixtures"
FORK = "riseproject-upstreaming/proj"
POLICY = policy()
WORK = {"dedupe_key": "example--proj/12@g0.r0:development_repair", "task": "development_repair",
        "revision": 0, "attempt": 1, "requested_at": NOW,
        "deadline_at": "2026-11-24T09:00:00+00:00", "dispatched_run_id": None, "reason_ids": [],
        "generation": 0, "feedback_evidence_ids": []}


def run(run_id, attempt=1, status="completed", head=SHA_A, conclusion="failure"):
    return {"id": run_id, "head_sha": head, "run_attempt": attempt, "status": status,
            "conclusion": conclusion, "workflow_id": 1, "event": "pull_request",
            "path": ".github/workflows/ci.yml"}


def job(job_id, run_id, conclusion="failure", name="Build riscv64", **extra):
    return {"id": job_id, "run_id": run_id, "name": name, "status": "completed",
            "conclusion": conclusion, "started_at": "x", "labels": [], **extra}


def snap(runs, jobs):
    return collect.CiSnapshot(repository={"id": 2, "full_name": FORK}, head_sha=SHA_A,
                              runs=runs, jobs_by_run=jobs)


class FakeApi:
    """The fork as the rerun reads and writes it."""

    def __init__(self, runs, *, refuse=None, land=True, head=SHA_A):
        self.runs = {r["id"]: dict(r) for r in runs}
        self.refuse, self.land, self.head = refuse, land, head
        self.posts: list[str] = []

    def get(self, path):
        if path.startswith(f"/repos/{FORK}/actions/runs/"):
            return copy.deepcopy(self.runs[int(path.rsplit("/", 1)[1])])
        if path == f"/repos/{FORK}/branches/riscv64-dev":
            return {"name": "riscv64-dev", "commit": {"sha": self.head,
                                                    "commit": {"tree": {"sha": "t" * 40}},
                                                    "parents": []}}
        if path == f"/repos/{FORK}":
            return {"id": 2, "full_name": FORK, "default_branch": "main", "fork": True,
                    "parent": {"full_name": "example/proj", "id": 1}}
        raise collect.CollectError(f"GET {path}: HTTP 404")

    def paginate(self, path, *, key=None, max_pages=10):
        if path.startswith(f"/repos/{FORK}/actions/runs?head_sha="):
            return [copy.deepcopy(r) for r in self.runs.values()]
        if "/jobs" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            return [job(run_id * 10, run_id)]
        raise collect.CollectError(f"GET {path}: HTTP 404")

    def mutate(self, method, path):
        self.posts.append(path)
        if self.refuse:
            raise urllib.error.HTTPError(path, self.refuse, "refused", {}, None)
        if self.land:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            self.runs[run_id]["run_attempt"] += 1
            self.runs[run_id]["status"] = "queued"


class TestTargets(unittest.TestCase):
    def test_flaky_and_interrupted_targets(self):
        s = snap([run(5), run(3, attempt=2), run(4, conclusion="success"), run(9, head=SHA_B)],
                 {5: [job(50, 5)], 3: [job(30, 3, "timed_out")],
                  4: [job(40, 4, "success")], 9: [job(90, 9)]})
        runs, in_progress = ci_rerun.targets(s, SHA_A, interrupted_only=False)
        self.assertEqual(runs, [{"run_id": 3, "from_attempt": 2}, {"run_id": 5, "from_attempt": 1}])
        self.assertFalse(in_progress)
        # Only a cancelled job, or one that lost its runner, is interrupted.
        s.jobs_by_run[5] = [job(50, 5, "cancelled")]
        s.jobs_by_run[3] = [job(30, 3, runner_lost=True)]
        runs, _ = ci_rerun.targets(s, SHA_A, interrupted_only=True)
        self.assertEqual([r["run_id"] for r in runs], [3, 5])
        s.jobs_by_run[3] = [job(30, 3, "timed_out")]
        runs, _ = ci_rerun.targets(s, SHA_A, interrupted_only=True)
        self.assertEqual([r["run_id"] for r in runs], [5])

    def test_in_progress_publishing_jobs_and_the_cap(self):
        s = snap([run(1, status="in_progress", conclusion=None), run(2)],
                 {1: [], 2: [job(20, 2, name="upload_pypi")]})
        runs, in_progress = ci_rerun.targets(s, SHA_A, interrupted_only=False)
        self.assertEqual((runs, in_progress), ([], True))
        many = snap([run(i) for i in range(1, 9)], {i: [job(i * 10, i)] for i in range(1, 9)})
        runs, _ = ci_rerun.targets(many, SHA_A, interrupted_only=False)
        self.assertEqual([r["run_id"] for r in runs], [1, 2, 3, 4, 5])
        # The latest attempt of a run is what is rerun.
        s = snap([run(7, attempt=1), run(7, attempt=3)], {7: [job(70, 7)]})
        runs, _ = ci_rerun.targets(s, SHA_A, interrupted_only=False)
        self.assertEqual(runs, [{"run_id": 7, "from_attempt": 3}])


class TestRequest(unittest.TestCase):
    def request(self, api, runs):
        return ci_rerun.request(api, FORK, SHA_A, runs, sleep=lambda _s: None)

    def test_a_rerun_is_requested_and_read_back(self):
        api = FakeApi([run(5), run(6)])
        receipt = self.request(api, [{"run_id": 5, "from_attempt": 1},
                                     {"run_id": 6, "from_attempt": 1}])
        self.assertEqual(receipt, {"status": "rerun", "runs": [
            {"run_id": 5, "from_attempt": 1, "run_attempt": 2},
            {"run_id": 6, "from_attempt": 1, "run_attempt": 2}]})
        self.assertEqual(api.posts, [f"/repos/{FORK}/actions/runs/5/rerun-failed-jobs",
                                     f"/repos/{FORK}/actions/runs/6/rerun-failed-jobs"])

    def test_a_landed_rerun_is_never_requested_again(self):
        api = FakeApi([run(5, attempt=2)])
        receipt = self.request(api, [{"run_id": 5, "from_attempt": 1}])
        self.assertEqual((receipt["status"], api.posts), ("rerun", []))

    def test_stale_refused_and_unconfirmed(self):
        self.assertEqual(self.request(FakeApi([run(5, head=SHA_B)]),
                                      [{"run_id": 5, "from_attempt": 1}])["status"], "stale")
        for code in (403, 404, 422):
            api = FakeApi([run(5)], refuse=code)
            self.assertEqual(self.request(api, [{"run_id": 5, "from_attempt": 1}])["status"],
                             "refused")
        api = FakeApi([run(5)], refuse=502)
        self.assertEqual(self.request(api, [{"run_id": 5, "from_attempt": 1}])["status"],
                         "unconfirmed")
        api = FakeApi([run(5)], land=False)
        self.assertEqual(self.request(api, [{"run_id": 5, "from_attempt": 1}])["status"],
                         "unconfirmed")

    def test_the_api_admits_only_the_one_write(self):
        api = ci_rerun.Api(token="t", opener=lambda *_a, **_k: self.fail("no request"))
        for method, path in (("POST", f"/repos/{FORK}/actions/runs/5/rerun"),
                             ("POST", f"/repos/{FORK}/actions/runs/5/cancel"),
                             ("DELETE", f"/repos/{FORK}/actions/runs/5/rerun-failed-jobs"),
                             ("POST", f"/repos/{FORK}/actions/runs/../5/rerun-failed-jobs"),
                             ("POST", f"/repos/{FORK}/actions/jobs/5/rerun"),
                             ("POST", f"/repos/{FORK}/pulls")):
            with self.subTest(path=path), self.assertRaises(ci_rerun.RerunError):
                api.mutate(method, path)
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def read(self, _n):
                return b""

        reader = FakeApi([run(5)])
        api = ci_rerun.Api(token="t", reader=reader,
                           opener=lambda req, timeout: calls.append(req) or Response())
        api.mutate("POST", f"/repos/{FORK}/actions/runs/5/rerun-failed-jobs")
        # Reads go through the reader, never with the fork token.
        self.assertEqual(api.get(f"/repos/{FORK}/actions/runs/5")["id"], 5)
        self.assertEqual(len(api.paginate(f"/repos/{FORK}/actions/runs?head_sha={SHA_A}")), 1)
        self.assertEqual([(r.get_method(), r.full_url) for r in calls],
                         [("POST", f"https://api.github.com/repos/{FORK}/actions/runs/5"
                                   "/rerun-failed-jobs")])


class TestOperation(unittest.TestCase):
    def op(self):
        return ci_rerun.operation(contribution(), SHA_A, [{"run_id": 5, "from_attempt": 1},
                                                          {"run_id": 6, "from_attempt": 2}],
                                  ["ci-2-6-2", "rerun:0"], POLICY, NOW)

    def test_discovery_needs_every_run_above_its_attempt_on_the_head(self):
        op = self.op()
        self.assertEqual(op["proposed"]["marker"], "ci-rerun:5@1,6@2")
        self.assertEqual(ci_rerun.marker_runs(op), [{"run_id": 5, "from_attempt": 1},
                                                    {"run_id": 6, "from_attempt": 2}])
        new = [{"id": 5, "run_attempt": 2, "head_sha": SHA_A},
               {"id": 6, "run_attempt": 3, "head_sha": SHA_A}]
        self.assertEqual(recovery.DISCOVERERS["ci_rerun"](op, new), "confirmed")
        self.assertEqual(ci_rerun.discover(op, new[:1]), "absent")
        self.assertEqual(ci_rerun.discover(op, [new[0], dict(new[1], run_attempt=2)]), "absent")
        self.assertEqual(ci_rerun.discover(op, [new[0], dict(new[1], head_sha=SHA_B)]),
                         "absent")
        # The launch window of a dispatch cannot confirm a rerun.
        self.assertIsNot(recovery.DISCOVERERS["ci_rerun"], recovery.discover_dispatch)

    def test_a_snapshot_behind_a_confirmed_rerun_is_awaited(self):
        op = recovery.fold_receipt(self.op(), external_ids={"attempt:5": 2, "attempt:6": 3},
                                   detail="read back", now=NOW)
        ops = {op["operation_id"]: op}
        cid = op["contribution_id"]
        self.assertTrue(ci_rerun.awaited(ops, cid, SHA_A, {5: 1, 6: 3}))
        self.assertFalse(ci_rerun.awaited(ops, cid, SHA_A, {5: 2, 6: 3}))
        self.assertFalse(ci_rerun.awaited(ops, cid, SHA_A, {7: 1}))
        self.assertFalse(ci_rerun.awaited(ops, cid, SHA_B, {5: 1}))
        self.assertFalse(ci_rerun.awaited(ops, "other", SHA_A, {5: 1}))
        failed = recovery.fold_failure_receipt(self.op(), external_ids={}, now=NOW)
        self.assertFalse(ci_rerun.awaited({failed["operation_id"]: failed}, cid, SHA_A, {5: 1}))


class TestRunnerLoss(unittest.TestCase):
    """The annotations GitHub writes on a job whose runner was lost, and
    others that are not a runner loss."""

    def test_the_fixture_annotations(self):
        fixture = json.loads((FIXTURES / "runner-lost-annotations.json").read_text())
        for message in fixture["runner_lost"]:
            self.assertTrue(ci_rerun.runner_lost(message), message)
        for message in fixture["not_runner_lost"]:
            self.assertFalse(ci_rerun.runner_lost(message), message)
        self.assertFalse(ci_rerun.runner_lost(None))

    def test_the_snapshot_classifies_failed_jobs_by_failure_annotations(self):
        fixture = json.loads((FIXTURES / "runner-lost-annotations.json").read_text())
        reads = []

        class Api:
            def paginate(self, path, *, key=None, max_pages=10):
                reads.append(path)
                if path.startswith(f"/repos/{FORK}/check-runs/1/"):
                    return [{"annotation_level": "failure", "message": fixture["runner_lost"][0]}]
                if path.startswith(f"/repos/{FORK}/check-runs/2/"):
                    return [{"annotation_level": "warning", "message": fixture["runner_lost"][0]},
                            {"annotation_level": "failure", "message": "exit code 1"}]
                return []

        s = snap([run(5)], {5: [job(1, 5), job(2, 5), job(3, 5, "cancelled"),
                                job(4, 5, "success")]})
        collect.classify_runner_loss(Api(), FORK, s)
        self.assertEqual([j["runner_lost"] for j in s.jobs_by_run[5]], [True, False, False, False])
        self.assertEqual(len(reads), 2)
        self.assertFalse(ci_evidence.job_executed(s.jobs_by_run[5][0]))
        self.assertTrue(ci_evidence.job_interrupted(s.jobs_by_run[5][0]))


def no_change_proposal(record, **over):
    p = {"schema_version": 1, "contribution_id": record["contribution_id"], "outcome": "no_change",
         "expected_revision": 0, "expected_head": SHA_A, "operation": "none",
         "diagnosis": "the runner image lost its network", "progress": "none",
         "changed_files": [], "patch_digest": None, "ref": "riscv64-dev",
         "reconstruction_base": None, "fork_repository_id": 2, "reason_ids": ["ci-0"]}
    p.update(over)
    return p


def task(**over):
    t = {"contribution_id": "example--proj/12", "revision": 0, "expected_head": SHA_A,
         "fork": FORK, "fork_repository_id": 2, "allowed_operations": ["append"],
         "allowed_paths": ["a"], "reason_ids": ["ci-0"], "ref": "riscv64-dev",
         "task": "development_repair", "generation": 0, "dedupe_key": WORK["dedupe_key"]}
    t.update(over)
    return t


def repair_record(**over):
    return contribution(activity="agent_running", active_work=dict(WORK),
                        last_ci_evidence_id="ci-2-5-1", plan_evidence_id="plan-1",
                        scope_paths=["a"], scope_coverage_complete=True, **over)


class TestPreflight(unittest.TestCase):
    def preflight(self, record=None, t=None, proposal=None, pol=POLICY, lookup=None):
        record = record or repair_record()
        return ci_rerun.preflight(
            record=record, task=t or task(), proposal=proposal or no_change_proposal(record),
            policy=pol, now=NOW,
            fork_lookup=lookup or (lambda name: {"id": 2, "full_name": FORK, "archived": False}))

    def test_a_rerun_needs_every_check(self):
        schemas.validate("proposal", no_change_proposal(repair_record()))
        self.assertEqual(self.preflight(), {"rerun": True, "fork_name": "proj", "reason": None})
        carried = repair_record()
        carried["active_work"]["feedback_evidence_ids"] = ["review-1"]
        exhausted = repair_record(budget=dict(repair_record()["budget"], reruns={
            "head_sha": SHA_A, "flaky": 1, "interrupted": 0}))
        paused = repair_record(paused=True, paused_reason="hold", paused_by="human",
                               paused_at=NOW)
        cases = {
            "feedback": dict(record=carried),
            "exhausted": dict(record=exhausted),
            "paused": dict(record=paused),
            "upstream revision": dict(t=task(task="upstream_revision")),
            "read-only": dict(pol=policy(read_only=True)),
            "patch": dict(proposal=no_change_proposal(repair_record(), outcome="patch")),
            "other fork": dict(lookup=lambda name: {"id": 3, "full_name": FORK}),
            "unreadable fork": dict(lookup=lambda name: (_ for _ in ()).throw(OSError("x"))),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                out = self.preflight(**kwargs)
                self.assertFalse(out["rerun"])
                self.assertEqual(out["fork_name"], "")


class TestPublish(unittest.TestCase):
    def publish(self, api, record=None, t=None):
        with mock.patch.dict("os.environ", {"UPSTREAMING_PUBLISH_ATTEMPT": "1"}):
            return ci_rerun.publish(record=record or repair_record(), task=t or task(), api=api,
                                    policy=POLICY, now=NOW, sleep=lambda _s: None)

    def test_a_rerun_delivers_its_confirmed_operation(self):
        out = self.publish(FakeApi([run(5)]))
        self.assertEqual((out["status"], out["stage"], out["rerun"]["status"]),
                         ("no_change", "ci_rerun", "rerun"))
        op = out["operation"]
        schemas.validate("operation", op)
        self.assertEqual((op["kind"], op["status"], op["external_ids"]["attempt:5"],
                          op["target"]["expected_head"]), ("ci_rerun", "confirmed", 2, SHA_A))
        self.assertIsNone(ci_rerun.receipt_problem(out, repair_record()))

    def test_the_other_outcomes(self):
        out = self.publish(FakeApi([run(5, status="in_progress", conclusion=None)]))
        self.assertEqual((out["status"], out["rerun"]["status"]), ("no_change", "superseded"))
        out = self.publish(FakeApi([run(5, attempt=2)]))
        self.assertEqual((out["status"], out["rerun"]["status"]), ("no_change", "superseded"))
        api = FakeApi([run(5)])
        api.paginate = lambda path, **_k: ([copy.deepcopy(r) for r in api.runs.values()]
                                           if "head_sha=" in path else [job(50, 5, "success")])
        out = self.publish(api)
        self.assertEqual((out["status"], out["rerun"]["status"]), ("no_change", "nothing_to_rerun"))
        self.assertIsNone(ci_rerun.receipt_problem(out, repair_record()))
        out = self.publish(FakeApi([run(5)], head=SHA_B))
        self.assertEqual((out["status"], out["failure_class"]), ("rejected", "stale"))
        out = self.publish(FakeApi([run(5)]), record=repair_record(paused=True,
                                                                     paused_reason="hold",
                                                                     paused_by="human",
                                                                     paused_at=NOW))
        self.assertEqual((out["status"], out["failure_class"]), ("rejected", "stale"))
        out = self.publish(FakeApi([run(5)], refuse=422))
        self.assertEqual((out["status"], out["failure_class"], out["operation"]["status"]),
                         ("infra_error", "infrastructure", "failed"))
        out = self.publish(FakeApi([run(5)], land=False))
        self.assertEqual((out["status"], out["failure_class"]), ("infra_error", "infrastructure"))
        self.assertNotIn("operation", out)
        self.assertTrue(ci_rerun.receipt_problem(out, repair_record()))


class TestOperationRequest(unittest.TestCase):
    """The development PR workflow executes a planned operation only when
    it is the record's current authorized intent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".state/operations").mkdir(parents=True)
        (self.root / ".state/contributions").mkdir(parents=True)
        self.record = contribution()
        self.op = ci_rerun.operation(self.record, SHA_A, [{"run_id": 5, "from_attempt": 1}],
                                     ["ci-2-5-1", "rerun:0"], POLICY, NOW)
        self.record["active_operation_id"] = self.op["operation_id"]
        self.write()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self):
        (self.root / f".state/operations/{self.op['operation_id']}.json").write_text(
            json.dumps(self.op))
        (self.root / ".state/contributions/example--proj--12.json").write_text(
            json.dumps(self.record))

    def test_the_receipt(self):
        out = ci_rerun.run_operation(self.root, self.op["operation_id"], FakeApi([run(5)]),
                                     POLICY, "99", sleep=lambda _s: None)
        self.assertEqual((out["status"], out["runs"], out["ci_rerun_run_id"]),
                         ("rerun", [{"run_id": 5, "from_attempt": 1, "run_attempt": 2}], 99))
        out = ci_rerun.run_operation(self.root, self.op["operation_id"],
                                     FakeApi([run(5)], head=SHA_B), POLICY, "99")
        self.assertEqual(out["status"], "stale")

    def test_only_the_current_intent_runs(self):
        for change in ({"paused": True, "paused_reason": "x", "paused_by": "human",
                        "paused_at": NOW},
                       {"active_operation_id": None}, {"revision": 1},
                       {"phase": "handoff", "activity": "awaiting_submission"}):
            with self.subTest(change=sorted(change)):
                self.record = dict(contribution(active_operation_id=self.op["operation_id"]),
                                   **change)
                self.write()
                with self.assertRaises((ci_rerun.RerunError, schemas.SchemaError)):
                    ci_rerun.run_operation(self.root, self.op["operation_id"],
                                           FakeApi([run(5)]), POLICY, "99")
        self.record = contribution(active_operation_id=self.op["operation_id"])
        self.write()
        with self.assertRaises(ci_rerun.RerunError):
            ci_rerun.run_operation(self.root, self.op["operation_id"], FakeApi([run(5)]),
                                   policy(read_only=True), "99")
        with self.assertRaises(ci_rerun.RerunError):
            ci_rerun.load_request(self.root, "../x", POLICY)


class TestEngineWiring(unittest.TestCase):
    def test_every_rerun_verb_is_taken(self):
        self.assertEqual(engine.OPEN_DECISIONS, {})
        self.assertEqual(engine.UNWIRED_VERBS, frozenset())
        for verb, module in (("request_ci_rerun", "reconcile"),
                             ("rerun_interrupted_ci", "observe_ci"),
                             ("exhaust_interrupted_reruns", "observe_ci")):
            self.assertEqual(engine.PIPELINE_VERBS[verb], module)
        self.assertNotIn("answered_ci_run", engine.BESIDE_ROW)
        self.assertEqual(engine.BESIDE_ROW["ci_rerun_recorded"], ("active_operation_id",))


if __name__ == "__main__":
    unittest.main()
