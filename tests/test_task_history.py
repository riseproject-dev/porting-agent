# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The history every task carries, and how the tick records it."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import collect
import isolation
import state
import task_build
import task_file
import task_history
import tasks
from common import canonical_json
from tests import test_development_table as dev
from tests.helpers import NOW, SHA_A, SHA_B, ci_record, command, contribution, policy
from tests.test_worker_report import report

POLICY = policy()
CID = "example--proj/12"
WORK = {"dedupe_key": f"{CID}@g0.r4:development_repair", "task": "development_repair",
        "generation": 0, "revision": 4}


def report_evidence(eid, *, at, head_before=SHA_A, head_after=SHA_B, generation=0,
                    **fields):
    return {"schema_version": 1, "evidence_id": eid, "contribution_id": CID,
            "kind": "worker_report", "policy_version": "p", "collected_at": at,
            "summary": "s", "data": {
                "work": {"dedupe_key": f"{CID}@g{generation}.r1:development_patch",
                         "generation": generation, "revision": 1, "task": "development_patch",
                         "worker_run_id": "1"},
                "verb": "record_publish_success", "head_before": head_before,
                "head_after": head_after, "changed_files": ["a"],
                "report": report(**fields)}}


def ci_evidence(eid, head, *, at, failing="Build riscv64"):
    record = ci_record(eid, head, failing=failing)
    record["collected_at"] = at
    record["contribution_id"] = CID
    return record


def steering_evidence(eid, items, *, at):
    return {"schema_version": 1, "evidence_id": eid, "contribution_id": CID,
            "kind": "human_steering", "policy_version": "p", "collected_at": at,
            "summary": "s", "data": {"items": items, "omitted": {"tracking_issue_comment": 1},
                                     "deferred": 0}}


def steer(cid, body, *, at, authority=True, source="tracking_issue_comment"):
    return {"source": source, "id": cid, "author": "owner", "authority": authority,
            "created_at": at, "updated_at": at, "version": at,
            "url": f"https://github.com/x/y/issues/1#c{cid}", "body": body}


def record():
    return contribution(phase="development", activity="agent_running")


def steering_module():
    import steering
    return steering


