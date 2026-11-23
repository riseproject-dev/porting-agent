# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The stop flag, its commands, reminders, and the flag migration, by the table.

A record in a state of the lifecycle table takes ``pause``, ``resume``,
``park``, and ``refresh`` through the engine, in every phase; the controller's
own stops (``pause_on_identity_change``, ``pause_on_processing_error``) take
the engine for every table state; ``record_reminder`` is taken by the
controller in every gate, human wait, and locked state. The retired
``/upstreaming block`` is rejected with a message that names ``pause``.
Records written before ``blocked`` was merged into ``paused`` are rewritten
by an evented, exact-shape migration.
"""

from __future__ import annotations

import copy
import itertools
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import command_feedback
import commands
import compat
import controller
import engine
import lifecycle as lc
import reconcile
import schemas
import state as store
import status_ledger
from common import canonical_json
from tests import test_live_handoff_migration as live_handoff
from tests.helpers import (
    NOW,
    OWNER,
    SHA_A,
    apply_command,
    awaiting_submission,
    contribution,
    dossier,
    policy,
)
from tests.test_late_results import RESULTS, WORK, events, final, publication, running, tick
from tests.test_live_handoff_migration import HEAD, live, load

POLICY = policy()
TABLE_COMMANDS = {"pause": '/upstreaming pause reason="stop"',
                  "resume": '/upstreaming resume revision={revision} reason="go"',
                  "park": '/upstreaming park reason="stop"',
                  "refresh": '/upstreaming refresh revision={revision} reason="again"'}
LIVE_REQUESTED = "2026-11-22T22:54:12.110885+00:00"


def table_record(state, *, paused=False, pending=None):
    """A record in ``state`` of the table, with what the state is entered with."""
    phase, activity = state.split(".")
    record = contribution(phase=phase, activity=activity, dossier_evidence_id="dossier-1",
                          scope_paths=["a"], scope_coverage_complete=True)
    if phase in ("queued", "investigation", "provisioning"):
        record["fork"] = None
    if phase in ("upstream_review", "release_wait", "released"):
        record["upstream_pr"] = {"id": 9, "number": 9, "url": "https://github.com/example/proj/pull/9",
                                 "head_repository_id": 2, "head_ref": "riscv64",
                                 "head_sha": SHA_A, "base_ref": "main", "registered_by": OWNER}
    if phase == "parked":
        record.update(parked_reason="x", previous_phase="development")
    if phase in ("released", "closed"):
        record["terminal_reason"] = "x"
    kinds = lc.pending_kinds(state)
    if lc.LIFECYCLE[state].kind in lc.REMINDER_KINDS:
        kind = pending or (sorted(kinds)[0] if kinds else "task_without_outcome")
        record["pending_human_action"] = {"kind": kind, "requested_at": NOW, "reminded_at": None,
                                          "escalated_at": None, "counter": None}
    if state == "handoff.awaiting_submission":
        record = awaiting_submission()
    if state in ("handoff.waiting_upstream_pr",):
        record["clean_head_sha"] = SHA_A
    if paused:
        record.update(paused=True, paused_at=NOW, paused_by="human", paused_reason="x")
    schemas.validate("contribution", record)
    return record


class TestCommandsByTheTable(unittest.TestCase):
    """Per phase, the stop and resume commands take exactly what the table
    resolves; the tables are generated from ``lifecycle.LIFECYCLE``."""

    def test_every_table_state_takes_the_table_command(self):
        count = 0
        for state, paused, (verb, body) in itertools.product(
                lc.LIFECYCLE, (False, True), TABLE_COMMANDS.items()):
            record = table_record(state, paused=paused)
            cmd = commands.parse(body.format(revision=record["revision"]))
            with self.subTest(state=state, paused=paused, command=verb):
                self.assertTrue(engine.takes_command(record, cmd.verb))
                expected, _why = engine.resolve_command(record, cmd, policy=POLICY, now=NOW,
                                                        open_operations=())
                out = apply_command(record, cmd)
                self.assertEqual(out.accepted, expected is not None, out.reason)
                if expected is None:
                    self.assertEqual(out.record, record)
                    continue
                count += 1
                self.assertEqual(out.event["kind"], expected)
                self.assertEqual(out.event["details"]["verb"], expected)
                t, target = engine.resolve(state, expected)
                self.assertEqual(engine.state_key(out.record), target)
                self.assertEqual(out.record["revision"] - record["revision"],
                                 1 if t.effects.revision == "bump" else 0)
        self.assertGreater(count, 60)

    def test_every_verb_the_commands_reach_is_wired(self):
        reached = set()
        for state in lc.LIFECYCLE:
            for verb, t in lc.FLAGS["paused"].set_by.items():
                if t.command:
                    reached.add(verb)
            for verb, t in lc.FLAGS["paused"].cleared_by.items():
                reached.add(verb)
            for edge in lc.state_edges(state):
                command = (edge.transition.command or "").split(" ")[-1]
                if command in engine.TABLE_COMMANDS:
                    reached.add(edge.verb)
        self.assertEqual(reached - set(engine.PIPELINE_VERBS), set())
        self.assertIn("restart_generation", reached)
        self.assertIn("resume_stale", reached)

    def test_pause_from_agent_running_leads_to_the_state_where_work_is_due(self):
        for phase, target in (("investigation", "investigation.idle"),
                              ("development", "development.fix_needed"),
                              ("upstream_review", "upstream_review.fix_needed")):
            record = running(phase)
            record["pending_publication"] = {"worker_run_id": "5", "attempts": 1, "last_reason": "x",
                                             "next_attempt_at": NOW, "exhausted": False}
            with self.subTest(phase=phase):
                out = apply_command(record, commands.parse('/upstreaming pause reason="later"'))
                self.assertEqual(out.event["kind"], "pause")
                self.assertEqual(engine.state_key(out.record), target)
                self.assertEqual((out.record["active_work"], out.record["pending_publication"],
                                  out.record["paused_reason"], out.record["paused_by"]),
                                 (None, None, "later", "human"))
                self.assertEqual(out.record["revision"], record["revision"])
                # The resume bumps the revision; the state is the one where Work is due.
                resumed = apply_command(out.record, commands.parse(
                    f'/upstreaming resume revision={record["revision"]} reason="go"'))
                self.assertEqual(resumed.event["kind"], "resume_pause")
                self.assertEqual(engine.state_key(resumed.record), target)
                self.assertEqual(resumed.record["revision"], record["revision"] + 1)

    def test_replies_for_a_pause_or_resume_with_nothing_to_do(self):
        record = table_record("development.waiting_ci")
        resume = apply_command(record, commands.parse('/upstreaming resume revision=0 reason="x"'))
        self.assertEqual(resume.reason, "not paused")
        paused = apply_command(record, commands.parse("/upstreaming pause")).record
        again = apply_command(paused, commands.parse("/upstreaming pause"))
        self.assertEqual(again.reason, "already paused")

    def test_resume_of_a_paused_human_wait_names_the_wait_that_remains(self):
        record = table_record("development.waiting_human", paused=True, pending="work_failed")
        out = apply_command(record, commands.parse('/upstreaming resume revision=0 reason="go"'))
        self.assertEqual(out.event["kind"], "resume_pause")
        self.assertEqual(out.event["details"]["pending_human_action"], "work_failed")
        self.assertEqual(engine.state_key(out.record), "development.waiting_human")
        text = status_ledger.render(dict(out.event, sequence=3), out.record, "https://run")
        self.assertIn("still waits for a human: pending action `work_failed`", text)
        second = apply_command(out.record, commands.parse('/upstreaming resume revision=1 reason="go"'))
        self.assertEqual(second.event["kind"], "resume_development")
        self.assertEqual(engine.state_key(second.record), "development.fix_needed")


class TestBlockIsRetired(unittest.TestCase):
    def test_block_is_rejected_naming_pause(self):
        with self.assertRaises(commands.CommandError) as raised:
            commands.parse('/upstreaming block revision=1 reason="x"')
        self.assertIn("`block` is retired", str(raised.exception))
        self.assertIn('/upstreaming pause revision=N reason="..."', str(raised.exception))
        self.assertNotIn("block", commands.GRAMMAR)
        self.assertNotIn("block", commands.URGENT)
        reply = command_feedback.render(
            {"comment_id": 5, "reason": str(raised.exception), "actor": dict(OWNER), "revision": 1},
            {"body": '/upstreaming block revision=1 reason="x"'})
        self.assertIn('/upstreaming pause revision=1 reason="..."', reply)

    def test_no_record_carries_a_block(self):
        self.assertTrue(schemas.errors("contribution", contribution(blocked_reason="x")))
        self.assertFalse(lc.publication_check(
            contribution(paused=True), expected_revision=0, expected_head=SHA_A,
            observed_head=SHA_A, ref="riscv64", repository="riseproject-upstreaming/proj") is None)


class TestPauseMask(unittest.TestCase):
    """While paused: no launch, no charge, and a Result is late."""

    def test_paused_record_gets_no_work_no_charge_and_late_results(self):
        record = running("development")
        paused = apply_command(record, commands.parse("/upstreaming pause")).record
        self.assertEqual(engine.state_key(paused), "development.fix_needed")
        for name in RESULTS:
            with self.subTest(result=name):
                output = tick(paused, [publication(record, name)], capacity_ok=True)
                after = final(output, paused)
                self.assertEqual(output.report["requested_effects"], [])
                self.assertEqual(output.report["publication_retries"], [])
                self.assertEqual(after["budget"], paused["budget"])
                self.assertEqual([e["kind"] for e in events(output)], ["record_late_effect"])
                self.assertEqual(dict(after, processed_publications=[]),
                                 dict(paused, processed_publications=[]))
        # Not paused, the same state requests Work.
        resumed = apply_command(paused, commands.parse(
            f"/upstreaming resume revision={paused['revision']} reason=go")).record
        output = tick(resumed, capacity_ok=True)
        # No failing CI job and no feedback is recorded: the Work the pause
        # stopped is requested again as the patch it was (`request_patch`).
        self.assertEqual([w["task"] for w in output.report["requested_effects"]],
                         ["development_patch"])


class TestStaleResume(unittest.TestCase):
    LATER = (datetime.fromisoformat(NOW) + timedelta(days=8)).isoformat()

    def resume(self, record, now):
        return apply_command(record, commands.parse(
            f'/upstreaming resume revision={record["revision"]} reason="long stop"'), now=now)

    def test_development_and_provisioning_refresh_after_a_long_stop(self):
        for state in ("development.waiting_ci", "development.fix_needed", "provisioning.idle"):
            record = table_record(state, paused=True)
            with self.subTest(state=state):
                fresh = self.resume(record, NOW)
                self.assertEqual(fresh.event["kind"], "resume_pause")
                self.assertEqual(engine.state_key(fresh.record), state)
                stale = self.resume(record, self.LATER)
                self.assertEqual(stale.event["kind"], "resume_stale")
                self.assertEqual(engine.state_key(stale.record), "queued.idle")
                self.assertFalse(stale.record["paused"])
                refresh = stale.record["refresh"]
                self.assertEqual(refresh["from_phase"], record["phase"])
                self.assertEqual(refresh["head_sha"], (record["fork"] or {}).get("head_sha"))
                self.assertEqual(refresh["pr_number"], (record["fork"] or {}).get("pr_number"))
                self.assertIsNone(stale.record["dossier_evidence_id"])
                self.assertEqual(stale.record["fork"], record["fork"])

    def test_a_human_wait_in_development_refreshes_after_a_long_wait(self):
        record = table_record("development.waiting_human", pending="work_failed")
        self.assertEqual(self.resume(record, NOW).event["kind"], "resume_development")
        self.assertEqual(self.resume(record, self.LATER).event["kind"], "resume_stale")

    def test_never_in_handoff(self):
        record = dict(awaiting_submission(), paused=True, paused_at=NOW, paused_by="human")
        out = self.resume(record, (datetime.fromisoformat(NOW) + timedelta(days=60)).isoformat())
        self.assertEqual(out.event["kind"], "resume_pause")
        self.assertEqual(engine.state_key(out.record), "handoff.awaiting_submission")
        for name in ("handoff_request", "fork", "last_ci_evidence_id", "dossier_evidence_id",
                     "pending_human_action"):
            self.assertEqual(out.record[name], record[name])

    def test_a_refreshed_dossier_without_a_development_pr_is_admitted_afresh(self):
        stale = self.resume(table_record("provisioning.idle", paused=True), self.LATER).record
        rec = dict(stale, phase="investigation", activity="agent_running",
                   active_work=dict(WORK, task="investigation"))
        new, _staged = reconcile.apply_publication(
            rec, {"status": "investigation", "dossier": dossier("develop")},
            now=NOW, policy=policy(), worker_run_id="7")
        self.assertEqual(engine.state_key(new), "provisioning.idle")
        self.assertIsNone(new["refresh"])


class TestReminders(unittest.TestCase):
    def ticks(self, record, now):
        output = reconcile.tick(None, reconcile.Snapshot({"c": copy.deepcopy(record)}, {}, {}, {}, {}),
                                policy=POLICY, now=datetime.fromisoformat(now), publications=[],
                                pressure=reconcile.capacity.Pressure(ok=False, reason="none"),
                                observe=False, dispatcher_runs=[], queue_works={})
        return final(output, record), events(output)

    def test_cadence_in_business_days(self):
        record = table_record("development.waiting_human", pending="work_failed")
        record["pending_human_action"]["requested_at"] = "2026-11-20T10:00:00+00:00"  # a Friday
        # Monday and Tuesday count; the weekend does not.
        for now, due in (("2026-11-24T09:59:59+00:00", False), ("2026-11-24T10:00:00+00:00", False),
                         ("2026-11-25T09:59:59+00:00", False), ("2026-11-25T10:00:00+00:00", True)):
            with self.subTest(now=now):
                after, evs = self.ticks(record, now)
                self.assertEqual([e["kind"] for e in evs], ["record_reminder"] if due else [])
        after, _ = self.ticks(record, "2026-11-25T10:00:00+00:00")
        self.assertEqual(after["pending_human_action"]["reminded_at"], "2026-11-25T10:00:00+00:00")
        self.assertEqual(after["revision"], record["revision"])
        # The next one counts from the last reminder: Thu, Fri, Mon.
        self.assertEqual(self.ticks(after, "2026-11-30T09:59:59+00:00")[1], [])
        self.assertEqual([e["kind"] for e in self.ticks(after, "2026-11-30T10:00:00+00:00")[1]],
                         ["record_reminder"])
        # Paused, no reminder.
        paused = dict(record, paused=True, paused_at=NOW, paused_by="human")
        self.assertEqual(self.ticks(paused, "2026-12-30T10:00:00+00:00")[1], [])

    def test_every_reminder_state_and_no_other(self):
        states = set(lc.rule_states("record_reminder"))
        self.assertEqual(states, {k for k, s in lc.LIFECYCLE.items() if s.kind in lc.REMINDER_KINDS})
        for state in lc.LIFECYCLE:
            record = table_record(state)
            if not record.get("pending_human_action"):
                record["pending_human_action"] = {"kind": "x", "requested_at": NOW,
                                                  "reminded_at": None, "escalated_at": None}
            with self.subTest(state=state):
                _after, evs = self.ticks(record, "2026-12-30T10:00:00+00:00")
                self.assertEqual("record_reminder" in [e["kind"] for e in evs], state in states)

    def test_an_error_in_the_reminder_step_pauses_only_that_contribution(self):
        from unittest import mock

        record = table_record("development.waiting_human", pending="work_failed")
        with mock.patch("controller.reminder_details", side_effect=RuntimeError("injected")):
            after, evs = self.ticks(record, "2026-12-30T10:00:00+00:00")
        self.assertEqual([e["kind"] for e in evs], ["pause_on_processing_error"])
        self.assertEqual(evs[0]["details"]["step"], "reminder")
        self.assertEqual((after["paused"], after["pending_human_action"]),
                         (True, record["pending_human_action"]))

    def live_gate(self):
        record, _event = compat.migrate_handoff_wait(live(), evidence=load("evidence-ci.json"),
                                                     policy=POLICY, now=LIVE_REQUESTED)
        self.assertEqual(record["pending_human_action"]["requested_at"], LIVE_REQUESTED)
        record["fork"]["head_ref"] = "riscv64-dev"
        return record

    def test_the_live_record_is_first_reminded_three_business_days_after_the_request(self):
        record = self.live_gate()
        first = "2026-11-25T22:54:12.110885+00:00"
        for now in ("2026-11-22T23:30:00+00:00", "2026-11-23T09:00:00+00:00",
                    "2026-11-25T22:54:12.110884+00:00"):
            with self.subTest(now=now):
                after, evs = self.ticks(record, now)
                self.assertEqual(evs, [])
                self.assertEqual(after, record)
        # With CI observed as on every live tick: still nothing before it is due.
        harness = live_handoff.TestMigratedRecordUnderTheOrdinaryController(
            "test_pause_and_resume_keep_the_submission")
        output = harness.tick(record)
        self.assertEqual([e for k, e in output.staged if k == "events"], [])
        self.assertNotIn("reminders", output.report["controller"])
        after, evs = self.ticks(record, first)
        self.assertEqual([e["kind"] for e in evs], ["record_reminder"])
        event = evs[0]
        self.assertEqual(event["details"]["bound_to"], {"revision": 6, "head": HEAD})
        self.assertEqual((event["from"], event["to"], event["revision_bumped"]), ({}, {}, False))
        # Only the reminder time changes; the record keeps its fork, PR, head, and evidence.
        self.assertEqual(dict(after, pending_human_action=None, updated_at=None),
                         dict(record, pending_human_action=None, updated_at=None))
        self.assertEqual(after["pending_human_action"], dict(record["pending_human_action"],
                                                             reminded_at=first))
        text = status_ledger.render(dict(event, sequence=24), after, "https://run")
        self.assertIn(f"/upstreaming submit-upstream revision=6 head={HEAD}", text)
        self.assertNotIn("refuse", text)
        self.assertNotIn("changes nothing", text)
        self.assertIn("Reminder", text)
        # The next one is three business days later: Thursday, Friday, Monday.
        self.assertEqual(self.ticks(after, "2026-11-30T22:54:12.110884+00:00")[1], [])
        self.assertEqual([e["kind"] for e in self.ticks(after, "2026-11-30T22:54:12.110885+00:00")[1]],
                         ["record_reminder"])


class TestControllerStops(unittest.TestCase):
    def snapshot(self, upstream_id=99):
        return {"example/proj": {"repository_id": upstream_id, "base_ref": "main",
                                 "base_sha": "c" * 40}}

    def identity_tick(self, record):
        result = controller.tick(
            contributions={"c": copy.deepcopy(record)}, operations={}, consumed={}, projects={},
            policy=POLICY, inputs=controller.Inputs(
                now=datetime.fromisoformat(NOW), issues=[], comments=[],
                pressure=reconcile.capacity.Pressure(ok=True),
                upstream_snapshots=self.snapshot()))
        return result

    def test_identity_change_pauses_through_the_engine_in_a_table_state(self):
        record = running("development")
        result = self.identity_tick(record)
        recs = [r for k, r in result.staged if k == "contributions"]
        evs = [r for k, r in result.staged if k == "events"]
        self.assertEqual([e["kind"] for e in evs], ["pause_on_identity_change"])
        paused = recs[-1]
        self.assertEqual(engine.state_key(paused), "development.fix_needed")
        self.assertEqual((paused["paused"], paused["paused_by"], paused["paused_reason"],
                          paused["active_work"], paused["blocked_reason"]),
                         (True, "controller", "upstream_identity_changed", None, None))
        self.assertEqual(result.requested_effects, [])
        # Already paused: nothing again.
        self.assertEqual(self.identity_tick(paused).staged, [])

    def test_identity_change_in_upstream_review(self):
        record = contribution(phase="upstream_review", activity="agent_running",
                              upstream_pr={"id": 9, "number": 9,
                                           "url": "https://github.com/example/proj/pull/9",
                                           "head_repository_id": 2, "head_ref": "riscv64",
                                           "head_sha": "a" * 40, "base_ref": "main",
                                           "registered_by": OWNER},
                              pending_publication={"worker_run_id": "5", "attempts": 1,
                                                   "last_reason": "x", "next_attempt_at": NOW,
                                                   "exhausted": False})
        evs = [r for k, r in self.identity_tick(record).staged if k == "events"]
        recs = [r for k, r in self.identity_tick(record).staged if k == "contributions"]
        self.assertEqual([e["kind"] for e in evs], ["pause_on_identity_change"])
        self.assertEqual((engine.state_key(recs[-1]), recs[-1]["pending_publication"],
                          recs[-1]["paused_by"]), ("upstream_review.fix_needed", None, "controller"))


class TestFlagMigration(unittest.TestCase):
    def migrate(self, record):
        return compat.migrate_flags(record, now="2026-11-23T00:00:00+00:00")

    def test_blocked_by_a_human_becomes_a_human_pause(self):
        record = contribution(phase="development", activity="waiting_human", blocked_reason="hold",
                              updated_at="2026-11-19T00:00:00+00:00")
        self.assertTrue(compat.needs_flag_migration(record))
        new, event = self.migrate(record)
        # The block's wait ends in the development state of the table: the
        # head differs from the provisioned base, so CI is observed.
        self.assertEqual((new["paused"], new["paused_by"], new["paused_reason"], new["paused_at"],
                          new["blocked_reason"], new["activity"]),
                         (True, "human", "hold", "2026-11-19T00:00:00+00:00", None, "waiting_ci"))
        self.assertEqual(event["kind"], compat.FLAG_MIGRATED)
        self.assertEqual(event["cause"], {"type": "migration", "id": compat.FLAG_MIGRATION})
        self.assertEqual(event["details"]["changed"], ["blocked_reason", "paused_at"])
        self.assertFalse(compat.needs_flag_migration(new))
        with self.assertRaises(lc.TransitionError):
            self.migrate(new)

    def test_identity_block_becomes_the_controller_pause(self):
        record = contribution(phase="queued", activity="waiting_human", fork=None,
                              blocked_reason=compat.IDENTITY_BLOCK)
        new, _event = self.migrate(record)
        self.assertEqual((new["paused_by"], new["paused_reason"], engine.state_key(new)),
                         ("controller", "upstream_identity_changed", "queued.idle"))

    def test_a_block_over_a_human_wait_keeps_the_wait(self):
        pending = {"kind": "work_failed", "requested_at": NOW, "reminded_at": None,
                   "escalated_at": None}
        record = contribution(activity="waiting_human", blocked_reason="x",
                              pending_human_action=pending)
        new, _event = self.migrate(record)
        self.assertEqual((engine.state_key(new), new["pending_human_action"]["kind"]),
                         ("development.waiting_human", "work_failed"))
        # A wait the table does not have, with a pending action: no reviewed migration.
        with self.assertRaises(lc.TransitionError):
            self.migrate(contribution(phase="parked", activity="waiting_human", blocked_reason="x",
                                      pending_human_action=pending))

    def test_old_pauses_get_paused_at_and_lose_what_stops_clear(self):
        held = {"worker_run_id": "5", "attempts": 1, "last_reason": "x", "next_attempt_at": NOW,
                "exhausted": False}
        proposal = {"outcome": "proposal", "run_id": "5"}
        record = contribution(activity="waiting_ci", paused=True, paused_by="human",
                              pending_publication=held, proposal_artifact=proposal)
        new, event = self.migrate(record)
        self.assertEqual((new["paused_at"], new["pending_publication"], new["proposal_artifact"]),
                         (record["updated_at"], None, None))
        self.assertEqual(event["details"]["changed"],
                         ["paused_at", "pending_publication", "proposal_artifact"])
        parked = contribution(phase="parked", activity="idle", parked_reason="x",
                              pending_publication=held)
        new, _event = self.migrate(parked)
        self.assertIsNone(new["pending_publication"])
        # A parked record without anything held needs nothing.
        self.assertFalse(compat.needs_flag_migration(dict(parked, pending_publication=None)))

    def test_fail_closed(self):
        with self.assertRaises(lc.TransitionError):
            self.migrate(awaiting_submission(blocked_reason="x"))
        self.assertTrue(compat.needs_flag_migration(awaiting_submission(blocked_reason="x")))
        self.assertFalse(compat.awaits_flag_migration(awaiting_submission(blocked_reason="x")))

    def test_the_live_record_is_untouched(self):
        record = json.loads(Path(".state/contributions/closeio--ciso8601--1.json").read_text()) \
            if Path(".state/contributions/closeio--ciso8601--1.json").exists() else None
        for candidate in filter(None, (record, TestReminders().live_gate())):
            self.assertFalse(compat.needs_flag_migration(candidate))

    def test_the_controller_migrates_in_its_own_commit_and_refuses_unknown_shapes(self):
        p = policy()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / ".state" / "contributions"
            directory.mkdir(parents=True)
            blocked = contribution(phase="development", activity="waiting_human",
                                   blocked_reason="hold")
            path = directory / "example--proj--12.json"
            path.write_text(canonical_json(blocked))
            # The store admits the record only as the migration reads it.
            self.assertEqual(store.check_blob(".state/contributions/example--proj--12.json",
                                              path.read_bytes(), p), [])
            self.assertEqual(store.pending_record_migrations(root, p),
                             [".state/contributions/example--proj--12.json"])
            written = store.apply_pending_record_migrations(root, p, now=NOW)
            self.assertEqual(sorted(written), [".state/contributions/example--proj--12.json",
                                               ".state/events/example--proj--12/00000000.json"])
            migrated = json.loads(path.read_text())
            self.assertEqual((migrated["paused"], migrated["blocked_reason"]), (True, None))
            self.assertEqual(store.pending_record_migrations(root, p), [])
            # A shape without a reviewed migration stops the controller.
            path.write_text(canonical_json(awaiting_submission(blocked_reason="x")))
            self.assertTrue(store.check_blob(".state/contributions/example--proj--12.json",
                                             path.read_bytes(), p))
            with self.assertRaises(store.StateError):
                store.pending_record_migrations(root, p)


if __name__ == "__main__":
    unittest.main()
