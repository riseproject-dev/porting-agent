# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Graph properties of the lifecycle table and drift of the pages generated from it."""

import dataclasses
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import lifecycle as lc
from scripts import generate_state_machine_doc as gen
from scripts.state_machine_decisions import DECISIONS
from tests import helpers

ROOT = Path(__file__).parents[1]
POLICY = json.loads((ROOT / "policy.json").read_text())


def policy_value(key: str):
    value = POLICY
    for part in key.split("."):
        value = value[part]
    return value


class TestLifecycleTable(unittest.TestCase):
    def test_consistency_check_passes(self):
        self.assertEqual(lc.check_table(POLICY), [])

    def test_states_are_phase_activity(self):
        self.assertIn(lc.INITIAL_STATE, lc.LIFECYCLE)
        for state in lc.LIFECYCLE:
            phase, activity = state.split(".")
            self.assertTrue(phase and activity, state)

    def test_every_target_exists_and_verbs_are_unique_per_state(self):
        for state in lc.LIFECYCLE:
            verbs = [e.verb for e in lc.state_edges(state)]
            self.assertEqual(len(verbs), len(set(verbs)), state)
            for e in lc.state_edges(state):
                self.assertIn(e.target, lc.LIFECYCLE, (state, e.verb))
                self.assertTrue(e.transition.guard, (state, e.verb))
                if e.transition.trigger == lc.Trigger.COMMAND:
                    self.assertTrue(e.transition.command, (state, e.verb))

    def test_every_state_is_reachable_from_the_initial_state(self):
        reached, todo = {lc.INITIAL_STATE}, [lc.INITIAL_STATE]
        while todo:
            for e in lc.state_edges(todo.pop()):
                if e.target not in reached:
                    reached.add(e.target)
                    todo.append(e.target)
        self.assertEqual(reached, set(lc.LIFECYCLE))

    def test_terminal_states_have_no_exits_and_others_do(self):
        self.assertEqual(lc.TERMINAL_STATES, {"released.idle", "closed.idle"})
        for state, spec in lc.LIFECYCLE.items():
            leaves = [e for e in lc.state_edges(state) if e.target != state]
            if state in lc.TERMINAL_STATES:
                self.assertEqual(spec.transitions, {}, state)
            else:
                self.assertTrue(leaves, state)

    def test_locked_upstream_pr_wait_has_exactly_two_exits(self):
        exits = {e.verb: e.target for e in lc.state_edges("handoff.waiting_upstream_pr")
                 if e.target != "handoff.waiting_upstream_pr"}
        self.assertEqual(exits, {"verify_upstream_pr": "upstream_review.waiting_ci",
                                 "retract_submission": "development.fix_needed"})

    def test_budgets_name_policy_keys_and_reachable_exhaustion(self):
        charged = set()
        for e in lc.table_edges():
            for name in e.transition.charges:
                charged.add(name)
                self.assertIn(name, lc.BUDGETS, (e.source, e.verb))
                budget = lc.BUDGETS[name]
                self.assertIsInstance(policy_value(budget.policy_key), int, budget.policy_key)
                self.assertGreater(lc.budget_limit(name, POLICY), 0, name)
                exhaustion = [x for x in lc.exhaustion_edges(name) if x.source in (e.source, e.target)]
                self.assertTrue(exhaustion, (e.source, e.verb, name))
        self.assertEqual(charged, set(lc.BUDGETS))

    def test_flags_keep_the_state(self):
        for flag in lc.FLAGS.values():
            for t in (flag.set_by, flag.cleared_by):
                self.assertEqual(t.trigger, lc.Trigger.COMMAND)
            self.assertEqual(set(flag.mask), set(lc.Trigger))

    def test_existing_constants_agree_with_the_table(self):
        writes = {lc.state_phase(s) for s, spec in lc.LIFECYCLE.items() if spec.writes}
        self.assertEqual(writes, set(lc.WRITE_PHASES))
        phase, activity = lc.INITIAL_STATE.split(".")
        record = helpers.contribution(phase=lc.state_phase(lc.INITIAL_STATE))
        fresh = lc.new_contribution(
            contribution_id=record["contribution_id"], project_id=record["project_id"],
            issue=record["tracking_issue"], upstream=record["upstream"]["full_name"],
            distributions=record["distributions"], base_ref=None, policy_id="test", now=helpers.NOW)
        record = fresh
        self.assertEqual((record["phase"], record["activity"]), (phase, activity))

    def test_table_is_a_definition_without_annotations(self):
        source = (ROOT / "lifecycle.py").read_text()
        table = source[source.index("# Lifecycle table"):]
        for pattern in (r"\bplan\b", r"\bgap\b", r"\b\w+\.py:\d+", r"\bstatus\b", r"\bimplemented\b"):
            self.assertIsNone(re.search(pattern, table, re.IGNORECASE), pattern)


