# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Upstream review and release on the table's rows.

``upstream_review.waiting_ci`` is observed by its rows (merge, closure,
head change, explanation, actionable review, CI failure once per head and
run); the Result of revision Work is folded by the rows of
``upstream_review.agent_running``; revision Work is allocated by the rows of
``upstream_review.fix_needed``; nothing parks from upstream review without a
command; ``release_wait.idle`` is observed by its rows.
"""

from __future__ import annotations

import contextlib
import copy
import io
import unittest
from datetime import datetime, timedelta
from unittest import mock

import capacity
import collect
import commands
import engine
import lifecycle
import reconcile
import release
import review_response
import schemas
import status_ledger
from tests.helpers import NOW, OWNER, SHA_A, SHA_B, SHA_C, apply_command, contribution, policy
from tests.test_late_results import PENDING, WORK, ci_snapshot, publication

T = datetime.fromisoformat("2026-11-23T09:00:00+00:00")
POLICY = policy()
UPSTREAM_PR = {"id": 9, "number": 9, "url": "https://github.com/example/proj/pull/9",
               "head_repository_id": 2, "head_ref": "riscv64", "head_sha": SHA_A,
               "base_ref": "main", "registered_by": OWNER}
REVIEW = {"review_ids": [77], "comment_ids": [], "conversation_comment_ids": [],
          "reviews": [{"id": 77, "body": "Please keep the aarch64 row."}], "comments": [],
          "conversation_comments": [], "inquiry_comment_ids": [],
          "inquiry_conversation_comment_ids": [], "inquiry_comments": [],
          "inquiry_conversation_comments": []}
INQUIRY = dict(REVIEW, review_ids=[], reviews=[], inquiry_comment_ids=[88],
               inquiry_comments=[{"id": 88, "body": "Why riscv64 @maintainer?",
                                  "url": "https://github.com/example/proj/pull/9#c88"}])
MERGE = {"merge_sha": "e" * 40, "merged_at": "2026-11-20T09:00:00+00:00", "target_ref": "main",
         "evidence_id": "merge-1"}
REVISION_WORK = dict(WORK, task="upstream_revision", dedupe_key="example--proj/12@0:upstream_revision")


def record(activity="waiting_ci", **fields):
    rec = contribution(phase="upstream_review", activity=activity, dossier_evidence_id="dossier-1",
                       scope_paths=["a"], scope_coverage_complete=True,
                       upstream_pr=copy.deepcopy(UPSTREAM_PR), **fields)
    schemas.validate("contribution", rec)
    return rec


def released_wait(**fields):
    rec = contribution(phase="release_wait", activity="idle", upstream_pr=copy.deepcopy(UPSTREAM_PR),
                       merge=dict(MERGE), target_contract_id=fields.pop("contract", None), **fields)
    schemas.validate("contribution", rec)
    return rec


def pull(head=SHA_A, state="open", merged=False):
    return {"id": 9, "number": 9, "state": state, "merged": merged,
            "merged_at": "2026-11-23T08:00:00Z" if merged else None,
            "merge_commit_sha": "e" * 40 if merged else None, "base_ref": "main",
            "base_repository_id": 1, "head_ref": "riscv64", "head_sha": head,
            "head_repository_id": 2}


def tick(rec, publications=(), *, verdict="pending", pr=None, branch=SHA_A, feedback=None,
         now=T, evidence=None, operations=None, contracts=(), pypi=None, contains=None,
         audited=None, comments=(), capacity_ok=False):
    evidence = dict(evidence or {})
    evidence.update({c["evidence_id"]: c for c in contracts})
    snapshot = reconcile.Snapshot({"c": copy.deepcopy(rec)}, copy.deepcopy(operations or {}), {},
                                  {}, evidence, audited or {})
    reads = []

    def upstream_pull(_api, _record):
        reads.append("pull")
        return copy.deepcopy(pr or pull())

    head = {"head": branch, "tree": "t" * 40, "parents": [SHA_C]} if branch else None
    with mock.patch("observe_ci.collect.branch_head", return_value=head), \
            mock.patch("observe_ci.collect.upstream_pull", side_effect=upstream_pull), \
            mock.patch("observe_ci.collect.upstream_review_feedback",
                       return_value=copy.deepcopy(feedback)), \
            mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
            mock.patch("observe_ci.collect.ci_snapshot",
                       return_value=ci_snapshot((pr or pull())["head_sha"], verdict)), \
            mock.patch("observe_ci.collect.pypi_project",
                       side_effect=lambda name: copy.deepcopy((pypi or {}).get(name))), \
            mock.patch("observe_ci.collect.release_contains",
                       side_effect=lambda _api, _repo, version, _sha: (contains or {}).get(version)), \
            contextlib.redirect_stderr(io.StringIO()):
        out = reconcile.tick(object(), snapshot, policy=POLICY, now=now,
                             publications=list(publications), comments=list(comments),
                             pressure=capacity.Pressure(ok=capacity_ok, reason="test"),
                             observe=True,
                             dispatcher_runs=[], queue_works={})
    out.reads = reads
    return out


def final(output, rec):
    staged = [r for kind, r in output.staged if kind == "contributions"]
    return staged[-1] if staged else rec


def kinds(output):
    return [e["kind"] for kind, e in output.staged if kind == "events"]


def staged(output, kind):
    return [r for k, r in output.staged if k == kind]


class TestObservation(unittest.TestCase):
    """The observation matrix of upstream review, per state."""

    def test_waiting_ci_takes_one_row_per_observation(self):
        cases = {
            "merged": (dict(pr=pull(merged=True, state="closed")), ["record_merge"],
                       "release_wait.idle"),
            "closed": (dict(pr=pull(state="closed")), ["record_pr_closed_unmerged"],
                       "upstream_review.waiting_human"),
            "head moved": (dict(pr=pull(head=SHA_B), branch=SHA_B), ["record_head_change"],
                           "upstream_review.waiting_human"),
            "review": (dict(feedback=REVIEW, verdict="failed"), ["record_actionable_review"],
                       "upstream_review.fix_needed"),
            "explanation": (dict(feedback=INQUIRY, verdict="passed"), ["record_explanation"],
                            "upstream_review.waiting_ci"),
            "ci failed": (dict(verdict="failed"), ["record_upstream_ci_failure"],
                          "upstream_review.fix_needed"),
            "ci passed": (dict(verdict="passed"), [], "upstream_review.waiting_ci"),
            "ci pending": (dict(verdict="pending"), [], "upstream_review.waiting_ci"),
        }
        for name, (kwargs, verbs, state) in cases.items():
            with self.subTest(case=name):
                rec = record(last_ci_evidence_id=None)
                out = tick(rec, **kwargs)
                self.assertEqual(out.failures, [])
                self.assertEqual(kinds(out), verbs)
                after = final(out, rec)
                self.assertEqual(engine.state_key(after), state)
                self.assertFalse(after["paused"])

    def test_the_rows_record_what_the_table_says(self):
        rec = record()
        merged = final(tick(rec, pr=pull(merged=True, state="closed")), rec)
        self.assertEqual(merged["merge"]["merge_sha"], "e" * 40)
        self.assertEqual(merged["merge"]["target_ref"], "main")
        out = tick(rec, pr=pull(merged=True, state="closed"))
        [ev] = staged(out, "evidence")
        self.assertEqual((ev["kind"], merged["merge"]["evidence_id"]), ("merge", ev["evidence_id"]))

        closed = final(tick(rec, pr=pull(state="closed")), rec)
        self.assertEqual(closed["pending_human_action"]["kind"], "pr_closed_unmerged")
        self.assertEqual(closed["revision"], rec["revision"])

        moved = final(tick(dict(rec, last_ci_evidence_id="ci-old"), pr=pull(head=SHA_B),
                           branch=SHA_B), rec)
        self.assertEqual((moved["upstream_pr"]["head_sha"], moved["revision"],
                          moved["last_ci_evidence_id"], moved["pending_human_action"]["kind"]),
                         (SHA_B, rec["revision"] + 1, None, "upstream_head_changed"))
        self.assertEqual(moved["fork"], rec["fork"])

        reviewed = final(tick(rec, feedback=REVIEW), rec)
        self.assertEqual(reviewed["revision"], rec["revision"] + 1)
        self.assertEqual(len(reviewed["pending_feedback_evidence_ids"]), 1)
        self.assertTrue(reviewed["pending_feedback_evidence_ids"][0].startswith("review-1-9-"))
        self.assertEqual(reviewed["processed_review_ids"], [77])

        explained = final(tick(rec, feedback=INQUIRY, verdict="passed"), rec)
        self.assertEqual((explained["revision"], explained["processed_review_comment_ids"]),
                         (rec["revision"], [88]))

        failed = final(tick(rec, verdict="failed"), rec)
        self.assertEqual((failed["last_ci_evidence_id"], failed["revision"]),
                         ("ci-2-777-1", rec["revision"]))

    def test_a_failure_is_recorded_once_per_head_and_run(self):
        rec = record(last_ci_evidence_id="ci-2-777-1")
        out = tick(rec, verdict="failed")
        self.assertEqual(kinds(out), [])
        self.assertEqual(engine.state_key(final(out, rec)), "upstream_review.waiting_ci")

    def test_a_merged_pr_is_recorded_without_its_branch(self):
        rec = record()
        out = tick(rec, pr=pull(merged=True, state="closed"), branch=None)
        self.assertEqual(kinds(out), ["record_merge"])

    def test_the_head_the_pipeline_published_is_recorded_without_a_row(self):
        op = {"operation_id": "op-1", "contribution_id": "example--proj/12",
              "kind": "branch_update", "status": "confirmed",
              "target": {"ref": "riscv64", "expected_head": SHA_A},
              "proposed": {"commit": SHA_B}}
        rec = record(publication_operation_id="op-1")
        out = tick(rec, pr=pull(head=SHA_B), branch=SHA_B, operations={"op-1": op},
                   verdict="failed")
        after = final(out, rec)
        self.assertEqual(kinds(out), ["record_upstream_ci_failure"])
        self.assertEqual((after["upstream_pr"]["head_sha"], after["revision"]),
                         (SHA_B, rec["revision"]))
        # Another head on the branch is a change the pipeline did not publish.
        out = tick(rec, pr=pull(head=SHA_C), branch=SHA_C, operations={"op-1": op})
        self.assertEqual(kinds(out), ["record_head_change"])

    def test_states_without_an_observation_row_are_not_read(self):
        pending = {"kind": "answer_reviewer", "requested_at": NOW, "reminded_at": None,
                   "escalated_at": None, "counter": None}
        for rec in (record("fix_needed"), record("agent_running", active_work=dict(REVISION_WORK)),
                    record("waiting_human", pending_human_action=pending)):
            with self.subTest(state=engine.state_key(rec)):
                out = tick(rec, pr=pull(merged=True, state="closed"), verdict="failed",
                           feedback=REVIEW)
                self.assertEqual(out.reads, [])
                self.assertNotIn("record_merge", kinds(out))
                self.assertEqual(engine.state_key(final(out, rec)), engine.state_key(rec))

    def test_a_closed_pr_is_resumed_only_once_it_is_open_again(self):
        rec = final(tick(record(), pr=pull(state="closed")), record())
        resume = f'/upstreaming resume revision={rec["revision"]} reason="reopened"'
        out = tick(rec, pr=pull(state="closed"),
                   comments=[{"issue_number": 12, "comment": {"id": 900, "body": resume,
                                                              "user": dict(OWNER)}}])
        self.assertEqual(out.reads, ["pull"])
        self.assertNotIn("resume_upstream_review", kinds(out))
        self.assertEqual(engine.state_key(final(out, rec)), "upstream_review.waiting_human")
        out = tick(rec, pr=pull(),
                   comments=[{"issue_number": 12, "comment": {"id": 901, "body": resume,
                                                              "user": dict(OWNER)}}])
        self.assertIn("resume_upstream_review", kinds(out))
        self.assertEqual(engine.state_key(final(out, rec)), "upstream_review.waiting_ci")

    def test_a_failed_read_for_a_resume_holds_no_other_command_back(self):
        parked = contribution(phase="parked", activity="idle", parked_reason="x",
                              previous_phase="upstream_review",
                              upstream_pr=copy.deepcopy(UPSTREAM_PR))
        snapshot = reconcile.Snapshot({"c": copy.deepcopy(parked)}, {}, {}, {}, {})
        abandon = {"issue_number": 12, "comment": {"id": 902, "user": dict(OWNER),
                                                   "body": '/upstreaming abandon reason="gone"'}}
        with mock.patch("observe_ci.collect.upstream_pull",
                        side_effect=collect.CollectError("GET pulls/9: HTTP 404")), \
                contextlib.redirect_stderr(io.StringIO()):
            out = reconcile.tick(object(), snapshot, policy=POLICY, now=T, publications=[],
                                 comments=[abandon], pressure=capacity.Pressure(ok=False),
                                 observe=True, dispatcher_runs=[], queue_works={})
        self.assertEqual(out.failures, [])
        self.assertEqual(kinds(out), ["abandon"])

    def test_the_resume_facts_follow_the_observed_pr(self):
        closed = record("waiting_human", pending_human_action={
            "kind": "pr_closed_unmerged", "requested_at": NOW, "reminded_at": None,
            "escalated_at": None, "counter": None})
        body = commands.parse(f'/upstreaming resume revision={closed["revision"]} reason="r"')
        self.assertFalse(apply_command(closed, body, upstream_pr={"state": "closed"}).accepted)
        self.assertFalse(apply_command(closed, body).accepted)
        self.assertTrue(apply_command(closed, body, upstream_pr={"state": "open"}).accepted)
        parked = contribution(phase="parked", activity="idle", parked_reason="x",
                              previous_phase="upstream_review",
                              upstream_pr=copy.deepcopy(UPSTREAM_PR))
        body = commands.parse(f'/upstreaming resume revision={parked["revision"]} reason="r"')
        self.assertFalse(apply_command(parked, body).accepted)
        self.assertFalse(apply_command(parked, body, upstream_pr={"state": "open"}).accepted)
        out = apply_command(parked, body, upstream_pr={"state": "closed", "merged": False})
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(engine.state_key(out.record), "queued.idle")


class TestRevisionResults(unittest.TestCase):
    """Every Result of revision Work takes a row of `upstream_review.agent_running`."""

    def running(self, **fields):
        fields.setdefault("active_work", dict(REVISION_WORK,
                                              feedback_evidence_ids=["review-old"]))
        return record("agent_running", pending_feedback_evidence_ids=["review-old", "review-new"],
                      **fields)

    def test_each_publisher_result_takes_its_row(self):
        expected = {
            "published": ("record_revision_publish_success", "upstream_review.waiting_ci", False),
            "uncertain": ("record_revision_publish_success", "upstream_review.waiting_ci", False),
            "no_change": ("require_reviewer_answer", "upstream_review.waiting_human", False),
            "proposal": ("reject_patch_proposal", "upstream_review.fix_needed", True),
            "rejected_validation": ("reject_patch_proposal", "upstream_review.fix_needed", True),
            "rejected_stale": ("reject_patch_proposal", "upstream_review.fix_needed", True),
            "worker_blocked": ("reject_patch_proposal", "upstream_review.fix_needed", True),
            "infra_error": ("schedule_publish_retry", "upstream_review.agent_running", False),
        }
        for name, (verb, state, bumped) in expected.items():
            with self.subTest(result=name):
                rec = self.running()
                out = tick(rec, [publication(rec, name)])
                self.assertEqual(out.failures, [])
                self.assertEqual(kinds(out)[0], verb)
                after = final(out, rec)
                self.assertEqual(engine.state_key(after), state)
                self.assertEqual(after["revision"], rec["revision"] + bumped)
                self.assertIsNone(after["active_work"])
                if verb == "record_revision_publish_success":
                    # Exactly the feedback the Work carried is answered.
                    self.assertEqual(after["pending_feedback_evidence_ids"], ["review-new"])
                else:
                    self.assertEqual(after["pending_feedback_evidence_ids"],
                                     ["review-old", "review-new"])
                if verb == "require_reviewer_answer":
                    self.assertEqual(after["pending_human_action"]["kind"], "answer_reviewer")

    def test_publish_retries_then_a_human_wait(self):
        rec = self.running()
        out = tick(rec, [publication(rec, "infra_error")])
        rec = final(out, rec)
        self.assertEqual(rec["pending_publication"]["attempts"], 1)
        now = T
        for _ in range(12):
            now = now + timedelta(hours=6)
            out = tick(rec, now=now)
            rec = final(out, rec)
            if engine.state_key(rec) != "upstream_review.agent_running":
                break
        self.assertEqual(engine.state_key(rec), "upstream_review.waiting_human")
        self.assertEqual((rec["pending_human_action"]["kind"],
                          rec["pending_human_action"]["counter"]),
                         ("publication_infrastructure_failure", "publish_recovery"))
        resumed = apply_command(rec, commands.parse(
            f'/upstreaming resume revision={rec["revision"]} reason="fixed"'))
        self.assertTrue(resumed.accepted, resumed.reason)
        self.assertEqual(engine.state_key(resumed.record), "upstream_review.fix_needed")

    def test_a_held_publication_is_retried_in_upstream_review(self):
        rec = record("agent_running", pending_publication=dict(PENDING))
        out = tick(rec)
        self.assertEqual([r["contribution_id"] for r in out.report["publication_retries"]],
                         [rec["contribution_id"]])

    def test_the_answer_to_the_reviewer_is_resumed_without_work(self):
        rec = final(tick(self.running(), [publication(self.running(), "no_change")]),
                    self.running())
        out = apply_command(rec, commands.parse(
            f'/upstreaming resume revision={rec["revision"]} reason="answered"'))
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(engine.state_key(out.record), "upstream_review.waiting_ci")
        self.assertEqual(out.record["pending_feedback_evidence_ids"], [])
        self.assertIsNone(out.record["active_work"])


class TestRevisionWork(unittest.TestCase):
    """Revision Work is allocated and exhausted by the rows of
    `upstream_review.fix_needed`; nothing parks without a command."""

    def test_a_failing_job_charges_its_cluster_and_review_only_work_total(self):
        rec = record("waiting_ci")
        failed = tick(rec, verdict="failed")
        observed = final(failed, rec)
        self.assertEqual(engine.state_key(observed), "upstream_review.fix_needed")
        evidence = {e["evidence_id"]: e for e in staged(failed, "evidence")}
        out = tick(observed, evidence=evidence, capacity_ok=True)
        self.assertIn("request_upstream_revision", kinds(out))
        after = final(out, observed)
        self.assertEqual(after["active_work"]["task"], "upstream_revision")
        self.assertEqual(after["budget"]["attempts_used"], 1)
        self.assertEqual(sum(after["budget"]["clusters"].values()), 1)

        reviewed = final(tick(rec, feedback=REVIEW), rec)
        out = tick(reviewed, capacity_ok=True)
        self.assertIn("request_review_revision", kinds(out))
        after = final(out, reviewed)
        self.assertEqual(after["active_work"]["feedback_evidence_ids"],
                         reviewed["pending_feedback_evidence_ids"])
        self.assertEqual((after["budget"]["attempts_used"], after["budget"]["clusters"]), (1, {}))

    def test_exhaustion_waits_for_a_human_and_never_parks(self):
        limit = lifecycle.budget_limit("no_progress", POLICY)
        reviewed = record("fix_needed", pending_feedback_evidence_ids=["review-1"])
        reviewed["budget"] = dict(reviewed["budget"], no_progress_streak=limit)
        out = tick(reviewed)
        self.assertEqual(kinds(out), ["exhaust_repair_attempts"])
        after = final(out, reviewed)
        self.assertEqual(engine.state_key(after), "upstream_review.waiting_human")
        self.assertEqual(after["pending_human_action"]["counter"], "no_progress")

    def test_no_park_from_upstream_review_without_a_command(self):
        for edge in lifecycle.table_edges():
            if lifecycle.state_phase(edge.source) == "upstream_review" \
                    and edge.target == "parked.idle":
                self.assertEqual(edge.transition.trigger, lifecycle.Trigger.COMMAND)
        park = commands.parse('/upstreaming park reason="x"')
        for activity in ("waiting_ci", "fix_needed", "agent_running"):
            with self.subTest(state=activity):
                out = apply_command(record(activity), park)
                self.assertFalse(out.accepted)
        waiting = record("waiting_human", pending_human_action={
            "kind": "answer_reviewer", "requested_at": NOW, "reminded_at": None,
            "escalated_at": None, "counter": None})
        self.assertTrue(apply_command(waiting, park).accepted)
        # Ticks over every observation and Result never park.
        for rec in (record(), record("fix_needed"), record("agent_running",
                                                           active_work=dict(REVISION_WORK))):
            for kwargs in (dict(verdict="failed"), dict(pr=pull(state="closed")),
                           dict(feedback=REVIEW), dict(pr=pull(head=SHA_B), branch=SHA_B)):
                pubs = [publication(rec, "rejected_validation")] if rec["active_work"] else []
                with self.subTest(state=engine.state_key(rec), **{k: str(v)[:20] for k, v in kwargs.items()}):
                    after = final(tick(rec, pubs, **kwargs), rec)
                    self.assertNotEqual(after["phase"], "parked")


class TestRelease(unittest.TestCase):
    CONTRACT = {"schema_version": 1, "evidence_id": "contract-1", "kind": "target_contract",
                "data": {"distributions": ["proj"],
                         "release_rows": [{"interpreter": "cp312", "abi": "cp312",
                                           "libc": "manylinux", "floor": [2, 39]}]}}

    def pypi(self, *files):
        return {"proj": {"releases": {"1.1.0": [
            {"filename": name, "yanked": False, "digests": {"sha256": "0" * 64},
             "upload_time_iso_8601": "2026-11-22T00:00:00Z"} for name in files]}}}

    def test_without_a_contract_an_operator_acts(self):
        rec = released_wait()
        out = tick(rec)
        self.assertEqual(kinds(out), ["require_release_intervention"])
        after = final(out, rec)
        self.assertEqual((engine.state_key(after), after["pending_human_action"]["kind"]),
                         ("release_wait.waiting_human", release.CONTRACT_MISSING))
        resumed = apply_command(after, commands.parse(
            f'/upstreaming resume revision={after["revision"]} reason="contract recorded"'))
        self.assertTrue(resumed.accepted, resumed.reason)
        self.assertEqual(engine.state_key(resumed.record), "release_wait.idle")

    def test_an_unknown_distribution_needs_an_operator(self):
        rec = released_wait(contract="contract-1")
        out = tick(rec, contracts=[self.CONTRACT], pypi={})
        self.assertEqual(final(out, rec)["pending_human_action"]["kind"],
                         release.DISTRIBUTION_MISSING)

    def test_incomplete_coverage_is_recorded_once_and_polled(self):
        rec = released_wait(contract="contract-1")
        pypi = self.pypi("proj-1.1.0-cp312-cp312-manylinux_2_39_x86_64.whl")
        out = tick(rec, contracts=[self.CONTRACT], pypi=pypi, contains={"1.1.0": True})
        self.assertEqual(kinds(out), ["record_release_incomplete"])
        after = final(out, rec)
        self.assertEqual(engine.state_key(after), "release_wait.idle")
        self.assertIsNotNone(after["next_poll_at"])
        key = staged(out, "events")[0]["details"]["observation_key"]
        # Not read again before the poll time; recorded once per observation.
        self.assertEqual(kinds(tick(after, contracts=[self.CONTRACT], pypi=pypi,
                                    contains={"1.1.0": True})), [])
        later = tick(after, contracts=[self.CONTRACT], pypi=pypi, contains={"1.1.0": True},
                     now=T + timedelta(hours=2), audited={rec["contribution_id"]: {key}})
        self.assertEqual(kinds(later), [])

    def test_a_release_without_the_merge_is_incomplete(self):
        rec = released_wait(contract="contract-1")
        pypi = self.pypi("proj-1.1.0-cp312-cp312-manylinux_2_39_riscv64.whl")
        for contains in ({"1.1.0": False}, {}):
            out = tick(rec, contracts=[self.CONTRACT], pypi=pypi, contains=contains)
            self.assertEqual(kinds(out), ["record_release_incomplete"])

    def test_verified_coverage_releases(self):
        rec = released_wait(contract="contract-1")
        pypi = self.pypi("proj-1.1.0-cp312-cp312-manylinux_2_39_riscv64.whl")
        out = tick(rec, contracts=[self.CONTRACT], pypi=pypi, contains={"1.1.0": True})
        self.assertEqual(kinds(out), ["verify_release"])
        after = final(out, rec)
        self.assertEqual(engine.state_key(after), "released.idle")
        [ev] = [e for e in staged(out, "evidence") if e["kind"] == "release_acceptance"]
        self.assertEqual(after["release_acceptance"]["evidence_id"], ev["evidence_id"])
        self.assertTrue(after["release_acceptance"]["complete"])

    def test_a_release_uploaded_before_the_merge_is_not_a_candidate(self):
        contract = release.acceptance_contract(self.CONTRACT)
        pypi = self.pypi("proj-1.1.0-cp312-cp312-manylinux_2_39_riscv64.whl")
        merge = dict(MERGE, merged_at="2026-11-22T12:00:00+00:00")
        decision = release.decide(contract, merge=merge, pypi=pypi, contains=lambda v: True)
        self.assertEqual(decision.verb, "record_release_incomplete")


class TestLedgerAndResponses(unittest.TestCase):
    def test_upstream_messages_and_quoted_comments(self):
        rec = record()
        out = tick(rec, feedback=INQUIRY, verdict="passed")
        [event] = staged(out, "events")
        event["sequence"] = 3
        body = status_ledger.render(event, final(out, rec), "https://github.com/run")
        self.assertIn("on the upstream PR", body)
        self.assertIn("`Why riscv64 @maintainer?`", body)
        for verb, kwargs in (("record_merge", dict(pr=pull(merged=True, state="closed"))),
                             ("record_pr_closed_unmerged", dict(pr=pull(state="closed"))),
                             ("record_upstream_ci_failure", dict(verdict="failed")),
                             ("record_head_change", dict(pr=pull(head=SHA_B), branch=SHA_B))):
            with self.subTest(verb=verb):
                out = tick(rec, **kwargs)
                [event] = [e for e in staged(out, "events") if e["kind"] == verb]
                event["sequence"] = 3
                text = status_ledger.render(event, final(out, rec), "https://github.com/run")
                self.assertNotIn(verb.replace("_", " ").capitalize(), text)

    def test_nothing_is_answered_on_the_upstream_pr(self):
        rec = record()
        self.assertEqual(review_response.apply(object(), record=rec, task={}, proposal={},
                                               outcome={"status": "published",
                                                        "candidate": SHA_B}), [])


if __name__ == "__main__":
    unittest.main()
