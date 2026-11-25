# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The worker report: its schema, its trusted check, and how it is cleaned."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import artifact
import isolation
import schemas
import state
import submission
import worker_gate
import worker_report

HISTORY = "e" * 64
CID = "example--proj/12"
TOKEN = "ghp_" + "A" * 36


def report(**over) -> dict:
    value = {
        "schema_version": 1, "contribution_id": CID, "expected_revision": 3,
        "task": "development_repair", "history_digest": HISTORY,
        "summary": "Added riscv64 to the wheel matrix; the musllinux build needed a newer image.",
        "attempted": [{"what": "Add riscv64 to CIBW_ARCHS_LINUX",
                       "why": "The project builds every other architecture there."}],
        "approaches": [
            {"title": "Native riscv64 runner row", "description": "A dedicated job.",
             "outcome": "abandoned", "evidence": "The project uses QEMU for aarch64.",
             "lesson": "Follow the QEMU topology."},
            {"title": "QEMU row", "description": "riscv64 in the shared list.",
             "outcome": "adopted", "evidence": "docker run ... exit 0", "lesson": ""}],
        "failures": [{"what": "musllinux_1_1 has no riscv64 image",
                      "evidence": "manifest unknown"}],
        "open_items": ["The CHANGELOG convention is unclear."],
        "verification": [{"check": "build and test in the riscv64 image", "result": "passed",
                          "evidence": "exit 0"}],
        "revisited": [],
        "review_loop": {"rounds": 1, "round_digests": ["0" * 64], "stopped_because": "clean",
                        "findings": [],
                        "summary": "One round of fresh reviewers found nothing to fix."},
    }
    value.update(over)
    return value


def task(history=True, **over) -> dict:
    value = {"task": "development_repair", "contribution_id": CID, "revision": 3,
             "expected_head": "a" * 40, "dedupe_key": f"{CID}@g0.r3:development_repair",
             "generation": 0,
             "context": {"history": {"sha256": HISTORY}} if history else {}}
    value.update(over)
    return value


def raw(value) -> bytes:
    return json.dumps(value).encode()


class TestSchema(unittest.TestCase):
    def test_a_complete_report_is_valid(self):
        schemas.validate("worker_report", report())
        schemas.validate("worker_report", report(history_digest=None))

    def test_malformed_reports_are_refused(self):
        for broken in (report(summary=""), report(summary="x" * 1001), report(approaches=[]),
                       report(attempted=[]), report(task="planning"),
                       report(approaches=[dict(report()["approaches"][0], outcome="maybe")]),
                       report(extra=1), report(history_digest="xyz"),
                       {k: v for k, v in report().items() if k != "revisited"}):
            with self.subTest(broken=list(broken)[:3]):
                with self.assertRaises(schemas.SchemaError):
                    schemas.validate("worker_report", broken)


class TestCheck(unittest.TestCase):
    def test_optional_without_a_history_required_with_one(self):
        self.assertIsNone(worker_report.check(task(history=False), None))
        # A report of a task dispatched before reports existed is not checked
        # strictly: an unusable one is ignored.
        self.assertIsNone(worker_report.check(task(history=False), b"not json"))
        with self.assertRaisesRegex(worker_report.ReportError, "no report.json"):
            worker_report.check(task(), None)
        self.assertEqual(worker_report.check(task(), raw(report()))["summary"],
                         report()["summary"])
        self.assertEqual(worker_report.check(task(), report())["task"], "development_repair")

    def test_identity_and_history_mismatches(self):
        for name, value in (("contribution_id", report(contribution_id="other/1")),
                            ("expected_revision", report(expected_revision=4)),
                            ("task", report(task="development_patch")),
                            ("history_digest", report(history_digest="f" * 64)),
                            ("history_digest", report(history_digest=None))):
            with self.subTest(field=name):
                with self.assertRaisesRegex(worker_report.ReportError, name):
                    worker_report.check(task(), raw(value))

    def test_oversized_and_non_json_reports(self):
        with self.assertRaisesRegex(worker_report.ReportError, "more than"):
            worker_report.check(task(), b"{" + b" " * worker_report.MAX_REPORT_BYTES + b"}")
        for value in (b"\xff\xfe", b"[]", b"nope"):
            with self.assertRaises(worker_report.ReportError):
                worker_report.check(task(), value)

    def test_a_deeply_nested_report_is_invalid(self):
        nested = b"[" * (worker_report.MAX_REPORT_BYTES - 1)
        with self.assertRaisesRegex(worker_report.ReportError, "RecursionError|JSONDecodeError"):
            worker_report.check(task(), nested)
        self.assertIsNone(worker_report.check(task(history=False), nested))

    def test_the_controller_loader_keeps_the_bytes_of_the_report(self):
        import reconcile
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "9001"
            directory.mkdir()
            (directory / "outcome.json").write_text(json.dumps({"status": "published"}))
            (directory / "report.json").write_text("[" * (worker_report.MAX_REPORT_BYTES - 1))
            [outcome] = reconcile._publication_outcomes(root)
        # The fold checks the bytes the gate checked: invalid, and refused
        # loudly when the task carries a history.
        self.assertEqual(outcome["worker_report"], b"[" * (worker_report.MAX_REPORT_BYTES - 1))
        self.assertIsNone(worker_report.check(task(history=False), outcome["worker_report"]))
        with self.assertRaisesRegex(worker_report.ReportError, "RecursionError|JSONDecodeError"):
            worker_report.check(task(), outcome["worker_report"])

    def test_a_link_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real.json"
            real.write_bytes(raw(report()))
            link = Path(directory) / "report.json"
            link.symlink_to(real)
            with self.assertRaisesRegex(worker_report.ReportError, "regular file"):
                worker_report.check_path(task(), link)
            self.assertIsNone(worker_report.check_path(task(history=False), link))
            self.assertIsNotNone(worker_report.check_path(task(), real))


