# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The name migration of the task rename on the live state.

``tests/fixtures/task-rename`` is the committed `.state` of the control
repository as the controller left it before the unit it requests was named a
task instead of Work: the contribution `luhenry--d2vm/3` holds a requested
`development_repair` task in `active_work`, and `closeio--ciso8601/1` holds
none. The controller rewrites both by itself, in one evented state-only
commit, and changes nothing but the names: the task in flight keeps its
`dedupe_key`, its kind, and its queue graph, so the queued payload stays valid
and its Result is folded as before. The events stay as they were written and
are read under the current names.
"""

import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
import task_build
import tasks
from common import canonical_json
from tests.helpers import git, init_repo, policy
from tests.test_late_results import events as events_of
from tests.test_late_results import final, tick
from tests.test_task_identity import result

FIXTURES = Path(__file__).parent / "fixtures" / "task-rename"
NOW = "2026-11-24T18:00:00+00:00"
D2VM = ".state/contributions/luhenry--d2vm--3.json"
CISO = ".state/contributions/closeio--ciso8601--1.json"
D2VM_EVENT = ".state/events/luhenry--d2vm--3/00000051.json"
CISO_EVENT = ".state/events/closeio--ciso8601--1/00000032.json"
CONTRIBUTIONS = (CISO, D2VM)
MIGRATED = sorted([*CONTRIBUTIONS, D2VM_EVENT, CISO_EVENT])
IN_FLIGHT = "luhenry--d2vm/3@g1.r13:development_repair"


def fixture(rel: str) -> dict:
    return json.loads((FIXTURES / rel.removeprefix(".state/")).read_text())


def renamed(record: dict) -> dict:
    """``record`` as the migration rewrites it: the field `active_work` is
    called `active_task`, nothing else changes."""
    record = copy.deepcopy(record)
    record["active_task"] = record.pop("active_work")
    charged = (record["active_task"] or {}).get("charged")
    if charged is not None:
        record["active_task"]["charged"] = [e.replace("work_total", "task_total") for e in charged]
    return record


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

    def commit(self, message: str = "state") -> str:
        git(self.repo, "add", ".state")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")


class TestTheRecordMigratesOnce(StateCopy):
    def test_the_fixture_is_the_live_state_before_the_rename(self):
        record = fixture(D2VM)
        self.assertEqual((engine.state_key(record), record["revision"], record["generation"]),
                         ("development.agent_running", 13, 1))
        self.assertEqual((record["active_work"]["task"], record["active_work"]["dedupe_key"],
                          record["active_work"]["attempt"]), ("development_repair", IN_FLIGHT, 1))
        self.assertNotIn("active_task", record)
        self.assertIsNone(fixture(CISO)["active_work"])

    def test_both_records_await_the_rename(self):
        self.assertEqual(state.pending_record_migrations(self.repo, self.p), sorted(CONTRIBUTIONS))
        for rel in CONTRIBUTIONS:
            self.assertTrue(state.needs_name_migration("contributions", self.read(rel)))
        # Valid state: validation reads each file as the migration rewrites it.
        store = state.StateStore(self.repo, self.p)
        for kind in ("contributions", "projects", "evidence", "events", "operations", "commands"):
            store.load_all(kind)

    def test_the_record_migrates_once_and_only_the_field_changes(self):
        written = state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        self.assertEqual(sorted(written), MIGRATED)
        for rel in CONTRIBUTIONS:
            self.assertEqual(self.read(rel), renamed(fixture(rel)), rel)
        # The task in flight is the same task: its identity, kind, attempt,
        # deadline, and graph are untouched.
        before, after = fixture(D2VM)["active_work"], self.read(D2VM)["active_task"]
        self.assertEqual(after, dict(before, charged=["task_total"]))
        self.assertEqual(before["charged"], ["work_total"])
        self.assertEqual(tasks.graph_id(after["dedupe_key"]), tasks.graph_id(IN_FLIGHT))
        self.assertEqual(self.read(D2VM)["updated_at"], fixture(D2VM)["updated_at"])

    def test_one_event_per_record_names_what_it_renamed(self):
        state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        renamed_places = {D2VM: ["active_work", "active_work.charged=work_total"],
                          CISO: ["active_work"]}
        for rel, event_path in ((D2VM, D2VM_EVENT), (CISO, CISO_EVENT)):
            event = self.read(event_path)
            self.assertEqual((event["kind"], event["cause"], event["from"], event["to"],
                              event["revision_bumped"], event["at"]),
                             (state.NAMES_MIGRATED,
                              {"type": "migration", "id": lifecycle.TASK_RENAME}, {}, {}, False,
                              NOW))
            self.assertEqual(event["details"], {
                "from_state": engine.state_key(fixture(rel)),
                "to_state": engine.state_key(fixture(rel)), "renamed": renamed_places[rel],
                "evidence": [], "projects": []})
            self.assertEqual(event["contribution_id"], fixture(rel)["contribution_id"])
        self.assertFalse(status_ledger.notifies(self.read(D2VM_EVENT), "development.agent_running",
                                                "development.agent_running"))

    def test_the_events_and_the_other_state_are_never_rewritten(self):
        state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        modified = git(self.repo, "diff", "--name-only").split("\n")
        added = git(self.repo, "ls-files", "--others", "--exclude-standard").split("\n")
        self.assertEqual(sorted(modified), sorted(CONTRIBUTIONS))
        self.assertEqual(sorted(added), sorted([D2VM_EVENT, CISO_EVENT]))

    def test_the_migration_is_idempotent(self):
        state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        self.commit()
        self.assertEqual(state.pending_record_migrations(self.repo, self.p), [])
        self.assertEqual(state.apply_pending_record_migrations(self.repo, self.p, now=NOW), [])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        for rel in CONTRIBUTIONS:
            self.assertFalse(state.needs_name_migration("contributions", self.read(rel)))

    def test_the_controller_commits_only_the_migration(self):
        report = self.repo / "report.json"
        stderr = io.StringIO()
        with redirect_stderr(stderr), mock.patch("reconcile.datetime") as clock, \
                mock.patch.object(collect, "Api", side_effect=AssertionError("no API")):
            clock.now.return_value = datetime.fromisoformat(NOW)
            clock.fromisoformat = datetime.fromisoformat
            code = reconcile.cmd_reconcile(SimpleNamespace(root=str(self.repo),
                                                           report=str(report)))
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        written = json.loads(report.read_text())
        self.assertEqual(sorted(written["written"]), MIGRATED)
        self.assertEqual(written["requested_effects"], [])
        self.assertEqual(self.read(D2VM)["active_task"]["dedupe_key"], IN_FLIGHT)

    def test_the_state_publisher_accepts_the_migration_commit(self):
        state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        candidate = self.commit()
        report = state.validate_state_diff(self.repo, self.base, candidate, self.p)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(sorted(report.changed), MIGRATED)
        # Writing the historic name again is refused by the same final check.
        (self.repo / D2VM).write_text(canonical_json(fixture(D2VM)))
        old = self.commit("old")
        problems = state.validate_state_diff(self.repo, candidate, old, self.p).problems
        self.assertTrue(any("awaits the record migration" in p for p in problems), problems)

    def test_a_field_under_both_names_fails_closed(self):
        record = dict(fixture(D2VM), active_task=None)
        with self.assertRaises(state.StateError):
            state.migrate_names("contributions", record)
        (self.repo / D2VM).write_text(canonical_json(record))
        with self.assertRaises(state.StateError):
            state.pending_record_migrations(self.repo, self.p)


class TestTheOtherStoredNames(unittest.TestCase):
    """The key of the held patch and the values of the pending action."""

    def record(self, **fields):
        return dict(fixture(D2VM), **fields)

    def test_the_pending_action_values_are_renamed(self):
        pending = {"kind": "work_failed", "requested_at": NOW, "reminded_at": None,
                   "escalated_at": None, "counter": "work_recovery"}
        new, places = state.migrate_names("contributions",
                                          self.record(pending_human_action=pending))
        self.assertEqual((new["pending_human_action"]["kind"],
                          new["pending_human_action"]["counter"]),
                         ("task_failed", "task_recovery"))
        self.assertEqual(places, ["active_work", "active_work.charged=work_total",
                                  "pending_human_action.kind=work_failed",
                                  "pending_human_action.counter=work_recovery"])
        new, _places = state.migrate_names("contributions", self.record(
            pending_human_action=dict(pending, kind="work_budget_exhausted",
                                      counter="work_total")))
        self.assertEqual((new["pending_human_action"]["kind"],
                          new["pending_human_action"]["counter"]),
                         ("task_budget_exhausted", "task_total"))

    def test_the_budgets_a_lost_dispatch_gives_back_are_renamed(self):
        record = self.record()
        record["active_work"] = dict(record["active_work"], charged=[
            "work_total", "repair_attempts_cluster:build"])
        record["pending_human_action"] = {
            "kind": "dispatch_failed", "requested_at": NOW, "reminded_at": None,
            "escalated_at": None, "counter": "dispatch_recovery",
            "credit": ["work_total", "repair_attempts_cluster:build"]}
        new, places = state.migrate_names("contributions", record)
        self.assertEqual(new["active_task"]["charged"],
                         ["task_total", "repair_attempts_cluster:build"])
        self.assertEqual(new["pending_human_action"]["credit"],
                         ["task_total", "repair_attempts_cluster:build"])
        self.assertEqual(places, ["active_work", "active_work.charged=work_total",
                                  "pending_human_action.credit=work_total"])

    def test_the_current_names_are_left_alone(self):
        current = renamed(fixture(D2VM))
        self.assertEqual(state.migrate_names("contributions", current), (current, []))

    def test_every_renamed_name_is_read_under_its_current_name(self):
        for old, new in lifecycle.TASK_RENAME_NAMES.items():
            self.assertEqual(lifecycle.current_name(old), new)
            self.assertNotEqual(old, new)
        self.assertEqual(set(lifecycle.RENAMES), {lifecycle.PLAN_RENAME, lifecycle.TASK_RENAME})
        verbs = {e.verb for e in lifecycle.table_edges()}
        self.assertTrue({"allocate_task_retry", "exhaust_task_total"} <= verbs)
        self.assertFalse({"allocate_work_retry", "exhaust_work_total"} & verbs)
        self.assertTrue({"task_total", "task_recovery"} <= set(lifecycle.BUDGETS))

    def test_a_historic_event_is_read_under_the_current_verb(self):
        event = fixture(".state/events/luhenry--d2vm--3/00000039.json")
        event = dict(event, kind="allocate_work_retry",
                     details=dict(event["details"], verb="allocate_work_retry"))
        text = status_ledger.table_message(event, renamed(fixture(D2VM)))
        self.assertTrue(text.startswith("`allocate_task_retry` "), text)
        # The event itself is never rewritten.
        self.assertEqual(event["kind"], "allocate_work_retry")


class TestAnInFlightTaskStillFoldsItsResult(StateCopy):
    """The Result of the task in flight is folded as before, whether the
    record is read before or after the migration."""

    def folded(self, record):
        pub = result(record, "published")
        pub["task"].update(generation=1, revision=13, dedupe_key=IN_FLIGHT)
        self.assertIsNone(reconcile.result_mismatch(record, pub))
        output = tick(record, [pub], capacity_ok=False)
        self.assertNotIn("record_late_effect", [e["kind"] for e in events_of(output)])
        return final(output, record), [e["kind"] for e in events_of(output)]

    def test_the_result_folds_before_and_after_the_migration(self):
        before = state.StateStore(self.repo, self.p).load_all("contributions")[D2VM]
        self.assertEqual(before["active_task"]["dedupe_key"], IN_FLIGHT)
        folded_before = self.folded(before)
        self.assertIsNone(folded_before[0]["active_task"])
        state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        self.commit()
        after = state.StateStore(self.repo, self.p).load_all("contributions")[D2VM]
        self.assertEqual(after, self.read(D2VM))
        self.assertEqual(after, before)
        self.assertEqual(self.folded(after), folded_before)

    def test_a_result_of_another_task_is_still_late(self):
        record = state.StateStore(self.repo, self.p).load_all("contributions")[D2VM]
        pub = result(record, "published")
        pub["task"].update(generation=1, revision=13,
                           dedupe_key="luhenry--d2vm/3@g1.r13:development_patch")
        self.assertEqual(reconcile.result_mismatch(record, pub),
                         "late Result without matching a requested task")


class TestTheDispatcherReadsTheRecord(StateCopy):
    """`python -m tasks plan` reads the committed record directly, before or
    after the controller migrates it."""

    def drop_task_file(self):
        """A task whose file the controller has not committed yet."""
        (self.repo / task_build.path(task_build.evidence_id(IN_FLIGHT))).unlink()
        self.commit("without the task file")

    def plan(self):
        out = self.repo / "plan.json"
        args = SimpleNamespace(root=str(self.repo), out=str(out), control_repo_id="1")
        stdout = io.StringIO()
        # The pinned control commit is the same before and after.
        with redirect_stdout(stdout), mock.patch("subprocess.run", return_value=(
                subprocess.CompletedProcess([], 0, stdout="c" * 40))):
            code = tasks.cmd_plan(args)
        self.assertEqual(code, 0)
        plan = json.loads(out.read_text()) if out.exists() else None
        out.unlink(missing_ok=True)
        return json.loads(stdout.getvalue()), plan

    def test_a_task_without_its_file_is_deferred_before_and_after_the_migration(self):
        self.drop_task_file()
        self.assertEqual(state.pending_record_migrations(self.repo, self.p), sorted(CONTRIBUTIONS))
        before, plan_before = self.plan()
        state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        self.commit()
        self.assertEqual(state.pending_record_migrations(self.repo, self.p), [])
        after, plan_after = self.plan()
        self.assertEqual((after, plan_after), (before, plan_before))
        # The task in flight is read from the record, which the dispatcher
        # reports instead of failing on it.
        self.assertEqual([(d["contribution"], d["task"]) for d in after["deferred"]],
                         [("luhenry--d2vm/3", "development_repair")])

    def test_the_plan_is_the_same_before_and_after_the_migration(self):
        self.assertEqual(state.pending_record_migrations(self.repo, self.p), sorted(CONTRIBUTIONS))
        before, plan_before = self.plan()
        self.assertEqual([(s["contribution"], s["task"]) for s in before["selected"]],
                         [("luhenry--d2vm/3", "development_repair")], before)
        state.apply_pending_record_migrations(self.repo, self.p, now=NOW)
        self.commit()
        self.assertEqual(state.pending_record_migrations(self.repo, self.p), [])
        after, plan_after = self.plan()
        self.assertEqual(after, before)
        # The queued payload is the same bytes: the plan names the same task
        # id and graph, with the same payload.
        [node] = plan_after["nodes"]
        self.assertEqual((node["graph_id"], node["payload"]["dedupe_key"], node["payload"]["task"]),
                         (tasks.graph_id(IN_FLIGHT), IN_FLIGHT, "development_repair"))
        self.assertEqual(plan_after, plan_before)
        self.assertNotIn("active_work", json.dumps(plan_after))


class TestThePublishJobReadsTheRecord(StateCopy):
    """The publish job reads the record from the artifact the controller's
    checkout produced: a record not yet migrated (an old controller run
    rebuilt the state commit on top of the merge) is written under the
    current names, so the publication check accepts the task in flight."""

    def test_publication_check_passes_on_an_old_named_record_written_through_write_record(self):
        import artifact
        import lifecycle
        raw = self.read(D2VM)
        self.assertIn("active_work", raw)
        task = {"contribution_id": raw["contribution_id"]}
        out = self.repo / "artifact"
        out.mkdir()
        (out / "task.json").write_text(json.dumps(task))
        artifact.write_record(self.repo, out / "task.json", out)
        record = json.loads((out / "record.json").read_text())
        self.assertNotIn("active_work", record)
        self.assertEqual(record["active_task"]["dedupe_key"], IN_FLIGHT)
        self.assertEqual(record, state.migrate_names("contributions", raw)[0])
        fork = record["fork"]
        why = lifecycle.publication_check(
            record, expected_revision=record["revision"],
            expected_head=fork["head_sha"], observed_head=fork["head_sha"],
            ref=lifecycle.work_ref(record), repository=fork["name"],
            expected_generation=record["generation"], task_key=IN_FLIGHT)
        self.assertIsNone(why)


if __name__ == "__main__":
    unittest.main()
