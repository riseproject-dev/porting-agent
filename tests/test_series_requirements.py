# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The series requirements of a plan (schema version 4) are checked on the
exact head before nomination; unmet ones are returned as development Work
(``return_for_feedback``), and the worker gate enforces them on
that Work."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collect
import commit_rules
import engine
import lifecycle
import schemas
import status_ledger
import work
import worker_gate
from tests import helpers, test_plan_gate
from tests.helpers import NOW, SHA_A, SHA_B, SHA_C, policy, take
from tests.test_development_table import final, kinds, record, tick
from tests.test_milestone5_worker import proposal, task

POLICY = policy()
ROOT = Path(__file__).resolve().parent.parent


def plan_evidence(*requirements, version=4):
    data = test_plan_gate.plan()
    data["schema_version"] = version
    data["contribution_guidelines"]["series_requirements"] = list(requirements)
    return {"schema_version": 1, "evidence_id": "plan-1", "contribution_id": "example--proj/12",
            "kind": "plan", "policy_version": POLICY["policy_version"], "collected_at": NOW,
            "summary": "the plan", "data": data}


CHANGELOG = test_plan_gate.changelog_requirement()
NEWS = test_plan_gate.changelog_requirement(id="news", paths=["changes/*.rst"])


def commit(sha, message):
    return {"sha": sha, "commit": {"message": message}}


class FakeGitHub:
    """The compare and commit reads of a series of the fork."""

    def __init__(self, commits, files, by_commit=None, ahead=None):
        self.commits, self.files, self.by_commit = commits, files, by_commit or {}
        self.ahead = len(commits) if ahead is None else ahead
        self.compared: list[tuple] = []
        self.read: list[str] = []

    def compare(self, _api, full_name, base, head):
        self.compared.append((full_name, base, head))
        return {"ahead_by": self.ahead, "behind_by": 0, "commits": self.commits,
                "files": [{"filename": f} for f in self.files]}

    def paginate(self, path, **_kwargs):
        # The (empty) steering lists of the development Work a return
        # allocates.
        found = helpers.steering_list(path)
        if found is None:
            raise collect.CollectError(f"GET {path}: not expected in this test")
        return found

    def get(self, path):
        sha = path.rsplit("/", 1)[1]
        self.read.append(sha)
        return {"sha": sha, "files": [{"filename": f} for f in self.by_commit[sha]]}


def observed(rec, gh, plan, **kw):
    with mock.patch("collect.compare_commits", side_effect=gh.compare):
        return tick(rec, verdict="passed", evidence={"plan-1": plan}, api=gh, **kw)


