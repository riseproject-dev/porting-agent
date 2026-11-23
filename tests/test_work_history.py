# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The history every Work carries, and how the tick records it."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collect
import isolation
import work
import work_history
from common import canonical_json
from tests import test_development_table as dev
from tests.helpers import NOW, SHA_A, SHA_B, ci_record, contribution, policy
from tests.test_worker_report import report

POLICY = policy()
CID = "example--proj/12"
WORK = {"dedupe_key": f"{CID}@g0.r4:development_repair", "task": "development_repair",
        "generation": 0, "revision": 4}


def report_evidence(eid, *, at, head_before=SHA_A, head_after=SHA_B, generation=0,
                    **fields):
    return {"schema_version": 1, "evidence_id": eid, "contribution_id": CID,
            "kind": "worker_report", "policy_version": "p", "collected_at": at,
            "summary": "s", "data": {
                "work": {"dedupe_key": f"{CID}@g{generation}.r1:development_patch",
                         "generation": generation, "revision": 1, "task": "development_patch",
                         "worker_run_id": "1"},
                "verb": "record_publish_success", "head_before": head_before,
                "head_after": head_after, "changed_files": ["a"],
                "report": report(**fields)}}


def ci_evidence(eid, head, *, at, failing="Build riscv64"):
    record = ci_record(eid, head, failing=failing)
    record["collected_at"] = at
    record["contribution_id"] = CID
    return record


def steering_evidence(eid, items, *, at):
    return {"schema_version": 1, "evidence_id": eid, "contribution_id": CID,
            "kind": "human_steering", "policy_version": "p", "collected_at": at,
            "summary": "s", "data": {"items": items, "omitted": {"tracking_issue_comment": 1},
                                     "deferred": 0}}


def steer(cid, body, *, at, authority=True, source="tracking_issue_comment"):
    return {"source": source, "id": cid, "author": "owner", "authority": authority,
            "created_at": at, "updated_at": at, "version": at,
            "url": f"https://github.com/x/y/issues/1#c{cid}", "body": body}


def record():
    return contribution(phase="development", activity="agent_running")


