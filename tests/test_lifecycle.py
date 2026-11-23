# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Section 21: lifecycle, pause/park, handoff and revision rules."""

import unittest

import commands
import controller
import engine
import lifecycle
import schemas
from tests.helpers import NOW, SHA_A, SHA_B, apply_command, contribution, policy


def cmd(text):
    return commands.parse(text)


def apply(rec, text, **kw):
    return apply_command(rec, cmd(text), command_id="comment-1", now=NOW, **kw)


CID = "example--proj/12"


class TestRevision(unittest.TestCase):
    def test_head_change_bumps_and_invalidates(self):
        rec = contribution(last_ci_evidence_id="ev-1")
        out = engine.apply_transition(rec, "record_head_change", policy=policy(), now=NOW,
                                      facts={"fork_head_changed"}, values={"fork.head_sha": SHA_B})
        self.assertEqual(out.record["revision"], rec["revision"] + 1)
        self.assertIsNone(out.record["last_ci_evidence_id"])

    def test_a_moved_head_stales_a_proposal(self):
        rec = contribution()
        self.assertIsNone(lifecycle.publication_check(rec, expected_revision=rec["revision"],
                                                      expected_head=SHA_A, observed_head=SHA_A,
                                                      ref="riscv64-dev", repository="riseproject-upstreaming/proj"))
        moved = engine.apply_transition(rec, "record_head_change", policy=policy(), now=NOW,
                                        facts={"fork_head_changed"},
                                        values={"fork.head_sha": SHA_B}).record
        self.assertEqual(lifecycle.publication_check(moved, expected_revision=rec["revision"],
                                                     expected_head=SHA_A, observed_head=SHA_B,
                                                     ref="riscv64-dev", repository="riseproject-upstreaming/proj"),
                         "work revision changed")


class TestPublicationCheck(unittest.TestCase):
    def args(self, **kw):
        a = dict(expected_revision=0, expected_head=SHA_A, observed_head=SHA_A, ref="riscv64-dev",
                 repository="riseproject-upstreaming/proj")
        a.update(kw)
        return a

    def test_ok(self):
        self.assertIsNone(lifecycle.publication_check(contribution(), **self.args()))

    def test_rejects(self):
        cases = {
            "paused": (contribution(paused=True), {}),
            "ref": (contribution(), {"ref": "riscv64-2"}),
            "repo": (contribution(), {"repository": "riseproject-dev/proj"}),
            "head": (contribution(), {"observed_head": SHA_B}),
            "phase": (contribution(phase="release_wait"), {}),
            "parked": (contribution(phase="parked"), {}),
            "active op": (contribution(active_operation_id="op-1"), {}),
        }
        for name, (rec, kw) in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(lifecycle.publication_check(rec, **self.args(**kw)))


class TestPauseParkResume(unittest.TestCase):
    @staticmethod
    def head_changed(rec):
        return engine.apply_transition(rec, "record_head_change", policy=policy(), now=NOW,
                                       facts={"fork_head_changed"},
                                       values={"fork.head_sha": SHA_B}).record

    def test_pause_then_stale_resume_rejected(self):
        rec = contribution(activity="waiting_ci")
        p = apply(rec, f"/upstreaming pause contribution={CID} revision=0 reason=r")
        self.assertTrue(p.accepted)
        self.assertTrue(p.record["paused"])
        bumped = self.head_changed(p.record)
        stale = apply(bumped, f"/upstreaming resume contribution={CID} revision=0 reason=r")
        self.assertFalse(stale.accepted)
        ok = apply(bumped, f"/upstreaming resume contribution={CID} revision=1 reason=r")
        self.assertTrue(ok.accepted)
        self.assertFalse(ok.record["paused"])
        self.assertEqual(ok.record["revision"], 2)

    def test_stale_pause_still_honoured(self):
        rec = self.head_changed(contribution(activity="waiting_ci"))
        self.assertTrue(apply(rec, f"/upstreaming pause contribution={CID} revision=0 reason=r").accepted)

    def test_park_only_explicit_refresh(self):
        rec = apply(contribution(), f"/upstreaming park contribution={CID} revision=0 reason=evidence").record
        self.assertEqual(rec["phase"], "parked")
        self.assertFalse(apply(rec, f"/upstreaming resume contribution={CID} revision=0 "
                                    "reason=r").accepted)
        out = apply(rec, f"/upstreaming refresh contribution={CID} revision=0 reason=\"new evidence\"")
        # Plan 3.3.4 failure case 2 and 3.5: a clean generation at queued:idle.
        self.assertEqual((out.record["phase"], out.record["activity"]), ("queued", "idle"))
        self.assertEqual(out.record["generation"], rec["generation"] + 1)
        self.assertEqual(out.record["revision"], 1)
        self.assertIsNone(out.record["parked_reason"])

    def test_refresh_from_parked_requires_quiescence(self):
        rec = apply(contribution(), f"/upstreaming park contribution={CID} revision=0 reason=evidence").record
        busy = dict(rec, active_operation_id="op-1")
        out = apply(busy, f"/upstreaming refresh contribution={CID} revision=0 reason=r")
        self.assertFalse(out.accepted)

    def test_terminal_rejects(self):
        rec = contribution(phase="closed", activity="idle", terminal_reason="abandoned")
        for text in (f"/upstreaming resume contribution={CID} revision=0 reason=r",
                     f"/upstreaming pause contribution={CID} revision=0",
                     f"/upstreaming park contribution={CID} revision=0 reason=r",
                     f"/upstreaming abandon contribution={CID} revision=0 reason=r"):
            with self.subTest(text=text):
                out = apply(rec, text)
                self.assertFalse(out.accepted)
                self.assertEqual(out.record, rec)


