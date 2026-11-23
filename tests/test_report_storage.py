# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The report of a worker is stored only when its Result is accepted.

The six success rows (``reconcile.REPORT_VERBS``) store it as
``worker_report`` evidence bound to the Work and name it in their event;
every other fold discards it."""

from __future__ import annotations

import copy
import json
import unittest

import engine
import reconcile
import state
import work_history
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

    def test_an_invalid_report_never_fails_the_fold(self):
        rec = dev.running()
        outcome = with_report(rec, publication(rec, "published"))
        outcome["worker_report"]["expected_revision"] += 1
        new, staged = fold(rec, outcome)
        self.assertEqual(engine.state_key(new), "development.waiting_ci")
        self.assertEqual(stored(staged), [])


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


if __name__ == "__main__":
    unittest.main()
