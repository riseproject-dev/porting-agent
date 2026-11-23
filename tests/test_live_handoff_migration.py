# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The live closeio--ciso8601/1 record reaches `handoff.awaiting_submission`.

The fixtures under ``tests/fixtures/live-handoff-r6`` are copies of the
record, its last two events and its CI evidence as the controller committed
them: phase ``handoff``, activity ``waiting_human``, revision 6, pending
action ``approve_handoff``. The ordinary controller must bring that record,
by itself, to the table state, ask for ``submit-upstream`` on the tracking
issue, and never lose the fork, the development PR, the head or the evidence.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import capacity
import collect
import compat
import controller
import engine
import lifecycle
import observe_ci
import reconcile
import state
from common import canonical_json
from tests.helpers import OWNER, commit_files, git, init_repo, policy

FIXTURES = Path(__file__).parent / "fixtures" / "live-handoff-r6"
CID = "closeio--ciso8601/1"
HEAD = "b29a8f49374004d650cf312e9c31848953e5ae60"
FORK = {"head_ref": "riscv64", "head_sha": HEAD, "name": "riseproject-upstreaming/ciso8601",
        "pr_number": 1, "repository_id": 1467208233}
CI_EVIDENCE = "ci-1467208233-45008094068-1"
RECORD_PATH = ".state/contributions/closeio--ciso8601--1.json"
EVIDENCE_PATH = f".state/evidence/{CI_EVIDENCE}.json"
EVENT_PATH = ".state/events/closeio--ciso8601--1/00000023.json"
NOW = "2026-11-23T09:00:00+00:00"
T = datetime.fromisoformat(NOW)
SUBMIT = f"/upstreaming submit-upstream revision=6 head={HEAD}"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def live():
    return load("contribution.json")


def migrated():
    record, _event = compat.migrate_handoff_wait(live(), evidence=load("evidence-ci.json"),
                                                 policy=policy(), now=NOW)
    return record


def comment(body, cid=900):
    return {"issue_number": 1, "comment": {"id": cid, "body": body, "user": dict(OWNER)}}


class TestLiveFixtureIsTheCommittedRecord(unittest.TestCase):
    def test_fixture_shape(self):
        record = live()
        self.assertEqual((record["phase"], record["activity"], record["revision"]),
                         ("handoff", "waiting_human", 6))
        self.assertEqual(record["pending_human_action"]["kind"], "approve_handoff")
        self.assertEqual(record["fork"], FORK)
        self.assertEqual(record["last_ci_evidence_id"], CI_EVIDENCE)
        self.assertEqual([load(f"event-{n}.json")["kind"] for n in (21, 22)],
                         ["worker_no_change", "handoff_ready"])
        evidence = load("evidence-ci.json")
        self.assertEqual((evidence["evidence_id"], evidence["ci"]["head_sha"]), (CI_EVIDENCE, HEAD))
        # The old wait is not a table state; only its migration admits it.
        self.assertIsNotNone(engine.state_problem(record))
        self.assertTrue(compat.needs_migration(record))
        self.assertTrue(compat.is_handoff_wait(record))