class TestPlanRefresh(unittest.TestCase):
    def test_refresh_preserves_existing_pr_and_queues_planning(self):
        rec = contribution(activity="waiting_ci", revision=7, plan_evidence_id="plan-old",
                           scope_coverage_complete=True,
                           scope_paths=[".github/workflows/wheels.yml"],
                           proposal_artifact={"outcome": "published"},
                           last_ci_evidence_id="ci-old", clean_head_sha=SHA_A,
                           processed_review_comment_ids=[44],
                           pending_feedback_evidence_ids=["review-old"])
        out = apply(rec, f'/upstreaming refresh contribution={CID} revision=7 reason="new dependency policy"')
        self.assertTrue(out.accepted)
        refreshed = out.record
        self.assertEqual((refreshed["phase"], refreshed["activity"], refreshed["revision"]),
                         ("queued", "idle", 8))
        self.assertEqual(refreshed["fork"], rec["fork"])
        self.assertEqual(refreshed["processed_review_comment_ids"], [44])
        self.assertEqual(refreshed["pending_feedback_evidence_ids"], ["review-old"])
        self.assertIsNone(refreshed["plan_evidence_id"])
        self.assertIsNone(refreshed["scope_paths"])
        self.assertIsNone(refreshed["last_ci_evidence_id"])
        self.assertEqual(refreshed["refresh"]["head_sha"], SHA_A)
        self.assertEqual(refreshed["refresh"]["pr_number"], 1)
        self.assertEqual(out.event["kind"], "refresh_planning")

    def test_refresh_requires_existing_pr_and_quiescence(self):
        cases = [contribution(phase="queued"),
                 contribution(activity="waiting_ci", fork=dict(contribution()["fork"], pr_number=None)),
                 contribution(activity="fix_needed", active_work={"x": 1}),
                 contribution(activity="waiting_ci", active_operation_id="op-1"),
                 contribution(activity="waiting_ci", pending_publication={"x": 1}),
                 # A state the table does not have: refresh applies in states of the table only.
                 contribution(activity="idle")]
        for rec in cases:
            with self.subTest(record=rec):
                self.assertFalse(apply(rec,
                    f"/upstreaming refresh contribution={CID} revision=0 reason=r").accepted)


