# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The review of every development patch before it is published.

The worker's trusted publication job verifies the patch and publishes
nothing (``verified``); the reconciler holds it (``hold_patch_for_review``)
and the controller allocates its review Work; a passing review makes it
pending publication (``accept_patch_review``); a failed one returns it for
repair with the review as pending feedback (``reject_patch_review``), until
``dev_reviews`` is exhausted and the patch is published anyway
(``publish_after_failed_reviews``). Only ``development.publishing``
publishes it, pinned to the reviewed commit. Every review is evidence that
the history of every later Work carries, and nothing about it is posted on
the tracking issue."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta

import capacity
import controller
import engine
import fork_publish
import lifecycle as lc
import patch_review
import reconcile
import state
import status_ledger
import work
import work_history
from tests.helpers import NOW, SHA_A, contribution, policy
from tests.test_worker_report import HISTORY, report

POLICY = policy(read_only=False)
DIGEST = "d" * 64
CANDIDATE = "c" * 40
TREE = "e" * 40
CHANGED = [".github/workflows/wheels.yml"]


def running(task="development_patch", *, feedback=(), revision=3, **over):
    """A record whose patch Work runs, with the review contract."""
    key = f"example--proj/12@g0.r{revision}:{task}"
    rec = contribution(activity="agent_running", revision=revision, active_work={
        "dedupe_key": key, "task": task, "generation": 0, "revision": revision, "attempt": 1,
        "requested_at": NOW, "deadline_at": NOW, "dispatched_run_id": "4000",
        "reason_ids": ["plan-1", *feedback], "feedback_evidence_ids": list(feedback)},
        pending_feedback_evidence_ids=list(feedback), plan_evidence_id="plan-1", **over)
    return rec


def worker_task(rec, *, review=True, history=False):
    work_ = rec["active_work"]
    task = {"task": work_["task"], "contribution_id": rec["contribution_id"],
            "revision": work_["revision"], "generation": 0, "dedupe_key": work_["dedupe_key"],
            "expected_head": SHA_A, "fork": rec["fork"]["name"], "ref": "riscv64-dev",
            "context": {"requested_at": NOW}}
    if review:
        task["review"] = "required"
    if history:
        task["context"]["history"] = {"sha256": HISTORY}
    return task


def verified(rec, *, run="4242", artifact=901, history=False):
    task = worker_task(rec, history=history)
    out = {"status": "verified", "candidate": CANDIDATE, "tree": TREE, "patch_digest": DIGEST,
           "changed_files": CHANGED, "operation_kind": "append", "expected_head": SHA_A,
           "task": task, "worker_run_id": run, "publication_artifact_id": artifact}
    if history:
        out["worker_report"] = report(contribution_id=rec["contribution_id"],
                                      expected_revision=task["revision"], task=task["task"])
    return out


def tick(rec, pubs=(), *, now=None, evidence=None):
    snap = reconcile.Snapshot(contributions={"c": rec}, operations={}, consumed={},
                              projects={}, evidence=dict(evidence or {}))
    return reconcile.tick(None, snap, policy=POLICY,
                          now=now or datetime.fromisoformat(NOW), publications=list(pubs),
                          pressure=capacity.Pressure(ok=True), observe=False)


def final(out):
    return [r for k, r in out.staged if k == "contributions"][-1]


def kinds(out):
    return [e["kind"] for k, e in out.staged if k == "events"]


def staged(out, kind):
    return [r for k, r in out.staged if k == "evidence" and r["kind"] == kind]


def review_task(rec, evidence=None):
    request = work.request_from_active(rec)
    _paths, context = work.payload_inputs(request, rec, evidence or {}, plan_reference=lambda eid: {
        "repository": "riseproject-dev/porting-agent", "ref": "0" * 40,
        "path": f".state/evidence/{eid}.json", "sha256": "0" * 64})
    return work.payload(request, rec, control_repo_id="1", allowed_paths=CHANGED,
                        context=context)