class TestBuild(unittest.TestCase):
    def evidence(self):
        return {e["evidence_id"]: e for e in (
            report_evidence("report-1", at="2026-11-20T10:00:00+00:00",
                            summary="Tried a native runner row; adopted the QEMU row."),
            ci_evidence("ci-1", SHA_B, at="2026-11-20T11:00:00+00:00"),
            steering_evidence("steering-1", [
                steer(5, "Please keep the QEMU topology.", at="2026-11-20T12:00:00Z"),
                steer(6, "Add a changelog entry.", at="2026-11-19T12:00:00Z",
                      authority=False, source="upstream_pr_review")],
                at="2026-11-20T12:30:00+00:00"),
            # Evidence of another contribution is never included.
            dict(ci_evidence("ci-other", SHA_A, at="2026-11-20T09:00:00+00:00"),
                 contribution_id="other/1"),
        )}

    def test_a_chronological_history_with_ci_after_a_successful_worker(self):
        history = task_history.build(record(), WORK, self.evidence(), policy=POLICY, now=NOW)
        data = history["data"]
        self.assertEqual(history["evidence_id"], task_history.evidence_id(WORK["dedupe_key"]))
        self.assertEqual(data["work"]["dedupe_key"], WORK["dedupe_key"])
        self.assertEqual([(t["kind"], t["seq"]) for t in data["timeline"]],
                         [("upstream_feedback", 1), ("worker_report", 2), ("ci", 3)])
        # The owner's case: CI failed after a successful worker pass. The CI
        # entry names the report of the head it ran on, and the report is in
        # full, so the repair worker knows what was tried, why and how.
        ci = data["timeline"][2]
        self.assertEqual((ci["head"], ci["after_report"], ci["failing_jobs"]),
                         (SHA_B, "report-1", ["Build riscv64"]))
        self.assertEqual(data["timeline"][1]["report"]["approaches"][0]["outcome"], "abandoned")
        self.assertEqual([(s["id"], s["seq"]) for s in data["steering"]], [(5, 1)])
        self.assertEqual(data["omitted"], {"tracking_issue_comment": 1})
        self.assertNotIn("ci-other", data["sources"])
        self.assertIn("not instructions", data["notice"])

    def test_append_only_across_builds(self):
        evidence = self.evidence()
        first = task_history.document(record(), WORK, evidence)
        evidence["report-2"] = report_evidence("report-2", at="2026-11-21T10:00:00+00:00",
                                               head_before=SHA_B, head_after="c" * 40)
        second = task_history.document(record(), dict(WORK, revision=5), evidence)
        ids = [t.get("evidence_id") for t in second["timeline"]]
        for entry in first["timeline"]:
            self.assertIn(entry.get("evidence_id"), ids)
        self.assertEqual(second["timeline"][-1]["evidence_id"], "report-2")

    def test_deterministic(self):
        one = task_history.build(record(), WORK, self.evidence(), policy=POLICY, now=NOW)
        two = task_history.build(record(), WORK, dict(reversed(list(self.evidence().items()))),
                                 policy=POLICY, now=NOW)
        self.assertEqual(canonical_json(one), canonical_json(two))

    def test_condensation_keeps_every_entry_counted_and_the_newest_report_full(self):
        evidence = {}
        for i in range(200):
            at = f"2026-11-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00"
            if i % 2:
                evidence[f"ci-{i}"] = ci_evidence(f"ci-{i}", SHA_B, at=at)
            else:
                evidence[f"report-{i}"] = report_evidence(
                    f"report-{i}", at=at, summary=f"attempt {i}: " + "w" * 1000,
                    approaches=[{"title": f"approach {i}", "description": "d" * 1900,
                                 "outcome": "failed", "evidence": "e" * 1900,
                                 "lesson": "l" * 500}])
        evidence["steering-1"] = steering_evidence("steering-1", [
            steer(i, "s" * 3000, at=f"2026-11-0{1 + i % 9}T00:00:00Z") for i in range(40)],
            at="2026-11-09T00:00:00+00:00")
        history = task_history.build(record(), WORK, evidence, policy=POLICY, now=NOW)
        data = history["data"]
        self.assertLessEqual(len(canonical_json(data).encode()), task_history.HISTORY_MAX_BYTES)
        newest = max((t for t in data["timeline"] if t["kind"] == "worker_report"),
                     key=lambda t: t["at"])
        self.assertEqual(newest["evidence_id"], "report-198")
        self.assertNotIn("condensed", newest)
        self.assertEqual(len(newest["report"]["approaches"][0]["description"]), 1900)
        earlier = data["timeline"][0]
        self.assertEqual(earlier["kind"], "earlier")
        unfolded = len([t for t in data["timeline"] if t["kind"] != "earlier"])
        self.assertEqual(earlier["entries"] + unfolded, 200)
        self.assertEqual(sum(earlier["counts"].values()), earlier["entries"])
        self.assertTrue(earlier["failed_approaches"])
        self.assertEqual(data["condensed"]["folded"], earlier["entries"])
        condensed = [t for t in data["timeline"] if t.get("condensed") and t["kind"] != "earlier"]
        for entry in condensed:
            self.assertEqual(entry["evidence_path"],
                             f".state/evidence/{entry['evidence_id']}.json")
        # The steering is not folded: the oldest items beyond the reserve
        # are cut short, and the newest keep their whole text.
        self.assertEqual(len(data["steering"]), 40)
        self.assertTrue(data["steering"][0].get("condensed"))
        self.assertNotIn("condensed", data["steering"][-1])
        self.assertEqual(len(data["steering"][-1]["body"]), 3000)
        # Determinism of the condensed form.
        again = task_history.build(record(), WORK, copy.deepcopy(evidence), policy=POLICY,
                                   now=NOW)
        self.assertEqual(canonical_json(again), canonical_json(history))

    def test_a_failed_approach_whose_title_has_a_colon_is_listed_when_folded(self):
        evidence = {}
        for i in range(60):
            evidence[f"report-{i}"] = report_evidence(
                f"report-{i}", at=f"2026-11-{1 + i // 24:02d}T{i % 24:02d}:00:00+00:00",
                summary="w" * 1000, approaches=[{
                    "title": f"fix: use foo {i}", "description": "d" * 1900,
                    "outcome": "failed" if i % 2 else "adopted", "evidence": "e" * 1900,
                    "lesson": "l" * 500}])
        data = task_history.document(record(), WORK, evidence)
        earlier = data["timeline"][0]
        self.assertEqual(earlier["kind"], "earlier")
        folded = [t for t in range(earlier["entries"]) if t % 2]
        self.assertEqual(earlier["failed_approaches"],
                         [{"title": f"fix: use foo {i}", "outcome": "failed"} for i in folded])
        short = next(t for t in data["timeline"] if t.get("condensed") and t["kind"] ==
                     "worker_report")
        self.assertEqual(set(short["approaches"][0]), {"title", "outcome", "lesson"})

    def test_steering_replaces_only_the_feedback_of_the_same_kind_and_id(self):
        relay = {"schema_version": 1, "evidence_id": "relay-1", "contribution_id": CID,
                 "kind": "human_relay", "policy_version": "p",
                 "collected_at": "2026-11-21T00:00:00+00:00", "summary": "s", "data": {
                     "reviews": [], "conversation_comments": [],
                     "comments": [{"id": 5, "reviewer": "owner", "url": "u",
                                   "body": "Rename the job."}]}}
        # Steering comment 5 is a tracking issue comment, not review comment 5.
        evidence = {"relay-1": relay, "steering-1": steering_evidence(
            "steering-1", [steer(5, "Keep QEMU.", at="2026-11-20T00:00:00Z")],
            at="2026-11-20T00:00:00+00:00")}
        [entry] = task_history.document(record(), WORK, evidence)["timeline"]
        self.assertEqual(entry["items"][0]["body"], "Rename the job.")
        evidence["steering-2"] = steering_evidence(
            "steering-2", [steer(5, "Rename the job.", at="2026-11-21T00:00:00Z",
                                 source="development_pr_review_comment")],
            at="2026-11-21T00:00:00+00:00")
        [entry] = task_history.document(record(), WORK, evidence)["timeline"]
        self.assertEqual(entry["items"][0]["body"], "(in the steering section)")

    def test_heavy_steering_never_takes_the_place_of_the_newest_report(self):
        """The report of the worker whose head failed CI reaches the repair
        whole, with its CI run linked, however much the owner wrote: the
        oldest steering items are folded first."""
        for count in (100, 400):
            with self.subTest(steering=count):
                evidence = {"report-1": report_evidence(
                    "report-1", at="2026-11-20T00:00:00+00:00",
                    summary="Pinned the toolchain " + "x" * 900),
                    "ci-1": ci_evidence("ci-1", SHA_B, at="2026-11-20T01:00:00+00:00")}
                for i in range(count):
                    eid = f"steering-{i:04d}"
                    evidence[eid] = steering_evidence(eid, [steer(
                        i, f"steer {i} " + "s" * 400, at=f"2026-11-19T{i // 60 % 24:02d}:"
                        f"{i % 60:02d}:00Z", source="development_pr_review_comment")],
                        at="2026-11-19T00:00:00+00:00")
                doc = task_history.document(record(), WORK, evidence)
                kinds = [t["kind"] for t in doc["timeline"]]
                self.assertEqual(kinds, ["worker_report", "ci"])
                [full, ci] = doc["timeline"]
                self.assertNotIn("condensed", full)
                self.assertTrue(full["report"]["summary"].startswith("Pinned the toolchain"))
                self.assertEqual(ci["after_report"], "report-1")
                self.assertGreater(doc["steering_earlier"]["items"], 0)
                self.assertEqual(doc["steering_earlier"]["items"] + len(doc["steering"]), count)
                self.assertEqual(doc["steering"][-1]["id"], count - 1)

    def test_review_feedback_beyond_the_bound_is_counted(self):
        relay = {"schema_version": 1, "evidence_id": "relay-1", "contribution_id": CID,
                 "kind": "human_relay", "policy_version": "p",
                 "collected_at": "2026-11-21T00:00:00+00:00", "summary": "s", "data": {
                     "reviews": [], "conversation_comments": [],
                     "comments": [{"id": i, "reviewer": "owner", "url": "u",
                                   "body": f"comment {i}"} for i in range(70)]}}
        [entry] = task_history.document(record(), WORK, {"relay-1": relay})["timeline"]
        self.assertEqual(len(entry["items"]), 60)
        self.assertEqual(entry["items_not_listed"], 10)
        self.assertEqual(entry["items"][-1]["body"], "comment 69")
        # The short form keeps the 20 newest items, and counts the rest.
        short = task_history._short(entry)
        self.assertEqual((len(short["items"]), short["items_not_listed"]), (20, 50))
        self.assertEqual(short["items"][-1]["body"], "comment 69")

    def test_omitted_counts_are_those_of_the_newest_collection(self):
        evidence = {"steering-1": steering_evidence("steering-1", [], at="2026-11-20T00:00:00Z"),
                    "steering-2": steering_evidence("steering-2", [], at="2026-11-21T00:00:00Z")}
        evidence["steering-2"]["data"]["omitted"] = {"tracking_issue_comment": 3}
        data = task_history.document(record(), WORK, evidence)
        self.assertEqual(data["omitted"], {"tracking_issue_comment": 3})

    def test_deferred_and_unreadable_steering_are_stated(self):
        history = task_history.build(record(), WORK, self.evidence(), policy=POLICY, now=NOW,
                                     deferred=4, unreadable=["upstream_pr_review"])
        data = history["data"]
        self.assertEqual(data["omitted"]["steering_deferred"], 4)
        self.assertEqual(data["steering_unreadable"], ["upstream_pr_review"])
        self.assertTrue(any("upstream_pr_review" in v for v in history["limitations"]))
        plain = task_history.build(record(), WORK, self.evidence(), policy=POLICY, now=NOW)
        self.assertNotIn("steering_unreadable", plain["data"])
        self.assertEqual(plain["limitations"], [task_history.NOTICE])

    def test_sources_left_out_are_counted(self):
        evidence = {f"ci-{i:03d}": ci_evidence(f"ci-{i:03d}", SHA_A, at=f"2026-11-01T00:{i % 60:02d}:00Z")
                    for i in range(task_history.MAX_SOURCES + 7)}
        data = task_history.document(record(), WORK, evidence)
        self.assertEqual(len(data["sources"]), task_history.MAX_SOURCES)
        self.assertEqual(data["sources_not_listed"], 7)

    def test_condensation_serializes_the_whole_document_rarely(self):
        evidence = {}
        for i in range(600):
            evidence[f"report-{i}"] = report_evidence(
                f"report-{i}", at=f"2026-{1 + i // 300:02d}-{1 + (i // 24) % 12:02d}T"
                f"{i % 24:02d}:00:00+00:00", summary="w" * 900)
        real = task_history._size
        whole = []

        def counting(value):
            if isinstance(value, dict) and "timeline" in value:
                whole.append(1)
            return real(value)

        with mock.patch("task_history._size", side_effect=counting):
            data = task_history.document(record(), WORK, evidence)
        self.assertGreater(data["condensed"]["folded"], 100)
        self.assertLess(len(whole), 20)
        self.assertLessEqual(len(canonical_json(data).encode()), task_history.HISTORY_MAX_BYTES)

    def test_a_condensation_cut_never_creates_a_credential_match(self):
        # Each body is clean on its own: the AWS key id is followed by more
        # capitals, so it has no word boundary. The condensation cut at
        # SHORT characters falls right after its 16th character.
        straddling = "x" * (task_history.SHORT - 21) + " AKIA" + "B" * 30 + " " + "y" * 3500
        self.assertFalse(state.find_secrets(straddling))
        items = [steer(i, straddling, at=f"2026-11-{1 + i % 9:02d}T00:00:{i:02d}Z",
                       authority=i % 2 == 0,
                       source="tracking_issue_comment" if i % 2 == 0 else "upstream_pr_review")
                 for i in range(40)]
        evidence = {"steering-1": steering_evidence("steering-1", items,
                                                    at="2026-11-10T00:00:00+00:00")}
        reason = {"title": "t", "description": "d", "outcome": "failed",
                  "evidence": straddling[:1900], "lesson": "l"}
        for i in range(4):
            evidence[f"report-{i}"] = report_evidence(
                f"report-{i}", at=f"2026-11-0{1 + i}T05:00:00+00:00",
                summary=straddling[:1900], approaches=[reason])
        history = task_history.build(record(), WORK, evidence, policy=POLICY, now=NOW)
        condensed = history["data"]["condensed"]
        self.assertTrue(condensed["timeline_short"] and condensed["steering_short"])
        path = f".state/evidence/{history['evidence_id']}.json"
        self.assertEqual(state.check_blob(path, canonical_json(history).encode(), POLICY), [])

    def test_a_record_that_still_matches_after_redaction_raises(self):
        evidence = {"report-1": report_evidence("report-1", at="2026-11-20T10:00:00+00:00")}
        with mock.patch("worker_report.redact_tree", side_effect=lambda value: value), \
                mock.patch("task_history.document",
                           return_value=dict(task_history.document(record(), WORK, evidence),
                                             notice="AKIA" + "B" * 16)):
            with self.assertRaises(ValueError):
                task_history.build(record(), WORK, evidence, policy=POLICY, now=NOW)

    def test_ci_observed_in_the_tick_that_folds_the_report_is_linked_to_it(self):
        # The tick folds the publication and observes CI on the new head
        # with the same time; "ci-" sorts before "report-" by id.
        at = "2026-11-20T10:00:00+00:00"
        evidence = {"report-1": report_evidence("report-1", at=at, head_after=SHA_B),
                    "ci-1": ci_evidence("ci-1", SHA_B, at=at)}
        timeline = task_history.document(record(), WORK, evidence)["timeline"]
        self.assertEqual([(t["kind"], t["seq"]) for t in timeline],
                         [("worker_report", 1), ("ci", 2)])
        self.assertEqual(timeline[1]["after_report"], "report-1")

    def test_ci_is_linked_to_its_report_whatever_their_order(self):
        evidence = {"report-1": report_evidence("report-1", at="2026-11-20T10:00:00+00:00",
                                                head_after=SHA_B),
                    "ci-1": ci_evidence("ci-1", SHA_B, at="2026-11-20T09:00:00+00:00"),
                    "ci-2": ci_evidence("ci-2", SHA_A, at="2026-11-20T11:00:00+00:00")}
        timeline = task_history.document(record(), WORK, evidence)["timeline"]
        links = {t["evidence_id"]: t.get("after_report") for t in timeline if t["kind"] == "ci"}
        self.assertEqual(links, {"ci-1": "report-1", "ci-2": None})

    def test_a_small_history_is_not_condensed(self):
        data = task_history.build(record(), WORK, self.evidence(), policy=POLICY,
                                  now=NOW)["data"]
        self.assertEqual(data["condensed"], {"timeline_short": 0, "steering_short": 0,
                                             "folded": 0})


