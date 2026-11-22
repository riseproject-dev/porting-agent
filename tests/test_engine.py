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
import compat
import engine
import lifecycle as lc
import schemas
from tests.helpers import NOW, OWNER, SHA_A, SHA_B, SHA_C, awaiting_submission, contribution, policy

ROOT = Path(__file__).resolve().parents[1]
POLICY = policy()
LONG_AGO = (datetime.fromisoformat(NOW) - timedelta(days=40)).isoformat()

# A value for every field a row sets without stating its value.
SAMPLE = {
    "fork": {"repository_id": 2, "name": "riseproject-upstreaming/proj", "head_ref": "riscv64",
             "head_sha": SHA_A, "pr_number": None},
    "fork.head_sha": SHA_B,
    "upstream.repository_id": 1, "upstream.base_ref": "main", "upstream.base_sha": SHA_C,
    "upstream_pr": {"id": 9, "number": 9, "url": "https://github.com/example/proj/pull/9",
                    "head_repository_id": 2, "head_ref": "riscv64", "head_sha": SHA_A,
                    "base_ref": "main", "registered_by": OWNER},
    "upstream_pr.head_sha": SHA_B,
    "dossier_evidence_id": "dossier-1", "admission_class": "configuration_integration",
    "scope_paths": ["pyproject.toml"], "parked_reason": "no bounded patch",
    "terminal_reason": "not applicable", "last_ci_evidence_id": "ci-2-8-1",
    "clean_head_sha": SHA_B,
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
    "admission_override": {"class": "configuration_integration", "paths": ["a"], "reason": "r"},
    "active_work": {"dedupe_key": "example--proj/12@0:investigation", "task": "investigation",
                    "revision": 0, "attempt": 1, "requested_at": NOW, "deadline_at": NOW},
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
    if lc.REPAIR_DUE in guards:
        rec["last_ci_evidence_id"] = "ci-2-7-1"
    if lc.REFRESH_USED in guards:
        rec["automatic_refresh_used"] = True
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
    if lc.REPAIR_BUDGETS_REMAIN in guards or lc.REVISION_BUDGETS_REMAIN in guards:
        counts.update(repair_attempts_cluster=0, no_progress=0, work_total=0)
    if lc.REPAIR_BUDGET_EXHAUSTED in guards:
        counts["no_progress"] = lc.budget_limit("no_progress", POLICY)
    if lc.REVISION_BUDGET_EXHAUSTED in guards:
        counts.update(repair_attempts_cluster=0, no_progress=0,
                      work_total=lc.budget_limit("work_total", POLICY))
    if verb == "require_provisioning_intervention":
        facts.add("provisioning_blocked")
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
                  cluster="build riscv64" if "repair_attempts_cluster" in t.charges else None)
    return rec, kwargs


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
        self.assertEqual(report["legacy_write_sites"], sorted(compat.LEGACY_SITES))

    def test_every_row_is_taken_with_its_effects(self):
        rows = all_rows()
        self.assertGreaterEqual(len(rows), 155)
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
                    if name not in sets:
                        self.assertFalse(get(new, name), f"{name} is not cleared")
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
                self.assertIsNone(compat.state_problem(new) if new["phase"] != "handoff"
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


class TestEngineRefusesWhatTheTableDoesNotState(unittest.TestCase):
    def test_unknown_verb_state_fact_and_value(self):
        rec = awaiting_submission()
        with self.assertRaises(lc.TransitionError):
            engine.apply_transition(rec, "request_patch", policy=POLICY, now=NOW)
        with self.assertRaises(lc.TransitionError):
            engine.apply_transition(contribution(), "nominate_for_handoff", policy=POLICY, now=NOW)
        with self.assertRaises(lc.TransitionError):
            engine.apply_transition(rec, "return_for_repair", policy=POLICY, now=NOW,
                                    facts={"made_up"})
        with self.assertRaises(lc.TransitionError):
            engine.apply_transition(rec, "return_for_repair", policy=POLICY, now=NOW,
                                    facts={"candidate_head_needs_repair"},
                                    values={"last_ci_evidence_id": "ci-9"})
        with self.assertRaises(lc.TransitionError):
            engine.apply_transition(rec, "record_head_change", policy=POLICY, now=NOW,
                                    facts={"candidate_head_changed"})

    def test_charges_and_external_counters(self):
        rec = contribution(phase="investigation", activity="idle", fork=None)
        work = dict(SAMPLE["active_work"])
        out = engine.apply_transition(rec, "dispatch_investigation", policy=POLICY, now=NOW,
                                      facts={"worker_slot_free"}, values={"active_work": work})
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.record["budget"]["attempts_used"], 1)
        self.assertEqual(out.event["details"]["charged"], ["work_total"])
        self.assertEqual(out.charged, ())
        spent = dict(rec, budget=dict(rec["budget"], attempts_used=lc.budget_limit("work_total", POLICY)))
        refused = engine.apply_transition(spent, "dispatch_investigation", policy=POLICY, now=NOW,
                                          facts={"worker_slot_free"}, values={"active_work": work})
        self.assertEqual((refused.accepted, refused.reason), (False, "work_total is exhausted"))
        waiting = engine.apply_transition(spent, "exhaust_work_total", policy=POLICY, now=NOW)
        self.assertEqual(engine.state_key(waiting.record), "investigation.waiting_human")
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
        rec = contribution(phase="investigation", activity="waiting_human")
        rec["budget"].update(attempts_used=24, investigation_attempts=3, no_progress_streak=2)
        rec["pending_human_action"] = {"kind": "work_budget_exhausted", "requested_at": NOW,
                                       "reminded_at": None, "escalated_at": None,
                                       "counter": "work_total"}
        resume = commands.parse("/upstreaming resume revision=0 reason=r")
        out = engine.apply_command(rec, resume, policy=POLICY, now=NOW, command_id="comment-9",
                                   actor=OWNER)
        self.assertEqual(out.verb, "resume_investigation")
        self.assertEqual(out.record["budget"]["attempts_used"], 0)
        self.assertEqual(out.record["budget"]["investigation_attempts"], 3)
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
        self.assertEqual(engine.resolve_command(locked, stale_park, policy=POLICY, now=NOW),
                         (None, "`/upstreaming park` does not apply in `release_wait.idle`"))

    def test_restart_starts_a_clean_generation(self):
        rec = contribution(phase="parked", activity="idle", parked_reason="x", revision=7,
                           dossier_evidence_id="dossier-1", last_ci_evidence_id="ci-1",
                           processed_publications=[5])
        resume = commands.parse("/upstreaming resume revision=7 reason=r")
        out = engine.apply_command(rec, resume, policy=POLICY, now=NOW, command_id="c")
        new = out.record
        self.assertEqual(out.verb, "restart_generation")
        self.assertEqual((engine.state_key(new), new["generation"], new["revision"]),
                         ("queued.idle", 1, 8))
        self.assertEqual((new["fork"], new["dossier_evidence_id"], new["parked_reason"],
                          new["admission_override"]), (None, None, None, None))
        self.assertEqual(new["processed_publications"], [5])
        self.assertEqual(new["contribution_id"], rec["contribution_id"])
        with_pr = dict(rec, upstream_pr=SAMPLE["upstream_pr"])
        refused = engine.apply_command(with_pr, resume, policy=POLICY, now=NOW, command_id="c")
        self.assertFalse(refused.accepted)


