# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The report of a worker is stored only when its Result is accepted.

The six success rows (``reconcile.REPORT_VERBS``) store it as
``worker_report`` evidence bound to the Work and name it in their event;
every other fold discards it."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import engine
import reconcile
import state
import work_history
import worker_report
from common import canonical_json
from tests import test_development_table as dev
from tests import test_upstream_review_table as up
from tests.helpers import NOW, SHA_A, SHA_B, policy
from tests.test_late_results import publication
from tests.test_worker_report import HISTORY, TOKEN, report

POLICY = policy()


def with_report(rec: dict, outcome: dict, *, task: str | None = None, **report_fields) -> dict:
    """``outcome`` as the Result of the record's Work, carrying a history
    and a valid report of that Work."""
    work = rec.get("active_work") or {}
    name = task or work.get("task") or "development_repair"
    outcome = copy.deepcopy(outcome)
    outcome["task"] = dict(outcome.get("task") or {}, contribution_id=rec["contribution_id"],
                           revision=rec["revision"], task=name,
                           dedupe_key=work.get("dedupe_key"), generation=work.get("generation", 0),
                           expected_head=SHA_A,
                           context={"history": {"sha256": HISTORY}})
    outcome["worker_report"] = report(contribution_id=rec["contribution_id"],
                                      expected_revision=rec["revision"], task=name,
                                      **report_fields)
    return outcome