def _big_report(char="x", scale=1.0, outcome="failed"):
    """The fields of a report near the schema's limits (``scale`` 1 is
    about 32 KiB of ASCII)."""
    def n(value):
        return int(value * scale)
    return dict(summary=char * n(1000),
                attempted=[{"what": char * n(300), "why": char * n(400)}] * 8,
                approaches=[{"title": char * n(150), "description": char * n(600),
                             "outcome": outcome, "evidence": char * n(600),
                             "lesson": char * n(250)}] * 10,
                failures=[{"what": char * n(300), "evidence": char * n(500)}] * 8,
                open_items=[char * n(300)] * 10,
                verification=[{"check": char * n(300), "result": "passed",
                               "evidence": char * n(300)}] * 12,
                revisited=[{"history_entry": char * n(150), "new_evidence": char * n(500)}] * 6)


def _sha(i):
    return f"{i:040x}"


class TestCondensationOrder(unittest.TestCase):
    """What is protected when the history does not fit: the newest report
    with the newest linked CI run, then the owner's steering; maintainer
    feedback, further CI runs and older entries give way first. Every case
    was a failure of an earlier condensation order."""

    def reports(self, count, *, scale=0.7, char="x", prefix="report-example--proj--12-r",
                steering=0, length=500, ci=True):
        evidence = {}
        for i in range(count):
            at = f"2026-10-{1 + i // 3:02d}T{i % 3 + 10}:00:00+00:00"
            eid = f"{prefix}{i}-449829025{i:02d}"
            evidence[eid] = report_evidence(eid, at=at, head_before=_sha(i), head_after=_sha(i + 1),
                                            **_big_report(char, scale))
            if ci:
                cid = f"ci-1467208233-449849431{i:02d}-1"
                evidence[cid] = ci_evidence(cid, _sha(i + 1),
                                            at=f"2026-10-{1 + i // 3:02d}T{i % 3 + 10}:30:00+00:00")
        if steering:
            evidence["steering-a"] = steering_evidence("steering-a", [
                steer(i, "s" * length, at=f"2026-09-01T00:{i // 60:02d}:{i % 60:02d}Z")
                for i in range(steering)], at="2026-11-01T00:00:00+00:00")
        return evidence

    def maintainers(self, evidence, count, length):
        evidence["steering-m"] = steering_evidence("steering-m", [
            steer(1000 + i, "m" * length, at=f"2026-11-10T00:{i // 60:02d}:{i % 60:02d}Z",
                  authority=False, source="upstream_pr_review_comment") for i in range(count)],
            at="2026-11-11T00:00:00+00:00")
        return evidence

    def ci_tail(self, evidence, count, head):
        for i in range(count):
            eid = f"ci-1467208233-55{i:04d}-1"
            evidence[eid] = ci_evidence(eid, head, at=f"2026-11-12T{i // 60:02d}:{i % 60:02d}:00+00:00")
        return evidence

    def check(self, evidence, *, steering_whole=None, report_whole=True):
        """The history of ``evidence``, checked: it fits, every entry and
        every steering item is kept or counted, the newest report (and its
        newest linked CI run) is whole when ``report_whole``, and the newest
        steering item is whole; with ``steering_whole`` every steering item
        is whole and none is folded."""
        timeline_total = len(task_history._entries(record(), evidence)[1])
        steering_total = len(task_history._entries(record(), evidence)[0])
        doc = task_history.document(record(), WORK, evidence)
        self.assertLessEqual(len(canonical_json(doc).encode()), task_history.HISTORY_MAX_BYTES)
        counted = [t for t in doc["timeline"] if t["kind"] in ("earlier", "later")]
        self.assertEqual(len(doc["timeline"]) - len(counted)
                         + sum(t["entries"] for t in counted), timeline_total)
        self.assertEqual(len(doc["steering"]) + (doc.get("steering_earlier") or {}).get("items", 0),
                         steering_total)
        reports = [t for t in doc["timeline"] if t["kind"] == "worker_report"]
        newest = max((e for e in evidence.values() if e["kind"] == "worker_report"),
                     key=lambda e: (e["collected_at"], e["evidence_id"]), default=None)
        if newest is not None and report_whole:
            self.assertEqual(reports[-1]["evidence_id"], newest["evidence_id"])
            self.assertNotIn("condensed", reports[-1])
            linked = [t for t in doc["timeline"] if t["kind"] == "ci"
                      and t.get("after_report") == newest["evidence_id"]]
            if any(e["kind"] == "ci" and e["ci"]["head_sha"] == newest["data"]["head_after"]
                   for e in evidence.values()):
                self.assertNotIn("condensed", linked[-1])
        if steering_total and report_whole:
            self.assertNotIn("condensed", doc["steering"][-1])
        if steering_whole:
            self.assertEqual(len(doc["steering"]), steering_total)
            self.assertFalse([s for s in doc["steering"] if s.get("condensed")])
        return doc

    def test_a_large_report_then_many_ci_runs_keep_the_owner_steering(self):
        # One 32 KiB report and 70 CI runs on its head: the CI runs gave
        # way to the steering.
        for scale, runs in ((0.7, 70), (0.18, 120), (0.18, 200)):
            with self.subTest(scale=scale, runs=runs):
                evidence = self.ci_tail(self.reports(1, scale=scale, ci=False, steering=3,
                                                     length=300), runs, _sha(1))
                doc = self.check(evidence, steering_whole=True)
                self.assertEqual(doc["timeline"][-1]["kind"], "ci")

    def test_the_report_of_the_failing_head_outlives_a_later_rerun(self):
        """A worker publishes a head, CI fails on it, the repair worker asks
        for a rerun (its report publishes nothing), and CI fails again: the
        next repair reads the report of the worker whose head failed, whole,
        with the CI linked to it, besides the newest report."""
        evidence = {}
        for i in range(25):
            report = report_evidence(f"report-{i:03d}", at=f"2026-10-{1 + i:02d}T10:00:00+00:00",
                                     head_before=_sha(i), head_after=_sha(i + 1),
                                     **_big_report(scale=0.3))
            evidence[report["evidence_id"]] = report
            ci = ci_evidence(f"ci-{i:03d}", _sha(i + 1), at=f"2026-10-{1 + i:02d}T11:00:00+00:00")
            evidence[ci["evidence_id"]] = ci
        rerun = report_evidence("report-rerun", at="2026-10-27T10:00:00+00:00",
                                head_before=_sha(25), head_after=None, **_big_report(scale=0.7))
        rerun["data"]["verb"] = "request_ci_rerun"
        evidence["report-rerun"] = rerun
        evidence["ci-rerun"] = ci_evidence("ci-rerun", _sha(25), at="2026-10-27T12:00:00+00:00")
        self.maintainers(evidence, 50, 800)
        evidence["steering-a"] = steering_evidence("steering-a", [
            steer(i, "s" * 1500, at=f"2026-09-01T00:00:{i:02d}Z") for i in range(12)],
            at="2026-11-01T00:00:00+00:00")
        rec = record()
        rec["fork"]["head_sha"] = _sha(25)
        doc = task_history.document(rec, WORK, evidence)
        self.assertLessEqual(len(canonical_json(doc).encode()), task_history.HISTORY_MAX_BYTES)
        whole = {t["evidence_id"]: t for t in doc["timeline"]
                 if t["kind"] in ("worker_report", "ci") and not t.get("condensed")}
        self.assertIn("report-024", whole)
        self.assertIn("report-rerun", whole)
        self.assertEqual(whole["ci-rerun"]["after_report"], "report-024")
        self.assertEqual(len(doc["steering"]), 12)
        # Without a report on the recorded head, the newest report that
        # published a head is the one protected.
        rec["fork"]["head_sha"] = _sha(99)
        latest, head = task_history.protected_reports(
            rec, task_history._entries(rec, evidence)[1])
        self.assertEqual((latest["evidence_id"], head["evidence_id"]),
                         ("report-rerun", "report-024"))

    def test_a_report_whose_push_was_not_read_back_is_the_report_of_its_head(self):
        """P published H1; U pushed H2 but its push was not read back, so
        its report names H2 as ``head_candidate`` only; CI failed on H2;
        the repair R asked for a rerun, which failed too. At every scale the
        report of the head is U, with the CI on H2 linked to it, and never
        P; once the recorded head is another commit, U is not the report of
        the head."""
        h0, h1, h2 = _sha(1), _sha(2), _sha(3)

        def rep(eid, revision, at, before, after, verb, char, scale, candidate=None):
            e = report_evidence(eid, at=at, head_before=before, head_after=after,
                                **_big_report(char, scale))
            e["data"]["work"].update(revision=revision, requested_at=at,
                                     dedupe_key=f"{CID}@g0.r{revision}:development_repair")
            e["data"]["verb"] = verb
            if candidate:
                e["data"]["head_candidate"] = candidate
            return e

        for scale in (0.3, 0.6, 0.7):
            with self.subTest(scale=scale):
                evidence = {e["evidence_id"]: e for e in (
                    rep("report-p", 1, "2026-11-01T00:00:00+00:00", h0, h1,
                        "record_publish_success", "p", scale),
                    rep("report-u", 3, "2026-11-10T00:00:00+00:00", h1, None,
                        "record_late_effect", "u", scale, candidate=h2),
                    rep("report-r", 5, "2026-11-12T00:00:00+00:00", h2, None,
                        "request_ci_rerun", "r", scale),
                    ci_evidence("ci-p", h1, at="2026-11-02T00:00:00+00:00"),
                    ci_evidence("ci-u1", h2, at="2026-11-11T00:00:00+00:00"),
                    ci_evidence("ci-u2", h2, at="2026-11-13T00:00:00+00:00"),
                    steering_evidence("steering-0", [
                        steer(i, "Keep the QEMU row; do not touch aarch64. " * 20,
                              at=f"2026-11-{1 + i % 20:02d}T05:00:00Z") for i in range(30)],
                        at="2026-11-20T00:00:00+00:00"))}
                rec = record()
                rec["fork"]["head_sha"] = h2
                doc = task_history.document(rec, dict(WORK, revision=7), evidence)
                self.assertLessEqual(len(canonical_json(doc).encode()),
                                     task_history.HISTORY_MAX_BYTES)
                entries = {t.get("evidence_id"): t for t in doc["timeline"]}
                self.assertFalse(entries["report-u"].get("condensed"))
                self.assertEqual(entries["report-u"]["head_candidate"], h2)
                for ci in ("ci-u1", "ci-u2"):
                    if ci in entries:
                        self.assertEqual(entries[ci]["after_report"], "report-u")
                timeline = task_history._entries(rec, evidence)[1]
                latest, head = task_history.protected_reports(rec, timeline)
                self.assertEqual((latest["evidence_id"], head["evidence_id"]),
                                 ("report-r", "report-u"))
                rec["fork"]["head_sha"] = _sha(9)
                self.assertEqual(task_history.protected_reports(rec, timeline)[1]["evidence_id"],
                                 "report-p")

    def test_a_report_stored_late_never_passes_for_the_newest(self):
        """The report of a late Result whose push landed is stored when the
        Result is seen, possibly after a newer task: it is placed at the time
        its task was requested, and the newest report is decided by the
        task each describes."""
        older = report_evidence("report-old", at="2026-11-20T12:00:00+00:00",
                                head_before=_sha(1), head_after=_sha(2))
        older["data"]["verb"] = "record_late_effect"
        older["data"]["work"].update(revision=3, requested_at="2026-11-18T09:00:00+00:00")
        newer = report_evidence("report-new", at="2026-11-19T12:00:00+00:00",
                                head_before=_sha(2), head_after=_sha(3))
        newer["data"]["work"]["revision"] = 4
        evidence = {e["evidence_id"]: e for e in (older, newer)}
        rec = record()
        rec["fork"]["head_sha"] = _sha(3)
        timeline = task_history._entries(rec, evidence)[1]
        self.assertEqual([(t["evidence_id"], t["at"], t.get("stored_at")) for t in timeline],
                         [("report-old", "2026-11-18T09:00:00+00:00",
                           "2026-11-20T12:00:00+00:00"),
                          ("report-new", "2026-11-19T12:00:00+00:00", None)])
        latest, head = task_history.protected_reports(rec, timeline)
        self.assertEqual((latest["evidence_id"], head["evidence_id"]),
                         ("report-new", "report-new"))
        # Even without the time of its task, the revision decides.
        del older["data"]["work"]["requested_at"]
        latest, _head = task_history.protected_reports(
            rec, task_history._entries(rec, evidence)[1])
        self.assertEqual(latest["evidence_id"], "report-new")

    def test_the_newest_published_fallback_relies_on_monotonic_revisions(self):
        """When no report left the recorded head, the report of the head is
        the newest report with a ``head_after``, by the task it describes:
        a report of an older revision stored later never passes for it.
        That order is the request order only because the revision and the
        generation of a contribution never decrease, which the engine
        guarantees: a bumping row adds one, and a new generation bumps the
        revision too."""
        import commands
        late = report_evidence("report-late", at="2026-11-22T12:00:00+00:00",
                               head_before=_sha(1), head_after=_sha(2))
        late["data"]["work"]["revision"] = 2
        newest = report_evidence("report-newest", at="2026-11-19T12:00:00+00:00",
                                 head_before=_sha(2), head_after=_sha(3))
        newest["data"]["work"]["revision"] = 5
        rec = record()
        rec["fork"]["head_sha"] = _sha(9)
        timeline = task_history._entries(
            rec, {e["evidence_id"]: e for e in (late, newest)})[1]
        latest, head = task_history.protected_reports(rec, timeline)
        self.assertEqual((latest["evidence_id"], head["evidence_id"]),
                         ("report-newest", "report-newest"))
        # The engine never makes a revision or a generation go back.
        parked = contribution(phase="parked", activity="idle", parked_reason="x",
                              previous_phase="development", revision=4)
        out = command(parked, commands.parse(
            '/upstreaming refresh revision=4 reason="r"'), policy=policy(), now=NOW,
            command_id="c", open_operations=())
        self.assertEqual(out.verb, "restart_generation")
        self.assertEqual((out.record["revision"], out.record["generation"]),
                         (5, parked.get("generation", 0) + 1))

    def test_the_newest_report_is_reduced_before_any_steering_is_folded(self):
        """Thirty owner comments of 1500 bytes and a report of 32 KiB: the
        report takes its reduced form, every section kept, and no steering
        item is folded."""
        evidence = self.reports(1, scale=1.0, steering=30, length=1500)
        doc = task_history.document(record(), WORK, evidence)
        self.assertLessEqual(len(canonical_json(doc).encode()), task_history.HISTORY_MAX_BYTES)
        self.assertNotIn("steering_earlier", doc)
        self.assertEqual(len(doc["steering"]), 30)
        [report] = [t for t in doc["timeline"] if t["kind"] == "worker_report"]
        self.assertTrue(report["reduced"])
        self.assertNotIn("condensed", report)
        self.assertEqual(sorted(report["report"]),
                         ["approaches", "attempted", "failures", "open_items", "revisited",
                          "summary", "verification"])
        self.assertEqual(len(report["report"]["approaches"]), 10)
        self.assertEqual(set(report["report"]["approaches"][0]),
                         {"title", "outcome", "lesson", "evidence", "description"})

    def test_maintainer_feedback_never_displaces_owner_steering(self):
        rows = [(0.7, 5, count, length) for count, length in ((20, 3000), (40, 1500),
                                                              (150, 1500), (400, 400))]
        # Reports of about 32, 10, 10 and 17 KiB under realistic feedback.
        rows += [(0.7, 8, 35, 300), (0.18, 8, 70, 300), (0.18, 8, 40, 800), (0.35, 8, 45, 400)]
        for scale, steering, count, length in rows:
            with self.subTest(scale=scale, maintainer_comments=count, length=length):
                evidence = self.maintainers(self.reports(2, scale=scale, steering=steering,
                                                         length=500), count, length)
                doc = self.check(evidence, steering_whole=True)
                self.assertNotIn("steering_earlier", doc)

    def test_the_newest_report_outlives_any_tail(self):
        cases = {"150 maintainer comments": self.maintainers(
                     self.reports(2, steering=5, length=500), 150, 1500),
                 "400 maintainer comments": self.maintainers(
                     self.reports(2, steering=5, length=500), 400, 400),
                 "150 CI runs": self.ci_tail(self.reports(2, steering=5, length=500), 150,
                                             _sha(2)),
                 "200 CI runs": self.ci_tail(self.reports(1, scale=0.18, ci=False, steering=3,
                                                          length=300), 200, _sha(1))}
        for name, evidence in cases.items():
            with self.subTest(name):
                doc = self.check(evidence, steering_whole=True)
                kinds = [t["kind"] for t in doc["timeline"]]
                self.assertIn("later", kinds)

    def test_many_reports_and_steering(self):
        for count, steering, length, scale in ((3, 10, 1500, 0.7), (5, 30, 1500, 0.7),
                                               (30, 40, 1500, 0.7), (30, 40, 600, 0.18),
                                               (80, 40, 600, 0.18)):
            with self.subTest(reports=count, steering=steering):
                self.check(self.reports(count, steering=steering, length=length, scale=scale))

    def test_steering_within_the_reserve_survives_timeline_pressure(self):
        evidence = self.reports(60, scale=0.18, steering=15, length=500)
        doc = self.check(evidence, steering_whole=True)
        self.assertGreater(doc["condensed"]["folded"], 50)

    def test_heavy_history_of_every_kind(self):
        evidence = {}
        for i in range(60):
            e = report_evidence(f"report-{i:03d}", at=f"2026-10-{1 + i // 3:02d}T{i % 3 + 10}:00:00+00:00",
                                head_before=_sha(i), head_after=_sha(i + 1),
                                **_big_report(scale=0.7))
            evidence[e["evidence_id"]] = e
            c = ci_evidence(f"ci-{i:03d}", _sha(i + 1),
                            at=f"2026-10-{1 + i // 3:02d}T{i % 3 + 10}:30:00+00:00")
            evidence[c["evidence_id"]] = c
        items = [steer(i, "s" * 4000, at=f"2026-09-01T00:{i // 60:02d}:{i % 60:02d}Z")
                 for i in range(600)]
        for k in range(3):
            evidence[f"steering-{k}"] = steering_evidence(
                f"steering-{k}", items[k * 200:(k + 1) * 200], at=f"2026-11-0{k + 1}T00:00:00+00:00")
        doc = self.check(evidence)
        self.assertEqual(doc["timeline"][-1]["after_report"], "report-059")
        self.assertEqual(doc["steering"][-1]["id"], 599)

    def test_many_failing_ci_runs_after_the_newest_report(self):
        evidence = {"report-1": report_evidence("report-1", at="2026-10-01T00:00:00+00:00",
                                                head_after=SHA_B, **_big_report())}
        for i in range(30):
            c = ci_evidence(f"ci-{i:03d}", SHA_B, at=f"2026-10-02T00:00:{i:02d}+00:00")
            c["ci"]["jobs"] = [dict(c["ci"]["jobs"][0], name=f"job {j} " + "n" * 150,
                                    executed=True, conclusion="failure") for j in range(40)]
            evidence[c["evidence_id"]] = c
        doc = self.check(evidence)
        self.assertEqual(doc["timeline"][-1]["evidence_id"], "ci-029")

    def test_multibyte_text_and_long_ids_never_raise(self):
        """Every bound is in bytes: a history of CJK text and long ids fits
        once folded, and never holds the task."""
        evidence = {}
        for i in range(120):
            e = report_evidence("report-" + "q" * 100 + f"-{i:03d}",
                                at=f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}+00:00",
                                head_before=_sha(i), head_after=_sha(i + 1),
                                **_big_report("漢"))
            evidence[e["evidence_id"]] = e
            c = ci_evidence("ci-" + "q" * 100 + f"-{i:03d}", _sha(i + 1),
                            at=f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}+00:00")
            evidence[c["evidence_id"]] = c
        for k in range(3):
            eid = "steering-" + "q" * 100 + f"-{k}"
            evidence[eid] = steering_evidence(eid, [
                steer(i, "漢" * 4000, at=f"2026-09-01T00:{i // 60:02d}:{i % 60:02d}Z")
                for i in range(k * 200, (k + 1) * 200)], at=f"2026-11-0{k + 1}T00:00:00+00:00")
        # The newest report alone is larger than the history: it is condensed.
        self.check(evidence, report_whole=False)
        cjk = self.ci_tail(self.reports(40, char="漢", scale=0.2, ci=False, steering=150,
                                        length=500), 250, _sha(40))
        self.check(cjk)
        for i in range(100):
            eid = f"report-example--proj--12-r{i + 50}-449829025{i:02d}"
            cjk[eid] = report_evidence(eid, at=f"2026-10-02T00:{i // 60:02d}:{i % 60:02d}+00:00",
                                       head_before=_sha(i), head_after=_sha(i + 1),
                                       **_big_report("漢", 0.2))
        self.check({k: v for k, v in cjk.items() if not k.startswith("ci-")})

    def test_a_cut_is_bounded_in_bytes(self):
        text = task_history._text("漢" * 1000, task_history.SHORT)
        self.assertLessEqual(len(text.split(" [truncated")[0].encode()), task_history.SHORT)
        self.assertTrue(text.endswith("[truncated: 900 more characters]"))
        self.assertEqual(task_history._text("plain", task_history.SHORT), "plain")

    def test_a_fully_folded_history_degrades_instead_of_raising(self):
        evidence = self.reports(3, steering=3, length=100)
        with mock.patch("task_history.HISTORY_MAX_BYTES", 2500):
            doc = task_history.document(record(), WORK, evidence)
        self.assertLessEqual(len(canonical_json(doc).encode()), 2500)
        self.assertTrue(doc["condensed"]["degraded"])
        self.assertEqual({t["kind"] for t in doc["timeline"]}, {"earlier", "later"})
        self.assertEqual(sum(t["entries"] for t in doc["timeline"]), 6)

    def test_condensed_steering_names_its_evidence_file(self):
        evidence = self.reports(1, scale=0.7, steering=60, length=3000)
        doc = self.check(evidence)
        short = [s for s in doc["steering"] if s.get("condensed")]
        self.assertTrue(short)
        for item in short:
            self.assertEqual(item["evidence_path"], ".state/evidence/steering-a.json")


