# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""An error in one contribution's processing does not stop the controller tick.

Each test injects an exception into one step of the tick for one of two
contributions and checks the decision for that case:

* an unexpected error in one contribution discards everything the tick staged
  for it and every effect requested for it, the other contribution is
  processed as if nothing happened, and the errored one takes
  ``pause_on_processing_error`` on its committed record;
* unexpected errors in two contributions, an error outside any contribution,
  and a pause that cannot be applied stop the whole tick;
* expected rejections pause nothing;
* a collector failure skips the contribution for the tick.
"""

from __future__ import annotations

import contextlib
import copy
import http.client
import io
import json
import ssl
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import capacity
import collect
import compat
import controller
import engine
import isolation
import lifecycle
import observe_ci
import reconcile
import state
import status_ledger
from common import canonical_json
from tests.helpers import NOW, OWNER, SHA_A, awaiting_submission, contribution, policy
from tests.test_collect_read_errors import Response
from tests.test_late_results import WORK, ci_snapshot, publication

T = datetime.fromisoformat("2026-11-23T09:00:00+00:00")
ISO = T.isoformat()
POLICY = policy()
A = "example--proj/12"
B = "other--lib/13"
FORK_A = "riseproject-upstreaming/proj"
FORK_B = "riseproject-upstreaming/lib"
VERB = "pause_on_processing_error"
REAL = {name: getattr(module, name) for module, name in (
    (reconcile, "apply_publication"), (controller, "_actionable"), (controller, "apply_command"),
    (compat, "observe_status"))}


def expected_pause(before, reason):
    """``pause_on_processing_error`` on ``before``: through the engine in a
    state of the table, through ``compat.legacy_pause`` in a legacy state."""
    if compat.is_legacy_state(before):
        return compat.legacy_pause(before, VERB, reason=reason, now=ISO, cause_id="4242").record
    return engine.apply_transition(before, VERB, policy=POLICY, now=ISO, cause_id="4242",
                                   facts={"processing_error"},
                                   values={"paused_reason": reason}).record


class Boom(RuntimeError):
    pass


def other(**fields):
    """A second contribution, tracked by issue 13, with its own fork."""
    rec = contribution(**fields)
    rec.update({
        "contribution_id": B, "project_id": "other--lib",
        "tracking_issue": dict(rec["tracking_issue"], id=5001, number=13),
        "upstream": dict(rec["upstream"], repository_id=3, full_name="other/lib"),
    })
    if rec.get("fork"):
        rec["fork"] = dict(rec["fork"], repository_id=4, name=FORK_B)
    return rec


def waiting(**fields):
    return contribution(**{"activity": "waiting_ci", "dossier_evidence_id": "dossier-1",
                           "scope_paths": ["a"], "scope_coverage_complete": True, **fields})


def other_waiting(**fields):
    return other(**{"activity": "waiting_ci", "dossier_evidence_id": "dossier-1",
                    "scope_paths": ["a"], "scope_coverage_complete": True, **fields})


def comment(body, issue, cid=900):
    return {"issue_number": issue, "comment": {"id": cid, "body": body, "user": dict(OWNER)}}


def only_for(cid, real, error=Boom("injected failure")):
    """``real``, raising ``error`` when called for contribution ``cid``."""
    def call(record, *args, **kwargs):
        if record["contribution_id"] == cid:
            raise error
        return real(record, *args, **kwargs)
    return call


def branch_head(failing=None, error=Boom("injected failure")):
    def read(_api, fork, _ref):
        if fork == failing:
            raise error
        return {"head": SHA_A}
    return read


def ci(repository, head, verdict):
    """The CI of one fork: its own repository, so its own evidence."""
    snapshot = ci_snapshot(head, verdict)
    snapshot.repository = {"id": 2 if repository == FORK_A else 4, "full_name": repository}
    return snapshot


def tick(records, *, publications=(), comments=(), issues=(), observe=True, verdict="pending",
         head=None, queue_works=None, patches=(), policy=POLICY, run_id="4242", operations=None):
    """One tick over ``records``; ``patches`` are (target, replacement)."""
    snapshot = reconcile.Snapshot({r["contribution_id"]: copy.deepcopy(r) for r in records},
                                  copy.deepcopy(operations or {}), {}, {}, {})
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("observe_ci.collect.branch_head",
                                       side_effect=head or branch_head()))
        stack.enter_context(mock.patch("observe_ci.collect.development_review_feedback",
                                       return_value=None))
        stack.enter_context(mock.patch("observe_ci.collect.ci_snapshot",
                                       side_effect=lambda _a, repo, sha: ci(repo, sha, verdict)))
        for target, replacement in patches:
            stack.enter_context(mock.patch(target, side_effect=replacement))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return reconcile.tick(object(), snapshot, policy=policy, now=T, run_id=run_id,
                              publications=list(publications), comments=list(comments),
                              issues=list(issues), pressure=capacity.Pressure(ok=True),
                              observe=observe, dispatcher_runs=[], queue_works=queue_works or {})


def staged_for(output, cid):
    return [(kind, r) for kind, r in output.staged if r.get("contribution_id") == cid]


def record_of(output, cid):
    records = [r for kind, r in staged_for(output, cid) if kind == "contributions"]
    return records[-1] if records else None


def kinds(output, cid):
    return [r["kind"] for kind, r in staged_for(output, cid) if kind == "events"]


class TestOneContributionErrors(unittest.TestCase):
    """An unexpected error in one step, for one contribution."""

    def assert_isolated(self, output, before, baseline, step):
        """A is paused on its committed record and nothing else of it is
        staged or requested; B is processed exactly as without the error."""
        staged = staged_for(output, A)
        self.assertEqual([kind for kind, _r in staged], ["contributions", "events"], staged)
        paused, event = staged[0][1], staged[1][1]
        self.assertEqual(paused, expected_pause(before, paused["paused_reason"]))
        self.assertEqual((paused["paused"], paused["paused_by"], paused["paused_at"]),
                         (True, "controller", ISO))
        self.assertEqual(paused["revision"], before["revision"])
        self.assertIsNone(paused["active_work"])
        self.assertIsNone(paused["pending_publication"])
        for text in ("processing_error", "Boom", "injected failure", "run 4242", f"step {step}"):
            self.assertIn(text, paused["paused_reason"])
        self.assertEqual((event["kind"], event["cause"]), (VERB, {"type": "tick", "id": "4242"}))
        self.assertEqual({k: event["details"][k] for k in ("step", "error_class", "message",
                                                           "run_id")},
                         {"step": step, "error_class": "Boom", "message": "injected failure",
                          "run_id": "4242"})
        self.assertEqual(reconcile._touched(
            reconcile.TickOutput([], output.report), A), [])
        # The other contribution: the same records, events, and requests.
        self.assertEqual(staged_for(output, B), staged_for(baseline, B))
        for name in reconcile._EFFECT_LISTS:
            self.assertEqual([i for i in output.report[name] if i.get("contribution_id") == B],
                             [i for i in baseline.report[name] if i.get("contribution_id") == B])
        alerts = [a for a in output.report["controller"]["alerts"] if a.get("contribution") == A]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Boom: injected failure", alerts[0]["alert"])
        self.assertIn("paused by the controller", alerts[0]["alert"])
        self.assertEqual([(e["contribution"], e["step"]) for e in output.report["processing_errors"]],
                         [(A, step)])
        self.assertEqual([(f.contribution_id, type(f)) for f in output.failures],
                         [(A, isolation.ProcessingError)])

    def test_publication_fold(self):
        a = contribution(activity="agent_running", active_work=dict(WORK))
        b = other_waiting()
        pubs = [publication(a, "published")]
        baseline = tick([a, b], publications=pubs)
        self.assertEqual(record_of(baseline, A)["activity"], "waiting_ci")
        output = tick([a, b], publications=pubs, patches=[
            ("reconcile.apply_publication", only_for(A, REAL["apply_publication"]))])
        self.assert_isolated(output, a, baseline, "publication")
        # The artifact is not marked as processed: the next tick reads it again.
        self.assertEqual(record_of(output, A)["processed_publications"],
                         a.get("processed_publications") or [])

    def test_an_operation_staged_over_a_closed_one_pauses_only_its_contribution(self):
        import recovery
        a = contribution(activity="agent_running", active_work=dict(WORK))
        b = other_waiting()
        failed = recovery.plan(contribution_id=A, revision=0, kind="branch_update",
                               policy_version="p", reason_ids=["r"],
                               target={"repository": FORK_A, "ref": "riscv64",
                                       "expected_head": SHA_A}, now=NOW)
        failed["status"] = "failed"
        ops = {failed["operation_id"]: failed}
        reused = dict(failed, status="confirmed")
        baseline = tick([a, b], publications=[publication(a, "published")], operations=ops)
        output = tick([a, b], publications=[dict(publication(a, "published"), operation=reused)],
                      operations=ops)
        self.assertEqual([(f.contribution_id, f.step, type(f)) for f in output.failures],
                         [(A, "operation_check", isolation.ProcessingError)])
        staged = staged_for(output, A)
        self.assertEqual([kind for kind, _r in staged], ["contributions", "events"], staged)
        paused = staged[0][1]
        self.assertEqual((paused["paused"], paused["paused_by"]), (True, "controller"))
        self.assertIn("failed cannot become confirmed", paused["paused_reason"])
        self.assertIn("step operation_check", paused["paused_reason"])
        self.assertEqual(staged_for(output, B), staged_for(baseline, B))
        self.assertNotIn("operations", [kind for kind, _r in output.staged])
        # What remains to commit passes the store's own check.
        for kind, r in output.staged:
            if kind == "operations":
                self.assertIsNone(recovery.transition_problem(ops.get(r["operation_id"]), r))

    def test_dispatch_recovery(self):
        a = contribution(activity="agent_running", active_work=dict(WORK))
        b = other_waiting()
        import work
        real = work.graph_id

        def graph_id(key):
            if key == WORK["dedupe_key"]:
                raise Boom("injected failure")
            return real(key)
        baseline = tick([a, b])
        output = tick([a, b], patches=[("work.graph_id", graph_id)])
        self.assert_isolated(output, a, baseline, "dispatch_recovery")
        self.assertEqual(output.report["dispatch_relaunch"], [])
        # The pause from `development.agent_running` leads to the state in
        # which the Work is due again.
        self.assertEqual(engine.state_key(record_of(output, A)), "development.fix_needed")

    def test_observation(self):
        a, b = waiting(), other_waiting()
        baseline = tick([a, b], verdict="passed")
        self.assertEqual(engine.state_key(record_of(baseline, B)), "handoff.awaiting_submission")
        output = tick([a, b], verdict="passed", head=branch_head(FORK_A))
        self.assert_isolated(output, a, baseline, "observation")

    def test_observation_error_after_evidence_was_staged(self):
        """All or nothing: the CI evidence the observation had already staged
        for the errored contribution is discarded with the rest."""
        a = waiting(activity="idle", last_ci_evidence_id="ci-0")
        # Paused, so it competes for no worker slot in either tick.
        b = other_waiting(paused=True, paused_by="human", paused_at=NOW)
        baseline = tick([a, b], verdict="failed")
        self.assertIn("evidence", [kind for kind, _r in staged_for(baseline, A)])
        output = tick([a, b], verdict="failed", patches=[
            ("observe_ci.compat.observe_status", only_for(A, REAL["observe_status"]))])
        self.assert_isolated(output, a, baseline, "observation")

    def test_work_selection(self):
        a = contribution(activity="fix_needed", dossier_evidence_id="dossier-1",
                         scope_paths=["a"], scope_coverage_complete=True)
        b = other_waiting()
        baseline = tick([a, b])
        self.assertEqual([e["contribution_id"] for e in baseline.report["requested_effects"]], [A])
        output = tick([a, b], patches=[
            ("controller._actionable", only_for(A, REAL["_actionable"]))])
        self.assert_isolated(output, a, baseline, "work_selection")
        self.assertEqual(output.report["requested_effects"], [])

    def test_ci_evidence_missing_from_the_store(self):
        """A record that names CI evidence the store does not hold is not
        deferred silently: the error is attributed to it, it is paused with
        ``pause_on_processing_error``, and the other contribution is
        processed."""
        a = contribution(activity="fix_needed", dossier_evidence_id="dossier-1",
                         scope_paths=["a"], scope_coverage_complete=True,
                         last_ci_evidence_id="ci-gone")
        b = other_waiting()
        output = tick([a, b], observe=False)
        paused = record_of(output, A)
        self.assertTrue(paused["paused"])
        self.assertIn("MissingEvidence", paused["paused_reason"])
        self.assertIn("ci-gone", paused["paused_reason"])
        self.assertEqual(kinds(output, A), [VERB])
        self.assertEqual([(e["contribution"], e["step"], e["error_class"])
                          for e in output.report["processing_errors"]],
                         [(A, "work_selection", "MissingEvidence")])
        self.assertEqual(output.report["requested_effects"], [])

    def test_the_other_contribution_gets_the_worker_slot(self):
        """The errored contribution requests nothing; the slot it would have
        taken goes to the next candidate in the same tick."""
        fields = dict(activity="fix_needed", dossier_evidence_id="dossier-1", scope_paths=["a"],
                      scope_coverage_complete=True)
        a, b = contribution(**fields), other(**fields)
        output = tick([a, b], patches=[
            ("controller._actionable", only_for(A, REAL["_actionable"]))])
        self.assertEqual([e["contribution_id"] for e in output.report["requested_effects"]], [B])
        self.assertEqual(record_of(output, B)["activity"], "agent_running")
        self.assertEqual(kinds(output, A), [VERB])

    def test_command_application_and_stop_command_failure(self):
        """A stop command that cannot be applied: the contribution is errored,
        nothing is launched for it, and the command stays unconsumed."""
        fields = dict(activity="fix_needed", dossier_evidence_id="dossier-1", scope_paths=["a"],
                      scope_coverage_complete=True)
        a, b = contribution(**fields), other_waiting()
        for body in ("/upstreaming pause", '/upstreaming park reason="stop"'):
            with self.subTest(command=body):
                comments = [comment(body, 12)]
                applied = tick([a, b], comments=comments)
                self.assertTrue(compat.stopped(record_of(applied, A)))
                self.assertEqual(applied.report["requested_effects"], [])
                baseline = tick([a, b])
                output = tick([a, b], comments=comments, patches=[
                    ("controller.apply_command", only_for(A, REAL["apply_command"]))])
                self.assert_isolated(output, a, baseline, "command")
                self.assertEqual(output.report["requested_effects"], [])
                self.assertEqual([r for kind, r in output.staged if kind == "commands"], [])
                self.assertEqual(output.report["controller"]["commands"],
                                 [{"comment": 900, "result": "deferred",
                                   "reason": "the contribution is not processed in this tick"}])

    def test_a_record_on_another_development_branch_is_a_processing_error(self):
        """Development happens on `riscv64-dev`: a record that names another
        development branch is paused on its committed record, never acted
        on, and the other contribution is processed as without it."""
        a, b = awaiting_submission(), other_waiting()
        a["fork"]["head_ref"] = "riscv64"
        baseline = tick([dict(a, fork=dict(a["fork"], head_ref="riscv64-dev")), b],
                        verdict="passed")
        output = tick([a, b], verdict="passed")
        staged = staged_for(output, A)
        self.assertEqual([kind for kind, _r in staged], ["contributions", "events"], staged)
        paused = staged[0][1]
        self.assertTrue(paused["paused"])
        self.assertIn("'riscv64', not 'riscv64-dev'", paused["paused_reason"])
        self.assertIn("step record_shape", paused["paused_reason"])
        self.assertEqual((engine.state_key(paused), paused["revision"], paused["fork"]),
                         (engine.state_key(a), a["revision"], a["fork"]))
        self.assertEqual(staged_for(output, B), staged_for(baseline, B))
        # Paused, it is not paused again, and nothing is done for it.
        again = tick([paused, b], verdict="passed")
        self.assertEqual(staged_for(again, A), [])

    def test_error_in_a_handoff_state_is_taken_through_the_engine(self):
        a, b = awaiting_submission(), other_waiting()
        baseline = tick([a, b], verdict="passed")
        output = tick([a, b], verdict="passed", head=branch_head(FORK_A))
        self.assert_isolated(output, a, baseline, "observation")
        paused = record_of(output, A)
        self.assertEqual(engine.state_key(paused), "handoff.awaiting_submission")
        self.assertEqual((paused["handoff_request"], paused["pending_human_action"],
                          paused["fork"], paused["last_ci_evidence_id"]),
                         (a["handoff_request"], a["pending_human_action"], a["fork"],
                          a["last_ci_evidence_id"]))
        event = [r for kind, r in staged_for(output, A) if kind == "events"][0]
        self.assertEqual((event["details"]["from_state"], event["details"]["to_state"]),
                         ("handoff.awaiting_submission",) * 2)

    def test_what_is_held_is_cleared_like_other_stops(self):
        held = {"worker_run_id": "555", "attempts": 1, "last_reason": "x",
                "next_attempt_at": "2026-11-24T09:00:00+00:00", "exhausted": False}
        artifact = {"work_id": "w", "claim_id": "c", "artifact_id": 5, "name": "n",
                    "digest": "sha256:" + "0" * 64, "run_id": "555", "result_commit": "d" * 40,
                    "outcome": "published"}
        a = waiting(activity="waiting_capacity", pending_publication=held,
                    proposal_artifact=artifact)
        output = tick([a, other_waiting()], head=branch_head(FORK_A))
        paused = record_of(output, A)
        # The published Result is kept; what is held for publication is not.
        self.assertEqual((paused["paused"], paused["pending_publication"],
                          paused["proposal_artifact"]), (True, None, artifact))
        self.assertEqual(output.report["publication_retries"], [])


class TestPipelineFailures(unittest.TestCase):
    """What stops the whole tick: nothing is returned, so nothing is committed."""

    def test_errors_in_two_contributions_stop_the_tick(self):
        a, b = waiting(), other_waiting()

        def read(_api, _fork, _ref):
            raise Boom("injected failure")
        with self.assertRaises(isolation.TickFailure) as raised:
            tick([a, b], head=read)
        message = str(raised.exception)
        self.assertIn("more than one contribution", message)
        self.assertIn("no contribution is paused", message)
        self.assertIn(A, message)
        self.assertIn(B, message)
        self.assertEqual({f.contribution_id for f in raised.exception.failures}, {A, B})

    def test_errors_in_two_different_steps_stop_the_tick(self):
        a = contribution(activity="agent_running", active_work=dict(WORK))
        with self.assertRaises(isolation.TickFailure):
            tick([a, other_waiting()], publications=[publication(a, "published")],
                 head=branch_head(FORK_B),
                 patches=[("reconcile.apply_publication", only_for(A, REAL["apply_publication"]))])

    def test_error_outside_any_contribution_stops_the_tick(self):
        a, b = waiting(), other_waiting()

        def refuse(*_args, **_kwargs):
            raise Boom("command intake failed")
        with self.assertRaises(Boom):
            tick([a, b], comments=[comment("/upstreaming pause", 12)],
                 patches=[("controller.intake.accept_command", refuse)])
        with self.assertRaises(Boom):
            tick([a, b], patches=[("reconcile.observe_ci.dedupe", refuse)])
        # A command on an issue that tracks no contribution belongs to none.
        with self.assertRaises(Boom):
            tick([a, b], comments=[comment("/upstreaming pause", 99)],
                 patches=[("controller._apply", lambda *_a, **_k: (_ for _ in ()).throw(Boom()))])

    def test_a_pause_that_cannot_be_applied_stops_the_tick(self):
        a, b = waiting(), other_waiting()

        def refuse(*_args, **_kwargs):
            raise lifecycle.TransitionError("the verb does not apply")
        with self.assertRaises(isolation.TickFailure) as raised:
            tick([a, b], head=branch_head(FORK_A),
                 patches=[("reconcile.pause_on_processing_error", refuse)])
        self.assertIn(f"{VERB} could not be applied to {A}", str(raised.exception))
        self.assertIn("the verb does not apply", str(raised.exception))

    def test_a_record_outside_every_admitted_state_cannot_be_paused(self):
        description = {"step": "s", "error_class": "Boom", "message": "m", "run_id": "1"}
        strange = waiting(phase="handoff", activity="waiting_capacity")
        with self.assertRaises(lifecycle.TransitionError):
            reconcile.pause_on_processing_error(strange, description, policy=POLICY, now=ISO)
        # The controller always states the reason, in a legacy state and in the table.
        with self.assertRaises(lifecycle.TransitionError):
            compat.legacy_pause(waiting(activity="idle"), VERB, reason=" ", now=ISO, cause_id=None)
        with self.assertRaises(lifecycle.TransitionError):
            engine.apply_transition(waiting(), VERB, policy=POLICY, now=ISO,
                                    facts={"processing_error"}, values={"paused_reason": " "})
        with self.assertRaises(lifecycle.TransitionError):
            compat.legacy_pause(waiting(), VERB, reason="r", now=ISO, cause_id=None)


class TestExpectedRejections(unittest.TestCase):
    """Rejected with a reason; no pause, and the tick is computed once."""

    def assert_not_isolated(self, output):
        self.assertEqual(output.failures, [])
        self.assertNotIn("processing_errors", output.report)
        self.assertNotIn("skipped_contributions", output.report)
        self.assertEqual(output.report["controller"]["alerts"], [])
        self.assertNotIn(VERB, kinds(output, A))

    def test_rejected_command(self):
        a, b = waiting(), other_waiting()
        output = tick([a, b], comments=[comment("/upstreaming resume revision=0 reason=r", 12)])
        self.assert_not_isolated(output)
        receipt = [r for kind, r in output.staged if kind == "commands"][0]
        self.assertEqual((receipt["result"], receipt["reason"]), ("rejected", "not paused"))
        self.assertFalse((record_of(output, A) or a)["paused"])

    def test_transition_error_from_a_command(self):
        a, b = waiting(), other_waiting()
        refused = lifecycle.TransitionError("the table refuses this")
        output = tick([a, b], comments=[comment("/upstreaming pause", 12)], patches=[
            ("controller.apply_command", only_for(A, REAL["apply_command"], refused))])
        self.assert_not_isolated(output)
        receipt = [r for kind, r in output.staged if kind == "commands"][0]
        self.assertEqual((receipt["result"], receipt["reason"]),
                         ("rejected", "the table refuses this"))
        self.assertFalse((record_of(output, A) or a)["paused"])

    def test_intake_error(self):
        a, b = waiting(), other_waiting()
        issue = {"id": 7000, "number": 70, "body": "not the form", "user": dict(OWNER),
                 "labels": []}
        output = tick([a, b], issues=[issue])
        self.assert_not_isolated(output)
        self.assertEqual([(i["issue"], i["decision"]) for i in output.report["controller"]["intake"]],
                         [(70, "invalid")])


class TestCollectorFailure(unittest.TestCase):
    def test_contribution_is_skipped_without_state_change_or_pause(self):
        errors = (collect.CollectError("GET /repos/x: HTTP 502"),
                  urllib.error.URLError("connection refused"), TimeoutError("timed out"))
        a = contribution(activity="agent_running", active_work=dict(WORK))
        b = other_waiting()
        pubs = [publication(a, "published")]
        baseline = tick([a, b], publications=pubs, verdict="passed")
        self.assertTrue(staged_for(baseline, A))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                output = tick([a, b], publications=pubs, verdict="passed",
                              head=branch_head(FORK_A, error))
                # Nothing at all for the skipped contribution, not even the
                # publication the same tick would have folded.
                self.assertEqual(staged_for(output, A), [])
                self.assertEqual(staged_for(output, B), staged_for(baseline, B))
                self.assertEqual(output.report["controller"]["alerts"], [])
                self.assertNotIn("processing_errors", output.report)
                skipped = output.report["skipped_contributions"]
                self.assertEqual([(s["contribution"], s["step"], s["error_class"])
                                  for s in skipped], [(A, "observation", type(error).__name__)])
                self.assertIn("no pause", skipped[0]["decision"])

    def test_collector_failures_never_count_as_processing_errors(self):
        """GitHub unreadable for every contribution: all are skipped, the
        tick still returns. One processing error beside them still pauses."""
        a, b = waiting(), other_waiting()

        def down(_api, _fork, _ref):
            raise collect.CollectError("HTTP 503")
        output = tick([a, b], head=down)
        self.assertEqual(output.staged, [])
        self.assertEqual({s["contribution"] for s in output.report["skipped_contributions"]}, {A, B})
        output = tick([a, b], head=branch_head(FORK_B, collect.CollectError("HTTP 503")), patches=[
            ("controller._actionable", only_for(A, REAL["_actionable"]))])
        self.assertEqual(kinds(output, A), [VERB])
        self.assertEqual(staged_for(output, B), [])

    def test_receipt_readback_failure_skips_the_contribution(self):
        a = waiting()

        class Down:
            def get(self, _path):
                raise collect.CollectError("HTTP 502")
        snapshot = reconcile.Snapshot({A: a}, {}, {}, {}, {})
        op = {"operation_id": "op-1", "kind": "dev_pr_create", "status": "planned",
              "contribution_id": A,
              "target": {"repository": FORK_A, "repository_id": 2, "expected_head": SHA_A},
              "proposed": {"marker": "m"}}
        snapshot.operations["op-1"] = op
        snapshot.contributions[A] = dict(a, active_operation_id="op-1")
        receipt = {"contribution_id": A, "operation_id": "op-1", "revision": 0,
                   "status": "published", "repository": FORK_A, "repository_id": 2,
                   "head_sha": SHA_A, "pr_number": 1}
        with contextlib.redirect_stderr(io.StringIO()):
            output = reconcile.tick(Down(), snapshot, policy=POLICY, now=T, publications=[],
                                    pressure=capacity.Pressure(ok=True), observe=False,
                                    development_prs=[receipt])
        self.assertEqual(staged_for(output, A), [])
        self.assertEqual([(s["contribution"], s["step"]) for s in
                          output.report["skipped_contributions"]], [(A, "development_pr_receipt")])


class TestTruncatedReadOnTheLiveHandoffRecord(unittest.TestCase):
    """The live handoff record observed through the real ``collect.Api``:
    a read that fails in transport or decoding skips it for the tick, with
    no pause and nothing written."""

    FIXTURES = Path(__file__).parent / "fixtures" / "live-handoff-r6"
    HEAD = "b29a8f49374004d650cf312e9c31848953e5ae60"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        live = json.loads((self.FIXTURES / "contribution.json").read_text())
        evidence = json.loads((self.FIXTURES / "evidence-ci.json").read_text())
        record, _event = compat.migrate_handoff_wait(live, evidence=evidence, policy=POLICY,
                                                     now=ISO)
        self.assertEqual(engine.state_key(record), "handoff.awaiting_submission")
        record["fork"]["head_ref"] = "riscv64-dev"
        self.cid = record["contribution_id"]
        for kind, value in (("contributions", record), ("evidence", evidence)):
            path = self.root / state.record_path(kind, value)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canonical_json(value))

    def tearDown(self):
        self.tmp.cleanup()

    def opener(self, failing=None, error=None, body=None):
        """GitHub for the fork: ``failing`` is the first path fragment whose
        read raises ``error`` or returns the raw ``body``."""
        head = self.HEAD

        def open_(req, timeout=None):
            url = req.full_url
            if failing and failing in url:
                return Response(body=body or b"", error=error)
            if "/branches/" in url:
                value = {"commit": {"sha": head, "commit": {"tree": {"sha": "f" * 40}},
                                    "parents": []}}
            elif "/actions/runs?" in url:
                value = {"workflow_runs": []}
            elif "/check-runs" in url:
                value = {"check_runs": []}
            elif url.endswith("/repos/riseproject-upstreaming/ciso8601"):
                value = {"id": 1467208233, "full_name": "riseproject-upstreaming/ciso8601",
                         "default_branch": "master", "fork": True}
            elif "/reviews" in url or "/comments" in url or "/statuses" in url:
                value = []
            else:
                raise AssertionError(f"unexpected read {url}")
            return Response(body=json.dumps(value).encode())
        return open_

    def run_tick(self, **failure):
        store, snapshot = reconcile.load_snapshot(self.root, POLICY)
        api = collect.Api(token="", opener=self.opener(**failure))
        with contextlib.redirect_stderr(io.StringIO()):
            output = reconcile.tick(api, snapshot, policy=POLICY, now=T, run_id="4242",
                                    publications=[], pressure=capacity.Pressure(ok=True),
                                    dispatcher_runs=[], queue_works={})
        for kind, record in output.staged:
            store.stage(kind, record)
        return output, store.write_staged()

    def test_baseline_reads_everything_and_processes_the_record(self):
        output, _written = self.run_tick()
        self.assertNotIn("skipped_contributions", output.report)
        self.assertNotIn("processing_errors", output.report)
        self.assertEqual([o["contribution"] for o in output.report["observation"]], [self.cid])

    def test_a_failed_read_skips_the_record_with_no_pause_and_no_write(self):
        before = {p: p.read_text() for p in self.root.rglob("*.json")}
        cases = {
            "IncompleteRead on the branch": dict(
                failing="/branches/riscv64",
                error=http.client.IncompleteRead(b'{"commit": {"sha": "b29a', 900)),
            "truncated JSON body of the reviews": dict(
                failing="/pulls/1/reviews", body=b'[{"id": 1, "user": {"id"'),
            "SSLEOFError on the CI runs": dict(
                failing="/actions/runs?",
                error=ssl.SSLEOFError(8, "EOF occurred in violation of protocol")),
            "RemoteDisconnected on the check runs": dict(
                failing="/check-runs",
                error=http.client.RemoteDisconnected("Remote end closed connection")),
        }
        for name, failure in cases.items():
            with self.subTest(name):
                output, written = self.run_tick(**failure)
                self.assertEqual(written, [])
                self.assertEqual(output.staged, [])
                self.assertEqual(output.report["controller"]["alerts"], [])
                self.assertNotIn("processing_errors", output.report)
                skipped = output.report["skipped_contributions"]
                self.assertEqual([(s["contribution"], s["step"], s["error_class"])
                                  for s in skipped], [(self.cid, "observation", "CollectError")])
                self.assertIn("no pause", skipped[0]["decision"])
                self.assertEqual({p: p.read_text() for p in self.root.rglob("*.json")}, before)


class TestRepeatedErrorAndResume(unittest.TestCase):
    def test_no_new_event_while_the_contribution_is_stopped(self):
        a, b = waiting(), other_waiting()
        first = tick([a, b], head=branch_head(FORK_A))
        paused = record_of(first, A)
        self.assertEqual(kinds(first, A), [VERB])
        for label, stopped in (("controller", paused),
                               ("human", dict(a, paused=True, paused_by="human", paused_at=NOW))):
            with self.subTest(stopped_by=label):
                again = tick([stopped, b], head=branch_head(FORK_A, Boom("another failure")))
                self.assertEqual(staged_for(again, A), [])
                alert = again.report["controller"]["alerts"][0]["alert"]
                self.assertIn("another failure", alert)
                self.assertIn("not paused again: contribution is paused", alert)
                self.assertEqual(again.report["processing_errors"][0]["decision"],
                                 "not paused again: contribution is paused")

    def test_terminal_contribution_is_not_paused(self):
        a = contribution(phase="closed", activity="idle", terminal_reason="abandoned")
        output = tick([a, other_waiting()], patches=[
            ("controller._actionable", only_for(A, REAL["_actionable"]))])
        self.assertEqual(staged_for(output, A), [])
        self.assertIn("contribution is terminal", output.report["processing_errors"][0]["decision"])

    def test_resume_bumps_the_revision_and_clears_the_flag(self):
        resume = comment("/upstreaming resume revision=0 reason=fixed", 12)
        for label, a in (("development", waiting()), ("handoff", awaiting_submission())):
            with self.subTest(state=label):
                paused = record_of(tick([a, other_waiting()], head=branch_head(FORK_A)), A)
                output = tick([paused, other_waiting()], comments=[resume], observe=False)
                resumed = record_of(output, A)
                self.assertEqual((resumed["paused"], resumed["paused_reason"],
                                  resumed["paused_by"], resumed["paused_at"]),
                                 (False, None, None, None))
                self.assertEqual(resumed["revision"], a["revision"] + 1)
                self.assertEqual(engine.state_key(resumed), engine.state_key(a))
                # The error gone, the next tick processes it again.
                later = tick([resumed, other_waiting()], verdict="pending")
                self.assertEqual(later.failures, [])


class TestErrorText(unittest.TestCase):
    SECRET = "ghp_" + "a1B2" * 9

    def failure(self, message):
        return isolation.ProcessingError(A, "observation", Boom(message))

    def test_message_is_bounded_and_carries_no_secret_or_traceback(self):
        message = (f"token {self.SECRET} leaked\n  File \"x.py\", line 3\n\ttab {chr(0x2014)} dash "
                   + "y" * 5000)
        description = isolation.describe(self.failure(message), "4242")
        self.assertLessEqual(len(description["message"]), isolation.MAX_MESSAGE)
        self.assertNotIn(self.SECRET, description["message"])
        self.assertIn("[redacted]", description["message"])
        self.assertNotIn("\n", description["message"])
        self.assertNotIn(chr(0x2014), description["message"])
        reason = isolation.pause_reason(description)
        self.assertLessEqual(len(reason), 2000)
        self.assertEqual(state.find_secrets(reason), [])

    def test_a_secret_in_the_error_is_never_staged(self):
        a, b = waiting(), other_waiting()
        output = tick([a, b], head=branch_head(FORK_A, Boom(f"Bearer {self.SECRET}")))
        with tempfile.TemporaryDirectory() as tmp:
            store = state.StateStore(Path(tmp), POLICY)
            for kind, record in output.staged:
                store.stage(kind, record)
            written = store.write_staged()
            text = "".join((Path(tmp) / p).read_text() for p in written)
        self.assertNotIn(self.SECRET, text)
        self.assertNotIn(self.SECRET, json.dumps(output.report, default=str))
        self.assertIn("[redacted]", record_of(output, A)["paused_reason"])

    def test_ledger_entry_states_the_error(self):
        a = waiting()
        message = f"bad `value` <!-- hidden --> {self.SECRET} " + "z" * 900
        output = tick([a, other_waiting()], head=branch_head(FORK_A, Boom(message)))
        paused = record_of(output, A)
        event = dict([r for kind, r in staged_for(output, A) if kind == "events"][0], sequence=7)
        body = status_ledger.render(event, paused, "https://github.com/run/1")
        self.assertIn("**Upstreaming r0 · development/paused**", body)
        self.assertIn("unexpected error while it processed this contribution", body)
        self.assertIn("> Boom: bad 'value'", body)
        self.assertIn("Step: observation. Run: 4242.", body)
        self.assertIn('/upstreaming resume revision=0 reason="..."', body)
        self.assertNotIn(self.SECRET, body)
        self.assertNotIn("Traceback", body)
        self.assertNotIn("hidden -->", body)
        self.assertEqual(body.count("<!--"), 1)       # the ledger marker only
        self.assertTrue(body.endswith(f"<!-- upstreaming-ledger:{A}:7 -->"))
        self.assertLess(len(body), 2000)
        self.assertNotIn(chr(0x2014), body)

    def test_ledger_entry_for_a_handoff_record_offers_the_table_commands(self):
        a = awaiting_submission()
        output = tick([a, other_waiting()], head=branch_head(FORK_A))
        event = dict([r for kind, r in staged_for(output, A) if kind == "events"][0], sequence=7)
        body = status_ledger.render(event, record_of(output, A), "https://github.com/run/1")
        self.assertIn("**Upstreaming r0 · handoff/paused**", body)
        self.assertIn('/upstreaming resume revision=0 reason="..."', body)
        self.assertNotIn("/upstreaming submit-upstream", body)


class TestReconcileCommand(unittest.TestCase):
    """The workflow's entry point: what is written and how it exits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = contribution(activity="fix_needed", dossier_evidence_id="dossier-1",
                              scope_paths=["a"], scope_coverage_complete=True)
        self.b = other(activity="fix_needed", dossier_evidence_id="dossier-1", scope_paths=["a"],
                       scope_coverage_complete=True)
        for record in (self.a, self.b):
            path = self.root / state.record_path("contributions", record)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canonical_json(record))

    def tearDown(self):
        self.tmp.cleanup()

    def files(self):
        return {p.relative_to(self.root).as_posix(): p.read_text()
                for p in sorted(self.root.rglob("*.json")) if p.name != "report.json"}

    def run_reconcile(self, patches=()):
        report = self.root / "report.json"
        args = SimpleNamespace(root=str(self.root), report=str(report), publications=None,
                               provisions=None, development_prs=None, since=None, offline=True,
                               gh_aw=None)
        stderr, self.stdout = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stderr(stderr))
            stack.enter_context(contextlib.redirect_stdout(self.stdout))
            stack.enter_context(mock.patch("reconcile.load_policy", return_value=POLICY))
            stack.enter_context(mock.patch.dict("os.environ", {"GITHUB_RUN_ID": "4242"}))
            stack.enter_context(mock.patch(
                "capacity.Pressure", side_effect=lambda **_k: SimpleNamespace(ok=True, reason="")))
            stack.enter_context(mock.patch("controller.capacity.admit_expensive",
                                           return_value=(True, None)))
            for target, replacement in patches:
                stack.enter_context(mock.patch(target, side_effect=replacement))
            try:
                result = reconcile.cmd_reconcile(args)
            except Exception as error:      # an error outside any contribution
                result = error
        return result, (json.loads(report.read_text()) if report.exists() else None), stderr.getvalue()

    def test_only_the_pause_of_the_errored_contribution_is_written(self):
        before = self.files()
        result, report, stderr = self.run_reconcile([
            ("controller._actionable", only_for(A, REAL["_actionable"]))])
        self.assertEqual(result, 0)
        self.assertEqual(sorted(report["written"]), [
            ".state/contributions/example--proj--12.json",
            ".state/contributions/other--lib--13.json",
            ".state/events/example--proj--12/00000000.json",
            ".state/events/other--lib--13/00000000.json"])
        after = self.files()
        a = json.loads(after[".state/contributions/example--proj--12.json"])
        self.assertEqual(a, dict(self.a, paused=True, paused_by="controller",
                                 paused_at=a["paused_at"], paused_reason=a["paused_reason"],
                                 updated_at=a["updated_at"]))
        self.assertEqual(json.loads(after[".state/events/example--proj--12/00000000.json"])["kind"],
                         VERB)
        b = json.loads(after[".state/contributions/other--lib--13.json"])
        self.assertEqual(b["activity"], "agent_running")
        self.assertEqual([e["contribution_id"] for e in report["requested_effects"]], [B])
        self.assertIn("run 4242", a["paused_reason"])
        # The traceback is in the workflow log, not in state or the report.
        self.assertIn("Traceback", stderr)
        self.assertIn("contribution not processed in this tick", stderr)
        self.assertNotIn("Traceback", json.dumps(report) + "".join(after.values()))
        self.assertEqual(set(after) - set(before),
                         {".state/events/example--proj--12/00000000.json",
                          ".state/events/other--lib--13/00000000.json"})
        # The run succeeds, and the error is on the workflow run as an
        # annotation.
        self.assertEqual(self.stdout.getvalue().splitlines(), [
            "::error::processing error for example--proj/12 in work_selection: Boom: "
            "injected failure; paused by the controller"])
        # The next tick, with the error still there, writes nothing for it.
        result, report, _stderr = self.run_reconcile([
            ("controller._actionable", only_for(A, REAL["_actionable"]))])
        self.assertEqual(result, 0)
        self.assertEqual([p for p in report["written"] if "example--proj--12" in p], [])
        self.assertEqual(len(report["controller"]["alerts"]), 1)
        self.assertEqual(len(self.stdout.getvalue().splitlines()), 1)
        self.assertIn("not paused again", self.stdout.getvalue())

    def test_two_errors_exit_non_zero_and_write_nothing(self):
        before = self.files()

        def fail(_record):
            raise Boom("injected failure")
        result, report, stderr = self.run_reconcile([("controller._actionable", fail)])
        self.assertEqual(result, 1)
        self.assertIsNone(report)
        self.assertIn("reconciliation stopped: processing failed for more than one contribution",
                      stderr)
        self.assertEqual(self.files(), before)

    def test_store_validation_and_commit_errors_stop_the_tick(self):
        before = self.files()
        # The staging boundary refuses a record: nothing at all is written.
        result, report, _stderr = self.run_reconcile([
            ("state.StateStore.stage", lambda *_a, **_k: (_ for _ in ()).throw(
                state.StateError("refused")))])
        self.assertIsInstance(result, state.StateError)
        self.assertIsNone(report)
        self.assertEqual(self.files(), before)
        # A committed record that is not valid state: the load refuses it.
        path = self.root / state.record_path("contributions", self.b)
        path.write_text(canonical_json(dict(self.b, phase="nowhere")))
        result, report, stderr = self.run_reconcile()
        self.assertEqual((result, report), (1, None))
        self.assertIn("refused", stderr)

    def test_policy_error_stops_the_tick(self):
        before = self.files()
        result, report, _stderr = self.run_reconcile([
            ("reconcile.load_policy", lambda: (_ for _ in ()).throw(Boom("no policy")))])
        self.assertIsInstance(result, Boom)
        self.assertEqual((report, self.files()), (None, before))

    def test_record_migration_error_stops_the_tick(self):
        """A record awaiting the handoff migration is not ordinary state yet:
        an error while migrating it is a pipeline failure, and no table row
        could pause it."""
        fixtures = Path(__file__).parent / "fixtures" / "live-handoff-r6"
        record = json.loads((fixtures / "contribution.json").read_text())
        (self.root / state.record_path("contributions", record)).write_text(
            canonical_json(record))
        evidence = json.loads((fixtures / "evidence-ci.json").read_text())
        path = self.root / state.record_path("evidence", evidence)
        path.parent.mkdir(parents=True)
        path.write_text(canonical_json(evidence))
        before = self.files()
        for error, expected in ((lifecycle.TransitionError("no reviewed migration"), 1),
                                (Boom("injected failure"), Boom)):
            with self.subTest(error=type(error).__name__):
                result, report, stderr = self.run_reconcile([
                    ("compat.migrate_handoff_wait",
                     lambda *_a, _e=error, **_k: (_ for _ in ()).throw(_e))])
                if expected == 1:
                    self.assertEqual(result, 1)
                    self.assertIn("record migration refused", stderr)
                else:
                    self.assertIsInstance(result, Boom)
                self.assertEqual((report, self.files()), (None, before))


