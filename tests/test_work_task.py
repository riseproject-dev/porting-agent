# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The task file of one Work: the content, the id, the bounds, and the tick
that commits it beside the history."""

from __future__ import annotations

import copy
import hashlib
import unittest
from unittest import mock

import reconcile
import schemas
import state
import work
import work_history
import work_task
from common import canonical_json
from tests import live_d2vm as live
from tests import test_development_table as dev
from tests.helpers import policy

POLICY = policy()


def digest(evidence):
    return work_task.file_digest_reader(None, evidence)


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.record, self.evidence = live.fresh()
        self.request = work.request_from_active(self.record)

    def data(self):
        return work_task.build_data(self.record, self.evidence,
                                    file_digest=digest(self.evidence))

    def test_the_task_holds_every_field_of_the_payload_the_dispatcher_built(self):
        """The same inputs as the dispatcher took, with the references as
        the payload carried them: only the commit and the URL, which a file
        cannot name, and the resource scope and effect contract, which the
        queue carries, are not in the task file."""
        full = lambda eid: {"repository": "riseproject-dev/porting-agent", "ref": "c" * 40,
                            "path": f".state/evidence/{eid}.json",
                            "sha256": digest(self.evidence)(eid),
                            "url": f"https://raw.githubusercontent.com/x/{eid}"}
        paths, context = work.payload_inputs(self.request, self.record, self.evidence,
                                             plan_reference=full)
        history = work_history.evidence_id(live.WORK)
        context["history"] = dict(full(history), evidence_id=history)
        context["history"].pop("url")
        old = work.payload(self.request, self.record, control_repo_id="1", allowed_paths=paths,
                           context=context)
        new = self.data()
        for key in ("resource_scope", "effect_contract"):
            old.pop(key)
        for reference in (old["context"]["history"],
                          *(i["reference"] for i in old["context"]["reason_evidence"]
                            if "reference" in i)):
            for key in ("repository", "ref", "url"):
                reference.pop(key, None)
        self.assertEqual(new, old)
        self.assertEqual(new["context"]["history"]["evidence_id"], live.HISTORY)

    def test_the_task_is_valid_and_bounded(self):
        data = self.data()
        schemas.validate("task", data)
        self.assertLess(len(canonical_json(data).encode()), work_task.TASK_FILE_MAX_BYTES)

    def test_the_nested_references_name_no_commit(self):
        data = self.data()
        [plan] = [i for i in data["context"]["reason_evidence"] if i["kind"] == "plan"]
        self.assertEqual(set(plan["reference"]), {"path", "sha256"})
        self.assertEqual(plan["reference"]["sha256"],
                         hashlib.sha256(work_task.file_bytes(self.evidence[live.PLAN])).hexdigest())
        self.assertEqual(set(data["context"]["history"]), {"path", "sha256", "evidence_id"})

    def test_the_evidence_is_derived_from_the_work_id_and_stores(self):
        task = work_task.build(self.record, self.evidence, policy=POLICY,
                               now="2026-11-24T17:00:00+00:00", file_digest=digest(self.evidence))
        self.assertEqual(task["evidence_id"],
                         "task-" + hashlib.sha256(live.WORK.encode()).hexdigest()[:32])
        self.assertEqual((task["kind"], task["digests"]["task_id"]),
                         ("task", hashlib.sha256(live.WORK.encode()).hexdigest()))
        schemas.validate("evidence", task)
        data = canonical_json(task).encode()
        self.assertEqual(state.check_blob(work_task.path(task["evidence_id"]), data, POLICY), [])

    def test_a_task_that_cannot_be_built_is_refused(self):
        for change, why in (
                (lambda r, e: r["upstream"].update(base_sha=None), "not bound"),
                (lambda r, e: r["active_work"].pop("tracking_issue"), "frozen tracking-issue"),
                (lambda r, e: e.pop(live.HISTORY), "history of the Work is not recorded")):
            record, evidence = live.fresh()
            change(record, evidence)
            with self.assertRaises(work_task.TaskFileError) as raised:
                work_task.build_data(record, evidence, file_digest=digest(evidence))
            self.assertIn(why, str(raised.exception))

    def test_the_dry_run_answers_without_the_history_it_would_record(self):
        record, evidence = live.fresh()
        evidence.pop(live.HISTORY)
        self.assertIsNone(work_task.dry_run(record, evidence))
        record["upstream"]["base_sha"] = None
        self.assertIn("not bound", work_task.dry_run(record, evidence))

    def test_an_oversized_task_condenses_the_oldest_reason_evidence_it_may(self):
        data = copy.deepcopy(self.data())
        items = data["context"]["reason_evidence"]
        items[:0] = [{"evidence_id": f"ci-{index}", "kind": "ci", "summary": "s" * 1600,
                      "limitations": [], "data": {"blob": "x" * 30000}} for index in range(4)]
        small = work_task.condensed(copy.deepcopy(data), limit=len(canonical_json(data)) - 70000)
        kept = small["context"]["reason_evidence"]
        self.assertEqual(small["context"]["reason_evidence_condensed"], 3)
        self.assertEqual([i["evidence_id"] for i in kept[:4]], [f"ci-{n}" for n in range(4)])
        self.assertTrue(all(i.get("condensed") for i in kept[:3]))
        self.assertFalse(kept[3].get("condensed"))
        self.assertEqual(kept[0]["evidence_path"], ".state/evidence/ci-0.json")
        # The plan and the relayed feedback are never reduced.
        self.assertEqual([i for i in kept[4:]], [i for i in items[4:]])
        with self.assertRaises(work_task.TaskFileError):
            work_task.condensed(copy.deepcopy(data), limit=1000)


class TestAlwaysBuildable(unittest.TestCase):
    """A task of schema-valid input is always built: a hold that no change of
    the code could release would hold the Work forever."""

    def test_an_oversized_plan_is_reduced_to_its_reference(self):
        record, evidence = live.fresh()
        plan = evidence[live.PLAN]
        plan["data"]["proposal"]["architecture_precedent"] = ["x" * 799] * 400
        plan["data"]["proposal"]["risks"] = [{"r": "y" * 500}] * 600
        plan["data"]["proposal"]["path_coverage"] = [
            {"area": f"a{i}", "paths": ["p"] * 20, "assessment": "z" * 599} for i in range(400)]
        data = work_task.build_data(record, evidence, file_digest=digest(evidence))
        # ...with the plan item over the bound on its own.
        plain = work.payload_inputs(work.request_from_active(record), record, evidence,
                                    plan_reference=lambda eid: {})[1]
        self.assertGreater(len(canonical_json(plain).encode()), work_task.TASK_FILE_MAX_BYTES)
        [item] = [i for i in data["context"]["reason_evidence"] if i["kind"] == "plan"]
        self.assertTrue(item["condensed"])
        self.assertEqual(set(item["reference"]), {"path", "sha256"})
        self.assertLess(len(canonical_json(data).encode()), work_task.TASK_FILE_MAX_BYTES)
        self.assertEqual(data["context"]["reason_evidence_condensed"], 1)
        # The relayed feedback keeps its ids, and the plan is reduced after
        # the other evidence.
        [relay] = [i for i in data["context"]["reason_evidence"] if i["kind"] == "human_relay"]
        self.assertFalse(relay.get("condensed"))
        self.assertIsNone(work_task.dry_run(record, evidence))

    def test_relayed_feedback_is_cut_short_last_and_keeps_its_ids(self):
        record, evidence = live.fresh()
        relay = evidence[live.REVIEW]
        relay["data"]["comments"] = [{"id": i, "body": "b" * 1200} for i in range(400)]
        relay["data"]["comment_ids"] = list(range(400))
        data = work_task.build_data(record, evidence, file_digest=digest(evidence))
        [item] = [i for i in data["context"]["reason_evidence"] if i["kind"] == "human_relay"]
        self.assertEqual(item["data"]["comment_ids"], list(range(400)))
        self.assertEqual([c["id"] for c in item["data"]["comments"]], list(range(400)))
        self.assertLessEqual(len(item["data"]["comments"][0]["body"]), 200)

    def test_the_longest_contribution_id_the_record_schema_admits_is_a_valid_task(self):
        record, evidence = live.fresh()
        self.assertEqual(schemas._validator("contribution").schema["properties"][
            "contribution_id"]["pattern"], "^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
        data = work_task.build_data(record, evidence, file_digest=digest(evidence))
        data = dict(data, contribution_id="a" * 128, dedupe_key="k" * 300)
        schemas.validate("task", data)
        with self.assertRaises(schemas.SchemaError):
            schemas.validate("task", dict(data, contribution_id="a" * 129))

    def test_the_digest_of_evidence_staged_in_the_tick_is_the_one_the_store_writes(self):
        import tempfile
        from pathlib import Path
        record, evidence = live.fresh()
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / work_task.path(live.PLAN)
            file.parent.mkdir(parents=True)
            file.write_text("stale bytes of a file under the same id")
            committed = work_task.file_digest_reader(Path(root), evidence)(live.PLAN)
            staged = work_task.file_digest_reader(Path(root), evidence,
                                                  frozenset({live.PLAN}))(live.PLAN)
            self.assertEqual(committed, hashlib.sha256(file.read_bytes()).hexdigest())
        self.assertEqual(staged, hashlib.sha256(
            work_task.file_bytes(evidence[live.PLAN])).hexdigest())


class TestTick(unittest.TestCase):
    def evidence(self):
        return {"plan-1": {"schema_version": 1, "evidence_id": "plan-1", "kind": "plan",
                           "policy_version": "p", "collected_at": "2026-11-20T10:00:00+00:00",
                           "summary": "s", "data": {"proposal": {}}}}

    def allocate(self, **kw):
        return dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence(), **kw)

    def test_the_task_file_is_staged_with_the_allocation_and_its_history(self):
        out = self.allocate()
        record = dev.final(out, None)
        key = record["active_work"]["dedupe_key"]
        staged = {e["evidence_id"]: e for k, e in out.staged if k == "evidence"}
        self.assertIn(work_history.evidence_id(key), staged)
        task = staged[work_task.evidence_id(key)]
        self.assertEqual(task["kind"], "task")
        self.assertEqual(task["data"]["dedupe_key"], key)
        self.assertEqual(task["data"]["context"]["history"]["sha256"],
                         hashlib.sha256(work_task.file_bytes(
                             staged[work_history.evidence_id(key)])).hexdigest())
        [entry] = out.report["work_task"]
        self.assertEqual(entry["evidence_id"], task["evidence_id"])
        self.assertNotIn("dispatch", entry)
        # Recorded once.
        evidence = dict(self.evidence(), **staged)
        again = dev.tick(record, evidence=evidence)
        self.assertEqual(again.report["work_task"], [])

    def test_a_task_file_recorded_after_the_request_launches_the_dispatcher(self):
        """Work an earlier tick requested, whose history is recorded but not
        its task file (the dispatcher defers it), gets the file in the next
        tick and the dispatcher is launched at once."""
        out = self.allocate()
        record = dev.final(out, None)
        evidence = dict(self.evidence(), **{e["evidence_id"]: e for k, e in out.staged
                                            if k == "evidence" and e["kind"] != "task"})
        again = dev.tick(record, evidence=evidence)
        [entry] = again.report["work_task"]
        self.assertIn("relaunch", entry["dispatch"])
        self.assertEqual(again.report["dispatch_relaunch"],
                         [{"type": "relaunch_dispatcher",
                           "contribution_id": record["contribution_id"]}])
        self.assertTrue([e for k, e in again.staged if k == "evidence" and e["kind"] == "task"])
        self.assertEqual(out.report["dispatch_relaunch"], [])

    def test_the_task_files_share_the_byte_bound_of_the_histories(self):
        """A tick that spent its bytes on steering and histories records the
        task file in a later tick, so the commit stays within its bound."""
        with mock.patch("reconcile.HISTORY_BYTES_PER_TICK",
                        reconcile.HISTORY_RECORD_BYTES + 1000):
            out = self.allocate()
        kinds = [e["kind"] for k, e in out.staged if k == "evidence"]
        self.assertIn("work_history", kinds)
        self.assertNotIn("task", kinds)
        [entry] = out.report["work_task"]
        self.assertIn("byte bound", entry["deferred"])
        again = dev.tick(dev.final(out, None), evidence=dict(
            self.evidence(), **{e["evidence_id"]: e for k, e in out.staged if k == "evidence"}))
        self.assertTrue([e for k, e in again.staged if k == "evidence" and e["kind"] == "task"])

    def test_a_deferred_history_defers_the_task_file(self):
        from unittest import mock

        import collect
        with mock.patch("collect.steering", side_effect=collect.CollectError("HTTP 502")):
            out = self.allocate()
        self.assertFalse([e for k, e in out.staged if k == "evidence" and e["kind"] == "task"])
        [entry] = out.report["work_task"]
        self.assertIn("history of the Work is not recorded", entry["deferred"])

    def test_a_task_that_cannot_be_built_holds_the_allocation_before_anything_is_charged(self):
        # A record that froze no description at intake.
        rec = dev.record("waiting_ci")
        rec["tracking_issue"] = {"id": 5000, "number": 12}
        out = dev.tick(rec, verdict="failed", evidence=self.evidence())
        self.assertEqual(dev.kinds(out), ["record_ci_failure"])
        held = dev.final(out, rec)
        self.assertIsNone(held["active_work"])
        self.assertEqual(held["budget"]["attempts_used"], rec["budget"]["attempts_used"])
        self.assertEqual([e["kind"] for k, e in out.staged if k == "evidence"], ["ci"])
        self.assertEqual(out.report["controller"]["selected"], [])
        [alert] = [a["alert"] for a in out.report["controller"]["alerts"]
                   if "task file" in a["alert"]]
        self.assertIn("request_repair is held until the pipeline code changes", alert)
        self.assertIn("a failure of the pipeline, not of the contribution", alert)
        self.assertIn("no frozen tracking-issue description", alert)
        [entry] = [d for d in out.report["controller"]["deferred"] if "held" in d["why"]]
        self.assertIn("could not be built", entry["why"])
        # Nothing was stored: the next tick allocates once the task can be
        # built.
        rec["tracking_issue"] = dev.record("waiting_ci")["tracking_issue"]
        again = dev.tick(rec, verdict="failed", evidence=self.evidence())
        self.assertEqual(dev.kinds(again), ["record_ci_failure", "request_repair"])

if __name__ == "__main__":
    unittest.main()
