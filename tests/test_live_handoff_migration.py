# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The live closeio--ciso8601/1 record at the submission gate.

The fixture ``tests/fixtures/live-gate-dev/contribution.json`` is the record
as the controller committed it: `handoff.awaiting_submission`, revision 6,
development on `riscv64-dev` at b29a8f4 with development PR #2, pending
action ``submit_upstream``, written before the strict record schema (it
lacks `budget.reruns` and three handoff fields, and carries the null
`blocked_reason`). The ordinary controller adds the missing fields by
itself, in one evented record migration, and changes nothing else; after
it, only table verbs move the record, and it never loses the fork, the
development PR, the head, or the evidence.
"""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import capacity
import collect
import controller
import engine
import observe_ci
import reconcile
import state
from common import canonical_json
from tests import helpers
from tests.helpers import OWNER, commit_files, git, init_repo, policy

FIXTURES = Path(__file__).parent / "fixtures" / "live-handoff-r6"
LIVE = Path(__file__).parent / "fixtures" / "live-gate-dev" / "contribution.json"
CID = "closeio--ciso8601/1"
HEAD = "b29a8f49374004d650cf312e9c31848953e5ae60"
FORK = {"head_ref": "riscv64-dev", "head_sha": HEAD, "name": "riseproject-upstreaming/ciso8601",
        "pr_number": 2, "repository_id": 1467208233}
CI_EVIDENCE = "ci-1467208233-45008094068-1"
RECORD_PATH = ".state/contributions/closeio--ciso8601--1.json"
EVIDENCE_PATH = f".state/evidence/{CI_EVIDENCE}.json"
EVENT_PATH = ".state/events/closeio--ciso8601--1/00000027.json"
NOW = "2026-11-23T09:00:00+00:00"
T = datetime.fromisoformat(NOW)
SUBMIT = f"/upstreaming submit-upstream revision=6 head={HEAD}"
# The fields the record migration adds to the live record, and the one it
# removes.
ADDED = ["clean_head_ci_since", "submission_rewrite_id", "submission_review_id", "budget.reruns"]
REMOVED = ["blocked_reason"]


def load(name):
    return json.loads((FIXTURES / name).read_text())


def live():
    """The live record as committed, before the strict record schema."""
    return json.loads(LIVE.read_text())


def migrated():
    """The live record after the record migration."""
    record, _event = state.migrate_record_fields(live(), now=NOW)
    return record


def canary_operations() -> dict:
    """The confirmed repair of the tracking issue that the live store holds
    for the live record's contribution: a closed operation, which changes
    nothing."""
    op = json.loads((Path(__file__).parent / "fixtures/canary-recovery-operation.json").read_text())
    return {op["operation_id"]: op}


def comment(body, cid=900):
    return {"issue_number": 1, "comment": {"id": cid, "body": body, "user": dict(OWNER)}}


class TestLiveFixtureIsTheCommittedRecord(unittest.TestCase):
    def test_fixture_shape(self):
        record = live()
        self.assertEqual((engine.state_key(record), record["revision"], record["fork"],
                          record["last_ci_evidence_id"], record["pending_human_action"]["kind"]),
                         ("handoff.awaiting_submission", 6, FORK, CI_EVIDENCE, "submit_upstream"))
        self.assertIsNone(record["blocked_reason"])
        self.assertNotIn("reruns", record["budget"])
        self.assertTrue(state.needs_field_migration(record))
        evidence = load("evidence-ci.json")
        self.assertEqual((evidence["evidence_id"], evidence["ci"]["head_sha"]), (CI_EVIDENCE, HEAD))


class TestRecordMigration(unittest.TestCase):
    def test_only_the_missing_fields_are_added_and_the_null_flag_removed(self):
        before = live()
        after, event = state.migrate_record_fields(before, now=NOW)
        changed = {k for k in set(before) | set(after) if before.get(k, "-") != after.get(k, "-")}
        self.assertEqual(changed, {"clean_head_ci_since", "submission_rewrite_id",
                                   "submission_review_id", "blocked_reason", "budget",
                                   "updated_at"})
        for name in ("clean_head_ci_since", "submission_rewrite_id", "submission_review_id"):
            self.assertIsNone(after[name])
        self.assertNotIn("blocked_reason", after)
        self.assertEqual(after["budget"], dict(before["budget"], reruns={
            "head_sha": HEAD, "flaky": 0, "interrupted": 0}))
        # The rerun counts are those the engine read for the absent field.
        self.assertEqual(engine.head_reruns(before), engine.head_reruns(after))
        self.assertEqual((engine.state_key(after), after["revision"], after["generation"],
                          after["fork"], after["last_ci_evidence_id"],
                          after["handoff_request"], after["pending_human_action"]),
                         (engine.state_key(before), 6, 0, FORK, CI_EVIDENCE,
                          before["handoff_request"], before["pending_human_action"]))
        self.assertEqual((event["kind"], event["cause"], event["from"], event["to"],
                          event["revision_bumped"]),
                         (state.FIELD_MIGRATED, {"type": "migration", "id": state.FIELD_MIGRATION},
                          {}, {}, False))
        self.assertEqual(event["details"], {
            "from_state": "handoff.awaiting_submission", "to_state": "handoff.awaiting_submission",
            "added": ADDED, "removed": REMOVED})

    def test_the_migrated_record_needs_no_migration(self):
        self.assertFalse(state.needs_field_migration(migrated()))
        with self.assertRaises(state.StateError):
            state.migrate_record_fields(migrated(), now=NOW)

    def test_any_other_difference_from_the_strict_schema_fails_closed(self):
        record = live()
        variants = {
            "a set blocked_reason": dict(record, blocked_reason="hold"),
            "a state outside the table": dict(record, activity="waiting_capacity"),
            "an unknown field": dict(record, approved=True),
            "an invalid value": dict(record, revision=-1),
            "a pending action the state is never entered with": dict(
                record, pending_human_action=dict(record["pending_human_action"],
                                                  kind="approve_handoff")),
        }
        for name, variant in variants.items():
            with self.subTest(shape=name), self.assertRaises(state.StateError):
                state.migrate_record_fields(variant, now=NOW)

    def test_requested_work_and_a_pending_action_get_their_defaults(self):
        record = copy.deepcopy(live())
        record.update(phase="development", activity="agent_running",
                      handoff_request=None, pending_human_action=None,
                      active_work={"dedupe_key": "k", "task": "development_repair", "revision": 6,
                                   "requested_at": NOW, "deadline_at": NOW})
        after, event = state.migrate_record_fields(record, now=NOW)
        self.assertEqual(after["active_work"], dict(record["active_work"], **engine.WORK_DEFAULTS))
        self.assertIn("active_work.feedback_evidence_ids", event["details"]["added"])
        wait = copy.deepcopy(live())
        wait["pending_human_action"].pop("counter")
        after, event = state.migrate_record_fields(wait, now=NOW)
        self.assertIsNone(after["pending_human_action"]["counter"])
        self.assertIn("pending_human_action.counter", event["details"]["added"])

    def test_the_defaults_cover_every_optional_field_of_the_old_schema(self):
        schema = json.loads((Path(state.__file__).parent / "schemas/contribution.schema.json")
                            .read_text())
        always = {"schema_version", "contribution_id", "project_id", "revision", "phase",
                  "activity", "paused", "tracking_issue", "owners", "upstream", "budget",
                  "created_at", "updated_at"}
        self.assertEqual(set(engine.RECORD_DEFAULTS), set(schema["properties"]) - always)
        # `budget.source_unavailable` and the fields of its run are optional
        # in the strict schema: an absent count reads 0, so no record is
        # migrated for them.
        self.assertEqual(set(engine.BUDGET_DEFAULTS) | {"reruns", "policy_id", "attempts_used",
                                                        "source_unavailable", "review_rejections",
                                                        *engine.SOURCE_READ_FIELDS},
                         set(schema["properties"]["budget"]["properties"]))
        for name in ("source_unavailable", "source_unavailable_since", "review_rejections"):
            self.assertNotIn(name, schema["properties"]["budget"]["required"])


class TestControllerMigratesByItself(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = init_repo(Path(self.tmp.name) / "control")
        self.files = {
            RECORD_PATH: LIVE.read_text(),
            EVIDENCE_PATH: (FIXTURES / "evidence-ci.json").read_text(),
            ".state/events/closeio--ciso8601--1/00000026.json": canonical_json({
                "schema_version": 1, "contribution_id": CID, "sequence": 26,
                "at": "2026-11-23T08:00:00+00:00", "kind": "record_reminder", "from": {},
                "to": {}, "cause": {"type": "tick", "id": None}, "revision_bumped": False}),
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
        for key in ("requested_effects", "dispatch_relaunch", "provision_requests",
                    "publication_retries", "dev_pr_requests", "handoff_requests"):
            self.assertEqual(report[key], [], key)
        changed = sorted(line.strip() for line in git(
            self.repo, "status", "--porcelain", "--untracked-files=all").split("\n"))
        self.assertEqual(changed, [f"?? {EVENT_PATH}", "?? report.json", f"M {RECORD_PATH}"])
        record = json.loads((self.repo / RECORD_PATH).read_text())
        self.assertEqual(record, state.migrate_record_fields(live(), now=T.isoformat())[0])
        event = json.loads((self.repo / EVENT_PATH).read_text())
        self.assertEqual((event["sequence"], event["kind"]), (27, state.FIELD_MIGRATED))
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
        # Writing the old shape again is refused by the same final check.
        old = commit_files(self.repo, {RECORD_PATH: canonical_json(dict(live(), updated_at=NOW))})
        problems = state.validate_state_diff(self.repo, candidate, old, self.p).problems
        self.assertTrue(any("awaits the record migration" in p for p in problems), problems)

    def test_migration_is_idempotent(self):
        self.assertEqual(self.run_reconcile()[0], 0)
        self.assertEqual(state.pending_record_migrations(self.repo, self.p), [])
        self.assertEqual(state.apply_pending_record_migrations(self.repo, self.p, now=NOW), [])
        self.assertFalse((self.repo / ".state/events/closeio--ciso8601--1/00000028.json").exists())
        store = state.StateStore(self.repo, self.p)
        self.assertEqual(len(store.load_all("contributions")), 1)
        self.assertEqual(len(store.load_all("events")), 2)

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
        (self.repo / RECORD_PATH).write_text(canonical_json(dict(live(), blocked_reason="hold")))
        with self.assertRaises(state.StateError):
            store.load_all("contributions")

    def test_an_unknown_shape_stops_the_controller(self):
        text = canonical_json(dict(live(), activity="waiting_capacity"))
        (self.repo / RECORD_PATH).write_text(text)
        result, report, stderr = self.run_reconcile()
        self.assertEqual(result, 1)
        self.assertIsNone(report)
        self.assertIn("record migration refused", stderr)
        self.assertIn("no reviewed record migration for this record", stderr)
        self.assertEqual((self.repo / RECORD_PATH).read_text(), text)
        self.assertFalse((self.repo / EVENT_PATH).exists())
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(controller.cmd_observe(
                SimpleNamespace(root=str(self.repo), offline=True)), 1)


class TestMigratedRecordUnderTheOrdinaryController(unittest.TestCase):
    """After the migration, ticks keep the record; only table verbs move it."""

    def tick(self, record, *, comments=(), publications=(), snapshot=None, head=HEAD, feedback=None,
             pressure=True):
        contributions = {"c": record}
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": head}), \
             mock.patch("observe_ci.collect.development_review_feedback", return_value=feedback), \
             mock.patch("observe_ci.collect.ci_snapshot", return_value=snapshot or self.passed()):
            return reconcile.tick(
                helpers.NoReadApi(), reconcile.Snapshot(contributions, canary_operations(), {}, {},
                                             {CI_EVIDENCE: load("evidence-ci.json")}),
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

    def test_submit_upstream_is_accepted_by_the_table(self):
        record = migrated()
        output = self.tick(record, comments=[comment(SUBMIT)])
        receipt = next(r for kind, r in output.staged if kind == "commands")
        self.assertEqual((receipt["verb"], receipt["result"], receipt["revision"]),
                         ("submit-upstream", "accepted", 6))
        after = self.final(output, record)
        # The rewrite Work is allocated in the same tick; nothing is written to
        # the fork and no handoff operation is launched yet.
        self.assertEqual(engine.state_key(after), "handoff.agent_running")
        self.assertEqual(self.events(output), ["submit_upstream", "request_submission_rewrite"])
        self.assertEqual((after["revision"], after["fork"], after["last_ci_evidence_id"]),
                         (6, FORK, CI_EVIDENCE))
        self.assertEqual(after["handoff_approval"]["head"], HEAD)
        self.assertIsNone(after["pending_human_action"])
        self.assertIsNone(after["active_operation_id"])
        self.assertFalse(any(kind == "operations" for kind, _r in output.staged))
        [work] = output.report["requested_effects"]
        self.assertEqual((work["task"], work["expected_head"], work["effect_contract"]),
                         ("submission_rewrite", HEAD, {"kind": "none"}))
        self.assertEqual(output.report["handoff_requests"], [])
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
                self.assertEqual(output.report["handoff_requests"], [])

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
             "`class` and `paths` are given on `/upstreaming refresh` in `parked.idle`"),
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
                    self.assertIn('/upstreaming refresh revision=N reason="..."', receipt["reason"])
                elif "`force-develop`" in reason:
                    self.assertIn("/upstreaming refresh revision=N class=", receipt["reason"])
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
        for name in ("fork", "handoff_request", "last_ci_evidence_id", "plan_evidence_id",
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
        self.assertIn("refresh_planning", self.events(output))
        self.assertEqual(refreshed["refresh"],
                         {"command_id": "comment-900", "reason": "new base", "from_phase": "handoff",
                          "head_sha": HEAD, "pr_number": 2})
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
        self.assertEqual(after["plan_evidence_id"], record["plan_evidence_id"])
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
                                 ["review-1467208233-2-77"])
                self.assertEqual((after["handoff_request"], after["pending_human_action"],
                                  after["clean_head_sha"]), (None, None, None))
                # The CI evidence of the candidate head is kept.
                self.assertEqual(after["last_ci_evidence_id"], CI_EVIDENCE)
                self.assertIn("review-1467208233-2-77",
                              [e["evidence_id"] for kind, e in output.staged if kind == "evidence"])
                event = next(e for kind, e in output.staged if kind == "events")
                self.assertEqual((event["revision_bumped"], event["details"]["from_state"],
                                  event["details"]["to_state"]),
                                 (True, "handoff.awaiting_submission", "development.fix_needed"))

    def test_a_ci_failure_and_a_review_in_the_same_observation_take_both_rows(self):
        """The CI failure applies first (`return_for_repair`); the review
        observed with it is then recorded by `record_actionable_review` in
        `development.fix_needed`, so it is not dropped."""
        record = migrated()
        output = self.tick(record, feedback=self.REVIEW,
                           snapshot=self.passed(conclusion="failure"), pressure=False)
        after = self.final(output, record)
        self.assertEqual(self.events(output), ["return_for_repair", "record_actionable_review"])
        self.assertEqual((engine.state_key(after), after["revision"]), ("development.fix_needed", 8))
        self.assertEqual(after["processed_review_ids"], [77])
        self.assertEqual(after["pending_feedback_evidence_ids"], ["review-1467208233-2-77"])
        self.assertEqual(after["last_ci_evidence_id"], CI_EVIDENCE)
        self.assertEqual((after["handoff_request"], after["pending_human_action"],
                          after["clean_head_sha"]), (None, None, None))
        self.assertIn("review-1467208233-2-77",
                      [e["evidence_id"] for kind, e in output.staged if kind == "evidence"])
        failure, review = [e for kind, e in output.staged if kind == "events"]
        self.assertEqual((failure["cause"]["id"], review["cause"]["id"]),
                         (CI_EVIDENCE, "review-1467208233-2-77"))
        self.assertEqual((review["details"]["from_state"], review["details"]["to_state"]),
                         ("development.fix_needed", "development.fix_needed"))
        self.assertTrue(review["revision_bumped"])

    def test_the_repair_after_a_failure_and_a_review_carries_the_review(self):
        record = migrated()
        output = self.tick(record, feedback=self.REVIEW,
                           snapshot=self.passed(conclusion="failure"))
        after = self.final(output, record)
        self.assertEqual(self.events(output),
                         ["return_for_repair", "record_actionable_review", "request_repair"])
        self.assertEqual(engine.state_key(after), "development.agent_running")
        self.assertIn("review-1467208233-2-77", after["active_work"]["feedback_evidence_ids"])

    def test_an_explanation_a_review_and_a_ci_failure_in_one_observation(self):
        record = migrated()
        feedback = dict(self.REVIEW, inquiry_conversation_comment_ids=[88],
                        inquiry_conversation_comments=[{"id": 88, "body": "Why riscv64 only?"}])
        output = self.tick(record, feedback=feedback,
                           snapshot=self.passed(conclusion="failure"), pressure=False)
        after = self.final(output, record)
        self.assertEqual(self.events(output),
                         ["record_explanation", "return_for_repair", "record_actionable_review"])
        self.assertEqual((engine.state_key(after), after["revision"]), ("development.fix_needed", 8))
        self.assertEqual((after["processed_review_ids"],
                          after["processed_pr_conversation_comment_ids"]), ([77], [88]))
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
        self.assertIn("inquiry-1467208233-2-88",
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

    def test_committed_record_is_a_table_state_before_and_after_the_migration(self):
        root = Path(__file__).resolve().parents[1]
        path = root / RECORD_PATH
        if not path.exists():
            self.skipTest("no committed live record")
        record = json.loads(path.read_text())
        after = state.migrated_view("contributions", record)
        self.assertIsNone(engine.state_problem(after))
        self.assertEqual((engine.state_key(after), after["revision"], after["fork"]),
                         (engine.state_key(record), record["revision"], record["fork"]))


if __name__ == "__main__":
    unittest.main()
