# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Replay of the live incident of ``luhenry--d2vm/3``: its repair task
(`luhenry--d2vm/3@g1.r10:development_repair`) was never queued, because the
payload carrying its whole task was 15592 bytes against the queue's limit of
16384 (with a safety margin of 15500); the dispatch recovery blamed the 244
byte tracking issue description, charged `dispatch_recovery`, and waited for
a human, while the request had charged `task_total` for a task that never
reached the queue.

The fixtures (``tests/fixtures/live-d2vm-payload``) are the committed record
when the task was requested and when its dispatch recovery waited for a human,
the evidence the task was built from, and events 37 to 41, as the controller
committed them."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import capacity
import engine
import reconcile
import task_build
import task_file
import task_history
import tasks
from common import canonical_json
from tests import helpers
from tests import live_d2vm as live
from tests.helpers import policy

POLICY = policy()
NOW = datetime.fromisoformat("2026-11-24T18:30:00+00:00")
OLD_CONTROL_REF = "c" * 40
LIVE_CONTROL_REPO_ID = "1234567890"
# The version of the pipeline code that carries this change, and the one the
# dispatch failed at (the live wait recorded none).
FIXED = "f" * 64


def old_payload() -> dict:
    """The payload the dispatcher of the incident built for the task: the
    task with the references it named at the pinned control commit, and the
    resource scope and effect contract."""
    record, evidence = live.fresh()
    request = tasks.request_from_active(record)

    def reference(eid: str) -> dict:
        path = f".state/evidence/{eid}.json"
        raw = (live.DIRECTORY / "evidence" / f"{eid}.json").read_bytes()
        return {"repository": tasks.CONTROL_REPO, "ref": OLD_CONTROL_REF, "path": path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "url": f"https://raw.githubusercontent.com/{tasks.CONTROL_REPO}/"
                       f"{OLD_CONTROL_REF}/{path}"}
    paths, context = tasks.payload_inputs(request, record, evidence, plan_reference=reference)
    history = reference(live.HISTORY)
    history.pop("url")
    context["history"] = dict(history, evidence_id=live.HISTORY)
    return tasks.payload(request, record, control_repo_id=LIVE_CONTROL_REPO_ID,
                        allowed_paths=paths, context=context)


def compact(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


class TestTheIncident(unittest.TestCase):
    def test_the_payload_of_the_incident_was_over_the_budget(self):
        """The task at r10 inlined the task: about 15600 bytes, of which the plan
        item of the reason evidence (the proposal and its coverage, copied
        from the plan the pre-agent step fetches in full anyway) took 11088,
        against a safety margin of 15500."""
        payload = old_payload()
        self.assertEqual(len(compact(payload)), 15664)
        [plan] = [i for i in payload["context"]["reason_evidence"] if i["kind"] == "plan"]
        self.assertEqual(len(compact(plan)), 11088)
        self.assertGreater(len(compact(payload)), 15_500)
        # The frozen description was never the cause: 244 bytes as JSON.
        description = payload["context"]["tracking_issue"]["description"]
        self.assertEqual(len(compact(description)) - 2, 244)

    def test_the_task_file_holds_every_field_of_that_payload(self):
        record, evidence = live.fresh()
        data = task_build.build_data(record, evidence,
                                    file_digest=task_build.file_digest_reader(None, evidence))
        old = old_payload()
        for name in ("resource_scope", "effect_contract"):
            old.pop(name)
        references = [old["context"]["history"],
                      *(i["reference"] for i in old["context"]["reason_evidence"]
                        if "reference" in i)]
        for reference in references:
            for name in ("repository", "ref", "url"):
                reference.pop(name, None)
        self.assertEqual(data, old)
        self.assertLess(len(canonical_json(data).encode()), task_build.TASK_FILE_MAX_BYTES)

    def test_the_queue_payload_is_a_reference_of_a_fixed_size(self):
        record, evidence = live.fresh()
        request = tasks.request_from_active(record)
        payload = tasks.queue_payload(
            request, control_repo_id=LIVE_CONTROL_REPO_ID,
            reference=task_file.reference(live.WORK, "a" * 40, "b" * 64))
        size = len(compact(payload))
        self.assertLess(size, 1024)
        self.assertLessEqual(size, tasks.MAX_QUEUE_PAYLOAD_BYTES)
        self.assertEqual(task_build.dry_run(record, evidence), None)

    def test_the_dispatcher_no_longer_skips_the_task_it_skipped(self):
        """With the task file committed beside the history, the dispatcher
        of the incident's task reads it at its own commit and stages the
        reference, which the worker resolves to the task of the incident."""
        record, evidence = live.fresh()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_dir = root / ".state" / "evidence"
            evidence_dir.mkdir(parents=True)
            for eid, item in evidence.items():
                (evidence_dir / f"{eid}.json").write_bytes(task_build.file_bytes(item))
            task = task_build.build(
                record, evidence, policy=POLICY, now="2026-11-24T17:20:00+00:00",
                file_digest=task_build.file_digest_reader(root, evidence))
            (root / task_build.path(task["evidence_id"])).write_bytes(task_build.file_bytes(task))
            evidence[task["evidence_id"]] = task
            snapshot = type("S", (), {"contributions": {"c": record}, "evidence": evidence})
            out = root / "plan.json"
            args = type("A", (), {"root": str(root), "out": str(out),
                                  "control_repo_id": LIVE_CONTROL_REPO_ID})
            with mock.patch("reconcile.load_snapshot", return_value=(None, snapshot)), \
                    mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout="d" * 40)), \
                    mock.patch("builtins.print") as printed:
                self.assertEqual(tasks.cmd_plan(args), 0)
            selection = json.loads(printed.call_args.args[0])
            [node] = json.loads(out.read_text())["nodes"]
            resolved = task_file.resolve(
                node["payload"],
                lambda repository, ref, path: (root / path).read_bytes())
        self.assertEqual(selection["selected"][0]["contribution"], live.CID)
        self.assertNotIn("skipped", selection)
        self.assertLess(len(compact(node["payload"])), 1024)
        self.assertEqual(node["payload"]["task_file"]["ref"], "d" * 40)
        self.assertEqual(resolved["dedupe_key"], live.WORK)
        self.assertEqual(resolved["context"]["history"]["ref"], "d" * 40)
        self.assertEqual(resolved["context"]["reason_evidence"][0]["evidence_id"], live.REVIEW)