class TestObservation(unittest.TestCase):
    def test_an_unmet_requirement_returns_the_series_as_development_work(self):
        gh = FakeGitHub([commit(SHA_B, "Build riscv64 wheels")], ["setup.py"])
        rec = record()
        out = observed(rec, gh, plan_evidence(CHANGELOG, NEWS))
        self.assertEqual(kinds(out)[0], "return_for_feedback")
        self.assertEqual(gh.compared, [(rec["fork"]["name"], SHA_C, SHA_A)])
        [ev] = [e for kind, e in out.staged
                if kind == "evidence" and e["kind"] == "series_requirements"]
        schemas.validate("evidence", ev)
        self.assertEqual(ev["evidence_id"], f"series-requirements-2-{SHA_A[:12]}")
        self.assertEqual([u["id"] for u in ev["data"]["unmet"]], ["changelog", "news"])
        self.assertEqual(ev["digests"], {"head": SHA_A})
        self.assertIn("`changelog`: change `CHANGELOG.md`", ev["summary"])
        returned = next(e for kind, e in out.staged
                        if kind == "events" and e["kind"] == "return_for_feedback")
        self.assertEqual(returned["details"]["series_requirements_unmet"], ["changelog", "news"])
        self.assertTrue(returned["revision_bumped"])
        new = final(out, rec)
        self.assertIn(ev["evidence_id"], new["pending_feedback_evidence_ids"])
        self.assertEqual(new["revision"], rec["revision"] + 1)
        # Work carrying the requirements is requested in the same tick.
        self.assertEqual(kinds(out)[1:], ["request_patch"])
        self.assertEqual(new["active_work"]["task"], "development_repair")
        self.assertIn(ev["evidence_id"], new["active_work"]["feedback_evidence_ids"])
        self.assertIn(ev["evidence_id"], new["active_work"]["reason_ids"])

    def test_the_return_is_idempotent_for_the_same_head(self):
        gh = FakeGitHub([commit(SHA_B, "Build riscv64 wheels")], ["setup.py"])
        out = observed(record(), gh, plan_evidence(CHANGELOG))
        again = observed(record(), gh, plan_evidence(CHANGELOG))
        ids = [[e["evidence_id"] for kind, e in o.staged
                if kind == "evidence" and e["kind"] == "series_requirements"] for o in (out, again)]
        self.assertEqual(ids[0], ids[1])

    def test_a_met_requirement_nominates(self):
        gh = FakeGitHub([commit(SHA_B, "Build riscv64 wheels")], ["setup.py", "CHANGELOG.md"])
        out = observed(record(), gh, plan_evidence(CHANGELOG))
        self.assertEqual(kinds(out), ["nominate_for_handoff"])
        self.assertNotIn("series_requirements", out.staged[-1][1].get("details", {}))

    def test_a_plan_without_requirements_reads_no_series(self):
        # The behavior of every plan before version 4 is unchanged: no read.
        for plan in (plan_evidence(CHANGELOG, version=3), plan_evidence(),
                     plan_evidence(version=1)):
            gh = FakeGitHub([], [])
            out = observed(record(), gh, plan)
            self.assertEqual(kinds(out), ["nominate_for_handoff"])
            self.assertEqual(gh.compared, [])
        gh = FakeGitHub([], [])
        with mock.patch("collect.compare_commits", side_effect=gh.compare):
            out = tick(record(), verdict="passed", evidence={}, api=gh)
        self.assertEqual(kinds(out), ["nominate_for_handoff"])

    def test_a_series_too_large_to_read_is_nominated_unchecked(self):
        for gh in (FakeGitHub([commit(SHA_B, "x")], [f"f{i}" for i in range(300)]),
                   FakeGitHub([commit(SHA_B, "x")], ["setup.py"], ahead=2),
                   FakeGitHub([commit(f"{i:040x}", "x") for i in range(51)], ["setup.py"])):
            out = observed(record(), gh, plan_evidence(CHANGELOG))
            self.assertEqual(kinds(out), ["nominate_for_handoff"])
            event = next(e for kind, e in out.staged if kind == "events")
            self.assertEqual(event["details"]["series_requirements"],
                             "not checked: the series is too large to read")

    def test_pending_feedback_returns_for_feedback_first(self):
        gh = FakeGitHub([commit(SHA_B, "x")], ["setup.py"])
        out = observed(record(pending_feedback_evidence_ids=["review-1"]), gh,
                       plan_evidence(CHANGELOG))
        self.assertEqual(kinds(out)[0], "return_for_feedback")
        self.assertEqual(gh.compared, [])

    def test_a_path_changed_only_by_a_debug_commit_or_reverted_does_not_count(self):
        d1, d2 = "1" * 40, "2" * 40
        gh = FakeGitHub([commit(d1, "{debug} Lift the CI guard"), commit(d2, "Build riscv64")],
                        ["CHANGELOG.md", "setup.py"],
                        by_commit={d1: ["CHANGELOG.md"], d2: ["setup.py"]})
        out = observed(record(), gh, plan_evidence(CHANGELOG))
        self.assertEqual(kinds(out)[0], "return_for_feedback")
        self.assertEqual(gh.read, [d2])
        # A path changed by a non-debug commit and reverted later in the
        # series is not in the comparison, so it does not count either.
        gh = FakeGitHub([commit(d1, "{debug} Lift the CI guard"), commit(d2, "Build riscv64")],
                        ["setup.py"], by_commit={d1: [], d2: ["setup.py", "CHANGELOG.md"]})
        self.assertEqual(kinds(observed(record(), gh, plan_evidence(CHANGELOG)))[0],
                         "return_for_feedback")
        gh = FakeGitHub([commit(d1, "{debug} Lift the CI guard"), commit(d2, "Build riscv64")],
                        ["setup.py", "CHANGELOG.md"],
                        by_commit={d1: [], d2: ["setup.py", "CHANGELOG.md"]})
        self.assertEqual(kinds(observed(record(), gh, plan_evidence(CHANGELOG))),
                         ["nominate_for_handoff"])

    def test_a_failed_read_skips_the_contribution(self):
        def failing(*_a):
            raise collect.CollectError("GET compare: 502")
        rec = record()
        with mock.patch("collect.compare_commits", side_effect=failing):
            out = tick(rec, verdict="passed", evidence={"plan-1": plan_evidence(CHANGELOG)},
                       api=FakeGitHub([], []))
        self.assertNotIn("nominate_for_handoff", kinds(out))
        self.assertNotIn("return_for_feedback", kinds(out))
        self.assertEqual(engine.state_key(final(out, rec)), "development.waiting_ci")

    def test_a_nomination_needs_the_fact(self):
        out = take(
            record(), "nominate_for_handoff", policy=POLICY, now=NOW,
            facts={"ci_passed_on_head"},
            values={"last_ci_evidence_id": "ci-1",
                    "handoff_request": {"command_id": "c", "head": SHA_A, "revision": 0}})
        self.assertFalse(out.accepted)
        self.assertIn(lifecycle.SERIES_REQUIREMENTS_MET, out.reason)

    def test_the_ledger_renders_the_requirement_ids(self):
        gh = FakeGitHub([commit(SHA_B, "x")], ["setup.py"])
        out = observed(record(), gh, plan_evidence(CHANGELOG))
        event = next(e for kind, e in out.staged
                     if kind == "events" and e["kind"] == "return_for_feedback")
        body = status_ledger.render(dict(event, sequence=3), final(out, record()),
                                    "https://github.com/run")
        self.assertIn("`return_for_feedback`", body)
        self.assertIn("`changelog`", body)
        self.assertNotIn(CHANGELOG["description"], body)


