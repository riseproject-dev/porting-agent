# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The invariants of every state (``lifecycle.REQUIRES``).

``lifecycle check`` proves that no row enters a state without the
invariants the state requires; these tests confirm the proof on records:
every row taken from a valid record leaves a valid record or is refused as
``target_invalid``, the guards and effects the proof counts do what it
claims, a record that breaks its state is refused as ``source_invalid``
(but for the stops, the late Result and its heal), the proof fails on a
table with a guard removed, and the live records satisfy their states.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest import mock

import engine
import lifecycle as lc
from tests.helpers import NOW
from tests.test_engine import POLICY, all_rows, scenario

ROOT = Path(__file__).resolve().parents[1]


def kwargs_of(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if k != "cause_id"}


class TestEveryRowKeepsTheInvariants(unittest.TestCase):
    def test_a_valid_record_exists_for_every_state(self):
        seen = set()
        for state, verb, t, _target in all_rows():
            if state in seen:
                continue
            seen.add(state)
            rec, _kwargs = scenario(state, verb, t)
            with self.subTest(state=state):
                self.assertEqual(lc.requirement_problems(state, rec), [])
        self.assertLessEqual(set(lc.LIFECYCLE) - lc.TERMINAL_STATES, seen)

    def test_no_row_enters_a_state_that_violates_its_requirements(self):
        for state, verb, t, target in all_rows():
            rec, kwargs = scenario(state, verb, t)
            with self.subTest(state=state, verb=verb):
                out = engine.try_apply_transition(rec, verb, **kwargs)
                if isinstance(out, engine.Refused):
                    self.assertEqual(out.kind, engine.Refusal.TARGET_INVALID)
                    continue
                self.assertEqual(lc.requirement_problems(target, out.record), [])

    def test_every_requirement_of_a_target_has_its_reason(self):
        for source, verb, t, target in lc.requirement_edges():
            for name in lc.LIFECYCLE[target].requires:
                with self.subTest(row=(source, verb), requirement=name):
                    self.assertIsNotNone(lc.requirement_reason(source, verb, t, target, name))

    def test_what_the_effects_establish_holds_after_the_row(self):
        for state, verb, t, target in all_rows():
            rec, kwargs = scenario(state, verb, t)
            out = engine.try_apply_transition(rec, verb, **kwargs)
            if not isinstance(out, engine.Applied):
                continue
            for name, invariant in lc.INVARIANTS.items():
                if not invariant.establish(t, verb):
                    continue
                with self.subTest(state=state, verb=verb, invariant=name):
                    self.assertIsNone(invariant.check(out.record))

    def test_what_a_guard_implies_holds_where_the_guard_holds(self):
        checked = 0
        for state, verb, t, _target in all_rows():
            implied = [(g, n) for g in t.guard for n in lc.IMPLIES.get(g, ())]
            if not implied:
                continue
            rec, kwargs = scenario(state, verb, t)
            ctx = engine.Context(rec, state, POLICY, NOW, kwargs.get("command"),
                                 frozenset(kwargs.get("facts", ())), dict(kwargs["counts"]),
                                 None, kwargs.get("ci_evidence"))
            for guard, name in implied:
                with self.subTest(state=state, verb=verb, guard=guard, invariant=name):
                    self.assertIsNone(engine.PREDICATES[guard].check(ctx))
                    self.assertIsNone(lc.INVARIANTS[name].check(rec))
                    checked += 1
        self.assertGreater(checked, 20)