class TestRestartGeneration(unittest.TestCase):
    def parked(self, **over):
        rec = contribution(
            phase="parked", activity="idle", generation=2, revision=8, parked_reason="old attempt",
            previous_phase="development",
            scope_paths=[".github/workflows/wheels.yml"], plan_evidence_id="plan-old",
            scope_coverage_complete=True, processed_publications=[11, 12],
            processed_review_ids=[21], processed_review_comment_ids=[22],
            processed_pr_conversation_comment_ids=[23],
            pending_feedback_evidence_ids=["review-old"],
            proposal_artifact={"artifact_id": 1}, last_ci_evidence_id="ci-old",
            budget={"policy_id": "test", "attempts_used": 3, "no_progress_streak": 2,
                    "clusters": {"x": 2}, "spent_estimate_usd_cents": 99,
                    "planning_attempts": 0,
                    "reruns": {"head_sha": None, "flaky": 0, "interrupted": 0}})
        rec.update(over)
        return rec

    def test_parked_contribution_refreshes_on_same_identity(self):
        rec = self.parked()
        out = apply(rec, f'/upstreaming refresh contribution={CID} revision=8 reason="clean start"')
        self.assertTrue(out.accepted, out.reason)
        restarted = out.record
        self.assertEqual(restarted["contribution_id"], CID)
        self.assertEqual(restarted["tracking_issue"], rec["tracking_issue"])
        self.assertEqual((restarted["generation"], restarted["revision"]), (3, 9))
        self.assertEqual((restarted["phase"], restarted["activity"]), ("queued", "idle"))
        self.assertIsNone(restarted["fork"])
        self.assertIsNone(restarted["plan_evidence_id"])
        self.assertIsNone(restarted["scope_paths"])
        self.assertIsNone(restarted["proposal_artifact"])
        self.assertIsNone(restarted["admission_override"])
        self.assertEqual(restarted["processed_publications"], [11, 12])
        self.assertEqual(restarted["processed_review_comment_ids"], [])
        self.assertEqual(restarted["pending_feedback_evidence_ids"], [])
        self.assertEqual(restarted["budget"]["attempts_used"], 0)
        self.assertEqual(out.event["kind"], "restart_generation")
        self.assertTrue(out.event["revision_bumped"])
        self.assertNotIn("admission_override", out.event["details"])
        schemas.validate("contribution", restarted)

    def test_refresh_from_parked_records_the_admission_override(self):
        out = apply(self.parked(), f'/upstreaming refresh contribution={CID} revision=8 '
                                   'class=architecture_correctness paths=".github/workflows/*,setup.py" '
                                   'reason="the patch is bounded"')
        self.assertTrue(out.accepted, out.reason)
        override = {"class": "architecture_correctness",
                    "paths": [".github/workflows/*", "setup.py"], "reason": "the patch is bounded"}
        self.assertEqual(out.record["admission_override"], override)
        self.assertEqual((out.record["phase"], out.record["activity"]), ("queued", "idle"))
        self.assertEqual(out.event["details"]["admission_override"], override)
        schemas.validate("contribution", out.record)

    def test_a_partial_admission_override_is_refused(self):
        """An override admits development only with both a class and paths:
        a refresh that names one of them alone is refused when it is posted,
        with nothing changed, instead of starting planning runs that the
        plan fold could never admit."""
        for args in ("class=optimized_backend", 'paths=".github/workflows/*"'):
            with self.subTest(args=args):
                parked = self.parked()
                out = apply(parked, f"/upstreaming refresh contribution={CID} revision=8 "
                                    f"{args} reason=r")
                self.assertFalse(out.accepted)
                self.assertIn("names both `class` and `paths`, or neither", out.reason)
                self.assertEqual(out.record, parked)
                self.assertIsNone(out.event)

    def test_the_admission_override_needs_parked_and_a_safe_scope(self):
        unsafe = apply(self.parked(), f'/upstreaming refresh contribution={CID} revision=8 '
                                      'class=optimized_backend paths="../etc" reason=r')
        self.assertFalse(unsafe.accepted)
        self.assertIn("unsafe scope path", unsafe.reason)
        fix_needed = contribution(activity="fix_needed", revision=3)
        paused = contribution(activity="waiting_ci", paused=True, paused_at=NOW, paused_by="human")
        for rec in (fix_needed, paused):
            with self.subTest(state=f"{rec['phase']}.{rec['activity']}"):
                out = apply(rec, f"/upstreaming refresh contribution={CID} revision={rec['revision']} "
                                 'class=optimized_backend paths="a/*" reason=r')
                self.assertFalse(out.accepted)
                self.assertEqual(out.reason,
                                 "`class` and `paths` apply only to a refresh from `parked.idle` "
                                 "that starts a new generation")
                self.assertEqual(out.record, rec)

    def test_refresh_from_parked_is_stale_proof(self):
        out = apply(self.parked(), f"/upstreaming refresh contribution={CID} revision=7 reason=r")
        self.assertFalse(out.accepted)
        self.assertIn("stale revision 7", out.reason)

    def test_refresh_from_parked_waits_for_writer_quiescence(self):
        for over in ({"active_operation_id": "op-1"}, {"pending_publication": {"x": 1}}):
            with self.subTest(over=over):
                self.assertFalse(apply(self.parked(**over),
                    f"/upstreaming refresh contribution={CID} revision=8 reason=r").accepted)

    def test_a_resume_in_parked_is_refused_with_the_refresh_form(self):
        parked = self.parked()
        out = apply(parked, f"/upstreaming resume contribution={CID} revision=8 reason=r")
        self.assertFalse(out.accepted)
        self.assertEqual(out.reason, commands.PARKED_RESUME)
        self.assertIn(commands.REFRESH_FORM, out.reason)
        self.assertEqual(out.record, parked)
        self.assertIsNone(out.event)

    def test_a_resume_in_paused_parked_only_ends_the_pause(self):
        parked = self.parked(paused=True, paused_at=NOW, paused_by="human", proposal_artifact=None)
        out = apply(parked, f"/upstreaming resume contribution={CID} revision=8 reason=r")
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.event["kind"], "resume_pause")
        self.assertEqual(engine.state_key(out.record), "parked.idle")
        self.assertFalse(out.record["paused"])
        again = apply(out.record, f"/upstreaming resume contribution={CID} revision=9 reason=r")
        self.assertFalse(again.accepted)
        self.assertEqual(again.reason, commands.PARKED_RESUME)

    def test_a_refresh_in_paused_parked_restarts_and_stays_paused(self):
        parked = self.parked(paused=True, paused_at=NOW, paused_by="human", proposal_artifact=None)
        out = apply(parked, f"/upstreaming refresh contribution={CID} revision=8 reason=r")
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.event["kind"], "restart_generation")
        self.assertEqual(engine.state_key(out.record), "queued.idle")
        self.assertTrue(out.record["paused"])
        self.assertEqual((out.record["paused_at"], out.record["paused_by"]), (NOW, "human"))
        schemas.validate("contribution", out.record)
        resumed = apply(out.record, f"/upstreaming resume contribution={CID} revision=9 reason=r")
        self.assertTrue(resumed.accepted, resumed.reason)
        self.assertEqual(resumed.event["kind"], "resume_pause")
        self.assertEqual(engine.state_key(resumed.record), "queued.idle")
        self.assertFalse(resumed.record["paused"])

    def test_a_refresh_in_parked_refuses_head(self):
        parked = self.parked()
        out = apply(parked, f"/upstreaming refresh contribution={CID} revision=8 "
                            f"head={SHA_A} reason=r")
        self.assertFalse(out.accepted)
        self.assertEqual(out.reason, "`head` does not apply to `/upstreaming refresh` in "
                                     "`parked.idle`")
        self.assertEqual(out.record, parked)

    def test_abandon_closes_only_a_parked_contribution(self):
        rec = self.parked(paused=True, paused_at=NOW, paused_by="human", proposal_artifact=None)
        out = apply(rec, f'/upstreaming abandon contribution={CID} revision=5 reason="not needed"')
        self.assertTrue(out.accepted, out.reason)
        closed = out.record
        self.assertEqual((closed["phase"], closed["activity"]), ("closed", "idle"))
        self.assertEqual(closed["terminal_reason"], "abandoned")
        self.assertEqual((closed["revision"], closed["generation"]), (8, 2))
        self.assertEqual(out.event["kind"], "abandon")
        self.assertEqual(out.event["details"]["reason"], "not needed")
        schemas.validate("contribution", closed)
        self.assertTrue(closed["paused"])
        # Closed is final. A resume of the paused record clears the flag only
        # (`resume_pause` has no other effect); the next resume is refused.
        cleared = apply(closed, f"/upstreaming resume contribution={CID} revision=8 reason=r")
        self.assertTrue(cleared.accepted, cleared.reason)
        self.assertEqual(cleared.event["kind"], "resume_pause")
        self.assertEqual((cleared.record["phase"], cleared.record["paused"]), ("closed", False))
        self.assertFalse(apply(cleared.record, f"/upstreaming resume contribution={CID} revision=9 "
                                               "reason=r").accepted)
        for rec in (contribution(activity="waiting_ci"), contribution(activity="idle"),
                    contribution(phase="queued", activity="idle")):
            with self.subTest(state=f"{rec['phase']}.{rec['activity']}"):
                refused = apply(rec, f"/upstreaming abandon contribution={CID} reason=r")
                self.assertFalse(refused.accepted)
                self.assertEqual(refused.record, rec)
                self.assertTrue(refused.reason.endswith("; it applies in `parked.idle`"),
                                refused.reason)


