# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The name migration of the plan rename on the committed state.

``tests/fixtures/plan-rename`` holds the contributions, projects, plan
evidence, and the events of the parked contribution as the controller
committed them before the planning phase and its plan were renamed from
`investigation` and `dossier`. The controller rewrites them by itself, in one
evented state-only commit, and changes nothing but the names; the events stay
as they were written and are read under the current names.
"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import collect
import engine
import lifecycle
import reconcile
import state
import status_ledger
from common import canonical_json
from tests.helpers import git, init_repo, policy

FIXTURES = Path(__file__).parent / "fixtures" / "plan-rename"
NOW = "2026-11-23T20:00:00+00:00"
LIVE = ".state/contributions/closeio--ciso8601--1.json"
PARKED = ".state/contributions/luhenry--d2vm--3.json"
LIVE_PLAN = ".state/evidence/dossier-closeio--ciso8601--1-r2.json"
PARKED_PLAN = ".state/evidence/dossier-luhenry--d2vm--3-r0.json"
PROJECTS = (".state/projects/closeio--ciso8601.json", ".state/projects/luhenry--d2vm.json")
LIVE_EVENT = ".state/events/closeio--ciso8601--1/00000000.json"
PARKED_EVENT = ".state/events/luhenry--d2vm--3/00000004.json"
MIGRATED = sorted([LIVE, PARKED, LIVE_PLAN, PARKED_PLAN, *PROJECTS, LIVE_EVENT, PARKED_EVENT])


def flat(value, prefix=""):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out.update(flat(item, f"{prefix}{key}."))
        return out
    return {prefix[:-1]: value}


def changed(before: dict, after: dict) -> dict:
    a, b = flat(before), flat(after)
    return {k: (a.get(k, "-"), b.get(k, "-")) for k in sorted(set(a) | set(b))
            if a.get(k, "-") != b.get(k, "-")}


def fixture(rel: str) -> dict:
    return json.loads((FIXTURES / rel.removeprefix(".state/")).read_text())


class StateCopy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = init_repo(Path(self.tmp.name) / "repo")
        shutil.copytree(FIXTURES, self.repo / ".state")
        git(self.repo, "add", ".state")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.p = policy()

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, rel: str) -> dict:
        return json.loads((self.repo / rel).read_text())


