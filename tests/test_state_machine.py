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
import reconcile
from scripts import generate_state_machine_doc as gen
from scripts.state_machine_decisions import (
    DECISIONS,
    FURTHER_CHANGES,
    OPEN_ITEMS,
    SETTLED_OPEN_ITEMS,
)
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
                self.assertEqual(leaves, [], state)
                self.assertEqual([e.verb for e in lc.state_edges(state)], ["record_late_effect"])
            else:
                self.assertTrue(leaves, state)
        self.assertNotIn("waiting_capacity", {s.split(".")[1] for s in lc.LIFECYCLE})

    def test_locked_upstream_pr_wait_has_exactly_three_exits(self):
        exits = {e.verb: e.target for e in lc.state_edges("handoff.waiting_upstream_pr")
                 if e.target != "handoff.waiting_upstream_pr"}
        self.assertEqual(exits, {"verify_upstream_pr": "upstream_review.waiting_ci",
                                 "retract_submission": "development.fix_needed",
                                 "refresh_planning": "queued.idle"})
        self.assertEqual(lc.find_edge("handoff.waiting_upstream_pr", "refresh_planning").transition.guard,
                         lc.find_edge("handoff.waiting_upstream_pr", "retract_submission").transition.guard)

    def test_rules_expand_into_the_table(self):
        self.assertEqual(set(lc.RULES), {"park_contribution", "resume_stale", "record_reminder",
                                         "record_late_effect", "record_source_unavailable"})
        self.assertEqual(set(lc.rule_states("record_source_unavailable")),
                         set(lc.BASE_BRANCH_WAITS))
        self.assertEqual(set(lc.rule_states("record_late_effect")), set(lc.LIFECYCLE))
        self.assertEqual(
            set(lc.rule_states("record_reminder")),
            {k for k, s in lc.LIFECYCLE.items() if s.kind in ("gate", "human_wait", "locked")})
        for state in ("handoff.awaiting_submission", "handoff.waiting_upstream_pr",
                      "handoff.waiting_human"):
            self.assertIn(state, lc.rule_states("record_reminder"))
            self.assertTrue(lc.is_rule_row(lc.find_edge(state, "record_reminder")))
        parks = set(lc.rule_states("park_contribution"))
        self.assertIn("upstream_review.waiting_human", parks)
        self.assertIn("development.agent_running", parks)
        for state in ("parked.idle", "closed.idle", "released.idle", "handoff.waiting_upstream_pr",
                      "release_wait.idle", "release_wait.waiting_human", "upstream_review.waiting_ci",
                      "upstream_review.fix_needed", "upstream_review.agent_running"):
            self.assertNotIn(state, parks)
        self.assertEqual(set(lc.rule_states("resume_stale")),
                         {k for k in lc.LIFECYCLE if lc.state_phase(k) in ("provisioning", "development")})
        for verb, rule in lc.RULES.items():
            for state in lc.rule_states(verb):
                edge = lc.find_edge(state, verb)
                self.assertIs(edge.transition, rule.transition)
                self.assertEqual(edge.target, lc.resolve_target(state, rule.target))

    def test_nothing_parks_from_upstream_review_automatically(self):
        for e in lc.table_edges():
            if lc.state_phase(e.source) == "upstream_review" and e.target == "parked.idle":
                self.assertEqual((e.source, e.verb), ("upstream_review.waiting_human", "park_contribution"))
        self.assertIn("no open upstream PR", lc.find_edge("parked.idle", "restart_generation").transition.guard)
        self.assertEqual(lc.find_edge("parked.idle", "abandon").target, "closed.idle")

    def test_every_stop_clears_the_pending_publication(self):
        stops = dict(lc.stop_transitions())
        for where in ("flag paused pause", "flag paused pause_on_identity_change",
                      "flag paused pause_on_processing_error",
                      "development.agent_running --park_contribution-->",
                      "development.waiting_human --park_contribution-->",
                      "upstream_review.waiting_human --park_contribution-->",
                      "planning.agent_running --park_planning-->",
                      "planning.agent_running --close_planning-->",
                      "development.fix_needed --exhaust_repair_attempts-->",
                      "parked.idle --abandon-->"):
            self.assertIn(lc.HELD_RESULT, stops[where].effects.clears, where)
        for where, t in stops.items():
            if t.effects.work == "clear" or t.trigger == lc.Trigger.COMMAND:
                self.assertIn(lc.HELD_RESULT, t.effects.clears, where)
            # `proposal_artifact` holds a published Result only; no stop clears it.
            self.assertNotIn("proposal_artifact", t.effects.clears, where)
        self.assertFalse(hasattr(lc, "HELD_PROPOSAL"))
        # The check reports a stop row, a rule, or a flag verb that keeps it.
        keep = lc.Effects(work="clear", sets=("parked_reason", "previous_phase"))
        park = lc.RULES["park_contribution"].transition
        key = ("park_contribution", "parked.idle")
        with mock.patch.dict(lc.LIFECYCLE["development.agent_running"].transitions,
                             {key: dataclasses.replace(park, effects=keep)}):
            self.assertTrue(any(
                p.startswith("development.agent_running --park_contribution-->: a stop")
                and lc.HELD_RESULT in p for p in lc.check_table(POLICY)))
        abandon = lc.find_edge("parked.idle", "abandon").transition
        with mock.patch.dict(lc.LIFECYCLE["parked.idle"].transitions, {("abandon", "closed.idle"): (
                dataclasses.replace(abandon, effects=lc.Effects(sets=abandon.effects.sets)))}):
            self.assertTrue(any(p.startswith("parked.idle --abandon-->: a stop")
                                for p in lc.check_table(POLICY)))
        pause = lc.FLAGS["paused"].set_by["pause"]
        effects = dataclasses.replace(pause.effects, clears=())
        with mock.patch.dict(lc.FLAGS["paused"].set_by,
                             {"pause": dataclasses.replace(pause, effects=effects)}):
            self.assertTrue(any(p.startswith("flag paused pause: a stop")
                                for p in lc.check_table(POLICY)))

    def test_a_generation_end_restarts_by_refresh_and_never_by_resume(self):
        key = ("restart_generation", "queued.idle")
        restart = lc.LIFECYCLE["parked.idle"].transitions[key]
        with mock.patch.dict(lc.LIFECYCLE["parked.idle"].transitions,
                             {key: dataclasses.replace(restart, command=lc.RESUME)}):
            problems = lc.check_table(POLICY)
        self.assertIn("parked.idle: generation_end state has no `/upstreaming refresh` exit",
                      problems)
        self.assertTrue(any(p.startswith("parked.idle: generation_end state has a "
                                         "`/upstreaming resume` row") for p in problems), problems)

    def mutated(self, state, verb, **changes):
        """The problems the check reports when one row is changed."""
        edge = lc.find_edge(state, verb)
        key = next(k for k in lc.LIFECYCLE[state].transitions if k[0] == verb)
        t = edge.transition
        effects = dataclasses.replace(t.effects, **{k: v for k, v in changes.items()
                                                    if k in ("sets", "clears")})
        row = dataclasses.replace(t, effects=effects, **{k: v for k, v in changes.items()
                                                         if k in ("guard", "charges")})
        with mock.patch.dict(lc.LIFECYCLE[state].transitions, {key: row}):
            return lc.check_table(POLICY)

    def without(self, entries, *names):
        return tuple(e for e in entries if e not in names)

    def test_every_work_allocation_requires_the_bound_upstream_identity(self):
        allocating = [e for e in lc.table_edges()
                      if e.transition.effects.work in ("allocate", "reallocate")]
        self.assertEqual({e.verb for e in allocating},
                         {"dispatch_planning", "request_patch", "request_repair",
                          "request_submission_rewrite", "request_submission_review",
                          "request_upstream_revision", "request_review_revision",
                          "allocate_work_retry"})
        for e in allocating:
            self.assertIn(lc.UPSTREAM_BOUND, e.transition.guard, f"{e.source} --{e.verb}-->")
        for state, verb in (("planning.idle", "dispatch_planning"),
                            ("upstream_review.fix_needed", "request_review_revision"),
                            ("development.agent_running", "allocate_work_retry")):
            with self.subTest(verb=verb):
                guard = lc.find_edge(state, verb).transition.guard
                problems = self.mutated(state, verb, guard=self.without(guard, lc.UPSTREAM_BOUND))
                self.assertTrue(any(p.startswith(f"{state} --{verb}-->") and lc.UPSTREAM_BOUND in p
                                    for p in problems), problems)

    def test_the_check_holds_the_feedback_rules(self):
        patch = lc.find_edge("development.fix_needed", "request_patch").transition
        published = lc.find_edge("development.agent_running", "record_publish_success").transition
        rerun = lc.find_edge("development.agent_running", "request_ci_rerun").transition
        resume = lc.find_edge("development.waiting_human", "resume_development").transition
        cases = (
            ("development.fix_needed", "request_patch",
             {"sets": self.without(patch.effects.sets, lc.WORK_FEEDBACK)}, lc.WORK_FEEDBACK),
            ("development.agent_running", "record_publish_success",
             {"clears": self.without(published.effects.clears, lc.CARRIED_FEEDBACK)},
             "rows that record a published Result"),
            ("development.agent_running", "request_ci_rerun",
             {"guard": self.without(rerun.guard, lc.WORK_WITHOUT_FEEDBACK)},
             "a CI rerun without the guard"),
            ("development.waiting_human", "resume_development",
             {"clears": (*resume.effects.clears, "pending_feedback_evidence_ids")},
             "clears pending_feedback_evidence_ids"),
        )
        for state, verb, change, expected in cases:
            with self.subTest(verb=verb):
                problems = self.mutated(state, verb, **change)
                self.assertTrue(any(p.startswith(f"{state} --{verb}-->") and expected in p
                                    for p in problems), problems)
        for verb in ("reject_patch_proposal", "replan_development"):
            clears = lc.find_edge("development.agent_running", verb).transition.effects.clears
            self.assertFalse({"pending_feedback_evidence_ids", lc.CARRIED_FEEDBACK} & set(clears))
        self.assertEqual(lc.FEEDBACK_ANSWERED_BY, ("resume_upstream_review",))

    def test_passing_ci_with_pending_feedback_makes_work_due(self):
        edge = lc.find_edge("development.waiting_ci", "return_for_feedback")
        t = edge.transition
        self.assertEqual((edge.target, t.trigger, t.command, t.charges),
                         ("development.fix_needed", lc.Trigger.OBSERVATION, None, ()))
        self.assertEqual(t.guard, ("every required CI job executed and passed on the exact head",
                                   lc.PENDING_FEEDBACK, "no requested Work"))
        self.assertEqual((t.effects.sets, t.effects.clears, t.effects.revision, t.effects.work),
                         (("last_ci_evidence_id",), (), "keep", "keep"))
        self.assertFalse(lc.must_bump(edge))
        self.assertIn(lc.NO_PENDING_FEEDBACK,
                      lc.find_edge("development.waiting_ci", "nominate_for_handoff")
                      .transition.guard)
        self.assertEqual(lc.EXCLUSIVE_OBSERVATIONS["development.waiting_ci"],
                         ("nominate_for_handoff", "return_for_feedback"))
        self.assertIn(((lc.NO_PENDING_FEEDBACK,), (lc.PENDING_FEEDBACK,)), lc.EXCLUSIVE_GUARDS)
        key = next(k for k in lc.LIFECYCLE["development.waiting_ci"].transitions
                   if k[0] == "return_for_feedback")
        with mock.patch.dict(lc.LIFECYCLE["development.waiting_ci"].transitions):
            del lc.LIFECYCLE["development.waiting_ci"].transitions[key]
            problems = lc.check_table(POLICY)
        self.assertTrue(any(p.startswith("development.waiting_ci: blocks a nomination on")
                            for p in problems), problems)
        problems = self.mutated("development.waiting_ci", "return_for_feedback",
                                guard=self.without(t.guard, lc.PENDING_FEEDBACK))
        self.assertTrue(any("nominate_for_handoff and return_for_feedback can apply together" in p
                            for p in problems), problems)

    def test_the_check_holds_the_settled_rows(self):
        gate = "handoff.awaiting_submission"
        review = lc.find_edge(gate, "record_actionable_review").transition
        dev_review = lc.find_edge("development.waiting_ci", "record_actionable_review").transition
        due_review = lc.find_edge("development.fix_needed", "record_actionable_review")
        self.assertEqual((due_review.target, due_review.transition.trigger,
                          due_review.transition.effects.revision, due_review.transition.charges),
                         ("development.fix_needed", lc.Trigger.OBSERVATION, "bump", ()))
        due_review = due_review.transition
        repair = lc.find_edge(gate, "return_for_repair").transition
        clean = "handoff.waiting_ci"
        clean_repair = lc.find_edge(clean, "return_for_repair").transition
        explain = lc.find_edge(gate, "record_explanation").transition
        revision = lc.find_edge("upstream_review.fix_needed", "request_review_revision").transition
        patch = lc.find_edge("development.fix_needed", "request_patch").transition
        resume = lc.find_edge("development.waiting_human", "resume_development").transition
        cases = (
            (gate, "record_actionable_review",
             {"sets": self.without(review.effects.sets, "pending_feedback_evidence_ids")},
             "recorded as pending_feedback_evidence_ids"),
            (gate, "record_actionable_review",
             {"clears": self.without(review.effects.clears, "handoff_request")},
             "leaving the gate clears"),
            (gate, "record_actionable_review",
             {"sets": (*review.effects.sets, "last_ci_evidence_id")}, "never CI evidence"),
            ("development.waiting_ci", "record_actionable_review",
             {"clears": (*dev_review.effects.clears, "last_ci_evidence_id")}, "never CI evidence"),
            ("development.fix_needed", "record_actionable_review",
             {"sets": self.without(due_review.effects.sets, "pending_feedback_evidence_ids")},
             "recorded as pending_feedback_evidence_ids"),
            ("development.fix_needed", "record_actionable_review",
             {"sets": (*due_review.effects.sets, "last_ci_evidence_id")}, "never CI evidence"),
            (gate, "return_for_repair",
             {"sets": self.without(repair.effects.sets, "last_ci_evidence_id")},
             "records its evidence"),
            (gate, "return_for_repair",
             {"sets": (*repair.effects.sets, "pending_feedback_evidence_ids")},
             "records no pending feedback"),
            (clean, "return_for_repair",
             {"sets": self.without(clean_repair.effects.sets, "last_ci_evidence_id")},
             "records its evidence"),
            (clean, "return_for_repair",
             {"sets": (*clean_repair.effects.sets, "pending_feedback_evidence_ids")},
             "records no pending feedback"),
            (clean, "return_for_repair",
             {"clears": self.without(clean_repair.effects.clears, "handoff_request")},
             "leaving the handoff clears"),
            (gate, "record_explanation",
             {"clears": (*explain.effects.clears, "last_ci_evidence_id")},
             "keeps last_ci_evidence_id"),
            ("upstream_review.fix_needed", "request_review_revision",
             {"charges": (*revision.charges, "no_progress")}, "charges no no_progress"),
            ("upstream_review.fix_needed", "request_review_revision",
             {"guard": self.without(revision.guard, lc.CONTROLLED_REF)}, "controlled fork ref"),
            ("development.fix_needed", "request_patch",
             {"guard": (*patch.guard, "a proposal is admitted")}, "write admission only"),
            ("development.waiting_human", "resume_development",
             {"clears": self.without(resume.effects.clears, lc.HELD_RESULT)},
             f"clears {lc.HELD_RESULT}"),
        )
        for state, verb, change, expected in cases:
            with self.subTest(verb=verb, expected=expected):
                problems = self.mutated(state, verb, **change)
                self.assertTrue(any(p.startswith(f"{state} --{verb}-->") and expected in p
                                    for p in problems), problems)

    def test_the_check_holds_the_rerun_counts_and_the_cluster_charge(self):
        head = lc.find_edge("development.waiting_ci", "record_head_change").transition
        review = lc.find_edge("upstream_review.fix_needed", "request_review_revision").transition
        patch = lc.find_edge("development.fix_needed", "request_patch").transition
        admit = lc.find_edge("planning.agent_running", "admit_development").transition
        gate = lc.find_edge("handoff.awaiting_submission", "record_actionable_review").transition
        cases = (
            ("development.waiting_ci", "record_head_change",
             {"sets": self.without(head.effects.sets, lc.RERUNS)}, lc.RERUNS),
            ("upstream_review.fix_needed", "request_review_revision",
             {"charges": ("repair_attempts_cluster", "work_total"),
              "guard": (*review.guard, "repair_attempts_cluster remains")},
             "repair_attempts_cluster is charged exactly when"),
            ("development.fix_needed", "request_patch",
             {"guard": tuple("proposal or write admission" if g == "write admission" else g
                             for g in patch.guard)}, "proposal admission guard"),
            ("development.fix_needed", "request_patch",
             {"guard": self.without(patch.guard, lc.NO_PROGRESS_REMAINS)},
             "without requiring that no_progress remains"),
            ("planning.agent_running", "admit_development",
             {"guard": self.without(admit.guard, lc.PATCH_SCOPE_COMPLETE)},
             lc.PATCH_SCOPE_COMPLETE),
        )
        for state, verb, change, expected in cases:
            with self.subTest(verb=verb, expected=expected):
                problems = self.mutated(state, verb, **change)
                self.assertTrue(any(expected in p for p in problems), problems)
        problems = self.mutated("handoff.awaiting_submission", "record_actionable_review",
                                guard=self.without(gate.guard, lc.CANDIDATE_NOT_FAILED))
        self.assertIn("handoff.awaiting_submission: return_for_repair and "
                      "record_actionable_review can apply together; their guards are not "
                      "exclusive", problems)
        self.assertEqual(
            {e.verb for e in lc.table_edges() if "repair_attempts_cluster" in e.transition.charges},
            {"request_repair", "request_upstream_revision"})
        self.assertEqual(lc.BUDGETS["flaky_reruns"].counter.split(",")[0], "`budget.reruns.flaky`")
        self.assertEqual(lc.BUDGETS["interrupted_reruns"].counter.split(",")[0],
                         "`budget.reruns.interrupted`")

    def test_every_work_allocation_charges_work_total(self):
        allocating = [e for e in lc.table_edges() if e.transition.effects.work in ("allocate", "reallocate")]
        self.assertEqual({e.verb for e in allocating},
                         {"dispatch_planning", "request_patch", "request_repair",
                          "request_upstream_revision", "request_review_revision",
                          "request_submission_rewrite", "request_submission_review",
                          "allocate_work_retry"})
        for e in allocating:
            self.assertIn("work_total", e.transition.charges, (e.source, e.verb))

    def test_revision_bumps_follow_the_rule(self):
        for e in lc.table_edges():
            self.assertEqual(e.transition.effects.revision == "bump", lc.must_bump(e), (e.source, e.verb))
            if e.transition.command == lc.RESUME:
                self.assertEqual(e.transition.effects.revision, "bump", (e.source, e.verb))
        self.assertEqual(lc.find_edge("provisioning.idle", "confirm_provisioning").transition.effects.revision,
                         "keep")
        for state, verb in (("handoff.waiting_human", "resume_upstream_branch"),
                            ("handoff.waiting_human", "resume_submission_review"),
                            ("handoff.waiting_human", "resume_submission_preparation"),
                            ("release_wait.waiting_human", "resume_release_observation")):
            self.assertEqual(lc.find_edge(state, verb).transition.effects.revision, "bump")

    def test_stale_and_normal_resumes_are_complementary(self):
        stale = set(lc.rule_states("resume_stale"))
        self.assertIn(lc.STALE, lc.RULES["resume_stale"].transition.guard)
        for e in lc.table_edges():
            if e.transition.command == lc.RESUME and e.verb != "resume_stale":
                self.assertEqual(lc.NOT_STALE in e.transition.guard, e.source in stale, (e.source, e.verb))
        self.assertIn(lc.FLAG_NOT_STALE, lc.FLAGS["paused"].cleared_by["resume_pause"].guard)
        self.assertEqual(policy_value(lc.STALE_KEY), 7)

    def test_resume_targets_in_upstream_review(self):
        self.assertEqual(lc.find_edge("upstream_review.waiting_human", "resume_upstream_review").target,
                         "upstream_review.waiting_ci")
        self.assertEqual(lc.find_edge("upstream_review.waiting_human", "resume_upstream_revision").target,
                         "upstream_review.fix_needed")
        self.assertEqual(lc.find_edge("upstream_review.waiting_ci", "record_head_change").target,
                         "upstream_review.waiting_human")
        review = lc.find_edge("upstream_review.waiting_human", "resume_upstream_review").transition
        revision = lc.find_edge("upstream_review.waiting_human", "resume_upstream_revision").transition
        for kind in ("pr_closed_unmerged", "answer_reviewer", "upstream_head_changed"):
            self.assertTrue(lc._admits(review, kind) and not lc._admits(revision, kind), kind)
        for kind in ("dispatch_failed", "work_failed", "publication_infrastructure_failure",
                     "repair_budget_exhausted"):
            self.assertTrue(lc._admits(revision, kind) and not lc._admits(review, kind), kind)
        self.assertIn("pending_feedback_evidence_ids", review.effects.clears)
        self.assertNotIn("pending_feedback_evidence_ids", revision.effects.clears)
        self.assertIn("not yet recorded for this head and CI run",
                      lc.find_edge("upstream_review.waiting_ci", "record_upstream_ci_failure").transition.guard)

    def test_one_state_in_which_development_work_is_due(self):
        self.assertNotIn("development.idle", lc.LIFECYCLE)
        self.assertEqual(lc.find_edge("provisioning.idle", "confirm_provisioning").target,
                         "development.fix_needed")
        patch = lc.find_edge("development.fix_needed", "request_patch")
        repair = lc.find_edge("development.fix_needed", "request_repair")
        self.assertEqual(patch.target, repair.target)
        self.assertIn("no failing CI job is recorded for the head", patch.transition.guard)
        self.assertIn("a failing CI job is recorded for the head", repair.transition.guard)
        # Patch Work needs the fork: write admission only, and no proposal before it.
        self.assertIn("write admission", patch.transition.guard)
        self.assertFalse(any("proposal" in g for g in patch.transition.guard))
        self.assertEqual([e.source for e in lc.table_edges() if e.verb == "exhaust_repair_attempts"],
                         ["development.fix_needed", "upstream_review.fix_needed"])
        self.assertEqual([e.source for e in lc.table_edges() if e.verb == "refresh_planning"],
                         ["development.waiting_ci", "development.fix_needed",
                          "development.waiting_human", "handoff.awaiting_submission",
                          "handoff.waiting_upstream_pr"])

    def test_resume_stale_requires_quiescence(self):
        guard = lc.RULES["resume_stale"].transition.guard
        refresh = lc.find_edge("development.fix_needed", "refresh_planning").transition.guard
        for condition in (lc.QUIESCENT, lc.NO_WORK):
            self.assertIn(condition, guard)
            self.assertIn(condition, refresh)

    def test_resumes_of_a_human_wait_are_mutually_exclusive(self):
        waits = {k for k, s in lc.LIFECYCLE.items() if s.kind == "human_wait"}
        self.assertEqual({k for k in waits if lc.state_phase(k) == "handoff"},
                         {"handoff.waiting_human"})
        self.assertEqual(
            {e.verb: e.target for e in lc.state_edges("handoff.waiting_human")
             if e.transition.command == lc.RESUME},
            {"resume_submission_review": "handoff.review_due",
             "resume_clean_head_ci": "handoff.waiting_ci",
             "resume_upstream_branch": "handoff.building_upstream_branch",
             "resume_ci": "handoff.waiting_ci",
             "resume_submission_preparation": "handoff.preparing_submission"})
        self.assertEqual(lc.pending_kinds("handoff.waiting_human"),
                         {"work_budget_exhausted", "dispatch_failed", "work_failed",
                          "upstream_branch_failed", "upstream_branch_blocked", "ci_interrupted",
                          "submission_comment_failed", "clean_head_ci_missing",
                          "base_branch_unavailable", "upstream_unavailable"})
        for key in ("handoff.waiting_human", "development.waiting_human",
                    "upstream_review.waiting_human"):
            resumes = [e for e in lc.state_edges(key)
                       if e.transition.command == lc.RESUME and not lc.is_rule_row(e)]
            self.assertGreater(len(resumes), 1, key)
            for kind in lc.pending_kinds(key):
                applies = [e.verb for e in resumes if lc._admits(e.transition, kind)]
                self.assertEqual(len(applies), 1, (key, kind, applies))

    def test_starting_clean_head_ci_again_restarts_its_bounded_wait(self):
        for state, verb in (("handoff.waiting_ci", "rerun_interrupted_ci"),
                            ("handoff.waiting_human", "resume_ci"),
                            ("handoff.waiting_human", "resume_clean_head_ci")):
            with self.subTest(verb=verb):
                self.assertIn("clean_head_ci_since",
                              lc.find_edge(state, verb).transition.effects.sets)
        # In development there is no bounded wait to restart.
        for state, verb in (("development.waiting_ci", "rerun_interrupted_ci"),
                            ("development.waiting_human", "resume_ci")):
            self.assertNotIn("clean_head_ci_since",
                             lc.find_edge(state, verb).transition.effects.sets)
        self.assertEqual(lc.find_edge("handoff.waiting_ci", "rerun_interrupted_ci")
                         .transition.charges, ("interrupted_reruns",))

    def test_resume_after_exhaustion_restarts_the_named_counter(self):
        cases = (("planning.idle", "exhaust_work_total", "resume_planning", {"work_total"}),
                 ("development.fix_needed", "exhaust_work_total", "resume_development",
                  {"work_total"}),
                 ("planning.agent_running", "exhaust_planning_recovery",
                  "resume_planning", {"planning_attempts"}),
                 ("planning.agent_running", "exhaust_dispatch_recovery",
                  "resume_planning", {"dispatch_recovery"}),
                 ("provisioning.idle", "require_provisioning_intervention",
                  "resume_provisioning", {"operation_launch"}),
                 ("development.agent_running", "exhaust_publish_recovery",
                  "resume_development", {"publish_recovery"}),
                 ("development.waiting_ci", "exhaust_interrupted_reruns", "resume_ci",
                  {"interrupted_reruns"}),
                 ("handoff.waiting_ci", "exhaust_interrupted_reruns", "resume_ci",
                  {"interrupted_reruns"}),
                 ("handoff.building_upstream_branch", "exhaust_upstream_branch_retries",
                  "resume_upstream_branch", {"upstream_branch"}),
                 ("handoff.review_due", "exhaust_work_total", "resume_submission_review",
                  {"work_total"}),
                 ("handoff.agent_running", "exhaust_dispatch_recovery",
                  "resume_submission_review", {"dispatch_recovery"}),
                 ("handoff.agent_running", "exhaust_work_recovery", "resume_submission_review",
                  {"work_recovery", "work_total"}),
                 ("handoff.preparing_submission", "exhaust_comment_retries",
                  "resume_submission_preparation", {"submission_comment"}),
                 ("upstream_review.fix_needed", "exhaust_repair_attempts", "resume_upstream_revision",
                  {"repair_attempts_cluster", "no_progress", "work_total"}))
        for state, verb, resume, counters in cases:
            edge = lc.find_edge(state, verb)
            self.assertEqual(lc.counters_named(edge), counters, (state, verb))
            if len(counters) == 1 and verb != "require_provisioning_intervention":
                self.assertIn(f"{lc.PENDING_COUNTER}={next(iter(counters))}",
                              edge.transition.effects.sets)
            self.assertLessEqual(counters, set(lc.find_edge(edge.target, resume).transition.effects.restarts))
        self.assertIn("starts again the counter named in `pending_human_action.counter`",
                      gen.render_table(POLICY))

    def test_every_exhaustion_into_a_human_wait_names_its_counter(self):
        exhaustions = [e for e in lc.table_edges() if lc.exhausted_budgets(e)
                       and e.target != e.source and lc.LIFECYCLE[e.target].kind == "human_wait"]
        self.assertEqual({b for e in exhaustions for b in lc.exhausted_budgets(e)},
                         set(lc.BUDGETS) - {"flaky_reruns"})
        for e in exhaustions:
            named = lc.counters_named(e)
            self.assertTrue(named, (e.source, e.verb))
            kinds = [s.partition("=")[2] for s in e.transition.effects.sets
                     if s.startswith("pending_human_action=")]
            resumes = [x for x in lc.state_edges(e.target) if x.transition.command == lc.RESUME
                       and all(lc._admits(x.transition, k) for k in kinds)]
            self.assertTrue(resumes, e.target)
            for x in resumes:
                self.assertLessEqual(named, set(x.transition.effects.restarts),
                                     (e.source, e.verb, x.verb))
        restarting = {(e.source, e.verb) for e in lc.table_edges()
                      if e.transition.effects.restarts and not lc.is_rule_row(e)}
        self.assertEqual(restarting, {
            ("planning.waiting_human", "resume_planning"),
            ("provisioning.waiting_human", "resume_provisioning"),
            ("development.waiting_human", "resume_ci"),
            ("development.waiting_human", "resume_development"),
            ("handoff.waiting_human", "resume_submission_review"),
            ("handoff.waiting_human", "resume_upstream_branch"),
            ("handoff.waiting_human", "resume_ci"),
            ("handoff.waiting_human", "resume_submission_preparation"),
            ("upstream_review.waiting_human", "resume_upstream_revision"),
            # The automatic resume of a dispatch the installed queue Policy refused.
            ("planning.waiting_human", "rearm_dispatch"),
            ("development.waiting_human", "rearm_dispatch"),
            ("handoff.waiting_human", "rearm_dispatch"),
            ("upstream_review.waiting_human", "rearm_dispatch"),
        ("upstream_review.waiting_human", "resume_renamed_base"),
        *((wait, "resume_upstream_read") for wait in lc.UPSTREAM_READ_RESUMES),
        *((wait, "resume_renamed_upstream") for wait in lc.UPSTREAM_READ_RESUMES)})
        self.assertEqual({v for v, r in lc.RULES.items() if r.transition.effects.restarts},
                         {"resume_stale"})

    def test_work_total_found_while_work_runs_takes_one_resume(self):
        resumes = {"planning": "resume_planning", "development": "resume_development",
                   "upstream_review": "resume_upstream_revision"}
        for phase, resume in resumes.items():
            edge = lc.find_edge(f"{phase}.agent_running", "exhaust_work_recovery")
            self.assertEqual(edge.target, f"{phase}.waiting_human")
            self.assertEqual(lc.counters_named(edge), {"work_total", "work_recovery"})
            self.assertIn("names `work_total` when that budget is exhausted, and `work_recovery` "
                          "otherwise", edge.transition.description)
            restarts = lc.find_edge(edge.target, resume).transition.effects.restarts
            self.assertIn("work_total", restarts)
            self.assertIn("work_recovery", restarts)

    def test_work_total_found_when_development_work_is_due_waits_for_a_human(self):
        state = "development.fix_needed"
        edge = lc.find_edge(state, "exhaust_work_total")
        self.assertEqual(edge.target, "development.waiting_human")
        self.assertEqual(edge.transition.guard, (lc.WORK_TOTAL_EXHAUSTED,))
        self.assertEqual(edge.transition.effects.sets,
                         ("pending_human_action=work_budget_exhausted",
                          f"{lc.PENDING_COUNTER}=work_total"))
        for text in ("number of Work allocations spent", "`/upstreaming resume`",
                     "`/upstreaming park`"):
            self.assertIn(text, edge.transition.description)
        resume = lc.find_edge(edge.target, "resume_development")
        self.assertTrue(lc._admits(resume.transition, "work_budget_exhausted"))
        self.assertIn("work_total", resume.transition.effects.restarts)
        self.assertEqual(resume.target, state)
        self.assertEqual(lc.find_edge(edge.target, "park_contribution").target, "parked.idle")
        park = lc.find_edge(state, "exhaust_repair_attempts")
        self.assertEqual(park.target, "parked.idle")
        self.assertEqual(lc.exhausted_budgets(park), ["repair_attempts_cluster", "no_progress"])
        self.assertIn(lc.REFRESH_USED, park.transition.guard)
        self.assertEqual([(e.source, e.verb, e.target) for e in lc.exhaustion_edges("work_total")
                          if lc.state_phase(e.source) == "development"],
                         [("development.agent_running", "exhaust_work_recovery",
                           "development.waiting_human"),
                          (state, "exhaust_work_total", "development.waiting_human")])
        self.assertFalse([e for e in lc.exhaustion_edges("work_total")
                          if lc.LIFECYCLE[e.target].kind != "human_wait"])

    def test_transitions_of_a_state_in_which_work_is_due_are_mutually_exclusive(self):
        self.assertEqual([e.verb for e in lc.due_work_edges("development.fix_needed")],
                         ["request_patch", "request_repair", "refresh_on_exhaustion",
                          "exhaust_repair_attempts", "exhaust_work_total"])
        for state in ("planning.idle", "development.fix_needed", "upstream_review.fix_needed"):
            edges = lc.due_work_edges(state)
            self.assertGreater(len(edges), 1, state)
            for a in edges:
                for b in edges:
                    self.assertEqual(lc.excluded(a.transition, b.transition), a is not b,
                                     (state, a.verb, b.verb))
        for verb in ("request_patch", "request_repair", "refresh_on_exhaustion",
                     "exhaust_repair_attempts"):
            self.assertIn(lc.WORK_TOTAL_REMAINS,
                          lc.find_edge("development.fix_needed", verb).transition.guard)

    def test_first_patch_after_a_rejected_proposal(self):
        patch = lc.find_edge("development.fix_needed", "request_patch").transition
        self.assertEqual(patch.charges, ("work_total",))
        self.assertIn(lc.NO_FAILING_JOB, patch.guard)
        self.assertTrue(any("no_progress" in g and "remain" in g for g in patch.guard))
        self.assertNotIn("feedback", " ".join(
            lc.find_edge("development.agent_running", "reject_patch_proposal").transition.effects.sets))

    def test_interrupted_reruns_are_bounded_per_head(self):
        budget = lc.BUDGETS["interrupted_reruns"]
        self.assertEqual(budget.policy_key, lc.INFRASTRUCTURE_KEY)
        self.assertTrue(budget.infrastructure)
        self.assertIn("head", budget.counter)
        for phase in ("development", "handoff"):
            ci, wait = f"{phase}.waiting_ci", f"{phase}.waiting_human"
            self.assertEqual(lc.find_edge(ci, "rerun_interrupted_ci").transition.charges,
                             ("interrupted_reruns",))
            exhausted = lc.find_edge(ci, "exhaust_interrupted_reruns")
            self.assertEqual(exhausted.target, wait)
            self.assertIn("pending_human_action=ci_interrupted", exhausted.transition.effects.sets)
            self.assertIn("pending_human_action.counter=interrupted_reruns",
                          exhausted.transition.effects.sets)
            resume = lc.find_edge(wait, "resume_ci")
            self.assertEqual(resume.target, ci)
            self.assertEqual(resume.transition.effects.restarts, ("interrupted_reruns",))
            self.assertIn("pending_human_action is ci_interrupted", resume.transition.guard)
        self.assertEqual(lc.budget_limit("interrupted_reruns", POLICY), 2)

    def test_admission_override_applies_against_park_only(self):
        state = "planning.agent_running"
        for verb in ("admit_development", "readmit_development"):
            self.assertIn("verified plan decision develop, or verified plan decision park and "
                          "a recorded admission_override", lc.find_edge(state, verb).transition.guard)
        self.assertIn("no admission_override recorded",
                      lc.find_edge(state, "park_planning").transition.guard)
        close = lc.find_edge(state, "close_planning").transition
        self.assertEqual(close.guard, ("verified plan decision close", lc.CLOSE_CONFIRMED))
        self.assertIn("not applied", close.description)
        reject = lc.find_edge(state, "reject_plan").transition
        self.assertEqual(reject.guard[0], lc.PLAN_REJECTED)
        self.assertIn("PyPI does not confirm a decision close", lc.PLAN_REJECTED)

    def test_the_plan_contract_admits_every_decision_the_fold_takes(self):
        # The contract, the planner's instructions, and the fold name
        # the same decisions: a decision the fold takes is never refused by
        # the research gate before it reaches the reconciler.
        contract = json.loads((ROOT / "schemas" / "plan.schema.json").read_text())
        decisions = contract["properties"]["proposal"]["properties"]["decision"]["enum"]
        self.assertEqual(tuple(decisions), reconcile.PLAN_DECISIONS)
        prompt = (ROOT / ".github" / "workflows" / "planner.md").read_text()
        for decision in reconcile.PLAN_DECISIONS:
            self.assertIn(f"`{decision}`", prompt)
        self.assertIn("rejects a\n  `close` that PyPI does not confirm", prompt)

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

    def test_budget_table(self):
        infrastructure = "retries.infrastructure_per_head_failure"
        self.assertEqual({name: b.policy_key for name, b in lc.BUDGETS.items()}, {
            "work_total": "retries.max_attempts_per_contribution",
            "work_recovery": "retries.work_recovery",
            "planning_attempts": "retries.planning_attempts",
            "repair_attempts_cluster": "retries.patch_attempts_per_cluster",
            "no_progress": "retries.no_progress_review_after",
            "flaky_reruns": "retries.flaky_reruns",
            "dispatch_recovery": infrastructure, "publish_recovery": infrastructure,
            "interrupted_reruns": infrastructure,
            "operation_launch": infrastructure, "upstream_branch": infrastructure,
            "submission_comment": infrastructure,
            "source_reads": "retries.source_unavailable_reads",
        })
        self.assertEqual(policy_value("retries.source_unavailable_reads"), 3)
        # The base branch is needed in each state of `BASE_BRANCH_WAITS`; its
        # wait is that of the phase, or of planning for a request in `queued`.
        for state, wait in lc.BASE_BRANCH_WAITS.items():
            edge = lc.find_edge(state, "require_base_branch")
            self.assertEqual(edge.target, wait)
            self.assertEqual(lc.state_phase(wait),
                             "planning" if state == "queued.idle" else lc.state_phase(state))
        self.assertEqual(policy_value("retries.planning_attempts"), 3)
        self.assertGreater(policy_value("retries.max_attempts_per_contribution"), 12)
        for name, b in lc.BUDGETS.items():
            if not b.infrastructure:
                continue
            for x in lc.exhaustion_edges(name):
                self.assertEqual(lc.LIFECYCLE[x.target].kind, "human_wait", (name, x.source))
                self.assertEqual(lc.state_phase(x.target), lc.state_phase(x.source), (name, x.source))
        for e in lc.table_edges():
            if e.verb in ("exhaust_work_recovery", "exhaust_dispatch_recovery", "exhaust_publish_recovery"):
                self.assertEqual(e.target, f"{lc.state_phase(e.source)}.waiting_human")

    def test_one_flag_with_reason_and_setter(self):
        self.assertEqual(set(lc.FLAGS), {"paused"})
        flag = lc.FLAGS["paused"]
        self.assertEqual(set(flag.set_by), {"pause", "pause_on_identity_change",
                                            "pause_on_processing_error"})
        self.assertNotIn("a reason is given", flag.set_by["pause"].guard)
        self.assertIn("paused_reason=upstream_identity_changed",
                      flag.set_by["pause_on_identity_change"].effects.sets)
        self.assertNotIn("/upstreaming block", gen.render_index())
        for t in flag.set_by.values():
            fields = {f.split("=")[0] for f in t.effects.sets}
            self.assertEqual(fields, {"paused", "paused_at", "paused_reason", "paused_by"})
            self.assertEqual(t.effects.work, "clear")
        for t in flag.cleared_by.values():
            self.assertEqual(t.trigger, lc.Trigger.COMMAND)
            self.assertEqual(t.effects.revision, "bump")
        self.assertEqual(set(flag.mask), set(lc.Trigger))
        self.assertEqual(lc.resolve_node("development.agent_running", "pause")[1],
                         "development.fix_needed+paused")
        self.assertEqual(lc.resolve_node("planning.agent_running", "pause")[1],
                         "planning.idle+paused")
        self.assertEqual(lc.resolve_node("upstream_review.agent_running", "pause")[1],
                         "upstream_review.fix_needed+paused")
        self.assertEqual(lc.resolve_node("development.waiting_human+paused", "resume_pause")[1],
                         "development.waiting_human")
        self.assertEqual(lc.resolve_node("development.waiting_ci+paused", "resume_stale")[1], "queued.idle")

    def test_operation_table(self):
        import recovery

        self.assertEqual(set(lc.OPERATIONS), set(recovery.STATUSES))
        self.assertEqual({k for k, s in lc.OPERATIONS.items() if s.open}, set(recovery.OPEN))
        for key, state in lc.OPERATIONS.items():
            targets = {e.target for e in lc.state_edges(key, lc.OPERATIONS)
                       if e.transition.command == lc.RESOLVE}
            self.assertEqual(targets, {"confirmed", "failed", "not_launched"} if state.open else set())

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

    def test_the_exhaustion_of_a_row_is_declared_not_read_from_its_guards(self):
        edge = lc.find_edge("queued.idle", "exhaust_source_reads")
        reworded = dataclasses.replace(edge.transition,
                                       guard=("reads kept failing",) + edge.transition.guard[1:])
        self.assertEqual(lc.exhausted_budgets(edge._replace(transition=reworded)),
                         ["source_reads"])
        undeclared = dataclasses.replace(edge.transition, exhausts=())
        self.assertEqual(lc.exhausted_budgets(edge._replace(transition=undeclared)), [])
        # The definitive answer about a renamed repository is no exhaustion,
        # and its wait names no counter.
        renamed = lc.find_edge("queued.idle", "require_renamed_upstream")
        self.assertEqual(lc.exhausted_budgets(renamed), [])
        self.assertEqual(lc.counters_named(renamed), set())
        self.assertFalse([s for s in renamed.transition.effects.sets
                          if s.startswith(lc.PENDING_COUNTER)])
        self.assertEqual(lc.exhausted_budgets(lc.find_edge("queued.idle",
                                                           "require_upstream_access")),
                         ["source_reads"])

    def test_table_is_a_definition_without_annotations(self):
        source = (ROOT / "lifecycle.py").read_text()
        table = source[source.index("# Lifecycle table"):]
        for pattern in (r"\bplan (?:section|step|\d)", r"\bgap\b", r"\b\w+\.py:\d+", r"\bstatus\b", r"\bimplemented\b"):
            self.assertIsNone(re.search(pattern, table, re.IGNORECASE), pattern)