def compact(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def large(**fields) -> dict:
    """Report fields that make a schema-valid report of just under 32 KiB in
    compact JSON (as a worker's ``json.dump`` writes it), whose indented
    canonical form is larger than 32 KiB."""
    base = report(**fields)
    filler = [{"what": f"step {i} " + "w" * 280, "why": "y" * 400} for i in range(8)]
    approaches = [{"title": f"approach {i}: fix: use foo", "description": "d" * 600,
                   "outcome": "failed" if i % 2 else "adopted", "evidence": "e" * 600,
                   "lesson": "l" * 250} for i in range(10)]
    verification = [{"check": f"check {i}", "result": "passed", "evidence": "v" * 300}
                     for i in range(12)]
    base.update(attempted=filler, approaches=approaches, verification=verification)
    # Pad the summary, then the open items, up to the target size.
    target = worker_report.MAX_REPORT_BYTES - 120
    items = []
    while len(compact(dict(base, open_items=items + ["o" * 300]))) < target and len(items) < 10:
        items.append("o" * 300)
    base["open_items"] = items
    missing = target - len(compact(base))
    base["failures"] = [{"what": "f", "evidence": "x" * min(500, max(0, missing - 30 - 60 * i))}
                        for i in range(max(1, min(8, missing // 500 + 1)))]
    while len(compact(base)) > target:
        base["failures"][-1]["evidence"] = base["failures"][-1]["evidence"][:-50]
    return {k: v for k, v in base.items()
            if k not in ("schema_version", "contribution_id", "expected_revision", "task",
                         "history_digest")}


def fold(rec, outcome):
    return reconcile.apply_publication(rec, outcome, now=NOW, policy=POLICY, worker_run_id="555")


def stored(staged):
    return [r for kind, r in staged if kind == "evidence" and r["kind"] == "worker_report"]


def events(staged):
    return [r for kind, r in staged if kind == "events"]


class TestDevelopment(unittest.TestCase):
    def test_the_success_rows_store_the_report_of_the_work(self):
        for name, verb in (("published", "record_publish_success"),
                           ("no_change", "request_ci_rerun")):
            with self.subTest(result=name):
                rec = dev.running()
                outcome = dev.rerun_outcome(rec) if name == "no_change" else publication(rec, name)
                new, staged = fold(rec, with_report(rec, outcome))
                [event] = events(staged)
                [evidence] = stored(staged)
                self.assertEqual(event["kind"], verb)
                self.assertEqual(event["details"]["report_evidence_id"], evidence["evidence_id"])
                self.assertEqual(evidence["data"]["work"], {
                    "dedupe_key": rec["active_work"]["dedupe_key"], "generation": 0,
                    "revision": rec["revision"], "task": "development_repair",
                    "worker_run_id": "555"})
                self.assertEqual(evidence["data"]["verb"], verb)
                self.assertEqual(evidence["evidence_id"],
                                 f"report-example--proj--12-r{rec['revision']}-555")
                # No record field is written for it.
                self.assertNotIn("report", json.dumps(sorted(new)))

    def test_every_other_fold_discards_the_report(self):
        for name in ("rejected_validation", "worker_blocked", "rejected_stale", "proposal",
                     "infra_error"):
            with self.subTest(result=name):
                rec = dev.running()
                _new, staged = fold(rec, with_report(rec, publication(rec, name)))
                self.assertEqual(stored(staged), [])
                self.assertNotIn("report_evidence_id", events(staged)[0]["details"])
        # A no-change report counted as a rejection (the Work carries
        # feedback) is discarded too.
        rec = dev.running(active_work=dict(dev.running()["active_work"],
                                           feedback_evidence_ids=["review-1"]),
                          pending_feedback_evidence_ids=["review-1"])
        _new, staged = fold(rec, with_report(rec, publication(rec, "no_change")))
        self.assertEqual([e["kind"] for e in events(staged)], ["reject_patch_proposal"])
        self.assertEqual(stored(staged), [])

    def test_a_late_result_stores_nothing(self):
        rec = dev.running()
        late = with_report(rec, publication(rec, "published"))
        late["task"]["revision"] = rec["revision"] - 1 if rec["revision"] else 99
        out = dev.tick(rec, [late], observe=False)
        self.assertEqual([e["kind"] for k, e in out.staged if k == "events"],
                         ["record_late_effect"])
        self.assertEqual(stored(out.staged), [])

    def test_a_held_result_stores_its_report_when_the_retry_publishes(self):
        rec = dev.running()
        failed = with_report(rec, publication(rec, "infra_error"))
        held, staged = fold(rec, failed)
        self.assertEqual(stored(staged), [])
        self.assertIsNotNone(held["pending_publication"])
        retried = with_report(rec, publication(held, "published"))
        retried["task"]["revision"] = rec["revision"]
        new, staged = reconcile.apply_publication(held, retried, now=NOW, policy=POLICY,
                                                  worker_run_id=None)
        [evidence] = stored(staged)
        self.assertEqual(events(staged)[0]["kind"], "record_publish_success")
        self.assertTrue(evidence["evidence_id"].endswith("-555"))

    def test_a_credential_in_the_report_is_stored_redacted(self):
        rec = dev.running()
        _new, staged = fold(rec, with_report(rec, publication(rec, "published"),
                                             summary=f"pasted {TOKEN}"))
        [evidence] = stored(staged)
        self.assertNotIn(TOKEN, json.dumps(evidence))
        self.assertEqual(state.check_blob(state.record_path("evidence", evidence),
                                          json.dumps(evidence).encode(), POLICY), [])

    def test_an_old_payload_without_a_report_still_folds(self):
        rec = dev.running()
        new, staged = fold(rec, publication(rec, "published"))
        self.assertEqual(engine.state_key(new), "development.waiting_ci")
        self.assertEqual(stored(staged), [])
        self.assertEqual(events(staged)[0]["details"]["report"], "absent")

    def test_an_accepted_result_without_a_storable_report_fails_loudly(self):
        """The gate accepts a Result of Work that carries a history only with
        a valid report, so a success fold never drops it: the fold raises,
        and the tick pauses the contribution instead of folding the Result
        without its report."""
        def invalid(outcome):
            outcome["worker_report"]["expected_revision"] += 1
        def missing(outcome):
            del outcome["worker_report"]
        def unreadable(outcome):
            del outcome["worker_report"]
            outcome["worker_report_error"] = "report.json cannot be read: not a regular file"
        for name, spoil in (("invalid", invalid), ("missing", missing),
                            ("unreadable", unreadable)):
            with self.subTest(report=name):
                rec = dev.running()
                outcome = with_report(rec, publication(rec, "published"))
                spoil(outcome)
                with self.assertRaisesRegex(worker_report.ReportError,
                                            "record_publish_success Result"):
                    fold(rec, outcome)
                out = dev.tick(rec, [outcome], observe=False)
                self.assertEqual(dev.kinds(out), ["pause_on_processing_error"])
                self.assertEqual(stored(out.staged), [])
                paused = dev.final(out, rec)
                self.assertIn("carries no report that can be stored",
                              paused["paused_reason"])
                self.assertIn(rec["active_work"]["dedupe_key"], paused["paused_reason"])
                # The Result is never folded without its report.
                self.assertEqual(engine.state_key(paused), "development.fix_needed")
                self.assertTrue(paused["paused"])


class TestUpstreamReview(unittest.TestCase):
    def running(self):
        return up.record("agent_running", active_work=dict(up.REVISION_WORK))

    def test_a_published_revision_and_a_no_change_answer_store_the_report(self):
        for name, verb in (("published", "record_revision_publish_success"),
                           ("no_change", "require_reviewer_answer")):
            with self.subTest(result=name):
                rec = self.running()
                result = (up.published(rec, SHA_B) if name == "published"
                          else publication(rec, name))
                _new, staged = fold(rec, with_report(rec, result))
                [evidence] = stored(staged)
                self.assertEqual(events(staged)[0]["kind"], verb)
                self.assertEqual(evidence["data"]["head_after"],
                                 SHA_B if name == "published" else None)

    def test_a_rejected_revision_discards_the_report(self):
        rec = self.running()
        _new, staged = fold(rec, with_report(rec, publication(rec, "rejected_validation")))
        self.assertEqual(events(staged)[0]["kind"], "reject_patch_proposal")
        self.assertEqual(stored(staged), [])


class TestSubmission(unittest.TestCase):
    def test_an_accepted_rewrite_and_review_store_their_reports(self):
        from tests import test_handoff as th
        world = th.submitted()
        outcome = with_report(world.record, world.rewrite_outcome(), task="submission_rewrite")
        out = world.tick(th.at(1), publications=[outcome])
        [evidence] = stored(out.staged)
        accept = next(e for k, e in out.staged if k == "events"
                      and e["kind"] == "accept_submission_rewrite")
        self.assertEqual(accept["details"]["report_evidence_id"], evidence["evidence_id"])
        outcome = with_report(world.record, world.review_outcome(), task="submission_review")
        out = world.tick(th.at(2), publications=[outcome])
        self.assertEqual([e["data"]["verb"] for e in stored(out.staged)],
                         ["accept_submission_review"])

    def test_a_failed_or_unusable_rewrite_or_review_discards_its_report(self):
        from tests import test_handoff as th
        world = th.submitted()
        outcome = with_report(world.record, world.rewrite_outcome("fail", ["no changelog"]),
                              task="submission_rewrite")
        out = world.tick(th.at(1), publications=[outcome])
        self.assertIn("reject_submission_rewrite", world.kinds(out))
        self.assertEqual(stored(out.staged), [])
        world = th.rewritten()
        outcome = with_report(world.record, world.review_outcome("fail", ["no changelog"]),
                              task="submission_review")
        out = world.tick(th.at(2), publications=[outcome])
        self.assertIn("reject_submission_review", world.kinds(out))
        self.assertEqual(stored(out.staged), [])
        world = th.submitted()
        outcome = with_report(world.record, world.rewrite_outcome(verdict="maybe"),
                              task="submission_rewrite")
        out = world.tick(th.at(1), publications=[outcome])
        self.assertIn("allocate_work_retry", world.kinds(out))
        self.assertEqual(stored(out.staged), [])

    def test_the_next_work_carries_the_stored_report(self):
        from tests import test_handoff as th
        world = th.submitted()
        world.tick(th.at(1), publications=[with_report(world.record, world.rewrite_outcome(),
                                                       task="submission_rewrite")])
        history = world.evidence[work_history.evidence_id(
            world.record["active_work"]["dedupe_key"])]
        [entry] = [t for t in history["data"]["timeline"] if t["kind"] == "worker_report"]
        self.assertEqual(entry["work"]["task"], "submission_rewrite")
        self.assertEqual(entry["report"]["summary"], report()["summary"])
        self.assertEqual(world.record["active_work"]["task"], "submission_review")


class TestLargeReport(unittest.TestCase):
    """A compact report just under the cap that the gate accepts is stored
    by every success fold, although its indented canonical form is larger
    than the cap."""

    def check_accepted(self, outcome: dict) -> None:
        value = outcome["worker_report"]
        data = compact(value)
        self.assertLess(len(data), worker_report.MAX_REPORT_BYTES)
        self.assertGreater(len(canonical_json(value).encode()), worker_report.MAX_REPORT_BYTES)
        # The check the worker gate and the submission checks run on the file.
        self.assertEqual(worker_report.check(outcome["task"], data), value)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_bytes(data)
            self.assertEqual(worker_report.check_path(outcome["task"], path), value)

    def test_every_development_and_revision_success_fold_stores_it(self):
        cases = [(dev.running, lambda rec, name: (dev.rerun_outcome(rec) if name == "no_change"
                                                  else publication(rec, name)), name, verb)
                 for name, verb in (("published", "record_publish_success"),
                                    ("no_change", "request_ci_rerun"))]
        def revision():
            return up.record("agent_running", active_work=dict(up.REVISION_WORK))
        cases += [(revision, lambda rec, name: (up.published(rec, SHA_B) if name == "published"
                                                else publication(rec, name)), name, verb)
                  for name, verb in (("published", "record_revision_publish_success"),
                                     ("no_change", "require_reviewer_answer"))]
        for make, result, name, verb in cases:
            with self.subTest(result=name, verb=verb):
                rec = make()
                outcome = with_report(rec, result(rec, name), **large())
                self.check_accepted(outcome)
                _new, staged = fold(rec, outcome)
                [evidence] = stored(staged)
                self.assertEqual(events(staged)[0]["kind"], verb)
                self.assertEqual(events(staged)[0]["details"]["report_evidence_id"],
                                 evidence["evidence_id"])
                self.assertEqual(len(evidence["data"]["report"]["approaches"]), 10)

    def test_the_submission_success_folds_store_it(self):
        from tests import test_handoff as th
        world = th.submitted()
        outcome = with_report(world.record, world.rewrite_outcome(), task="submission_rewrite",
                              **large())
        self.check_accepted(outcome)
        out = world.tick(th.at(1), publications=[outcome])
        self.assertEqual([e["data"]["verb"] for e in stored(out.staged)],
                         ["accept_submission_rewrite"])
        outcome = with_report(world.record, world.review_outcome(), task="submission_review",
                              **large())
        self.check_accepted(outcome)
        out = world.tick(th.at(2), publications=[outcome])
        self.assertEqual([e["data"]["verb"] for e in stored(out.staged)],
                         ["accept_submission_review"])

    def test_a_dict_over_the_cap_in_compact_json_is_still_refused(self):
        rec = dev.running()
        outcome = with_report(rec, publication(rec, "published"), **large())
        outcome["worker_report"]["summary"] = "s" * 1000
        outcome["worker_report"]["open_items"] = ["o" * 300] * 10
        self.assertGreater(len(compact(outcome["worker_report"])),
                           worker_report.MAX_REPORT_BYTES)
        with self.assertRaises(worker_report.ReportError):
            worker_report.check(outcome["task"], outcome["worker_report"])


class TestFoldFromArtifact(unittest.TestCase):
    """End to end: the publication artifact as the controller unpacks it,
    read by ``reconcile._publication_outcomes`` and folded by the tick. The
    fold checks the bytes of the file, exactly as the gate did."""

    def tick_with(self, files: dict[str, bytes | None], *, link: bool = False):
        rec = dev.running()
        outcome = with_report(rec, publication(rec, "published"))
        body = outcome.pop("worker_report")
        task = outcome.pop("task")
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "555"
            directory.mkdir()
            (directory / "outcome.json").write_text(json.dumps(outcome))
            (directory / "task.json").write_text(json.dumps(task))
            (directory / "artifact-id").write_text("999")
            data = files.get("report.json", compact(body))
            if link:
                (directory / "real.json").write_bytes(data)
                (directory / "report.json").symlink_to(directory / "real.json")
            elif data is not None:
                (directory / "report.json").write_bytes(data)
            gate = worker_report.check_path(task, directory / "report.json") \
                if data is not None and not link else None
            [loaded] = reconcile._publication_outcomes(root)
        return rec, gate, dev.tick(rec, [loaded], observe=False)

    def test_a_report_the_gate_accepted_is_stored(self):
        rec = dev.running()
        base = with_report(rec, publication(rec, "published"))["worker_report"]
        big = with_report(rec, publication(rec, "published"), **large())["worker_report"]
        cases = {
            "compact": compact(base),
            "indented": json.dumps(base, indent=4).encode(),
            # Escapes and number forms that grow when the parsed document is
            # serialized again, and a lone surrogate that cannot be encoded.
            "escapes": compact(dict(base, summary="caf\u00e9 \ud800 done")).replace(
                b'"schema_version":1', b'"schema_version":1e0'),
            "just under the cap": compact(big),
        }
        self.assertIn(b'"schema_version":1e0', cases["escapes"])
        self.assertIn(b"\\ud800", cases["escapes"])
        for name, data in cases.items():
            with self.subTest(report=name):
                self.assertLessEqual(len(data), worker_report.MAX_REPORT_BYTES)
                _rec, gate, out = self.tick_with({"report.json": data})
                self.assertIsNotNone(gate)
                self.assertEqual(dev.kinds(out)[0], "record_publish_success")
                [evidence] = stored(out.staged)
                self.assertEqual(dev.kinds(out).count("pause_on_processing_error"), 0)
                event = next(e for k, e in out.staged if k == "events")
                self.assertEqual(event["details"]["report_evidence_id"], evidence["evidence_id"])
                # The staged record can be written by the store.
                blob = canonical_json(evidence).encode()
                self.assertEqual(state.check_blob(state.record_path("evidence", evidence),
                                                  blob, POLICY), [])

    def test_a_missing_or_linked_report_pauses_the_contribution(self):
        for name, files, link in (("missing", {"report.json": None}, False),
                                  ("link", {}, True)):
            with self.subTest(report=name):
                rec, _gate, out = self.tick_with(files, link=link)
                self.assertEqual(dev.kinds(out), ["pause_on_processing_error"])
                self.assertEqual(stored(out.staged), [])
                reason = dev.final(out, rec)["paused_reason"]
                self.assertIn("carries no report that can be stored", reason)
                if link:
                    self.assertIn("not a regular file", reason)


if __name__ == "__main__":
    unittest.main()