class TestHandoff(unittest.TestCase):
    def test_retired_handoff_commands_name_submit_upstream(self):
        for text in (f"/upstreaming request-handoff revision=0 head={SHA_A}",
                     f"/upstreaming approve-handoff revision=0 head={SHA_A}",
                     "/upstreaming register-upstream-pr revision=0 "
                     "url=https://github.com/example/proj/pull/9"):
            with self.subTest(text=text), self.assertRaises(commands.CommandError) as raised:
                cmd(text)
            self.assertIn("is retired", str(raised.exception))
            self.assertIn("/upstreaming submit-upstream revision=N head=FULL_SHA",
                          str(raised.exception))

    def test_a_command_the_engine_does_not_take_is_refused_without_change(self):
        rec = contribution(phase="handoff", activity="awaiting_submission")
        out = controller.authorize_followup(
            rec, cmd(f"/upstreaming authorize-followup contribution={CID} revision=0 proposal=p-1"),
            command_id="c", now=NOW)
        self.assertEqual((out.accepted, out.record), (False, rec))
        self.assertEqual(out.reason, "follow-ups need a merged contribution")

    def test_a_write_beside_a_row_names_its_fields(self):
        rec = contribution(activity="agent_running")
        with self.assertRaises(lifecycle.TransitionError):
            engine.record_beside(rec, "somewhere.else", now=NOW, active_operation_id="op-1")
        with self.assertRaises(lifecycle.TransitionError):
            engine.record_beside(rec, "operation_planned", now=NOW, fork=None)
        with self.assertRaises(lifecycle.TransitionError):
            engine.record_beside(rec, "patch_published", now=NOW,
                                 budget=dict(rec["budget"], attempts_used=9))
        with self.assertRaises(lifecycle.TransitionError):
            engine.record_beside(rec, "operation_planned", now=NOW, revision=7)
        moved = engine.record_beside(rec, "operation_planned", now=NOW, active_operation_id="op-1")
        self.assertEqual((moved["active_operation_id"], engine.state_key(moved)),
                         ("op-1", engine.state_key(rec)))