class TestProcessingErrorAnnotations(unittest.TestCase):
    def test_one_bounded_line_per_processing_error(self):
        report = {"processing_errors": [
            {"contribution": A, "step": "observation", "error_class": "Boom",
             "message": "100% wrong\n::warning::injected\r" + "x" * 2000,
             "decision": "paused by the controller"},
            {"contribution": B, "step": "command", "error_class": "KeyError",
             "message": "'k'", "decision": "not paused again: contribution is paused"}],
            "skipped_contributions": [{"contribution": "c/1"}]}
        lines = reconcile.processing_error_annotations(report)
        self.assertEqual(len(lines), 2)
        first = lines[0]
        self.assertTrue(first.startswith("::error::processing error for example--proj/12 in "
                                         "observation: Boom: 100%25 wrong ::warning::injected"))
        self.assertNotIn("\n", first)
        self.assertNotIn("\r", first)
        self.assertLessEqual(len(first), len("::error::") + reconcile.MAX_ANNOTATION + 200)
        self.assertEqual(lines[1], "::error::processing error for other--lib/13 in command: "
                                   "KeyError: 'k'; not paused again: contribution is paused")

    def test_a_tick_without_processing_errors_has_no_annotation(self):
        self.assertEqual(reconcile.processing_error_annotations(
            {"skipped_contributions": [{"contribution": A}]}), [])


class TestObserveStandsAlone(unittest.TestCase):
    def test_observe_attributes_an_error_to_the_contribution(self):
        a, b = waiting(), other_waiting()
        with mock.patch("observe_ci.collect.branch_head", side_effect=branch_head(FORK_B)), \
                self.assertRaises(isolation.ProcessingError) as raised:
            observe_ci.observe(object(), contributions={A: a, B: b}, operations={}, contracts={},
                               policy=POLICY, now=ISO, skip=frozenset({A}))
        self.assertEqual((raised.exception.contribution_id, raised.exception.step),
                         (B, "observation"))
        self.assertIsInstance(raised.exception.error, Boom)

if __name__ == "__main__":
    unittest.main()