def _with_state(key: str, **changes) -> dict:
    return {key: dataclasses.replace(lc.LIFECYCLE[key], **changes)}


class TestConsistencyCheckViolations(unittest.TestCase):
    """The check reports each kind of inconsistency with a message naming it."""

    def problems(self, states: dict) -> list[str]:
        with mock.patch.dict(lc.LIFECYCLE, states):
            return lc.check_table(POLICY)

    def with_row(self, state: str, verb: str, target: str | None = None, **changes) -> dict:
        """The table with one row replaced or added (``target``) or changed."""
        transitions = dict(lc.LIFECYCLE[state].transitions)
        if target is not None:
            edge = [e for e in lc.state_edges(state) if e.verb == verb]
            old = edge[0].transition if edge else lc.Transition(lc.Trigger.TICK, None, (lc.ALWAYS,))
            transitions = {k: v for k, v in transitions.items() if k[0] != verb}
            transitions[(verb, target)] = dataclasses.replace(old, **changes)
        else:
            key = next(k for k in transitions if k[0] == verb)
            transitions[key] = dataclasses.replace(transitions[key], **changes)
        return _with_state(state, transitions=transitions)

    def test_a_declared_exhaustion_of_another_budget(self):
        self.assertReports(
            self.with_row("queued.idle", "require_renamed_upstream", exhausts=("work_total",)),
            "queued.idle --require_renamed_upstream--> planning.waiting_human: declares the "
            "exhaustion of 'work_total', which does not name 'require_renamed_upstream'")

    def test_work_allocation_without_work_total(self):
        self.assertReports(
            self.with_row("development.fix_needed", "request_patch", charges=()),
            "development.fix_needed --request_patch--> development.agent_running: allocates Work "
            "without charging 'work_total'")

    def test_resume_without_bump_and_bump_without_reason(self):
        edge = lc.find_edge("handoff.waiting_human", "resume_submission_preparation")
        self.assertReports(
            self.with_row("handoff.waiting_human", "resume_submission_preparation",
                          effects=dataclasses.replace(edge.transition.effects, revision="keep")),
            "resume_submission_preparation--> handoff.preparing_submission: the revision rule "
            "requires a revision bump")
        edge = lc.find_edge("provisioning.idle", "confirm_provisioning")
        self.assertReports(
            self.with_row("provisioning.idle", "confirm_provisioning",
                          effects=dataclasses.replace(edge.transition.effects, revision="bump")),
            "confirm_provisioning--> development.fix_needed: bumps the revision although")

    def test_fourth_exit_from_the_locked_wait(self):
        self.assertReports(
            self.with_row("handoff.waiting_upstream_pr", "record_audit_observation",
                          "development.waiting_ci"),
            "handoff.waiting_upstream_pr: exits", "differ from the 3 allowed exits")

    def test_automatic_park_from_upstream_review(self):
        self.assertReports(
            self.with_row("upstream_review.fix_needed", "exhaust_repair_attempts", "parked.idle",
                          effects=lc.Effects(sets=("parked_reason", "previous_phase"))),
            "ends the generation from upstream_review without a command",
            "exhaustion of 'repair_attempts_cluster' in upstream_review must lead to its human wait")

    def test_infrastructure_exhaustion_that_parks(self):
        self.assertReports(
            self.with_row("development.agent_running", "exhaust_dispatch_recovery", "parked.idle",
                          effects=lc.Effects(work="clear", sets=("parked_reason", "previous_phase"))),
            "exhaustion of infrastructure budget 'dispatch_recovery' must lead to a human wait "
            "of the same phase")

    def test_missing_rule_rows(self):
        transitions = {k: v for k, v in lc.LIFECYCLE["handoff.awaiting_submission"].transitions.items()
                       if k[0] not in ("record_reminder", "record_late_effect")}
        self.assertReports(
            _with_state("handoff.awaiting_submission", transitions=transitions),
            "handoff.awaiting_submission: gate state without record_reminder",
            "rule record_reminder: applies in handoff.awaiting_submission but the table has no such row",
            "rule record_late_effect: applies in handoff.awaiting_submission but the table has no such row")

    def test_resume_without_the_complementary_guard(self):
        edge = lc.find_edge("development.waiting_human", "resume_development")
        guard = tuple(g for g in edge.transition.guard if g != lc.NOT_STALE)
        self.assertReports(
            self.with_row("development.waiting_human", "resume_development", guard=guard),
            "resume_development--> development.fix_needed: a resume beside resume_stale without")
        edge = lc.find_edge("planning.waiting_human", "resume_planning")
        self.assertReports(
            self.with_row("planning.waiting_human", "resume_planning",
                          guard=(*edge.transition.guard, lc.NOT_STALE)),
            "in a state without resume_stale")

    def test_reminder_row_beside_the_rule(self):
        rule = lc.RULES["record_reminder"].transition
        self.assertReports(
            self.with_row("handoff.waiting_upstream_pr", "record_reminder", lc.SELF,
                          description="own row"),
            "handoff.waiting_upstream_pr --record_reminder-->: a reminder row beside the rule")
        self.assertIs(lc.find_edge("handoff.waiting_upstream_pr", "record_reminder").transition, rule)

    def test_resumes_of_a_wait_that_overlap_or_leave_a_gap(self):
        edge = lc.find_edge("handoff.waiting_human", "resume_ci")
        guard = tuple(lc.pending_is("upstream_branch_failed") if g in lc.PENDING_GUARDS else g
                      for g in edge.transition.guard)
        self.assertReports(
            self.with_row("handoff.waiting_human", "resume_ci", guard=guard),
            "handoff.waiting_human: several resumes apply when pending_human_action is "
            "upstream_branch_failed",
            "handoff.waiting_human: not exactly one resume applies when pending_human_action is "
            "ci_interrupted")
        self.assertReports(
            self.with_row("handoff.waiting_human", "resume_ci", guard=(lc.EXACT_REVISION,)),
            "one of 5 resumes of the wait, so exactly one pending-action guard is required")
        self.assertReports(
            self.with_row("handoff.building_upstream_branch", "exhaust_upstream_branch_retries",
                          effects=lc.Effects(sets=("pending_human_action",))),
            "handoff.waiting_human: entered without naming the pending action")
        edge = lc.find_edge("upstream_review.waiting_human", "resume_upstream_review")
        guard = tuple(lc.pending_is("pr_closed_unmerged") if g in lc.PENDING_GUARDS else g
                      for g in edge.transition.guard)
        self.assertReports(
            self.with_row("upstream_review.waiting_human", "resume_upstream_review", guard=guard),
            "upstream_review.waiting_human: not exactly one resume applies when "
            "pending_human_action is answer_reviewer")

    def test_transitions_that_can_apply_together_where_work_is_due(self):
        edge = lc.find_edge("development.fix_needed", "exhaust_repair_attempts")
        guard = tuple(g for g in edge.transition.guard if g != lc.WORK_TOTAL_REMAINS)
        self.assertReports(
            self.with_row("development.fix_needed", "exhaust_repair_attempts", guard=guard),
            "development.fix_needed: exhaust_repair_attempts and exhaust_work_total can apply "
            "together; their guards are not exclusive")
        self.assertReports(
            self.with_row("development.fix_needed", "exhaust_work_total", "parked.idle",
                          effects=lc.Effects(sets=("parked_reason", "previous_phase"))),
            "development.fix_needed --exhaust_work_total--> parked.idle: exhaustion of "
            "work_total where Work is due is exhaust_work_total, guarded by nothing else, and "
            "leads to a human wait")

    def test_exhaustion_without_a_counter_or_a_restart(self):
        self.assertReports(
            self.with_row("planning.idle", "exhaust_work_total",
                          effects=lc.Effects(sets=("pending_human_action=work_budget_exhausted",))),
            "planning.idle --exhaust_work_total--> planning.waiting_human: exhaustion "
            "that leads to a human wait without pending_human_action.counter")
        edge = lc.find_edge("upstream_review.waiting_human", "resume_upstream_revision")
        self.assertReports(
            self.with_row("upstream_review.waiting_human", "resume_upstream_revision",
                          effects=dataclasses.replace(edge.transition.effects,
                                                      restarts=("work_total",))),
            "upstream_review.waiting_human --resume_upstream_revision--> "
            "upstream_review.fix_needed: does not start 'no_progress' again")
        edge = lc.find_edge("handoff.waiting_human", "resume_ci")
        self.assertReports(
            self.with_row("handoff.waiting_human", "resume_ci",
                          effects=dataclasses.replace(edge.transition.effects, restarts=())),
            "handoff.waiting_human --resume_ci--> handoff.waiting_ci: does not start "
            "'interrupted_reruns' again")
        self.assertReports(
            self.with_row("handoff.waiting_human", "resume_ci",
                          effects=dataclasses.replace(edge.transition.effects,
                                                      restarts=("interrupted_reruns", "work_total"))),
            "starts 'work_total' again, but no exhaustion of it names its counter")
        self.assertReports(
            self.with_row("handoff.building_upstream_branch", "exhaust_upstream_branch_retries",
                          effects=lc.Effects(sets=("pending_human_action=upstream_branch_failed",))),
            "exhaustion that leads to a human wait without pending_human_action.counter")
        edge = lc.find_edge("development.agent_running", "exhaust_work_recovery")
        self.assertReports(
            self.with_row("development.agent_running", "exhaust_work_recovery", description=""),
            "pending_human_action.counter names no counter, so the description must state")
        rule = lc.RULES["resume_stale"]
        stale = dataclasses.replace(rule, transition=dataclasses.replace(
            rule.transition, effects=dataclasses.replace(rule.transition.effects, restarts=())))
        with mock.patch.dict(lc.RULES, {"resume_stale": stale}):
            self.assertTrue(any(p.startswith("rule resume_stale: does not start")
                                for p in lc.check_table(POLICY)))

    def test_resume_stale_without_quiescence(self):
        rule = lc.RULES["resume_stale"]
        guard = tuple(g for g in rule.transition.guard if g != lc.QUIESCENT)
        broken = dataclasses.replace(rule, transition=dataclasses.replace(rule.transition, guard=guard))
        with mock.patch.dict(lc.RULES, {"resume_stale": broken}):
            problems = lc.check_table(POLICY)
        self.assertIn(f"rule resume_stale: without the guard {lc.QUIESCENT!r}", problems)

    def test_budget_table_violations(self):
        policy = json.loads(json.dumps(POLICY))
        del policy["retries"]["work_recovery"]
        del policy["refresh"]
        problems = lc.check_table(policy)
        self.assertIn("budget work_recovery: policy.json has no integer retries.work_recovery", problems)
        self.assertIn("policy.json has no integer refresh.stale_after_days", problems)
        budget = dataclasses.replace(lc.BUDGETS["upstream_branch"], infrastructure=False)
        with mock.patch.dict(lc.BUDGETS, {"upstream_branch": budget}):
            self.assertTrue(any("is not marked infrastructure" in p for p in lc.check_table(POLICY)))

    def test_flag_violations(self):
        flag = lc.FLAGS["paused"]
        clear = dataclasses.replace(flag.cleared_by["resume_pause"],
                                    effects=lc.Effects(clears=("paused",)))
        broken = dataclasses.replace(flag, cleared_by={"resume_pause": clear}, set_moves={})
        with mock.patch.dict(lc.FLAGS, {"paused": broken}):
            problems = lc.check_table(POLICY)
        self.assertTrue(any("flag paused resume_pause: a resume must bump the revision" in p
                            for p in problems), problems)
        self.assertTrue(any("flag paused: set_moves" in p for p in problems), problems)
        stop = flag.set_by["pause_on_identity_change"]
        sets = tuple(s.split("=")[0] if s.startswith("paused_reason") else s for s in stop.effects.sets)
        broken = dataclasses.replace(flag, set_by={
            **flag.set_by,
            "pause_on_identity_change": dataclasses.replace(
                stop, effects=dataclasses.replace(stop.effects, sets=sets))})
        with mock.patch.dict(lc.FLAGS, {"paused": broken}):
            problems = lc.check_table(POLICY)
        self.assertIn("flag paused pause_on_identity_change: a stop set by the controller must "
                      "record its reason", problems)

    def test_processing_error_stop_is_checked(self):
        flag = lc.FLAGS["paused"]
        stop = flag.set_by["pause_on_processing_error"]
        self.assertEqual((stop.trigger, stop.command), (lc.Trigger.TICK, None))
        self.assertEqual(stop.guard, (lc.NOT_PAUSED, "not terminal", lc.PROCESSING_ERROR))
        self.assertEqual(stop.effects, lc.Effects(
            work="clear", sets=("paused", "paused_at", "paused_reason", "paused_by=controller"),
            clears=(lc.HELD_RESULT,)))
        self.assertEqual(lc.CALLER_REASONS, ("pause_on_processing_error",))

        def problems_with(**changes):
            broken = dataclasses.replace(flag, set_by={
                **flag.set_by, "pause_on_processing_error": dataclasses.replace(stop, **changes)})
            with mock.patch.dict(lc.FLAGS, {"paused": broken}):
                return lc.check_table(POLICY)

        where = "flag paused pause_on_processing_error"
        by_human = dataclasses.replace(stop.effects, sets=(
            "paused", "paused_at", "paused_reason", "paused_by=human"))
        self.assertIn(f"{where}: does not set paused_by=controller",
                      problems_with(effects=by_human))
        named = dataclasses.replace(stop.effects, sets=(
            "paused", "paused_at", "paused_reason=error", "paused_by=controller"))
        self.assertTrue(any(p.startswith(f"{where}: a stop whose reason the caller states")
                            for p in problems_with(effects=named)))
        kept = dataclasses.replace(stop.effects, clears=())
        self.assertTrue(any(p.startswith(f"{where}: a stop that clears the requested Work")
                            for p in problems_with(effects=kept)))
        bumped = dataclasses.replace(stop.effects, revision="bump")
        self.assertIn(f"{where}: a stop must not bump the revision",
                      problems_with(effects=bumped))
        self.assertIn(f"{where}: sets the flag without the guard {lc.NOT_PAUSED!r}",
                      problems_with(guard=("not terminal", lc.PROCESSING_ERROR)))
        removed = dataclasses.replace(flag, set_by={
            v: t for v, t in flag.set_by.items() if v != "pause_on_processing_error"})
        with mock.patch.dict(lc.FLAGS, {"paused": removed}):
            self.assertIn("flag paused: pause_on_processing_error is not a set verb",
                          lc.check_table(POLICY))

    def test_operation_table_violations(self):
        broken = dataclasses.replace(lc.OPERATIONS["uncertain"], transitions={})
        with mock.patch.dict(lc.OPERATIONS, {"uncertain": broken}):
            problems = lc.check_table(POLICY)
        self.assertIn("operation uncertain: not an end state but no transition leaves it", problems)
        self.assertIn("operation uncertain: no end state is reachable from this state", problems)

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
        self.assertEqual(self.problems({}), [])

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
        transitions[("begin_planning", "planning.idle")] = lc.Transition(lc.Trigger.TICK)
        transitions[("withdraw_request", "closed.idle")] = lc.Transition(
            lc.Trigger.COMMAND, None, ("any revision, current or stale",))
        self.assertReports(
            _with_state("queued.idle", transitions=transitions),
            "queued.idle --begin_planning--> planning.idle: empty guard",
            "queued.idle --withdraw_request--> closed.idle: command trigger without")

    def test_unknown_budget_and_missing_revision_bump(self):
        transitions = dict(lc.LIFECYCLE["development.waiting_ci"].transitions)
        transitions[("record_actionable_review", "development.fix_needed")] = lc.Transition(
            lc.Trigger.OBSERVATION, None, ("authorized actionable review feedback",),
            lc.Effects(sets=("pending_feedback_evidence_ids",)), ("review_rounds",))
        self.assertReports(
            _with_state("development.waiting_ci", transitions=transitions),
            "charges unknown budget 'review_rounds'",
            "record_actionable_review--> development.fix_needed: the revision rule requires a "
            "revision bump")