def run_tick(record: dict, evidence: dict, *, pipeline=FIXED):
    """One controller tick on the committed state of the incident."""
    snapshot = reconcile.Snapshot({"c": copy.deepcopy(record)}, {}, {}, {}, dict(evidence))
    steering = {"items": [], "omitted": {}}
    with mock.patch("collect.steering", return_value=steering), \
            mock.patch("observe_ci.collect.branch_head",
                       return_value={"head": record["fork"]["head_sha"]}), \
            contextlib.redirect_stderr(io.StringIO()):
        return reconcile.tick(
            helpers.NoReadApi(), snapshot, policy=POLICY, now=NOW, publications=[],
            pressure=capacity.Pressure(ok=True), observe=True, dispatcher_runs=[],
            queue_works={}, work_results={}, pipeline=pipeline)


def staged(out, kind):
    return [r for k, r in out.staged if k == kind]


class TestTheFirstRunAfterTheFix(unittest.TestCase):
    """What the first controller run after the fix lands does with the record
    that waits for a human with `dispatch_failed`."""

    def setUp(self):
        self.record = live.record("df439c0")
        self.evidence = live.evidence()

    def tick(self, record=None, *, pipeline=FIXED):
        return run_tick(record or self.record, self.evidence, pipeline=pipeline)

    staged = staticmethod(staged)

    def test_the_record_is_the_wait_of_the_incident(self):
        record = self.record
        self.assertEqual(engine.state_key(record), "development.waiting_human")
        self.assertEqual((record["pending_human_action"]["kind"],
                          record["pending_human_action"]["counter"],
                          record["budget"]["attempts_used"], record["revision"]),
                         ("dispatch_failed", "dispatch_recovery", 7, 10))
        self.assertNotIn("pipeline_version", record["pending_human_action"])
        self.assertIsNone(record["active_task"])
        events = {e["sequence"]: e for e in live.events()}
        # An event is the history as it was written: it names the budget as it did.
        self.assertEqual(events[38]["details"]["charged"], ["work_total"])
        self.assertEqual(events[41]["kind"], "exhaust_recovery")
        self.assertIn("15601 bytes", events[41]["details"]["why"])

    def test_the_wait_ends_without_a_human_and_the_task_is_requested_again(self):
        out = self.tick()
        events = self.staged(out, "events")
        self.assertEqual([e["kind"] for e in events], ["rearm_dispatch", "request_patch"])
        rearm, request = events
        # The controller took the row a human would have posted.
        self.assertEqual(rearm["cause"]["type"], "tick")
        self.assertEqual(rearm["cause"]["id"], "pipeline:" + FIXED[:12])
        self.assertEqual(rearm["details"]["cause"], "pipeline_changed")
        self.assertTrue(rearm["details"]["automatic"])
        self.assertIsNone(rearm["details"]["failed_at_version"])
        self.assertEqual(rearm["details"]["pipeline_version"], FIXED)
        # The task the lost allocation charged is credited, and the new
        # request charges its own: the net charge is one task.
        self.assertEqual(rearm["details"]["credited"], ["task_total"])
        self.assertEqual(request["details"]["charged"], ["task_total"])
        record = [r for r in self.staged(out, "contributions")][-1]
        self.assertEqual(record["budget"]["attempts_used"], 7)
        self.assertIsNone(record["pending_human_action"])
        self.assertEqual(engine.state_key(record), "development.agent_running")
        self.assertEqual(record["active_task"]["dedupe_key"],
                         "luhenry--d2vm/3@g1.r11:development_repair")
        self.assertEqual(record["active_task"]["charged"], ["task_total"])
        # What the human wait held is kept: the feedback the repair answers
        # and the verified plan.
        self.assertEqual(record["pending_feedback_evidence_ids"], [live.REVIEW])
        self.assertEqual(record["active_task"]["feedback_evidence_ids"], [live.REVIEW])
        self.assertEqual(record["plan_evidence_id"], live.PLAN)
        self.assertEqual(record["revision"], 11)
        # The task is requested again for dispatch.
        [requested] = out.report["controller"]["selected"]
        self.assertEqual(requested["task"], "development_repair")
        # Nothing waits for a human.
        self.assertEqual(out.report["dispatch_recovery"], [])

    def test_the_task_file_and_the_history_are_committed_with_the_request(self):
        out = self.tick()
        record = [r for r in self.staged(out, "contributions")][-1]
        key = record["active_task"]["dedupe_key"]
        evidence = {e["evidence_id"]: e for e in self.staged(out, "evidence")}
        history, task = evidence[task_history.evidence_id(key)], evidence[
            task_build.evidence_id(key)]
        self.assertEqual((history["kind"], task["kind"]), ("work_history", "task"))
        self.assertEqual(task["data"]["context"]["history"]["sha256"], hashlib.sha256(
            task_build.file_bytes(history)).hexdigest())
        self.assertEqual(task["data"]["dedupe_key"], key)
        self.assertEqual(task["data"]["task"], "development_repair")
        self.assertEqual(task["data"]["reason_ids"], [live.REVIEW, live.PLAN])
        kinds = [i["kind"] for i in task["data"]["context"]["reason_evidence"]]
        self.assertEqual(kinds, ["human_relay", "plan"])
        self.assertTrue(task["data"]["review_scope_extension"])
        self.assertEqual(task["data"]["context"]["tracking_issue"]["number"], 3)
        self.assertLess(len(canonical_json(task).encode()), task_build.TASK_FILE_MAX_BYTES)
        [entry] = out.report["work_task"]
        self.assertEqual((entry["work"], entry["evidence_id"]), (key, task["evidence_id"]))
        # The reference the dispatcher stages for it is within the bound.
        request = tasks.request_from_active(record)
        payload = tasks.queue_payload(
            request, control_repo_id=LIVE_CONTROL_REPO_ID,
            reference=task_file.reference(key, "a" * 40, "b" * 64))
        self.assertLess(len(compact(payload)), 1024)
        # The history and the task file are what the wait never had: the
        # earlier ones are the task of the incident's.
        self.assertIn(live.HISTORY, self.evidence)
        self.assertNotIn(task_history.evidence_id(key), self.evidence)

    def test_a_wait_that_recorded_this_version_of_the_code_takes_nothing(self):
        record = copy.deepcopy(self.record)
        record["pending_human_action"]["pipeline_version"] = FIXED
        out = self.tick(record)
        self.assertEqual(self.staged(out, "events"), [])
        self.assertEqual(self.staged(out, "contributions"), [])
        self.assertEqual(out.report["controller"]["selected"], [])

    def test_an_unknown_version_of_the_code_takes_nothing(self):
        out = self.tick(pipeline=None)
        self.assertEqual(self.staged(out, "events"), [])

    def test_a_failure_at_the_new_version_waits_for_the_next_change(self):
        """The wait the dispatch recovery enters records the version of the
        code it failed at, so the controller resumes it once per version."""
        record = live.record("900e2b9")
        run = {"id": 45447556135, "status": "completed", "created_at": "2026-11-24T17:24:00Z"}
        limit = reconcile.lifecycle.budget_limit("dispatch_recovery", POLICY)
        new, event, decision = reconcile._lost_dispatch(
            record, record["active_task"], run, limit + 1, now=NOW, policy=POLICY,
            pipeline=FIXED)
        self.assertEqual(decision, "dispatch failed; waiting for a human")
        self.assertEqual(new["pending_human_action"]["pipeline_version"], FIXED)
        # The task of the incident predates `charged`: the wait records no
        # credit, and the re-arm gives back `task_total` alone.
        self.assertNotIn("credit", new["pending_human_action"])
        self.assertEqual(event["details"]["pipeline_version"], FIXED)
        self.assertNotIn("payload", event["details"].get("why", ""))
        again = self.tick(new, pipeline=FIXED)
        self.assertEqual(self.staged(again, "events"), [])
        later = self.tick(new, pipeline="e" * 64)
        self.assertEqual([e["kind"] for e in self.staged(later, "events")][0], "rearm_dispatch")


