# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A worker Result that matches no request of the record.

The table's ``record_late_effect`` applies in every state: a Result that does
not match the current generation, revision, and requested Work is recorded and
never folded. ``park``, ``abandon``, ``pause``, and ``block`` clear the
requested Work without a revision bump, so the Result of the Work they stopped
carries the record's own revision. It must be recorded, change nothing, and
never stop the tick.
"""

from __future__ import annotations

import contextlib
import copy
import io
import itertools
import unittest
from datetime import datetime
from unittest import mock

import capacity
import collect
import commands
import compat
import engine
import lifecycle
import reconcile
import schemas
from tests.helpers import NOW, OWNER, SHA_A, SHA_B, contribution, policy

T = datetime.fromisoformat("2026-11-23T09:00:00+00:00")
POLICY = policy()


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
         capacity_ok=True):
    snapshot = reconcile.Snapshot({"c": copy.deepcopy(record)}, {}, {}, {}, {})
    with mock.patch("observe_ci.collect.branch_head", return_value={"head": head}), \
            mock.patch("observe_ci.collect.development_review_feedback", return_value=feedback), \
            mock.patch("observe_ci.collect.ci_snapshot", return_value=ci_snapshot(head, verdict)), \
            contextlib.redirect_stderr(io.StringIO()):
        return reconcile.tick(object() if observe else None, snapshot, policy=POLICY, now=T,
                              publications=list(publications), comments=[],
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
    """``running(phase)`` after a human stopped it with ``verb``."""
    record = running(phase)
    out = compat.apply_command(record, commands.parse(f'/upstreaming {verb} reason="stop"'),
                               command_id="c", actor=dict(OWNER), now=NOW)
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

    def test_park_abandon_pause_and_block_during_running_work(self):
        expected = {"park": "parked.idle", "abandon": "abandoned.idle"}
        for phase, verb in itertools.product(("investigation", "development", "upstream_review"),
                                             ("park", "abandon", "pause", "block")):
            record = stopped(phase, verb)
            if verb in expected:
                self.assertEqual(engine.state_key(record), expected[verb])
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
            "abandoned.idle": contribution(phase="abandoned", activity="idle"),
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
                       contribution(phase="abandoned", activity="idle")):
            state = engine.state_key(record)
            self.assertEqual(state in lifecycle.LIFECYCLE, state == "parked.idle")
            event = compat.record_late_effect(record, policy=POLICY, now=NOW, cause_id="555",
                                              details=details)
            schemas.validate("event", event)
            self.assertEqual((event["kind"], event["cause"], event["from"], event["to"]),
                             ("record_late_effect", {"type": "result", "id": "555"}, {}, {}))
            self.assertEqual(event["details"], {"verb": "record_late_effect", "from_state": state,
                                                "to_state": state, **details})


class TestNoReachableRecordStopsTheTick(unittest.TestCase):
    """Every admitted state, with and without a stop flag and requested Work,
    against every Result and every CI observation: the tick always returns
    and leaves the record in an admitted state."""

    def records(self):
        states = sorted(set(lifecycle.LIFECYCLE) | compat.LEGACY_STATES)
        for state, flag, work in itertools.product(states, (None, "paused", "blocked"),
                                                   (False, True)):
            phase, activity = state.split(".")
            record = contribution(phase=phase, activity=activity, dossier_evidence_id="dossier-1",
                                  scope_paths=["a"], scope_coverage_complete=True)
            if phase in ("queued", "investigation", "provisioning"):
                record["fork"] = None
            if phase in ("upstream_review", "release_wait", "released"):
                record["upstream_pr"] = copy.deepcopy(UPSTREAM_PR)
            if flag == "paused":
                record["paused"] = True
            if flag == "blocked":
                record["blocked_reason"] = "x"
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
                    count += 1
        self.assertGreater(count, 4000)


if __name__ == "__main__":
    unittest.main()
