# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The preconditions of every row are a pure function, and a row is taken
only through ``engine.try_apply_transition``.

``engine.preconditions`` evaluates a row without changing anything: it
answers ``Ok`` exactly when ``try_apply_transition`` applies the row, and a
refusal is one of the closed ``engine.Refusal`` kinds, the one registered for
the guard that does not hold, whatever its wording. A refused row leaves the
record deep-equal and emits nothing.
"""

from __future__ import annotations

import copy
import unittest

import commands
import engine
import lifecycle as lc
from tests.helpers import NOW, contribution
from tests.test_engine import POLICY, all_rows, scenario


def evaluate(rec: dict, verb: str, kwargs: dict):
    """``preconditions`` and ``try_apply_transition`` of the same row, and
    whether either changed the record."""
    before = copy.deepcopy(rec)
    checked = engine.preconditions(rec, verb, **{k: v for k, v in kwargs.items()
                                                 if k != "cause_id"})
    unchanged = rec == before
    outcome = engine.try_apply_transition(rec, verb, **kwargs)
    return checked, outcome, unchanged and rec == before


class TestPreconditionsArePure(unittest.TestCase):
    def test_every_row_is_ok_exactly_when_it_applies(self):
        for state, verb, t, target in all_rows():
            with self.subTest(state=state, verb=verb):
                rec, kwargs = scenario(state, verb, t)
                checked, outcome, unchanged = evaluate(rec, verb, kwargs)
                self.assertTrue(unchanged, "the record is never changed")
                self.assertEqual(checked, engine.Ok(verb, target))
                self.assertIsInstance(outcome, engine.Applied)
                self.assertEqual(engine.state_key(outcome.record), target)

    def test_a_refusal_leaves_the_record_deep_equal_and_emits_nothing(self):
        refused = 0
        for state, verb, t, _target in all_rows():
            rec, kwargs = scenario(state, verb, t)
            facts = set(kwargs["facts"])
            for fact in sorted(facts):
                with self.subTest(state=state, verb=verb, without=fact):
                    less = dict(kwargs, facts=facts - {fact})
                    checked, outcome, unchanged = evaluate(rec, verb, less)
                    self.assertTrue(unchanged)
                    self.assertIsInstance(checked, engine.Refused)
                    self.assertEqual(outcome, checked)
                    self.assertFalse(hasattr(outcome, "event"))
                    refused += 1
        self.assertGreater(refused, 100)


class TestEveryGuardRefusesWithItsKind(unittest.TestCase):
    """Each guard made false in isolation refuses with the kind registered
    for it."""

    def test_every_predicate_has_a_kind(self):
        for guard, predicate in engine.PREDICATES.items():
            with self.subTest(guard=guard):
                self.assertIsInstance(predicate.refusal, engine.Refusal)
        kinds = {p.refusal for g, p in engine.PREDICATES.items() if g in engine.table_guards()}
        self.assertEqual(kinds, {engine.Refusal.COMMAND_MISMATCH, engine.Refusal.PAUSED,
                                 engine.Refusal.FENCED, engine.Refusal.BUDGET_EXHAUSTED,
                                 engine.Refusal.FACT_MISSING, engine.Refusal.GUARD_UNMET})

    def test_the_kind_never_depends_on_the_wording(self):
        for guard, predicate in engine.PREDICATES.items():
            with self.subTest(guard=guard):
                expected = engine._refusal_of(predicate.name, predicate.kind)
                self.assertEqual(predicate.refusal, expected)

    def test_a_missing_fact_is_fact_missing(self):
        for state, verb, t, _target in all_rows():
            rec, kwargs = scenario(state, verb, t)
            for guard in t.guard:
                predicate = engine.PREDICATES[guard]
                if predicate.kind != "fact":
                    continue
                with self.subTest(state=state, verb=verb, guard=guard):
                    less = dict(kwargs, facts=set(kwargs["facts"]) - {predicate.name})
                    out = engine.preconditions(rec, verb, **{k: v for k, v in less.items()
                                                             if k != "cause_id"})
                    self.assertIsInstance(out, engine.Refused)
                    self.assertEqual((out.kind, out.guard),
                                     (engine.Refusal.FACT_MISSING, guard))

    def test_a_stale_revision_is_a_command_mismatch(self):
        for state, verb, t, _target in all_rows():
            if lc.EXACT_REVISION not in t.guard:
                continue
            rec, kwargs = scenario(state, verb, t)
            with self.subTest(state=state, verb=verb):
                stale = commands.Command(kwargs["command"].verb,
                                         dict(kwargs["command"].args, revision="99"))
                out = engine.preconditions(rec, verb, **dict(
                    {k: v for k, v in kwargs.items() if k != "cause_id"}, command=stale))
                self.assertEqual((out.kind, out.guard),
                                 (engine.Refusal.COMMAND_MISMATCH, lc.EXACT_REVISION))

    def test_a_spent_budget_is_budget_exhausted(self):
        checked = 0
        for state, verb, t, _target in all_rows():
            for guard in t.guard:
                name = guard.removesuffix(" remains")
                if name not in lc.BUDGETS or name not in engine.RECORD_COUNTERS \
                        and name not in ("operation_launch",):
                    continue
                rec, kwargs = scenario(state, verb, t)
                counts = dict(kwargs["counts"], **{name: lc.budget_limit(name, POLICY)})
                with self.subTest(state=state, verb=verb, budget=name):
                    out = engine.preconditions(rec, verb, **dict(
                        {k: v for k, v in kwargs.items() if k != "cause_id"}, counts=counts))
                    self.assertIsInstance(out, engine.Refused)
                    self.assertEqual((out.kind, out.guard),
                                     (engine.Refusal.BUDGET_EXHAUSTED, guard))
                    checked += 1
        self.assertGreater(checked, 20)

    def test_an_open_operation_fences(self):
        checked = 0
        for state, verb, t, _target in all_rows():
            if not ({"no open operation", lc.QUIESCENT} & set(t.guard)):
                continue
            rec, kwargs = scenario(state, verb, t)
            with self.subTest(state=state, verb=verb):
                out = engine.preconditions(rec, verb, **dict(
                    {k: v for k, v in kwargs.items() if k != "cause_id"},
                    open_operations=("op-open",)))
                self.assertIsInstance(out, engine.Refused)
                self.assertEqual(out.kind, engine.Refusal.FENCED)
                checked += 1
        self.assertGreater(checked, 10)

    def test_the_mask_of_the_flag_is_paused(self):
        rec = contribution(activity="fix_needed", plan_evidence_id="plan-1", scope_paths=["a"],
                           paused=True, paused_by="human", paused_at=NOW)
        out = engine.preconditions(rec, "request_patch", policy=POLICY, now=NOW,
                                   facts={"worker_slot_free", "capacity_available"},
                                   values={"active_task": {}})
        self.assertEqual(out.kind, engine.Refusal.PAUSED)

    def test_no_row_of_the_state_is_not_applicable(self):
        rec = contribution(activity="fix_needed")
        out = engine.preconditions(rec, "nominate_for_handoff", policy=POLICY, now=NOW)
        self.assertEqual((out.kind, out.verb), (engine.Refusal.NOT_APPLICABLE,
                                                "nominate_for_handoff"))
        with self.assertRaises(lc.TransitionError):
            engine.preconditions(rec, "not_a_verb", policy=POLICY, now=NOW)

    def test_a_record_the_schema_refuses_is_target_invalid(self):
        # The effects are simulated on a copy: the schema failure is a
        # refusal, before anything is applied.
        state, verb = "development.agent_running", "reject_patch_proposal"
        t = lc.find_edge(state, verb).transition
        base, kwargs = scenario(state, verb, t)
        base["fork"] = dict(base["fork"] or {}, head_sha="not a sha")
        before = copy.deepcopy(base)
        out = engine.try_apply_transition(base, verb, **kwargs)
        self.assertIsInstance(out, engine.Refused)
        self.assertEqual(out.kind, engine.Refusal.TARGET_INVALID)
        self.assertEqual(base, before)


class TestOneOf(unittest.TestCase):
    def test_the_one_admitted_row_is_taken_and_prepared_once(self):
        state = "development.fix_needed"
        t = lc.find_edge(state, "request_patch").transition
        rec, kwargs = scenario(state, "request_patch", t)
        prepared = []

        def prepare(verb):
            prepared.append(verb)
            return {"values": kwargs["values"]}

        common = {k: v for k, v in kwargs.items() if k != "values"}
        out = engine.try_apply_one_of(rec, ("request_repair", "request_patch"), prepare=prepare,
                                      **common)
        self.assertIsInstance(out, engine.Applied)
        self.assertEqual((out.verb, prepared), ("request_patch", ["request_patch"]))

    def test_no_admitted_row_returns_one_refusal_each(self):
        rec = contribution(activity="fix_needed", paused=True, paused_by="human", paused_at=NOW)
        out = engine.try_apply_one_of(rec, ("request_repair", "request_patch"), policy=POLICY,
                                      now=NOW)
        self.assertIsInstance(out, engine.Refusals)
        self.assertEqual([r.verb for r in out.refused], ["request_repair", "request_patch"])
        self.assertEqual(engine.refusal_kind(out), engine.Refusal.PAUSED)


if __name__ == "__main__":
    unittest.main()