class TestTheEngineEnforcesThem(unittest.TestCase):
    def test_a_record_that_breaks_its_state_is_refused_without_change(self):
        state, verb = "development.fix_needed", "request_patch"
        rec, kwargs = scenario(state, verb, lc.find_edge(state, verb).transition)
        broken = dict(rec, plan_evidence_id=None)
        before = copy.deepcopy(broken)
        out = engine.try_apply_transition(broken, verb, **kwargs)
        self.assertEqual(out.kind, engine.Refusal.SOURCE_INVALID)
        self.assertIn("development.fix_needed requires plan", out.detail)
        self.assertEqual(broken, before)
        self.assertEqual(engine.invariant_problems(broken)[0][0], "plan")

    def test_stops_the_late_result_and_the_heal_still_apply(self):
        state = "development.waiting_ci"
        rec, _kwargs = scenario(state, "record_explanation",
                                lc.find_edge(state, "record_explanation").transition)
        broken = dict(rec, plan_evidence_id=None)
        out = engine.try_apply_transition(broken, "pause_on_processing_error", policy=POLICY,
                                          now=NOW, facts={"processing_error"},
                                          values={"paused_reason": "processing_error: x"})
        self.assertIsInstance(out, engine.Applied)
        self.assertTrue(out.record["paused"])
        late = engine.try_apply_transition(broken, "record_late_effect", policy=POLICY, now=NOW,
                                           facts={"result_mismatch"})
        self.assertIsInstance(late, engine.Applied)
        # Another row is refused.
        other = engine.try_apply_transition(broken, "record_explanation", policy=POLICY, now=NOW,
                                            facts={"explanation_inquiry"},
                                            values={"processed_review_ids": [],
                                                    "processed_review_comment_ids": [],
                                                    "processed_pr_conversation_comment_ids": []})
        self.assertEqual(other.kind, engine.Refusal.SOURCE_INVALID)

    def test_a_row_that_would_break_its_target_is_refused(self):
        # A head pushed while a task is due is awaited only with the
        # development PR `development.waiting_ci` requires; without the guard
        # the record after the row would break it.
        state, verb = "development.fix_needed", "record_head_change"
        target = "development.waiting_ci"
        t = lc.find_edge(state, verb).transition
        rec, kwargs = scenario(state, verb, t)
        no_pr = dict(rec, fork=dict(rec["fork"], pr_number=None))
        out = engine.try_apply_transition(no_pr, verb, **kwargs)
        self.assertEqual(out.kind, engine.Refusal.GUARD_UNMET)
        stripped = mock.patch.dict(lc.LIFECYCLE[state].transitions, {
            (verb, target): lc.Transition(
                t.trigger, t.command, tuple(g for g in t.guard
                                            if g != lc.DEVELOPMENT_PR_RECORDED),
                t.effects, t.charges, t.description, t.exhausts)})
        before = copy.deepcopy(no_pr)
        with stripped:
            out = engine.try_apply_transition(no_pr, verb, **kwargs)
        self.assertEqual(out.kind, engine.Refusal.TARGET_INVALID)
        self.assertIn(f"{target} requires dev_pr", out.detail)
        self.assertEqual(no_pr, before)


class TestTheProof(unittest.TestCase):
    def test_the_check_fails_on_a_table_with_a_guard_removed(self):
        self.assertEqual([p for p in lc.check_table(POLICY) if "requires" in p], [])
        state, verb = "development.fix_needed", "record_head_change"
        target = "development.waiting_ci"
        t = lc.find_edge(state, verb).transition
        mutated = lc.Transition(t.trigger, t.command,
                                tuple(g for g in t.guard if g != lc.DEVELOPMENT_PR_RECORDED),
                                t.effects, t.charges, t.description, t.exhausts)
        with mock.patch.dict(lc.LIFECYCLE[state].transitions, {(verb, target): mutated}):
            problems = lc.check_table(POLICY)
        self.assertIn(f"{state} --{verb}--> {target}: nothing keeps, implies, "
                      f"or establishes 'dev_pr', which {target} requires", problems)

    def test_the_check_fails_on_an_effect_that_destroys_a_requirement(self):
        state, verb = "development.waiting_human", "resume_ci"
        t = lc.find_edge(state, verb).transition
        from dataclasses import replace
        mutated = replace(t, effects=replace(t.effects, clears=tuple(
            c for c in t.effects.clears if c != "pending_human_action")))
        with mock.patch.dict(lc.LIFECYCLE[state].transitions,
                             {(verb, "development.waiting_ci"): mutated}):
            problems = lc.check_table(POLICY)
        self.assertTrue(any(p.startswith(f"{state} --{verb}--> development.waiting_ci")
                            and "'no_pending'" in p for p in problems), problems)

    def test_a_row_that_always_applies_needs_no_handling(self):
        # No row of the table has the guard `always` today; one that had it
        # would be proved to refuse with no kind.
        self.assertFalse([e for e in lc.table_edges() if e.transition.guard == (lc.ALWAYS,)])
        state = "development.waiting_ci"
        rows = {("always_row", state):
                lc.Transition(lc.Trigger.OBSERVATION, None, (lc.ALWAYS,),
                              lc.Effects(sets=("last_ci_evidence_id",)))}
        with mock.patch.dict(lc.LIFECYCLE[state].transitions, rows):
            problems = lc.check_table(POLICY)
        self.assertTrue(any("an `always` row sets last_ci_evidence_id" in p for p in problems),
                        problems)
        rows = {("always_row", "development.fix_needed"):
                lc.Transition(lc.Trigger.TICK, None, (lc.ALWAYS,))}
        with mock.patch.dict(lc.LIFECYCLE[state].transitions, rows):
            problems = lc.check_table(POLICY)
        self.assertTrue(any("an `always` row is refused while paused" in p for p in problems),
                        problems)

    def test_every_requirement_names_an_invariant_and_every_state_has_some(self):
        for key, state in lc.LIFECYCLE.items():
            with self.subTest(state=key):
                self.assertTrue(state.requires)
                self.assertLessEqual(set(state.requires), set(lc.INVARIANTS))


class TestTheLiveRecords(unittest.TestCase):
    def test_the_gate_record_satisfies_its_state(self):
        rec = json.loads((ROOT / ".state/contributions/closeio--ciso8601--1.json").read_text())
        self.assertEqual(engine.state_key(rec), "handoff.awaiting_submission")
        self.assertEqual(engine.invariant_problems(rec), [])


if __name__ == "__main__":
    unittest.main()