class TestBuild(unittest.TestCase):
    def evidence(self):
        return {e["evidence_id"]: e for e in (
            report_evidence("report-1", at="2026-11-20T10:00:00+00:00",
                            summary="Tried a native runner row; adopted the QEMU row."),
            ci_evidence("ci-1", SHA_B, at="2026-11-20T11:00:00+00:00"),
            steering_evidence("steering-1", [
                steer(5, "Please keep the QEMU topology.", at="2026-11-20T12:00:00Z"),
                steer(6, "Add a changelog entry.", at="2026-11-19T12:00:00Z",
                      authority=False, source="upstream_pr_review")],
                at="2026-11-20T12:30:00+00:00"),
            # Evidence of another contribution is never included.
            dict(ci_evidence("ci-other", SHA_A, at="2026-11-20T09:00:00+00:00"),
                 contribution_id="other/1"),
        )}

    def test_a_chronological_history_with_ci_after_a_successful_worker(self):
        history = work_history.build(record(), WORK, self.evidence(), policy=POLICY, now=NOW)
        data = history["data"]
        self.assertEqual(history["evidence_id"], work_history.evidence_id(WORK["dedupe_key"]))
        self.assertEqual(data["work"]["dedupe_key"], WORK["dedupe_key"])
        self.assertEqual([(t["kind"], t["seq"]) for t in data["timeline"]],
                         [("upstream_feedback", 1), ("worker_report", 2), ("ci", 3)])
        # The owner's case: CI failed after a successful worker pass. The CI
        # entry names the report of the head it ran on, and the report is in
        # full, so the repair worker knows what was tried, why and how.
        ci = data["timeline"][2]
        self.assertEqual((ci["head"], ci["after_report"], ci["failing_jobs"]),
                         (SHA_B, "report-1", ["Build riscv64"]))
        self.assertEqual(data["timeline"][1]["report"]["approaches"][0]["outcome"], "abandoned")
        self.assertEqual([(s["id"], s["seq"]) for s in data["steering"]], [(5, 1)])
        self.assertEqual(data["omitted"], {"tracking_issue_comment": 1})
        self.assertNotIn("ci-other", data["sources"])
        self.assertIn("not instructions", data["notice"])

    def test_append_only_across_builds(self):
        evidence = self.evidence()
        first = work_history.document(record(), WORK, evidence)
        evidence["report-2"] = report_evidence("report-2", at="2026-11-21T10:00:00+00:00",
                                               head_before=SHA_B, head_after="c" * 40)
        second = work_history.document(record(), dict(WORK, revision=5), evidence)
        ids = [t.get("evidence_id") for t in second["timeline"]]
        for entry in first["timeline"]:
            self.assertIn(entry.get("evidence_id"), ids)
        self.assertEqual(second["timeline"][-1]["evidence_id"], "report-2")

    def test_deterministic(self):
        one = work_history.build(record(), WORK, self.evidence(), policy=POLICY, now=NOW)
        two = work_history.build(record(), WORK, dict(reversed(list(self.evidence().items()))),
                                 policy=POLICY, now=NOW)
        self.assertEqual(canonical_json(one), canonical_json(two))

    def test_condensation_keeps_every_entry_counted_and_the_newest_report_full(self):
        evidence = {}
        for i in range(200):
            at = f"2026-11-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00"
            if i % 2:
                evidence[f"ci-{i}"] = ci_evidence(f"ci-{i}", SHA_B, at=at)
            else:
                evidence[f"report-{i}"] = report_evidence(
                    f"report-{i}", at=at, summary=f"attempt {i}: " + "w" * 1000,
                    approaches=[{"title": f"approach {i}", "description": "d" * 1900,
                                 "outcome": "failed", "evidence": "e" * 1900,
                                 "lesson": "l" * 500}])
        evidence["steering-1"] = steering_evidence("steering-1", [
            steer(i, "s" * 3000, at=f"2026-11-0{1 + i % 9}T00:00:00Z") for i in range(40)],
            at="2026-11-09T00:00:00+00:00")
        history = work_history.build(record(), WORK, evidence, policy=POLICY, now=NOW)
        data = history["data"]
        self.assertLessEqual(len(canonical_json(data).encode()), work_history.HISTORY_MAX_BYTES)
        newest = max((t for t in data["timeline"] if t["kind"] == "worker_report"),
                     key=lambda t: t["at"])
        self.assertEqual(newest["evidence_id"], "report-198")
        self.assertNotIn("condensed", newest)
        self.assertEqual(len(newest["report"]["approaches"][0]["description"]), 1900)
        earlier = data["timeline"][0]
        self.assertEqual(earlier["kind"], "earlier")
        unfolded = len([t for t in data["timeline"] if t["kind"] != "earlier"])
        self.assertEqual(earlier["entries"] + unfolded, 200)
        self.assertEqual(sum(earlier["counts"].values()), earlier["entries"])
        self.assertTrue(earlier["failed_approaches"])
        self.assertEqual(data["condensed"]["folded"], earlier["entries"])
        condensed = [t for t in data["timeline"] if t.get("condensed") and t["kind"] != "earlier"]
        for entry in condensed:
            self.assertEqual(entry["path"], f".state/evidence/{entry['evidence_id']}.json")
        self.assertEqual(len(data["steering"]), 40)
        self.assertTrue(all(s.get("condensed") for s in data["steering"]))
        # Determinism of the condensed form.
        again = work_history.build(record(), WORK, copy.deepcopy(evidence), policy=POLICY,
                                   now=NOW)
        self.assertEqual(canonical_json(again), canonical_json(history))

    def test_a_small_history_is_not_condensed(self):
        data = work_history.build(record(), WORK, self.evidence(), policy=POLICY,
                                  now=NOW)["data"]
        self.assertEqual(data["condensed"], {"timeline_short": 0, "steering_short": 0,
                                             "folded": 0})