def review_result(rec, *, severity=None, run="5000", artifact=902, evidence=None):
    task = review_task(rec, evidence)
    findings = [] if severity is None else [{
        "id": "f1", "severity": severity, "category": "tests", "file": CHANGED[0], "line": 3,
        "title": "Tests are skipped", "detail": "The riscv64 job skips the test suite."}]
    value = {"schema_version": 1, "contribution_id": rec["contribution_id"],
             "expected_revision": task["revision"], "expected_head": task["expected_head"],
             "patch_digest": DIGEST, "candidate": CANDIDATE,
             "verdict": "fail" if severity in patch_review.BLOCKING else "pass",
             "summary": "The patch adds the riscv64 row.", "findings": findings}
    out = patch_review.check(task, value)
    return dict(out, task=task, worker_run_id=run, publication_artifact_id=artifact)


def held(rec, **kw):
    """The record once the verified patch is held and its review requested."""
    out = tick(rec, [verified(rec, **kw)])
    return final(out), out


def published(rec, *, run="4242", artifact=903):
    """The Result of the publish-only retry of the held patch."""
    task = worker_task({**rec, "active_work": {"task": rec["held_patch"]["work"].split(":")[1],
                                               "revision": rec["held_patch"]["revision"],
                                               "dedupe_key": rec["held_patch"]["work"]}})
    op = {"operation_id": "op-publish-1", "contribution_id": rec["contribution_id"],
          "kind": "branch_update", "status": "confirmed", "revision": task["revision"],
          "target": {"ref": "riscv64-dev", "expected_head": SHA_A},
          "proposed": {"commit": CANDIDATE, "changed_files": CHANGED}}
    return {"status": "published", "candidate": CANDIDATE, "operation": op, "task": task,
            "worker_run_id": run, "publication_artifact_id": artifact,
            "artifact": {"work_id": "w", "claim_id": "c", "artifact_id": artifact, "name": "n",
                         "digest": "sha256:" + "0" * 64, "run_id": run}, "attempt": 1}