def mbox(*commits) -> bytes:
    parts = []
    for i, (subject, paths) in enumerate(commits):
        diff = "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-a\n+b\n"
                       for p in paths)
        parts.append(f"From {i:040x} Mon Sep 17 00:00:00 2001\nFrom: App <app@example.com>\n"
                     f"Subject: [PATCH] {subject}\n\nBody.\n---\n{diff}")
    return "".join(parts).encode()


class TestWorkerGate(unittest.TestCase):
    def test_non_debug_paths_of_an_mbox(self):
        data = mbox(("{debug} Lift guard", ["CHANGELOG.md"]), ("Build riscv64", ["setup.py"]))
        self.assertEqual(commit_rules.mbox_non_debug_paths(data), ["setup.py"])

    def gate(self, data, requirements):
        value = task()
        value["allowed_paths"] = ["setup.py", "CHANGELOG.md"]
        value["context"] = {"series_requirements": requirements}
        prop = proposal(data)
        prop["changed_files"] = worker_gate._patch_paths(data.decode())
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "series.mbox"
            path.write_bytes(data)
            return worker_gate.validate(value, prop, path)

    def test_a_patch_that_misses_a_carried_requirement_is_rejected(self):
        reqs = [{"id": "changelog", "paths": ["CHANGELOG.md"]}]
        with self.assertRaisesRegex(worker_gate.WorkerGateError, "series requirements: changelog"):
            self.gate(mbox(("Build riscv64", ["setup.py"])), reqs)
        self.assertEqual(self.gate(mbox(("Build riscv64", ["setup.py", "CHANGELOG.md"])), reqs)
                         ["status"], "accepted")
        self.assertEqual(self.gate(mbox(("Build riscv64", ["setup.py"])), [])["status"],
                         "accepted")


class TestWorkPayload(unittest.TestCase):
    def evidence(self, head=SHA_A):
        return {"evidence_id": "series-requirements-2-x", "kind": "series_requirements",
                "summary": "s", "digests": {"head": head},
                "data": {"head": head, "unmet": [{"id": "changelog", "paths": ["CHANGELOG.md"],
                                                  "description": "d" * 10}]}}

    def test_the_payload_carries_requirements_of_the_current_head_only(self):
        rec = record(pending_feedback_evidence_ids=["series-requirements-2-x"])
        rec["active_work"] = {"feedback_evidence_ids": ["series-requirements-2-x"]}
        store = {"series-requirements-2-x": self.evidence()}
        self.assertEqual(work.series_requirements_context(rec, store),
                         [{"id": "changelog", "paths": ["CHANGELOG.md"]}])
        self.assertIsNone(work.series_requirements_context(
            rec, {"series-requirements-2-x": self.evidence(head=SHA_B)}))
        self.assertIsNone(work.series_requirements_context(dict(rec, active_work={}), store))
        item = work._reason_item(self.evidence())
        self.assertEqual(item["data"], {"head": SHA_A,
                                        "unmet": [{"id": "changelog", "paths": ["CHANGELOG.md"]}]})

    def test_the_largest_requirements_fit_the_payload(self):
        largest = [{"id": "r" + "x" * 39, "paths": ["p" * 120] * 3} for _ in range(6)]
        self.assertLess(len(json.dumps(largest, separators=(",", ":"))), 2600)


class TestLiveRecord(unittest.TestCase):
    """The live contribution at the submission gate needs at most the field
    migration that adds the held patch of a development review (the
    controller applies it on its first tick, so a migrated record needs
    none), and records no series requirement."""

    def test_the_live_record_is_unchanged(self):
        path = ROOT / ".state/contributions/closeio--ciso8601--1.json"
        if not path.exists():
            self.skipTest("no live record in this checkout")
        rec = json.loads(path.read_text())
        added, removed = engine.with_defaults(copy.deepcopy(rec))[1:]
        self.assertLessEqual(set(added), {"held_patch", "review_comments"})
        self.assertEqual(removed, [])
        self.assertEqual(engine.state_key(rec), "handoff.awaiting_submission")
        plan = json.loads((ROOT / ".state/evidence" / f"{rec['plan_evidence_id']}.json").read_text())
        self.assertEqual(commit_rules.series_requirements_from_plan(plan["data"]), [])


class TestQualitativeOnlyPlan(unittest.TestCase):
    def test_qualitative_items_stay_with_the_submission_review(self):
        gh = FakeGitHub([], [])
        out = observed(record(), gh, plan_evidence())
        self.assertEqual(kinds(out), ["nominate_for_handoff"])
        edge = lifecycle.find_edge("handoff.reviewing", "reject_submission_review")
        self.assertEqual(edge.target, "development.fix_needed")
        self.assertIn("pending_feedback_evidence_ids", edge.transition.effects.sets)


if __name__ == "__main__":
    unittest.main()