def engine_state(record):
    import engine
    return engine.state_key(record)


class TestTick(unittest.TestCase):
    """The tick records the history of the task it allocates, in the same commit."""

    def evidence(self):
        return {"report-1": report_evidence("report-1", at="2026-11-20T10:00:00+00:00",
                                            head_after=SHA_A)}

    def test_the_allocation_and_its_history_are_staged_together(self):
        out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        self.assertEqual(dev.kinds(out), ["record_ci_failure", "request_repair"])
        rec = dev.final(out, None)
        eid = task_history.evidence_id(rec["active_task"]["dedupe_key"])
        [history] = [e for k, e in out.staged if k == "evidence" and e["evidence_id"] == eid]
        kinds = [t["kind"] for t in history["data"]["timeline"]]
        self.assertEqual(kinds, ["worker_report", "ci"])
        self.assertEqual(history["data"]["timeline"][1]["after_report"], "report-1")
        self.assertEqual(out.report["work_history"][0]["evidence_id"], eid)
        # The task of this tick is dispatched by the launch after the commit.
        self.assertNotIn("dispatch", out.report["work_history"][0])
        self.assertEqual(out.report["dispatch_relaunch"], [])
        # Recorded once: a tick that finds it committed records nothing.
        evidence = dict(self.evidence(), **{e["evidence_id"]: e for k, e in out.staged
                                            if k == "evidence"})
        again = dev.tick(rec, evidence=evidence)
        self.assertEqual(again.report["work_history"], [])

    def test_a_failed_steering_read_defers_only_the_history(self):
        with mock.patch("collect.steering", side_effect=collect.CollectError("HTTP 502")):
            out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        # The allocation is committed; the history is not, and the report
        # says why.
        self.assertEqual(dev.kinds(out), ["record_ci_failure", "request_repair"])
        self.assertFalse([e for k, e in out.staged if k == "evidence"
                          and e["kind"] in ("work_history", "human_steering")])
        self.assertNotIn("skipped_contributions", out.report)
        [entry] = out.report["work_history"]
        self.assertIn("a steering list could not be read: HTTP 502", entry["deferred"])

    def test_an_unexpected_error_pauses_the_contribution(self):
        with mock.patch("task_history.build", side_effect=RuntimeError("boom")):
            out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        self.assertEqual(dev.kinds(out), ["pause_on_processing_error"])
        self.assertEqual(out.report["processing_errors"][0]["step"], "work_history")
        self.assertIsInstance(out.failures[0], isolation.ProcessingError)

    def test_steering_is_collected_and_recorded_with_the_history(self):
        collected = {"items": [steer(5, "Please keep the QEMU topology.",
                                     at="2026-11-22T00:00:00Z")], "omitted": {}}
        with mock.patch("collect.steering", return_value=collected) as read:
            out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        self.assertEqual(read.call_args.args[2], POLICY["command_authority"]["actor_ids"])
        [steering] = [e for k, e in out.staged if k == "evidence" and e["kind"] == "human_steering"]
        [history] = [e for k, e in out.staged if k == "evidence" and e["kind"] == "work_history"]
        self.assertEqual([s["body"] for s in history["data"]["steering"]],
                         ["Please keep the QEMU topology."])
        self.assertIn(steering["evidence_id"], history["data"]["sources"])
        # Steering changed no field of the record.
        rec = dev.final(out, None)
        self.assertNotIn("Please keep", json.dumps(rec))

    def test_many_steering_items_are_recorded_in_the_same_tick(self):
        many = [steer(i, "z" * 150, at=f"2026-11-{1 + i // 24:02d}T{i % 24:02d}:00:00Z")
                for i in range(1, 450)]
        with mock.patch("collect.steering", return_value={"items": many, "omitted": {}}):
            out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        records = [e for k, e in out.staged if k == "evidence" and e["kind"] == "human_steering"]
        self.assertGreater(len(records), 1)
        self.assertEqual(sum(len(r["data"]["items"]) for r in records), len(many))
        self.assertEqual(records[-1]["data"]["deferred"], 0)
        [history] = [e for k, e in out.staged if k == "evidence" and e["kind"] == "work_history"]
        # Every item is in the history, the oldest folded into a count, and
        # the newest kept.
        data = history["data"]
        self.assertEqual(len(data["steering"]) + data["steering_earlier"]["items"], len(many))
        self.assertEqual(data["steering"][-1]["id"], many[-1]["id"])
        self.assertNotIn("steering_deferred", history["data"]["omitted"])
        self.assertEqual(out.report["work_history"][0]["steering_evidence_ids"],
                         [r["evidence_id"] for r in records])

    def test_the_steering_of_one_tick_is_bounded_in_bytes_newest_first(self):
        """However much was written, what one tick records fits the state
        commit: the newest steering is recorded, the rest is counted and
        recorded by later ticks."""
        import reconcile

        many = [steer(i, "\u6f22" * 3900, at=f"2026-11-{1 + i // 24:02d}T{i % 24:02d}:00:00Z")
                for i in range(1, 450)]
        with mock.patch("collect.steering", return_value={"items": many, "omitted": {}}):
            out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        self.assertEqual(dev.kinds(out), ["record_ci_failure", "request_repair"])
        records = [e for k, e in out.staged if k == "evidence" and e["kind"] == "human_steering"]
        size = sum(len(canonical_json(r).encode()) for r in records)
        self.assertLessEqual(size, reconcile.STEERING_BYTES_PER_CONTRIBUTION)
        recorded = [i["id"] for r in records for i in r["data"]["items"]]
        self.assertIn(many[-1]["id"], recorded)
        self.assertNotIn(many[0]["id"], recorded)
        [history] = [e for k, e in out.staged if k == "evidence" and e["kind"] == "work_history"]
        deferred = len(many) - len(recorded)
        self.assertEqual(history["data"]["omitted"]["steering_deferred"], deferred)
        self.assertEqual(out.report["work_history"][0]["steering_deferred"], deferred)
        # The next history records the rest.
        evidence = dict(self.evidence(), **{e["evidence_id"]: e for e in records})
        rest = steering_module().pending(dev.final(out, None), {"items": many}, evidence)
        self.assertEqual(rest, deferred)

    def test_the_histories_of_one_tick_are_bounded_in_bytes(self):
        import reconcile

        with mock.patch("reconcile.HISTORY_BYTES_PER_TICK", reconcile.HISTORY_RECORD_BYTES - 1):
            out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        self.assertEqual(dev.kinds(out), ["record_ci_failure", "request_repair"])
        self.assertFalse([e for k, e in out.staged if k == "evidence"
                          and e["kind"] == "work_history"])
        self.assertIn("byte bound", out.report["work_history"][0]["deferred"])
        # The next tick builds it.
        again = dev.tick(dev.final(out, None), evidence=self.evidence())
        self.assertTrue([e for k, e in again.staged if k == "evidence"
                         and e["kind"] == "work_history"])

    def test_a_steering_list_that_keeps_failing_stops_holding_the_task(self):
        """A list that fails from the tick that allocates the task on: the
        allocation is committed at once, so the grace is measured from its
        durable ``requested_at``; the ticks within the grace build no
        history and charge no dispatch, and the first tick after it builds
        the history without that list, with an alert."""

        calls = []

        def read(api, record, authority, *, issue, degrade=False):
            calls.append(degrade)
            if not degrade:
                raise collect.CollectError("GET x: HTTP 403", status=403)
            return {"items": [], "omitted": {},
                    "unreadable": ["development_pr_review", "upstream_pr_comment"],
                    "failed": {"development_pr_review": 403,
                               "upstream_pr_comment": "no HTTP answer: GET y: timed out"}}
        rec, evidence = dev.record("waiting_ci"), self.evidence()
        runs = []
        with mock.patch("collect.steering", side_effect=read):
            for minutes in (0, 10, 20, 30, 45, 61):
                now = dev.T + timedelta(minutes=minutes)
                # The dispatcher ran after every tick and deferred the task.
                out = dev.tick(rec, verdict="failed", evidence=evidence, now=now,
                               runs=list(runs))
                rec = dev.final(out, rec)
                evidence.update({e["evidence_id"]: e for k, e in out.staged if k == "evidence"})
                runs.insert(0, {"id": 100 + minutes, "status": "completed",
                                "created_at": (now + timedelta(minutes=1)).isoformat()})
                eid = task_history.evidence_id(rec["active_task"]["dedupe_key"])
                if minutes == 0:
                    self.assertEqual(dev.kinds(out), ["record_ci_failure", "request_repair"])
                    requested = rec["active_task"]["requested_at"]
                self.assertEqual(rec["active_task"]["requested_at"], requested)
                self.assertNotIn("skipped_contributions", out.report)
                if minutes < 60:
                    self.assertNotIn(eid, evidence)
                    self.assertIn("deferred", out.report["work_history"][0])
                    self.assertNotIn("retry_dispatch", dev.kinds(out))
                    if minutes:
                        self.assertEqual(out.report["dispatch_recovery"][0]["decision"],
                                         "waiting for the history and the task file of the task")
        self.assertEqual(calls, [False] * 5 + [True])
        history = evidence[eid]
        self.assertEqual(history["data"]["steering_unreadable"],
                         ["development_pr_review", "upstream_pr_comment"])
        self.assertEqual(out.report["work_history"][0]["steering_failed"],
                         {"development_pr_review": 403,
                          "upstream_pr_comment": "no HTTP answer: GET y: timed out"})
        [alert] = [a["alert"] for a in out.report["controller"]["alerts"]
                   if "steering" in a["alert"]]
        self.assertIn("development_pr_review (HTTP 403), "
                      "upstream_pr_comment (no HTTP answer: GET y: timed out)", alert)
        self.assertNotIn("HTTP None", alert)
        # The tick that recorded the history launches the dispatcher, which
        # deferred the task until then.
        self.assertEqual(out.report["dispatch_relaunch"],
                         [{"type": "relaunch_dispatcher", "contribution_id": rec["contribution_id"]}])
        self.assertEqual((engine_state(rec), rec["pending_human_action"]),
                         ("development.agent_running", None))
        # The dispatcher runs before the history was recorded charge
        # nothing: should the launch not show up as a run, a later tick
        # relaunches it at once.
        later = dev.tick(rec, verdict="failed", evidence=evidence, observe=False,
                         now=dev.T + timedelta(minutes=76),
                         runs=list(runs[1:]))
        self.assertEqual(later.report["dispatch_recovery"][0]["decision"],
                         "relaunch: no dispatcher run")
        self.assertEqual(dev.kinds(later), [])


    def test_an_unreadable_steering_list_does_not_hold_the_task(self):
        collected = {"items": [], "omitted": {}, "unreadable": ["development_pr_review"]}
        with mock.patch("collect.steering", return_value=collected):
            out = dev.tick(dev.record("waiting_ci"), verdict="failed", evidence=self.evidence())
        self.assertEqual(dev.kinds(out), ["record_ci_failure", "request_repair"])
        [history] = [e for k, e in out.staged if k == "evidence" and e["kind"] == "work_history"]
        self.assertEqual(history["data"]["steering_unreadable"], ["development_pr_review"])
        self.assertEqual(out.report["work_history"][0]["steering_unreadable"],
                         ["development_pr_review"])

    def test_an_offline_tick_builds_nothing(self):
        out = dev.tick(dev.running(), observe=False)
        self.assertNotIn("work_history", out.report)
        self.assertFalse([e for k, e in out.staged if k == "evidence"])

    def test_task_in_flight_gets_its_history(self):
        """A task an earlier tick requested whose history this tick records
        was deferred by the dispatcher: the tick asks for a dispatcher
        launch at once, not through dispatch recovery."""
        out = dev.tick(dev.running())
        [entry] = out.report["work_history"]
        self.assertEqual(entry["work"], dev.running()["active_task"]["dedupe_key"])
        self.assertEqual(entry["dispatch"], "relaunch: the history was recorded after the request")
        self.assertEqual(out.report["dispatch_relaunch"],
                         [{"type": "relaunch_dispatcher",
                           "contribution_id": dev.running()["contribution_id"]}])


