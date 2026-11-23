# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The report of a worker is stored only when its Result is accepted.

The six success rows (``reconcile.REPORT_VERBS``) store it as
``worker_report`` evidence bound to the Work and name it in their event;
every other fold discards it. A Result whose push may have landed (an
``uncertain`` Result held for a publish-only retry, a retry rejected as
stale behind the operation of an earlier attempt) stores it with the
commit it proposed (``reconcile.pushed_report_evidence``)."""

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
from tests.test_late_results import PENDING, publication
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

    def test_a_late_result_whose_push_landed_stores_its_report(self):
        """A late Result whose head was read back on the branch is the work
        of a successful worker: CI runs on that head, so the next repair
        must read what was tried there."""
        rec = dev.running()
        late = with_report(rec, publication(rec, "published"), summary="Pinned the QEMU row.")
        late["candidate"] = SHA_B
        late["task"]["revision"] = late["worker_report"]["expected_revision"] = rec["revision"] + 7
        out = dev.tick(rec, [late], observe=False)
        self.assertEqual(dev.kinds(out), ["record_late_effect"])
        [evidence] = stored(out.staged)
        self.assertEqual((evidence["data"]["verb"], evidence["data"]["head_after"]),
                         ("record_late_effect", SHA_B))
        self.assertIn(f"-r{rec['revision'] + 7}-", evidence["evidence_id"])
        event = events(out.staged)[0]
        self.assertEqual(event["details"]["report_evidence_id"], evidence["evidence_id"])
        # The repair after failing CI on that head reads the report.
        from tests.test_work_history import ci_evidence
        ci = dict(ci_evidence("ci-9", SHA_B, at="2026-11-23T10:00:00+00:00"),
                  contribution_id=rec["contribution_id"])
        doc = work_history.document(rec, {"dedupe_key": "k", "task": "development_repair"},
                                    {evidence["evidence_id"]: evidence, "ci-9": ci})
        [report_entry, ci_entry] = doc["timeline"]
        self.assertEqual(report_entry["report"]["summary"], "Pinned the QEMU row.")
        self.assertEqual(ci_entry["after_report"], evidence["evidence_id"])

    def test_a_late_result_that_published_nothing_stores_nothing(self):
        rec = dev.running()
        for name in ("rejected_validation", "infra_error"):
            with self.subTest(result=name):
                late = with_report(rec, publication(rec, name))
                late["candidate"] = SHA_B
                late["task"]["revision"] = rec["revision"] + 7
                out = dev.tick(rec, [late], observe=False)
                self.assertEqual(dev.kinds(out), ["record_late_effect"])
                self.assertEqual(stored(out.staged), [])

    def test_a_late_uncertain_result_stores_the_commit_it_proposed(self):
        """A late Result whose push was not read back may have landed: its
        report is stored with the commit it proposed (``head_candidate``),
        never as ``head_after``. The history treats it as the report of the
        head only when the recorded head, or the head a CI run ran on, is
        that very commit; otherwise the report that published the head
        stays the report of the head."""
        from tests.test_work_history import ci_evidence, report_evidence
        rec = dev.running()
        late = with_report(rec, publication(rec, "uncertain"), summary="Pinned the QEMU row.")
        late["candidate"] = SHA_B
        late["task"]["revision"] = late["worker_report"]["expected_revision"] = rec["revision"] + 7
        out = dev.tick(rec, [late], observe=False)
        self.assertEqual(dev.kinds(out), ["record_late_effect"])
        [evidence] = stored(out.staged)
        self.assertEqual((evidence["data"]["head_after"], evidence["data"]["head_candidate"]),
                         (None, SHA_B))
        older = report_evidence("report-older", at="2026-11-01T00:00:00+00:00",
                                head_before="c" * 40, head_after=SHA_A)
        ci = dict(ci_evidence("ci-9", SHA_B, at="2026-11-23T10:00:00+00:00"),
                  contribution_id=rec["contribution_id"])
        known = {evidence["evidence_id"]: evidence, "report-older": older, "ci-9": ci}
        work_ = {"dedupe_key": "k", "task": "development_repair"}
        landed = dict(rec, fork=dict(rec["fork"], head_sha=SHA_B))
        doc = work_history.document(landed, work_, known)
        ci_entry = next(t for t in doc["timeline"] if t["kind"] == "ci")
        self.assertEqual(ci_entry["after_report"], evidence["evidence_id"])
        timeline = work_history._entries(landed, known)[1]
        self.assertEqual(work_history.protected_reports(landed, timeline)[1]["evidence_id"],
                         evidence["evidence_id"])
        # The push did not land: the recorded head is the older one.
        lost = dict(rec, fork=dict(rec["fork"], head_sha=SHA_A))
        timeline = work_history._entries(lost, {k: v for k, v in known.items() if k != "ci-9"})[1]
        self.assertEqual(work_history.protected_reports(lost, timeline)[1]["evidence_id"],
                         "report-older")

    def test_a_late_result_whose_report_cannot_be_stored_is_recorded_as_such(self):
        rec = dev.running()
        late = with_report(rec, publication(rec, "published"))
        late["candidate"] = SHA_B
        late["task"]["revision"] = rec["revision"] + 7
        late["worker_report"]["expected_revision"] += 1
        out = dev.tick(rec, [late], observe=False)
        self.assertEqual(dev.kinds(out), ["record_late_effect"])
        self.assertEqual(stored(out.staged), [])
        self.assertTrue(events(out.staged)[0]["details"]["report"].startswith("not stored: "))
        self.assertFalse(out.failures)

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


class TestUncertainPublicationHistory(unittest.TestCase):
    """The owner's case after a lost push response: patch Work U pushes H2
    but its push is not read back (`uncertain`); the publish-only retry
    confirms it, CI fails on H2, the repair Work R answers `no_change` and
    its rerun fails again. The Work allocated next reads U's report as the
    report of the head, whole or reduced, with the CI on H2 linked to it,
    even when the history is condensed and an older report P published the
    head before."""

    def run_chain(self, *, stale):
        from datetime import timedelta

        import recovery
        from tests.test_late_results import ci_snapshot
        from tests.test_work_history import _big_report, report_evidence, steer, steering_evidence
        cid = dev.running()["contribution_id"]
        older = report_evidence("report-p", at="2026-11-01T00:00:00+00:00",
                                head_before="c" * 40, head_after=SHA_A,
                                **_big_report("p", 0.7))
        older["contribution_id"] = cid
        heavy = steering_evidence("steering-0", [
            steer(i, "Keep the QEMU row; do not touch aarch64. " * 25,
                  at=f"2026-11-{1 + i % 20:02d}T05:00:00Z") for i in range(30)],
            at="2026-11-20T00:00:00+00:00")
        heavy["contribution_id"] = cid
        evidence = {"report-p": older, "steering-0": heavy}
        ops: dict = {}
        now = dev.T

        def step(rec, pubs=(), minutes=0, **kw):
            out = dev.tick(rec, list(pubs), evidence=evidence, operations=ops,
                           now=now + timedelta(minutes=minutes), **kw)
            self.assertEqual([str(f) for f in out.failures], [])
            for kind, staged in out.staged:
                if kind == "evidence":
                    evidence[staged["evidence_id"]] = staged
                if kind == "operations":
                    ops[staged["operation_id"]] = staged
            return dev.final(out, rec), dev.kinds(out)

        def settle(rec):
            # The development PR operations the tick planned are carried out.
            for key, op in list(ops.items()):
                if op["status"] == "planned":
                    ops[key] = recovery.record_discovery(dict(op, status="uncertain"),
                                                         "confirmed", "done", now.isoformat())
            return dict(rec, fork=dict(rec["fork"], pr_number=12), active_operation_id=None)

        start = dev.running(fork=dict(dev.record()["fork"], pr_number=None))
        op = dev.branch_operation(start)
        lost = with_report(start, dev.uncertain(start, op), **_big_report("u", 0.7))
        rec, verbs = step(start, [lost], observe=False)
        self.assertEqual(verbs, ["schedule_publish_retry"])
        # The held report is stored at once, with the commit it proposed.
        [held] = [e for e in evidence.values()
                  if e["kind"] == "worker_report" and e["evidence_id"] != "report-p"]
        self.assertEqual((held["data"]["head_after"], held["data"]["head_candidate"],
                          held["data"]["verb"]), (None, SHA_B, "schedule_publish_retry"))
        if stale:
            ops[op["operation_id"]] = dev.branch_operation(start, confirmed=True)
            retry = dev.retried(rec, "rejected_stale")
        else:
            retry = dev.retried(rec)
        rec, verbs = step(rec, [with_report(start, retry, **_big_report("u", 0.7))],
                          minutes=10, head=SHA_B)
        self.assertEqual(verbs, ["record_publish_success", "record_head_change"])
        [own] = [e for e in evidence.values()
                 if e["kind"] == "worker_report" and e["evidence_id"] != "report-p"]
        # The success fold stores the same evidence again, upgraded.
        self.assertEqual(own["evidence_id"], held["evidence_id"])
        self.assertEqual(own["data"]["head_after"], SHA_B)
        self.assertNotIn("head_candidate", own["data"])
        rec, verbs = step(settle(rec), minutes=20, head=SHA_B, verdict="failed")
        self.assertEqual(verbs, ["record_ci_failure", "request_repair"])
        repair = rec
        rerun = dict(with_report(repair, dev.rerun_outcome(repair), **_big_report("r", 0.7)),
                     publication_artifact_id=1001, worker_run_id="556")
        rec, verbs = step(rec, [rerun], minutes=40, head=SHA_B)
        self.assertEqual(verbs, ["request_ci_rerun"])

        def attempt(verdict):
            snapshot = ci_snapshot(SHA_B, verdict)
            snapshot.runs[0]["run_attempt"] = 2
            return snapshot
        rec, verbs = step(rec, minutes=50, head=SHA_B, ci=attempt("failed"))
        self.assertEqual(verbs, ["record_ci_failure"])
        rec, verbs = step(settle(rec), minutes=60, head=SHA_B, ci=attempt("failed"))
        self.assertEqual(verbs, ["request_repair"])
        key = rec["active_work"]["dedupe_key"]
        history = evidence[work_history.evidence_id(key)]["data"]
        return own, history

    def test_the_report_of_the_confirmed_head_is_protected(self):
        for stale in (False, True):
            with self.subTest(stale=stale):
                own, history = self.run_chain(stale=stale)
                self.assertTrue(history["condensed"])
                entries = {t.get("evidence_id"): t for t in history["timeline"]}
                head = entries[own["evidence_id"]]
                self.assertFalse(head.get("condensed"))
                self.assertEqual(head["head_after"], SHA_B)
                self.assertEqual(head["report"]["summary"][:1], "u")
                for ci in (t for t in history["timeline"] if t["kind"] == "ci"):
                    self.assertEqual(ci.get("after_report"), own["evidence_id"])
                older = entries.get("report-p")
                self.assertTrue(older is None or older.get("condensed"))


class TestHeldReportOutlivesTheHold(unittest.TestCase):
    """An `uncertain` Result is marked processed when it is held: its push
    may have landed, so its report is stored at the hold, with the commit it
    proposed as ``head_candidate``, whatever ends the hold. The owner's case:
    the push of H2 landed but its response was lost, the branch observation
    confirms H2, and the hold ends by anything but a retry Result (the
    retries are exhausted, the owner pauses or parks, or the controller
    stops on an error or an identity change). After the resume, CI fails on
    H2 and the repair Work reads the report with that CI linked to it."""

    def chain(self, end):
        from datetime import timedelta
        from unittest import mock

        import commands
        import controller
        import recovery
        from tests.helpers import apply_command
        evidence: dict = {}
        ops: dict = {}
        now = dev.T

        def at(minutes):
            return now + timedelta(minutes=minutes)

        def step(rec, pubs=(), minutes=0, failures=0, **kw):
            out = dev.tick(rec, list(pubs), evidence=evidence, operations=ops, now=at(minutes),
                           **kw)
            self.assertEqual(len(out.failures), failures, [str(f) for f in out.failures])
            for kind, item in out.staged:
                if kind == "evidence":
                    evidence[item["evidence_id"]] = item
                if kind == "operations":
                    ops[item["operation_id"]] = item
            return dev.final(out, rec), dev.kinds(out)

        def command(rec, text, minutes):
            out = apply_command(rec, commands.parse(text), operations=ops,
                                now=at(minutes).isoformat())
            self.assertTrue(out.accepted, out.reason)
            return out.record

        start = dev.running()
        op = dev.branch_operation(start)
        rec, verbs = step(start, [with_report(start, dev.uncertain(start, op))], observe=False)
        self.assertEqual(verbs, ["schedule_publish_retry"])
        [held] = [e for e in evidence.values() if e["kind"] == "worker_report"]
        self.assertEqual((held["data"]["verb"], held["data"]["head_after"],
                          held["data"]["head_candidate"]),
                         ("schedule_publish_retry", None, SHA_B))
        ops[op["operation_id"]] = op
        rec, _ = step(rec, minutes=5, head=SHA_B)
        self.assertEqual(ops[op["operation_id"]]["status"], "confirmed")
        minutes = 5
        if end == "exhausted":
            # The retries return nothing until `publish_recovery` is spent.
            while engine.state_key(rec) == "development.agent_running":
                minutes += 10
                self.assertLess(minutes, 4000)
                rec, verbs = step(rec, minutes=minutes, head=SHA_B)
            self.assertEqual(verbs, ["exhaust_publish_recovery"])
        elif end == "pause":
            rec = command(rec, '/upstreaming pause reason="look"', minutes + 1)
        elif end == "park":
            rec = command(rec, '/upstreaming park reason="later"', minutes + 1)
        elif end == "pause_on_processing_error":
            with mock.patch("observe_ci._observe_development", side_effect=KeyError("x")):
                rec, verbs = step(rec, minutes=minutes + 1, head=SHA_B, failures=1)
            self.assertEqual(verbs, ["pause_on_processing_error"])
        elif end == "pause_on_identity_change":
            result = controller.tick(
                contributions={"c": copy.deepcopy(rec)}, operations=dict(ops), consumed={},
                projects={}, policy=POLICY, inputs=controller.Inputs(
                    now=at(minutes + 1), issues=[], comments=[],
                    pressure=reconcile.capacity.Pressure(ok=True),
                    upstream_snapshots={"example/proj": {"repository_id": 99,
                                                         "base_ref": "main",
                                                         "base_sha": "c" * 40}}))
            self.assertEqual([e["kind"] for k, e in result.staged if k == "events"],
                             ["pause_on_identity_change"])
            rec = [r for k, r in result.staged if k == "contributions"][-1]
        # The hold ended without a retry Result: exhausted, or cleared.
        self.assertEqual((rec["pending_publication"] or {}).get("exhausted"),
                         True if end == "exhausted" else None)
        if end == "park":
            return rec, evidence, held
        rec = command(rec, f'/upstreaming resume revision={rec["revision"]} reason="ok"',
                      minutes + 2)
        for verdict in ("pending", "failed", "failed"):
            minutes += 10
            for key, planned in list(ops.items()):
                if planned["status"] == "planned":
                    ops[key] = recovery.record_discovery(
                        dict(planned, status="uncertain"), "confirmed", "done",
                        at(minutes).isoformat())
            rec, _ = step(dict(rec, active_operation_id=None), minutes=minutes, head=SHA_B,
                          verdict=verdict)
            if rec.get("active_work"):
                break
        self.assertEqual(rec["active_work"]["task"], "development_repair")
        return rec, evidence, held

    def test_the_repair_reads_the_held_report_with_the_failing_ci(self):
        for end in ("exhausted", "pause", "pause_on_processing_error",
                    "pause_on_identity_change"):
            with self.subTest(end=end):
                rec, evidence, held = self.chain(end)
                history = evidence[work_history.evidence_id(
                    rec["active_work"]["dedupe_key"])]["data"]
                entries = {t.get("evidence_id"): t for t in history["timeline"]}
                report_entry = entries[held["evidence_id"]]
                self.assertEqual(report_entry.get("head_candidate"), SHA_B)
                [ci] = [t for t in history["timeline"] if t["kind"] == "ci"]
                self.assertEqual((ci["head"], ci.get("after_report")),
                                 (SHA_B, held["evidence_id"]))
                self.assertEqual([r and r["evidence_id"] for r in
                                  work_history.protected_reports(rec, history["timeline"])],
                                 [held["evidence_id"], held["evidence_id"]])

    def test_a_park_keeps_the_held_report_for_the_next_generation(self):
        import commands
        from tests.helpers import apply_command
        rec, evidence, held = self.chain("park")
        self.assertEqual(engine.state_key(rec), "parked.idle")
        restarted = apply_command(rec, commands.parse(
            f'/upstreaming refresh revision={rec["revision"]} reason="again"'),
            now="2026-11-23T10:00:00+00:00")
        self.assertEqual(restarted.verb, "restart_generation")
        nxt = restarted.record
        doc = work_history.document(nxt, {
            "dedupe_key": f"{nxt['contribution_id']}@g{nxt['generation']}.r{nxt['revision']}"
                          ":planning", "task": "planning", "generation": nxt["generation"],
            "revision": nxt["revision"]}, evidence)
        [entry] = [t for t in doc["timeline"] if t["kind"] == "worker_report"]
        self.assertEqual((entry["evidence_id"], entry.get("head_candidate")),
                         (held["evidence_id"], SHA_B))

    def test_an_uncertain_result_that_spends_the_budget_keeps_the_report(self):
        rec = dev.running(pending_publication=dict(dev.HELD, attempts=99))
        op = dev.branch_operation(rec)
        out = dev.tick(rec, [with_report(rec, dict(dev.uncertain(rec, op), attempt=100))],
                       observe=False)
        self.assertEqual(dev.kinds(out), ["exhaust_publish_recovery"])
        [evidence] = stored(out.staged)
        self.assertEqual((evidence["data"]["verb"], evidence["data"]["head_candidate"]),
                         ("exhaust_publish_recovery", SHA_B))
        self.assertEqual(events(out.staged)[0]["details"]["report_evidence_id"],
                         evidence["evidence_id"])

    def test_a_report_that_cannot_be_stored_at_the_hold_pauses(self):
        """Like a success row: the gate accepted the Result with its report,
        so a report the reconciler cannot store pauses the contribution
        instead of holding the Result without it."""
        rec = dev.running()
        lost = with_report(rec, dev.uncertain(rec, dev.branch_operation(rec)))
        del lost["worker_report"]
        lost["worker_report_error"] = "report.json cannot be read: gone"
        out = dev.tick(rec, [lost], observe=False)
        self.assertEqual(dev.kinds(out), ["pause_on_processing_error"])
        self.assertIn("carries no report that can be stored",
                      events(out.staged)[0]["details"]["message"])
        self.assertEqual(stored(out.staged), [])

    def test_a_stale_retry_behind_an_open_operation_keeps_the_report(self):
        """The publish-only retry of a held Result is rejected as stale while
        the operation of the earlier attempt is still open: the push may
        have landed, so `replan_development` stores the report with the
        commit that operation proposed."""
        rec = dev.running(pending_publication=dict(dev.HELD))
        op = dev.branch_operation(rec)
        self.assertEqual(op["status"], "uncertain")
        out = dev.tick(rec, [with_report(rec, dev.retried(rec, "rejected_stale"))],
                       observe=False, operations={op["operation_id"]: op})
        self.assertEqual(dev.kinds(out)[:1], ["replan_development"])
        [evidence] = stored(out.staged)
        self.assertEqual((evidence["data"]["verb"], evidence["data"]["head_after"],
                          evidence["data"]["head_candidate"]),
                         ("replan_development", None, SHA_B))
        self.assertEqual(events(out.staged)[0]["details"]["report_evidence_id"],
                         evidence["evidence_id"])
        # Without an operation of an earlier attempt nothing landed: the
        # report is discarded.
        out = dev.tick(rec, [with_report(rec, dev.retried(rec, "rejected_stale"))],
                       observe=False)
        self.assertEqual((dev.kinds(out)[:1], stored(out.staged)), (["replan_development"], []))


class TestHeldRevisionReport(unittest.TestCase):
    """The same in `upstream_review.agent_running`."""

    def run_hold(self, end):
        from datetime import timedelta

        import commands
        from tests.helpers import apply_command
        evidence: dict = {}
        ops: dict = {}

        def step(rec, pubs=(), now=up.T, **kw):
            out = up.tick(rec, list(pubs), evidence=evidence, operations=ops, now=now, **kw)
            self.assertEqual([str(f) for f in out.failures], [])
            for kind, item in out.staged:
                if kind == "evidence":
                    evidence[item["evidence_id"]] = item
                if kind == "operations":
                    ops[item["operation_id"]] = item
            return up.final(out, rec), up.kinds(out)
        rec = up.record("agent_running", active_work=dict(up.REVISION_WORK))
        lost = up.branch_operation(rec, SHA_B, confirmed=False)
        rec, verbs = step(rec, [with_report(rec, dict(
            publication(rec, "uncertain"), candidate=SHA_B, operation=lost))])
        self.assertEqual(verbs, ["schedule_publish_retry"])
        [held] = [e for e in evidence.values() if e["kind"] == "worker_report"]
        self.assertEqual((held["data"]["head_after"], held["data"]["head_candidate"]),
                         (None, SHA_B))
        ops[lost["operation_id"]] = lost
        now = up.T
        if end == "exhausted":
            while engine.state_key(rec) == "upstream_review.agent_running":
                now += timedelta(hours=1)
                self.assertLess(now, up.T + timedelta(days=3))
                rec, verbs = step(rec, now=now, pr=up.pull(head=SHA_B), branch=SHA_B)
            self.assertEqual(verbs, ["exhaust_publish_recovery"])
        else:
            paused = apply_command(rec, commands.parse('/upstreaming pause reason="x"'),
                                   operations=ops, now=now.isoformat())
            self.assertTrue(paused.accepted, paused.reason)
            rec = paused.record
        self.assertEqual((rec["pending_publication"] or {}).get("exhausted"),
                         True if end == "exhausted" else None)
        resumed = apply_command(rec, commands.parse(
            f'/upstreaming resume revision={rec["revision"]} reason="ok"'),
            operations=ops, now=now.isoformat())
        self.assertTrue(resumed.accepted, resumed.reason)
        rec, verbs = step(resumed.record, now=now + timedelta(minutes=30),
                          pr=up.pull(head=SHA_B), branch=SHA_B, capacity_ok=True)
        self.assertEqual(verbs, ["request_review_revision"])
        history = evidence[work_history.evidence_id(rec["active_work"]["dedupe_key"])]["data"]
        return history, held

    def test_the_next_revision_reads_the_held_report(self):
        for end in ("exhausted", "pause"):
            with self.subTest(end=end):
                history, held = self.run_hold(end)
                [entry] = [t for t in history["timeline"] if t["kind"] == "worker_report"]
                self.assertEqual((entry["evidence_id"], entry.get("head_candidate")),
                                 (held["evidence_id"], SHA_B))

    def test_a_stale_retry_behind_a_confirmed_operation_keeps_the_report(self):
        """A revision retry rejected as stale behind the confirmed operation
        of an earlier attempt takes `reject_patch_proposal`; that push
        landed, so the report is stored with its commit."""
        rec = up.record("agent_running", active_work=dict(up.REVISION_WORK),
                        pending_publication=dict(PENDING))
        landed = up.branch_operation(rec, SHA_B, confirmed=True)
        retry = dict(publication(rec, "rejected_stale"), attempt=2)
        out = up.tick(rec, [with_report(rec, retry)], operations={landed["operation_id"]: landed},
                      pr=up.pull(head=SHA_B), branch=SHA_B)
        self.assertEqual(up.kinds(out)[:1], ["reject_patch_proposal"])
        [evidence] = stored(out.staged)
        self.assertEqual((evidence["data"]["verb"], evidence["data"]["head_after"],
                          evidence["data"]["head_candidate"]),
                         ("reject_patch_proposal", None, SHA_B))


if __name__ == "__main__":
    unittest.main()