class TestClean(unittest.TestCase):
    def test_tokens_are_redacted_newlines_kept_and_truncation_marked(self):
        text = f"line one\r\nline two\twith a tab\x07 and {TOKEN}\u2014end"
        cleaned = worker_report.clean(text, 1000)
        self.assertNotIn(TOKEN, cleaned)
        self.assertIn(worker_report.REDACTED, cleaned)
        self.assertIn("line one\nline two\twith a tab and", cleaned)
        self.assertNotIn("\x07", cleaned)
        self.assertNotIn("\u2014", cleaned)
        self.assertEqual(state.find_secrets(cleaned), [])
        cut = worker_report.clean("x" * 50, 10)
        self.assertEqual(cut, "x" * 10 + " [truncated: 40 more characters]")

    def test_a_cut_never_leaves_a_credential(self):
        value = worker_report.clean("AKIA" + "B" * 16 + "C", 20)
        self.assertEqual(state.find_secrets(value), [])


class TestGates(unittest.TestCase):
    def test_the_worker_gate_rejects_a_result_without_its_report(self):
        from tests import test_milestone5_worker as m5
        series = m5.mbox()
        own = dict(m5.task(), context={"history": {"sha256": HISTORY}})
        valid = report(contribution_id=own["contribution_id"], task=own["task"])
        valid["review_loop"]["round_digests"] = [hashlib.sha256(series).hexdigest()]
        with tempfile.TemporaryDirectory() as directory:
            mbox = Path(directory) / "patches.mbox"
            mbox.write_bytes(series)
            path = Path(directory) / "report.json"
            with self.assertRaisesRegex(worker_gate.WorkerGateError,
                                        "worker report is missing or invalid"):
                worker_gate.validate(own, m5.proposal(series), mbox, path)
            path.write_bytes(raw(dict(valid, expected_revision=2)))
            with self.assertRaisesRegex(worker_gate.WorkerGateError, "expected_revision"):
                worker_gate.validate(own, m5.proposal(series), mbox, path)
            path.write_bytes(raw(valid))
            self.assertEqual(worker_gate.validate(own, m5.proposal(series), mbox, path)["status"],
                             "accepted")
            # A patch needs its report whatever the task carries: it holds
            # the review loop.
            with self.assertRaisesRegex(worker_gate.WorkerGateError, "review loop"):
                worker_gate.validate(m5.task(), m5.proposal(series), mbox,
                                     Path(directory) / "none.json")
            # The CLI reports the rejection.
            (Path(directory) / "task.json").write_text(json.dumps(own))
            (Path(directory) / "proposal.json").write_text(json.dumps(m5.proposal(series)))
            path.unlink()
            code = worker_gate.main(["--task", f"{directory}/task.json", "--proposal",
                                     f"{directory}/proposal.json", "--mbox", str(mbox),
                                     "--report", str(path), "--out", f"{directory}/gate.json"])
            gate = json.loads((Path(directory) / "gate.json").read_text())
            self.assertEqual((code, gate["status"]), (1, "rejected"))
            self.assertIn("worker report", gate["reason"])

    def test_a_blocked_result_is_classified_whatever_its_report(self):
        # A blocked worker's report is discarded, so a missing one does not
        # turn an infrastructure blocker into a validation rejection.
        from tests import test_milestone5_worker as m5
        own = dict(m5.task(), context={"history": {"sha256": HISTORY}})
        exact = m5.IMAGE + "@sha256:" + "b" * 64
        candidate = m5.TestWorkerGate._no_change(None, m5.proposal(m5.mbox()))
        candidate.update(outcome="blocked",
                         failure_cluster="reproduction-image-infrastructure-docker")
        candidate["reproduction"]["commands"] = [
            {"command": "docker run --rm --platform linux/riscv64 " + exact + " make test",
             "exit_status": 1, "log_sha256": "c" * 64}]
        with tempfile.TemporaryDirectory() as directory:
            gate = worker_gate.validate(own, candidate, Path(directory) / "patches.mbox",
                                        Path(directory) / "report.json")
        self.assertEqual(gate["status"], "infrastructure_blocked")

    def test_the_submission_check_refuses_a_result_without_its_report(self):
        rewrite_task = {"task": "submission_rewrite", "contribution_id": CID, "revision": 3,
                        "expected_head": "a" * 40, "base_sha": "b" * 40,
                        "context": {"history": {"sha256": HISTORY}}}
        out = submission.check_rewrite(rewrite_task, {"verdict": "pass"}, b"")
        self.assertEqual((out["status"], out["failure_class"], out["stage"]),
                         ("rejected", "validation", "submission_check"))
        self.assertIn("worker report", out["reason"])