class TestSubmitUpstream(unittest.TestCase):
    """`submit-upstream` is the table's `submit_upstream`: exact revision and
    exact candidate head, from `handoff.awaiting_submission` only."""

    VALUES = {"handoff_approval": {"command_id": "comment-1", "actor": OWNER, "head": SHA_A,
                                   "revision": 0, "evidence_ids": ["ci-2-7-1"]},
              "active_operation_id": "op-handoff-1"}

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

    def test_exact_revision_and_candidate_head_enter_debug_cleanup(self):
        rec = awaiting_submission()
        out = self.submit(rec, f"/upstreaming submit-upstream revision=0 head={SHA_A}")
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.verb, "submit_upstream")
        new = out.record
        self.assertEqual(engine.state_key(new), "handoff.cleaning_debug")
        self.assertEqual(new["handoff_approval"], self.VALUES["handoff_approval"])
        self.assertEqual(new["active_operation_id"], "op-handoff-1")
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
             "`/upstreaming submit-upstream` does not apply in `development.waiting_ci`"),
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
WRITERS = {"engine.py", "compat.py"}
# Names that hold a tick report, a command receipt, or a Work request, never
# a contribution record.
REPORT_NAMES = {"entry", "receipt", "aw"}


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
        if isinstance(node, ast.Dict) and any(k is None for k in node.keys) \
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

    def test_the_shim_writes_no_handoff_state_itself(self):
        # The one place the shim names the handoff phase in a write is the
        # record migration, which takes the table verb through the engine.
        tree = ast.parse((ROOT / "compat.py").read_text())
        handoff = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.keyword)
                   and node.arg == "phase" and isinstance(node.value, ast.Constant)
                   and node.value.value == "handoff"]
        self.assertEqual(handoff, [])

    def test_every_legacy_write_names_a_registered_site(self):
        used = set()
        for path in pipeline_modules():
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ("legacy_update", "legacy_assign"):
                    site = node.args[1]
                    self.assertIsInstance(site, ast.Constant, f"{path.name}:{node.lineno}")
                    self.assertEqual(site.value.split(".")[0], path.stem)
                    used.add(site.value)
        self.assertEqual(used, set(compat.LEGACY_SITES))

    def test_legacy_states_are_exactly_the_states_outside_the_table(self):
        self.assertEqual(compat.LEGACY_STATES & set(lc.LIFECYCLE), set())
        self.assertFalse(any(key.startswith("handoff.") for key in compat.LEGACY_STATES))
        for key in ("development.idle", "development.waiting_capacity", "abandoned.idle"):
            self.assertIn(key, compat.LEGACY_STATES)
        for key in ("handoff.idle", "handoff.waiting_capacity", "closed.waiting_human"):
            self.assertIsNotNone(compat.state_problem(contribution(
                phase=key.split(".")[0], activity=key.split(".")[1])))

    def test_no_fork_write_is_admitted_at_the_submission_gate(self):
        args = dict(expected_revision=0, expected_head=SHA_A, observed_head=SHA_A,
                    ref="riscv64", repository="riseproject-upstreaming/proj")
        self.assertIsNone(lc.publication_check(contribution(activity="agent_running"), **args))
        self.assertEqual(lc.publication_check(awaiting_submission(), **args),
                         "state handoff.awaiting_submission does not permit fork writes")
        cleaning = awaiting_submission(activity="cleaning_debug", pending_human_action=None)
        self.assertIsNone(lc.publication_check(cleaning, **args))

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