class TestHold(unittest.TestCase):
    def test_a_verified_patch_is_held_and_its_review_requested(self):
        rec = running(feedback=["review-1"])
        new, out = held(rec)
        self.assertEqual(kinds(out), ["hold_patch_for_review", "request_patch_review"])
        self.assertEqual((engine.state_key(new), new["revision"]),
                         ("development.reviewing", rec["revision"] + 1))
        pin = new["held_patch"]
        self.assertEqual({k: pin[k] for k in ("worker_run_id", "artifact_id", "work", "revision",
                                              "patch_digest", "candidate", "tree",
                                              "expected_head", "operation")},
                         {"worker_run_id": "4242", "artifact_id": 901,
                          "work": rec["active_work"]["dedupe_key"], "revision": 3,
                          "patch_digest": DIGEST, "candidate": CANDIDATE, "tree": TREE,
                          "expected_head": SHA_A, "operation": "append"})
        self.assertEqual(pin["feedback_evidence_ids"], ["review-1"])
        self.assertEqual(pin["reason_ids"], ["plan-1", "review-1"])
        # Nothing was published: the head and the pending feedback are kept.
        self.assertEqual(new["fork"]["head_sha"], SHA_A)
        self.assertEqual(new["pending_feedback_evidence_ids"], ["review-1"])
        self.assertIsNone(new["pending_publication"])
        # The review Work, on the reviewer profile, carries no feedback and
        # names the held patch and the reasons of the worker's Work.
        work_ = new["active_work"]
        self.assertEqual((work_["task"], work_["feedback_evidence_ids"], work_["reason_ids"]),
                         ("development_review", [], ["plan-1", "review-1"]))
        self.assertEqual(work.profile_for(work_["task"]), "reviewer")
        self.assertEqual(out.report["requested_effects"][0]["effect_contract"], {"kind": "none"})
        task = review_task(new)
        self.assertEqual(task["context"]["patch"]["candidate"], CANDIDATE)
        self.assertNotIn("review", task)

    def test_the_report_is_stored_at_the_hold_with_the_commit_it_proposed(self):
        rec = running()
        new, out = held(rec, history=True)
        [stored] = staged(out, "worker_report")
        self.assertEqual(stored["data"]["verb"], "hold_patch_for_review")
        self.assertEqual((stored["data"]["head_after"], stored["data"]["head_candidate"]),
                         (None, CANDIDATE))
        self.assertEqual(new["held_patch"]["report_evidence_id"], stored["evidence_id"])
        # The review names the report it reads, at its pinned commit.
        request = work.request_from_active(new)
        _paths, context = work.payload_inputs(
            request, new, {stored["evidence_id"]: stored},
            plan_reference=lambda eid: {"repository": "r", "ref": "0" * 40,
                                        "path": f".state/evidence/{eid}.json", "sha256": "0" * 64,
                                        "url": "u"})
        self.assertEqual(context["report"]["evidence_id"], stored["evidence_id"])
        self.assertNotIn("url", context["report"])

    def test_work_without_the_review_contract_still_publishes_in_its_run(self):
        rec = running()
        pub = published({**rec, "held_patch": {"work": rec["active_work"]["dedupe_key"],
                                               "revision": rec["revision"]}})
        del pub["task"]["review"]
        out = tick(rec, [pub])
        self.assertEqual(kinds(out)[0], "record_publish_success")
        # The same Result of Work with the review contract has no row.
        out = tick(rec, [published({**rec, "held_patch": {
            "work": rec["active_work"]["dedupe_key"], "revision": rec["revision"]}})])
        self.assertEqual(kinds(out), ["pause_on_processing_error"])

    def test_a_failed_verification_is_retried_without_a_model_call(self):
        rec = running()
        pub = dict(verified(rec), status="infra_error", failure_class="infrastructure",
                   reason="could not fetch")
        retry = final(tick(rec, [pub]))
        self.assertEqual((engine.state_key(retry), retry["pending_publication"]["attempts"]),
                         ("development.agent_running", 1))
        # The retry verifies again; its Result is held like the worker's.
        new = final(tick(retry, [verified(rec, artifact=905)],
                         now=datetime.fromisoformat(NOW) + timedelta(minutes=1)))
        self.assertEqual(engine.state_key(new), "development.reviewing")
        self.assertIsNone(new["pending_publication"])
        self.assertEqual(new["held_patch"]["artifact_id"], 905)


