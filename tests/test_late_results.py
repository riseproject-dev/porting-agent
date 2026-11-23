# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A worker Result that matches no request of the record.

The table's ``record_late_effect`` applies in every state: a Result that does
not match the current generation, revision, and requested Work is recorded and
never folded. ``park`` and ``pause`` clear the requested Work without a
revision bump (``abandon`` then closes a parked contribution), so the Result of the Work they stopped
carries the record's own revision. It must be recorded, change nothing, and
never stop the tick.

A stop also clears the pending publication: the Result held for publication is
invalidated, so its publication Result is recorded like any other late Result,
no publish retry is launched for a stopped record, and the stop never keeps a
later resume from applying.
"""

from __future__ import annotations

import contextlib
import copy
import io
import itertools
import unittest
from datetime import datetime, timedelta
from unittest import mock

import capacity
import collect
import commands
import compat
import engine
import lifecycle
import reconcile
import schemas
from tests.helpers import NOW, OWNER, SHA_A, SHA_B, apply_command, contribution, policy

T = datetime.fromisoformat("2026-11-23T09:00:00+00:00")
POLICY = policy()
WRITES = policy(destination_writes=True)


def _dossier(decision):
    return {"proposal": {"decision": decision, "rationale": "r",
                         "admission_class": "configuration_integration", "allowed_paths": ["a"]}}


# One Result of every outcome status the publisher and the queue produce.
RESULTS = {
    "published": {"status": "published"},
    "uncertain": {"status": "uncertain"},
    "proposal": {"status": "proposal"},
    "no_change": {"status": "no_change"},
    "rejected_validation": {"status": "rejected", "failure_class": "validation", "reason": "x"},
    "rejected_stale": {"status": "rejected", "failure_class": "stale", "reason": "x"},
    "infra_error": {"status": "infra_error", "failure_class": "infrastructure", "reason": "x"},
    "worker_blocked": {"status": "worker_blocked", "reason": "x", "proposal": {}},
    "investigation_without_dossier": {"status": "investigation"},
    "investigation_develop": {"status": "investigation", "dossier": _dossier("develop")},
    "investigation_park": {"status": "investigation", "dossier": _dossier("park")},
    "investigation_close": {"status": "investigation", "dossier": _dossier("close")},
}

WORK = {"dedupe_key": "example--proj/12@0:development_repair", "task": "development_repair",
        "revision": 0, "attempt": 1, "requested_at": NOW,
        "deadline_at": "2026-11-24T09:00:00+00:00", "dispatched_run_id": None, "reason_ids": []}
UPSTREAM_PR = {"id": 9, "number": 9, "url": "https://github.com/example/proj/pull/9",
               "head_repository_id": 2, "head_ref": "riscv64", "head_sha": SHA_A,
               "base_ref": "main", "registered_by": OWNER}
FEEDBACK = {"review_ids": [77], "comment_ids": [], "conversation_comment_ids": [],
            "reviews": [{"id": 77, "body": "x"}], "comments": [], "conversation_comments": [],
            "inquiry_comment_ids": [], "inquiry_conversation_comment_ids": [],
            "inquiry_comments": [], "inquiry_conversation_comments": []}


def publication(record, name, artifact_id=999):
    return dict(RESULTS[name],
                task={"contribution_id": record["contribution_id"], "revision": record["revision"]},
                worker_run_id="555", publication_artifact_id=artifact_id, operation=None,
                artifact=None)


def ci_snapshot(head, verdict):
    status = "in_progress" if verdict == "pending" else "completed"
    conclusion = {"passed": "success", "failed": "failure", "pending": None}[verdict]
    run = {"id": 777, "head_sha": head, "run_attempt": 1, "status": status,
           "conclusion": conclusion, "workflow_id": 1, "event": "pull_request",
           "path": ".github/workflows/lint.yml"}
    jobs = [{"id": 1, "run_id": 777, "name": "Build - ubuntu-24.04-riscv (riscv64)",
             "status": status, "conclusion": conclusion, "started_at": "x",
             "labels": ["ubuntu-24.04-riscv"]}]
    return collect.CiSnapshot(repository={"id": 2, "full_name": "riseproject-upstreaming/proj"},
                              head_sha=head, runs=[run], jobs_by_run={777: jobs},
                              artifacts_by_run={})


def tick(record, publications=(), *, observe=False, verdict="pending", head=SHA_A, feedback=None,
         capacity_ok=True, policy=POLICY, now=T, comments=()):
    snapshot = reconcile.Snapshot({"c": copy.deepcopy(record)}, {}, {}, {}, {})
    with mock.patch("observe_ci.collect.branch_head", return_value={"head": head}), \
            mock.patch("observe_ci.collect.development_review_feedback", return_value=feedback), \
            mock.patch("observe_ci.collect.ci_snapshot", return_value=ci_snapshot(head, verdict)), \
            contextlib.redirect_stderr(io.StringIO()):
        return reconcile.tick(object() if observe else None, snapshot, policy=policy, now=now,
                              publications=list(publications), comments=list(comments),
                              pressure=capacity.Pressure(ok=capacity_ok, reason="none"),
                              observe=observe,
                              dispatcher_runs=[], queue_works={})


def final(output, record):
    staged = [r for kind, r in output.staged if kind == "contributions"]
    return staged[-1] if staged else record


def events(output):
    return [e for kind, e in output.staged if kind == "events"]


def running(phase):
    """A contribution whose worker runs."""
    task = "investigation" if phase == "investigation" else "development_repair"
    record = contribution(phase=phase, activity="agent_running", dossier_evidence_id="dossier-1",
                          scope_paths=["a"], scope_coverage_complete=True)
    if phase == "investigation":
        record["fork"] = None
    if phase == "upstream_review":
        record["upstream_pr"] = copy.deepcopy(UPSTREAM_PR)
    record["active_work"] = dict(WORK, task=task, dedupe_key=f"example--proj/12@0:{task}")
    schemas.validate("contribution", record)
    return record


def stopped(phase, verb):
    """``running(phase)`` after a human stopped it with ``verb``, or ``None``
    where the table has no such stop."""
    record = running(phase)
    if verb == "abandon":
        # Only a parked contribution is abandoned.
        refused = apply_command(record, commands.parse('/upstreaming abandon reason="stop"'))
        assert not refused.accepted and "does not apply" in refused.reason, refused.reason
        parked = stopped(phase, "park")
        if parked is None:
            return None
        out = apply_command(parked, commands.parse('/upstreaming abandon reason="stop"'))
        assert out.accepted, out.reason
        return out.record
    out = apply_command(record, commands.parse(f'/upstreaming {verb} reason="stop"'))
    if (phase, verb) == ("upstream_review", "park"):
        # Nothing parks from upstream review but its human wait.
        assert not out.accepted and "does not apply" in out.reason, out.reason
        return None
    assert out.accepted, out.reason
    assert out.record["revision"] == record["revision"] and out.record["active_work"] is None
    return out.record


class TestResultAfterTheWorkWasStopped(unittest.TestCase):
    def assert_recorded_only(self, record, name):
        pub = publication(record, name)
        # Without capacity the tick requests no new Work, so the record shows
        # exactly what the Result did to it.
        output = tick(record, [pub], capacity_ok=False)
        after = final(output, record)
        # Only the replay fence changes.
        self.assertEqual(dict(after, processed_publications=[]),
                         dict(record, processed_publications=[]))
        self.assertIn(999, after["processed_publications"])
        late = events(output)
        self.assertEqual([e["kind"] for e in late], ["record_late_effect"])
        self.assertEqual((late[0]["from"], late[0]["to"], late[0]["revision_bumped"]),
                         ({}, {}, False))
        self.assertEqual(late[0]["cause"], {"type": "result", "id": "555"})
        state = engine.state_key(record)
        self.assertEqual(late[0]["details"],
                         {"verb": "record_late_effect", "from_state": state, "to_state": state,
                          "result_status": pub["status"], "task_revision": record["revision"]})
        self.assertEqual([k for k, _ in output.staged if k in ("operations", "evidence")], [])
        self.assertEqual(output.report["requested_effects"], [])
        self.assertTrue(output.report["publications"][0]["ignored"].startswith("late Result"))
        # The same artifact on the next tick: nothing is recorded again.
        again = tick(after, [pub], capacity_ok=False)
        self.assertEqual(events(again), [])
        self.assertEqual(final(again, after), after)

    def test_park_abandon_and_pause_during_running_work(self):
        # A pause from an `agent_running` state leads to the state in which
        # the Work is due again.
        expected = {"park": "parked.idle", "abandon": "closed.idle",
                    "pause": {"investigation": "investigation.idle",
                              "development": "development.fix_needed",
                              "upstream_review": "upstream_review.fix_needed"}}
        for phase, verb in itertools.product(("investigation", "development", "upstream_review"),
                                             ("park", "abandon", "pause")):
            record = stopped(phase, verb)
            if record is None:
                continue
            target = expected[verb]
            self.assertEqual(engine.state_key(record),
                             target[phase] if isinstance(target, dict) else target)
            for name in RESULTS:
                with self.subTest(phase=phase, verb=verb, result=name):
                    self.assert_recorded_only(record, name)

    def test_states_that_request_no_work(self):
        human = {"kind": "task_without_outcome", "requested_at": NOW, "reminded_at": None,
                 "escalated_at": None}
        records = {
            "queued.idle": contribution(phase="queued", activity="idle", fork=None),
            "queued.waiting_human": contribution(phase="queued", activity="waiting_human",
                                                 fork=None, pending_human_action=human),
            "release_wait.idle": contribution(phase="release_wait", activity="idle",
                                              upstream_pr=UPSTREAM_PR),
            "release_wait.waiting_human": contribution(
                phase="release_wait", activity="waiting_human", upstream_pr=UPSTREAM_PR,
                pending_human_action=human),
            "released.idle": contribution(phase="released", activity="idle",
                                          upstream_pr=UPSTREAM_PR),
            "parked.idle": contribution(phase="parked", activity="idle", parked_reason="x",
                                        previous_phase="development"),
            "parked.waiting_human": contribution(
                phase="parked", activity="waiting_human", parked_reason="x",
                previous_phase="development", pending_human_action=human),
            "closed.idle": contribution(phase="closed", activity="idle"),
        }
        for state, record in records.items():
            self.assertEqual(engine.state_key(record), state)
            # Stale Work left on such a record does not make the Result match.
            for work in (None, WORK):
                rec = dict(record, active_work=copy.deepcopy(work))
                for name in RESULTS:
                    with self.subTest(state=state, work=bool(work), result=name):
                        if state == "queued.idle" and not work:
                            self.assert_recorded_before_new_work(rec, name)
                        else:
                            self.assert_recorded_only(rec, name)

    def test_working_state_without_matching_work(self):
        for phase, activity in (("development", "fix_needed"), ("development", "waiting_ci"),
                                ("development", "idle"), ("upstream_review", "fix_needed")):
            record = contribution(phase=phase, activity=activity, dossier_evidence_id="dossier-1")
            if phase == "upstream_review":
                record["upstream_pr"] = copy.deepcopy(UPSTREAM_PR)
            other = dict(record, active_work=dict(WORK, revision=7))
            for rec, name in itertools.product((record, other), RESULTS):
                with self.subTest(state=engine.state_key(rec), work=bool(rec["active_work"]),
                                  result=name):
                    self.assert_recorded_only(rec, name)

        idle = contribution(phase="investigation", activity="idle", fork=None)
        for name in RESULTS:
            with self.subTest(state="investigation.idle", result=name):
                self.assert_recorded_before_new_work(idle, name)

    def assert_recorded_before_new_work(self, record, name):
        """Where an investigation is due, the same tick requests it after the
        Result is recorded. Nothing of the Result reaches the record."""
        output = tick(record, [publication(record, name)])
        after = final(output, record)
        self.assertEqual([e["kind"] for e in events(output)][:1], ["record_late_effect"])
        self.assertEqual(engine.state_key(after), "investigation.agent_running")
        self.assertEqual((after["active_work"]["task"], after["revision"]),
                         ("investigation", record["revision"]))
        self.assertEqual(
            (after["dossier_evidence_id"], after["proposal_artifact"],
             after["pending_publication"], after["publication_operation_id"],
             after["pending_human_action"], after["budget"], after["processed_publications"]),
            (None, None, None, None, None, record["budget"], [999]))
        self.assertEqual([k for k, _ in output.staged if k in ("operations", "evidence")], [])

    def test_requested_work_and_trusted_publication_still_fold(self):
        record = running("development")
        output = tick(record, [publication(record, "published")])
        self.assertEqual(events(output), [])
        self.assertEqual((final(output, record)["activity"], final(output, record)["active_work"]),
                         ("waiting_ci", None))
        # A publish-only retry of the same worker run answers the pending publication.
        pending = {"worker_run_id": "555", "attempts": 1, "last_reason": "x",
                   "next_attempt_at": "2026-11-24T09:00:00+00:00", "exhausted": False}
        retry = contribution(activity="waiting_capacity", pending_publication=pending)
        after = final(tick(retry, [publication(retry, "published")]), retry)
        self.assertEqual((after["activity"], after["pending_publication"]), ("waiting_ci", None))
        # Another worker run does not.
        other = dict(publication(retry, "published"), worker_run_id="556")
        output = tick(retry, [other])
        self.assertEqual([e["kind"] for e in events(output)], ["record_late_effect"])
        self.assertEqual(final(output, retry)["pending_publication"], pending)

    def test_event_is_the_same_for_a_table_state_and_a_legacy_state(self):
        details = {"result_status": "published", "task_revision": 0}
        for record in (contribution(phase="parked", activity="idle", parked_reason="x",
                                    previous_phase="development"),
                       contribution(phase="development", activity="idle")):
            state = engine.state_key(record)
            self.assertEqual(state in lifecycle.LIFECYCLE, state == "parked.idle")
            event = compat.record_late_effect(record, policy=POLICY, now=NOW, cause_id="555",
                                              details=details)
            schemas.validate("event", event)
            self.assertEqual((event["kind"], event["cause"], event["from"], event["to"]),
                             ("record_late_effect", {"type": "result", "id": "555"}, {}, {}))
            self.assertEqual(event["details"], {"verb": "record_late_effect", "from_state": state,
                                                "to_state": state, **details})


ARTIFACT = {"work_id": "w", "claim_id": "c", "artifact_id": 5, "name": "n",
            "digest": "sha256:" + "0" * 64, "run_id": "555", "result_commit": "d" * 40}
PENDING = {"worker_run_id": "555", "attempts": 1, "last_reason": "x", "next_attempt_at": NOW,
           "exhausted": False}
# What the trusted publisher can return for a launched publication.
PUBLISHER_RESULTS = ("published", "uncertain", "proposal", "no_change", "rejected_validation",
                     "rejected_stale", "infra_error", "worker_blocked")
RESUME = {"park": "resume", "pause": "resume"}


def command(record, body):
    return apply_command(record, commands.parse(body))


class TestStopWithAPendingPublication(unittest.TestCase):
    def ticks(self, record, publications, count):
        """``count`` reconciliations of ``record``, 40 minutes apart, with the
        same publication artifacts collected every time."""
        outputs = []
        for index in range(count):
            output = tick(record, copy.deepcopy(publications), observe=True, policy=WRITES,
                          now=T + timedelta(minutes=40 * index))
            record = final(output, record)
            outputs.append(output)
        return record, outputs

    def pending(self, kind):
        """A development record whose verified Result waits for publication:
        a held proposal, or a publish-only retry after an infrastructure error."""
        record = running("development")
        result = dict(publication(record, "proposal" if kind == "proposal" else "infra_error"),
                      artifact=dict(ARTIFACT))
        record, outputs = self.ticks(record, [result], 2)
        self.assertEqual(record["pending_publication"]["worker_run_id"], "555")
        self.assertIsNone(record["active_work"])
        self.assertEqual((record["proposal_artifact"] or {}).get("outcome"),
                         "proposal" if kind == "proposal" else None)
        launched = [o.report["proposal_publications"] + o.report["publication_retries"]
                    for o in outputs]
        self.assertTrue(any(launched), "the publication is launched while the record runs")
        return record, result

    def test_stop_clears_it_and_every_publisher_result_is_recorded_once(self):
        for kind, verb in itertools.product(("proposal", "retry"), RESUME):
            pending, first = self.pending(kind)
            out = command(pending, f'/upstreaming {verb} reason="stop"')
            self.assertTrue(out.accepted, out.reason)
            stopped_record = out.record
            self.assertIsNone(stopped_record["pending_publication"])
            # A held proposal is cleared with it; nothing is left to publish.
            self.assertIsNone(stopped_record["proposal_artifact"])
            self.assertEqual(stopped_record["revision"], pending["revision"])
            self.assertTrue(compat.stopped(stopped_record))
            for name in PUBLISHER_RESULTS:
                with self.subTest(pending=kind, verb=verb, result=name):
                    result = dict(publication(pending, name, artifact_id=1000),
                                  artifact=dict(ARTIFACT))
                    record, outputs = self.ticks(stopped_record, [first, result], 4)
                    late = [e for o in outputs for e in events(o)
                            if e["kind"] == "record_late_effect"]
                    self.assertEqual(len(late), 1)
                    self.assertEqual(late[0]["details"]["result_status"], result["status"])
                    self.assertEqual([e["kind"] for e in events(outputs[0])
                                      if e["kind"] != "activity"], ["record_late_effect"])
                    for output in outputs:
                        self.assertEqual(output.report["publication_retries"], [])
                        self.assertEqual(output.report["proposal_publications"], [])
                        self.assertEqual(output.report["requested_effects"], [])
                        self.assertEqual([k for k, _ in output.staged
                                          if k in ("operations", "evidence")], [])
                    self.assertIsNone(record["pending_publication"])
                    self.assertIsNone(record["active_work"])
                    self.assertTrue(compat.stopped(record))
                    self.assertEqual((record["revision"], record["budget"]),
                                     (pending["revision"], pending["budget"]))
                    # The stop no longer keeps the contribution from continuing.
                    resumed = command(record, f"/upstreaming {RESUME[verb]} "
                                              f"revision={record['revision']} reason=go")
                    self.assertTrue(resumed.accepted, resumed.reason)
                    self.assertEqual(resumed.record["revision"], record["revision"] + 1)
                    self.assertFalse(compat.stopped(resumed.record))
                    if verb == "park":
                        self.assertEqual(engine.state_key(resumed.record), "queued.idle")
                        self.assertEqual(resumed.record["generation"], record["generation"] + 1)

    def test_nothing_held_is_published_after_a_stop(self):
        """A proposal that is held and not published does not survive a stop:
        after the resume no publication is launched for it, and its
        publication Result is late. A published proposal is kept."""
        held = dict(ARTIFACT, result_commit=None, outcome="proposal")
        for verb in ("pause",):
            with self.subTest(verb=verb):
                record = contribution(phase="development", activity="waiting_capacity",
                                      proposal_artifact=dict(held))
                out = command(record, f'/upstreaming {verb} reason="stop"')
                self.assertIsNone(out.record["proposal_artifact"])
                resumed = command(out.record, "/upstreaming resume revision=0 reason=go").record
                self.assertEqual((resumed["proposal_artifact"], resumed["pending_publication"],
                                  resumed["activity"]), (None, None, "idle"))
                result = dict(publication(record, "published", artifact_id=1000),
                              artifact=dict(ARTIFACT))
                after, outputs = self.ticks(resumed, [result], 3)
                for output in outputs:
                    self.assertEqual(output.report["proposal_publications"], [])
                    self.assertEqual(output.report["publication_retries"], [])
                self.assertEqual([e["kind"] for o in outputs for e in events(o)
                                  if e["kind"] == "record_late_effect"], ["record_late_effect"])
                self.assertIsNone(after["proposal_artifact"])
        # A record that was stopped before stops cleared the proposal: the
        # resume clears it.
        old = contribution(phase="development", activity="waiting_capacity", paused=True,
                           proposal_artifact=dict(held))
        self.assertIsNone(command(old, "/upstreaming resume revision=0 reason=go")
                          .record["proposal_artifact"])
        parked = compat.park(contribution(phase="development", activity="waiting_capacity",
                                          proposal_artifact=dict(held)), "stop")
        self.assertIsNone(parked["proposal_artifact"])
        published = dict(held, outcome="published")
        kept = command(contribution(phase="development", activity="waiting_ci",
                                    proposal_artifact=dict(published)),
                       '/upstreaming pause reason="stop"').record
        self.assertEqual(kept["proposal_artifact"], published)

    def test_no_publish_retry_while_paused(self):
        for kind in ("proposal", "retry"):
            with self.subTest(pending=kind):
                # A record that was stopped before the stop cleared the publication.
                pending, first = self.pending(kind)
                record = dict(pending, paused=True, paused_at=NOW, paused_by="human")
                record, outputs = self.ticks(record, [first], 6)
                for output in outputs:
                    self.assertEqual(output.report["publication_retries"], [])
                    self.assertEqual(output.report["proposal_publications"], [])
                self.assertEqual(record["pending_publication"], pending["pending_publication"])
        # The same record, not stopped, is retried.
        pending, first = self.pending("retry")
        _record, outputs = self.ticks(pending, [first], 6)
        self.assertTrue(any(o.report["publication_retries"] for o in outputs))

    def test_legacy_stop_paths_outside_commands(self):
        record = contribution(pending_publication=dict(PENDING))
        self.assertIsNone(compat.park(copy.deepcopy(record), "x")["pending_publication"])
        for body in ('/upstreaming park reason="x"', "/upstreaming pause"):
            for activity in ("agent_running", "waiting_capacity", "waiting_ci", "waiting_human"):
                with self.subTest(command=body, activity=activity):
                    out = command(dict(record, activity=activity), body)
                    self.assertIsNone(out.record["pending_publication"])
                    self.assertNotIn(out.record["activity"], ("agent_running", "waiting_capacity"))
                    self.assertIsNone(compat.state_problem(out.record))

    def test_recovered_worker_blocker_is_recorded_once(self):
        # The rejected proposal of run 555 is recorded; its blocker report for
        # an older revision is collected on every tick.
        record = contribution(activity="fix_needed", revision=3, dossier_evidence_id="dossier-1",
                              proposal_artifact=dict(ARTIFACT, outcome="rejected"))
        blocker = dict(publication(record, "worker_blocked"),
                       task={"contribution_id": record["contribution_id"], "revision": 2})
        kinds = []
        for index in range(4):
            output = tick(record, [copy.deepcopy(blocker)], capacity_ok=False,
                          now=T + timedelta(minutes=10 * index))
            record = final(output, record)
            kinds.append([e["kind"] for e in events(output)])
            self.assertEqual([k for k, _ in output.staged if k == "evidence"], [])
        self.assertEqual(kinds, [["record_late_effect"], [], [], []])
        self.assertEqual((engine.state_key(record), record["revision"],
                          record["processed_publications"]),
                         ("development.fix_needed", 3, [999]))

    def test_work_state_without_recorded_work_folds_its_own_revision_only(self):
        for phase in ("development", "upstream_review"):
            record = contribution(phase=phase, activity="agent_running", revision=3,
                                  dossier_evidence_id="dossier-1")
            if phase == "upstream_review":
                record["upstream_pr"] = copy.deepcopy(UPSTREAM_PR)
            for name in RESULTS:
                with self.subTest(phase=phase, result=name):
                    older = dict(publication(record, name),
                                 task={"contribution_id": record["contribution_id"],
                                       "revision": 2})
                    output = tick(record, [older], capacity_ok=False)
                    self.assertEqual([e["kind"] for e in events(output)], ["record_late_effect"])
                    self.assertEqual(dict(final(output, record), processed_publications=[]),
                                     dict(record, processed_publications=[]))
            output = tick(record, [publication(record, "published")], capacity_ok=False)
            self.assertEqual([e["kind"] for e in events(output)], [])
            self.assertEqual(final(output, record)["activity"], "waiting_ci")


class TestNoReachableRecordStopsTheTick(unittest.TestCase):
    """Every admitted state, with and without a stop flag and requested Work,
    against every Result and every CI observation: the tick always returns
    and leaves the record in an admitted state."""

    def records(self):
        states = sorted(set(lifecycle.LIFECYCLE) | compat.LEGACY_STATES)
        for state, flag, work in itertools.product(states, (None, "paused"), (False, True)):
            phase, activity = state.split(".")
            record = contribution(phase=phase, activity=activity, dossier_evidence_id="dossier-1",
                                  scope_paths=["a"], scope_coverage_complete=True)
            if phase in ("queued", "investigation", "provisioning"):
                record["fork"] = None
            if phase in ("upstream_review", "release_wait", "released"):
                record["upstream_pr"] = copy.deepcopy(UPSTREAM_PR)
            if flag == "paused":
                record.update(paused=True, paused_at=NOW, paused_by="human")
            if work:
                record["active_work"] = copy.deepcopy(WORK)
            if activity == "waiting_human" and not flag:
                record["pending_human_action"] = {
                    "kind": "task_without_outcome", "requested_at": NOW, "reminded_at": None,
                    "escalated_at": None}
            if phase == "parked":
                record.update(parked_reason="x", previous_phase="development")
            if schemas.errors("contribution", record) or compat.state_problem(record):
                continue                    # not a record the controller admits
            yield f"{state}|{flag}|work={work}", record

    def test_every_result_and_observation(self):
        observations = list(itertools.product(("passed", "failed", "pending"), (SHA_A, SHA_B),
                                              (None, FEEDBACK)))
        count = 0
        for key, record in self.records():
            cases = [((publication(record, name),), {"observe": True}) for name in RESULTS]
            cases += [((), {"observe": True, "verdict": v, "head": h, "feedback": f})
                      for v, h, f in observations]
            for pubs, kwargs in cases:
                label = pubs[0]["status"] if pubs else "ci"
                with self.subTest(record=key, case=label, **{k: bool(v) if k == "feedback" else v
                                                             for k, v in kwargs.items()}):
                    after = final(tick(record, pubs, **kwargs), record)
                    self.assertIsNone(compat.state_problem(after))
                    if compat.stopped(after):
                        self.assertIsNone(after["pending_publication"])
                    count += 1
        self.assertGreater(count, 3000)

    # One function of every step of the tick that works on one contribution.
    INJECTIONS = {
        "publication": "reconcile.apply_publication",
        "late_result": "reconcile.compat.record_late_effect",
        "dispatch_recovery": "work.graph_id",
        "observed_head": "observe_ci.collect.branch_head",
        "observed_ci": "observe_ci.collect.evaluate_snapshot",
        "command": "compat.apply_command",
        "table_command": "controller._apply_table_command",
        "work_selection": "controller._actionable",
    }

    def test_an_injected_error_in_any_step_never_stops_the_tick(self):
        """One contribution errors in one step, whatever its state: the tick
        returns, stages at most the pause of that contribution, and leaves it
        stopped in an admitted state."""
        pause = {"issue_number": 12,
                 "comment": {"id": 900, "body": "/upstreaming pause", "user": dict(OWNER)}}
        count = errored = 0
        for key, record in self.records():
            for step, target in self.INJECTIONS.items():
                pubs = (publication(record, "published"),)
                with self.subTest(record=key, step=step), \
                        mock.patch(target, side_effect=RuntimeError("injected failure")) as hit:
                    output = tick(record, pubs, observe=True, verdict="passed", comments=[pause])
                    count += 1
                    after = final(output, record)
                    self.assertIsNone(compat.state_problem(after))
                    if not hit.called:
                        self.assertEqual(output.failures, [])
                        continue
                    errored += 1
                    self.assertEqual(len(output.failures), 1)
                    self.assertEqual(len(output.report["controller"]["alerts"]), 1)
                    self.assertTrue(compat.stopped(after))
                    self.assertEqual(output.report["requested_effects"], [])
                    self.assertEqual(output.report["dispatch_relaunch"], [])
                    if record["paused"] or record["phase"] in lifecycle.TERMINAL:
                        # Already stopped, or terminal: the verb does not apply.
                        self.assertEqual(output.staged, [])
                        continue
                    self.assertEqual([kind for kind, _r in output.staged],
                                     ["contributions", "events"])
                    self.assertEqual(events(output)[0]["kind"], "pause_on_processing_error")
                    self.assertEqual((after["paused_by"], after["active_work"],
                                      after["pending_publication"], after["revision"]),
                                     ("controller", None, None, record["revision"]))
        self.assertGreater(count, 1000)
        self.assertGreater(errored, 350)

    def test_no_stopped_state_retains_a_pending_publication(self):
        """From every admitted state with a pending publication, every stop
        clears it, and no later Result or publish step sets it again."""
        stops = ('/upstreaming park reason="x"', '/upstreaming abandon reason="x"',
                 "/upstreaming pause")
        held = {"retry": {"pending_publication": dict(PENDING)},
                "proposal": {"pending_publication": dict(PENDING),
                             "proposal_artifact": dict(ARTIFACT, outcome="proposal")}}
        count = 0
        for (key, record), (kind, fields) in itertools.product(self.records(), held.items()):
            if "|None|work=False" not in key:
                continue
            record = dict(record, **copy.deepcopy(fields))
            for body in stops:
                cmd = commands.parse(body)
                out = command(record, body)
                if not out.accepted:
                    # The table does not take this stop here (no row, or a
                    # guard; `abandon` applies in states of the table only),
                    # or the record is terminal.
                    self.assertTrue(engine.takes_command(record, cmd.verb)
                                    or cmd.verb not in compat.LEGACY_COMMANDS
                                    or record["phase"] in lifecycle.TERMINAL, out.reason)
                    continue
                stopped_record = out.record
                with self.subTest(record=key, pending=kind, stop=cmd.verb):
                    self.assertTrue(compat.stopped(stopped_record))
                    self.assertIsNone(stopped_record["pending_publication"])
                for name in RESULTS:
                    with self.subTest(record=key, pending=kind, stop=cmd.verb, result=name):
                        output = tick(stopped_record, [publication(record, name)], observe=True,
                                      policy=WRITES)
                        after = final(output, stopped_record)
                        self.assertIsNone(compat.state_problem(after))
                        self.assertTrue(compat.stopped(after))
                        self.assertIsNone(after["pending_publication"])
                        self.assertEqual(output.report["publication_retries"], [])
                        self.assertEqual(output.report["proposal_publications"], [])
                        count += 1
        self.assertGreater(count, 1200)


if __name__ == "__main__":
    unittest.main()
