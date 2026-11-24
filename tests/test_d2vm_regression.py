# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The stall of luhenry--d2vm/3, replayed from the live record and its
worker report (``tests/fixtures/live-d2vm``).

The publication of revision 3 widened the patch scope past the bound and
the fold crashed (`SchemaError`), so the controller paused the
contribution in `development.fix_needed`. While it was paused the moved
head was recorded (`record_head_change`, an observation the mask did not
refuse), which bumped the revision and entered `development.waiting_ci`
without a development PR and without a published artifact; the planner
skipped such a record forever. The record is committed in that shape:
paused by the controller, `development.waiting_ci`, revision 4, no PR.

What stops each step now: the widening keeps the scope within its bound,
the mask of the flag refuses the head change while paused, the CI wait
requires the development PR, the controller resumes its own stop once the
pipeline code changed, and a record that breaks its state takes the row
that heals it, here the existing wait on a development PR that does not
match, which the human resume leaves for `development.fix_needed`, where
the PR of the head is opened from the stored report.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import capacity
import commands
import common
import controller
import engine
import lifecycle as lc
import reconcile
import status_ledger
from tests import helpers
from tests.helpers import OWNER, apply_command, policy
from tests.test_late_results import ci_snapshot

FIXTURES = Path(__file__).parent / "fixtures/live-d2vm"
POLICY = policy()
T = datetime.fromisoformat("2026-11-24T08:00:00+00:00")
VERSION = "a" * 64
CID = "luhenry--d2vm/3"


def live() -> dict:
    return json.loads((FIXTURES / "contribution.json").read_text())


def report() -> dict:
    return json.loads((FIXTURES / "report.json").read_text())


HEAD = live()["fork"]["head_sha"]
BASE = live()["upstream"]["base_sha"]


def tick(rec: dict, *, now=T, pipeline=VERSION, observe=False, head=HEAD, operations=None):
    snapshot = reconcile.Snapshot({"c": copy.deepcopy(rec)}, dict(operations or {}), {}, {},
                                  {report()["evidence_id"]: report()})
    with mock.patch("observe_ci.collect.branch_head", return_value={"head": head}), \
            mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
            mock.patch("observe_ci.collect.ci_snapshot", return_value=ci_snapshot(head, "pending")), \
            contextlib.redirect_stderr(io.StringIO()):
        return reconcile.tick(helpers.NoReadApi() if observe else None, snapshot, policy=POLICY,
                              now=now, publications=[], pressure=capacity.Pressure(ok=True),
                              observe=observe, pipeline=pipeline)


def final(output, rec: dict) -> dict:
    staged = [r for kind, r in output.staged if kind == "contributions"]
    return staged[-1] if staged else rec


def kinds(output) -> list[str]:
    return [e["kind"] for kind, e in output.staged if kind == "events"]


class TestTheCrashIsGone(unittest.TestCase):
    def test_a_widening_past_the_bound_keeps_the_scope_and_says_why(self):
        scope = live()["scope_paths"]
        many = [f"pkg/new/file{i}.go" for i in range(common.SCOPE_PATHS_MAX)]
        kept, added, why = reconcile.extended_scope(scope, many)
        self.assertEqual((kept, added), (scope, []))
        self.assertIn("did not join the scope", why)
        # The files the report of revision 3 changed widen it within the bound.
        widened, added, why = reconcile.extended_scope(scope, report()["data"]["changed_files"])
        self.assertIsNone(why)
        self.assertLessEqual(len(widened), common.SCOPE_PATHS_MAX)
        self.assertEqual(widened[:len(scope)], scope)