def _with_state(key: str, **changes) -> dict:
    return {key: dataclasses.replace(lc.LIFECYCLE[key], **changes)}


class TestConsistencyCheckViolations(unittest.TestCase):
    """The check reports each kind of inconsistency with a message naming it."""

    def problems(self, states: dict) -> list[str]:
        with mock.patch.dict(lc.LIFECYCLE, states):
            return lc.check_table(POLICY)

    def assertReports(self, states: dict, *fragments: str) -> None:
        problems = self.problems(states)
        for fragment in fragments:
            self.assertTrue(any(fragment in p for p in problems), (fragment, problems))

    def test_one_verb_from_a_state_leads_to_two_targets(self):
        transitions = dict(lc.LIFECYCLE["development.waiting_ci"].transitions)
        transitions[("record_ci_failure", "development.waiting_human")] = lc.Transition(
            lc.Trigger.OBSERVATION, None, ("an executed blocking failure",),
            lc.Effects(sets=("pending_human_action",)))
        self.assertReports(
            _with_state("development.waiting_ci", transitions=transitions),
            "development.waiting_ci --record_ci_failure-->: the verb leads to 2 targets "
            "(development.fix_needed, development.waiting_human)")

    def test_several_verbs_may_lead_to_the_same_target(self):
        targets = [e.target for e in lc.state_edges("development.waiting_ci")]
        self.assertGreater(targets.count("development.fix_needed"), 1)
        self.assertGreater(targets.count("development.waiting_ci"), 1)
        self.assertEqual(lc.check_table(POLICY), [])

    def test_unknown_target(self):
        transitions = {("go", "nowhere.idle"): lc.Transition(lc.Trigger.TICK, None, (lc.ALWAYS,))}
        self.assertReports(_with_state("queued.idle", transitions=transitions),
                           "queued.idle --go--> nowhere.idle: target is not a table state")

    def test_dead_end_that_is_not_an_end_state(self):
        self.assertReports(_with_state("parked.idle", transitions={}),
                           "parked.idle: not an end state but no transition leaves it")

    def test_end_state_with_exits(self):
        transitions = {("reopen", "queued.idle"): lc.Transition(
            lc.Trigger.COMMAND, "/upstreaming reopen", ("exact revision",))}
        self.assertReports(_with_state("closed.idle", transitions=transitions),
                           "closed.idle: end state has outgoing transitions")

    def test_state_that_cannot_reach_an_end_state(self):
        loop = {("go", "release_wait.waiting_human"): lc.Transition(
            lc.Trigger.OBSERVATION, None, ("release evidence needs operator action",),
            lc.Effects(sets=("pending_human_action",)))}
        back = {("resume_release_observation", "release_wait.idle"): lc.Transition(
            lc.Trigger.COMMAND, lc.RESUME, ("exact revision",))}
        self.assertReports(
            {**_with_state("release_wait.idle", transitions=loop),
             **_with_state("release_wait.waiting_human", transitions=back)},
            "release_wait.idle: no end state is reachable from this state",
            "released.idle: end state is not reachable")

    def test_empty_guard_and_missing_command(self):
        transitions = dict(lc.LIFECYCLE["queued.idle"].transitions)
        transitions[("begin_investigation", "investigation.idle")] = lc.Transition(lc.Trigger.TICK)
        transitions[("withdraw_request", "closed.idle")] = lc.Transition(
            lc.Trigger.COMMAND, None, ("exact revision",))
        self.assertReports(
            _with_state("queued.idle", transitions=transitions),
            "queued.idle --begin_investigation--> investigation.idle: empty guard",
            "queued.idle --withdraw_request--> closed.idle: command trigger without")

    def test_unknown_budget_and_missing_revision_bump(self):
        transitions = dict(lc.LIFECYCLE["development.waiting_ci"].transitions)
        transitions[("record_actionable_review", "development.fix_needed")] = lc.Transition(
            lc.Trigger.OBSERVATION, None, ("authorized actionable review feedback",),
            lc.Effects(sets=("pending_feedback_evidence_ids",)), ("review_rounds",))
        self.assertReports(
            _with_state("development.waiting_ci", transitions=transitions),
            "charges unknown budget 'review_rounds'",
            "changes ['pending_feedback_evidence_ids'] without a revision bump")