class TestVerdicts(unittest.TestCase):
    def test_a_passing_review_makes_the_held_patch_pending_publication(self):
        reviewing, _ = held(running(feedback=["review-1"]))
        out = tick(reviewing, [review_result(reviewing, severity="minor")])
        new = final(out)
        self.assertEqual(kinds(out), ["accept_patch_review"])
        self.assertEqual((engine.state_key(new), new["revision"]),
                         ("development.publishing", reviewing["revision"]))
        [review] = staged(out, "patch_review")
        self.assertEqual((review["data"]["verdict"], review["data"]["disposition"]),
                         ("pass", "publish"))
        self.assertEqual(new["pending_publication"], {
            "worker_run_id": "4242", "attempts": 0, "last_reason": "",
            "next_attempt_at": (datetime.fromisoformat(NOW) + timedelta(
                minutes=reconcile.PUBLISH_RESULT_WAIT_MINUTES)).isoformat(),
            "launched_at": NOW, "feedback_evidence_ids": ["review-1"],
            "patch_review": {"evidence_id": review["evidence_id"], "verdict": "pass",
                             "published_anyway": False}})
        self.assertEqual(out.report["publication_retries"], [{
            "type": "retry_publication", "contribution_id": new["contribution_id"],
            "worker_run_id": "4242", "attempt": 1}])
        # The publication folds as the held patch: it clears exactly the
        # feedback its Work carried, the pin, and starts dev_reviews again.
        new = dict(new, pending_feedback_evidence_ids=["review-1", "review-9"],
                   budget=dict(new["budget"], review_rejections=2))
        done = tick(new, [published(new)])
        last = final(done)
        self.assertEqual(kinds(done)[0], "record_publish_success")
        self.assertEqual(engine.state_key(last), "development.waiting_ci")
        self.assertEqual(last["pending_feedback_evidence_ids"], ["review-9"])
        self.assertEqual((last["held_patch"], last["pending_publication"],
                          last["budget"]["review_rejections"]), (None, None, 0))

    def test_the_report_is_stored_again_with_the_published_head(self):
        reviewing, out = held(running(), history=True)
        [at_hold] = staged(out, "worker_report")
        publishing = final(tick(reviewing, [review_result(
            reviewing, evidence={at_hold["evidence_id"]: at_hold})]))
        pub = published(publishing)
        pub["task"]["context"]["history"] = {"sha256": HISTORY}
        pub["worker_report"] = report(contribution_id=publishing["contribution_id"],
                                      expected_revision=3, task="development_patch")
        [again] = staged(tick(publishing, [pub]), "worker_report")
        self.assertEqual(again["evidence_id"], at_hold["evidence_id"])
        self.assertEqual((again["data"]["head_after"], again["data"].get("head_candidate")),
                         (CANDIDATE, None))

    def test_a_failed_review_returns_the_patch_for_repair_with_its_findings(self):
        reviewing, _ = held(running(feedback=["review-1"]))
        out = tick(reviewing, [review_result(reviewing, severity="blocker")])
        new = final(out)
        [review] = staged(out, "patch_review")
        self.assertEqual(kinds(out)[0], "reject_patch_review")
        self.assertEqual(review["data"]["disposition"], "return_for_repair")
        self.assertEqual(engine.state_key(new)[:len("development.")], "development.")
        self.assertIsNone(new["held_patch"])
        self.assertEqual(new["budget"]["review_rejections"], 1)
        self.assertEqual(new["pending_feedback_evidence_ids"], ["review-1", review["evidence_id"]])
        # The next Work amends the patch: a repair that carries the review.
        work_ = new["active_work"]
        self.assertEqual(work_["task"], "development_repair")
        self.assertEqual(work_["feedback_evidence_ids"], ["review-1", review["evidence_id"]])
        self.assertIn(review["evidence_id"], work_["reason_ids"])
        item = work._reason_item(review)
        self.assertEqual(item["data"]["findings"][0]["severity"], "blocker")
        self.assertEqual(item["data"]["disposition"], "return_for_repair")

    def test_successive_failed_reviews_publish_the_patch_anyway(self):
        rec = running(feedback=["review-1"])
        limit = lc.budget_limit("dev_reviews", POLICY)
        self.assertEqual(limit, 2)
        for attempt in range(limit + 1):
            reviewing, _ = held(rec, run=str(4242 + attempt), artifact=901 + attempt)
            out = tick(reviewing, [review_result(reviewing, severity="major",
                                                 run=str(5000 + attempt),
                                                 artifact=950 + attempt)])
            rec = final(out)
            [review] = staged(out, "patch_review")
            self.assertEqual(review["data"]["failed_in_succession"], attempt + 1)
        self.assertEqual(kinds(out), ["publish_after_failed_reviews"])
        self.assertEqual(engine.state_key(rec), "development.publishing")
        self.assertEqual(review["data"]["disposition"], "publish_anyway")
        pending = rec["pending_publication"]
        self.assertEqual(pending["patch_review"], {"evidence_id": review["evidence_id"],
                                                   "verdict": "fail", "published_anyway": True})
        # The last review is not pending feedback: it requests no repair.
        self.assertNotIn(review["evidence_id"], rec["pending_feedback_evidence_ids"])
        self.assertEqual(pending["feedback_evidence_ids"], rec["held_patch"]["feedback_evidence_ids"])
        self.assertEqual(patch_review.comment_due(rec, {"status": "published"}),
                         review["evidence_id"])

    def test_an_unusable_review_is_work_without_a_usable_result(self):
        reviewing, _ = held(running())
        bad = dict(review_result(reviewing), candidate="0" * 40)
        out = tick(reviewing, [bad])
        self.assertEqual(kinds(out)[0], "allocate_work_retry")
        self.assertEqual(final(out)["held_patch"], reviewing["held_patch"])
        rejected = dict(patch_review.unusable("malformed"), task=review_task(reviewing),
                        worker_run_id="5000", publication_artifact_id=902)
        self.assertEqual(kinds(tick(reviewing, [rejected]))[0], "allocate_work_retry")

    def test_nothing_about_a_review_is_posted_on_the_tracking_issue(self):
        for verb in ("hold_patch_for_review", "request_patch_review", "accept_patch_review",
                     "reject_patch_review", "publish_after_failed_reviews", "expire_held_patch"):
            for e in lc.table_edges():
                if e.verb != verb:
                    continue
                event = {"kind": verb, "to": {}, "revision_bumped": True}
                self.assertFalse(status_ledger.notifies(event, e.source, e.target), (verb, e))