class TestDiagrams(unittest.TestCase):
    def test_diagrams_draw_only_table_entries_and_cover_the_table(self):
        drawn_edges, drawn_states, drawn_verbs, rules = set(), set(), set(), []
        for section in lc.DIAGRAM_SECTIONS:
            for d in section.diagrams:
                for node in d.nodes:
                    self.assertIn(node.partition("+")[0], lc.LIFECYCLE, d.title)
                drawn_states.update(n.partition("+")[0] for n in d.nodes)
                for node in (d.start, *d.ends):
                    if node:
                        self.assertIn(node, d.nodes, d.title)
                if d.rule:
                    rules.append(d.rule)
                    self.assertEqual(d.edges, (), d.title)
                for source, verb in d.edges:
                    _t, target = lc.resolve_node(source, verb)
                    self.assertIn(target, d.nodes, (d.title, source, verb))
                    drawn_edges.add((source, verb, target))
                    drawn_verbs.add(verb)
        self.assertEqual(drawn_states, set(lc.LIFECYCLE))
        own_rows = {(e.source, e.verb, e.target) for e in lc.table_edges() if not lc.is_rule_row(e)}
        self.assertLessEqual(own_rows, drawn_edges)
        self.assertEqual(sorted(rules), sorted(lc.RULES))
        for flag in lc.FLAGS.values():
            self.assertLessEqual({*flag.set_by, *flag.cleared_by}, drawn_verbs)

    def test_rule_rows_do_not_flood_the_diagrams(self):
        index = gen.render_index()
        self.assertEqual(index.count("|record_late_effect|"), 2)
        self.assertLess(index.count("|park_contribution<br/>"), 8)
        self.assertIn('any["every state"]', index)

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

    def test_decisions_are_settled_and_open_items_are_listed(self):
        self.assertEqual(len(DECISIONS), 24)
        for d in (*DECISIONS, *FURTHER_CHANGES):
            self.assertTrue(d.text and not d.options and not d.candidates, d.title)
            self.assertTrue(d.operations or d.highlighted, d.title)
        self.assertEqual(len(SETTLED_OPEN_ITEMS), 30)
        self.assertEqual(OPEN_ITEMS, ())
        for d in SETTLED_OPEN_ITEMS:
            self.assertTrue(d.text and d.highlighted and not d.options and not d.candidates, d.title)
        for d in OPEN_ITEMS:
            self.assertGreaterEqual(len(d.options), 2, d.title)
        page = gen.render_decisions()
        for number, d in enumerate(DECISIONS, 1):
            self.assertIn(f"### {number}. {d.title}", page)
        for number, d in enumerate(SETTLED_OPEN_ITEMS, 1):
            self.assertIn(f"### O{number}. {d.title}", page)
        for number, d in enumerate(OPEN_ITEMS, len(SETTLED_OPEN_ITEMS) + 1):
            self.assertIn(f"### O{number}. {d.title}", page)
        open_section = page.split("## Open items", 1)[1].split("## Settled decisions", 1)[0]
        self.assertEqual("No item is open." in open_section, not OPEN_ITEMS)
        with mock.patch.object(gen, "OPEN_ITEMS", ()):
            self.assertIn("No item is open.", gen.render_decisions())

    def test_decision_slices_use_table_entries(self):
        for d in (*DECISIONS, *FURTHER_CHANGES, *SETTLED_OPEN_ITEMS, *OPEN_ITEMS):
            states = {n.partition("+")[0] for n in d.nodes}
            self.assertLessEqual(len(states), 6, d.title)
            for source, verb in d.edges:
                self.assertIn(source, d.nodes, d.title)
                _t, target = lc.resolve_node(source, verb)
                self.assertIn(target, d.nodes, (d.title, source, verb))
            for edge in d.highlighted:
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
