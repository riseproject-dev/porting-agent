# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The transition engine applies the lifecycle table, and nothing else writes
lifecycle state."""

import ast
import copy
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import commands
import engine
import lifecycle as lc
import schemas
from tests.helpers import (
    NOW,
    OWNER,
    SHA_A,
    SHA_B,
    SHA_C,
    awaiting_submission,
    ci_record,
    contribution,
    policy,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY = policy()
LONG_AGO = (datetime.fromisoformat(NOW) - timedelta(days=40)).isoformat()

# A value for every field a row sets without stating its value.
SAMPLE = {
    "fork": {"repository_id": 2, "name": "riseproject-upstreaming/proj", "head_ref": "riscv64-dev",
             "head_sha": SHA_A, "pr_number": None},
    "fork.head_sha": SHA_B,
    "upstream.repository_id": 1, "upstream.base_ref": "main", "upstream.base_sha": SHA_C,
    "upstream_pr.base_ref": "main",
    "upstream_pr": {"id": 9, "number": 9, "url": "https://github.com/example/proj/pull/9",
                    "head_repository_id": 2, "head_ref": "riscv64", "head_sha": SHA_A,
                    "base_ref": "main", "registered_by": OWNER},
    "upstream_pr.head_sha": SHA_B,
    "plan_evidence_id": "plan-1", "admission_class": "configuration_integration",
    "scope_paths": ["pyproject.toml"], "parked_reason": "no bounded patch",
    "terminal_reason": "not applicable", "last_ci_evidence_id": "ci-2-8-1",
    "clean_head_sha": SHA_B, "submission_review_id": "submission-review-1",
    "submission_rewrite_id": "submission-rewrite-1",
    "handoff_request": {"command_id": "observation-ci-2-8-1", "head": SHA_A, "revision": 0},
    "handoff_approval": {"command_id": "comment-1", "actor": OWNER, "head": SHA_A, "revision": 0,
                         "evidence_ids": ["ci-2-7-1"]},
    "active_operation_id": "op-handoff-1", "publication_operation_id": "op-publish-1",
    "proposal_artifact": {"work_id": "w", "claim_id": "c", "artifact_id": 1, "name": "n",
                          "digest": "sha256:" + "0" * 64, "run_id": "1"},
    "pending_publication": {"worker_run_id": "1", "attempts": 1, "last_reason": "push failed",
                            "next_attempt_at": NOW, "exhausted": False},
    "pending_feedback_evidence_ids": ["review-2-1-5"],
    "processed_review_ids": [5], "processed_review_comment_ids": [6],
    "processed_pr_conversation_comment_ids": [7],
    "merge": {"merge_sha": SHA_C, "merged_at": NOW, "target_ref": "main", "evidence_id": "merge-1"},
    "release_acceptance": {"evidence_id": "release-1", "as_of": NOW, "complete": True},
    "refresh": {"command_id": "comment-1", "reason": "r", "from_phase": "development",
                "head_sha": SHA_A, "pr_number": 1},
    "pending_human_action": "operator_action",
    "paused_reason": "processing_error: KeyError: 'x' (step observation, run 1)",
    "admission_override": {"class": "configuration_integration", "paths": ["a"], "reason": "r"},
    "active_work": {"dedupe_key": "example--proj/12@0:planning", "task": "planning",
                    "revision": 0, "attempt": 1, "requested_at": NOW, "deadline_at": NOW, "generation": 0, "dispatched_run_id": None, "reason_ids": [], "feedback_evidence_ids": []},
}


def all_rows():
    """Every row of the table: its source state, verb, transition and target."""
    rows = [(e.source, e.verb, e.transition, e.target) for e in lc.table_edges()]
    for flag in lc.FLAGS.values():
        for state, entry in lc.LIFECYCLE.items():
            if entry.kind == "terminal":
                continue
            for verb, t in flag.set_by.items():
                rows.append((state, verb, t, flag.set_moves.get(state, state)))
            for verb, t in flag.cleared_by.items():
                rows.append((state, verb, t, state))
    return rows


def scenario(state, verb, t):
    """A record in ``state`` and the arguments with which every guard of the
    row holds, derived from the row alone."""
    phase, activity = state.split(".")
    rec = contribution(phase=phase, activity=activity)
    kind = lc.LIFECYCLE[state].kind
    if phase == "upstream_review":
        rec["upstream_pr"] = copy.deepcopy(SAMPLE["upstream_pr"])
    if kind in ("gate", "locked", "human_wait"):
        named = sorted(lc.pending_kinds(state) - {""})
        rec["pending_human_action"] = {"kind": named[0] if named else "operator_action",
                                       "requested_at": NOW, "reminded_at": None,
                                       "escalated_at": None, "counter": None}
    if state == "handoff.awaiting_submission":
        rec["handoff_request"] = dict(SAMPLE["handoff_request"])
    facts, counts, command = set(), {}, None
    if t.trigger == lc.Trigger.COMMAND:
        command = commands.Command(t.command.split()[-1],
                                   {"revision": str(rec["revision"]), "reason": "r", "head": SHA_A})
    for guard in t.guard:
        predicate = engine.PREDICATES[guard]
        if predicate.kind == "fact":
            facts.add(predicate.name)
        if guard in lc.PENDING_GUARDS:
            negated, kinds = lc.PENDING_GUARDS[guard]
            rec["pending_human_action"]["kind"] = "work_failed" if negated else kinds[0]
        for name in lc.BUDGETS:
            if guard == f"{name} remains":
                counts.setdefault(name, 0)
            elif guard == f"{name} exhausted":
                counts[name] = lc.budget_limit(name, POLICY)
    guards = set(t.guard)
    if "paused" in guards or "paused, or waiting for a human" in guards:
        rec.update(paused=True, paused_at=NOW, paused_by="human")
    if lc.STALE in guards:
        rec["paused_at"] = LONG_AGO
    if any(g.startswith("at least `human_sla") for g in guards):
        rec["pending_human_action"]["requested_at"] = LONG_AGO
    if lc.FAILING_JOB in guards:
        rec["last_ci_evidence_id"] = "ci-2-7-1"
    if lc.REFRESH_USED in guards:
        rec["automatic_refresh_used"] = True
    if lc.PENDING_FEEDBACK in guards:
        rec["pending_feedback_evidence_ids"] = ["review-1"]
    if "exact clean head" in guards:
        rec["clean_head_sha"] = SHA_A
    if "no fork" in guards:
        rec["fork"] = None
    if "refresh recorded" in guards:
        rec["refresh"] = dict(SAMPLE["refresh"])
    if "work_recovery and work_total remain" in guards:
        counts.update(work_recovery=1, work_total=0)
    if "work_recovery or work_total exhausted" in guards:
        counts["work_recovery"] = lc.budget_limit("work_recovery", POLICY)
    if guards & {lc.REPAIR_BUDGETS_REMAIN, lc.REVISION_BUDGETS_REMAIN,
                 lc.REVIEW_REVISION_BUDGETS_REMAIN}:
        counts.update(repair_attempts_cluster=0, no_progress=0, work_total=0)
    if lc.REPAIR_BUDGET_EXHAUSTED in guards:
        counts["no_progress"] = lc.budget_limit("no_progress", POLICY)
    if lc.REVISION_BUDGET_EXHAUSTED in guards:
        counts.update(repair_attempts_cluster=0, no_progress=0,
                      work_total=lc.budget_limit("work_total", POLICY))
    if verb == "require_provisioning_intervention":
        facts.add("provisioning_blocked")
    if verb in ("exhaust_source_reads", "require_upstream_access"):
        # Exhausted: the count is reached and the minimum time has passed.
        facts.add("base_branch_unread")
        if verb == "require_upstream_access":
            facts.add("upstream_repository_unread")
        rec["budget"] = dict(rec["budget"],
                             source_unavailable=lc.budget_limit("source_reads", POLICY),
                             source_unavailable_since=LONG_AGO)
    if verb == "reject_patch_proposal" and phase == "development":
        facts.add("proposal_rejected_or_blocked")
    e = t.effects
    values = {}
    for entry in e.sets:
        name, literal, _value = entry.partition("=")
        if not literal and name in SAMPLE and not engine._derived(name, None)[0] \
                and not (name in engine.REASON_VALUES and command):
            values[name] = copy.deepcopy(SAMPLE[name])
    if e.work in ("allocate", "reallocate"):
        values["active_work"] = copy.deepcopy(SAMPLE["active_work"])
    if "pending_publication.exhausted" in e.sets or "publish_recovery" in t.charges:
        rec["pending_publication"] = copy.deepcopy(SAMPLE["pending_publication"])
    if "work_recovery" in t.charges:
        rec["active_work"] = copy.deepcopy(SAMPLE["active_work"])
    schemas.validate("contribution", rec)
    kwargs = dict(policy=POLICY, now=NOW, command=command, facts=facts, counts=counts,
                  values=values, cause_id="cause-1",
                  ci_evidence=failing_evidence(rec) if lc.FAILING_JOB in guards else None)
    return rec, kwargs


def failing_evidence(rec, evidence_id="ci-2-7-1"):
    """The evidence ``last_ci_evidence_id`` names, with a failing job on the head."""
    return ci_record(evidence_id, engine.evidence_head(rec), failing="build riscv64")


def get(record, name):
    node = record
    for part in name.split("."):
        node = node[part]
    return node


class TestTableIsExercised(unittest.TestCase):
    """Every row of the table is taken through the engine."""

    def test_every_guard_has_one_named_predicate(self):
        self.assertEqual(engine.table_guards() - set(engine.PREDICATES), set())
        names = [p.name for p in engine.PREDICATES.values()]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({p.kind for p in engine.PREDICATES.values()}, {"record", "fact"})
        unused = set(engine.PREDICATES) - engine.table_guards()
        # Only the single-budget guards that no row states alone are unused.
        self.assertTrue(all(g == lc.ALWAYS or g.endswith((" remains", " exhausted"))
                            for g in unused), unused)

    def test_every_unwired_verb_waits_on_a_recorded_open_decision(self):
        # No verb is left unwired without a decision that blocks it, named
        # on the known-gaps page.
        self.assertEqual(engine.UNWIRED_VERBS, frozenset(engine.OPEN_DECISIONS))
        gaps = (ROOT / "docs/state-machine/known-gaps.md").read_text()
        for verb, entry in engine.OPEN_DECISIONS.items():
            with self.subTest(verb=verb):
                self.assertIn(f"\n## {entry}\n", gaps)
                section = gaps.split(f"\n## {entry}\n", 1)[1].split("\n## ", 1)[0]
                self.assertIn(f"`{verb}`", section)

    def test_pipeline_and_unwired_verbs_partition_the_table(self):
        verbs = engine.table_verbs()
        self.assertEqual(set(engine.PIPELINE_VERBS) | engine.UNWIRED_VERBS, verbs)
        self.assertEqual(set(engine.PIPELINE_VERBS) & engine.UNWIRED_VERBS, set())
        for verb, module in engine.PIPELINE_VERBS.items():
            source = (ROOT / f"{module}.py").read_text()
            with self.subTest(verb=verb):
                self.assertIn("engine.", source)
                command = any(t.command for _s, v, t, _t in all_rows() if v == verb)
                self.assertTrue(f'"{verb}"' in source or (command and "resolve_command" in source),
                                f"{module} does not take {verb}")

    def test_coverage_report_counts_the_table(self):
        report = engine.coverage()
        self.assertEqual(report["table_rows"], sum(1 for _ in lc.table_edges()))
        self.assertEqual(len(report["pipeline_verbs"]) + len(report["unwired_verbs"]),
                         report["table_verbs"])
        self.assertEqual(sum(report["guards"].values()), len(engine.table_guards()))
        self.assertNotIn("legacy_write_sites", report)

    def test_every_row_is_taken_with_its_effects(self):
        rows = all_rows()
        self.assertGreaterEqual(len(rows), 158)
        for state, verb, t, target in rows:
            with self.subTest(state=state, verb=verb):
                rec, kwargs = scenario(state, verb, t)
                before = copy.deepcopy(rec)
                out = engine.apply_transition(rec, verb, **kwargs)
                self.assertTrue(out.accepted, out.reason)
                self.assertEqual(rec, before, "the input record is never changed")
                new, e = out.record, t.effects
                self.assertEqual(engine.state_key(new), target)
                self.assertEqual(new["revision"], rec["revision"] + (e.revision == "bump"))
                self.assertEqual(new["generation"], rec["generation"] + (e.generation == "bump"))
                if e.work == "clear":
                    self.assertIsNone(new["active_work"])
                elif e.work in ("allocate", "reallocate"):
                    self.assertEqual(new["active_work"]["dedupe_key"],
                                     SAMPLE["active_work"]["dedupe_key"])
                else:
                    self.assertEqual(new["active_work"], rec["active_work"])
                sets = {lc._field_name(s) for s in e.sets}
                for name in e.clears:
                    if name in lc.QUALIFIED_CLEARS:
                        continue  # a part of a field, tested in TestTableDecisions
                    if name not in sets:
                        self.assertFalse(get(new, name), f"{name} is not cleared")
                if lc.WORK_FEEDBACK in e.sets:
                    self.assertEqual(new["active_work"]["feedback_evidence_ids"],
                                     rec.get("pending_feedback_evidence_ids") or [])
                if lc.RERUNS in e.sets:
                    self.assertEqual(new["budget"]["reruns"], engine.fresh_reruns(new))
                for entry in e.sets:
                    name, literal, value = entry.partition("=")
                    if name == "pending_human_action":
                        self.assertEqual(new[name]["kind"], value or kwargs["values"][name])
                        self.assertEqual(new[name]["requested_at"], NOW)
                    elif literal:
                        self.assertEqual(get(new, name), value)
                    elif name == "pending_publication" and "publish_recovery" in t.charges:
                        # The charge counts the retry in the value it was given.
                        self.assertEqual(new[name], dict(kwargs["values"][name], attempts=2))
                    elif name in kwargs["values"]:
                        self.assertEqual(get(new, name), kwargs["values"][name])
                self.assertEqual(out.event["kind"], verb)
                self.assertEqual(out.event["revision_bumped"], e.revision == "bump")
                self.assertEqual(out.event["details"]["from_state"], state)
                self.assertEqual(out.event["details"]["to_state"], target)
                schemas.validate("event", dict(out.event))
                self.assertIsNone(engine.state_problem(new) if new["phase"] != "handoff"
                                  else engine.state_problem(new))

    def test_no_row_is_taken_without_each_of_its_facts(self):
        for state, verb, t, _target in all_rows():
            rec, kwargs = scenario(state, verb, t)
            for fact in sorted(kwargs["facts"]):
                with self.subTest(state=state, verb=verb, missing=fact):
                    if verb == "resume_upstream_review" and fact == "upstream_pr_reopened":
                        continue  # required for pr_closed_unmerged only, tested below
                    fewer = dict(kwargs, facts=kwargs["facts"] - {fact})
                    if verb == "require_provisioning_intervention":
                        fewer["counts"] = {"operation_launch": 0}
                    if verb == "reject_patch_proposal":
                        fewer["counts"] = dict(kwargs["counts"], flaky_reruns=0)
                    out = engine.apply_transition(rec, verb, **fewer)
                    self.assertFalse(out.accepted)
                    self.assertIs(out.record, rec)
                    self.assertIsNone(out.event)

    def test_exact_revision_rows_refuse_another_revision(self):
        for state, verb, t, _target in all_rows():
            if lc.EXACT_REVISION not in t.guard:
                continue
            rec, kwargs = scenario(state, verb, t)
            rec["revision"] = 3
            for revision, reason in ((2, "stale revision 2, current is 3"),
                                     (4, "revision is not a known revision")):
                with self.subTest(state=state, verb=verb, revision=revision):
                    args = dict(kwargs["command"].args, revision=str(revision))
                    out = engine.apply_transition(
                        rec, verb, **dict(kwargs, command=commands.Command(kwargs["command"].verb, args)))
                    self.assertEqual((out.accepted, out.reason), (False, reason))

    def test_the_paused_flag_masks_ticks_results_and_other_commands(self):
        for state, verb, t, _target in all_rows():
            rec, kwargs = scenario(state, verb, t)
            if rec["paused"]:
                continue
            paused = dict(rec, paused=True, paused_at=NOW, paused_by="human")
            masked = (t.trigger == lc.Trigger.TICK
                      or (t.trigger == lc.Trigger.RESULT and verb != "record_late_effect")
                      or (t.trigger == lc.Trigger.COMMAND and t.command not in engine.PAUSED_COMMANDS)
                      or lc.NOT_PAUSED in t.guard)
            with self.subTest(state=state, verb=verb):
                out = engine.apply_transition(paused, verb, **kwargs)
                self.assertEqual(out.accepted, not masked, out.reason)


class TestProcessingErrorStop(unittest.TestCase):
    """``pause_on_processing_error``: the controller's stop after an
    unexpected error while it processed one contribution."""

    REASON = "processing_error: KeyError: 'x' (step observation, run 7)"

    def take(self, rec, **kwargs):
        kwargs.setdefault("values", {"paused_reason": self.REASON})
        kwargs.setdefault("facts", {"processing_error"})
        return engine.apply_transition(rec, "pause_on_processing_error", policy=POLICY, now=NOW,
                                       cause_id="7", **kwargs)

    def test_sets_the_flag_with_reason_setter_and_time_in_the_same_state(self):
        rec = contribution(phase="development", activity="waiting_ci")
        out = self.take(rec)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.record, dict(rec, paused=True, paused_at=NOW, paused_by="controller",
                                          paused_reason=self.REASON, updated_at=NOW))
        self.assertEqual((out.event["kind"], out.event["cause"], out.event["revision_bumped"]),
                         ("pause_on_processing_error", {"type": "tick", "id": "7"}, False))

    def test_follows_the_pause_semantics(self):
        rec = contribution(phase="development", activity="agent_running",
                           active_work=dict(SAMPLE["active_work"]),
                           pending_publication=dict(SAMPLE["pending_publication"]))
        paused = self.take(rec).record
        # Work cleared: the state in which it is due again; nothing held is kept.
        self.assertEqual(engine.state_key(paused), "development.fix_needed")
        self.assertEqual((paused["active_work"], paused["pending_publication"],
                          paused["revision"]), (None, None, rec["revision"]))
        resumed = engine.apply_command(
            paused, commands.parse("/upstreaming resume revision=0 reason=fixed"), policy=POLICY,
            now=NOW, command_id="c")
        self.assertEqual((resumed.verb, resumed.record["revision"], resumed.record["paused"],
                          resumed.record["paused_reason"], resumed.record["paused_by"]),
                         ("resume_pause", 1, False, None, None))

    def test_refused_while_stopped_or_terminal_or_without_the_fact(self):
        rec = contribution(phase="development", activity="waiting_ci")
        for label, record, why in (
                ("paused", dict(rec, paused=True, paused_by="human"), "contribution is paused"),
                ("terminal", contribution(phase="closed", activity="idle", terminal_reason="x"),
                 "contribution is terminal (closed.idle)")):
            with self.subTest(record=label):
                out = self.take(record)
                self.assertEqual((out.accepted, out.reason, out.record), (False, why, record))
        self.assertFalse(self.take(rec, facts=()).accepted)

    def test_the_caller_states_the_reason(self):
        rec = contribution(phase="development", activity="waiting_ci")
        for values in ({}, {"paused_reason": None}, {"paused_reason": "  "}):
            with self.subTest(values=values), self.assertRaises(engine.TransitionError):
                self.take(rec, values=values)


class TestEnginePage(unittest.TestCase):
    def test_page_names_what_the_pipeline_takes_holds_and_retired(self):
        page = (ROOT / "docs/state-machine/engine.md").read_text()
        for verb in engine.PIPELINE_VERBS:
            self.assertIn(f"`{verb}`", page)
        names = (*engine.HELD_COMMANDS, *commands.RETIRED,
                 *(command.split()[-1] for command in engine.PAUSED_COMMANDS))
        for name in names:
            self.assertIn(f"`{name}`", page)
        self.assertNotIn(chr(0x2014), page)


class TestTableDecisions(unittest.TestCase):
    """Pending feedback, the review rows of the gate, rerun counts per head,
    and the budgets a Work request charges."""

    FACTS = {"worker_slot_free", "capacity_available"}

    def fix_needed(self, **fields):
        return contribution(phase="development", activity="fix_needed", **fields)

    def request(self, rec, verb, **kwargs):
        return engine.apply_transition(rec, verb, policy=POLICY, now=NOW, facts=self.FACTS,
                                       values={"active_work": dict(SAMPLE["active_work"])},
                                       **kwargs)

    def test_work_carries_the_feedback_present_at_allocation(self):
        rec = self.fix_needed(pending_feedback_evidence_ids=["review-1", "review-2"])
        out = self.request(rec, "request_patch")
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.record["active_work"]["feedback_evidence_ids"],
                         ["review-1", "review-2"])
        self.assertEqual(out.record["pending_feedback_evidence_ids"], ["review-1", "review-2"])
        self.assertEqual(out.event["details"]["charged"], ["work_total"])

    def test_publish_success_clears_exactly_the_carried_feedback(self):
        rec = self.fix_needed(pending_feedback_evidence_ids=["review-1"])
        running = self.request(rec, "request_patch").record
        # Feedback recorded while the Work runs is not answered by its Result.
        running = dict(running, pending_feedback_evidence_ids=["review-1", "review-9"])
        published = engine.apply_transition(
            running, "record_publish_success", policy=POLICY, now=NOW,
            facts={"result_published"},
            values={"proposal_artifact": dict(SAMPLE["proposal_artifact"]),
                    "publication_operation_id": "op-publish-1"})
        self.assertTrue(published.accepted, published.reason)
        self.assertEqual(published.record["pending_feedback_evidence_ids"], ["review-9"])
        self.assertIsNone(published.record["active_work"])
        # In upstream review the published revision does the same.
        upstream = contribution(phase="upstream_review", activity="agent_running",
                                upstream_pr=copy.deepcopy(SAMPLE["upstream_pr"]),
                                pending_feedback_evidence_ids=["review-1", "review-9"],
                                active_work=dict(SAMPLE["active_work"],
                                                 feedback_evidence_ids=["review-1"]))
        revised = engine.apply_transition(
            upstream, "record_revision_publish_success", policy=POLICY, now=NOW,
            facts={"revision_published"},
            values={"proposal_artifact": dict(SAMPLE["proposal_artifact"]),
                    "publication_operation_id": "op-publish-1",
                    "upstream_pr.head_sha": SHA_C})
        self.assertTrue(revised.accepted, revised.reason)
        # It records the published head at a new revision.
        self.assertEqual((revised.record["upstream_pr"]["head_sha"], revised.record["revision"]),
                         (SHA_C, upstream["revision"] + 1))
        self.assertEqual(revised.record["pending_feedback_evidence_ids"], ["review-9"])

    def test_a_publish_retry_and_a_fresh_work_graph_carry_the_same_feedback(self):
        running = self.request(self.fix_needed(pending_feedback_evidence_ids=["review-1"]),
                               "request_patch").record
        retry = engine.apply_transition(
            running, "schedule_publish_retry", policy=POLICY, now=NOW,
            facts={"publication_infrastructure_error"},
            values={"pending_publication": dict(SAMPLE["pending_publication"], attempts=0)})
        self.assertTrue(retry.accepted, retry.reason)
        self.assertEqual(retry.record["pending_publication"]["feedback_evidence_ids"], ["review-1"])
        published = engine.apply_transition(
            retry.record, "record_publish_success", policy=POLICY, now=NOW,
            facts={"result_published"},
            values={"proposal_artifact": dict(SAMPLE["proposal_artifact"]),
                    "publication_operation_id": "op-publish-1"})
        self.assertEqual(published.record["pending_feedback_evidence_ids"], [])
        again = engine.apply_transition(
            running, "allocate_work_retry", policy=POLICY, now=NOW,
            facts={"work_terminal_without_result"},
            values={"active_work": dict(SAMPLE["active_work"], attempt=1)})
        self.assertTrue(again.accepted, again.reason)
        self.assertEqual(again.record["active_work"]["feedback_evidence_ids"], ["review-1"])

    def test_rejection_replan_resume_and_refresh_keep_the_feedback(self):
        rec = self.fix_needed(pending_feedback_evidence_ids=["review-1"])
        running = self.request(rec, "request_patch").record
        for verb, facts in (("reject_patch_proposal", {"proposal_rejected_or_blocked"}),
                            ("replan_development", {"proposal_stale"})):
            out = engine.apply_transition(running, verb, policy=POLICY, now=NOW, facts=facts)
            self.assertTrue(out.accepted, out.reason)
            self.assertEqual(out.record["pending_feedback_evidence_ids"], ["review-1"])
        for e in lc.table_edges():
            t = e.transition
            if t.command in (lc.RESUME, lc.REFRESH) and e.verb not in lc.FEEDBACK_ANSWERED_BY:
                self.assertNotIn("pending_feedback_evidence_ids", t.effects.clears, e.verb)
                self.assertNotIn(lc.CARRIED_FEEDBACK, t.effects.clears, e.verb)
        self.assertEqual(lc.check_table(POLICY), [])

    def test_work_that_carries_feedback_takes_no_ci_rerun(self):
        fact = {"failure_not_caused_by_patch"}
        plain = self.request(self.fix_needed(), "request_patch").record
        rerun = engine.apply_transition(plain, "request_ci_rerun", policy=POLICY, now=NOW,
                                        facts=fact)
        self.assertTrue(rerun.accepted, rerun.reason)
        self.assertEqual(rerun.record["budget"]["reruns"],
                         {"head_sha": plain["fork"]["head_sha"], "flaky": 1, "interrupted": 0})
        rejected = engine.apply_transition(plain, "reject_patch_proposal", policy=POLICY,
                                           now=NOW, facts=fact)
        self.assertFalse(rejected.accepted)
        carrying = self.request(self.fix_needed(pending_feedback_evidence_ids=["review-1"]),
                                "request_patch").record
        refused = engine.apply_transition(carrying, "request_ci_rerun", policy=POLICY, now=NOW,
                                          facts=fact)
        self.assertEqual((refused.accepted, refused.reason), (False, "the Work carries feedback"))
        counted = engine.apply_transition(carrying, "reject_patch_proposal", policy=POLICY,
                                          now=NOW, facts=fact)
        self.assertTrue(counted.accepted, counted.reason)
        self.assertEqual(engine.state_key(counted.record), "development.fix_needed")
        self.assertEqual(counted.record["budget"]["no_progress_streak"], 1)

    def test_gate_review_and_ci_failure_are_exclusive_and_record_their_cause(self):
        gate = awaiting_submission()
        values = {"pending_feedback_evidence_ids": ["review-2-1-5"]}
        review = engine.apply_transition(
            gate, "record_actionable_review", policy=POLICY, now=NOW,
            facts={"actionable_review", "candidate_head_not_failed"}, values=values)
        self.assertTrue(review.accepted, review.reason)
        after = review.record
        self.assertEqual(engine.state_key(after), "development.fix_needed")
        self.assertEqual(after["revision"], gate["revision"] + 1)
        self.assertEqual(after["pending_feedback_evidence_ids"], ["review-2-1-5"])
        self.assertEqual((after["clean_head_sha"], after["handoff_request"],
                          after["pending_human_action"]), (None, None, None))
        self.assertEqual(after["last_ci_evidence_id"], gate["last_ci_evidence_id"])
        # Without the fact that CI did not fail, the review row does not apply.
        self.assertFalse(engine.apply_transition(
            gate, "record_actionable_review", policy=POLICY, now=NOW,
            facts={"actionable_review"}, values=values).accepted)
        failed = engine.apply_transition(
            gate, "return_for_repair", policy=POLICY, now=NOW, facts={"candidate_head_failed"},
            values={"last_ci_evidence_id": "ci-2-9-1"})
        self.assertEqual(engine.state_key(failed.record), "development.fix_needed")
        self.assertEqual(failed.record["revision"], gate["revision"] + 1)
        self.assertEqual(failed.record["last_ci_evidence_id"], "ci-2-9-1")
        self.assertEqual(failed.record.get("pending_feedback_evidence_ids") or [], [])
        a = lc.find_edge("handoff.awaiting_submission", "return_for_repair").transition
        b = lc.find_edge("handoff.awaiting_submission", "record_actionable_review").transition
        self.assertTrue(lc.excluded(a, b))
        # The review observed with the failure is recorded in fix_needed.
        both = engine.apply_transition(
            failed.record, "record_actionable_review", policy=POLICY, now=NOW,
            facts={"actionable_review"}, values=values)
        self.assertTrue(both.accepted, both.reason)
        self.assertEqual(engine.state_key(both.record), "development.fix_needed")
        self.assertEqual(both.record["revision"], gate["revision"] + 2)
        self.assertEqual(both.record["pending_feedback_evidence_ids"], ["review-2-1-5"])
        self.assertEqual(both.record["last_ci_evidence_id"], "ci-2-9-1")
        self.assertFalse(engine.apply_transition(
            failed.record, "record_actionable_review", policy=POLICY, now=NOW,
            values=values).accepted)

    def test_explanation_at_the_gate_keeps_everything(self):
        gate = awaiting_submission()
        out = engine.apply_transition(
            gate, "record_explanation", policy=POLICY, now=NOW, facts={"explanation_inquiry"},
            values={"processed_review_ids": [], "processed_review_comment_ids": [6],
                    "processed_pr_conversation_comment_ids": [7]})
        self.assertTrue(out.accepted, out.reason)
        kept = ("revision", "phase", "activity", "handoff_request", "last_ci_evidence_id",
                "pending_human_action", "fork", "active_work")
        self.assertEqual({k: out.record[k] for k in kept}, {k: gate[k] for k in kept})
        self.assertEqual(out.record["processed_review_comment_ids"], [6])
        self.assertEqual(out.event["revision_bumped"], False)

    def test_rerun_counts_are_kept_for_the_current_head(self):
        waiting = contribution(phase="development", activity="waiting_ci")
        head = waiting["fork"]["head_sha"]
        fact = {"ci_interrupted_on_head"}
        once = engine.apply_transition(waiting, "rerun_interrupted_ci", policy=POLICY, now=NOW,
                                       facts=fact)
        self.assertTrue(once.accepted, once.reason)
        self.assertEqual(once.charged, ())
        self.assertEqual(once.record["budget"]["reruns"],
                         {"head_sha": head, "flaky": 0, "interrupted": 1})
        twice = engine.apply_transition(once.record, "rerun_interrupted_ci", policy=POLICY,
                                        now=NOW, facts=fact).record
        limit = lc.budget_limit("interrupted_reruns", POLICY)
        self.assertEqual(twice["budget"]["reruns"]["interrupted"], limit)
        refused = engine.apply_transition(twice, "rerun_interrupted_ci", policy=POLICY, now=NOW,
                                          facts=fact)
        self.assertEqual(refused.reason, "interrupted_reruns is exhausted")
        waited = engine.apply_transition(twice, "exhaust_interrupted_reruns", policy=POLICY,
                                         now=NOW, facts=fact).record
        self.assertEqual(waited["pending_human_action"]["counter"], "interrupted_reruns")
        resumed = engine.apply_command(
            dict(waited, budget=dict(waited["budget"],
                                     reruns=dict(waited["budget"]["reruns"], flaky=1))),
            commands.parse("/upstreaming resume revision=0 reason=r"), policy=POLICY, now=NOW,
            command_id="c")
        self.assertEqual(resumed.verb, "resume_ci")
        self.assertEqual(resumed.record["budget"]["reruns"],
                         {"head_sha": head, "flaky": 1, "interrupted": 0})
        self.assertEqual(resumed.event["details"]["restarted"],
                         {"counter": "interrupted_reruns", "count": limit})
        # A new recorded head starts both counts again.
        moved = engine.apply_transition(twice, "record_head_change", policy=POLICY, now=NOW,
                                        facts={"fork_head_changed"},
                                        values={"fork.head_sha": SHA_B})
        self.assertEqual(moved.record["budget"]["reruns"],
                         {"head_sha": SHA_B, "flaky": 0, "interrupted": 0})
        # Counts recorded for another head are not counts of this one.
        other = dict(twice, fork=dict(twice["fork"], head_sha=SHA_C))
        self.assertTrue(engine.apply_transition(other, "rerun_interrupted_ci", policy=POLICY,
                                                now=NOW, facts=fact).accepted)
        # A record without the field counts from zero.
        bare = dict(waiting, budget={k: v for k, v in waiting["budget"].items() if k != "reruns"})
        self.assertEqual(engine.apply_transition(
            bare, "rerun_interrupted_ci", policy=POLICY, now=NOW,
            facts=fact).record["budget"]["reruns"]["interrupted"], 1)
        setters = {e.verb for e in lc.table_edges() if lc.RERUNS in e.transition.effects.sets}
        self.assertEqual(setters, set(lc.RERUN_RESTART_VERBS))

    def test_the_cluster_budget_is_charged_only_for_a_failing_ci_job(self):
        review_only = self.fix_needed(pending_feedback_evidence_ids=["review-1"],
                                      last_ci_evidence_id="ci-2-7-1")
        review_only["budget"]["clusters"] = {"build riscv64": 99}
        passing = ci_record("ci-2-7-1", SHA_A)
        # Without the evidence the record names, neither row is decided.
        self.assertEqual(self.request(review_only, "request_patch").reason,
                         engine.UNKNOWN_EVIDENCE)
        self.assertEqual(self.request(review_only, "request_repair").reason,
                         engine.UNKNOWN_EVIDENCE)
        # Passing evidence of the head names no failing job: no cluster.
        self.assertEqual(self.request(review_only, "request_repair", ci_evidence=passing).reason,
                         "no failing CI job is recorded for the head")
        patch = self.request(review_only, "request_patch", ci_evidence=passing)
        self.assertTrue(patch.accepted, patch.reason)
        self.assertEqual(patch.record["budget"]["clusters"], {"build riscv64": 99})
        self.assertEqual(patch.record["budget"]["attempts_used"], 1)
        failing = self.fix_needed(last_ci_evidence_id="ci-2-7-1")
        bad = failing_evidence(failing)
        self.assertEqual(self.request(failing, "request_patch", ci_evidence=bad).reason,
                         "a failing CI job is recorded for the head")
        repair = self.request(failing, "request_repair", ci_evidence=bad)
        self.assertEqual(repair.record["budget"]["clusters"], {"build riscv64": 1})
        self.assertEqual(repair.event["details"]["charged"],
                         ["repair_attempts_cluster", "work_total"])
        # Both need that no_progress remains.
        stuck = dict(review_only, budget=dict(review_only["budget"], no_progress_streak=lc.budget_limit(
            "no_progress", POLICY)))
        self.assertEqual(self.request(stuck, "request_patch", ci_evidence=passing).reason,
                         "no_progress is exhausted")
        # Exactly one of the rows of the state applies in each case.
        for rec, evidence in ((review_only, passing), (failing, bad), (stuck, passing)):
            applying = [e.verb for e in lc.due_work_edges("development.fix_needed")
                        if engine.refusal(rec, e.verb, policy=POLICY, now=NOW, facts=self.FACTS,
                                          ci_evidence=evidence) is None]
            self.assertEqual(len(applying), 1, applying)

    def test_a_review_only_upstream_revision_charges_work_total_only(self):
        rec = contribution(phase="upstream_review", activity="fix_needed",
                           upstream_pr=copy.deepcopy(SAMPLE["upstream_pr"]),
                           pending_feedback_evidence_ids=["review-1"])
        rec["budget"]["clusters"] = {"build riscv64": 99}
        facts = {"worker_slot_free"}
        args = dict(policy=POLICY, now=NOW, facts=facts,
                    values={"active_work": dict(SAMPLE["active_work"])})
        self.assertFalse(engine.apply_transition(rec, "request_upstream_revision", **args).accepted)
        out = engine.apply_transition(rec, "request_review_revision", **args)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.event["details"]["charged"], ["work_total"])
        self.assertEqual(out.record["active_work"]["feedback_evidence_ids"], ["review-1"])
        self.assertEqual(engine.state_key(out.record), "upstream_review.agent_running")
        failing = dict(rec, last_ci_evidence_id="ci-1-7-1", budget=dict(rec["budget"], clusters={}))
        bad = failing_evidence(failing, "ci-1-7-1")
        self.assertFalse(engine.apply_transition(failing, "request_review_revision",
                                                 ci_evidence=bad, **args).accepted)
        revision = engine.apply_transition(failing, "request_upstream_revision",
                                           ci_evidence=bad, **args)
        self.assertEqual(revision.event["details"]["charged"],
                         ["repair_attempts_cluster", "work_total"])
        for record, evidence in ((rec, None), (failing, bad)):
            applying = [e.verb for e in lc.due_work_edges("upstream_review.fix_needed")
                        if engine.refusal(record, e.verb, policy=POLICY, now=NOW, facts=facts,
                                          ci_evidence=evidence) is None]
            self.assertEqual(len(applying), 1, applying)

    def test_patch_work_needs_the_fork_and_admission_checks_the_scope(self):
        no_fork = self.fix_needed(fork=None)
        self.assertEqual(self.request(no_fork, "request_patch").reason,
                         "fork not provisioned/verified")
        self.assertNotIn("proposal or write admission", engine.PREDICATES)
        admit = lc.find_edge("planning.agent_running", "admit_development").transition
        self.assertIn(lc.PATCH_SCOPE_COMPLETE, admit.guard)
        running = contribution(phase="planning", activity="agent_running", fork=None)
        values = {"plan_evidence_id": "plan-1", "scope_paths": ["pyproject.toml"],
                  "admission_class": "configuration_integration"}
        facts = {"development_admitted", "fresh_fork_required"}
        refused = engine.apply_transition(running, "admit_development", policy=POLICY, now=NOW,
                                          facts=facts, values=values)
        self.assertFalse(refused.accepted)
        admitted = engine.apply_transition(running, "admit_development", policy=POLICY, now=NOW,
                                           facts=facts | {"patch_scope_complete"}, values=values)
        self.assertEqual(engine.state_key(admitted.record), "provisioning.idle")
        sources = {e.source for e in lc.table_edges()
                   if e.transition.effects.work == "allocate" and lc.LIFECYCLE[e.source].writes}
        self.assertEqual(sources, {"development.fix_needed", "upstream_review.fix_needed"})


class TestEngineRefusesWhatTheTableDoesNotState(unittest.TestCase):
    def test_unknown_verb_state_fact_and_value(self):
        rec = awaiting_submission()
        with self.assertRaises(lc.TransitionError):
            engine.apply_transition(rec, "request_patch", policy=POLICY, now=NOW)
        with self.assertRaises(lc.TransitionError):
            engine.apply_transition(contribution(activity="idle"), "nominate_for_handoff",
                                    policy=POLICY, now=NOW)
        with self.assertRaises(lc.TransitionError):
            engine.apply_transition(rec, "return_for_repair", policy=POLICY, now=NOW,
                                    facts={"made_up"})
        with self.assertRaises(lc.TransitionError):
            engine.apply_transition(rec, "return_for_repair", policy=POLICY, now=NOW,
                                    facts={"candidate_head_failed"},
                                    values={"last_ci_evidence_id": "ci-9", "clean_head_sha": SHA_A})
        with self.assertRaises(lc.TransitionError):
            # A caller that asserts a failure and its absence established neither.
            engine.apply_transition(rec, "record_actionable_review", policy=POLICY, now=NOW,
                                    facts={"actionable_review", "candidate_head_failed",
                                           "candidate_head_not_failed"},
                                    values={"pending_feedback_evidence_ids": ["review-1"]})
        with self.assertRaises(lc.TransitionError):
            # The engine derives what the Work carries; the caller cannot state it.
            fix = contribution(phase="development", activity="fix_needed")
            engine.apply_transition(
                fix, "request_patch", policy=POLICY, now=NOW,
                facts={"worker_slot_free", "capacity_available"},
                values={"active_work": dict(SAMPLE["active_work"]),
                        lc.WORK_FEEDBACK: ["review-1"]})
        with self.assertRaises(lc.TransitionError):
            engine.apply_transition(rec, "record_head_change", policy=POLICY, now=NOW,
                                    facts={"candidate_head_changed"})

    def test_work_is_allocated_only_with_the_bound_upstream_identity(self):
        rec = contribution(phase="planning", activity="idle", fork=None)
        work = dict(SAMPLE["active_work"])
        for missing in ("repository_id", "base_ref", "base_sha"):
            with self.subTest(missing=missing):
                unbound = dict(rec, upstream=dict(rec["upstream"], **{missing: None}))
                out = engine.apply_transition(unbound, "dispatch_planning", policy=POLICY,
                                              now=NOW, facts={"worker_slot_free"},
                                              values={"active_work": work})
                self.assertFalse(out.accepted)
                self.assertEqual(out.reason,
                                 f"upstream identity and base not bound: upstream.{missing}")
                self.assertEqual(out.record, unbound)

    def test_charges_and_external_counters(self):
        rec = contribution(phase="planning", activity="idle", fork=None)
        work = dict(SAMPLE["active_work"])
        out = engine.apply_transition(rec, "dispatch_planning", policy=POLICY, now=NOW,
                                      facts={"worker_slot_free"}, values={"active_work": work})
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.record["budget"]["attempts_used"], 1)
        self.assertEqual(out.event["details"]["charged"], ["work_total"])
        self.assertEqual(out.charged, ())
        spent = dict(rec, budget=dict(rec["budget"], attempts_used=lc.budget_limit("work_total", POLICY)))
        refused = engine.apply_transition(spent, "dispatch_planning", policy=POLICY, now=NOW,
                                          facts={"worker_slot_free"}, values={"active_work": work})
        self.assertEqual((refused.accepted, refused.reason), (False, "work_total is exhausted"))
        waiting = engine.apply_transition(spent, "exhaust_work_total", policy=POLICY, now=NOW)
        self.assertEqual(engine.state_key(waiting.record), "planning.waiting_human")
        self.assertEqual(waiting.record["pending_human_action"]["kind"], "work_budget_exhausted")
        self.assertEqual(waiting.record["pending_human_action"]["counter"], "work_total")
        # A budget counted outside the record is refused until its count is supplied.
        running = dict(out.record)
        unknown = engine.apply_transition(running, "retry_dispatch", policy=POLICY, now=NOW,
                                          facts={"dispatch_lost"})
        self.assertEqual(unknown.reason, "the count of dispatch_recovery is not known")
        retried = engine.apply_transition(running, "retry_dispatch", policy=POLICY, now=NOW,
                                          facts={"dispatch_lost"}, counts={"dispatch_recovery": 1})
        self.assertEqual(retried.charged, ("dispatch_recovery",))

    def test_resume_starts_only_the_counter_the_wait_names(self):
        rec = contribution(phase="planning", activity="waiting_human")
        rec["budget"].update(attempts_used=24, planning_attempts=3, no_progress_streak=2)
        rec["pending_human_action"] = {"kind": "work_budget_exhausted", "requested_at": NOW,
                                       "reminded_at": None, "escalated_at": None,
                                       "counter": "work_total"}
        resume = commands.parse("/upstreaming resume revision=0 reason=r")
        out = engine.apply_command(rec, resume, policy=POLICY, now=NOW, command_id="comment-9",
                                   actor=OWNER)
        self.assertEqual(out.verb, "resume_planning")
        self.assertEqual(out.record["budget"]["attempts_used"], 0)
        self.assertEqual(out.record["budget"]["planning_attempts"], 3)
        self.assertEqual(out.record["budget"]["no_progress_streak"], 2)
        self.assertEqual(out.event["details"]["restarted"], {"counter": "work_total", "count": 24})
        self.assertEqual(out.event["details"]["actor"], OWNER)
        self.assertEqual(out.event["cause"], {"type": "command", "id": "comment-9"})
        unnamed = dict(rec, pending_human_action=dict(rec["pending_human_action"], counter=None))
        kept = engine.apply_command(unnamed, resume, policy=POLICY, now=NOW, command_id="c")
        self.assertEqual(kept.record["budget"]["attempts_used"], 24)
        self.assertNotIn("restarted", kept.event["details"])

    def test_pause_clears_work_and_resume_clears_the_flag_first(self):
        rec = contribution(phase="development", activity="agent_running",
                           active_work=dict(SAMPLE["active_work"]))
        pause = commands.parse('/upstreaming pause reason="hold on"')
        out = engine.apply_command(rec, pause, policy=POLICY, now=NOW, command_id="c1")
        paused = out.record
        self.assertEqual(engine.state_key(paused), "development.fix_needed")
        self.assertIsNone(paused["active_work"])
        self.assertEqual((paused["paused"], paused["paused_at"], paused["paused_by"],
                          paused["paused_reason"]), (True, NOW, "human", "hold on"))
        self.assertEqual(paused["revision"], rec["revision"])
        again = engine.apply_command(paused, pause, policy=POLICY, now=NOW, command_id="c2")
        self.assertEqual((again.accepted, again.reason), (False, "contribution is paused"))
        # In a human wait the first resume clears the flag only.
        waiting = contribution(phase="development", activity="waiting_human", paused=True,
                               paused_at=NOW, paused_by="human")
        waiting["pending_human_action"] = {"kind": "work_failed", "requested_at": NOW,
                                           "reminded_at": None, "escalated_at": None,
                                           "counter": "work_recovery"}
        resume = commands.parse("/upstreaming resume revision=0 reason=r")
        first = engine.apply_command(waiting, resume, policy=POLICY, now=NOW, command_id="c3")
        self.assertEqual(first.verb, "resume_pause")
        self.assertEqual(engine.state_key(first.record), "development.waiting_human")
        self.assertEqual((first.record["paused"], first.record["paused_at"]), (False, None))
        self.assertEqual(first.record["pending_human_action"], waiting["pending_human_action"])
        second = engine.apply_command(
            first.record, commands.parse("/upstreaming resume revision=1 reason=r"),
            policy=POLICY, now=NOW, command_id="c4")
        self.assertEqual(second.verb, "resume_development")
        self.assertEqual(engine.state_key(second.record), "development.fix_needed")

    def test_every_stop_clears_the_pending_publication(self):
        pending = {"worker_run_id": "555", "attempts": 1, "last_reason": "x",
                   "next_attempt_at": NOW, "exhausted": False}
        held = contribution(phase="development", activity="agent_running",
                            pending_publication=dict(pending))
        park = commands.parse('/upstreaming park reason="stop"')
        pause = commands.parse("/upstreaming pause")
        refresh = commands.parse("/upstreaming refresh revision=0 reason=r")
        self.assertEqual(
            engine.refusal(dict(held, phase="parked", activity="idle"), "restart_generation",
                           policy=POLICY, now=NOW, command=refresh, open_operations=()),
            "a publication is pending")

        parked = engine.apply_command(held, park, policy=POLICY, now=NOW, command_id="c",
                                      open_operations=())
        self.assertEqual((parked.verb, engine.state_key(parked.record)),
                         ("park_contribution", "parked.idle"))
        self.assertIsNone(parked.record["pending_publication"])
        self.assertEqual(parked.record["revision"], held["revision"])
        restarted = engine.apply_command(parked.record, refresh, policy=POLICY, now=NOW,
                                         command_id="c", open_operations=())
        self.assertEqual((restarted.verb, engine.state_key(restarted.record)),
                         ("restart_generation", "queued.idle"))

        paused = engine.apply_command(held, pause, policy=POLICY, now=NOW, command_id="c")
        self.assertEqual((paused.verb, engine.state_key(paused.record)),
                         ("pause", "development.fix_needed"))
        self.assertEqual((paused.record["paused"], paused.record["pending_publication"]),
                         (True, None))
        # A publication whose retries are exhausted is cleared by the stop as well.
        waiting = contribution(phase="development", activity="waiting_human",
                               pending_publication=dict(pending, exhausted=True))
        waiting["pending_human_action"] = {
            "kind": "publication_infrastructure_failure", "requested_at": NOW,
            "reminded_at": None, "escalated_at": None, "counter": "publish_recovery"}
        for command in (pause, park):
            out = engine.apply_command(waiting, command, policy=POLICY, now=NOW, command_id="c",
                                       open_operations=())
            self.assertTrue(out.accepted, out.reason)
            self.assertIsNone(out.record["pending_publication"])

        identity = engine.apply_transition(held, "pause_on_identity_change", policy=POLICY,
                                           now=NOW, facts={"upstream_identity_changed"})
        self.assertEqual((identity.record["paused_by"], identity.record["pending_publication"]),
                         ("controller", None))
        # A record parked before the stop cleared the publication is still abandoned cleanly.
        left = contribution(phase="parked", activity="idle", parked_reason="x",
                            previous_phase="development", pending_publication=dict(pending))
        abandoned = engine.apply_command(left, commands.parse('/upstreaming abandon reason="x"'),
                                         policy=POLICY, now=NOW, command_id="c")
        self.assertEqual((engine.state_key(abandoned.record),
                          abandoned.record["pending_publication"]), ("closed.idle", None))

    def test_every_stop_keeps_a_published_result(self):
        """`proposal_artifact` only ever holds a published Result: no stop
        clears it, and no row clears a part of it."""
        self.assertNotIn("held proposal_artifact", lc.QUALIFIED_CLEARS)
        published = contribution(phase="development", activity="fix_needed",
                                 proposal_artifact=dict(SAMPLE["proposal_artifact"],
                                                        outcome="published"))
        for command in (commands.parse('/upstreaming park reason="stop"'),
                        commands.parse("/upstreaming pause")):
            with self.subTest(command=command.verb):
                kept = engine.apply_command(published, command, policy=POLICY, now=NOW,
                                            command_id="c", open_operations=())
                self.assertTrue(kept.accepted, kept.reason)
                self.assertEqual(kept.record["proposal_artifact"],
                                 published["proposal_artifact"])
        identity = engine.apply_transition(published, "pause_on_identity_change", policy=POLICY,
                                           now=NOW, facts={"upstream_identity_changed"})
        self.assertEqual(identity.record["proposal_artifact"], published["proposal_artifact"])

    def test_a_long_stop_in_development_resumes_into_a_refresh(self):
        stale = contribution(phase="development", activity="fix_needed", paused=True,
                             paused_at=LONG_AGO, paused_by="human")
        resume = commands.parse("/upstreaming resume revision=0 reason=r")
        refresh = dict(SAMPLE["refresh"])
        out = engine.apply_command(stale, resume, policy=POLICY, now=NOW, command_id="c",
                                   values={"refresh": refresh})
        self.assertEqual(out.verb, "resume_stale")
        self.assertEqual(engine.state_key(out.record), "queued.idle")
        self.assertEqual((out.record["paused"], out.record["refresh"]), (False, refresh))
        fresh = dict(stale, paused_at=NOW)
        self.assertEqual(engine.resolve_command(fresh, resume, policy=POLICY, now=NOW)[0],
                         "resume_pause")
        # The handoff gate is never refreshed by a resume.
        gate = awaiting_submission(paused=True, paused_at=LONG_AGO, paused_by="human")
        self.assertEqual(engine.resolve_command(gate, resume, policy=POLICY, now=NOW)[0],
                         "resume_pause")

    def test_park_needs_a_reason_and_no_open_operation(self):
        rec = contribution(phase="development", activity="waiting_ci", revision=4)
        stale_park = commands.parse('/upstreaming park revision=2 reason="stop"')
        out = engine.apply_command(rec, stale_park, policy=POLICY, now=NOW, command_id="c")
        self.assertEqual(engine.state_key(out.record), "parked.idle")
        self.assertEqual((out.record["parked_reason"], out.record["previous_phase"]),
                         ("stop", "development"))
        busy = engine.apply_command(rec, stale_park, policy=POLICY, now=NOW, command_id="c",
                                    open_operations=("op-7",))
        self.assertEqual((busy.accepted, busy.reason), (False, "operation op-7 is open"))
        locked = contribution(phase="release_wait", activity="idle")
        verb, why = engine.resolve_command(locked, stale_park, policy=POLICY, now=NOW)
        self.assertIsNone(verb)
        self.assertTrue(why.startswith("`/upstreaming park` does not apply in `release_wait.idle`; "
                                       "it applies in `development.agent_running`, "), why)

    def test_restart_starts_a_clean_generation(self):
        rec = contribution(phase="parked", activity="idle", parked_reason="x", revision=7,
                           plan_evidence_id="plan-1", last_ci_evidence_id="ci-1",
                           processed_publications=[5])
        refresh = commands.parse("/upstreaming refresh revision=7 reason=r")
        out = engine.apply_command(rec, refresh, policy=POLICY, now=NOW, command_id="c")
        new = out.record
        self.assertEqual(out.verb, "restart_generation")
        self.assertEqual((engine.state_key(new), new["generation"], new["revision"]),
                         ("queued.idle", 1, 8))
        self.assertEqual((new["fork"], new["plan_evidence_id"], new["parked_reason"],
                          new["admission_override"]), (None, None, None, None))
        self.assertEqual(new["processed_publications"], [5])
        self.assertEqual(new["contribution_id"], rec["contribution_id"])
        with_pr = dict(rec, upstream_pr=SAMPLE["upstream_pr"])
        refused = engine.apply_command(with_pr, refresh, policy=POLICY, now=NOW, command_id="c")
        self.assertFalse(refused.accepted)
        resume = commands.parse("/upstreaming resume revision=7 reason=r")
        self.assertEqual(engine.command_verbs(rec, resume), ["resume_pause"])
        self.assertFalse(engine.apply_command(rec, resume, policy=POLICY, now=NOW,
                                              command_id="c").accepted)


class TestSubmitUpstream(unittest.TestCase):
    """`submit-upstream` is the table's `submit_upstream`: exact revision and
    exact candidate head, from `handoff.awaiting_submission` only."""

    VALUES = {"handoff_approval": {"command_id": "comment-1", "actor": OWNER, "head": SHA_A,
                                   "revision": 0, "evidence_ids": ["ci-2-7-1"]}}

    def submit(self, rec, text, **kw):
        return engine.apply_command(rec, commands.parse(text), policy=POLICY, now=NOW,
                                    command_id="comment-1", actor=OWNER, values=self.VALUES, **kw)

    def test_grammar_requires_revision_and_full_head(self):
        cmd = commands.parse(f"/upstreaming submit-upstream revision=6 head={SHA_A}")
        self.assertEqual((cmd.verb, cmd.revision, cmd.args["head"]), ("submit-upstream", 6, SHA_A))
        for text in ("/upstreaming submit-upstream revision=6",
                     f"/upstreaming submit-upstream head={SHA_A}",
                     "/upstreaming submit-upstream revision=6 head=abc123",
                     f"/upstreaming submit-upstream revision=6 head={SHA_A} url=https://x"):
            with self.subTest(text=text), self.assertRaises(commands.CommandError):
                commands.parse(text)

    def test_exact_revision_and_candidate_head_enter_the_submission_review(self):
        rec = awaiting_submission()
        out = self.submit(rec, f"/upstreaming submit-upstream revision=0 head={SHA_A}")
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.verb, "submit_upstream")
        new = out.record
        self.assertEqual(engine.state_key(new), "handoff.review_due")
        self.assertEqual(new["handoff_approval"], self.VALUES["handoff_approval"])
        self.assertIsNone(new["active_operation_id"])
        self.assertIsNone(new["pending_human_action"])
        self.assertEqual(new["revision"], rec["revision"])
        self.assertEqual((new["fork"], new["handoff_request"], new["last_ci_evidence_id"]),
                         (rec["fork"], rec["handoff_request"], rec["last_ci_evidence_id"]))

    def test_other_revision_head_state_or_pause_is_refused(self):
        rec = awaiting_submission(revision=6)
        rec["handoff_request"]["revision"] = 6
        cases = (
            (rec, f"/upstreaming submit-upstream revision=5 head={SHA_A}",
             "stale revision 5, current is 6"),
            (rec, f"/upstreaming submit-upstream revision=7 head={SHA_A}",
             "revision is not a known revision"),
            (rec, f"/upstreaming submit-upstream revision=6 head={SHA_B}",
             "head is not the candidate head"),
            (dict(rec, fork=dict(rec["fork"], head_sha=SHA_B)),
             f"/upstreaming submit-upstream revision=6 head={SHA_A}",
             "no candidate head is recorded for the current fork head"),
            (dict(rec, paused=True, paused_at=NOW, paused_by="human"),
             f"/upstreaming submit-upstream revision=6 head={SHA_A}", "contribution is paused"),
            (contribution(phase="development", activity="waiting_ci", revision=6),
             f"/upstreaming submit-upstream revision=6 head={SHA_A}",
             "`/upstreaming submit-upstream` does not apply in `development.waiting_ci`; it "
             "applies in `handoff.awaiting_submission`"),
        )
        for record, text, reason in cases:
            with self.subTest(text=text, reason=reason):
                out = self.submit(record, text)
                self.assertEqual((out.accepted, out.reason), (False, reason))
                self.assertIs(out.record, record)

    def test_a_resume_from_pause_keeps_the_submission(self):
        rec = awaiting_submission(paused=True, paused_at=NOW, paused_by="human")
        out = engine.apply_command(rec, commands.parse("/upstreaming resume revision=0 reason=r"),
                                   policy=POLICY, now=NOW, command_id="c")
        new = out.record
        self.assertEqual((engine.state_key(new), new["revision"], new["paused"]),
                         ("handoff.awaiting_submission", 1, False))
        self.assertEqual((new["handoff_request"], new["pending_human_action"]),
                         (rec["handoff_request"], rec["pending_human_action"]))
        # The command binds to the exact head, so it applies at the new revision.
        self.assertTrue(self.submit(new, f"/upstreaming submit-upstream revision=1 head={SHA_A}").accepted)


FENCED = set(engine.FENCED_FIELDS)
WRITERS = {"engine.py"}
# Names that hold a tick report, a command receipt, a Work request, event
# details, the values a row sets, a Work context, or the command grammar,
# never a contribution record.
REPORT_NAMES = {"entry", "receipt", "aw", "request", "details", "values", "context", "OPTIONAL"}


def fenced_writes(path: Path) -> list[str]:
    """Places where a module assigns a fenced lifecycle field of a record."""
    found = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        where = f"{path.name}:{getattr(node, 'lineno', 0)}"
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) \
                        and sub.slice.value in FENCED \
                        and not (isinstance(sub.value, ast.Name) and sub.value.id in REPORT_NAMES):
                    found.append(f"{where}: [{sub.slice.value!r}] is assigned")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else \
                node.func.attr if isinstance(node.func, ast.Attribute) else ""
            keywords = {k.arg for k in node.keywords} & FENCED
            literal = {k.value for a in node.args if isinstance(a, ast.Dict)
                       for k in a.keys if isinstance(k, ast.Constant)} & FENCED
            receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
            report = isinstance(receiver, ast.Name) and receiver.id in REPORT_NAMES
            first = node.args[0] if node.args else None
            if name == "dict" and keywords \
                    and not (isinstance(first, ast.Name) and first.id in REPORT_NAMES):
                found.append(f"{where}: dict(..., {', '.join(sorted(keywords))}=...)")
            if name in ("update", "setdefault", "replace") and (keywords or literal) and not report:
                found.append(f"{where}: .{name}() sets {', '.join(sorted(keywords | literal))}")
            if name in ("setdefault", "pop", "__setitem__") and node.args \
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value in FENCED:
                found.append(f"{where}: .{name}({node.args[0].value!r})")
        unpacked = [v for k, v in zip(node.keys, node.values) if k is None] \
            if isinstance(node, ast.Dict) else []
        if any(isinstance(v, (ast.Name, ast.Attribute, ast.Subscript))
               and not (isinstance(v, ast.Name) and v.id in REPORT_NAMES) for v in unpacked) \
                and any(isinstance(k, ast.Constant) and k.value in FENCED for k in node.keys):
            found.append(f"{where}: a record is copied with a fenced field replaced")
    return found


def pipeline_modules() -> list[Path]:
    return sorted(p for p in (*ROOT.glob("*.py"), *ROOT.glob("scripts/*.py")))


class TestSingleWritePath(unittest.TestCase):
    def test_only_the_engine_and_the_shim_assign_lifecycle_state(self):
        offenders = [w for path in pipeline_modules() if path.name not in WRITERS
                     for w in fenced_writes(path)]
        self.assertEqual(offenders, [])

    def test_the_scan_finds_each_kind_of_write(self):
        import tempfile
        samples = ('rec["phase"] = "x"', 'rec["revision"] += 1', 'new = dict(rec, activity="idle")',
                   'rec.update({"paused": True})', 'rec.update(generation=2)',
                   'new = {**rec, "phase": "x"}', 'rec.setdefault("paused", False)')
        with tempfile.TemporaryDirectory() as directory:
            for sample in samples:
                path = Path(directory) / "sample.py"
                path.write_text(sample + "\n")
                with self.subTest(sample=sample):
                    self.assertEqual(len(fenced_writes(path)), 1)
            path.write_text('entry["activity"] = "x"\nevent = {"phase": 1}\nx = c["revision"] + 1\n')
            self.assertEqual(fenced_writes(path), [])

    def test_the_shim_is_gone(self):
        self.assertFalse((ROOT / "compat.py").exists())
        for path in pipeline_modules():
            tree = ast.parse(path.read_text())
            names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                     for alias in node.names}
            self.assertNotIn("compat", names, path.name)

    def test_the_fence_is_every_field_a_table_effect_names(self):
        named = set()
        transitions = [e.transition for e in lc.table_edges()]
        for flag in lc.FLAGS.values():
            transitions += [*flag.set_by.values(), *flag.cleared_by.values()]
        for t in transitions:
            for entry in (*t.effects.sets, *t.effects.clears):
                named.add(lc._field_name(entry).removeprefix("carried ").split(".")[0])
        self.assertLessEqual(named, set(engine.FENCED_FIELDS))
        self.assertLessEqual(set(engine.STATE_FIELDS) | {"active_work"}, set(engine.FENCED_FIELDS))

    def test_every_write_beside_a_row_writes_fenced_fields_only(self):
        for write, fields in engine.BESIDE_ROW.items():
            for name in fields:
                with self.subTest(write=write, field=name):
                    self.assertIn(name.split(".")[0], engine.FENCED_FIELDS)
                    self.assertNotIn(name.split(".")[0], engine.STATE_FIELDS)

    def test_every_state_the_schema_admits_is_a_state_of_the_table(self):
        for key in ("development.idle", "development.waiting_capacity", "upstream_review.idle",
                    "upstream_review.waiting_capacity", "handoff.idle", "closed.waiting_human"):
            with self.subTest(state=key):
                self.assertIsNotNone(engine.state_problem(contribution(
                    phase=key.split(".")[0], activity=key.split(".")[1])))

    def test_no_fork_write_is_admitted_at_the_submission_gate(self):
        args = dict(expected_revision=0, expected_head=SHA_A, observed_head=SHA_A,
                    ref="riscv64-dev", repository="riseproject-upstreaming/proj")
        self.assertIsNone(lc.publication_check(contribution(activity="agent_running"), **args))
        self.assertEqual(lc.publication_check(awaiting_submission(), **args),
                         "state handoff.awaiting_submission does not permit fork writes")
        for activity in ("review_due", "agent_running", "waiting_ci", "preparing_submission"):
            other = awaiting_submission(activity=activity, pending_human_action=None)
            self.assertEqual(lc.publication_check(other, **args),
                             f"state handoff.{activity} does not permit fork writes")
        # The build of `riscv64` is the one handoff state that writes, and only
        # `riscv64`: never the development branch.
        building = awaiting_submission(activity="building_upstream_branch",
                                       pending_human_action=None)
        self.assertEqual(lc.publication_check(building, **args),
                         "ref 'riscv64-dev' is not 'riscv64'")
        self.assertIsNone(lc.publication_check(building, **dict(args, ref="riscv64")))

    def test_development_writes_only_the_development_branch(self):
        args = dict(expected_revision=0, expected_head=SHA_A, observed_head=SHA_A,
                    ref="riscv64", repository="riseproject-upstreaming/proj")
        rec = contribution(activity="agent_running")
        self.assertEqual(lc.publication_check(rec, **args), "ref 'riscv64' is not 'riscv64-dev'")
        legacy = dict(rec, fork=dict(rec["fork"], head_ref="riscv64"))
        self.assertEqual(lc.publication_check(legacy, **dict(args, ref="riscv64-dev")),
                         "the recorded development branch is not 'riscv64-dev'")

    def test_handoff_waits_carry_the_pending_action_the_table_names(self):
        self.assertIsNone(engine.state_problem(awaiting_submission()))
        for kind in ("approve_handoff", "open_upstream_pr", None):
            pending = None if kind is None else dict(awaiting_submission()["pending_human_action"],
                                                    kind=kind)
            for activity in ("awaiting_submission", "waiting_human", "waiting_upstream_pr"):
                with self.subTest(kind=kind, activity=activity):
                    rec = awaiting_submission(activity=activity, pending_human_action=pending)
                    expected = kind == "open_upstream_pr" and activity == "waiting_upstream_pr"
                    self.assertEqual(engine.state_problem(rec) is None, expected)


if __name__ == "__main__":
    unittest.main()
