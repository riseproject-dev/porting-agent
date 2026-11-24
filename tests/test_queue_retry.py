# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A worker run that ends without a Result leaves its task to the queue's
retry, which dispatch recovery neither charges nor mistakes for a lost
dispatch (luhenry--d2vm/3, worker run 45257915183 stopped by its timeout)."""

import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import queue_result
import reconcile
import tasks
from tests.helpers import contribution, policy

DEDUPE = "luhenry--d2vm/3@g1.r3:development_patch"
REQUESTED = "2026-11-23T20:13:29.927419+00:00"
# The cancelled Claim of worker run 45257915183 allows the next attempt from
# 22:20:43.206 (its `retry_not_before`).
RETRY_MS = 1795472443206
RETRY_AT = datetime.fromtimestamp(RETRY_MS / 1000, tz=timezone.utc)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def run(run_id: int, created: str, status: str = "completed", conclusion: str = "success") -> dict:
    return {"id": run_id, "status": status, "conclusion": conclusion, "created_at": created,
            "updated_at": created}


def projection(state: str = "available", attempts: int = 1, retry=RETRY_MS) -> dict:
    graph = tasks.graph_id(DEDUPE)
    item = {"kind": "Work", "work_id": "b7ec5153", "graph_id": graph, "pool": "default",
            "state": state, "attempts": attempts}
    if retry is not None:
        item["retry_not_before"] = retry
    return {"policy": {"pools": {"default": {"retry": {"max_attempts": 3, "backoff_ms": 30000}}}},
            "works": {"b7ec5153": item}}


class TestWorkRetries(unittest.TestCase):
    def test_a_cancelled_claim_leaves_available_task_to_retry(self):
        retries = queue_result.work_retries(projection())
        self.assertEqual(retries, {tasks.graph_id(DEDUPE): {
            "attempts": 1, "max_attempts": 3, "retry_at": RETRY_AT}})

    def test_only_available_task_after_a_claim_is_a_retry(self):
        for state, attempts, retry in (("claimed", 2, RETRY_MS), ("available", 0, None),
                                       ("cancelled", 3, RETRY_MS), ("available", 1, None),
                                       ("available", True, RETRY_MS)):
            with self.subTest(state=state, attempts=attempts, retry=retry):
                self.assertEqual(queue_result.work_retries(projection(state, attempts, retry)), {})
        self.assertEqual(queue_result.work_retries(None), {})

    def test_a_retry_time_out_of_range_skips_that_task_only(self):
        # datetime.fromtimestamp refuses a time past year 9999: that one
        # task is left out and reported, and the others are still read.
        p = projection()
        p["works"]["far"] = dict(p["works"]["b7ec5153"], work_id="far", graph_id="porting:far",
                                 retry_not_before=10 ** 20)
        skipped: list[str] = []
        retries = queue_result.work_retries(p, skipped)
        self.assertEqual(list(retries), [tasks.graph_id(DEDUPE)])
        self.assertEqual(len(skipped), 1)
        self.assertIn("porting:far", skipped[0])
        self.assertIn(str(10 ** 20), skipped[0])
        self.assertEqual(list(queue_result.work_retries(p)), [tasks.graph_id(DEDUPE)])


class TestDispatchRecoveryOfAQueueRetry(unittest.TestCase):
    def setUp(self):
        self.record = contribution(phase="development", activity="agent_running", revision=3,
                                   contribution_id="luhenry--d2vm/3")
        self.record["generation"] = 1
        self.record["active_task"] = {
            "dedupe_key": DEDUPE, "task": "development_patch", "revision": 3, "generation": 1,
            "attempt": 1, "requested_at": REQUESTED,
            "deadline_at": "2026-11-24T02:13:29.927419+00:00",
            "dispatched_run_id": None, "reason_ids": ["plan-luhenry--d2vm--3-r2"],
            "feedback_evidence_ids": []}
        # The dispatcher run that submitted the task and launched worker run
        # 45257915183, which ran until its job timeout (then 120 minutes).
        self.runs = [run(45257187396, "2026-11-23T20:13:50Z")]

    def recover(self, now, runs=None, retries=True, record=None):
        graph = tasks.graph_id(DEDUPE)
        p = projection()
        return reconcile.dispatch_recovery(
            {"c": copy.deepcopy(record or self.record)}, runs=runs or self.runs,
            queue_works={graph: "available"}, now=at(now), policy=policy(),
            queue_retries=queue_result.work_retries(p) if retries else None)

    def test_the_retry_is_dispatched_once_the_backoff_passed_and_charges_nothing(self):
        relaunch, staged, report = self.recover(
            (RETRY_AT + timedelta(seconds=1)).isoformat())
        self.assertEqual(staged, [])
        self.assertEqual(relaunch, [{"type": "relaunch_dispatcher",
                                     "contribution_id": "luhenry--d2vm/3"}])
        self.assertIn("the queue retries the task", report[0]["decision"])
        self.assertEqual(report[0]["queue_retry"]["attempts"], 1)
        self.assertEqual(report[0]["dispatcher_runs"], [])

    def test_a_dispatcher_run_in_flight_before_the_retry_is_waited_for(self):
        # A run created before the retry time that has not ended claims the
        # available task when it stages: nothing is launched or charged.
        runs = self.runs + [run(45278935001, (RETRY_AT - timedelta(minutes=2)).isoformat(),
                                status="in_progress")]
        relaunch, staged, report = self.recover(
            (RETRY_AT + timedelta(minutes=5)).isoformat(), runs=runs)
        self.assertEqual((relaunch, staged), ([], []))
        self.assertEqual(report[0]["decision"], "dispatcher running")

    def test_a_dispatcher_run_stuck_for_an_hour_does_not_block_recovery(self):
        created = RETRY_AT - timedelta(minutes=2)
        runs = self.runs + [run(45278935001, created.isoformat(), status="queued")]
        # Within the hour it is waited for.
        _relaunch, staged, report = self.recover(
            (created + timedelta(minutes=59)).isoformat(), runs=runs)
        self.assertEqual((staged, report[0]["decision"]), ([], "dispatcher running"))
        # Past it, the run no longer holds the task: it is launched again.
        relaunch, staged, report = self.recover(
            (created + reconcile.DISPATCHER_RUN_WAIT).isoformat(), runs=runs)
        self.assertEqual((len(relaunch), staged), (1, []))

    def test_a_lost_relaunch_is_relaunched_at_once(self):
        # The run decided last tick is still the latest: the relaunch it led
        # to is not listed, so it was lost.
        ended = run(45278935001, (RETRY_AT + timedelta(seconds=5)).isoformat(),
                    conclusion="failure")
        record = copy.deepcopy(self.record)
        record["active_task"]["dispatched_run_id"] = "45278935001"
        relaunch, staged, report = self.recover(
            (RETRY_AT + timedelta(seconds=40)).isoformat(), runs=self.runs + [ended],
            record=record)
        self.assertEqual(staged, [])
        self.assertEqual(len(relaunch), 1)
        self.assertEqual(report[0]["decision"], "relaunch: the requested run did not start")

    def test_without_the_retry_the_live_tick_charged_a_lost_dispatch(self):
        # What controller run 45278754335 decided: `retry_dispatch`, attempt
        # 2 of 3, for a task the queue was about to retry anyway.
        _relaunch, staged, report = self.recover("2026-11-23T22:21:19+00:00", retries=False)
        self.assertEqual([r["kind"] for kind, r in staged if kind == "events"], ["retry_dispatch"])
        self.assertIn("dispatch attempt 2 of 3", report[0]["decision"])

    def test_nothing_is_launched_before_the_backoff(self):
        relaunch, staged, report = self.recover(
            (RETRY_AT - timedelta(seconds=10)).isoformat())
        self.assertEqual((relaunch, staged), ([], []))
        self.assertEqual(report[0]["decision"], "waiting for the queue's retry backoff")

    def test_runs_before_the_retry_are_not_counted(self):
        # Two more dispatcher runs ended before the next worker run was
        # stopped: they never count against the retry.
        runs = self.runs + [run(45278935001, "2026-11-23T22:21:46Z", conclusion="failure"),
                            run(45287416687, "2026-11-23T23:16:55Z", conclusion="failure")]
        later = projection(attempts=2, retry=int(at("2026-11-24T00:30:00+00:00").timestamp() * 1000))
        relaunch, staged, report = reconcile.dispatch_recovery(
            {"c": copy.deepcopy(self.record)}, runs=runs,
            queue_works={tasks.graph_id(DEDUPE): "available"},
            now=at("2026-11-24T00:46:00+00:00"), policy=policy(),
            queue_retries=queue_result.work_retries(later))
        self.assertEqual(staged, [])
        self.assertEqual(len(relaunch), 1)
        self.assertEqual(report[0]["queue_retry"]["attempts"], 2)
        # Without the retry those three runs would exhaust the recovery.
        _relaunch, staged, _report = reconcile.dispatch_recovery(
            {"c": copy.deepcopy(self.record)}, runs=runs,
            queue_works={tasks.graph_id(DEDUPE): "available"},
            now=at("2026-11-24T00:46:00+00:00"), policy=policy())
        self.assertEqual([r["kind"] for kind, r in staged if kind == "events"],
                         ["exhaust_recovery"])

    def test_the_launched_dispatcher_is_awaited(self):
        runs = self.runs + [run(45278935001, "2026-11-23T22:21:46Z", status="in_progress")]
        relaunch, staged, report = self.recover("2026-11-23T22:22:30+00:00", runs=runs)
        self.assertEqual((relaunch, staged), ([], []))
        self.assertEqual(report[0]["decision"], "dispatcher running")

    def test_a_retry_no_dispatcher_claims_is_charged_and_explained(self):
        runs = self.runs + [run(45278935001, "2026-11-23T22:21:46Z", conclusion="failure")]
        relaunch, staged, report = self.recover("2026-11-23T22:40:00+00:00", runs=runs)
        events = [r for kind, r in staged if kind == "events"]
        self.assertEqual([e["kind"] for e in events], ["retry_dispatch"])
        self.assertEqual(events[0]["details"]["queue_retry"], {"attempts": 1, "max_attempts": 3})
        self.assertIn("ended without a Result", events[0]["details"]["why"])
        self.assertIn("dispatch attempt 2 of 3", report[0]["decision"])
        self.assertEqual(len(relaunch), 1)

    def test_a_claimed_retry_is_left_to_the_worker(self):
        relaunch, staged, report = reconcile.dispatch_recovery(
            {"c": copy.deepcopy(self.record)}, runs=self.runs,
            queue_works={tasks.graph_id(DEDUPE): "claimed"},
            now=at("2026-11-23T23:30:00+00:00"), policy=policy(),
            queue_retries=queue_result.work_retries(projection("claimed", 2)))
        self.assertEqual((relaunch, staged, report), ([], [], []))


class TestDispatcherStaging(unittest.TestCase):
    def test_the_dispatcher_stages_through_the_queue_check(self):
        # gh-aw refuses a second payload for one task id: the plan is staged
        # by the step that leaves queued Work out (tested against gh-aw
        # v1.2.2 in scripts/gh-aw-compat/tests/work_queue_stage.test.cjs).
        root = Path(__file__).parents[1]
        for name in ("dispatcher.md", "dispatcher.lock.yml"):
            with self.subTest(name=name):
                text = (root / ".github/workflows" / name).read_text()
                self.assertIn("scripts/work_queue_stage.cjs", text)
                self.assertNotIn("stagePlanIntents", text)

    def test_the_dispatcher_keeps_only_the_staged_intents_after_the_agent(self):
        # The agent's work-queue tools append to the staged intents; the
        # steps after the agent keep only those of the stage's output.
        lock = yaml.safe_load(
            (Path(__file__).parents[1] / ".github/workflows/dispatcher.lock.yml").read_text())
        steps = lock["jobs"]["agent"]["steps"]
        names = [step.get("name", "") for step in steps]
        at = names.index
        copy_step = steps[at("Copy the trusted intent check out of the agent's reach")]
        stage = steps[at("Stage the plan as trusted queue intents")]
        agent = at("Execute Pi CLI")
        keep = at("Keep only the intents the trusted stage wrote")
        uploads = [i for i, step in enumerate(steps) if "upload-artifact" in step.get("uses", "")]
        check = '"${RUNNER_TEMP}/gh-aw/trusted-intent-check/work_queue_stage.cjs"'
        # The check is copied from the workspace before the agent runs, into
        # the directory the agent's sandbox mounts read-only, and both the
        # stage and the step after the agent load it from there.
        self.assertLess(at("Checkout repository (gh-aw default)"), names.index(copy_step["name"]))
        self.assertLess(names.index(copy_step["name"]), names.index(stage["name"]))
        self.assertLess(names.index(stage["name"]), agent)
        self.assertIn(f"install -D -m 0444 scripts/work_queue_stage.cjs \\\n  {check}\n", copy_step["run"])
        awf = steps[agent]["run"]
        self.assertIn('--mount "${RUNNER_TEMP}/gh-aw:${RUNNER_TEMP}/gh-aw:ro"', awf)
        self.assertNotIn('${RUNNER_TEMP}/gh-aw:rw', awf)
        self.assertNotRegex(awf, r"\$\{RUNNER_TEMP\}/gh-aw/[^:\s\"]*:[^:\s\"]*:rw")
        loaded = 'require(path.join(process.env.RUNNER_TEMP, "gh-aw/trusted-intent-check/work_queue_stage.cjs"))'
        self.assertIn(loaded, stage["with"]["script"])
        script = steps[keep]["with"]["script"]
        self.assertIn(loaded, script)
        self.assertEqual(steps[keep]["env"]["TRUSTED_INTENTS"], "${{ steps.stage.outputs.intents }}")
        # One github-script step, after every step that collects intents
        # and before every upload, runs the check; it runs no shell, so no
        # command is looked up on PATH.
        collected = [at("Collect work queue intents"), at("Copy work queue claim finish intent")]
        self.assertLess(max([agent, *collected]), keep)
        self.assertTrue(uploads)
        self.assertLess(keep, min(uploads))
        self.assertEqual(steps[keep]["if"], "always()")
        self.assertNotIn("continue-on-error", steps[keep])
        self.assertNotIn("run", steps[keep])
        # The uploads are gated by gh-aw's final recovery check, which runs
        # after the check and refuses a link out of /tmp/gh-aw: the tripwire
        # the check plants when it fails.
        final = at("Validate recovery envelopes before upload", keep)
        self.assertLess(keep, final)
        for i in uploads:
            if steps[i]["with"]["name"] in ("agent", "agent-output-fallback"):
                self.assertIn("steps.validate_final_recovery_envelopes.outcome == 'success'", steps[i]["if"])
        self.assertIn('fs.symlinkSync("/", name)', script)
        self.assertIn('fs.readlinkSync(name) === "/"', script)
        self.assertIn("randomBytes(16)", script)
        # After the agent nothing is checked out, and no step of ours runs
        # git or loads code from the workspace the agent could write. The
        # only git after the agent is gh-aw's own credential step, which
        # reads no code from the workspace.
        for step in steps[agent + 1:]:
            with self.subTest(step=step.get("name")):
                self.assertNotIn("actions/checkout", step.get("uses", ""))
                body = step.get("run", "") + step.get("with", {}).get("script", "")
                self.assertNotIn("GITHUB_WORKSPACE", body)
                if step.get("name") != "Configure Git credentials":
                    self.assertNotRegex(body, r"(^|[\s;&|(])git\s")
                else:
                    self.assertEqual(body, 'bash "${RUNNER_TEMP}/gh-aw/actions/configure_git_credentials.sh"')

    def run_failed_check(self, root: Path, prelude: str = "") -> subprocess.CompletedProcess:
        # The check's module is missing, so the step takes its failure path.
        step = next(s for s in dispatcher_agent_steps()
                    if s.get("name") == "Keep only the intents the trusted stage wrote")
        harness = (
            "const core = { setFailed: m => { console.log('FAILED ' + m); process.exitCode = 1; } };\n"
            + prelude
            + step["with"]["script"].replace('"/tmp/gh-aw', json.dumps(str(root))[:-1])
            .replace("`/tmp/gh-aw", "`" + str(root))
        )
        return subprocess.run(
            ["node", "-e", harness], capture_output=True, text=True,
            env={**os.environ, "RUNNER_TEMP": str(root.parent / "runner-temp"), "TRUSTED_INTENTS": ""},
        )

    @staticmethod
    def tripwires(root: Path) -> list[Path]:
        return [p for p in root.iterdir() if p.name.startswith("work-queue.unchecked-") and p.is_symlink()]

    def test_a_check_that_cannot_run_keeps_the_collected_intents_from_upload(self):
        # The step removes the collected files, plants the tripwire that
        # fails gh-aw's final recovery check (tested against gh-aw v1.2.2 in
        # work_queue_stage.test.cjs), and fails. The agent cannot take the
        # tripwire's name first: a locked directory at the former fixed name
        # changes nothing.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "gh-aw"
            root.mkdir()
            (root / "work-queue.intents.jsonl").write_text('{"kind":"submit"}\n')
            (root / "work-queue.finish.jsonl").mkdir()
            squatted = root / "work-queue.unchecked"
            squatted.mkdir()
            (squatted / "keep").write_text("")
            squatted.chmod(0o555)
            try:
                result = self.run_failed_check(root)
            finally:
                squatted.chmod(0o755)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("none are uploaded", result.stdout)
            self.assertFalse((root / "work-queue.intents.jsonl").exists())
            self.assertFalse((root / "work-queue.finish.jsonl").exists())
            [tripwire] = self.tripwires(root)
            self.assertRegex(tripwire.name, r"^work-queue\.unchecked-[0-9a-f]{32}$")
            self.assertEqual(os.readlink(tripwire), "/")

    def test_the_tripwire_falls_back_to_another_random_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "gh-aw"
            root.mkdir()
            prelude = ('const realSymlink = require("node:fs").symlinkSync; let calls = 0;\n'
                       'require("node:fs").symlinkSync = (...a) => { if (calls++ === 0) '
                       '{ throw Object.assign(new Error("EEXIST"), { code: "EEXIST" }); } return realSymlink(...a); };\n')
            result = self.run_failed_check(root, prelude)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("none are uploaded", result.stdout)
            self.assertEqual(len(self.tripwires(root)), 1)

    def test_a_tripwire_that_cannot_be_planted_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "gh-aw"
            root.write_text("not a directory")
            result = self.run_failed_check(root)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertNotIn("none are uploaded", result.stdout)
            self.assertIn("no link could be planted", result.stdout)
            self.assertIn("must not be trusted", result.stdout)


def dispatcher_agent_steps() -> list:
    lock = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/dispatcher.lock.yml").read_text())
    return lock["jobs"]["agent"]["steps"]

if __name__ == "__main__":
    unittest.main()