class TestDiagrams(unittest.TestCase):
    def test_diagrams_draw_only_table_entries_and_cover_the_table(self):
        drawn_edges, drawn_states, flags = set(), set(), set()
        for section in lc.DIAGRAM_SECTIONS:
            for d in section.diagrams:
                for node in d.nodes:
                    self.assertIn(node, lc.LIFECYCLE, d.title)
                drawn_states.update(d.nodes)
                for node in (d.start, *d.ends):
                    if node:
                        self.assertIn(node, d.nodes, d.title)
                flags.update(d.flags)
                for source, verb in d.edges:
                    _t, target = lc.resolve_node(source, verb)
                    self.assertIn(target, d.nodes, (d.title, source, verb))
                    drawn_edges.add((source, verb, target))
        self.assertEqual(drawn_states, set(lc.LIFECYCLE))
        self.assertEqual(drawn_edges, {(e.source, e.verb, e.target) for e in lc.table_edges()})
        self.assertEqual(flags, set(lc.FLAGS))

    def test_overview_diagram_structure_matches_the_table(self):
        index = gen.render_index()
        block = index.split("```mermaid\n", 1)[1].split("\n```", 1)[0]
        phases, node_state, edges = [], {}, []
        current = None
        for line in block.splitlines():
            line = line.strip()
            if m := re.fullmatch(r'subgraph (\w+)\["(\w+)"\]', line):
                self.assertEqual(m[1], m[2])
                current = m[1]
                phases.append(current)
            elif line == "end":
                current = None
            elif m := re.fullmatch(r'(\w+)\["([\w.]+)"\]', line):
                self.assertEqual(lc.state_phase(m[2]), current, m[2])
                self.assertEqual(m[1], gen.node_id(m[2]))
                node_state[m[1]] = m[2]
            elif m := re.fullmatch(r"(\w+) -->\|([^|]+)\| (\w+)", line):
                edges.append((m[1], m[2].split("<br/>")[0], m[3]))
        self.assertEqual(set(phases), {lc.state_phase(s) for s in lc.LIFECYCLE})
        self.assertEqual(len(phases), len(set(phases)))
        for source, verb, target in edges:
            if source.startswith("start_"):
                self.assertEqual((verb, node_state[target]), (lc.INITIAL_VERB, lc.INITIAL_STATE))
                continue
            state = node_state[source]
            self.assertEqual(lc.find_edge(state, verb).target, node_state[target], (state, verb))
        self.assertGreater(len(edges), 40)

    def test_decision_slices_use_table_entries(self):
        for d in DECISIONS:
            states = {n.partition("+")[0] for n in d.nodes}
            self.assertLessEqual(len(states), 6, d.title)
            for source, verb in d.edges:
                self.assertIn(source, d.nodes, d.title)
                _t, target = lc.resolve_node(source, verb)
                self.assertIn(target, d.nodes, (d.title, source, verb))
            for edge in d.contested:
                self.assertIn(edge, d.edges, d.title)
            for c in d.candidates:
                self.assertIn(c.source, d.nodes, d.title)
                self.assertIn(c.target, d.nodes, d.title)
                self.assertIn(c.option, {letter for letter, _ in d.options}, d.title)
            proposed = {s for s in states if s not in lc.LIFECYCLE}
            used = {c.target for c in d.candidates} | {c.source for c in d.candidates}
            self.assertLessEqual(proposed, {n.partition("+")[0] for n in used}, d.title)


class TestGeneratedDocs(unittest.TestCase):
    def test_committed_pages_match_the_table(self):
        for path, text in gen.render(POLICY).items():
            self.assertEqual((ROOT / path).read_text(), text,
                             f"{path} is stale; run python -m scripts.generate_state_machine_doc")


if __name__ == "__main__":
    unittest.main()
