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
the tracking issue. ``dev_reviews`` allows one failed review in succession:
the second publishes the patch anyway."""

from __future__ import annotations

import copy
import json
import unittest
import unittest.mock
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
           "task": task, "worker_run_id": run, "publication_artifact_id": artifact,
           "publication_run_id": run, "publication_series_digest": DIGEST}
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
             "expected_revision": task["revision"],
             "expected_head": task["context"]["patch"]["expected_head"], "patch_digest": DIGEST, "candidate": CANDIDATE,
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
                         "digest": "sha256:" + "0" * 64, "run_id": run}, "attempt": 1,
            "publication_stage": "publish"}


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
        # The retry verifies again; its Result is held like the worker's,
        # pinned to the artifact of the retry run that carries the series.
        later = datetime.fromisoformat(NOW) + timedelta(minutes=1)
        new = final(tick(retry, [dict(verified(rec, artifact=905), publication_run_id="7777")],
                         now=later))
        self.assertEqual(engine.state_key(new), "development.reviewing")
        self.assertIsNone(new["pending_publication"])
        self.assertEqual({k: new["held_patch"][k] for k in ("worker_run_id", "artifact_run_id",
                                                            "artifact_id")},
                         {"worker_run_id": "4242", "artifact_run_id": "7777", "artifact_id": 905})
        # An artifact without the verified series is not held: the
        # publish-only retry verifies the patch again.
        out = tick(rec, [dict(verified(rec), publication_series_digest=None)])
        self.assertEqual(kinds(out), ["schedule_publish_retry"])
        self.assertIsNone(final(out)["held_patch"])

    def test_the_reviewer_reads_the_series_a_retry_held(self):
        """The trusted pre-agent step of the reviewer reads the series of a
        patch held after a verification retry, by the run that owns its
        artifact, end to end from the fold of the retry's Result."""
        import hashlib
        import io
        import tempfile
        import zipfile
        from pathlib import Path

        from scripts import held_patch

        mbox = b"From 0 Mon Sep 17 00:00:00 2001\nSubject: [PATCH] riscv64\n\n---\n"
        digest = hashlib.sha256(mbox).hexdigest()
        rec = running()
        failed = dict(verified(rec), status="infra_error", failure_class="infrastructure",
                      reason="could not fetch", patch_digest=digest)
        retry = final(tick(rec, [failed]))
        later = datetime.fromisoformat(NOW) + timedelta(minutes=1)
        # The Result of the publish-only retry: the controller names it by
        # the worker run it publishes, and by the run and the series of the
        # artifact it collected.
        reviewing = final(tick(retry, [dict(verified(rec, artifact=905), patch_digest=digest,
                                            publication_run_id="7777",
                                            publication_series_digest=digest)], now=later))
        self.assertEqual(engine.state_key(reviewing), "development.reviewing")
        task = review_task(reviewing)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("patches.mbox", mbox)
            z.writestr("proposal.json", "{}")
            z.writestr("outcome.json", "{}")
        worker_artifact = io.BytesIO()
        with zipfile.ZipFile(worker_artifact, "w") as z:
            # The worker's own artifact: the verification failed, so it
            # carries no series.
            z.writestr("outcome.json", "{}")
        artifacts = {901: ("4242", worker_artifact.getvalue()), 905: ("7777", archive.getvalue())}

        def get(path, limit):
            ident = int(path.split("/artifacts/")[1].split("/")[0])
            run, data = artifacts[ident]
            if path.endswith("/zip"):
                return data
            return json.dumps({"id": ident, "name": "porting-publication", "expired": False,
                               "workflow_run": {"id": int(run)}}).encode()

        with tempfile.TemporaryDirectory() as tmp:
            status = held_patch.fetch(task, "patch", Path(tmp), repository="o/r", get=get,
                                      required=True)
            self.assertEqual((status["status"], status["artifact_run_id"]), ("verified", "7777"))
            self.assertEqual((Path(tmp) / "patches.mbox").read_bytes(), mbox)
            # The pin of the review rejected before: the retry's artifact
            # with the worker run, or the worker's artifact without a series.
            for wrong in ({"artifact_run_id": "4242"},
                          {"artifact_id": 901, "artifact_run_id": "4242"}):
                bad = dict(task, context=dict(task["context"],
                                              patch=dict(task["context"]["patch"], **wrong)))
                with self.subTest(wrong=wrong), self.assertRaises(held_patch.HeldPatchError):
                    held_patch.fetch(bad, "patch", Path(tmp), repository="o/r", get=get,
                                     required=True)
            other = io.BytesIO()
            with zipfile.ZipFile(other, "w") as z:
                z.writestr("patches.mbox", mbox.replace(b"riscv64", b"riscv32"))
                z.writestr("proposal.json", "{}")
            artifacts[905] = ("7777", other.getvalue())
            with self.assertRaises(held_patch.HeldPatchError):
                held_patch.fetch(task, "patch", Path(tmp), repository="o/r", get=get,
                                 required=True)


    def test_the_series_is_read_within_its_bounds(self):
        import io
        import os
        import stat
        import tempfile
        import zipfile
        from pathlib import Path

        import artifact
        from scripts import held_patch

        self.assertEqual(held_patch.FILES["patches.mbox"], fork_publish.MAX_MBOX_BYTES)
        self.assertEqual(held_patch.FILES["proposal.json"], artifact.ALLOWED["proposal.json"])
        task = {"context": {"patch": {"artifact_run_id": "4242", "artifact_id": 901,
                                      "patch_digest": DIGEST}}}

        def answer(zipped, **meta):
            def get(path, limit):
                if path.endswith("/zip"):
                    return zipped
                return json.dumps({"name": "porting-publication", "expired": False,
                                   "workflow_run": {"id": 4242}, **meta}).encode()
            return get

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            # Compressed, the series is small; inflated, it passes its bound.
            z.writestr("patches.mbox", b"0" * (held_patch.FILES["patches.mbox"] + 1))
            z.writestr("proposal.json", b"{}")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(held_patch.HeldPatchError, "larger than"):
                held_patch.fetch(task, "patch", Path(tmp), repository="o/r",
                                 get=answer(buffer.getvalue()), required=True)
            # An archive larger than its bound is not downloaded.
            with self.assertRaisesRegex(held_patch.HeldPatchError, "too large"):
                held_patch.fetch(task, "patch", Path(tmp), repository="o/r",
                                 get=answer(b"", size_in_bytes=held_patch.MAX_ZIP_BYTES + 1),
                                 required=True)
            # The download is refused as soon as it passes its bound.
            gh = Path(tmp) / "gh"
            gh.write_text("#!/bin/sh\nhead -c 3000000 /dev/zero\n")
            gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
            with unittest.mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ['PATH']}"}):
                self.assertEqual(len(held_patch.gh_get("/x", 3000000)), 3000000)
                with self.assertRaisesRegex(held_patch.HeldPatchError, "larger than"):
                    held_patch.gh_get("/x", 2000000)


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
        # The repair gets the rejected patch itself, by the artifact and the
        # digest the review pinned.
        evidence = {review["evidence_id"]: review}
        request = work.request_from_active(new)
        _paths, context = work.payload_inputs(request, new, evidence,
                                              plan_reference=lambda eid: {})
        self.assertEqual(context["previous_patch"], {
            "evidence_id": review["evidence_id"], "artifact_run_id": "4242", "artifact_id": 901,
            "patch_digest": DIGEST, "candidate": CANDIDATE, "expected_head": SHA_A,
            "operation": "append"})
        # Not once its artifact expired when the Work was requested.
        late = dict(new, active_work=dict(new["active_work"], requested_at=(
            datetime.fromisoformat(NOW) + timedelta(days=30)).isoformat()))
        _paths, context = work.payload_inputs(work.request_from_active(late), late, evidence,
                                              plan_reference=lambda eid: {})
        self.assertNotIn("previous_patch", context)
        # A patch Work without a rejected review names none.
        _paths, context = work.payload_inputs(work.request_from_active(running()), running(),
                                              {}, plan_reference=lambda eid: {})
        self.assertNotIn("previous_patch", context)

    def test_successive_failed_reviews_publish_the_patch_anyway(self):
        rec = running(feedback=["review-1"])
        # The owner's decision: the second failed review in succession
        # publishes the patch anyway.
        limit = lc.budget_limit("dev_reviews", POLICY)
        self.assertEqual(limit, 1)
        for attempt in range(2):
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
        # Once published, the comment of the review is due on the
        # development PR, posted by trusted code, durably.
        done = tick(rec, [published(rec, run=rec["held_patch"]["worker_run_id"])])
        last = final(done)
        self.assertEqual(engine.state_key(last), "development.waiting_ci")
        [due] = last["review_comments"]
        self.assertEqual((due["evidence_id"], due["head"]), (review["evidence_id"], CANDIDATE))
        self.assertEqual((due["repository"], due["pr_number"]),
                         (rec["fork"]["name"], rec["fork"]["pr_number"]))
        self.assertEqual(done.report["review_comment_requests"], [{
            "type": "post_review_comment", "contribution_id": rec["contribution_id"],
            "contribution_file": "example--proj--12", "evidence_id": review["evidence_id"],
            "attempt": 1}])
        self.assertEqual((due["attempts"], due["launched_at"]), (1, NOW))

    def test_a_due_comment_survives_later_publications_and_generations(self):
        # A comment still due for a patch published anyway is kept when the
        # next patch is published after a passing review, and a second
        # patch published anyway adds its own comment beside it.
        due = {"evidence_id": "patch-review-old", "head": CANDIDATE,
               "repository": "riseproject-upstreaming/proj", "pr_number": 3, "attempts": 1,
               "next_attempt_at": NOW, "launched_at": None, "last_reason": "403",
               "exhausted": False}
        reviewing, _ = held(running(review_comments=[due]))
        publishing = final(tick(reviewing, [review_result(reviewing)]))
        self.assertEqual([d["evidence_id"] for d in publishing["review_comments"]],
                         ["patch-review-old"])
        later = datetime.fromisoformat(NOW) + timedelta(minutes=5)
        done = final(tick(publishing, [published(publishing)], now=later))
        self.assertEqual(engine.state_key(done), "development.waiting_ci")
        self.assertEqual([d["evidence_id"] for d in done["review_comments"]],
                         ["patch-review-old"])
        anyway = copy.deepcopy(publishing)
        anyway["pending_publication"]["patch_review"].update(evidence_id="patch-review-new",
                                                             published_anyway=True)
        both = final(tick(anyway, [published(anyway)], now=later))
        self.assertEqual([d["evidence_id"] for d in both["review_comments"]],
                         ["patch-review-old", "patch-review-new"])
        self.assertEqual([d["attempts"] for d in both["review_comments"]], [2, 1])
        # A new generation keeps every due comment, pinned to the PR it is
        # posted on even once the record names no fork.
        unpinned = dict(due, repository=None, pr_number=None)
        restarted = engine.reset_generation(dict(both, review_comments=[unpinned]))
        self.assertIsNone(restarted["fork"])
        self.assertEqual(restarted["review_comments"],
                         [dict(due, repository=both["fork"]["name"],
                               pr_number=both["fork"]["pr_number"])])

    def test_the_comment_of_a_patch_published_anyway_is_retried_then_alerted(self):
        due = {"evidence_id": "patch-review-x", "head": CANDIDATE, "repository": None,
               "pr_number": None, "attempts": 0, "next_attempt_at": NOW, "launched_at": None,
               "last_reason": "", "exhausted": False}
        rec = contribution(activity="waiting_ci", review_comments=[due])
        at = datetime.fromisoformat(NOW)

        def run(record, receipts=(), minutes=0, **over):
            snap = reconcile.Snapshot(contributions={"c": record}, operations={}, consumed={},
                                      projects={}, evidence={})
            return reconcile.tick(None, snap, policy=dict(POLICY, **over),
                                  now=at + timedelta(minutes=minutes), publications=[],
                                  pressure=capacity.Pressure(ok=True), observe=False,
                                  review_comment_receipts=list(receipts))

        def receipt(attempt, status, **extra):
            return {"contribution_id": rec["contribution_id"], "evidence_id": "patch-review-x",
                    "attempt": attempt, "status": status, **extra}

        # Not launched while paused or read-only.
        for record, over in ((dict(rec, paused=True), {}), (rec, {"read_only": True})):
            out = run(record, **over)
            self.assertEqual(out.report.get("review_comment_requests") or [], [])
        launched = final(run(rec))
        self.assertEqual(launched["review_comments"][0]["attempts"], 1)
        # The launch pins the development PR it is posted on.
        self.assertEqual((launched["review_comments"][0]["repository"],
                          launched["review_comments"][0]["pr_number"]),
                         (rec["fork"]["name"], rec["fork"]["pr_number"]))
        # Waiting for its receipt: nothing is launched again.
        waiting = run(launched, minutes=10)
        self.assertEqual(waiting.report.get("review_comment_requests") or [], [])
        # A receipt of an older attempt is not this one's.
        self.assertEqual(run(launched, [receipt(0, "posted")], minutes=10)
                         .report["review_comment_posts"][0]["status"], "launched")
        # Posted, or found by its marker: the comment is done.
        for status in ("posted", "found"):
            done = final(run(launched, [receipt(1, status, comment_id=9)], minutes=10))
            self.assertEqual(done["review_comments"], [])
        # A failed or skipped post, or one without a receipt, is launched
        # again later, at most COMMENT_ATTEMPTS times.
        record, minutes = launched, 0
        for attempt in range(1, patch_review.COMMENT_ATTEMPTS + 1):
            minutes += 61 + patch_review.COMMENT_RETRY_MINUTES * attempt
            receipts = [receipt(attempt, "failed", reason="HTTP 502")] if attempt % 2 else []
            out = run(record, receipts, minutes=minutes)
            record = final(out)
            if attempt < patch_review.COMMENT_ATTEMPTS:
                self.assertEqual(record["review_comments"][0]["attempts"], attempt, out.report)
                again = run(record, minutes=minutes + patch_review.COMMENT_RETRY_MINUTES * attempt)
                record = final(again)
                self.assertEqual(again.report["review_comment_requests"][0]["attempt"],
                                 attempt + 1)
        self.assertTrue(record["review_comments"][0]["exhausted"])
        self.assertIn("could not be posted", out.report["controller"]["alerts"][0]["alert"])
        # Exhausted, it stays due: every tick alerts on it, and it is posted
        # again daily until it is posted or found.
        quiet = run(record, minutes=minutes + 600)
        self.assertEqual(quiet.report.get("review_comment_requests") or [], [])
        self.assertIn("could not be posted", quiet.report["controller"]["alerts"][0]["alert"])
        daily = run(record, minutes=minutes + 60 * patch_review.COMMENT_EXHAUSTED_RETRY_HOURS)
        self.assertEqual(daily.report["review_comment_requests"][0]["attempt"],
                         patch_review.COMMENT_ATTEMPTS + 1)
        done = final(run(final(daily), [receipt(patch_review.COMMENT_ATTEMPTS + 1, "found")],
                         minutes=minutes + 60 * patch_review.COMMENT_EXHAUSTED_RETRY_HOURS + 5))
        self.assertEqual(done["review_comments"], [])

    def test_each_due_comment_has_its_own_attempts_and_bound(self):
        at = datetime.fromisoformat(NOW)

        def due(n, **over):
            return {"evidence_id": f"patch-review-{n}", "head": CANDIDATE,
                    "repository": "riseproject-upstreaming/proj", "pr_number": 3,
                    "attempts": 0, "next_attempt_at": NOW, "launched_at": None,
                    "last_reason": "", "exhausted": False, **over}

        def run(record, receipts=(), minutes=0):
            snap = reconcile.Snapshot(contributions={"c": record}, operations={}, consumed={},
                                      projects={}, evidence={})
            return reconcile.tick(None, snap, policy=POLICY,
                                  now=at + timedelta(minutes=minutes), publications=[],
                                  pressure=capacity.Pressure(ok=True), observe=False,
                                  review_comment_receipts=list(receipts))

        rec = contribution(activity="waiting_ci", review_comments=[due(1), due(2)])
        launched = final(run(rec))
        self.assertEqual([d["attempts"] for d in launched["review_comments"]], [1, 1])
        receipts = [{"contribution_id": rec["contribution_id"], "evidence_id": "patch-review-1",
                     "attempt": 1, "status": "posted"},
                    {"contribution_id": rec["contribution_id"], "evidence_id": "patch-review-2",
                     "attempt": 1, "status": "failed", "reason": "HTTP 502"}]
        left = final(run(launched, receipts, minutes=5))
        [only] = left["review_comments"]
        self.assertEqual((only["evidence_id"], only["attempts"], only["last_reason"]),
                         ("patch-review-2", 1, "HTTP 502"))
        # Beyond the bound, the oldest exhausted comments are dropped with an
        # alert; the others stay.
        many = [due(n, exhausted=True, attempts=5, next_attempt_at="2099-01-01T00:00:00+00:00")
                for n in range(patch_review.MAX_DUE_COMMENTS)] + [due(99)]
        out = run(contribution(activity="waiting_ci", review_comments=many))
        kept = final(out)["review_comments"]
        self.assertEqual(len(kept), patch_review.MAX_DUE_COMMENTS)
        self.assertNotIn("patch-review-0", [d["evidence_id"] for d in kept])
        self.assertIn("patch-review-99", [d["evidence_id"] for d in kept])
        self.assertTrue(any("is no longer posted" in a["alert"]
                            for a in out.report["controller"]["alerts"]))

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

    def test_a_review_that_cannot_run_keeps_the_held_patch_for_a_resume(self):
        import commands
        from tests.helpers import apply_command

        reviewing, _ = held(running())
        for verb, facts in (("exhaust_dispatch_recovery", set()),
                            ("exhaust_work_recovery",
                             {"work_terminal_without_result"})):
            with self.subTest(verb=verb):
                rec = dict(reviewing, active_work=dict(reviewing["active_work"], attempt=9))
                out = engine.apply_transition(rec, verb, policy=POLICY, now=NOW, facts=facts,
                                              counts={"dispatch_recovery": 99})
                self.assertTrue(out.accepted, out.reason)
                waiting = out.record
                self.assertEqual(engine.state_key(waiting), "development.waiting_human")
                self.assertEqual(waiting["held_patch"], reviewing["held_patch"])
                self.assertEqual(waiting["pending_human_action"]["kind"],
                                 lc.PATCH_REVIEW_UNAVAILABLE)
                # The entry says, in plain words, that the review could not run.
                entry = status_ledger.render(out.event, waiting, "https://run")
                self.assertIn("the review of the development patch could not run", entry)
                self.assertNotIn("patch_review_unavailable", entry.split("<details>")[0])
                # A resume asks for the review of the same patch again.
                resume = commands.Command("resume", {"revision": str(waiting["revision"]),
                                                     "reason": "infra fixed"})
                resumed = apply_command(waiting, resume, policy_=POLICY)
                self.assertTrue(resumed.accepted, resumed.reason)
                self.assertEqual(resumed.verb, "resume_patch_review")
                self.assertEqual(engine.state_key(resumed.record), "development.review_pending")
                self.assertEqual(resumed.record["held_patch"], reviewing["held_patch"])
                res = controller.tick(contributions={"c": resumed.record}, operations={},
                                      consumed={}, projects={}, policy=POLICY,
                                      inputs=controller.Inputs(
                                          now=datetime.fromisoformat(NOW),
                                          pressure=capacity.Pressure(ok=True)))
                again = [r for k, r in res.staged if k == "contributions"][-1]
                self.assertEqual(engine.state_key(again), "development.reviewing")
                self.assertEqual(again["active_work"]["task"], "development_review")
                # An expired artifact is discarded instead: fresh development
                # Work is due.
                res = controller.tick(contributions={"c": resumed.record}, operations={},
                                      consumed={}, projects={}, policy=POLICY,
                                      inputs=controller.Inputs(
                                          now=datetime.fromisoformat(NOW) + timedelta(days=30),
                                          pressure=capacity.Pressure(ok=True)))
                self.assertEqual([e["kind"] for k, e in res.staged if k == "events"][0],
                                 "expire_held_patch")
                # A stop during the wait discards the patch; the resume then
                # leads to fresh development Work too.
                paused = engine.apply_transition(
                    waiting, "pause", policy=POLICY, now=NOW,
                    command=commands.Command("pause", {"revision": str(waiting["revision"])}),
                    open_operations=()).record
                self.assertIsNone(paused["held_patch"])
                res = controller.tick(contributions={"c": dict(
                    resumed.record, held_patch=None)}, operations={}, consumed={}, projects={},
                    policy=POLICY, inputs=controller.Inputs(
                        now=datetime.fromisoformat(NOW), pressure=capacity.Pressure(ok=True)))
                self.assertEqual([e["kind"] for k, e in res.staged if k == "events"][0],
                                 "expire_held_patch")

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
        # A patch to be published anyway that is replanned or rejected
        # starts `dev_reviews` again; a passing one keeps the count.
        anyway = copy.deepcopy(publishing)
        anyway["pending_publication"]["patch_review"]["published_anyway"] = True
        anyway["budget"]["review_rejections"] = 1
        for pub in (stale, off):
            with self.subTest(status=pub["failure_class"]):
                self.assertEqual(final(tick(anyway, [pub]))["budget"]["review_rejections"], 0)

    def test_only_successive_failed_reviews_publish_a_patch_anyway(self):
        # A failed review, then a passing one whose publication is
        # rejected: the next failed review is the first in succession, so
        # the patch is returned for repair, not published anyway.
        t0 = datetime.fromisoformat(NOW)
        ev = {}

        def step(rec, pubs, minutes):
            out = tick(rec, pubs, now=t0 + timedelta(minutes=minutes), evidence=ev)
            ev.update({r["evidence_id"]: r for k, r in out.staged if k == "evidence"})
            return final(out), out

        reviewing, _ = held(running())
        first, _ = step(reviewing, [review_result(reviewing, severity="major")], 1)
        self.assertEqual(first["budget"]["review_rejections"], 1)
        again, _ = step(first, [verified(first, run="4300", artifact=910)], 5)
        publishing, _ = step(again, [review_result(again, run="5001", artifact=911,
                                                   evidence=ev)], 10)
        self.assertEqual(engine.state_key(publishing), lc.PUBLISHING)
        self.assertEqual(publishing["budget"]["review_rejections"], 0)
        off = dict(published(publishing, run="4300", artifact=912), status="rejected",
                   failure_class="validation", reason="the series rebuilds to X",
                   operation=None)
        rejected, out = step(publishing, [off], 12)
        self.assertEqual(kinds(out)[0], "reject_patch_proposal")
        third, _ = step(rejected, [verified(rejected, run="4400", artifact=920)], 20)
        fourth, out = step(third, [review_result(third, severity="major", run="5002",
                                                 artifact=921, evidence=ev)], 25)
        self.assertEqual(kinds(out)[0], "reject_patch_review")
        self.assertEqual([(e["data"]["disposition"], e["data"]["failed_in_succession"])
                          for e in staged(out, "patch_review")], [("return_for_repair", 1)])

    def test_every_exit_that_discards_a_patch_published_anyway_restarts_dev_reviews(self):
        reviewing, _ = held(running())
        anyway = final(tick(reviewing, [review_result(reviewing)]))
        anyway["pending_publication"]["patch_review"]["published_anyway"] = True
        anyway["budget"]["review_rejections"] = 1
        import commands
        pause = commands.Command("pause", {"revision": str(anyway["revision"])})
        paused = engine.apply_transition(anyway, "pause", policy=POLICY, now=NOW, command=pause,
                                         open_operations=())
        self.assertTrue(paused.accepted, paused.reason)
        self.assertEqual(paused.record["budget"]["review_rejections"], 0)
        # Its recovery exhausted, the wait keeps the patch; the resume that
        # discards it restarts the count.
        waiting = engine.apply_transition(
            dict(anyway, pending_publication=dict(anyway["pending_publication"], attempts=99)),
            "exhaust_publish_recovery", policy=POLICY, now=NOW, cause_id="x").record
        self.assertEqual(waiting["budget"]["review_rejections"], 1)
        resume = commands.Command("resume", {"revision": str(waiting["revision"])})
        resumed = engine.apply_transition(waiting, "resume_development", policy=POLICY, now=NOW,
                                          command=resume, open_operations=())
        self.assertTrue(resumed.accepted, resumed.reason)
        self.assertEqual(resumed.record["budget"]["review_rejections"], 0)
        # A patch that passed its review never counts.
        passed = engine.apply_transition(
            dict(reviewing, budget=dict(reviewing["budget"], review_rejections=1)),
            "accept_patch_review", policy=POLICY, now=NOW, cause_id="x",
            facts={"patch_review_passed"},
            values={"pending_publication": anyway["pending_publication"]})
        self.assertTrue(passed.accepted, passed.reason)
        self.assertEqual(passed.record["budget"]["review_rejections"], 0)

    def test_a_late_verification_is_not_the_rejection_of_the_publication(self):
        reviewing, _ = held(running())
        publishing = final(tick(reviewing, [review_result(reviewing)]))
        # A verification of the same Work, collected late (the worker's own
        # run, or a retry of it made before the review), that was rejected.
        for stage in (None, "other"):
            late = dict(published(publishing), status="rejected", failure_class="stale",
                        reason="fork head moved", operation=None, publication_stage=stage)
            with self.subTest(stage=stage):
                self.assertIn("did not publish",
                              reconcile.result_mismatch(publishing, late))
                out = tick(publishing, [late])
                self.assertEqual(kinds(out), ["record_late_effect"])
                self.assertEqual(engine.state_key(final(out)), lc.PUBLISHING)
        self.assertIsNone(reconcile.result_mismatch(publishing, dict(
            published(publishing), status="rejected", failure_class="stale", operation=None)))

    def test_the_fence_covers_every_run_the_controller_collects(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        controller_text = (root / ".github/workflows/controller.yml").read_text()
        self.assertIn("runs?per_page=20$filter", controller_text)
        workflows, runs = 4, 20
        self.assertGreaterEqual(reconcile.PROCESSED_PUBLICATIONS, 2 * workflows * runs)
        schema = json.loads((root / "schemas/contribution.schema.json").read_text())
        self.assertEqual(schema["properties"]["processed_publications"]["maxItems"],
                         reconcile.PROCESSED_PUBLICATIONS)

    def test_an_expired_artifact_is_never_published(self):
        reviewing, _ = held(running())
        publishing = final(tick(reviewing, [review_result(reviewing)]))
        publishing["pending_publication"]["patch_review"]["published_anyway"] = True
        publishing["budget"]["review_rejections"] = 1
        expires = datetime.fromisoformat(publishing["held_patch"]["expires_at"])
        out = tick(publishing, now=expires - timedelta(hours=2))
        self.assertEqual(kinds(out)[0], "expire_held_patch")
        new = final(out)
        self.assertIsNone(new["held_patch"])
        self.assertIsNone(new["pending_publication"])
        self.assertEqual(new["budget"]["review_rejections"], 0)
        self.assertEqual(out.report["publication_retries"], [])
        # Within its retention, the publication is launched.
        publishing["pending_publication"].pop("launched_at", None)
        current = tick(publishing, now=expires - timedelta(days=2))
        self.assertEqual(len(current.report["publication_retries"]), 1)

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
        # In development.publishing the pin applies whatever the payload
        # says: a write without the review contract is pinned too.
        unreviewed = dict(args, reviewed=False)
        self.assertIsNone(lc.publication_check(publishing, **unreviewed))
        self.assertIn("Work id", lc.publication_check(publishing, **dict(unreviewed,
                                                                        work_key="other")))
        self.assertIn("no held patch", lc.publication_check(
            dict(publishing, held_patch=None), **unreviewed))

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
        # The same series of a payload without the review contract is
        # refused in development.publishing too.
        unreviewed = {k: v for k, v in task.items() if k != "review"}
        out = fork_publish.publish(record=publishing, task=unreviewed, proposal=proposal,
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
                (running(), review_result(reviewing, artifact=992)),
                # The verification of the held worker run, collected again
                # in a later artifact, and a `no_change` report of it: no
                # publication, never folded, never a pause.
                (publishing, verified(running(), artifact=993)),
                (publishing, dict(verified(running(), artifact=994), status="no_change"))):
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


class TestCommentOfALatePublication(unittest.TestCase):
    """A patch published anyway whose `published` Result is folded late (the
    record stopped, paused, or exhausted the publication first) is on the
    fork all the same: the comment of its review becomes due in the write
    that records the late Result, end to end through ticks that carry the
    evidence store as the committed state does."""

    def setUp(self):
        self.at = datetime.fromisoformat(NOW)
        self.evidence: dict = {}

    def run_tick(self, rec, pubs=(), *, minutes=0, comments=(), receipts=()):
        from tests.helpers import OWNER

        snap = reconcile.Snapshot(contributions={"c": rec}, operations={}, consumed={},
                                  projects={}, evidence=dict(self.evidence))
        out = reconcile.tick(
            None, snap, policy=POLICY, now=self.at + timedelta(minutes=minutes),
            publications=list(pubs), pressure=capacity.Pressure(ok=True), observe=False,
            review_comment_receipts=list(receipts),
            comments=[{"issue_number": 12, "comment": {"id": 900 + i + minutes, "body": body,
                                                        "user": dict(OWNER)}}
                      for i, body in enumerate(comments)])
        self.evidence.update({r["evidence_id"]: r for k, r in out.staged if k == "evidence"})
        records = [r for k, r in out.staged if k == "contributions"]
        return (records[-1] if records else rec), out

    def publishing_anyway(self):
        rec = running(feedback=["review-1"])
        for attempt in range(2):
            reviewing, _ = self.run_tick(rec, [verified(rec, run=str(4242 + attempt),
                                                         artifact=901 + attempt)])
            rec, _ = self.run_tick(reviewing, [review_result(
                reviewing, severity="major", run=str(5000 + attempt), artifact=950 + attempt)])
        self.assertEqual(engine.state_key(rec), "development.publishing")
        [review] = [e for e in self.evidence.values()
                    if e["data"].get("disposition") == "publish_anyway"]
        return rec, review["evidence_id"]

    def result(self, rec, **over):
        pub = published(rec, run=rec["held_patch"]["worker_run_id"])
        return dict(pub, **over)

    def assert_due_and_held_by_the_pause(self, rec, eid, minutes):
        [due] = rec["review_comments"]
        self.assertEqual((due["evidence_id"], due["head"], due["attempts"], due["launched_at"]),
                         (eid, CANDIDATE, 0, None))
        # Held by the pause: no attempt is spent, and once it has been held
        # for the bound every tick alerts on it.
        held_, out = self.run_tick(rec, minutes=minutes + 5)
        self.assertEqual(out.report["review_comment_requests"], [])
        self.assertEqual(out.report["review_comment_posts"][0]["status"], "held")
        self.assertFalse(out.report["controller"]["alerts"])
        later = minutes + 60 * patch_review.COMMENT_HELD_ALERT_HOURS
        _, out = self.run_tick(held_, minutes=later)
        self.assertEqual(out.report["review_comment_requests"], [])
        self.assertIn("the contribution is paused",
                      out.report["controller"]["alerts"][0]["alert"])
        # The resume launches it, at its first attempt.
        resumed, out = self.run_tick(held_, minutes=later + 5, comments=[
            f'/upstreaming resume revision={held_["revision"]} reason="checked"'])
        self.assertFalse(resumed["paused"])
        self.assertEqual([(r["evidence_id"], r["attempt"])
                          for r in out.report["review_comment_requests"]], [(eid, 1)])

    def test_a_pause_folded_before_the_published_result(self):
        # Path X: the pause discards the held patch and its pending
        # publication before the Result of the push is collected.
        rec, eid = self.publishing_anyway()
        paused, out = self.run_tick(rec, minutes=3, comments=['/upstreaming pause reason="x"'])
        self.assertEqual((kinds(out), paused["held_patch"], paused["pending_publication"]),
                         (["pause"], None, None))
        late, out = self.run_tick(paused, [self.result(rec)], minutes=6)
        self.assertEqual(kinds(out), ["record_late_effect"])
        [event] = [e for k, e in out.staged if k == "events"]
        self.assertEqual(event["details"]["review_comments_due"], [eid])
        self.assert_due_and_held_by_the_pause(late, eid, 6)
        # The same Result collected again in another artifact adds nothing.
        again, _ = self.run_tick(late, [self.result(rec, publication_artifact_id=904)],
                                 minutes=7)
        self.assertEqual([d["evidence_id"] for d in again["review_comments"]], [eid])

    def test_a_published_result_whose_report_cannot_be_stored(self):
        # Path Y: the publish-only retry published, but its Work carries a
        # history and its artifact no storable report: the fold pauses the
        # record, discarding everything it staged; the next tick records
        # the Result late, and the comment is due all the same.
        rec, eid = self.publishing_anyway()
        pub = self.result(rec)
        pub["task"]["context"]["history"] = {"sha256": HISTORY}
        paused, out = self.run_tick(rec, [pub], minutes=3)
        self.assertEqual(kinds(out), ["pause_on_processing_error"])
        self.assertEqual(paused["review_comments"], [])
        late, out = self.run_tick(paused, [pub], minutes=6)
        self.assertEqual(kinds(out), ["record_late_effect"])
        self.assert_due_and_held_by_the_pause(late, eid, 6)

    def test_a_retry_lost_before_the_publication_was_exhausted(self):
        # The publish-only retry's Result is lost until `publish_recovery`
        # is exhausted; its late `published` Result makes the comment due
        # in the human wait, and it is posted.
        rec, eid = self.publishing_anyway()
        minutes = 0
        while engine.state_key(rec) == "development.publishing":
            minutes += 61
            rec, _ = self.run_tick(rec, minutes=minutes)
            self.assertLess(minutes, 61 * 40)
        self.assertEqual(rec["pending_human_action"]["kind"], "publication_infrastructure_failure")
        late, out = self.run_tick(rec, [self.result(rec, publication_artifact_id=990)],
                                  minutes=minutes + 5)
        self.assertEqual(kinds(out), ["record_late_effect"])
        self.assertEqual([(r["evidence_id"], r["attempt"])
                          for r in out.report["review_comment_requests"]], [(eid, 1)])
        self.assertEqual(late["review_comments"][0]["head"], CANDIDATE)

    def test_only_the_publication_of_the_reviewed_patch_makes_it_due(self):
        rec, eid = self.publishing_anyway()
        paused, _ = self.run_tick(rec, minutes=3, comments=['/upstreaming pause reason="x"'])
        for name, pub in (
                ("another worker run", self.result(rec, worker_run_id="9999")),
                ("another commit", self.result(rec, candidate="f" * 40, operation=None)),
                ("not published", self.result(rec, status="uncertain"))):
            with self.subTest(name):
                late, out = self.run_tick(paused, [pub], minutes=6)
                self.assertEqual(kinds(out), ["record_late_effect"])
                self.assertEqual(late["review_comments"], [], name)


class TestCommentHolds(unittest.TestCase):
    def run_tick(self, rec, minutes=0, receipts=()):
        snap = reconcile.Snapshot(contributions={"c": rec}, operations={}, consumed={},
                                  projects={}, evidence={})
        out = reconcile.tick(None, snap, policy=POLICY,
                             now=datetime.fromisoformat(NOW) + timedelta(minutes=minutes),
                             publications=[], pressure=capacity.Pressure(ok=True),
                             observe=False, review_comment_receipts=list(receipts))
        records = [r for k, r in out.staged if k == "contributions"]
        return (records[-1] if records else rec), out

    @staticmethod
    def due(**over):
        return {"evidence_id": "patch-review-x", "head": CANDIDATE, "repository": None,
                "pr_number": None, "attempts": 0, "next_attempt_at": NOW, "launched_at": None,
                "last_reason": "", "exhausted": False, **over}

    def test_the_first_patch_published_anyway_waits_for_its_development_pr(self):
        rec = running(feedback=["review-1"])
        rec["fork"] = dict(rec["fork"], pr_number=None)
        for attempt in range(2):
            reviewing, _ = held(rec, run=str(4242 + attempt), artifact=901 + attempt)
            rec = final(tick(reviewing, [review_result(
                reviewing, severity="major", run=str(5000 + attempt), artifact=950 + attempt)]))
        out = tick(rec, [published(rec, run=rec["held_patch"]["worker_run_id"])])
        done = final(out)
        self.assertEqual(engine.state_key(done), "development.waiting_ci")
        [due] = done["review_comments"]
        self.assertEqual((due["repository"], due["pr_number"], due["attempts"]),
                         (None, None, 0))
        self.assertEqual(out.report["review_comment_requests"], [])
        # Held without spending an attempt, alerted once held for the bound.
        hours = patch_review.COMMENT_HELD_ALERT_HOURS
        _, out = self.run_tick(done, minutes=60 * hours - 1)
        self.assertEqual((out.report["review_comment_requests"],
                          out.report["controller"]["alerts"]), ([], []))
        _, out = self.run_tick(done, minutes=60 * hours)
        self.assertIn("no development PR is recorded",
                      out.report["controller"]["alerts"][0]["alert"])
        # Once the development PR operation confirms the PR, attempt 1 is
        # launched on it.
        with_pr = dict(done, fork=dict(done["fork"], pr_number=7))
        launched, out = self.run_tick(with_pr, minutes=60 * hours + 5)
        self.assertEqual([r["attempt"] for r in out.report["review_comment_requests"]], [1])
        self.assertEqual((launched["review_comments"][0]["repository"],
                          launched["review_comments"][0]["pr_number"]),
                         (done["fork"]["name"], 7))

    def test_a_terminal_record_posts_whatever_its_pause(self):
        closed = contribution(phase="closed", activity="idle", paused=True,
                              terminal_reason="abandoned", review_comments=[self.due()])
        _, out = self.run_tick(closed)
        self.assertEqual([r["attempt"] for r in out.report["review_comment_requests"]], [1])
        record = json.loads(json.dumps(closed))
        record["review_comments"][0]["attempts"] = 1
        self.assertIsNone(patch_review.comment_check(record, "patch-review-x", 1, POLICY))
        # Without a development PR nothing will post it: alerted at once.
        no_pr = dict(closed, fork=dict(closed["fork"], pr_number=None))
        _, out = self.run_tick(no_pr)
        self.assertEqual(out.report["review_comment_requests"], [])
        self.assertIn("nothing else will post it", out.report["controller"]["alerts"][0]["alert"])
        # A paused record that is not terminal is held, by the check too.
        paused = contribution(activity="waiting_ci", paused=True,
                              review_comments=[self.due(attempts=1)])
        self.assertEqual(patch_review.comment_check(paused, "patch-review-x", 1, POLICY),
                         "the contribution is paused")

    def test_the_attempts_end_where_the_workflow_input_ends(self):
        import re
        from pathlib import Path

        text = Path(".github/workflows/review-comment.yml").read_text()
        pattern = re.search(r'\[\[ "\$ATTEMPT" =~ (\S+) \]\]', text).group(1)
        self.assertTrue(re.fullmatch(pattern, str(patch_review.COMMENT_LAST_ATTEMPT)))
        self.assertFalse(re.fullmatch(pattern, str(patch_review.COMMENT_LAST_ATTEMPT + 1)))
        schema = json.loads(Path("schemas/contribution.schema.json").read_text())
        attempts = schema["properties"]["review_comments"]["items"]["properties"]["attempts"]
        self.assertEqual(attempts["maximum"], patch_review.COMMENT_LAST_ATTEMPT)
        last = patch_review.COMMENT_LAST_ATTEMPT
        rec = contribution(activity="waiting_ci", review_comments=[self.due(
            repository="riseproject-upstreaming/proj", pr_number=3, attempts=last,
            exhausted=True, last_reason="HTTP 404")])
        _, out = self.run_tick(rec, minutes=60 * 24 * 3)
        self.assertEqual(out.report["review_comment_requests"], [])
        self.assertIn("no longer posted", out.report["controller"]["alerts"][0]["alert"])
        # One attempt short of it, the daily post is still launched.
        rec["review_comments"][0]["attempts"] = last - 1
        _, out = self.run_tick(rec, minutes=60 * 24 * 3)
        self.assertEqual([r["attempt"] for r in out.report["review_comment_requests"]], [last])
        self.assertEqual(patch_review.comment_check(
            dict(rec, review_comments=[dict(rec["review_comments"][0], attempts=last + 1)]),
            "patch-review-x", last + 1, POLICY),
            "the comment is not a due comment of the record at this attempt")


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
                self.assertEqual(event["details"]["added"], ["held_patch", "review_comments"])
                self.assertEqual({k: v for k, v in new.items()
                                  if k not in ("held_patch", "review_comments", "updated_at")},
                                 {k: v for k, v in rec.items() if k != "updated_at"})
                work_ = new.get("active_work")
                if work_ and work_.get("task") == "development_patch":
                    # Work dispatched on the old worker lock carries no
                    # review contract: its Result still publishes.
                    pub = {"status": "published", "task": {"task": "development_patch"}}
                    self.assertFalse(fork_publish.review_required(pub["task"]))
                    edge = lc.find_edge("development.agent_running", "record_publish_success")
                    self.assertIn(lc.NO_REVIEW_CONTRACT, edge.transition.guard)



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
        for check in ("python3 scripts/held_patch.py --task \"$task\" --key patch",
                      "--out /tmp/gh-aw/agent/patch --required",
                      '"the held series does not rebuild to the verified tree"',
                      "head=$(jq -r '.context.patch.expected_head' \"$task\")",
                      "fetch_evidence .context.report worker_report", "fetch_evidence .context.history work_history"):
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

    def test_the_worker_reads_the_rejected_patch_it_amends(self):
        import io
        import tempfile
        import zipfile
        from pathlib import Path

        from scripts import held_patch

        source = self.front("worker.md")
        steps = "\n".join(step.get("run", "") for step in source["pre-agent-steps"])
        self.assertIn("python3 scripts/held_patch.py --task \"$task\" --key previous_patch", steps)
        self.assertNotIn("--key previous_patch \\\n          --out /tmp/gh-aw/agent/previous-patch "
                         "--required", steps)
        self.assertIn("git checkout -q -B previous-patch", steps)
        # An expired artifact only means there is no rejected patch to read.
        task = {"context": {"previous_patch": {"artifact_run_id": "4242", "artifact_id": 901,
                                               "patch_digest": DIGEST}}}

        def get(path, limit):
            if path.endswith("/zip"):
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w") as z:
                    z.writestr("patches.mbox", b"x")
                    z.writestr("proposal.json", b"{}")
                return buffer.getvalue()
            return json.dumps({"name": "porting-publication", "expired": True,
                               "workflow_run": {"id": 4242}}).encode()

        with tempfile.TemporaryDirectory() as tmp:
            status = held_patch.fetch(task, "previous_patch", Path(tmp), repository="o/r",
                                      get=get)
            self.assertEqual(status["status"], "absent")
            with self.assertRaises(held_patch.Gone):
                held_patch.fetch(task, "previous_patch", Path(tmp), repository="o/r", get=get,
                                 required=True)
            self.assertEqual(held_patch.fetch({"context": {}}, "previous_patch", Path(tmp),
                                              repository="o/r", get=get)["status"], "absent")

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

    def test_the_publish_only_retry_verifies_and_publishes(self):
        retry = self.yaml.safe_load((self.root / "publish-retry.yml").read_text())
        steps = retry["jobs"]["publish"]["steps"]
        result = next(step for step in steps if step.get("id") == "result")
        self.assertIn("python -m fork_publish verify", result["run"])
        upload = next(step for step in steps
                      if str(step.get("uses", "")).startswith("actions/upload-artifact"))
        # The series a verification retry verified, which a held patch pins.
        self.assertIn("in/patches.mbox", upload["with"]["path"].split())
        # It posts no comment: review-comment.yml does, durably.
        self.assertNotIn("patch_review comment", (self.root / "publish-retry.yml").read_text())

    def test_the_comment_workflow_posts_with_a_pull_requests_token_only(self):
        source = self.yaml.safe_load((self.root / "review-comment.yml").read_text())
        self.assertEqual(source["permissions"], {"contents": "read"})
        steps = source["jobs"]["comment"]["steps"]
        due = next(step for step in steps if step.get("id") == "due")
        self.assertIn("python -m patch_review comment-due", due["run"])
        self.assertNotIn("TOKEN", str(due.get("env")))
        token = next(step for step in steps if step.get("id") == "comment-token")
        self.assertEqual({k: v for k, v in token["with"].items() if k.startswith("permission-")},
                         {"permission-pull-requests": "write"})
        self.assertEqual(token["if"], "steps.due.outputs.due == 'true'")
        post = next(step for step in steps if "patch_review comment " in step.get("run", ""))
        self.assertEqual(post["env"]["UPSTREAMING_FORK_TOKEN"],
                         "${{ steps.comment-token.outputs.token }}")
        upload = next(step for step in steps
                      if str(step.get("uses", "")).startswith("actions/upload-artifact"))
        self.assertEqual((upload["with"]["name"], upload["if"]),
                         ("porting-review-comment", "always()"))
        # A pending post is replaced only by a later attempt of its own
        # comment.
        self.assertEqual(source["concurrency"],
                         {"group": "porting-review-comment-${{ inputs.evidence-id }}",
                          "cancel-in-progress": False})
        controller_text = (self.root / "controller.yml").read_text()
        self.assertIn("--review-comments review-comments", controller_text)
        self.assertIn("gh workflow run review-comment.yml", controller_text)

    def test_the_controller_collects_and_follows_the_reviewer(self):
        text = (self.root / "controller.yml").read_text()
        self.assertIn("reviewer.lock.yml", text)
        dispatcher = self.front("dispatcher.md")
        self.assertEqual(dispatcher["safe-outputs"]["dispatch-workflow"]["workflows"],
                         ["planner", "worker", "reviewer"])
        self.assertEqual(work.PROFILE_WORKFLOWS["reviewer"], ".github/workflows/reviewer.lock.yml")


if __name__ == "__main__":
    unittest.main()