class TestHeldPatchEnds(unittest.TestCase):
    def test_an_expired_held_patch_is_never_reviewed(self):
        review_pending = dict(running(), activity="review_pending", active_work=None,
                              held_patch=held(running())[0]["held_patch"])
        later = datetime.fromisoformat(NOW) + timedelta(days=30)
        res = controller.tick(contributions={"c": review_pending}, operations={}, consumed={},
                              projects={}, policy=POLICY,
                              inputs=controller.Inputs(now=later,
                                                       pressure=capacity.Pressure(ok=True)))
        new = [r for k, r in res.staged if k == "contributions"][0]
        self.assertEqual([e["kind"] for k, e in res.staged if k == "events"][0],
                         "expire_held_patch")
        self.assertIsNone(new["held_patch"])
        self.assertEqual(engine.state_key(new), "development.fix_needed")

    def test_a_stop_in_a_review_state_discards_the_held_patch(self):
        reviewing, _ = held(running())
        publishing = final(tick(reviewing, [review_result(reviewing)]))
        review_pending = dict(reviewing, activity="review_pending", active_work=None)
        for rec in (review_pending, reviewing, publishing):
            for verb, extra in (("pause", {}), ("park_contribution", {"reason": "r"})):
                with self.subTest(state=engine.state_key(rec), verb=verb):
                    import commands
                    command = commands.Command(lc.FLAGS["paused"].set_by["pause"].command.split()[-1]
                                               if verb == "pause" else "park",
                                               {"revision": str(rec["revision"]), **extra})
                    out = engine.apply_transition(rec, verb, policy=POLICY, now=NOW,
                                                  command=command,
                                                  open_operations=())
                    self.assertTrue(out.accepted, out.reason)
                    self.assertIsNone(out.record["held_patch"])
                    self.assertIsNone(out.record["pending_publication"])
                    self.assertIsNone(out.record["active_work"])
                    self.assertEqual(engine.state_key(out.record),
                                     "development.fix_needed" if verb == "pause"
                                     else "parked.idle")

    def test_a_moved_head_or_a_rebuild_off_the_pin_publishes_nothing(self):
        reviewing, _ = held(running())
        publishing = final(tick(reviewing, [review_result(reviewing)]))
        stale = dict(published(publishing), status="rejected", failure_class="stale",
                     reason="fork head moved before push", operation=None)
        out = tick(publishing, [stale])
        self.assertEqual(kinds(out)[0], "replan_development")
        self.assertIsNone(final(out)["held_patch"])
        off = dict(published(publishing), status="rejected", failure_class="validation",
                   reason="the series rebuilds to another commit", operation=None)
        out = tick(publishing, [off])
        self.assertEqual(kinds(out)[:2], ["reject_patch_proposal", "request_patch"])
        self.assertIsNone(final(out)["held_patch"])

    def test_the_publication_check_pins_the_held_patch(self):
        reviewing, _ = held(running())
        publishing = final(tick(reviewing, [review_result(reviewing)]))
        task = published(publishing)["task"]
        args = dict(expected_revision=task["revision"], expected_head=SHA_A, observed_head=SHA_A,
                    ref="riscv64-dev", repository=publishing["fork"]["name"],
                    work_key=task["dedupe_key"], reviewed=True)
        self.assertIsNone(lc.publication_check(publishing, **args))
        self.assertIn("revision", lc.publication_check(publishing, **dict(args,
                                                                         expected_revision=9)))
        self.assertIn("Work id", lc.publication_check(publishing, **dict(args, work_key="other")))
        # A reviewed patch is never written from the state of its Work, and
        # the pin needs the pending publication.
        self.assertIn("only from development.publishing",
                      lc.publication_check(reviewing, **args))
        self.assertIn("no pending publication", lc.publication_check(
            dict(publishing, pending_publication=None), **args))

    def test_fork_publish_refuses_a_series_other_than_the_held_patch(self):
        reviewing, _ = held(running())
        publishing = final(tick(reviewing, [review_result(reviewing)]))
        task = dict(published(publishing)["task"], allowed_operations=["append"],
                    fork_repository_id=2, allowed_paths=CHANGED, reason_ids=[])
        proposal = {"schema_version": 1, "contribution_id": publishing["contribution_id"],
                    "outcome": "patch", "expected_revision": task["revision"],
                    "expected_head": SHA_A, "ref": "riscv64-dev", "reconstruction_base": SHA_A,
                    "operation": "append", "changed_files": CHANGED, "diagnosis": "d",
                    "progress": "requirement_fixed"}
        out = fork_publish.publish(record=publishing, task=task, proposal=proposal,
                                   mbox=b"another series", token=None,
                                   observe_head=lambda: SHA_A,
                                   scope=fork_publish.publisher.Scope(allowed_globs=CHANGED),
                                   policy=POLICY, now=NOW, save_operation=lambda op: None)
        self.assertEqual((out.status, out.failure_class), ("rejected", "validation"))
        self.assertIn("not the held patch", out.reason)
        # Verification is refused once the Work that wrote it is no longer running.
        out = fork_publish.verify(record=publishing, task=task, proposal=proposal, mbox=b"x",
                                  observe_head=lambda: SHA_A,
                                  scope=fork_publish.publisher.Scope(allowed_globs=CHANGED),
                                  policy=POLICY, now=NOW)
        self.assertEqual((out.status, out.failure_class), ("rejected", "stale"))
        # The preflight of the worker's run verifies, it never publishes.
        pf = fork_publish.preflight(record=running(), task=task, proposal=proposal,
                                    fork_lookup=lambda n: {"id": 2, "full_name": n})
        self.assertEqual((pf["publish"], pf["status"]), (False, "verify"))
        pf = fork_publish.preflight(record=publishing, task=task, proposal=proposal,
                                    fork_lookup=lambda n: {"id": 2, "full_name": n})
        self.assertEqual((pf["publish"], pf["status"]), (True, "publish"))