class TestMergeAndRelease(unittest.TestCase):
    def test_write_admission_is_automatic_after_plan_and_fork(self):
        rec = contribution()
        rec["owners"] = {"primary": None, "backup": None, "submitter": None, "handoff_reviewer": None}
        self.assertIsNone(lifecycle.write_admission(rec))
        rec["admission_class"] = None
        self.assertIn("admission class", lifecycle.write_admission(rec))

    def test_schema_rejects_wrong_ref_and_unknown_version(self):
        rec = contribution()
        rec["fork"]["head_ref"] = "riscv64-salt"
        self.assertTrue(schemas.errors("contribution", rec))
        rec = contribution(schema_version=2)
        self.assertTrue(schemas.errors("contribution", rec))


if __name__ == "__main__":
    unittest.main()


class TestStopEndsRequestedWork(unittest.TestCase):
    def test_stop_commands_clear_active_work(self):
        import commands as C
        from tests.helpers import NOW, contribution
        rec = contribution(phase="planning", activity="agent_running", revision=1)
        rec["active_work"] = {"dedupe_key": "k", "task": "planning", "revision": 1,
                              "requested_at": NOW, "deadline_at": NOW, "dispatched_run_id": None, "generation": 0, "attempt": 1, "reason_ids": [], "feedback_evidence_ids": []}
        for body in ("/upstreaming pause", "/upstreaming park reason=x"):
            out = apply_command(rec, C.parse(body))
            self.assertTrue(out.accepted, body)
            self.assertIsNone(out.record["active_work"], body)
            self.assertNotEqual(out.record["activity"], "agent_running", body)