class TestRecordMigration(unittest.TestCase):
    def test_migrated_record_is_the_table_state_and_keeps_everything_else(self):
        before = live()
        after, event = compat.migrate_handoff_wait(before, evidence=load("evidence-ci.json"),
                                                   policy=policy(), now=NOW)
        self.assertEqual(engine.state_key(after), "handoff.awaiting_submission")
        self.assertIsNone(engine.state_problem(after))
        self.assertEqual(after["pending_human_action"],
                         {"kind": "submit_upstream", "requested_at": NOW, "reminded_at": None,
                          "escalated_at": None, "counter": None})
        self.assertEqual((after["revision"], after["generation"]), (6, 0))
        self.assertEqual(after["fork"], FORK)
        self.assertEqual(after["handoff_request"],
                         {"command_id": f"observation-{CI_EVIDENCE}", "head": HEAD, "revision": 6})
        self.assertEqual(after["last_ci_evidence_id"], CI_EVIDENCE)
        self.assertEqual(after["dossier_evidence_id"], "dossier-closeio--ciso8601--1-r2")
        self.assertIsNone(after["active_work"])
        self.assertIsNone(after["handoff_approval"])
        # Exactly these fields differ; every other field is byte-for-byte kept.
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        self.assertEqual(changed, {"activity", "pending_human_action", "clean_head_sha",
                                   "updated_at", "budget", "automatic_refresh_used"})
        self.assertEqual(after["budget"], dict(before["budget"], investigation_attempts=0))
        # The clean head is produced by the debug cleanup after submission is
        # requested; the candidate head stays in the fork and the request.
        self.assertIsNone(after["clean_head_sha"])
        for name in ("paused_at", "paused_reason", "paused_by", "admission_override"):
            self.assertIn(name, after)
            self.assertIsNone(after[name])
        self.assertEqual(event["kind"], compat.HANDOFF_WAIT_MIGRATED)
        self.assertEqual(event["cause"], {"type": "migration", "id": compat.HANDOFF_WAIT_MIGRATION})
        self.assertEqual((event["from"], event["to"]),
                         ({"activity": "waiting_human"}, {"activity": "awaiting_submission"}))
        self.assertFalse(event["revision_bumped"])
        self.assertEqual((event["details"]["head"], event["details"]["revision"]), (HEAD, 6))
        self.assertEqual((event["details"]["from_state"], event["details"]["to_state"]),
                         ("handoff.waiting_human", "handoff.awaiting_submission"))

    def test_migration_equals_the_nomination_the_table_defines(self):
        """The same CI on a record that was never in the old wait gives the
        same state, so the migrated record is not a special case."""
        before = live()
        waiting_ci = compat.legacy_update(
            dict(before, phase="development", activity="idle"), "observe_ci.ci_passed",
            activity="waiting_ci", handoff_request=None, pending_human_action=None,
            clean_head_sha=None)
        out = engine.apply_transition(
            waiting_ci, "nominate_for_handoff", policy=policy(), now=NOW, cause_id=CI_EVIDENCE,
            facts={"ci_passed_on_head", "no_pending_actionable_feedback"},
            values={"last_ci_evidence_id": CI_EVIDENCE,
                    "handoff_request": dict(before["handoff_request"])})
        after = migrated()
        added = {"paused_at", "paused_reason", "paused_by", "admission_override",
                 "automatic_refresh_used", "budget"}
        self.assertEqual({k: v for k, v in out.record.items() if k not in added},
                         {k: v for k, v in after.items() if k not in added})

    def test_any_other_handoff_shape_fails_closed(self):
        evidence = load("evidence-ci.json")
        record = live()
        variants = {
            "approved": dict(record, pending_human_action=dict(record["pending_human_action"],
                                                               kind="open_upstream_pr")),
            "requested": dict(record, activity="idle", pending_human_action=None),
            "work": dict(record, active_work={"dedupe_key": "k", "task": "development_repair",
                                              "revision": 6, "requested_at": NOW,
                                              "deadline_at": NOW}),
            "operation": dict(record, active_operation_id="op-1"),
            "publication": dict(record, pending_publication={
                "worker_run_id": "1", "attempts": 1, "last_reason": "x", "next_attempt_at": NOW}),
            "paused": dict(record, paused=True),
            "blocked": dict(record, blocked_reason="x"),
            "head": dict(record, fork=dict(record["fork"], head_sha="c" * 40)),
            "clean head": dict(record, clean_head_sha="c" * 40),
            "revision": dict(record, revision=7),
            "no evidence": dict(record, last_ci_evidence_id=None),
            "pending feedback": dict(record, pending_feedback_evidence_ids=["review-1"]),
            "no PR": dict(record, fork=dict(record["fork"], pr_number=None)),
            "debug": dict(record, debug_commits=[{"sha": HEAD, "subject": "{debug} x",
                                                  "reason": "r"}]),
            "upstream PR": dict(record, upstream_pr={"id": 1, "number": 1, "url": "u",
                                                     "head_repository_id": 1, "head_ref": "riscv64",
                                                     "head_sha": HEAD, "base_ref": "master",
                                                     "registered_by": OWNER}),
        }
        for name, variant in variants.items():
            with self.subTest(shape=name), self.assertRaises(lifecycle.TransitionError):
                compat.migrate_handoff_wait(variant, evidence=evidence, policy=policy(), now=NOW)
        bad_evidence = {
            "missing": None,
            "other id": dict(evidence, evidence_id="ci-1-2-3"),
            "other head": dict(evidence, ci=dict(evidence["ci"], head_sha="c" * 40)),
            "failed": dict(evidence, summary="run 1 attempt 1: failed"),
            "not CI": dict(evidence, kind="rehearsal"),
        }
        for name, bad in bad_evidence.items():
            with self.subTest(evidence=name), self.assertRaises(lifecycle.TransitionError):
                compat.migrate_handoff_wait(record, evidence=bad, policy=policy(), now=NOW)
        # A record that is already in a table state is not migrated again.
        with self.assertRaises(lifecycle.TransitionError):
            compat.migrate_handoff_wait(migrated(), evidence=evidence, policy=policy(), now=NOW)
        self.assertFalse(compat.needs_migration(migrated()))