class TestLateResults(unittest.TestCase):
    def test_results_that_are_not_the_held_patch_or_its_review_are_late(self):
        reviewing, _ = held(running())
        publishing = final(tick(reviewing, [review_result(reviewing)]))
        for rec, pub in (
                # A review arriving after its Work was answered.
                (publishing, review_result(reviewing, artifact=990)),
                # Another worker run's publication of the same Work.
                (publishing, published(publishing, run="9999", artifact=991)),
                # A review where no review is requested.
                (running(), review_result(reviewing, artifact=992))):
            with self.subTest(state=engine.state_key(rec), status=pub["status"]):
                out = tick(rec, [pub])
                self.assertEqual(kinds(out), ["record_late_effect"])
                self.assertEqual(final(out)["held_patch"], rec["held_patch"])

    def test_a_review_after_a_pause_is_late(self):
        reviewing, _ = held(running())
        import commands
        paused = engine.apply_transition(
            reviewing, "pause", policy=POLICY, now=NOW,
            command=commands.Command("pause", {"revision": str(reviewing["revision"])})).record
        out = tick(paused, [review_result(reviewing)])
        self.assertEqual(kinds(out), ["record_late_effect"])


class TestHistory(unittest.TestCase):
    def test_every_review_is_a_history_entry_linked_to_its_report(self):
        reviewing, out = held(running(), history=True)
        [stored] = staged(out, "worker_report")
        fail = tick(reviewing, [review_result(reviewing, severity="major",
                                              evidence={stored["evidence_id"]: stored})])
        [review] = staged(fail, "patch_review")
        evidence = {stored["evidence_id"]: stored, review["evidence_id"]: review}
        rec = final(fail)
        doc = work_history.document(rec, rec["active_work"], evidence)
        entry = next(t for t in doc["timeline"] if t["kind"] == "patch_review")
        self.assertEqual((entry["verdict"], entry["disposition"], entry["report_evidence_id"],
                          entry["candidate"]),
                         ("fail", "return_for_repair", stored["evidence_id"], CANDIDATE))
        self.assertEqual(entry["findings"][0]["title"], "Tests are skipped")
        self.assertIn(review["evidence_id"], doc["sources"])
        short = work_history._short(dict(entry, seq=1))
        self.assertEqual(short["findings"], [{"severity": "major", "file": CHANGED[0],
                                              "title": "Tests are skipped"}])
        self.assertTrue(short["condensed"])