class TestTheRequestWithoutATaskFile(unittest.TestCase):
    """A task the controller could not queue is never allocated or charged: the
    request that failed at r10 would have been held before it was charged."""

    def test_task_whose_task_cannot_be_built_is_held_before_it_is_charged(self):
        record = live.record("df439c0")
        # A task no queue could take: the description was never frozen.
        record["tracking_issue"] = {k: v for k, v in record["tracking_issue"].items()
                                    if k not in ("description", "description_digest")}
        out = run_tick(record, live.evidence())
        self.assertEqual([e["kind"] for e in staged(out, "events")], ["rearm_dispatch"])
        final = staged(out, "contributions")[-1]
        self.assertIsNone(final["active_task"])
        # The credit of the lost allocation, and no new charge.
        self.assertEqual(final["budget"]["attempts_used"], 6)
        [alert] = [a["alert"] for a in out.report["controller"]["alerts"]
                   if "request_patch" in a["alert"]]
        self.assertIn("a failure of the pipeline, not of the contribution", alert)


class TestAResultWhoseTaskCouldNotBeResolved(unittest.TestCase):
    """When `queue_result` fails before it resolves the task, the publication
    holds the claim as `task.json`: it names the task as the reconciler keys
    it, so the Result folds exactly as before."""

    def fold(self, task):
        record, evidence = live.record("900e2b9"), live.evidence()
        request = tasks.request_from_active(record)
        pub = {"status": "infra_error", "failure_class": "infrastructure",
               "reason": "the task file could not be read", "stage": "result", "task": task,
               "worker_run_id": "999", "publication_run_id": "999",
               "publication_artifact_id": 5}
        snapshot = reconcile.Snapshot({"c": copy.deepcopy(record)}, {}, {}, {}, dict(evidence))
        with mock.patch("collect.steering", return_value={"items": [], "omitted": {}}), \
                mock.patch("observe_ci.collect.branch_head",
                           return_value={"head": record["fork"]["head_sha"]}), \
                contextlib.redirect_stderr(io.StringIO()):
            out = reconcile.tick(
                helpers.NoReadApi(), snapshot, policy=POLICY, now=NOW, publications=[pub],
                pressure=capacity.Pressure(ok=True), observe=True, dispatcher_runs=[],
                queue_works={tasks.graph_id(request["dedupe_key"]): "completed"},
                work_results={}, pipeline=FIXED)
        return [e["kind"] for e in staged(out, "events")], out.report["publications"]

    def test_the_claim_folds_like_the_resolved_task(self):
        record, _evidence = live.fresh()
        request = tasks.request_from_active(record)
        claim = tasks.queue_payload(
            request, control_repo_id=LIVE_CONTROL_REPO_ID,
            reference=task_file.reference(live.WORK, "a" * 40, "b" * 64))
        resolved = tasks.payload(request, record, control_repo_id=LIVE_CONTROL_REPO_ID,
                                allowed_paths=[], context={})
        kinds, report = self.fold(claim)
        self.assertEqual(kinds, ["schedule_publish_retry"])
        self.assertEqual(report[0]["status"], "infra_error")
        self.assertEqual(self.fold(resolved), (kinds, report))
        # The fields the reconciler reads of a task are all in the claim.
        for name in ("contribution_id", "revision", "task", "dedupe_key", "generation"):
            self.assertEqual(claim[name], resolved[name], name)


if __name__ == "__main__":
    unittest.main()
