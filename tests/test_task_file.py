# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The task of a claimed Work, resolved from the task file its payload names."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import task_file
import work
import work_history
import work_task
from tests import live_d2vm as live
from tests.helpers import policy

POLICY = policy()
COMMIT = "c" * 40


class Fixture:
    """The live d2vm Work with its task file committed, and the claim of the
    Work as the queue carries it."""

    def __init__(self, control_repo_id="1234567890"):
        self.record, self.evidence = live.fresh()
        self.request = work.request_from_active(self.record)
        digest = work_task.file_digest_reader(None, self.evidence)
        self.task = work_task.build(self.record, self.evidence, policy=POLICY,
                                    now="2026-11-24T17:00:00+00:00", file_digest=digest)
        self.raw = work_task.file_bytes(self.task)
        self.path = work_task.path(self.task["evidence_id"])
        self.files = {(task_file.CONTROL_REPO, COMMIT, self.path): self.raw}
        reference = task_file.reference(live.WORK, COMMIT, hashlib.sha256(self.raw).hexdigest())
        self.claim = work.queue_payload(self.request, control_repo_id=control_repo_id,
                                        reference=reference)

    def fetch(self, repository, ref, path):
        try:
            return self.files[(repository, ref, path)]
        except KeyError:
            raise task_file.Unavailable("no such file") from None

    def resolve(self, claim=None):
        return task_file.resolve(self.claim if claim is None else claim, self.fetch)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def test_the_resolved_work_is_the_payload_of_old(self):
        resolved = self.f.resolve()
        inline = work.payload(self.f.request, self.f.record, control_repo_id="1234567890",
                              allowed_paths=resolved["allowed_paths"],
                              context=self._context(resolved))
        resolved.pop("task_file")
        self.assertEqual(resolved, inline)

    @staticmethod
    def _context(resolved):
        context = copy.deepcopy(resolved["context"])
        for key in ("tracking_issue", "refresh", "existing_fork"):
            context.pop(key, None)
        return context

    def test_the_references_name_the_commit_of_the_task_file(self):
        resolved = self.f.resolve()
        history = resolved["context"]["history"]
        self.assertEqual((history["repository"], history["ref"], history["evidence_id"]),
                         (task_file.CONTROL_REPO, COMMIT, live.HISTORY))
        [plan] = [i for i in resolved["context"]["reason_evidence"] if i["kind"] == "plan"]
        self.assertEqual(plan["reference"]["ref"], COMMIT)
        self.assertEqual(plan["reference"]["url"],
                         "https://raw.githubusercontent.com/riseproject-dev/porting-agent/"
                         f"{COMMIT}/.state/evidence/{live.PLAN}.json")
        self.assertEqual(resolved["task_file"], self.f.claim["task_file"])
        self.assertEqual(resolved["resource_scope"], self.f.claim["resource_scope"])
        self.assertEqual(resolved["effect_contract"], self.f.claim["effect_contract"])

    def test_the_resolved_work_names_its_identity_as_the_claim_does(self):
        resolved = self.f.resolve()
        self.assertEqual(work.task_identity(resolved), (1, live.WORK))
        self.assertEqual(work.task_identity(self.f.claim), (1, live.WORK))

    def test_a_task_carried_inline_is_accepted_unchanged(self):
        inline = work.payload(self.f.request, self.f.record, control_repo_id="7",
                              allowed_paths=["a"], context={})
        self.assertIs(task_file.resolve(inline, self.refuse), inline)

    def refuse(self, *_):
        raise AssertionError("nothing is fetched")

    def refused(self, claim, message=None, fixture=None):
        with self.assertRaises(task_file.Refused) as raised:
            (fixture or self.f).resolve(claim)
        if message:
            self.assertIn(message, str(raised.exception))

    def mutate(self, **changes):
        claim = copy.deepcopy(self.f.claim)
        reference = claim["task_file"]
        for key, value in changes.items():
            if key in claim:
                claim[key] = value
            else:
                reference[key] = value
        return claim

    def test_a_reference_that_is_not_the_one_of_the_control_repository_is_refused(self):
        self.refused(self.mutate(repository="evil/repo"), "repository")
        self.refused(self.mutate(ref="main"), "ref")
        self.refused(self.mutate(ref="C" * 40), "ref")
        self.refused(self.mutate(sha256="x"), "digest")
        self.refused(self.mutate(path=".state/evidence/other.json"), "path and id differ")
        self.refused(self.mutate(path="../.state/evidence/" + self.f.task["evidence_id"]
                                 + ".json"), "path and id differ")
        self.refused(self.mutate(evidence_id="task-" + "0" * 32), "path and id differ")
        self.refused(self.mutate(evidence_id="plan-x"), "evidence id")
        claim = self.mutate()
        claim["task_file"]["extra"] = 1
        self.refused(claim, "malformed")
        claim = self.mutate()
        claim["task_file"] = "x"
        self.refused(claim, "malformed")

    def test_an_id_not_derived_from_the_work_id_is_refused(self):
        other = "task-" + "0" * 32
        claim = self.mutate(evidence_id=other, path=f".state/evidence/{other}.json")
        self.refused(claim, "not derived from the Work id")

    def test_a_file_that_is_not_the_one_the_digest_names_is_refused(self):
        self.f.files[(task_file.CONTROL_REPO, COMMIT, self.f.path)] = self.f.raw + b" "
        self.refused(None, "digest mismatch")

    def test_a_file_of_another_kind_or_identity_is_refused(self):
        for change, message in (
                (lambda d: d.update(kind="work_history"), "identity mismatch"),
                (lambda d: d.update(evidence_id="task-" + "1" * 32), "identity mismatch"),
                (lambda d: d["data"].update(dedupe_key="x@g1.r10:development_repair"),
                 "differ in dedupe_key"),
                (lambda d: d["data"].update(generation=0), "differ in generation"),
                (lambda d: d["data"].update(revision=3), "differ in revision"),
                (lambda d: d["data"].update(review="none"), "differ in review"),
                (lambda d: d["data"].update(task="development_patch"), "differ in task"),
                (lambda d: d["data"].update(contribution_id="a/1"), "differ in contribution_id"),
                (lambda d: d.update(data=[]), "holds no task"),
                (lambda d: d["data"].update(resource_scope={}), "fields of the queue"),
                (lambda d: d["data"].update(task_file={}), "fields of the queue")):
            with self.subTest(message=message):
                f = Fixture()
                document = json.loads(f.raw)
                change(document)
                raw = json.dumps(document).encode()
                f.files[(task_file.CONTROL_REPO, COMMIT, f.path)] = raw
                claim = copy.deepcopy(f.claim)
                claim["task_file"]["sha256"] = hashlib.sha256(raw).hexdigest()
                self.refused(claim, message, f)

    def test_a_reference_that_names_a_commit_or_a_url_itself_is_refused(self):
        for key, value in (("ref", COMMIT), ("url", "https://evil"), ("repository", "evil/x")):
            with self.subTest(key=key):
                f = Fixture()
                document = json.loads(f.raw)
                document["data"]["context"]["history"][key] = value
                raw = json.dumps(document).encode()
                f.files[(task_file.CONTROL_REPO, COMMIT, f.path)] = raw
                claim = copy.deepcopy(f.claim)
                claim["task_file"]["sha256"] = hashlib.sha256(raw).hexdigest()
                self.refused(claim, "malformed", f)

    def test_a_claim_without_what_the_queue_carries_is_refused(self):
        for name in ("resource_scope", "effect_contract"):
            claim = self.mutate()
            claim.pop(name)
            self.refused(claim, name)
        self.refused({"task": "planning"}, "neither")
        self.refused([], "not an object")

    def test_the_resolved_task_is_validated_by_a_caller_that_has_the_schemas(self):
        import schemas
        resolved = task_file.resolve(self.f.claim, self.f.fetch,
                                     validate=lambda t: schemas.validate("task", t))
        self.assertEqual(resolved, self.f.resolve())
        with self.assertRaises(task_file.Refused) as raised:
            task_file.resolve(self.f.claim, self.f.fetch, validate=lambda t: schemas.validate(
                "task", dict(t, instructions=None)))
        self.assertIn("the resolved task is invalid", str(raised.exception))

    def test_a_file_that_cannot_be_read_is_unavailable(self):
        self.f.files.clear()
        with self.assertRaises(task_file.Unavailable):
            self.f.resolve()