class TestPayload(unittest.TestCase):
    """The dispatcher gives every task a reference to its task file, which
    names its history."""

    def plan(self, evidence, record, root):
        snapshot = type("S", (), {"contributions": {"c": record}, "evidence": evidence})
        out = Path(root) / "plan.json"
        args = type("A", (), {"root": root, "out": str(out), "control_repo_id": "1"})
        with mock.patch("reconcile.load_snapshot", return_value=(None, snapshot)), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout="c" * 40)), \
                mock.patch("builtins.print") as printed:
            tasks.cmd_plan(args)
        selection = json.loads(printed.call_args.args[0])
        return selection, (json.loads(out.read_text()) if out.exists() else None)

    def record(self, description="d"):
        rec = contribution(phase="development", activity="agent_running", revision=4)
        rec["upstream"].update(repository_id=1, base_sha="a" * 40)
        rec["active_task"] = {"dedupe_key": WORK["dedupe_key"], "task": "development_repair",
                              "generation": 0, "revision": 4, "attempt": 1,
                              "requested_at": NOW, "deadline_at": NOW, "dispatched_run_id": None,
                              "reason_ids": [], "feedback_evidence_ids": [],
                              "tracking_issue": {"url": "https://x", "description": description}}
        return rec

    def commit(self, root, record):
        """The history and the task file of the task of ``record`` as the
        tick commits them under ``root``: the evidence by id."""
        key = record["active_task"]["dedupe_key"]
        history = task_history.evidence_id(key)
        evidence = {history: {"evidence_id": history, "kind": "work_history"}}
        directory = Path(root) / ".state" / "evidence"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{history}.json").write_bytes(task_build.file_bytes(evidence[history]))
        task = task_build.build(record, evidence, policy=POLICY, now=NOW,
                               file_digest=task_build.file_digest_reader(Path(root), evidence))
        (directory / f"{task['evidence_id']}.json").write_bytes(task_build.file_bytes(task))
        evidence[task["evidence_id"]] = task
        return evidence

    def test_the_payload_names_the_committed_task_file(self):
        with tempfile.TemporaryDirectory() as root:
            record = self.record("x" * 4000)
            evidence = self.commit(root, record)
            selection, plan = self.plan(evidence, record, root)
            self.assertEqual(selection["selected"][0]["task"], "development_repair")
            payload = plan["nodes"][0]["payload"]
            tid = task_build.evidence_id(WORK["dedupe_key"])
            reference = payload["task_file"]
            self.assertEqual(reference["path"], f".state/evidence/{tid}.json")
            self.assertEqual((reference["evidence_id"], reference["ref"],
                              reference["repository"]),
                             (tid, "c" * 40, "riseproject-dev/porting-agent"))
            import hashlib
            self.assertEqual(reference["sha256"], hashlib.sha256(
                (Path(root) / reference["path"]).read_bytes()).hexdigest())
            # A reference of a fixed size, however long the description is.
            self.assertLess(len(json.dumps(payload, separators=(",", ":")).encode()),
                            tasks.MAX_QUEUE_PAYLOAD_BYTES)
            self.assertNotIn("context", payload)
            # The resolved task names the history, and steering never widens
            # the task.
            task = task_file.resolve(
                payload, lambda repository, ref, path: (Path(root) / path).read_bytes())
            self.assertEqual(task["context"]["history"]["evidence_id"],
                             task_history.evidence_id(WORK["dedupe_key"]))
            self.assertEqual(task["context"]["tracking_issue"]["description"], "x" * 4000)
            self.assertFalse(task["review_scope_extension"])
            self.assertEqual(task["allowed_operations"], ["append", "rewrite"])

    def test_the_history_reference_of_the_task_file_carries_no_url_or_commit(self):
        with tempfile.TemporaryDirectory() as root:
            record = self.record()
            evidence = self.commit(root, record)
            self.plan(evidence, record, root)
            tid = task_build.evidence_id(WORK["dedupe_key"])
            document = json.loads((Path(root) / f".state/evidence/{tid}.json").read_text())
        self.assertEqual(set(document["data"]["context"]["history"]),
                         {"path", "sha256", "evidence_id"})

    def test_the_largest_description_is_dispatched_like_any_other(self):
        """A request description at the intake limit (16384 bytes) took the
        payload over the queue's budget when it was carried inline: the task
        file holds it, and the payload is the same size as any other."""
        largest = self.record("\u00e9" * (POLICY["intake"]["max_request_bytes"] // 2))
        other = self.record("short")
        other["contribution_id"] = "other--lib/13"
        other["active_task"] = dict(other["active_task"], dedupe_key="other--lib/13@g0.r4:"
                                    "development_repair", requested_at="2026-11-19T00:00:00Z")
        with tempfile.TemporaryDirectory() as root:
            evidence = {**self.commit(root, largest), **self.commit(root, other)}
            snapshot = type("S", (), {"contributions": {"a": largest, "b": other},
                                      "evidence": evidence})
            out = Path(root) / "plan.json"
            args = type("A", (), {"root": root, "out": str(out), "control_repo_id": "1"})
            with mock.patch("reconcile.load_snapshot", return_value=(None, snapshot)), \
                    mock.patch("subprocess.run", return_value=subprocess.CompletedProcess(
                        [], 0, stdout="c" * 40)), \
                    mock.patch("builtins.print") as printed, \
                    mock.patch("tasks.load_policy", return_value=dict(
                        POLICY, concurrency=dict(POLICY["concurrency"], coding_workers=2))):
                self.assertEqual(tasks.cmd_plan(args), 0)
            selection = json.loads(printed.call_args.args[0])
            plan = json.loads(out.read_text())
        self.assertEqual(sorted(s["contribution"] for s in selection["selected"]),
                         ["example--proj/12", "other--lib/13"])
        self.assertNotIn("skipped", selection)
        sizes = [len(json.dumps(n["payload"], separators=(",", ":"), ensure_ascii=False).encode())
                 for n in plan["nodes"]]
        self.assertLessEqual(max(sizes), tasks.MAX_QUEUE_PAYLOAD_BYTES)
        self.assertLess(max(sizes) - min(sizes), 40)

    def test_task_without_a_recorded_history_or_task_file_is_deferred(self):
        with tempfile.TemporaryDirectory() as root:
            selection, plan = self.plan({}, self.record(), root)
            self.assertEqual(selection["selected"], [])
            self.assertEqual(selection["deferred"][0]["reason"],
                             "history and task file not recorded yet")
            record = self.record()
            evidence = self.commit(root, record)
            tid = task_build.evidence_id(WORK["dedupe_key"])
            evidence.pop(tid)
            selection, plan = self.plan(evidence, record, root)
            self.assertEqual(selection["deferred"][0]["reason"], "task file not recorded yet")
        self.assertIsNone(plan)