class TestCapacityOwnBacklog(unittest.TestCase):
    def test_other_tenants_old_pending_jobs_do_not_block(self):
        from datetime import datetime, timezone

        import capacity
        now = datetime(2026, 11, 18, 22, tzinfo=timezone.utc)
        job = lambda ent, created: {"job_labels": ["ubuntu-24.04-riscv"], "status": "pending",
                                     "entity_name": ent, "job_created_at": created}
        pol = {"capacity": {"max_pending_jobs_rise": 100, "max_pending_age_seconds": 7200}}
        p = capacity.observe({"jobs": [job("someone-else", "2026-11-17T00:00:00Z")]}, fetched_at=now, now=now,
                             max_age_seconds=900)
        self.assertTrue(capacity.admit_expensive(p, reserved=0, estimated_jobs=1, policy=pol)[0])
        p = capacity.observe({"jobs": [job("riseproject-upstreaming", "2026-11-17T00:00:00Z")]}, fetched_at=now,
                             now=now, max_age_seconds=900)
        self.assertFalse(capacity.admit_expensive(p, reserved=0, estimated_jobs=1, policy=pol)[0])


class TestScopeAndProvision(unittest.TestCase):
    def setUp(self):
        from tests.helpers import contribution
        self.rec = contribution(phase="planning", activity="waiting_human", revision=1,
                                plan_evidence_id="plan-x", fork=None,
                                owners={"primary": None, "backup": None, "submitter": None,
                                        "handoff_reviewer": None}, admission_class=None)
        self.rec["upstream"]["base_sha"] = None

    def apply(self, rec, body, **kw):
        return apply_command(rec, commands.parse(body), **kw)

    def test_submit_upstream_outside_the_handoff_phase_names_where_it_applies(self):
        for phase, activity in (("development", "waiting_ci"), ("development", "fix_needed"),
                                ("planning", "idle"), ("parked", "idle")):
            rec = contribution(phase=phase, activity=activity)
            out = self.apply(rec, f"/upstreaming submit-upstream revision=0 head={SHA_A}")
            with self.subTest(state=f"{phase}.{activity}"):
                self.assertFalse(out.accepted)
                self.assertEqual(out.record, rec)
                self.assertEqual(
                    out.reason,
                    f"`/upstreaming submit-upstream` does not apply in `{phase}.{activity}`; it "
                    "applies in `handoff.awaiting_submission`")

    def test_human_overrides_a_plan_park_with_a_refresh_from_parked(self):
        parked = dict(self.rec, phase="parked", activity="idle", parked_reason="No current patch")
        with self.assertRaises(commands.CommandError) as raised:
            commands.parse('/upstreaming force-develop revision=1 class=configuration_integration '
                           'paths=".github/workflows/*" reason="Proceed."')
        self.assertIn("/upstreaming refresh revision=N class=", str(raised.exception))
        out = self.apply(parked, '/upstreaming refresh revision=1 class=configuration_integration '
                                 'paths=".github/workflows/*,pyproject.toml" '
                                 'reason="Proceed with a concrete reviewed scope."')
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual((out.record["phase"], out.record["activity"], out.record["generation"]),
                         ("queued", "idle", 1))
        self.assertEqual(out.record["admission_override"],
                         {"class": "configuration_integration",
                          "paths": [".github/workflows/*", "pyproject.toml"],
                          "reason": "Proceed with a concrete reviewed scope."})
        self.assertFalse(self.apply(parked, '/upstreaming refresh revision=1 '
                                            'class=configuration_integration paths="*" '
                                            'reason="Scope is too broad."').accepted)

    def test_retired_scope_and_provisioning_commands_name_their_replacement(self):
        for text, replacement in (
                ("/upstreaming approve-scope revision=1 class=configuration_integration backup=x",
                 "/upstreaming refresh revision=N"),
                ("/upstreaming provision revision=2", "/upstreaming resume revision=N"),
                ("/upstreaming revise-scope revision=1 proposal=p", "/upstreaming refresh revision=N")):
            with self.subTest(text=text), self.assertRaises(commands.CommandError) as raised:
                commands.parse(text)
            self.assertIn("is retired", str(raised.exception))
            self.assertIn(replacement, str(raised.exception))