class TestCommandLine(unittest.TestCase):
    def run_main(self, claim, fixture):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "claim.json"
            out = Path(directory) / "out" / "task.json"
            source.write_text(json.dumps(claim))
            with mock.patch("task_file.gh_fetch", side_effect=fixture.fetch), \
                    contextlib.redirect_stdout(io.StringIO()) as printed, \
                    contextlib.redirect_stderr(io.StringIO()) as failed:
                code = task_file.main(["resolve", "--claim", str(source), "--out", str(out)])
            return code, printed.getvalue(), failed.getvalue(), (
                json.loads(out.read_text()) if out.exists() else None)

    def test_the_resolved_task_is_written_and_only_its_id_printed(self):
        fixture = Fixture()
        code, printed, _failed, task = self.run_main(fixture.claim, fixture)
        self.assertEqual(code, 0)
        self.assertEqual(printed.strip(), f"resolved task {fixture.task['evidence_id']}")
        self.assertEqual(task, fixture.resolve())
        self.assertNotIn("instructions", printed)

    def test_the_exit_status_tells_a_refusal_from_an_unreadable_file(self):
        fixture = Fixture()
        claim = copy.deepcopy(fixture.claim)
        claim["generation"] = 0
        code, _printed, failed, task = self.run_main(claim, fixture)
        self.assertEqual((code, task), (1, None))
        self.assertIn("task file refused", failed)
        fixture.files.clear()
        code, _printed, _failed, task = self.run_main(fixture.claim, fixture)
        self.assertEqual((code, task), (3, None))

    def test_the_file_is_read_with_the_token_of_the_job(self):
        done = subprocess.CompletedProcess([], 0, stdout=b"{}")
        with mock.patch("subprocess.run", return_value=done) as run:
            self.assertEqual(task_file.gh_fetch("o/r", COMMIT, ".state/evidence/x.json"), b"{}")
        self.assertEqual(run.call_args.args[0][:2], ["gh", "api"])
        self.assertIn(f"/repos/o/r/contents/.state/evidence/x.json?ref={COMMIT}",
                      run.call_args.args[0])
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh")):
            with self.assertRaises(task_file.Unavailable):
                task_file.gh_fetch("o/r", COMMIT, "p")