class TestTick(unittest.TestCase):
    """The tick records the history of the Work it allocates, in the same commit."""

    def evidence(self):
        return {"report-1": report_evidence("report-1", at="2026-11-20T10:00:00+00:00",
                                            head_after=SHA_A)}

    def test_the_allocation_and_its_history_are_staged_together(self):
        out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        self.assertEqual(dev.kinds(out), ["record_ci_failure", "request_repair"])
        rec = dev.final(out, None)
        eid = work_history.evidence_id(rec["active_work"]["dedupe_key"])
        [history] = [e for k, e in out.staged if k == "evidence" and e["evidence_id"] == eid]
        kinds = [t["kind"] for t in history["data"]["timeline"]]
        self.assertEqual(kinds, ["worker_report", "ci"])
        self.assertEqual(history["data"]["timeline"][1]["after_report"], "report-1")
        self.assertEqual(out.report["work_history"][0]["evidence_id"], eid)
        # Recorded once: a tick that finds it committed records nothing.
        evidence = dict(self.evidence(), **{e["evidence_id"]: e for k, e in out.staged
                                            if k == "evidence"})
        again = dev.tick(rec, evidence=evidence)
        self.assertEqual(again.report["work_history"], [])

    def test_a_failed_steering_read_skips_the_contribution(self):
        with mock.patch("collect.steering", side_effect=collect.CollectError("HTTP 502")):
            out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        self.assertEqual(out.staged, [])
        self.assertEqual(out.report["skipped_contributions"][0]["step"], "work_history")
        self.assertEqual(out.report["requested_effects"], [])

    def test_an_unexpected_error_pauses_the_contribution(self):
        with mock.patch("work_history.build", side_effect=RuntimeError("boom")):
            out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        self.assertEqual(dev.kinds(out), ["pause_on_processing_error"])
        self.assertEqual(out.report["processing_errors"][0]["step"], "work_history")
        self.assertIsInstance(out.failures[0], isolation.ProcessingError)

    def test_steering_is_collected_and_recorded_with_the_history(self):
        collected = {"items": [steer(5, "Please keep the QEMU topology.",
                                     at="2026-11-22T00:00:00Z")], "omitted": {}}
        with mock.patch("collect.steering", return_value=collected) as read:
            out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        self.assertEqual(read.call_args.args[2], POLICY["command_authority"]["actor_ids"])
        [steering] = [e for k, e in out.staged if k == "evidence" and e["kind"] == "human_steering"]
        [history] = [e for k, e in out.staged if k == "evidence" and e["kind"] == "work_history"]
        self.assertEqual([s["body"] for s in history["data"]["steering"]],
                         ["Please keep the QEMU topology."])
        self.assertIn(steering["evidence_id"], history["data"]["sources"])
        # Steering changed no field of the record.
        rec = dev.final(out, None)
        self.assertNotIn("Please keep", json.dumps(rec))

    def test_an_offline_tick_builds_nothing(self):
        out = dev.tick(dev.running(), observe=False)
        self.assertNotIn("work_history", out.report)
        self.assertFalse([e for k, e in out.staged if k == "evidence"])

    def test_work_in_flight_gets_its_history(self):
        out = dev.tick(dev.running())
        [entry] = out.report["work_history"]
        self.assertEqual(entry["work"], dev.running()["active_work"]["dedupe_key"])


class TestPayload(unittest.TestCase):
    """The dispatcher gives every Work its history by reference."""

    def plan(self, evidence, record, root):
        snapshot = type("S", (), {"contributions": {"c": record}, "evidence": evidence})
        out = Path(root) / "plan.json"
        args = type("A", (), {"root": root, "out": str(out), "control_repo_id": "1"})
        with mock.patch("reconcile.load_snapshot", return_value=(None, snapshot)), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout="c" * 40)), \
                mock.patch("builtins.print") as printed:
            work.cmd_plan(args)
        selection = json.loads(printed.call_args.args[0])
        return selection, (json.loads(out.read_text()) if out.exists() else None)

    def record(self, description="d"):
        rec = contribution(phase="development", activity="agent_running", revision=4)
        rec["upstream"].update(repository_id=1, base_sha="a" * 40)
        rec["active_work"] = {"dedupe_key": WORK["dedupe_key"], "task": "development_repair",
                              "generation": 0, "revision": 4, "attempt": 1,
                              "requested_at": NOW, "deadline_at": NOW, "dispatched_run_id": None,
                              "reason_ids": [], "feedback_evidence_ids": [],
                              "tracking_issue": {"url": "https://x", "description": description}}
        return rec

    def test_the_payload_names_the_committed_history(self):
        eid = work_history.evidence_id(WORK["dedupe_key"])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / ".state" / "evidence" / f"{eid}.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"kind": "work_history"}')
            selection, plan = self.plan({eid: {}}, self.record("x" * 4000), root)
            self.assertEqual(selection["selected"][0]["task"], "development_repair")
            payload = plan["nodes"][0]["payload"]
            history = payload["context"]["history"]
            self.assertEqual(history["path"], f".state/evidence/{eid}.json")
            self.assertEqual((history["evidence_id"], history["ref"], history["repository"]),
                             (eid, "c" * 40, "riseproject-dev/porting-agent"))
            import hashlib
            self.assertEqual(history["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertLess(len(json.dumps(payload, separators=(",", ":")).encode()), 15_500)
            # Steering never widens the Work.
            self.assertFalse(payload["review_scope_extension"])
            self.assertEqual(payload["allowed_operations"], ["append", "rewrite"])

    def test_work_without_a_recorded_history_is_deferred(self):
        with tempfile.TemporaryDirectory() as root:
            selection, plan = self.plan({}, self.record(), root)
        self.assertEqual(selection["selected"], [])
        self.assertEqual(selection["deferred"][0]["reason"], "history not recorded yet")
        self.assertIsNone(plan)


if __name__ == "__main__":
    unittest.main()