class TestThePauseHoldsTheHead(unittest.TestCase):
    def paused_before_the_head_change(self) -> dict:
        """The record as the controller paused it: `development.fix_needed`
        at revision 3, on the base, without a development PR."""
        rec = live()
        rec.update(activity="fix_needed", revision=3)
        rec["fork"] = dict(rec["fork"], head_sha=BASE)
        return rec

    def test_the_head_change_is_refused_while_paused_and_changes_nothing(self):
        rec = self.paused_before_the_head_change()
        before = copy.deepcopy(rec)
        kwargs = {"policy": POLICY, "now": T.isoformat(), "facts": {"fork_head_changed"},
                  "values": {"fork.head_sha": HEAD}}
        checked = engine.preconditions(rec, "record_head_change", **kwargs)
        self.assertEqual(checked.kind, engine.Refusal.PAUSED)
        out = engine.try_apply_transition(rec, "record_head_change", **kwargs)
        self.assertEqual(out, checked)
        self.assertEqual(rec, before)

    def test_after_the_resume_the_head_gets_its_pr_before_ci_is_awaited(self):
        rec = self.paused_before_the_head_change()
        resumed = engine.try_apply_transition(rec, "resume_pause", policy=POLICY,
                                              now=T.isoformat(),
                                              facts={"pipeline_changed_since_pause"})
        self.assertIsInstance(resumed, engine.Applied)
        self.assertFalse(resumed.record["paused"])
        self.assertEqual(resumed.record["revision"], 4)
        # The CI wait requires the development PR: the head change is not
        # taken without one ...
        moved = engine.try_apply_transition(
            resumed.record, "record_head_change", policy=POLICY, now=T.isoformat(),
            facts={"fork_head_changed"}, values={"fork.head_sha": HEAD})
        self.assertEqual((moved.kind, moved.guard),
                         (engine.Refusal.GUARD_UNMET, lc.DEVELOPMENT_PR_RECORDED))
        # ... and the controller opens the PR of the observed head first,
        # from the report of the Work that pushed it.
        self.assertEqual(controller.published_head_without_pr(resumed.record, {}, HEAD), HEAD)
        reasons, digest = controller.development_pr_reasons(
            resumed.record, HEAD, {report()["evidence_id"]: report()})
        self.assertEqual(reasons, [rec["plan_evidence_id"], report()["evidence_id"]])
        self.assertEqual(digest, report()["digests"]["report"])

    def test_a_head_on_the_base_opens_no_pr(self):
        rec = self.paused_before_the_head_change()
        self.assertIsNone(controller.published_head_without_pr(rec, {}, BASE))


class TestTheLiveRecordHeals(unittest.TestCase):
    def test_the_committed_record_breaks_the_ci_wait(self):
        rec = live()
        self.assertEqual((engine.state_key(rec), rec["revision"], rec["paused"],
                          rec["paused_by"], rec["fork"]["pr_number"]),
                         ("development.waiting_ci", 4, True, "controller", None))
        self.assertEqual(engine.invariant_problems(rec),
                         [("dev_pr", "no development PR and head are recorded")])
        self.assertNotIn("paused_at_commit", rec)

    def test_the_first_tick_resumes_and_heals_without_a_hand_edit(self):
        out = tick(live())
        self.assertEqual(kinds(out), ["resume_pause", "record_pr_mismatch"])
        rec = final(out, live())
        self.assertEqual((engine.state_key(rec), rec["revision"], rec["paused"]),
                         ("development.waiting_human", 5, False))
        self.assertEqual(rec["pending_human_action"]["kind"], "development_pr_mismatch")
        self.assertEqual(engine.invariant_problems(rec), [])
        self.assertEqual(out.report["resumed_after_error"], [{"contribution": CID, "revision": 5}])
        [violation] = out.report["invariant_violations"]
        self.assertEqual((violation["invariant"], violation["heal"]),
                         ("dev_pr", "record_pr_mismatch"))
        # The tracking issue explains the wait in plain words.
        [event] = [e for k, e in out.staged if k == "events" and e["kind"] == "record_pr_mismatch"]
        self.assertEqual(event["details"]["invariant"], "dev_pr")
        text = status_ledger.render(event, rec, "https://github.com/o/r/actions/runs/1")
        self.assertIn("CI is awaited only on a development PR", text)
        self.assertIn("`/upstreaming resume`", text)
        [resume] = [e for k, e in out.staged if k == "events" and e["kind"] == "resume_pause"]
        self.assertIn("since the pipeline code changed",
                      status_ledger.render(resume, rec, "https://github.com/o/r/actions/runs/1"))
        # The next tick changes nothing.
        again = tick(rec, now=T + timedelta(minutes=10))
        self.assertEqual(kinds(again), [])
        self.assertNotIn("invariant_violations", again.report)

    def test_the_resume_opens_the_development_pr_of_the_head(self):
        rec = final(tick(live()), live())
        resumed = apply_command(rec, commands.parse(
            f"/upstreaming resume contribution={CID} revision=5 reason=open-the-pr"),
            actor=OWNER, now=T.isoformat())
        self.assertTrue(resumed.accepted, resumed.reason)
        self.assertEqual((resumed.verb, engine.state_key(resumed.record)),
                         ("resume_development", "development.fix_needed"))
        out = tick(resumed.record, now=T + timedelta(minutes=20), observe=True)
        self.assertEqual(kinds(out), ["launch_development_pr"])
        [request] = out.report["dev_pr_requests"]
        [op] = [r for k, r in out.staged if k == "operations"]
        self.assertEqual((op["kind"], op["target"]["expected_head"]), ("dev_pr_create", HEAD))
        self.assertIn(report()["evidence_id"], op["reason_ids"])
        self.assertEqual(request["operation_id"], op["operation_id"])
        # Its verified receipt enters the CI wait with the PR recorded.
        held = final(out, resumed.record)
        confirmed = engine.try_apply_transition(
            dict(held, active_operation_id=None), "confirm_development_pr", policy=POLICY,
            now=T.isoformat(), facts={"receipt_matches_operation", "development_pr_read_back"},
            values={"fork.pr_number": 3})
        self.assertEqual(engine.state_key(confirmed.record), "development.waiting_ci")
        self.assertEqual(engine.invariant_problems(confirmed.record), [])