class TestControllerMigratesByItself(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = init_repo(Path(self.tmp.name) / "control")
        self.files = {
            RECORD_PATH: (FIXTURES / "contribution.json").read_text(),
            EVIDENCE_PATH: (FIXTURES / "evidence-ci.json").read_text(),
            ".state/events/closeio--ciso8601--1/00000021.json": (FIXTURES / "event-21.json").read_text(),
            ".state/events/closeio--ciso8601--1/00000022.json": (FIXTURES / "event-22.json").read_text(),
        }
        self.base = commit_files(self.repo, dict(self.files, **{"README.md": "x\n"}))
        self.p = policy()

    def tearDown(self):
        self.tmp.cleanup()

    def run_reconcile(self):
        report = self.repo / "report.json"
        stderr = io.StringIO()
        args = SimpleNamespace(root=str(self.repo), report=str(report))
        with redirect_stderr(stderr), \
             mock.patch("reconcile.datetime") as clock, \
             mock.patch.object(collect, "Api", side_effect=AssertionError("no API in a migration")):
            clock.now.return_value = T
            clock.fromisoformat = datetime.fromisoformat
            result = reconcile.cmd_reconcile(args)
        return result, (json.loads(report.read_text()) if report.exists() else None), stderr.getvalue()

    def test_reconcile_stages_only_the_record_and_its_event(self):
        self.assertEqual(state.pending_record_migrations(self.repo, self.p), [RECORD_PATH])
        result, report, stderr = self.run_reconcile()
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(sorted(report["written"]), [RECORD_PATH, EVENT_PATH])
        self.assertEqual(report["record_migration"], {"written": report["written"]})
        # A migration-only tick launches nothing.
        self.assertEqual(report["controller"], {"canary_recovery_requests": []})
        for key in ("requested_effects", "dispatch_relaunch", "provision_requests",
                    "publication_retries", "dev_pr_requests"):
            self.assertEqual(report[key], [], key)
        changed = sorted(line.strip() for line in git(
            self.repo, "status", "--porcelain", "--untracked-files=all").split("\n"))
        self.assertEqual(changed, [f"?? {EVENT_PATH}", "?? report.json", f"M {RECORD_PATH}"])
        record = json.loads((self.repo / RECORD_PATH).read_text())
        self.assertEqual(record, compat.migrate_handoff_wait(
            live(), evidence=load("evidence-ci.json"), policy=self.p, now=T.isoformat())[0])
        event = json.loads((self.repo / EVENT_PATH).read_text())
        self.assertEqual((event["sequence"], event["kind"]), (23, compat.HANDOFF_WAIT_MIGRATED))
        # Earlier events are untouched: the history is append-only.
        for path, text in self.files.items():
            if "/events/" in path or "/evidence/" in path:
                self.assertEqual((self.repo / path).read_text(), text)

    def test_the_state_publisher_accepts_the_migration_commit(self):
        self.assertEqual(self.run_reconcile()[0], 0)
        (self.repo / "report.json").unlink()
        git(self.repo, "add", ".state")
        git(self.repo, "commit", "-q", "-m", "state")
        candidate = git(self.repo, "rev-parse", "HEAD")
        report = state.validate_state_diff(self.repo, self.base, candidate, self.p)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(sorted(report.changed), [RECORD_PATH, EVENT_PATH])
        # Writing the old wait again is refused by the same final check.
        old = commit_files(self.repo, {RECORD_PATH: canonical_json(dict(live(), revision=6,
                                                                       updated_at=NOW))})
        problems = state.validate_state_diff(self.repo, candidate, old, self.p).problems
        self.assertTrue(any("awaits record migration" in p for p in problems), problems)

    def test_migration_is_idempotent(self):
        self.assertEqual(self.run_reconcile()[0], 0)
        self.assertEqual(state.pending_record_migrations(self.repo, self.p), [])
        self.assertEqual(state.apply_pending_record_migrations(self.repo, self.p, now=NOW), [])
        self.assertFalse((self.repo / ".state/events/closeio--ciso8601--1/00000024.json").exists())
        store = state.StateStore(self.repo, self.p)
        self.assertEqual(len(store.load_all("contributions")), 1)
        self.assertEqual(len(store.load_all("events")), 3)

    def test_observe_reports_the_pending_migration_without_writing(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = controller.cmd_observe(SimpleNamespace(root=str(self.repo), offline=True))
        self.assertEqual((result, stderr.getvalue()), (0, ""))
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["record_migration"], {"written": [], "pending": [RECORD_PATH]})
        self.assertEqual(report["requested_effects"], [])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_state_validate_admits_only_the_exact_pending_shape(self):
        store = state.StateStore(self.repo, self.p)
        self.assertEqual(len(store.load_all("contributions")), 1)
        approved = dict(live(), pending_human_action=dict(live()["pending_human_action"],
                                                          kind="open_upstream_pr"))
        (self.repo / RECORD_PATH).write_text(canonical_json(approved))
        with self.assertRaises(state.StateError):
            store.load_all("contributions")

    def test_an_unknown_handoff_shape_stops_the_controller(self):
        approved = dict(live(), pending_human_action=dict(live()["pending_human_action"],
                                                          kind="open_upstream_pr"))
        text = canonical_json(approved)
        (self.repo / RECORD_PATH).write_text(text)
        result, report, stderr = self.run_reconcile()
        self.assertEqual(result, 1)
        self.assertIsNone(report)
        self.assertIn("record migration refused", stderr)
        self.assertIn("no reviewed migration for this handoff record", stderr)
        self.assertEqual((self.repo / RECORD_PATH).read_text(), text)
        self.assertFalse((self.repo / EVENT_PATH).exists())
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(controller.cmd_observe(
                SimpleNamespace(root=str(self.repo), offline=True)), 1)

    def test_missing_ci_evidence_stops_the_controller(self):
        (self.repo / EVIDENCE_PATH).unlink()
        result, _report, stderr = self.run_reconcile()
        self.assertEqual(result, 1)
        self.assertIn("CI evidence", stderr)
        self.assertEqual((self.repo / RECORD_PATH).read_text(), self.files[RECORD_PATH])


class TestMigratedRecordUnderTheOrdinaryController(unittest.TestCase):
    """After the migration, ticks keep the record; only table verbs move it."""

    def tick(self, record, *, comments=(), publications=(), snapshot=None, head=HEAD, feedback=None,
             pressure=True):
        contributions = {"c": record}
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": head}), \
             mock.patch("observe_ci.collect.development_review_feedback", return_value=feedback), \
             mock.patch("observe_ci.collect.ci_snapshot", return_value=snapshot or self.passed()):
            return reconcile.tick(
                object(), reconcile.Snapshot(contributions, {}, {}, {}, {CI_EVIDENCE: load("evidence-ci.json")}),
                policy=policy(), now=T, publications=list(publications), comments=list(comments),
                pressure=capacity.Pressure(ok=pressure), dispatcher_runs=[], queue_works={})

    def passed(self, head=HEAD, conclusion="success", status="completed"):
        run = {"id": 45008094068, "head_sha": head, "run_attempt": 1, "status": status,
               "conclusion": conclusion if status == "completed" else None, "workflow_id": 426522359,
               "event": "pull_request", "path": ".github/workflows/lint.yml"}
        jobs = [{"id": 1, "run_id": run["id"], "name": "Build - ubuntu-24.04-riscv (riscv64)",
                 "status": status, "conclusion": run["conclusion"], "started_at": "x",
                 "labels": ["ubuntu-24.04-riscv"]}]
        return collect.CiSnapshot(repository={"id": FORK["repository_id"], "full_name": FORK["name"]},
                                  head_sha=head, runs=[run], jobs_by_run={run["id"]: jobs},
                                  artifacts_by_run={})

    def final(self, output, record):
        records = [r for kind, r in output.staged if kind == "contributions"]
        return records[-1] if records else record

    def events(self, output):
        return [e["kind"] for kind, e in output.staged if kind == "events"]

    def test_repeated_ticks_request_no_work_and_change_nothing(self):
        record = migrated()
        for snapshot in (self.passed(), self.passed(status="in_progress")):
            with self.subTest(status=snapshot.runs[0]["status"]):
                output = self.tick(record, snapshot=snapshot)
                after = self.final(output, record)
                self.assertEqual({k: v for k, v in after.items() if k != "updated_at"},
                                 {k: v for k, v in record.items() if k != "updated_at"})
                self.assertEqual(self.events(output), [])
                self.assertEqual(output.report["requested_effects"], [])
                self.assertEqual(output.report["dispatch_relaunch"], [])
                self.assertEqual(output.report["controller"]["selected"], [])
                for key in ("provision_requests", "dev_pr_requests",
                            "publication_retries"):
                    self.assertEqual(output.report[key], [], key)

    def test_no_work_is_ever_selected_for_the_gate(self):
        for record in (migrated(), dict(migrated(), paused=True, paused_at=NOW, paused_by="human")):
            result = controller.tick(
                contributions={"c": record}, operations={}, consumed={}, projects={},
                policy=policy(), inputs=controller.Inputs(now=T, pressure=capacity.Pressure(ok=True)))
            self.assertEqual(result.requested_effects, [])
            self.assertEqual(result.staged, [])
            self.assertIsNone(controller._actionable(record))

    def test_submit_upstream_is_checked_against_the_table_then_held(self):
        record = migrated()
        output = self.tick(record, comments=[comment(SUBMIT)])
        receipt = next(r for kind, r in output.staged if kind == "commands")
        self.assertEqual((receipt["verb"], receipt["result"], receipt["revision"]),
                         ("submit-upstream", "rejected", 6))
        self.assertEqual(receipt["reason"], engine.HELD_COMMANDS["submit-upstream"])
        self.assertEqual(self.final(output, record), record)
        self.assertEqual(self.events(output), [])
        self.assertEqual(output.report["requested_effects"], [])
        wrong = (
            (f"/upstreaming submit-upstream revision=5 head={HEAD}", "stale revision 5, current is 6"),
            (f"/upstreaming submit-upstream revision=6 head={'c' * 40}",
             "head is not the candidate head"),
        )
        for body, reason in wrong:
            with self.subTest(body=body):
                output = self.tick(record, comments=[comment(body)])
                receipt = next(r for kind, r in output.staged if kind == "commands")
                self.assertEqual((receipt["result"], receipt["reason"]), ("rejected", reason))
                self.assertEqual(self.final(output, record), record)

    def test_retired_and_inapplicable_commands_are_rejected_and_change_nothing(self):
        record = migrated()
        cases = (
            (f"/upstreaming approve-handoff revision=6 head={HEAD}", "`approve-handoff` is retired"),
            (f"/upstreaming request-handoff revision=6 head={HEAD}", "`request-handoff` is retired"),
            ("/upstreaming register-upstream-pr revision=6 url=https://github.com/closeio/ciso8601/pull/9",
             "`register-upstream-pr` is retired"),
            ('/upstreaming abandon revision=6 reason="x"',
             "`/upstreaming abandon` does not apply in `handoff.awaiting_submission`; it applies "
             "in `parked.idle`"),
            ('/upstreaming restart revision=6 reason="x"', "`restart` is retired"),
            ('/upstreaming block revision=6 reason="x"', "`block` is retired"),
            ('/upstreaming resume revision=6 reason="x"', "not paused"),
            (f"/upstreaming retract-pr revision=6 head={HEAD}",
             "`/upstreaming retract-pr` does not apply in `handoff.awaiting_submission`; it "
             "applies in `handoff.waiting_upstream_pr`"),
            (f'/upstreaming refresh revision=6 head={HEAD} reason="x"',
             "`head` does not apply to `/upstreaming refresh` in `handoff.awaiting_submission`"),
            ("/upstreaming resume revision=6 class=optimized_backend reason=x",
             "not paused"),
            ('/upstreaming force-develop revision=6 class=configuration_integration paths=a '
             'reason="x"', "`force-develop` is retired."),
        )
        for body, reason in cases:
            with self.subTest(body=body):
                output = self.tick(record, comments=[comment(body)])
                receipt = next(r for kind, r in output.staged if kind == "commands")
                self.assertEqual(receipt["result"], "rejected")
                self.assertIn(reason, receipt["reason"])
                if "`restart`" in reason:
                    self.assertIn('/upstreaming resume revision=N reason="..."', receipt["reason"])
                elif "`force-develop`" in reason:
                    self.assertIn("/upstreaming resume revision=N class=", receipt["reason"])
                elif "retired" in reason and "block" not in reason:
                    self.assertIn("/upstreaming submit-upstream revision=N head=FULL_SHA",
                                  receipt["reason"])
                elif "retired" in reason:
                    self.assertIn('/upstreaming pause revision=N reason="..."', receipt["reason"])
                self.assertEqual(self.final(output, record), record)
                self.assertEqual(self.events(output), [])

    def test_pause_and_resume_keep_the_submission(self):
        record = migrated()
        output = self.tick(record, comments=[comment('/upstreaming pause reason="travel"')])
        paused = self.final(output, record)
        self.assertEqual(self.events(output), ["pause"])
        self.assertEqual((engine.state_key(paused), paused["paused"], paused["paused_by"],
                          paused["paused_reason"], paused["revision"]),
                         ("handoff.awaiting_submission", True, "human", "travel", 6))
        held = self.tick(paused, comments=[comment(SUBMIT, cid=901)])
        receipt = next(r for kind, r in held.staged if kind == "commands")
        self.assertEqual((receipt["result"], receipt["reason"]), ("rejected", "contribution is paused"))
        output = self.tick(paused, comments=[comment('/upstreaming resume revision=6 reason="back"', cid=902)])
        resumed = self.final(output, paused)
        self.assertEqual(self.events(output), ["resume_pause"])
        self.assertEqual((engine.state_key(resumed), resumed["paused"], resumed["revision"]),
                         ("handoff.awaiting_submission", False, 7))
        for name in ("fork", "handoff_request", "last_ci_evidence_id", "dossier_evidence_id",
                     "pending_human_action", "budget"):
            self.assertEqual(resumed[name], record[name], name)

    def test_park_and_refresh_follow_the_table(self):
        record = migrated()
        output = self.tick(record, comments=[comment('/upstreaming park revision=6 reason="stop"')])
        parked = self.final(output, record)
        self.assertEqual(self.events(output), ["park_contribution"])
        self.assertEqual((engine.state_key(parked), parked["parked_reason"], parked["previous_phase"]),
                         ("parked.idle", "stop", "handoff"))
        self.assertEqual((parked["fork"], parked["last_ci_evidence_id"]), (FORK, CI_EVIDENCE))
        output = self.tick(record, comments=[comment('/upstreaming refresh revision=6 reason="new base"')])
        refreshed = next(r for kind, r in output.staged
                         if kind == "contributions" and r["refresh"])
        self.assertIn("refresh_investigation", self.events(output))
        self.assertEqual(refreshed["refresh"],
                         {"command_id": "comment-900", "reason": "new base", "from_phase": "handoff",
                          "head_sha": HEAD, "pr_number": 1})
        self.assertEqual((refreshed["revision"], refreshed["fork"]), (7, FORK))
        self.assertIsNone(refreshed["handoff_request"])

    def test_a_new_head_returns_to_development_ci_with_the_fork_kept(self):
        record = migrated()
        new_head = "c" * 40
        output = self.tick(record, head=new_head, snapshot=self.passed(new_head, status="in_progress"))
        after = self.final(output, record)
        self.assertEqual(self.events(output), ["record_head_change"])
        self.assertEqual((engine.state_key(after), after["revision"]), ("development.waiting_ci", 7))
        self.assertEqual(after["fork"], dict(FORK, head_sha=new_head))
        self.assertEqual((after["last_ci_evidence_id"], after["handoff_request"],
                          after["pending_human_action"]), (None, None, None))
        self.assertEqual(output.report["requested_effects"], [])

    def test_a_ci_failure_on_the_candidate_head_returns_for_repair(self):
        record = migrated()
        output = self.tick(record, snapshot=self.passed(conclusion="failure"), pressure=False)
        after = self.final(output, record)
        self.assertEqual(self.events(output), ["return_for_repair"])
        self.assertEqual((engine.state_key(after), after["revision"]), ("development.fix_needed", 7))
        self.assertEqual(after["fork"], FORK)
        self.assertEqual((after["handoff_request"], after["pending_human_action"],
                          after["clean_head_sha"]), (None, None, None))
        self.assertEqual(after["dossier_evidence_id"], record["dossier_evidence_id"])
        # The row records the evidence of the failure, and no feedback.
        self.assertEqual(after["last_ci_evidence_id"], CI_EVIDENCE)
        self.assertEqual(after["pending_feedback_evidence_ids"], [])

    REVIEW = {"review_ids": [77], "comment_ids": [], "conversation_comment_ids": [],
              "reviews": [{"id": 77, "body": "Please drop the tox change."}],
              "comments": [], "conversation_comments": [],
              "inquiry_comment_ids": [], "inquiry_conversation_comment_ids": [],
              "inquiry_comments": [], "inquiry_conversation_comments": []}

    def test_an_actionable_review_on_the_candidate_head_is_recorded_by_its_row(self):
        record = migrated()
        for snapshot in (self.passed(), self.passed(status="in_progress")):
            with self.subTest(status=snapshot.runs[0]["status"]):
                output = self.tick(record, feedback=self.REVIEW, snapshot=snapshot,
                                   pressure=False)
                after = self.final(output, record)
                self.assertEqual(self.events(output), ["record_actionable_review"])
                self.assertEqual(engine.state_key(after), "development.fix_needed")
                # One transition, one revision.
                self.assertEqual(after["revision"], 7)
                self.assertEqual(after["fork"], FORK)
                self.assertEqual(after["processed_review_ids"], [77])
                self.assertEqual(after["pending_feedback_evidence_ids"],
                                 ["review-1467208233-1-77"])
                self.assertEqual((after["handoff_request"], after["pending_human_action"],
                                  after["clean_head_sha"]), (None, None, None))
                # The CI evidence of the candidate head is kept.
                self.assertEqual(after["last_ci_evidence_id"], CI_EVIDENCE)
                self.assertIn("review-1467208233-1-77",
                              [e["evidence_id"] for kind, e in output.staged if kind == "evidence"])
                event = next(e for kind, e in output.staged if kind == "events")
                self.assertEqual((event["revision_bumped"], event["details"]["from_state"],
                                  event["details"]["to_state"]),
                                 (True, "handoff.awaiting_submission", "development.fix_needed"))

    def test_a_ci_failure_takes_precedence_over_a_review_in_the_same_observation(self):
        record = migrated()
        output = self.tick(record, feedback=self.REVIEW,
                           snapshot=self.passed(conclusion="failure"), pressure=False)
        after = self.final(output, record)
        self.assertEqual(self.events(output), ["return_for_repair"])
        self.assertEqual((engine.state_key(after), after["revision"]), ("development.fix_needed", 7))
        # The review is not consumed: it is read again on the development record.
        self.assertEqual(after["processed_review_ids"], record["processed_review_ids"])
        self.assertEqual(after["pending_feedback_evidence_ids"], [])
        self.assertEqual(after["last_ci_evidence_id"], CI_EVIDENCE)

    def test_an_explanation_only_comment_keeps_the_gate(self):
        record = migrated()
        feedback = {"review_ids": [], "comment_ids": [], "conversation_comment_ids": [],
                    "reviews": [], "comments": [], "conversation_comments": [],
                    "inquiry_comment_ids": [], "inquiry_conversation_comment_ids": [88],
                    "inquiry_comments": [],
                    "inquiry_conversation_comments": [{"id": 88, "body": "Why riscv64 only?"}]}
        output = self.tick(record, feedback=feedback)
        after = self.final(output, record)
        self.assertEqual(self.events(output), ["record_explanation"])
        self.assertEqual(engine.state_key(after), "handoff.awaiting_submission")
        self.assertEqual((after["revision"], after["handoff_request"], after["pending_human_action"]),
                         (6, record["handoff_request"], record["pending_human_action"]))
        self.assertEqual((after["fork"], after["last_ci_evidence_id"]), (FORK, CI_EVIDENCE))
        self.assertEqual(after["processed_pr_conversation_comment_ids"], [88])
        self.assertEqual(output.report["requested_effects"], [])
        self.assertEqual(output.report["controller"]["selected"], [])
        event = next(e for kind, e in output.staged if kind == "events")
        self.assertEqual((event["revision_bumped"], event["cause"]["type"]), (False, "observation"))
        self.assertIn("inquiry-1467208233-1-88",
                      [e["evidence_id"] for kind, e in output.staged if kind == "evidence"])
        # The same comment is not recorded twice.
        again = self.tick(after)
        self.assertEqual(self.events(again), [])

    def test_an_explanation_and_a_review_in_one_observation_take_both_rows(self):
        record = migrated()
        feedback = dict(self.REVIEW, inquiry_conversation_comment_ids=[88],
                        inquiry_conversation_comments=[{"id": 88, "body": "Why riscv64 only?"}])
        output = self.tick(record, feedback=feedback, pressure=False)
        after = self.final(output, record)
        self.assertEqual(self.events(output), ["record_explanation", "record_actionable_review"])
        self.assertEqual((engine.state_key(after), after["revision"]), ("development.fix_needed", 7))
        self.assertEqual((after["processed_review_ids"],
                          after["processed_pr_conversation_comment_ids"]), ([77], [88]))

    def test_a_late_worker_result_is_recorded_and_never_folded(self):
        record = migrated()
        task = {"contribution_id": CID, "revision": 6}
        for status in ("rejected", "no_change", "published"):
            with self.subTest(status=status):
                publication = {"status": status, "task": task, "worker_run_id": "45013412209",
                               "publication_artifact_id": 14760000001, "operation": None,
                               "artifact": None}
                output = self.tick(record, publications=[publication])
                after = self.final(output, record)
                self.assertEqual(self.events(output), ["record_late_effect"])
                self.assertEqual(dict(after, processed_publications=[]),
                                 dict(record, processed_publications=[]))
                self.assertIn(14760000001, after["processed_publications"])
                self.assertEqual(output.report["requested_effects"], [])

    def test_observation_leaves_other_handoff_states_alone(self):
        record = dict(migrated(), activity="cleaning_debug", pending_human_action=None,
                      active_operation_id="op-1")
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": "c" * 40}):
            observation = observe_ci.observe(object(), contributions={"c": record}, operations={},
                                             contracts={}, policy=policy(), now=NOW)
        self.assertEqual([kind for kind, _ in observation.staged], [])
        self.assertIn("is not observed here", observation.report[0]["note"])


class TestRealStateDryRun(unittest.TestCase):
    """The committed `.state` of this repository, read without writing it."""

    def test_committed_record_is_either_the_pending_shape_or_a_table_state(self):
        root = Path(__file__).resolve().parents[1]
        record = json.loads((root / RECORD_PATH).read_text())
        if compat.needs_migration(record):
            self.assertIsNone(compat.handoff_wait_shape_problem(record))
            evidence = json.loads((root / state.record_path(
                "evidence", {"evidence_id": record["last_ci_evidence_id"]})).read_text())
            after, _event = compat.migrate_handoff_wait(record, evidence=evidence, policy=policy(),
                                                        now=datetime.now(timezone.utc).isoformat())
            self.assertEqual(engine.state_key(after), "handoff.awaiting_submission")
            self.assertEqual(after["fork"], record["fork"])
        else:
            self.assertIsNone(compat.state_problem(record))


if __name__ == "__main__":
    unittest.main()