class TestNameMigration(StateCopy):
    def test_validation_admits_the_pending_shape(self):
        self.assertEqual(state.pending_record_migrations(self.repo, self.p),
                         sorted([LIVE, PARKED, LIVE_PLAN, PARKED_PLAN, *PROJECTS]))
        store = state.StateStore(self.repo, self.p)
        for kind in ("contributions", "projects", "evidence", "events"):
            store.load_all(kind)

    def test_the_controller_commits_only_the_migration(self):
        report = self.repo / "report.json"
        stderr = io.StringIO()
        with redirect_stderr(stderr), mock.patch("reconcile.datetime") as clock, \
                mock.patch.object(collect, "Api", side_effect=AssertionError("no API")):
            clock.now.return_value = datetime.fromisoformat(NOW)
            clock.fromisoformat = datetime.fromisoformat
            result = reconcile.cmd_reconcile(SimpleNamespace(root=str(self.repo),
                                                             report=str(report)))
        self.assertEqual((result, stderr.getvalue()), (0, ""))
        written = json.loads(report.read_text())
        self.assertEqual(sorted(written["written"]), MIGRATED)
        self.assertEqual(written["requested_effects"], [])

    def test_migration_events_record_the_workflow_run(self):
        env = {"GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "2"}
        with mock.patch.dict("os.environ", env):
            state.apply_pending_record_migrations(self.repo, self.p, now=NOW,
                                                  run=state.run_from_env())
        for rel in (LIVE_EVENT, PARKED_EVENT):
            self.assertEqual(self.read(rel)["run"], {"id": 4242, "attempt": 2})

    def test_the_controller_records_its_run_on_migration_events(self):
        env = {"GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "1"}
        with redirect_stderr(io.StringIO()), mock.patch.dict("os.environ", env), \
                mock.patch("reconcile.datetime") as clock, \
                mock.patch.object(collect, "Api", side_effect=AssertionError("no API")):
            clock.now.return_value = datetime.fromisoformat(NOW)
            clock.fromisoformat = datetime.fromisoformat
            result = reconcile.cmd_reconcile(SimpleNamespace(
                root=str(self.repo), report=str(self.repo / "report.json")))
        self.assertEqual(result, 0)
        for rel in (LIVE_EVENT, PARKED_EVENT):
            self.assertEqual(self.read(rel)["run"], {"id": 4242, "attempt": 1})

    def test_only_the_names_change(self):
        written = state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        self.assertEqual(sorted(written), MIGRATED)
        self.assertEqual(changed(fixture(LIVE), self.read(LIVE)), {
            "budget.investigation_attempts": (0, "-"), "budget.planning_attempts": ("-", 0),
            "dossier_evidence_id": ("dossier-closeio--ciso8601--1-r2", "-"),
            "plan_evidence_id": ("-", "dossier-closeio--ciso8601--1-r2")})
        self.assertEqual(changed(fixture(PARKED), self.read(PARKED)), {
            "budget.investigation_attempts": (0, "-"), "budget.planning_attempts": ("-", 0),
            "dossier_evidence_id": (None, "-"), "plan_evidence_id": ("-", None),
            "previous_phase": ("investigation", "planning")})
        for rel in (LIVE_PLAN, PARKED_PLAN):
            self.assertEqual(changed(fixture(rel), self.read(rel)), {"kind": ("dossier", "plan")})
        for rel in PROJECTS:
            self.assertEqual(changed(fixture(rel), self.read(rel)), {
                "dossier_evidence_id": (None, "-"), "plan_evidence_id": ("-", None)})
        live = self.read(LIVE)
        self.assertEqual((engine.state_key(live), live["revision"], live["pending_human_action"]),
                         ("handoff.awaiting_submission", 6, fixture(LIVE)["pending_human_action"]))

    def test_one_event_per_contribution_names_what_it_renamed(self):
        state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        event = self.read(LIVE_EVENT)
        self.assertEqual((event["kind"], event["cause"], event["from"], event["to"],
                          event["revision_bumped"]),
                         (state.NAMES_MIGRATED, {"type": "migration", "id": state.NAME_MIGRATION},
                          {}, {}, False))
        self.assertEqual(event["details"], {
            "from_state": "handoff.awaiting_submission", "to_state": "handoff.awaiting_submission",
            "renamed": ["dossier_evidence_id", "budget.investigation_attempts"],
            "evidence": ["dossier-closeio--ciso8601--1-r2"], "projects": ["closeio--ciso8601"]})
        parked = self.read(PARKED_EVENT)
        self.assertEqual(parked["details"]["renamed"],
                         ["dossier_evidence_id", "budget.investigation_attempts",
                          "previous_phase=investigation"])
        self.assertFalse(status_ledger.notifies(event, "handoff.awaiting_submission",
                                                "handoff.awaiting_submission"))
        self.assertIn("current names", status_ledger.RECORDED_KINDS[event["kind"]])

    def test_the_migration_is_idempotent_and_the_state_valid(self):
        state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        self.assertEqual(state.pending_record_migrations(self.repo, self.p), [])
        self.assertEqual(state.apply_pending_record_migrations(self.repo, self.p, now=NOW), [])
        store = state.StateStore(self.repo, self.p)
        self.assertEqual(len(store.load_all("contributions")), 2)
        self.assertEqual(len(store.load_all("events")), 6)

    def test_the_state_publisher_accepts_the_migration_commit(self):
        state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        git(self.repo, "add", ".state")
        git(self.repo, "commit", "-q", "-m", "state")
        candidate = git(self.repo, "rev-parse", "HEAD")
        report = state.validate_state_diff(self.repo, self.base, candidate, self.p)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(sorted(report.changed), MIGRATED)
        # Writing a historic name again is refused by the same final check.
        for rel in (LIVE, LIVE_PLAN, PROJECTS[0]):
            (self.repo / rel).write_text(canonical_json(fixture(rel)))
            git(self.repo, "add", ".state")
            git(self.repo, "commit", "-q", "-m", "old")
            old = git(self.repo, "rev-parse", "HEAD")
            problems = state.validate_state_diff(self.repo, candidate, old, self.p).problems
            self.assertTrue(any("awaits the record migration" in p for p in problems), problems)
            git(self.repo, "reset", "-q", "--hard", candidate)

    def test_requested_planning_work_under_the_historic_task_fails_closed(self):
        record = fixture(PARKED)
        record.update(phase="investigation", activity="agent_running", previous_phase=None,
                      parked_reason=None, active_work={
                          "dedupe_key": "luhenry--d2vm/3@g0.r0:investigation",
                          "task": "investigation", "revision": 0, "generation": 0, "attempt": 1,
                          "requested_at": NOW, "deadline_at": NOW, "dispatched_run_id": None,
                          "reason_ids": [], "feedback_evidence_ids": []})
        (self.repo / PARKED).write_text(canonical_json(record))
        with self.assertRaises(state.StateError):
            state.pending_record_migrations(self.repo, self.p)
        problems = state.check_blob(PARKED, canonical_json(record).encode(), self.p)
        self.assertTrue(any("historic task investigation" in p for p in problems), problems)

    def test_a_record_in_the_historic_phase_is_renamed(self):
        record = fixture(PARKED)
        record.update(phase="investigation", activity="waiting_human", previous_phase=None,
                      parked_reason=None, pending_human_action={
                          "kind": "investigation_failed", "requested_at": NOW,
                          "reminded_at": None, "escalated_at": None,
                          "counter": "investigation_attempts"})
        new, renamed = state.migrate_names("contributions", record)
        self.assertEqual(engine.state_key(new), "planning.waiting_human")
        self.assertEqual((new["pending_human_action"]["kind"],
                          new["pending_human_action"]["counter"]),
                         ("planning_failed", "planning_attempts"))
        self.assertIn("phase=investigation", renamed)
        self.assertEqual(state.check_blob(PARKED, canonical_json(record).encode(), self.p), [])

    def test_a_field_under_both_names_fails_closed(self):
        record = dict(fixture(LIVE), plan_evidence_id=None)
        with self.assertRaises(state.StateError):
            state.migrate_names("contributions", record)


class TestHistoricEvents(unittest.TestCase):
    """Committed events keep the names they were written with."""

    def events(self):
        directory = FIXTURES / "events" / "luhenry--d2vm--3"
        return [json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))]

    def test_events_are_valid_and_never_rewritten(self):
        p = policy()
        for path in sorted((FIXTURES / "events").rglob("*.json")):
            rel = ".state/" + path.relative_to(FIXTURES).as_posix()
            self.assertEqual(state.check_blob(rel, path.read_bytes(), p), [])
            self.assertFalse(state.needs_name_migration("events", json.loads(path.read_text())))

    def test_historic_names_are_read_as_the_current_ones(self):
        events = self.events()
        self.assertEqual(status_ledger.event_states(events)[1:], [
            ("queued.idle", "planning.idle"), ("planning.idle", "planning.agent_running"),
            ("planning.agent_running", "parked.idle")])
        record = state.migrate_names("contributions", fixture(PARKED))[0]
        text = status_ledger.table_message(events[3], record)
        self.assertTrue(text.startswith("`park_planning` by the Result of requested Work"), text)

    def test_every_historic_name_maps_to_a_current_name(self):
        verbs = {e.verb for e in lifecycle.table_edges()}
        for old, new in lifecycle.HISTORIC_NAMES.items():
            self.assertNotIn(old, verbs | set(lifecycle.LIFECYCLE) | set(lifecycle.BUDGETS))
            self.assertTrue(new in verbs or new in lifecycle.LIFECYCLE or new in lifecycle.BUDGETS
                            or new in ("planning", "planning_failed", "plan", "plan_evidence_id"),
                            new)
        self.assertEqual(lifecycle.current_name("submit_upstream"), "submit_upstream")
        self.assertIsNone(lifecycle.current_name(None))


if __name__ == "__main__":
    unittest.main()