class TestRecovery(unittest.TestCase):
    def test_the_recovery_gate_reads_the_report_of_the_run(self):
        import worker_recovery
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / "record.json").write_text("{}")
            outcome = {"status": "rejected"}
            with mock.patch("worker_gate.validate",
                            side_effect=worker_gate.WorkerGateError("no")) as gate:
                self.assertIs(worker_recovery._recover_no_change(directory, "1", task(), {},
                                                                 outcome), outcome)
            self.assertEqual(gate.call_args.args[3], directory / "report.json")


class TestArtifact(unittest.TestCase):
    def test_report_json_is_extracted_and_unknown_files_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            zpath = Path(directory) / "a.zip"
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("porting-out/proposal.json", "{}")
                z.writestr("porting-out/report.json", json.dumps(report()))
            self.assertEqual(artifact.extract(zpath, Path(directory) / "out"),
                             ["proposal.json", "report.json"])
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("porting-out/proposal.json", "{}")
                z.writestr("porting-out/history.json", "{}")
            with self.assertRaises(artifact.ArtifactError):
                artifact.extract(zpath, Path(directory) / "out2")
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("porting-out/proposal.json", "{}")
                z.writestr("porting-out/report.json", "x" * (32 * 1024 + 1))
            with self.assertRaises(artifact.ArtifactError):
                artifact.extract(zpath, Path(directory) / "out3")