class TestTheAutomaticResume(unittest.TestCase):
    def test_once_per_change_of_the_pipeline_code(self):
        rec = dict(live(), paused_at_commit=VERSION)
        self.assertEqual(kinds(tick(rec)), [])
        # The stop of another version, or of a human, is not resumed by the
        # same code twice.
        out = tick(rec, pipeline="b" * 64)
        self.assertEqual(kinds(out)[0], "resume_pause")
        human = dict(live(), paused_by="human")
        self.assertEqual(kinds(tick(human)), [])
        # Without a version nothing is resumed.
        self.assertEqual(kinds(tick(live(), pipeline=None)), [])

    def test_a_retry_that_fails_again_stops_at_the_new_version_and_does_not_loop(self):
        def broken(rec, *a, **k):
            raise RuntimeError("still broken")

        old, new = "a" * 64, "b" * 64
        first = dict(live(), paused_at_commit=old)
        second = dict(copy.deepcopy(first), contribution_id="luhenry--d2vm/4")
        for names in (("c",), ("c", "d")):
            records = {"c": first, "d": second}
            snapshot = lambda: reconcile.Snapshot(
                {k: copy.deepcopy(records[k]) for k in names}, {}, {}, {},
                {report()["evidence_id"]: report()})
            with mock.patch("reconcile._heal_invariant", broken), \
                    contextlib.redirect_stderr(io.StringIO()):
                out = reconcile.tick(None, snapshot(), policy=POLICY, now=T, publications=[],
                                     pressure=capacity.Pressure(ok=True), observe=False,
                                     pipeline=new)
                committed = {r["contribution_id"]: r for kind, r in out.staged
                             if kind == "contributions"}
                self.assertEqual(len(committed), len(names))
                self.assertTrue(all(r["paused"] and r["paused_at_commit"] == new
                                    for r in committed.values()), names)
                # The next tick, on the same code, resumes neither and stops nothing.
                again = reconcile.Snapshot(
                    {k: r for k, r in zip(names, committed.values())}, {}, {}, {},
                    {report()["evidence_id"]: report()})
                out = reconcile.tick(None, again, policy=POLICY, now=T, publications=[],
                                     pressure=capacity.Pressure(ok=True), observe=False,
                                     pipeline=new)
                self.assertEqual(kinds(out), [], names)

    def test_a_human_pause_takes_the_stop_over(self):
        out = apply_command(live(), commands.parse(
            f"/upstreaming pause contribution={CID} revision=4 reason=hold"), actor=OWNER,
            now=T.isoformat())
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual((out.record["paused_by"], out.record["paused_at_commit"]),
                         ("human", None))
        self.assertEqual(kinds(tick(out.record)), [])

    def test_the_check_refuses_a_variant_that_is_not_the_same_clear(self):
        from dataclasses import replace
        flag = lc.FLAGS["paused"]
        variant = flag.variants["resume_pause"]
        broken = replace(variant, effects=replace(variant.effects, revision="keep"))
        with mock.patch.dict(flag.variants, {"resume_pause": broken}):
            problems = lc.check_table(POLICY)
        self.assertIn("flag paused resume_pause (tick): a variant must have the effects of "
                      "resume_pause", problems)


if __name__ == "__main__":
    unittest.main()
