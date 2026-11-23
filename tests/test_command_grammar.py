# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The command grammar is the lifecycle table's: its commands, the intake
commands, and nothing else. Retired commands name their replacement; each
table command applies exactly in the states whose rows take it."""

import copy
import unittest

import commands
import controller
import engine
import lifecycle as lc
from tests.helpers import NOW, SHA_A, SHA_B, apply_command, contribution

UPSTREAM_PR = {"id": 9, "number": 9, "url": "https://github.com/example/proj/pull/9",
               "head_repository_id": 2, "head_ref": "riscv64", "head_sha": SHA_A,
               "base_ref": "main", "registered_by": {"id": 1001, "login": "owner"}}


def record_in(state: str, revision: int = 3) -> dict:
    """A record in ``state`` as the table enters it, quiescent."""
    phase, activity = state.split(".")
    rec = contribution(phase=phase, activity=activity, revision=revision,
                       plan_evidence_id="plan-1", scope_paths=["a"],
                       scope_coverage_complete=True)
    if phase in ("queued", "planning", "provisioning"):
        rec["fork"] = None
    if phase in ("upstream_review", "release_wait", "released"):
        rec["upstream_pr"] = copy.deepcopy(UPSTREAM_PR)
    if phase == "parked":
        rec.update(parked_reason="x", previous_phase="development")
    if phase == "closed":
        rec["terminal_reason"] = "abandoned"
    kinds = sorted(lc.pending_kinds(state)) if state in lc.LIFECYCLE else []
    if kinds:
        rec["pending_human_action"] = {"kind": kinds[0], "requested_at": NOW, "reminded_at": None,
                                       "escalated_at": None, "counter": None}
    if state == "handoff.awaiting_submission":
        rec["handoff_request"] = {"command_id": "x", "head": SHA_A, "revision": revision}
        rec["last_ci_evidence_id"] = "ci-x"
    if state == "handoff.waiting_upstream_pr":
        rec["clean_head_sha"] = SHA_A
    return rec


def admitted_states():
    for state in sorted(set(lc.LIFECYCLE)):
        rec = record_in(state)
        if engine.state_problem(rec) is None:
            yield state, rec


class TestGrammarIsTheTable(unittest.TestCase):
    def test_grammar_is_the_table_commands_and_the_intake_commands(self):
        table = {text.split()[-1] for text in (
            *(e.transition.command for e in lc.table_edges()),
            *(t.command for flag in lc.FLAGS.values()
              for t in (*flag.set_by.values(), *flag.cleared_by.values()))) if text}
        self.assertEqual(table, set(engine.TABLE_COMMANDS))
        self.assertEqual(commands.INTAKE_COMMANDS, {"authorize-followup", "resolve-operation"})
        self.assertEqual(set(commands.GRAMMAR), table | commands.INTAKE_COMMANDS)
        self.assertFalse(hasattr(commands, "DEPRECATED"))
        self.assertFalse(set(commands.RETIRED) & set(commands.GRAMMAR))

    def test_every_retired_command_is_rejected_with_its_replacement(self):
        replacements = {
            "request-handoff": "/upstreaming submit-upstream", "approve-handoff":
            "/upstreaming submit-upstream", "register-upstream-pr": "/upstreaming submit-upstream",
            "block": "/upstreaming pause", "approve-scope": "/upstreaming resume",
            "provision": "/upstreaming resume", "restart": "/upstreaming resume",
            "clarify": "/upstreaming resume", "revise-scope": "/upstreaming refresh",
            "force-develop": "/upstreaming resume revision=N class="}
        self.assertEqual(set(replacements), set(commands.RETIRED))
        for verb, replacement in replacements.items():
            with self.subTest(verb=verb), self.assertRaises(commands.CommandError) as raised:
                commands.parse(f"/upstreaming {verb} revision=1 reason=x")
            self.assertTrue(str(raised.exception).startswith(f"`{verb}` is retired."))
            self.assertIn(replacement, str(raised.exception))

    def test_force_develop_names_the_resume_with_an_override_from_parked(self):
        self.assertIn("/upstreaming resume revision=N class=", commands.RETIRED["force-develop"])
        self.assertIn("`parked.idle`", commands.RETIRED["force-develop"])

    def test_optional_fields(self):
        resume = commands.parse('/upstreaming resume revision=2 class=optimized_backend '
                                'paths="a/*,b" reason=r')
        self.assertEqual(resume.args, {"revision": "2", "class": "optimized_backend",
                                       "paths": "a/*,b", "reason": "r"})
        self.assertEqual(commands.parse(f"/upstreaming refresh revision=2 head={SHA_A} reason=r")
                         .args["head"], SHA_A)
        for text in ("/upstreaming resume revision=2 class=other reason=r",
                     "/upstreaming retract-pr revision=2",
                     f"/upstreaming retract-pr head={SHA_A}",
                     f"/upstreaming retract-pr revision=2 head={SHA_A} reason=r",
                     "/upstreaming abandon revision=2"):
            with self.subTest(text=text), self.assertRaises(commands.CommandError):
                commands.parse(text)


class TestCommandsApplyWhereTheTableSays(unittest.TestCase):
    def take(self, rec, text):
        out = apply_command(rec, commands.parse(text))
        if not out.accepted:
            self.assertEqual(out.record, rec)
            self.assertIsNone(out.event)
        return out

    def test_abandon_only_from_parked(self):
        for state, rec in admitted_states():
            with self.subTest(state=state):
                out = self.take(rec, '/upstreaming abandon revision=2 reason="not needed"')
                self.assertEqual(out.accepted, state == "parked.idle", out.reason)
                if out.accepted:
                    self.assertEqual(engine.state_key(out.record), "closed.idle")
                    self.assertEqual(out.record["terminal_reason"], "abandoned")
                    self.assertEqual(out.record["revision"], rec["revision"])
                elif state != "closed.idle":
                    self.assertIn("it applies in `parked.idle`", out.reason)

    def test_refresh_only_from_the_states_of_its_rows(self):
        rows = set(engine.command_states("refresh"))
        self.assertEqual(rows, {"development.fix_needed", "development.waiting_ci",
                                "development.waiting_human", "handoff.awaiting_submission",
                                "handoff.waiting_upstream_pr"})
        for state, rec in admitted_states():
            head = f" head={SHA_A}" if state == "handoff.waiting_upstream_pr" else ""
            with self.subTest(state=state):
                out = self.take(rec, f"/upstreaming refresh revision=3{head} reason=r")
                self.assertEqual(out.accepted, state in rows, out.reason)
                if out.accepted:
                    self.assertEqual(engine.state_key(out.record), "queued.idle")
                    self.assertEqual(out.event["kind"], "refresh_planning")

    def test_refresh_names_the_exact_clean_head_where_the_table_requires_it(self):
        rec = record_in("handoff.waiting_upstream_pr")
        for text, reason in (
                ("/upstreaming refresh revision=3 reason=r", "head is not the clean head"),
                (f"/upstreaming refresh revision=3 head={SHA_B} reason=r",
                 "head is not the clean head"),
                (f"/upstreaming refresh revision=2 head={SHA_A} reason=r",
                 "stale revision 2, current is 3")):
            with self.subTest(text=text):
                out = self.take(rec, text)
                self.assertEqual((out.accepted, out.reason), (False, reason))
        rec = record_in("development.waiting_ci")
        out = self.take(rec, f"/upstreaming refresh revision=3 head={SHA_A} reason=r")
        self.assertEqual((out.accepted, out.reason),
                         (False, "`head` does not apply to `/upstreaming refresh` in "
                                 "`development.waiting_ci`"))

    def test_retract_pr_is_taken_by_the_table(self):
        rec = record_in("handoff.waiting_upstream_pr")
        out = self.take(rec, f"/upstreaming retract-pr revision=3 head={SHA_A}")
        self.assertEqual((out.accepted, out.reason, out.verb),
                         (True, None, "retract_submission"))
        self.assertEqual(engine.state_key(out.record), "development.fix_needed")
        self.assertEqual(out.record["revision"], 4)
        for name in ("pending_human_action", "handoff_request", "handoff_approval"):
            self.assertIsNone(out.record[name], name)
        for text, reason in (
                (f"/upstreaming retract-pr revision=3 head={SHA_B}", "head is not the clean head"),
                (f"/upstreaming retract-pr revision=2 head={SHA_A}",
                 "stale revision 2, current is 3")):
            with self.subTest(text=text):
                out = self.take(rec, text)
                self.assertEqual((out.accepted, out.reason), (False, reason))
        busy = dict(rec, pending_publication={"x": 1})
        self.assertEqual(self.take(busy, f"/upstreaming retract-pr revision=3 head={SHA_A}").reason,
                         "a publication is pending")
        for state, other in admitted_states():
            if state == "handoff.waiting_upstream_pr":
                continue
            with self.subTest(state=state):
                out = self.take(other, f"/upstreaming retract-pr revision=3 head={SHA_A}")
                self.assertFalse(out.accepted)
                self.assertIn("it applies in `handoff.waiting_upstream_pr`", out.reason)

    def test_resume_takes_restart_generation_only_from_parked(self):
        for state, rec in admitted_states():
            with self.subTest(state=state):
                out = self.take(rec, "/upstreaming resume revision=3 reason=r")
                self.assertEqual(out.accepted and out.event["kind"] == "restart_generation",
                                 state == "parked.idle")

    def test_force_develop_is_retired(self):
        with self.assertRaises(commands.CommandError) as raised:
            commands.parse('/upstreaming force-develop revision=3 class=configuration_integration '
                           'paths=".github/workflows/*" reason=r')
        self.assertTrue(str(raised.exception).startswith("`force-develop` is retired."))
        self.assertNotIn("force-develop", controller.FOLLOWUP_COMMANDS)


if __name__ == "__main__":
    unittest.main()