class TestBounds(unittest.TestCase):
    def test_the_queue_payload_is_fixed_in_size_whatever_the_task_holds(self):
        """The payload of the live d2vm Work that no queue accepted (15592
        bytes inline) is a reference of about a kilobyte."""
        fixture = Fixture()
        size = len(json.dumps(fixture.claim, separators=(",", ":"), ensure_ascii=False).encode())
        self.assertLess(size, 1024)
        self.assertLessEqual(size, work.MAX_QUEUE_PAYLOAD_BYTES)

    def test_the_largest_queue_payload_fits_the_bound(self):
        """Every field of the payload at the longest the schemas admit: the
        longest contribution id and Work id, the longest repository id, and
        the largest effect contract."""
        longest = "a" * 128
        request = {"task": "development_patch", "contribution_id": longest, "generation": 10**9,
                   "revision": 10**9, "dedupe_key": "x" * 300}
        reference = task_file.reference(request["dedupe_key"], COMMIT, "f" * 64)
        sizes = []
        for task in ("planning", *sorted(work.WRITE_TASKS), *work.SUBMISSION_TASKS):
            payload = work.queue_payload(dict(request, task=task),
                                         control_repo_id="9" * work.CONTROL_REPO_ID_MAX_DIGITS,
                                         reference=reference)
            sizes.append(len(json.dumps(payload, separators=(",", ":"),
                                        ensure_ascii=False).encode()))
        self.assertLessEqual(max(sizes), work.MAX_QUEUE_PAYLOAD_BYTES)
        # The bound has a margin below the queue's own limit (16384).
        self.assertLess(work.MAX_QUEUE_PAYLOAD_BYTES, 16384 // 4)

    def test_the_history_of_the_work_is_named_by_the_task_not_the_payload(self):
        fixture = Fixture()
        self.assertNotIn("history", json.dumps(fixture.claim))
        self.assertEqual(fixture.resolve()["context"]["history"]["evidence_id"],
                         work_history.evidence_id(live.WORK))


class TestQueueResult(unittest.TestCase):
    """``queue_result`` writes the claimed Work as ``claim.json`` and the
    task its payload names as ``task.json``."""

    def run_main(self, fixture, claim=None):
        import queue_result
        assignment = {"version": 3, "dispatch_id": "d1", "claims": [{
            "work_id": "w1", "claim_id": "c1", "handle": "h",
            "work": claim if claim is not None else fixture.claim}]}
        art = queue_result.ProposalArtifact("w1", "c1", 5, "claim-x-porting-out", "sha256:00",
                                            "99", 1, None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assignment.json").write_text(json.dumps(assignment))
            (root / "projection.json").write_text("{}")
            out = root / "in"
            with mock.patch("queue_result.resolve", return_value=art), \
                    mock.patch("queue_result.check_artifact_metadata"), \
                    mock.patch("queue_result.check_bytes"), \
                    mock.patch("collect.Api") as api, \
                    mock.patch("task_file.gh_fetch", side_effect=fixture.fetch), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                api.return_value.get_bytes.return_value = b"zip"
                code = queue_result.main([
                    "--projection", str(root / "projection.json"),
                    "--assignment", str(root / "assignment.json"), "--run-id", "99",
                    "--workflow", "w.yml", "--repo", "o/r", "--out", str(out)])
            return code, {p.name: json.loads(p.read_text()) for p in out.glob("*.json")}

    def test_the_task_of_the_claim_is_resolved_into_the_publication_directory(self):
        fixture = Fixture()
        code, files = self.run_main(fixture)
        self.assertEqual(code, 0)
        self.assertEqual(files["claim.json"], fixture.claim)
        self.assertEqual(files["task.json"], fixture.resolve())

    def test_the_resolved_task_must_satisfy_the_schema(self):
        fixture = Fixture()
        document = json.loads(fixture.raw)
        document["data"]["allowed_paths"] = [1]
        raw = json.dumps(document).encode()
        fixture.files[(task_file.CONTROL_REPO, COMMIT, fixture.path)] = raw
        claim = copy.deepcopy(fixture.claim)
        claim["task_file"]["sha256"] = hashlib.sha256(raw).hexdigest()
        code, files = self.run_main(fixture, claim)
        self.assertEqual((code, files["outcome.json"]["status"]), (1, "rejected"))

    def test_a_task_the_claim_does_not_match_is_a_rejected_result(self):
        fixture = Fixture()
        claim = copy.deepcopy(fixture.claim)
        claim["task_file"]["sha256"] = "0" * 64
        code, files = self.run_main(fixture, claim)
        self.assertEqual(code, 1)
        self.assertEqual((files["outcome.json"]["status"], files["outcome.json"]["failure_class"]),
                         ("rejected", "validation"))
        self.assertNotIn("task.json", files)

    def test_a_task_file_that_cannot_be_read_is_retried_later(self):
        fixture = Fixture()
        fixture.files.clear()
        code, files = self.run_main(fixture)
        self.assertEqual(code, 3)
        self.assertEqual(files["outcome.json"]["status"], "infra_error")


if __name__ == "__main__":
    unittest.main()