class TestEvidence(unittest.TestCase):
    def test_the_head_a_publication_left_or_proposed(self):
        """``head_after`` is the commit of a confirmed operation, of the one
        the row names, or the candidate a publish-only retry read back; the
        commit of a push that was not read back is only ``head_candidate``."""
        verb = "record_publish_success"

        def op(status, commit="c" * 40):
            return {"kind": "branch_update", "status": status,
                    "proposed": {"commit": commit, "changed_files": ["a"]}}
        cases = [
            ({"status": "published", "operation": op("confirmed")}, None, "c" * 40, None),
            ({"status": "uncertain", "operation": op("uncertain"), "candidate": "c" * 40},
             None, None, "c" * 40),
            ({"status": "uncertain", "operation": op("applying")}, None, None, "c" * 40),
            ({"status": "uncertain", "candidate": "d" * 40}, None, None, "d" * 40),
            ({"status": "published", "candidate": "d" * 40}, None, "d" * 40, None),
            # A stale retry behind the confirmed operation of an earlier
            # attempt: the row names that operation.
            ({"status": "rejected", "failure_class": "stale"}, op("confirmed", "e" * 40),
             "e" * 40, None),
            ({"status": "published", "operation": op("confirmed")}, None, None, None),
        ]
        # The same for a revision retry: its Result is rejected, and the
        # confirmed operation of the earlier attempt is the head it left.
        revision = "record_revision_publish_success"
        stale = {"status": "rejected", "failure_class": "stale"}
        self.assertEqual((worker_report.head_after(stale, revision, op("confirmed", "e" * 40)),
                          worker_report.head_candidate(stale, revision, op("confirmed", "e" * 40))),
                         ("e" * 40, None))
        self.assertEqual((worker_report.head_after(stale, revision, op("uncertain", "e" * 40)),
                          worker_report.head_candidate(stale, revision, op("uncertain", "e" * 40))),
                         (None, "e" * 40))
        for index, (outcome, named, after, candidate) in enumerate(cases):
            row = "request_ci_rerun" if index == len(cases) - 1 else verb
            with self.subTest(case=index):
                self.assertEqual((worker_report.head_after(outcome, row, named),
                                  worker_report.head_candidate(outcome, row, named)),
                                 (after, candidate))

    def test_the_evidence_is_cleaned_and_passes_the_store(self):
        rec = {"contribution_id": CID, "revision": 3}
        outcome = {"status": "published", "task": task(), "publication_artifact_id": 9,
                   "worker_report": report(summary=f"used {TOKEN} by mistake"),
                   "operation": {"status": "confirmed",
                                 "proposed": {"commit": "c" * 40, "changed_files": ["a"]}}}
        evidence = worker_report.evidence(rec, copy.deepcopy(outcome),
                                          verb="record_publish_success",
                                          now="2026-11-23T00:00:00+00:00", policy={}, run="77")
        self.assertEqual(evidence["evidence_id"], "report-example--proj--12-r3-77")
        self.assertEqual((evidence["data"]["head_before"], evidence["data"]["head_after"],
                          evidence["data"]["changed_files"]), ("a" * 40, "c" * 40, ["a"]))
        self.assertNotIn(TOKEN, json.dumps(evidence))
        self.assertEqual(state.check_blob(
            ".state/evidence/report-example--proj--12-r3-77.json",
            json.dumps(evidence).encode(), state_policy()), [])
        # A task without a history may return no report; one with a history
        # never loses it silently.
        self.assertIsNone(worker_report.evidence(
            rec, dict(outcome, worker_report=None, task=task(history=False)),
            verb="record_publish_success", now="2026-11-23T00:00:00+00:00", policy={},
            run="77"))
        with self.assertRaisesRegex(worker_report.ReportError, "no report.json"):
            worker_report.evidence(rec, dict(outcome, worker_report=None),
                                   verb="record_publish_success",
                                   now="2026-11-23T00:00:00+00:00", policy={}, run="77")


    def test_the_id_and_the_time_of_the_task_are_recorded_safely(self):
        outcome = {"status": "published", "publication_artifact_id": 9,
                   "task": task(context={"history": {"sha256": HISTORY},
                                         "requested_at": "2026-11-22T08:00:00+00:00"}),
                   "worker_report": report(),
                   "operation": {"status": "confirmed",
                                 "proposed": {"commit": "c" * 40, "changed_files": ["a"]}}}
        kwargs = {"verb": "record_publish_success", "now": "2026-11-23T00:00:00+00:00",
                  "policy": {}, "run": "77"}
        evidence = worker_report.evidence({"contribution_id": CID, "revision": 3},
                                          copy.deepcopy(outcome), **kwargs)
        self.assertEqual(evidence["data"]["work"]["requested_at"], "2026-11-22T08:00:00+00:00")
        # An id over the bound of a record id is shortened with a digest.
        long_id = "o" * 39 + "--" + "r" * 80 + "/12"
        evidence = worker_report.evidence({"contribution_id": long_id, "revision": 3},
                                          copy.deepcopy(outcome), **kwargs)
        self.assertEqual(len(evidence["evidence_id"]), 128)
        # An id that cannot be stored is a report that cannot be stored:
        # a pause of its own contribution, never a processing error.
        with self.assertRaises(worker_report.ReportError) as raised:
            worker_report.evidence({"contribution_id": "bad id/1", "revision": 3},
                                   copy.deepcopy(outcome), **kwargs)
        self.assertIsInstance(raised.exception, isolation.PauseRequest)


def state_policy():
    from tests.helpers import policy
    return policy()


if __name__ == "__main__":
    unittest.main()