class TestLiveRecords(unittest.TestCase):
    def test_both_live_records_migrate_by_the_evented_field_migration(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / ".state/contributions"
        for name in ("luhenry--d2vm--3.json", "closeio--ciso8601--1.json"):
            path = root / name
            if not path.exists():
                continue
            with self.subTest(record=name):
                rec = json.loads(path.read_text())
                if "held_patch" in rec:
                    self.assertFalse(state.needs_field_migration(rec))
                    continue
                new, event = state.migrate_record_fields(rec, now=NOW)
                self.assertEqual(event["details"]["added"], ["held_patch"])
                self.assertEqual({k: v for k, v in new.items()
                                  if k not in ("held_patch", "updated_at")},
                                 {k: v for k, v in rec.items() if k != "updated_at"})
                work_ = new.get("active_work")
                if work_ and work_.get("task") == "development_patch":
                    # Work dispatched on the old worker lock carries no
                    # review contract: its Result still publishes.
                    pub = {"status": "published", "task": {"task": "development_patch"}}
                    self.assertFalse(fork_publish.review_required(pub["task"]))
                    edge = lc.find_edge("development.agent_running", "record_publish_success")
                    self.assertIn(lc.NO_REVIEW_CONTRACT, edge.transition.guard)


if __name__ == "__main__":
    unittest.main()


class TestWorkflows(unittest.TestCase):
    """The reviewer workflow writes nothing; the worker verifies instead of
    pushing a development patch; the publish-only retry pushes a reviewed
    patch and posts the comment of a patch published after failed reviews."""

    def setUp(self):
        from pathlib import Path

        import yaml

        self.root = Path(__file__).resolve().parents[1] / ".github/workflows"
        self.yaml = yaml

    def front(self, name):
        return self.yaml.safe_load((self.root / name).read_text().split("---", 2)[1])

    def test_the_reviewer_reads_and_diffs_without_a_credential(self):
        source = self.front("reviewer.md")
        self.assertEqual(source["model"], "anthropic/claude-fable-5-2")
        self.assertEqual((source["timeout-minutes"], source["max-turns"]), (45, 150))
        self.assertEqual(source["engine"]["command"], "/tmp/gh-aw/agent/pi-scrubbed")
        self.assertTrue(source["tools"]["work-queue"]["worker"])
        self.assertEqual(source["mcp-servers"]["github-authenticated"]["env"]["GITHUB_READ_ONLY"], "1")
        text = (self.root / "reviewer.md").read_text()
        for absent in ("secrets.RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY", "docker", "setup-qemu",
                       "reproduction-image", "id: fork-token", "permission-contents: write"):
            self.assertFalse(absent in text, absent)
        steps = "\n".join(step.get("run", "") for step in source["pre-agent-steps"])
        for check in ('"held patch digest mismatch"', '"the held series does not rebuild to the verified tree"',
                      "fetch_evidence .context.report worker_report", "fetch_evidence .context.history work_history",
                      ".name == \"porting-publication\" and (.expired | not)"):
            self.assertIn(check, steps)
        publish = source["jobs"]["publish"]
        self.assertEqual(publish["permissions"], {"contents": "read", "actions": "read"})
        run = "\n".join(step.get("run", "") for step in publish["steps"])
        self.assertIn("WORKFLOW_PATH: .github/workflows/reviewer.lock.yml", (self.root / "reviewer.md").read_text())
        self.assertIn("unset GH_TOKEN", run)
        self.assertIn("python -m patch_review check", run)
        self.assertLess(run.index("unset GH_TOKEN"), run.index("python -m patch_review check"))
        lock = (self.root / "reviewer.lock.yml").read_text()
        self.assertIn("GH_AW_PI_COMMAND: /tmp/gh-aw/agent/pi-scrubbed", lock)
        self.assertFalse("secrets.RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY" in lock)

    def test_the_worker_verifies_a_development_patch_without_a_token(self):
        source = self.front("worker.md")
        steps = source["jobs"]["publish"]["steps"]
        result = next(step for step in steps if step.get("id") == "result")
        run = result["run"]
        self.assertIn('if [ "$(jq -r .status ../in/preflight.json)" = verify ]; then', run)
        self.assertIn("python -m fork_publish verify", run)
        self.assertNotIn("UPSTREAMING_FORK_TOKEN", str(result.get("env")))
        token = next(step for step in steps if step.get("id") == "fork-token")
        self.assertEqual(token["if"], "steps.result.outputs.publish == 'true'")

    def test_the_publish_only_retry_verifies_publishes_and_comments(self):
        retry = self.yaml.safe_load((self.root / "publish-retry.yml").read_text())
        steps = retry["jobs"]["publish"]["steps"]
        result = next(step for step in steps if step.get("id") == "result")
        self.assertIn("python -m fork_publish verify", result["run"])
        comment = next(step for step in steps
                       if step.get("name", "").startswith("Post the latest review"))
        self.assertEqual(comment["if"], "steps.result.outputs.publish == 'true'")
        self.assertTrue(comment["continue-on-error"])
        self.assertIn("python -m patch_review comment", comment["run"])
        self.assertEqual(comment["env"]["UPSTREAMING_FORK_TOKEN"],
                         "${{ steps.fork-token.outputs.token }}")
        names = [step.get("name") for step in steps]
        self.assertLess(names.index("Rebuild, validate and push to riscv64 (exact-head lease)"),
                        names.index(comment["name"]))
        upload = next(step for step in steps
                      if str(step.get("uses", "")).startswith("actions/upload-artifact"))
        self.assertIn("in/review-comment.json", upload["with"]["path"].split())

    def test_the_controller_collects_and_follows_the_reviewer(self):
        text = (self.root / "controller.yml").read_text()
        self.assertIn("reviewer.lock.yml", text)
        dispatcher = self.front("dispatcher.md")
        self.assertEqual(dispatcher["safe-outputs"]["dispatch-workflow"]["workflows"],
                         ["planner", "worker", "reviewer"])
        self.assertEqual(work.PROFILE_WORKFLOWS["reviewer"], ".github/workflows/reviewer.lock.yml")
