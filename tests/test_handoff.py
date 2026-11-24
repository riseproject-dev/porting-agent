# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The submission of the live closeio--ciso8601/1 record.

`/upstreaming submit-upstream` on the migrated live record runs the whole
handoff by the table: the submission review Work and its trusted check, the
build of the upstream branch `riscv64` from the reviewed series, CI on its
clean head, the submission package, and the locked wait for the upstream PR
with its three exits. Development stays on `riscv64-dev`, which nothing here
writes, and the development PR is never closed. The fake GitHub below
answers the reads of the controller and the trusted workflow, records every
call, and holds the resources the workflow changes.
"""

import contextlib
import copy
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import capacity
import ci_rerun
import collect
import engine
import handoff
import lifecycle
import reconcile
import recovery
import status_ledger
import submission
import work as work_plan
from common import CONTROL_REPO, canonical_json, contribution_file_id
from tests.helpers import OWNER, STRANGER, policy, steering_list
from tests.test_live_handoff_migration import (
    CI_EVIDENCE,
    CID,
    FIXTURES,
    FORK,
    HEAD,
    SUBMIT,
    canary_operations,
    comment,
    load,
    migrated,
)

PLAN = "plan-closeio--ciso8601--1-r2"
PACKAGE_OP = "op-" + "a" * 24
BASE = "cbe05770dc95966a9bf0bff32bbcfa8ba0a3cbf8"
UPSTREAM_ID = 20633992
FORK_PATH = "/repos/riseproject-upstreaming/ciso8601"
APP = {"id": 41898282, "login": "riseproject-dev[bot]", "type": "Bot"}
T0 = "2026-11-23T09:00:00+00:00"
# The reviewed series: the live series has no debug commit, so the reviewed
# commit has the tree of the candidate head and another message, hence
# another head.
TREE = "7" * 40
CLEAN = "c1" * 20
REVIEWED_MESSAGE = ("Build riscv64 wheels on Linux\n\nAdd a native riscv64 row to the wheel "
                    "build matrix.")
TITLE = "Build riscv64 wheels on Linux"
BODY = ("## Summary\n\nAdd a native riscv64 row to the wheel build matrix.\n\n## Testing\n\n"
        "- [x] CI on the fork")


def at(minutes: int, day: str = "2026-11-23") -> datetime:
    return datetime.fromisoformat(f"{day}T09:00:00+00:00") + timedelta(minutes=minutes)


class FakeGitHub:
    """The resources the handoff reads and changes, and every call made."""

    def __init__(self):
        self.branches = {"riscv64-dev": HEAD, "riscv64": None}
        self.trees = {HEAD: TREE, CLEAN: TREE}
        self.messages = {CLEAN: REVIEWED_MESSAGE}
        self.compare = json.loads((FIXTURES / "fork-compare.json").read_text())
        self.dev_pr = {"id": 5437693983, "number": 1, "state": "open", "merged": False}
        self.comments: dict[int, dict] = {}
        self.upstream: list[dict] = []
        self.ci_state = "success"
        # Other jobs of the run, as (name, conclusion), the attempt of the
        # run, and the annotations of each job by its id.
        self.ci_extra_jobs: list[tuple[str, str]] = []
        self.ci_attempt = 1
        self.annotations: dict[int, list[dict]] = {}
        self.ci_heads: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_post = False

    # Reads of the controller (collect.Api.get) and of the workflow.
    def get(self, path: str):
        self.calls.append(("GET", path))
        if path.startswith(f"/repos/{CONTROL_REPO}/issues/comments/"):
            return copy.deepcopy(self.comments[int(path.rsplit("/", 1)[1])])
        for pr in self.upstream:
            if path == f"/repos/closeio/ciso8601/pulls/{pr['number']}":
                return copy.deepcopy(pr)
        if path == f"/repos/{FORK['name']}/actions/runs/45008094068" and self.ci_heads:
            return {"id": 45008094068, "head_sha": self.ci_heads[-1],
                    "run_attempt": self.ci_attempt, "status": "completed"}
        if path == f"/repos/{FORK['name']}":
            return {"id": FORK["repository_id"], "full_name": FORK["name"],
                    "default_branch": "master", "fork": True,
                    "parent": {"full_name": "closeio/ciso8601", "id": UPSTREAM_ID}}
        raise collect.CollectError(f"GET {path}: HTTP 404")

    def paginate(self, path: str, *, key=None, max_pages=10, **_kwargs):
        # The steering lists of Work the controller allocates: no human text.
        found = steering_list(path)
        if found is not None:
            return found
        self.calls.append(("GET", path))
        match = re.fullmatch(rf"/repos/{FORK['name']}/check-runs/([0-9]+)/annotations\?per_page=100",
                             path)
        if match:
            return copy.deepcopy(self.annotations.get(int(match.group(1)), []))
        raise collect.CollectError(f"GET {path}: HTTP 404")

    def mutate(self, method: str, path: str):
        """The rerun request of the CI rerun token: a new attempt."""
        self.calls.append((method, path))
        self.ci_attempt += 1

    def branch_head(self, _api, full_name, ref):
        self.calls.append(("GET", f"/repos/{full_name}/branches/{ref}"))
        head = self.branches.get(ref)
        if head is None:
            return None
        return {"head": head, "tree": self.trees.get(head, "t" * 40), "parents": [BASE]}

    def compare_commits(self, _api, full_name, base, head):
        self.calls.append(("GET", f"/repos/{full_name}/compare/{base}...{head}"))
        if head == HEAD:
            return copy.deepcopy(self.compare)
        if head in self.messages:
            return dict(copy.deepcopy(self.compare), commits=[
                {"sha": head, "commit": {"message": self.messages[head]}}])
        raise collect.CollectError("unknown head")

    def upstream_pull_requests(self, _api, record, ref="riscv64"):
        self.calls.append(("GET", f"/repos/{record['upstream']['full_name']}/pulls?state=open&"
                                  f"head=riseproject-upstreaming:{ref}&per_page=100"))
        return [handoff.normalize_pull(pr) for pr in self.upstream]

    def ci_snapshot(self, _api, full_name, head):
        self.calls.append(("GET", f"/repos/{full_name}/actions/runs?head_sha={head}"))
        self.ci_heads.append(head)
        run = {"id": 45008094068, "head_sha": head, "run_attempt": self.ci_attempt,
               "status": "completed", "conclusion": self.ci_state, "workflow_id": 426522359,
               "event": "pull_request", "path": ".github/workflows/lint.yml"}
        jobs = [{"id": job_id, "run_id": run["id"], "name": name, "status": "completed",
                 "conclusion": conclusion, "started_at": "x", "labels": ["ubuntu-24.04-riscv"]}
                for job_id, (name, conclusion) in enumerate(
                    [("Build - ubuntu-24.04-riscv (riscv64)", self.ci_state),
                     *self.ci_extra_jobs], start=1)]
        return collect.CiSnapshot(repository={"id": FORK["repository_id"], "full_name": FORK["name"]},
                                  head_sha=head, runs=[run], jobs_by_run={run["id"]: jobs},
                                  artifacts_by_run={})

    # The write of the issue token.
    def request(self, method, path, body=None):
        self.calls.append((method, path.split("?", 1)[0]))
        if method == "GET" and path.startswith(f"/repos/{CONTROL_REPO}/issues/1/comments"):
            return [copy.deepcopy(c) for c in self.comments.values()]
        if method == "POST" and path == f"/repos/{CONTROL_REPO}/issues/1/comments":
            if self.fail_post:
                raise status_ledger.LedgerError("ledger API HTTP 502")
            cid = 9000 + len(self.comments)
            self.comments[cid] = {
                "id": cid, "body": body["body"], "user": dict(APP),
                "html_url": f"https://github.com/{CONTROL_REPO}/issues/1#issuecomment-{cid}",
                "issue_url": f"https://api.github.com/repos/{CONTROL_REPO}/issues/1"}
            return copy.deepcopy(self.comments[cid])
        if method == "GET" and path.startswith(f"/repos/{CONTROL_REPO}/issues/comments/"):
            return copy.deepcopy(self.comments[int(path.rsplit("/", 1)[1])])
        raise AssertionError(f"unexpected {method} {path}")

    def fork_writes(self):
        """Every write to a fork branch: the workflow's build of `riscv64`."""
        return [c for c in self.calls if c[0] == "PUSH"]


class World:
    """The live record under the controller, with the trusted workflow run
    against the state the controller committed."""

    def __init__(self, record=None, p=None):
        self.record = record or migrated()
        self.operations: dict[str, dict] = canary_operations()
        self.evidence = {CI_EVIDENCE: load("evidence-ci.json"), PLAN: load("plan.json")}
        self.consumed: dict[int, dict] = {}
        self.events: list[dict] = []
        self.gh = FakeGitHub()
        self.policy = p or policy()
        self.reports: list[dict] = []
        self.publication = 9100

    def audited(self):
        keys = {(e.get("details") or {}).get("observation_key") for e in self.events
                if e["kind"] == "record_audit_observation"}
        return {CID: {k for k in keys if k}}

    def tick(self, now: datetime, *, comments=(), receipts=(), publications=(), ci_reruns=(),
             handoff_runs=()):
        """``handoff_runs`` names the operations whose run is in flight; the
        run list is read and lists no other run. None: it was not read."""
        gh = self.gh
        snap = reconcile.Snapshot({"c": self.record}, copy.deepcopy(self.operations),
                                  dict(self.consumed), {}, dict(self.evidence), self.audited())
        with mock.patch("collect.branch_head", gh.branch_head), \
                mock.patch("collect.compare_commits", gh.compare_commits), \
                mock.patch("collect.ci_snapshot", gh.ci_snapshot), \
                mock.patch("collect.upstream_pull_requests", gh.upstream_pull_requests), \
                mock.patch("collect.development_review_feedback", return_value=None), \
                mock.patch("collect.upstream_review_feedback", return_value=None):
            out = reconcile.tick(gh, snap, policy=self.policy, now=now,
                                 publications=list(publications),
                                 comments=list(comments), handoffs=list(receipts),
                                 ci_reruns=list(ci_reruns),
                                 handoff_runs=(None if handoff_runs is None
                                               else {op: 1 for op in handoff_runs}),
                                 pressure=capacity.Pressure(ok=True), dispatcher_runs=[],
                                 queue_works={})
        self.assertion_free(out)
        for kind, rec in out.staged:
            if kind == "contributions":
                self.record = rec
            elif kind == "operations":
                self.operations[rec["operation_id"]] = rec
            elif kind == "events":
                self.events.append(dict(rec, sequence=len(self.events) + 24))
            elif kind == "evidence":
                self.evidence[rec["evidence_id"]] = rec
            elif kind == "commands":
                self.consumed[rec["comment_id"]] = rec
        self.reports.append(out.report)
        return out

    @staticmethod
    def assertion_free(out):
        if out.failures:
            raise out.failures[0].error

    def kinds(self, out):
        return [e["kind"] for k, e in out.staged if k == "events"]

    @property
    def state(self):
        return engine.state_key(self.record)

    @property
    def active(self):
        return self.operations[self.record["active_operation_id"]]

    @property
    def review(self):
        return self.evidence[self.record["submission_review_id"]]

    def rewrite_outcome(self, verdict="pass", reasons=(), expires_at=None, **over):
        """The trusted outcome of the requested rewrite Work, as the worker's
        publish job writes it and the controller collects it."""
        work = self.record["active_work"]
        self.publication += 1
        out = {"status": submission.REWRITE, "stage": "submission_check", "approved_head": HEAD,
               "base_sha": BASE, "verdict": verdict, "reasons": list(reasons),
               "task": {"task": "submission_rewrite", "contribution_id": CID,
                        "revision": work["revision"], "generation": work["generation"],
                        "dedupe_key": work["dedupe_key"], "expected_head": HEAD},
               "worker_run_id": str(self.publication * 10), "publication_artifact_id": self.publication}
        if expires_at is not None:
            out["publication_artifact_expires_at"] = expires_at
        if verdict == submission.PASS:
            out.update(title=TITLE, body=BODY, head=CLEAN, clean_tree=TREE, candidate_tree=TREE,
                       commits=[{"message": REVIEWED_MESSAGE, "tree": TREE}],
                       series_digest="5" * 64, text_digest=submission.text_digest(TITLE, BODY),
                       rules_from=[])
        out.update(over)
        return out

    def review_outcome(self, verdict="pass", reasons=(), **over):
        """The trusted outcome of the requested review Work of the rewrite
        the record names."""
        work = self.record["active_work"]
        rewrite = self.evidence[self.record["submission_rewrite_id"]]["data"]
        self.publication += 1
        out = {"status": submission.REVIEW, "stage": "review_check", "approved_head": HEAD,
               "base_sha": BASE, "verdict": verdict, "reasons": list(reasons),
               "task": {"task": "submission_review", "contribution_id": CID,
                        "revision": work["revision"], "generation": work["generation"],
                        "dedupe_key": work["dedupe_key"], "expected_head": HEAD},
               "rewrite_evidence_id": self.record["submission_rewrite_id"],
               "series_digest": rewrite["series_digest"], "text_digest": rewrite["text_digest"],
               "expectations": [{"expectation": "The template's sections are filled",
                                 "met": verdict == submission.PASS}],
               "worker_run_id": str(self.publication * 10), "publication_artifact_id": self.publication}
        out.update(over)
        return out

    def root(self, directory: Path) -> Path:
        """The committed state the trusted workflow checks out."""
        for rel, value in [
            (f".state/contributions/{contribution_file_id(CID)}.json", self.record),
            *((f".state/operations/{oid}.json", op) for oid, op in self.operations.items()),
            *((f".state/evidence/{eid}.json", ev) for eid, ev in self.evidence.items()),
        ]:
            path = directory / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canonical_json(value))
        return directory

    def run_workflow(self, op_id: str | None = None, final_check=None) -> dict:
        """The receipt the trusted workflow uploads for the operation. The
        build of `riscv64` is the effect `TestUpstreamBranchWithGit` runs
        against a local fork; here it is simulated after the same checks.
        ``final_check`` stands for the check at the head of main immediately
        before the write (by default it passes)."""
        final_check = final_check or (lambda record, op: None)
        op = self.operations[op_id or self.record["active_operation_id"]]
        meta = {"operation_id": op["operation_id"], "kind": op["kind"],
                "contribution_id": CID, "run_id": "777"}
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(Path(directory))
            try:
                if op["kind"] == "upstream_branch":
                    record, loaded = handoff.load_request(root, op["operation_id"], self.policy)
                    head = op["proposed"]["commit"]
                    observed = self.gh.branches["riscv64"]
                    if observed != head:
                        if self.gh.upstream:
                            raise handoff.HandoffBlocked("an upstream PR is open from riscv64")
                        final_check(record, loaded)
                        self.gh.calls.append(("PUSH", f"riscv64:{observed or ''}->{head}"))
                        self.gh.branches["riscv64"] = head
                    result = {"result": "applied", "clean_head": head, "replaced": observed,
                              "written": observed != head}
                else:
                    with mock.patch("collect.compare_commits", self.gh.compare_commits), \
                            mock.patch("collect.branch_head", self.gh.branch_head):
                        result = handoff.post_package(root, op["operation_id"], policy=self.policy,
                                                      read_api=self.gh, issue_api=self.gh,
                                                      final_check=final_check)
                return handoff.receipt(meta, result)
            except Exception as error:
                return handoff.receipt(meta, error=error)


    def run_rerun(self, op_id: str | None = None, final_check=None) -> dict:
        """The receipt the ``ci-rerun`` job of the handoff workflow uploads:
        the check before any token, then the rerun and its readback."""
        op_id = op_id or self.record["active_operation_id"]
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(Path(directory))
            ci_rerun.load_request(root, op_id, self.policy, phase="handoff")
            with mock.patch("collect.branch_head", self.gh.branch_head):
                return ci_rerun.run_operation(root, op_id, self.gh, self.policy, "4242",
                                              phase="handoff", sleep=lambda _s: None,
                                              final_check=final_check or (lambda record, op: None))


def failed(world: World, failure=handoff.INFRASTRUCTURE) -> dict:
    op = world.active
    return {"schema_version": 1, "operation_id": op["operation_id"], "kind": op["kind"],
            "contribution_id": CID, "run_id": "778", "result": "failed", "failure": failure,
            "reason": "HTTP 502"}


def submitted(world: World | None = None) -> World:
    """The live record once `submit-upstream` is accepted: the review Work is
    requested in the same tick."""
    world = world or World()
    world.tick(at(0), comments=[comment(SUBMIT)])
    return world


def rewritten(world: World | None = None) -> World:
    """The live record once its rewrite passed the trusted check: the review
    Work is requested in the same tick."""
    world = submitted(world)
    world.tick(at(1), publications=[world.rewrite_outcome()])
    return world


def reviewed(world: World | None = None) -> World:
    """The live record once the review of its rewrite passed: the build of
    `riscv64` is planned and launched in the same tick."""
    world = rewritten(world)
    world.tick(at(1), publications=[world.review_outcome()])
    return world


def through(target: str) -> World:
    """The live record taken along the success path up to ``target``."""
    world = reviewed()
    minute = 2
    while world.state != target:
        receipt = world.run_workflow()
        world.tick(at(minute), receipts=[receipt])
        minute += 1
        if minute > 10:
            raise AssertionError(f"stuck in {world.state}")
    return world


def upstream_pr(**over):
    pr = {"id": 777, "number": 205, "state": "open", "merged_at": None,
          "html_url": "https://github.com/closeio/ciso8601/pull/205",
          "base": {"ref": "master", "repo": {"id": UPSTREAM_ID}},
          "head": {"ref": "riscv64", "sha": CLEAN, "repo": {"id": FORK["repository_id"]}},
          "user": {"id": OWNER["id"], "login": OWNER["login"], "type": "User"}}
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(pr.get(key), dict):
            pr[key] = dict(pr[key], **value)
        else:
            pr[key] = value
    return pr


class TestLivePath(unittest.TestCase):
    def test_submit_to_the_upstream_pr_wait_and_verification(self):
        world = World()
        self.assertEqual(world.record["fork"]["head_ref"], "riscv64-dev")
        out = world.tick(at(0), comments=[comment(SUBMIT)])
        self.assertEqual(world.kinds(out), ["submit_upstream", "request_submission_rewrite"])
        self.assertEqual(world.state, "handoff.agent_running")
        approval = world.record["handoff_approval"]
        self.assertEqual((approval["head"], approval["revision"], approval["actor"]),
                         (HEAD, 6, {"id": OWNER["id"], "login": OWNER["login"]}))
        self.assertEqual(approval["evidence_ids"], [CI_EVIDENCE, PLAN])
        # The rewrite Work is uncredentialed and writes nothing; no operation
        # and nothing remote yet.
        [work] = out.report["requested_effects"]
        self.assertEqual((work["task"], work["expected_head"], work["effect_contract"]),
                         ("submission_rewrite", HEAD, {"kind": "none"}))
        self.assertEqual(world.record["active_work"]["reason_ids"], [PLAN, CI_EVIDENCE])
        self.assertEqual(out.report["handoff_requests"], [])
        self.assertIsNone(world.record["active_operation_id"])

        # The checked rewrite: its review is requested in the same tick, as
        # new Work of the next revision, in a fresh context.
        out = world.tick(at(1), publications=[world.rewrite_outcome()])
        self.assertEqual(world.kinds(out), ["accept_submission_rewrite", "request_submission_review"])
        self.assertEqual((world.state, world.record["revision"]), ("handoff.reviewing", 7))
        rewrite = world.evidence[world.record["submission_rewrite_id"]]
        self.assertEqual((rewrite["kind"], rewrite["data"]["head"], rewrite["data"]["title"]),
                         ("submission_rewrite", CLEAN, TITLE))
        [work] = out.report["requested_effects"]
        self.assertEqual((work["task"], work["effect_contract"]),
                         ("submission_review", {"kind": "none"}))
        self.assertEqual(out.report["handoff_requests"], [])

        # The review passes: `riscv64` is built from the rewrite, under a
        # lease that it does not exist yet.
        out = world.tick(at(1), publications=[world.review_outcome()])
        self.assertEqual(world.kinds(out), ["accept_submission_review"])
        self.assertEqual(world.state, "handoff.building_upstream_branch")
        review = world.review
        self.assertEqual((review["kind"], review["data"]["head"], review["data"]["title"],
                          review["data"]["rewrite_evidence_id"]),
                         ("submission_review", CLEAN, TITLE, rewrite["evidence_id"]))
        build = world.active
        self.assertEqual((build["kind"], build["status"], build["reason_ids"][1:3]),
                         ("upstream_branch", "planned", ["attempt:1", "count:1"]))
        self.assertEqual(build["target"], {"repository": FORK["name"],
                                           "repository_id": FORK["repository_id"],
                                           "ref": "riscv64", "expected_head": HEAD,
                                           "base_sha": BASE})
        self.assertEqual((build["proposed"]["commit"], build["proposed"]["tree"]), (CLEAN, TREE))
        self.assertEqual(build["external_ids"]["publication_artifact_id"],
                         rewrite["data"]["artifact"]["artifact_id"])
        self.assertEqual([r["kind"] for r in out.report["handoff_requests"]], ["upstream_branch"])

        # The build receipt; the same tick observes CI and accepts the
        # evidence of the candidate head, which has the same tree.
        out = world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "accept_clean_head_ci"])
        self.assertEqual(world.gh.branches, {"riscv64-dev": HEAD, "riscv64": CLEAN})
        self.assertEqual(world.gh.fork_writes(), [("PUSH", f"riscv64:->{CLEAN}")])
        self.assertEqual((world.record["clean_head_sha"], world.record["fork"]["head_sha"]),
                         (CLEAN, HEAD))
        self.assertEqual(world.record["last_ci_evidence_id"], CI_EVIDENCE)
        self.assertEqual(world.gh.ci_heads[-1], HEAD)
        self.assertEqual(world.state, "handoff.preparing_submission")
        package_op = world.active
        self.assertEqual(package_op["target"]["ref"], "issues/1")

        out = world.tick(at(3), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_submission_package"])
        self.assertEqual(world.state, "handoff.waiting_upstream_pr")
        self.assertEqual(world.record["pending_human_action"]["kind"], "open_upstream_pr")
        self.assertEqual(world.record["handoff_request"],
                         {"command_id": package_op["operation_id"], "head": CLEAN, "revision": 7})
        self.assertIsNone(world.record["active_operation_id"])
        [posted] = world.gh.comments.values()
        self.assertEqual(posted["body"], handoff.build_package(
            world.record, plan=load("plan.json"), ci=world.evidence[CI_EVIDENCE],
            review=world.review, commits=[{"sha": CLEAN, "message": REVIEWED_MESSAGE}],
            files=[".github/workflows/build-wheels.yml", "pyproject.toml"],
            operation_id=package_op["operation_id"])["comment"])
        self.assertIn("riseproject-upstreaming:riscv64?quick_pull=1", posted["body"])
        self.assertIn("development PR #2, which stays open", posted["body"])
        self.assertEqual((world.record["revision"], world.record["fork"], world.record["generation"]),
                         (7, FORK, 0))
        entry = status_ledger.render(world.events[-1], world.record, "https://run")
        self.assertIn(posted["html_url"], entry)
        self.assertIn(f"/upstreaming retract-pr revision=7 head={CLEAN}", entry)

        # The human opens the PR from `riscv64`; observation verifies it.
        world.gh.upstream = [upstream_pr()]
        out = world.tick(at(4))
        self.assertEqual(world.kinds(out), ["verify_upstream_pr"])
        self.assertEqual(world.state, "upstream_review.waiting_ci")
        self.assertEqual((world.record["upstream_pr"]["head_ref"],
                          world.record["upstream_pr"]["head_sha"]), ("riscv64", CLEAN))
        self.assertIsNone(world.record["pending_human_action"])
        # Upstream review works on `riscv64` at the head of the upstream PR:
        # observing it there is no head change and bumps no revision.
        revision = world.record["revision"]
        self.assertEqual(lifecycle.recorded_head(world.record), CLEAN)
        # The recorded head of upstream review is the upstream PR's; the fork
        # keeps the approved head of `riscv64-dev`.
        self.assertEqual(world.record["fork"]["head_sha"], HEAD)
        out = world.tick(at(5))
        self.assertNotIn("head_changed", world.kinds(out))
        self.assertNotIn("record_head_change", world.kinds(out))
        self.assertEqual(world.record["revision"], revision)
        self.assertEqual(world.record["upstream_pr"]["head_sha"], CLEAN)
        # `riscv64-dev` and the development PR were never touched; the only
        # writes were `riscv64` and the tracking issue.
        self.assertEqual(world.gh.branches["riscv64-dev"], HEAD)
        self.assertEqual(world.gh.dev_pr["state"], "open")
        writes = [c for c in world.gh.calls if c[0] != "GET"]
        self.assertEqual(writes, [("PUSH", f"riscv64:->{CLEAN}"),
                                  ("POST", f"/repos/{CONTROL_REPO}/issues/1/comments")])

    def test_a_failed_rewrite_or_review_returns_to_development_with_its_reasons(self):
        for step, verb, revision in (("rewrite", "reject_submission_rewrite", 7),
                                     ("review", "reject_submission_review", 8)):
            with self.subTest(step=step):
                world = submitted() if step == "rewrite" else rewritten()
                reasons = [f"{step}: CONTRIBUTING.md asks for a CHANGELOG entry the series lacks"]
                outcome = (world.rewrite_outcome("fail", reasons) if step == "rewrite"
                           else world.review_outcome("fail", reasons))
                out = world.tick(at(2), publications=[outcome])
                # The next development Work is requested in the same tick and
                # carries the reasons as its feedback.
                self.assertEqual(world.kinds(out), [verb, "request_patch"])
                rejected = next(r for k, r in out.staged if k == "events" and r["kind"] == verb)
                self.assertEqual((rejected["to"]["phase"], rejected["to"]["activity"],
                                  rejected["to"]["revision"]),
                                 ("development", "fix_needed", revision))
                [eid] = world.record["pending_feedback_evidence_ids"]
                self.assertEqual(world.evidence[eid]["data"]["reasons"], reasons)
                self.assertIn(reasons[0], world.evidence[eid]["summary"])
                for name in ("clean_head_sha", "submission_rewrite_id", "submission_review_id",
                             "handoff_request", "pending_human_action", "active_operation_id"):
                    self.assertIsNone(world.record[name], name)
                self.assertEqual(world.record["active_work"]["task"], "development_repair")
                self.assertEqual(world.record["active_work"]["feedback_evidence_ids"], [eid])
                self.assertIn(eid, world.record["active_work"]["reason_ids"])
                # Nothing was posted and no branch was written.
                self.assertEqual((world.gh.comments, world.gh.fork_writes()), ({}, []))
                self.assertEqual(world.gh.branches["riscv64"], None)
                # The ledger entry renders no untrusted reason.
                entry = status_ledger.render(rejected, world.record, "https://run")
                self.assertNotIn("CHANGELOG", entry)

    def test_a_split_the_project_requires_fails_the_rewrite_and_returns_to_development(self):
        """The rewrite may squash and reword; a split into trees the approved
        series never has fails its check, so the split commits are made and
        approved in development."""
        world = submitted()
        reasons = ["commit 2: its tree is not a tree of the approved series (a split)"]
        out = world.tick(at(2), publications=[world.rewrite_outcome("fail", reasons)])
        self.assertEqual(world.kinds(out)[0], "reject_submission_rewrite")
        self.assertEqual(world.state, "development.agent_running")
        [eid] = world.record["pending_feedback_evidence_ids"]
        self.assertEqual(world.evidence[eid]["data"]["reasons"], reasons)
        self.assertEqual(world.gh.branches["riscv64"], None)

    def test_an_unusable_rewrite_or_review_is_a_work_retry(self):
        for step, state in (("rewrite", "handoff.agent_running"), ("review", "handoff.reviewing")):
            with self.subTest(step=step):
                world = submitted() if step == "rewrite" else rewritten()
                make = world.rewrite_outcome if step == "rewrite" else world.review_outcome
                first = world.record["active_work"]["dedupe_key"]
                for outcome in ({"status": "rejected", "failure_class": "validation",
                                 "reason": "the review is malformed"},
                                {"verdict": "pass", "commits": []} if step == "rewrite"
                                else {"text_digest": "0" * 64}):
                    out = world.tick(at(2), publications=[make(**outcome)])
                    self.assertEqual(world.kinds(out), ["allocate_work_retry"])
                    self.assertEqual(world.state, state)
                    self.assertNotEqual(world.record["active_work"]["dedupe_key"], first)
                    first = world.record["active_work"]["dedupe_key"]
                # Once `work_recovery` is spent, a human resumes a fresh rewrite.
                out = world.tick(at(3), publications=[make(verdict="maybe")])
                self.assertEqual(world.kinds(out), ["exhaust_recovery"])
                self.assertEqual((world.state, world.record["pending_human_action"]["kind"]),
                                 ("handoff.waiting_human", "work_failed"))
                revision = world.record["revision"]
                out = world.tick(at(4), comments=[comment(
                    f'/upstreaming resume revision={revision} reason="x"', cid=940)])
                self.assertEqual(world.kinds(out),
                                 ["resume_submission_review", "request_submission_rewrite"])
                self.assertEqual(world.state, "handoff.agent_running")

    def test_a_review_of_another_revision_or_state_is_late(self):
        world = submitted()
        late = world.rewrite_outcome()
        late["task"]["revision"] = 5
        out = world.tick(at(1), publications=[late])
        self.assertEqual(world.kinds(out), ["record_late_effect"])
        self.assertEqual(world.state, "handoff.agent_running")
        world = through("handoff.preparing_submission")
        out = world.tick(at(20), publications=[dict(late, task=dict(late["task"], revision=7),
                                                    publication_artifact_id=1)])
        self.assertEqual(world.kinds(out), ["record_late_effect"])

    def test_ticks_without_receipts_relaunch_and_then_fail_the_attempt(self):
        world = reviewed()
        op_id = world.record["active_operation_id"]
        out = world.tick(at(2))
        self.assertEqual((out.report["handoff_requests"], world.kinds(out)), ([], []))
        out = world.tick(at(7))
        self.assertEqual([r["attempt"] for r in out.report["handoff_requests"]], [2])
        world.tick(at(13))
        out = world.tick(at(15))
        self.assertEqual((out.report["handoff_requests"], world.kinds(out)), ([], []))
        self.assertEqual(world.operations[op_id]["status"], "planned")
        out = world.tick(at(19))
        self.assertEqual(world.kinds(out), ["retry_upstream_branch"])
        failed_op = world.operations[op_id]
        self.assertEqual((failed_op["status"], failed_op["external_ids"]["failure"],
                          failed_op["external_ids"]["launches_spent"]),
                         ("failed", handoff.INFRASTRUCTURE, 3))
        self.assertEqual(world.state, "handoff.building_upstream_branch")
        self.assertEqual(world.active["reason_ids"][1:3], ["attempt:2", "count:2"])
        late = {"schema_version": 1, "operation_id": op_id, "kind": "upstream_branch",
                "contribution_id": CID, "run_id": "9", "result": "applied", "clean_head": CLEAN}
        out = world.tick(at(20), receipts=[late])
        self.assertEqual(world.kinds(out), [])
        self.assertEqual(world.operations[op_id]["status"], "failed")

    def test_a_run_in_flight_is_neither_relaunched_nor_closed(self):
        world = reviewed()
        op_id = world.record["active_operation_id"]
        for minute in (7, 13, 19, 40):
            out = world.tick(at(minute), handoff_runs=[op_id])
            self.assertEqual((out.report["handoff_requests"], world.kinds(out)), ([], []))
            self.assertEqual(recovery.launch_attempts(world.operations[op_id]), 1)
            self.assertIn({"contribution": CID, "operation": op_id,
                           "why": "upstream_branch run 1 is still in flight"},
                          out.report["controller"]["deferred"])
        out = world.tick(at(41), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out)[0], "confirm_clean_head")
        self.assertEqual(world.operations[op_id]["status"], "confirmed")
        self.assertEqual(world.operations[op_id]["reason_ids"][1:3], ["attempt:1", "count:1"])

    def test_spent_launches_are_not_closed_while_the_last_run_is_in_flight(self):
        world = reviewed()
        op_id = world.record["active_operation_id"]
        world.tick(at(7))
        world.tick(at(13))
        self.assertEqual(recovery.launch_attempts(world.operations[op_id]), 3)
        for minute in (19, 30, 50):
            out = world.tick(at(minute), handoff_runs=[op_id])
            self.assertEqual(world.kinds(out), [])
            self.assertEqual(world.operations[op_id]["status"], "planned")
        out = world.tick(at(51))
        self.assertEqual(world.kinds(out), ["retry_upstream_branch"])
        self.assertEqual(world.operations[op_id]["external_ids"]["launches_spent"], 3)

    def test_a_run_in_flight_longer_than_the_bound_is_relaunched_then_spent(self):
        world = reviewed()
        bound = handoff.RUN_SECONDS // 60
        minute, closes = 1, []
        while world.state != "handoff.waiting_human":
            op_id = world.record["active_operation_id"]
            out = world.tick(at(minute + bound - 1), handoff_runs=[op_id])
            self.assertEqual((out.report["handoff_requests"], world.kinds(out)), ([], []))
            minute += bound
            out = world.tick(at(minute), handoff_runs=[op_id])
            if world.kinds(out):
                closes.append(world.kinds(out))
                event = [e for k, e in out.staged if k == "events"][0]
                self.assertIn(f"(run still listed after {handoff.RUN_SECONDS} s)",
                              event["details"]["reason"])
                self.assertEqual(world.operations[op_id]["external_ids"]["launches_spent"], 3)
            else:
                self.assertEqual(len(out.report["handoff_requests"]), 1)
            self.assertLess(minute, 20 * bound)
        self.assertEqual(closes[-1], ["exhaust_upstream_branch_retries"])
        self.assertEqual(world.record["pending_human_action"]["counter"], "upstream_branch")

    def test_an_unread_run_list_holds_for_the_bound_only(self):
        world = reviewed()
        op_id = world.record["active_operation_id"]
        out = world.tick(at(7), handoff_runs=None)
        self.assertEqual(out.report["handoff_requests"], [])
        self.assertIn({"contribution": CID, "operation": op_id,
                       "why": "upstream_branch run status not observed"},
                      out.report["controller"]["deferred"])
        out = world.tick(at(61), handoff_runs=None)
        self.assertEqual([r["attempt"] for r in out.report["handoff_requests"]], [2])

    def test_a_submission_comment_run_in_flight_is_held(self):
        world = through("handoff.preparing_submission")
        op_id = world.record["active_operation_id"]
        self.assertEqual(world.active["kind"], "submission_comment")
        start = datetime.fromisoformat(world.active["updated_at"])
        out = world.tick(start + timedelta(minutes=20), handoff_runs=[op_id])
        self.assertEqual((out.report["handoff_requests"], world.kinds(out)), ([], []))
        self.assertEqual(recovery.launch_attempts(world.operations[op_id]), 1)
        out = world.tick(start + timedelta(minutes=21))
        self.assertEqual([r["attempt"] for r in out.report["handoff_requests"]], [2])

    def test_a_run_of_another_operation_does_not_hold(self):
        world = reviewed()
        out = world.tick(at(7), handoff_runs=["op-" + "0" * 24])
        self.assertEqual([r["attempt"] for r in out.report["handoff_requests"]], [2])

    def test_a_refused_attempt_is_not_folded(self):
        world = reviewed()
        receipt = dict(failed(world), result="refused", failure=None)
        out = world.tick(at(2), receipts=[receipt])
        self.assertEqual(world.kinds(out), [])
        self.assertEqual(world.active["status"], "planned")
        self.assertEqual(out.report["handoffs"][0]["outcome"], "not folded: refused")

    def test_a_receipt_of_another_operation_or_a_mismatched_readback_is_not_folded(self):
        world = reviewed()
        other = dict(world.run_workflow(), operation_id="op-" + "0" * 24)
        self.assertEqual(world.kinds(world.tick(at(2), receipts=[other])), [])
        receipt = {"schema_version": 1, "operation_id": world.active["operation_id"],
                   "kind": "upstream_branch", "contribution_id": CID, "run_id": "1",
                   "result": "applied", "clean_head": CLEAN}
        cases = {"another head": {"branches": {"riscv64-dev": HEAD, "riscv64": "d" * 40}},
                 "another tree": {"trees": {HEAD: TREE, CLEAN: "8" * 40}},
                 "another message": {"messages": {CLEAN: "Other"}}}
        for name, change in cases.items():
            with self.subTest(case=name):
                w = copy.deepcopy(world)
                w.gh.branches["riscv64"] = CLEAN
                for attr, value in change.items():
                    setattr(w.gh, attr, value)
                out = w.tick(at(3), receipts=[receipt])
                self.assertEqual(w.kinds(out), [])
                self.assertEqual(out.report["handoffs"][0]["outcome"], "readback mismatch")
                self.assertEqual(w.state, "handoff.building_upstream_branch")


class TestRewriteExpiry(unittest.TestCase):
    """The rewritten series lives in the rewrite run's publication artifact,
    kept 30 days: an expired one leads back to a fresh rewrite, never to an
    endless wait."""

    @staticmethod
    def age(world, days):
        eid = world.record["submission_rewrite_id"]
        evidence = world.evidence[eid]
        stamp = datetime.fromisoformat(evidence["collected_at"]) - timedelta(days=days)
        world.evidence[eid] = dict(evidence, collected_at=stamp.isoformat())

    def test_the_expiry_follows_the_retention_the_worker_lock_uses(self):
        """The generated lock keeps the publication artifact for the
        repository variable's days when it is set; the controller's
        workflow hands that value to the tick."""
        world = rewritten()
        for value, fresh, stale in (("10", 8.9, 9), ("1", 0.4, 0.5), ("90", 88.9, 89),
                                    (None, 28.9, 29), ("0", 28.9, 29), ("x", 28.9, 29),
                                    ("401", 28.9, 29)):
            with self.subTest(value=value), mock.patch.dict(
                    "os.environ", {} if value is None else {handoff.RETENTION_ENV: value}):
                if value is None:
                    os.environ.pop(handoff.RETENTION_ENV, None)
                for days, expired in ((fresh, False), (stale, True)):
                    copy_ = dict(world.evidence)
                    eid = world.record["submission_rewrite_id"]
                    stamp = datetime.fromisoformat(copy_[eid]["collected_at"]) \
                        - timedelta(days=days)
                    copy_[eid] = dict(copy_[eid], collected_at=stamp.isoformat())
                    self.assertEqual(handoff.rewrite_expired(
                        world.record, copy_, datetime.fromisoformat(
                            world.evidence[eid]["collected_at"])), expired, days)

    @staticmethod
    def expiring(world, expires_at):
        eid = world.record["submission_rewrite_id"]
        evidence = copy.deepcopy(world.evidence)
        data = evidence[eid]["data"]
        data["artifact"] = dict(data["artifact"], expires_at=expires_at)
        return evidence, datetime.fromisoformat(evidence[eid]["collected_at"])

    def test_the_expiry_is_the_earliest_of_the_reported_expiry_and_the_retention(self):
        world = rewritten()
        eid = world.record["submission_rewrite_id"]
        collected = datetime.fromisoformat(world.evidence[eid]["collected_at"])
        with mock.patch.dict("os.environ", {}):
            os.environ.pop(handoff.RETENTION_ENV, None)
            for reported_days, days, expired in ((7, 5.9, False), (7, 6, True),
                                                 (90, 28.9, False), (90, 29, True)):
                with self.subTest(reported=reported_days, days=days):
                    evidence, _ = self.expiring(
                        world, (collected + timedelta(days=reported_days)).isoformat())
                    self.assertEqual(handoff.rewrite_expired(
                        world.record, evidence, collected + timedelta(days=days)), expired)
                    self.assertEqual(handoff.rewrite_expired(
                        world.record, evidence, collected + timedelta(days=days), review=True),
                        expired)
            # A Z suffix is read, and an invalid expiry is ignored.
            evidence, _ = self.expiring(world, (collected + timedelta(days=7)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"))
            self.assertTrue(handoff.rewrite_expired(world.record, evidence,
                                                    collected + timedelta(days=6)))
            for invalid in ("soon", "", None, 12):
                with self.subTest(invalid=invalid):
                    evidence, _ = self.expiring(world, invalid)
                    self.assertFalse(handoff.rewrite_expired(
                        world.record, evidence, collected + timedelta(days=28.9)))
                    self.assertTrue(handoff.rewrite_expired(
                        world.record, evidence, collected + timedelta(days=29)))
            # A rewrite recorded without a reported expiry keeps the age rule.
            self.assertFalse(handoff.rewrite_expired(world.record, world.evidence,
                                                     collected + timedelta(days=28.9)))

    def test_the_rewrite_evidence_records_the_reported_expiry(self):
        world = submitted()
        world.tick(at(1), publications=[world.rewrite_outcome(
            expires_at="2026-12-20T09:00:00Z")])
        data = world.evidence[world.record["submission_rewrite_id"]]["data"]
        self.assertEqual(data["artifact"]["expires_at"], "2026-12-20T09:00:00Z")
        world = submitted()
        world.tick(at(1), publications=[world.rewrite_outcome()])
        data = world.evidence[world.record["submission_rewrite_id"]]["data"]
        self.assertIsNone(data["artifact"]["expires_at"])

    def test_a_reported_expiry_leads_back_to_a_fresh_rewrite(self):
        world = rewritten()
        world.record = dict(world.record, activity="review_pending", active_work=None)
        eid = world.record["submission_rewrite_id"]
        collected = datetime.fromisoformat(world.evidence[eid]["collected_at"])
        world.evidence, _ = self.expiring(world, (collected + timedelta(days=2)).isoformat())
        self.assertFalse(handoff.rewrite_expired(world.record, world.evidence, at(2)))
        late = collected + timedelta(days=1.1)
        self.assertTrue(handoff.rewrite_expired(world.record, world.evidence, late))
        out = world.tick(late)
        self.assertEqual(world.kinds(out), ["expire_submission_rewrite"])
        self.assertEqual(world.state, "handoff.review_due")

    def test_an_expired_rewrite_is_never_reviewed(self):
        world = rewritten()
        # The review is due but not allocated yet (no worker slot).
        world.record = dict(world.record, activity="review_pending", active_work=None)
        self.age(world, 28)
        self.assertFalse(handoff.rewrite_expired(world.record, world.evidence, at(2)))
        self.age(world, 1)
        self.assertTrue(handoff.rewrite_expired(world.record, world.evidence, at(2)))
        out = world.tick(at(2))
        self.assertEqual(world.kinds(out), ["expire_submission_rewrite"])
        self.assertEqual((world.state, world.record["submission_rewrite_id"]),
                         ("handoff.review_due", None))
        out = world.tick(at(3))
        self.assertEqual(world.kinds(out), ["request_submission_rewrite"])

    def test_a_long_human_wait_before_the_build_leads_to_a_fresh_rewrite(self):
        world = reviewed()
        out = world.tick(at(2), receipts=[failed(world, handoff.BLOCKED)])
        self.assertEqual(world.kinds(out), ["require_branch_intervention"])
        self.assertEqual(world.state, "handoff.waiting_human")
        self.age(world, 40)
        out = world.tick(at(3), comments=[comment(
            f'/upstreaming resume revision={world.record["revision"]} reason="closed"', cid=990)])
        # The fresh rewrite is requested in the same tick.
        self.assertEqual(world.kinds(out), ["resume_upstream_branch", "expire_submission_rewrite",
                                            "request_submission_rewrite"])
        self.assertEqual(world.state, "handoff.agent_running")
        for name in ("submission_rewrite_id", "submission_review_id"):
            self.assertIsNone(world.record[name], name)
        self.assertEqual(world.gh.fork_writes(), [])


class TestInfrastructureRetries(unittest.TestCase):
    """Each handoff operation: a failed attempt is retried within its budget,
    the exhaustion names the budget in a human wait, and the resume starts
    that budget again."""

    CASES = (("handoff.building_upstream_branch", "upstream_branch", "upstream_branch_failed",
              "retry_upstream_branch", "exhaust_upstream_branch_retries", "resume_upstream_branch"),
             ("handoff.preparing_submission", "submission_comment", "submission_comment_failed",
              "retry_submission_comment", "exhaust_comment_retries",
              "resume_submission_preparation"))

    def test_retry_exhaustion_and_resume(self):
        limit = lifecycle.budget_limit("upstream_branch", policy())
        self.assertEqual(limit, 3)
        for state, kind, pending, retry, exhaust, resume in self.CASES:
            with self.subTest(kind=kind):
                world = through(state)
                minute = 20
                for attempt in range(1, limit + 1):
                    op = world.active
                    self.assertEqual(op["reason_ids"][1:3], [f"attempt:{attempt}", f"count:{attempt}"])
                    out = world.tick(at(minute), receipts=[failed(world)])
                    minute += 1
                    self.assertEqual(world.operations[op["operation_id"]]["status"], "failed")
                    if attempt < limit:
                        self.assertEqual(world.kinds(out), [retry])
                        self.assertEqual(world.state, state)
                        self.assertEqual(world.events[-1]["details"]["charged"], [kind])
                        self.assertNotEqual(world.record["active_operation_id"], op["operation_id"])
                        self.assertEqual(len(out.report["handoff_requests"]), 1)
                    else:
                        self.assertEqual(world.kinds(out), [exhaust])
                self.assertEqual(world.state, "handoff.waiting_human")
                self.assertEqual((world.record["pending_human_action"]["kind"],
                                  world.record["pending_human_action"]["counter"]), (pending, kind))
                self.assertIsNone(world.record["active_operation_id"])
                self.assertEqual(world.tick(at(minute + 10)).report["handoff_requests"], [])
                revision = world.record["revision"]
                out = world.tick(at(minute + 11), comments=[comment(
                    f'/upstreaming resume revision={revision} reason="fixed"', cid=950)])
                self.assertEqual(world.kinds(out), [resume])
                self.assertEqual(world.events[-1]["details"]["restarted"],
                                 {"counter": kind, "count": limit})
                self.assertEqual((world.state, world.record["revision"]), (state, revision + 1))
                self.assertEqual(world.active["reason_ids"][1:3], [f"attempt:{limit + 1}", "count:1"])
                out = world.tick(at(minute + 12), receipts=[world.run_workflow()])
                self.assertNotEqual(world.state, state)

    def test_a_build_that_cannot_be_admitted_returns_to_development(self):
        world = reviewed()
        out = world.tick(at(2), receipts=[failed(world, handoff.INVALID)])
        self.assertEqual(world.kinds(out), ["return_for_repair", "request_patch"])
        self.assertEqual((world.state, world.record["revision"]), ("development.agent_running", 8))
        for name in ("clean_head_sha", "submission_review_id", "handoff_request",
                     "pending_human_action", "active_operation_id"):
            self.assertIsNone(world.record[name], name)
        self.assertEqual(world.gh.fork_writes(), [])

    def test_an_open_upstream_pr_from_riscv64_waits_for_a_human(self):
        world = reviewed()
        world.gh.upstream = [upstream_pr(head={"sha": "e" * 40})]
        receipt = world.run_workflow()
        self.assertEqual((receipt["result"], receipt["failure"]), ("failed", handoff.BLOCKED))
        out = world.tick(at(2), receipts=[receipt])
        self.assertEqual(world.kinds(out), ["require_branch_intervention"])
        self.assertEqual((world.state, world.record["pending_human_action"]["kind"]),
                         ("handoff.waiting_human", "upstream_branch_blocked"))
        self.assertIsNone(world.record["pending_human_action"]["counter"])
        self.assertEqual(world.gh.fork_writes(), [])
        # Closed by a human; the resume builds `riscv64`.
        world.gh.upstream = []
        revision = world.record["revision"]
        out = world.tick(at(3), comments=[comment(
            f'/upstreaming resume revision={revision} reason="closed"', cid=951)])
        self.assertEqual(world.kinds(out), ["resume_upstream_branch"])
        out = world.tick(at(4), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out)[0], "confirm_clean_head")

    def test_an_invalid_failure_is_never_charged_to_the_budget(self):
        world = reviewed()
        for minute in (2, 3):
            world.tick(at(minute), receipts=[failed(world)])
        self.assertEqual(world.active["reason_ids"][2], "count:3")
        out = world.tick(at(4), receipts=[failed(world, handoff.INVALID)])
        self.assertEqual(world.kinds(out)[0], "return_for_repair")


class TestSubmitWithoutAFreeSlot(unittest.TestCase):
    def test_the_approval_is_recorded_and_the_rewrite_waits_for_a_slot(self):
        busy = policy()
        busy["concurrency"] = dict(busy["concurrency"], coding_workers=0)
        world = World(p=busy)
        out = world.tick(at(0), comments=[comment(SUBMIT)])
        self.assertEqual(world.kinds(out), ["submit_upstream"])
        self.assertEqual(world.state, "handoff.review_due")
        self.assertEqual(world.record["handoff_approval"]["evidence_ids"], [CI_EVIDENCE, PLAN])
        self.assertIsNone(world.record["active_operation_id"])
        self.assertEqual([o for o in world.operations.values()
                          if o["kind"] == "upstream_branch"], [])
        self.assertEqual(out.report["requested_effects"], [])
        world.policy = policy()
        out = world.tick(at(1))
        self.assertEqual(world.kinds(out), ["request_submission_rewrite"])
        self.assertEqual(world.state, "handoff.agent_running")


class TestCandidateWorkflow(unittest.TestCase):
    def test_only_the_workflow_of_the_recorded_evidence_is_dispatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".state/evidence").mkdir(parents=True)

            def write(path):
                (root / ".state/evidence/ci-1.json").write_text(
                    json.dumps({"ci": {"workflow_path": path}}))

            record = {"last_ci_evidence_id": "ci-1"}
            write(".github/workflows/build.yml")
            self.assertEqual(handoff._candidate_workflow(root, record),
                             ".github/workflows/build.yml")
            write("../../etc/passwd")
            self.assertIsNone(handoff._candidate_workflow(root, record))
            self.assertIsNone(handoff._candidate_workflow(root, {"last_ci_evidence_id": None}))


class TestCleanHeadCiDispatch(unittest.TestCase):
    """The trusted build dispatches the project's required CI workflow on
    `riscv64` when it can, and reads its run back by head."""

    FORK = "riseproject-upstreaming/proj"
    PATH = ".github/workflows/build.yml"

    class Api:
        def __init__(self, text, runs=()):
            import base64
            self.content = {"content": base64.b64encode(text.encode()).decode()}
            self.runs = list(runs)
            self.paths = []

        def get(self, path):
            self.paths.append(path)
            if "/contents/" in path:
                return self.content
            return {"workflow_runs": [dict(r) for r in self.runs]}

    def go(self, api, dispatch=None):
        return handoff.dispatch_clean_head_ci(api, fork=self.FORK, head=CLEAN,
                                              workflow_path=self.PATH, dispatch=dispatch,
                                              wait=lambda _s: None, readbacks=2)

    def test_dispatchable_workflows_are_read_as_data(self):
        yes = ["on: workflow_dispatch\n", "on: [push, workflow_dispatch]\n",
               "on:\n  workflow_dispatch:\n  pull_request:\n",
               "on:\n  workflow_dispatch:\n    inputs:\n      x:\n        default: a\n"
               "        required: true\n"]
        no = ["on: pull_request\n", "on:\n  pull_request:\n",
              "on:\n  workflow_dispatch:\n    inputs:\n      x:\n        required: true\n",
              "!!python/object/apply:os.system ['x']", "[", "x" * (600 * 1024)]
        for text in yes:
            self.assertTrue(handoff.workflow_dispatchable(text), text)
        for text in no:
            self.assertFalse(handoff.workflow_dispatchable(text), text[:40])

    def test_dispatch_and_readback(self):
        api = self.Api("on: [pull_request, workflow_dispatch]\n")
        calls = []

        def dispatch(path, body):
            calls.append((path, body))
            api.runs = [{"id": 7, "head_sha": CLEAN}]

        out = self.go(api, dispatch)
        self.assertEqual(out, {"ci_dispatch": handoff.DISPATCHED, "ci_workflow": self.PATH,
                               "ci_run_id": 7})
        self.assertEqual(calls, [(f"/repos/{self.FORK}/actions/workflows/build.yml/dispatches",
                                  {"ref": "refs/heads/riscv64"})])
        self.assertIn(f"/repos/{self.FORK}/contents/.github/workflows/build.yml?ref={CLEAN}",
                      api.paths)
        # A run that already exists for the head is not dispatched again.
        out = self.go(self.Api("on: workflow_dispatch\n", runs=[{"id": 8, "head_sha": CLEAN}]),
                       lambda *_a: self.fail("dispatched again"))
        self.assertEqual(out["ci_dispatch"], handoff.EXISTS)

    def test_what_cannot_be_dispatched_or_read_back(self):
        out = self.go(self.Api("on: pull_request\n"), lambda *_a: self.fail("dispatched"))
        self.assertEqual(out["ci_dispatch"], handoff.UNSUPPORTED)
        self.assertEqual(handoff.dispatch_clean_head_ci(
            self.Api(""), fork=self.FORK, head=CLEAN, workflow_path=None,
            dispatch=None)["ci_dispatch"], handoff.UNSUPPORTED)
        with self.assertRaisesRegex(handoff.HandoffError, "no run"):
            self.go(self.Api("on: workflow_dispatch\n"), lambda *_a: None)
        with self.assertRaisesRegex(handoff.HandoffError, "dispatch failed"):
            self.go(self.Api("on: workflow_dispatch\n"),
                     lambda *_a: (_ for _ in ()).throw(RuntimeError("HTTP 403")))
        # A final check that refuses before the dispatch is not a failure of
        # the attempt: nothing was written and nothing is charged.
        with self.assertRaises(handoff.HandoffRefused):
            self.go(self.Api("on: workflow_dispatch\n"), refuse)
        with self.assertRaisesRegex(handoff.HandoffError, "needs the CI token"):
            self.go(self.Api("on: workflow_dispatch\n"), None)


class TestCleanHeadCi(unittest.TestCase):
    def test_a_failure_or_a_moved_head_returns_for_repair(self):
        world = reviewed()
        gate_evidence = world.record["last_ci_evidence_id"]
        world.gh.ci_state = "failure"
        # A failure after a pass is a new attempt: its evidence has a new id.
        world.gh.ci_attempt = 2
        out = world.tick(at(2), receipts=[world.run_workflow()])
        # The repair is requested in the same tick, on `riscv64-dev`.
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "return_for_repair",
                                            "request_repair"])
        self.assertEqual(world.state, "development.agent_running")
        self.assertEqual(world.record["active_work"]["task"], "development_repair")
        failed = world.record["last_ci_evidence_id"]
        self.assertNotEqual(failed, gate_evidence)
        self.assertEqual(world.evidence[failed]["ci"]["head_sha"], HEAD)
        self.assertNotIn("candidate_head_sha", world.evidence[failed]["ci"])
        self.assertIn(failed, world.record["active_work"]["reason_ids"])
        world = reviewed()
        receipt = world.run_workflow()
        with mock.patch.object(FakeGitHub, "ci_snapshot", lambda self, a, f, h: collect.CiSnapshot(
                repository={"id": 1, "full_name": f}, head_sha=h, runs=[], jobs_by_run={},
                artifacts_by_run={})):
            out = world.tick(at(2), receipts=[receipt])
        self.assertEqual(world.kinds(out), ["confirm_clean_head"])
        self.assertEqual(world.state, "handoff.waiting_ci")
        # `riscv64-dev` moving changes nothing here; `riscv64` moving does.
        world.gh.branches["riscv64-dev"] = "b" * 40
        with mock.patch.object(FakeGitHub, "ci_snapshot", lambda self, a, f, h: collect.CiSnapshot(
                repository={"id": 1, "full_name": f}, head_sha=h, runs=[], jobs_by_run={},
                artifacts_by_run={})):
            self.assertEqual(world.kinds(world.tick(at(3))), [])
        world.gh.branches["riscv64"] = "e" * 40
        kept = world.record["last_ci_evidence_id"]
        out = world.tick(at(4))
        # No CI verdict: the passing evidence of the candidate head is kept.
        self.assertEqual(world.kinds(out), ["return_for_repair", "request_patch"])
        returned = next(e for e in world.events if e["kind"] == "return_for_repair")
        self.assertEqual(returned["details"]["head"], "e" * 40)
        self.assertEqual(returned["details"]["kept_evidence_id"], kept)
        self.assertEqual(world.record["last_ci_evidence_id"], kept)

    def test_a_failure_on_another_tree_records_its_evidence(self):
        world = submitted()
        gate_evidence = world.record["last_ci_evidence_id"]
        world.tick(at(1), publications=[world.rewrite_outcome(clean_tree="9" * 40, commits=[
            {"message": REVIEWED_MESSAGE, "tree": "9" * 40}])])
        world.tick(at(1), publications=[world.review_outcome()])
        world.gh.trees[CLEAN] = "9" * 40
        world.gh.ci_state = "failure"
        world.gh.ci_attempt = 3
        out = world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "return_for_repair",
                                            "request_repair"])
        self.assertEqual(world.gh.ci_heads[-1], CLEAN)
        returned = next(e for e in world.events if e["kind"] == "return_for_repair")
        failed = world.record["last_ci_evidence_id"]
        self.assertEqual(failed, returned["details"]["evidence_id"])
        self.assertNotEqual(failed, gate_evidence)
        ci = world.evidence[failed]["ci"]
        self.assertEqual((ci["head_sha"], ci["candidate_head_sha"]),
                         (CLEAN, world.record["fork"]["head_sha"]))
        self.assertTrue(world.evidence[failed]["limitations"][0].startswith(
            f"CI ran on the clean head {CLEAN} of `riscv64`"))
        self.assertIn(failed, world.record["active_work"]["reason_ids"])
        self.assertEqual(world.record["budget"]["clusters"]
                         .get("Build - ubuntu-24.04-riscv (riscv64)"), 1)

    def test_another_tree_needs_ci_on_the_clean_head(self):
        world = submitted()
        world.tick(at(1), publications=[world.rewrite_outcome(clean_tree="9" * 40, commits=[
            {"message": REVIEWED_MESSAGE, "tree": "9" * 40}])])
        world.tick(at(1), publications=[world.review_outcome()])
        world.gh.trees[CLEAN] = "9" * 40
        out = world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "accept_clean_head_ci"])
        self.assertEqual(world.gh.ci_heads[-1], CLEAN)
        self.assertEqual(world.evidence[world.record["last_ci_evidence_id"]]["ci"]["head_sha"], CLEAN)

    LOST = [{"annotation_level": "failure",
             "message": "The runner has received a shutdown signal."}]

    def rerun_of(self, world):
        op = world.active
        self.assertEqual((op["kind"], op["status"]), ("ci_rerun", "planned"))
        return op

    def test_a_cancelled_job_is_rerun_on_the_ci_head(self):
        world = reviewed()
        world.gh.ci_state = "cancelled"
        out = world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "rerun_interrupted_ci"])
        self.assertEqual(world.state, "handoff.waiting_ci")
        self.assertEqual(engine.head_reruns(world.record)["interrupted"], 1)
        self.assertEqual(world.record["clean_head_ci_since"], at(2).isoformat())
        self.assertIsNone(world.record["next_poll_at"])
        # The reviewed series has the tree of the candidate head: its CI,
        # on `riscv64-dev`, is CI on the clean head.
        op = self.rerun_of(world)
        self.assertEqual((op["target"]["ref"], op["target"]["expected_head"]),
                         ("riscv64-dev", HEAD))
        # The handoff workflow runs it, not the development PR workflow.
        [request] = out.report["controller"]["handoff_requests"]
        self.assertEqual((request["operation_id"], request["kind"]),
                         (op["operation_id"], "ci_rerun"))
        self.assertEqual(out.report["controller"]["dev_pr_requests"], [])
        # The receipt is read back: the operation is confirmed and released,
        # and the snapshot of the new attempt decides.
        receipt = world.run_rerun()
        self.assertEqual((receipt["status"], receipt["runs"][0]["run_attempt"]), ("rerun", 2))
        world.gh.ci_state = "success"
        out = world.tick(at(3), ci_reruns=[receipt])
        self.assertEqual(world.operations[op["operation_id"]]["status"], "confirmed")
        self.assertEqual(world.kinds(out)[:2], ["ci_rerun_recorded", "accept_clean_head_ci"])
        self.assertEqual(world.state, "handoff.preparing_submission")

    def test_a_snapshot_behind_the_rerun_and_passing_ci_behind_an_open_one_decide_nothing(self):
        world = reviewed()
        world.gh.ci_state = "cancelled"
        world.tick(at(2), receipts=[world.run_workflow()])
        op = self.rerun_of(world)
        # Passing CI while the rerun is still open keeps the wait.
        world.gh.ci_state = "success"
        out = world.tick(at(3))
        self.assertEqual(world.kinds(out), [])
        self.assertIn("accept_deferred", out.report["observation"][0])
        receipt = world.run_rerun()
        world.gh.ci_state = "cancelled"
        snapshot = FakeGitHub.ci_snapshot

        def behind(gh, api, name, head):
            snap = snapshot(gh, api, name, head)
            snap.runs[0]["run_attempt"] = 1
            return snap

        # The new attempt is read back, but the snapshot still shows the
        # attempt before the rerun: nothing is decided from it.
        with mock.patch.object(FakeGitHub, "ci_snapshot", behind):
            out = world.tick(at(4), ci_reruns=[receipt])
        self.assertEqual(world.kinds(out), ["ci_rerun_recorded"])
        self.assertEqual(world.operations[op["operation_id"]]["status"], "confirmed")
        self.assertEqual(out.report["observation"][0]["note"],
                         "CI on the clean head is behind a confirmed rerun")
        self.assertEqual(world.state, "handoff.waiting_ci")

    def test_a_lost_runner_is_rerun_and_a_failure_returns_for_repair(self):
        world = reviewed()
        world.gh.ci_state = "failure"
        world.gh.annotations[1] = self.LOST
        out = world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "rerun_interrupted_ci"])
        # Interrupted and failed: the failure returns for repair.
        world = reviewed()
        world.gh.ci_state = "cancelled"
        world.gh.ci_extra_jobs = [("Lint", "failure")]
        out = world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "return_for_repair",
                                            "request_repair"])

    def test_the_budget_then_the_resume_restarts_the_count_and_the_wait(self):
        world = reviewed()
        world.gh.ci_state = "cancelled"
        world.tick(at(2), receipts=[world.run_workflow()])
        confirmed = world.record["clean_head_ci_since"]
        minute = 3
        for _ in range(lifecycle.budget_limit("interrupted_reruns", world.policy) - 1):
            receipt = world.run_rerun()
            out = world.tick(at(minute), ci_reruns=[receipt])
            self.assertEqual(world.kinds(out), ["ci_rerun_recorded", "rerun_interrupted_ci"])
            minute += 1
        receipt = world.run_rerun()
        out = world.tick(at(minute), ci_reruns=[receipt])
        self.assertEqual(world.kinds(out), ["ci_rerun_recorded", "exhaust_interrupted_reruns"])
        self.assertEqual(world.state, "handoff.waiting_human")
        pending = world.record["pending_human_action"]
        self.assertEqual((pending["kind"], pending["counter"]),
                         ("ci_interrupted", "interrupted_reruns"))
        # The resume, 13 hours after the confirm: the counter and the
        # bounded wait start again, and the next observation reruns.
        late = at(2 + 13 * 60)
        out = world.tick(late, comments=[comment(
            f'/upstreaming resume revision={world.record["revision"]} reason="runners back"',
            cid=993)])
        self.assertEqual(world.kinds(out), ["resume_ci"])
        self.assertEqual(engine.head_reruns(world.record)["interrupted"], 0)
        self.assertEqual(world.record["clean_head_ci_since"], late.isoformat())
        self.assertNotEqual(confirmed, late.isoformat())
        out = world.tick(late + timedelta(minutes=1))
        self.assertEqual(world.kinds(out), ["rerun_interrupted_ci"])
        self.assertEqual(world.state, "handoff.waiting_ci")

    def test_paused_or_an_open_operation_plans_no_rerun(self):
        world = reviewed()
        world.gh.ci_state = "cancelled"
        world.tick(at(2), receipts=[world.run_workflow()])
        op = self.rerun_of(world)
        out = world.tick(at(3))
        self.assertEqual(world.kinds(out), [])
        self.assertIn(op["operation_id"], out.report["observation"][0]["note"])
        out = world.tick(at(4), comments=[comment(
            f'/upstreaming pause revision={world.record["revision"]} reason="x"', cid=994)])
        self.assertIn("pause", world.kinds(out))
        # The paused rerun is closed and nothing new is planned.
        self.assertEqual(world.operations[op["operation_id"]]["status"], "failed")
        self.assertIsNone(world.record["active_operation_id"])
        out = world.tick(at(5))
        self.assertEqual(world.kinds(out), [])
        self.assertFalse(any(o["kind"] == "ci_rerun" and o["status"] == "planned"
                             for o in world.operations.values()))

    def test_interrupted_ci_with_no_completed_run_waits_twelve_hours(self):
        world = reviewed()
        world.gh.ci_state = "cancelled"
        snapshot = FakeGitHub.ci_snapshot

        def running(gh, api, name, head):
            snap = snapshot(gh, api, name, head)
            snap.runs[0]["status"] = "in_progress"
            return snap

        with mock.patch.object(FakeGitHub, "ci_snapshot", running):
            out = world.tick(at(2), receipts=[world.run_workflow()])
            self.assertEqual(world.kinds(out), ["confirm_clean_head"])
            out = world.tick(at(3 + 12 * 60))
        self.assertEqual(world.kinds(out), ["require_clean_head_ci"])

    def other_tree(self):
        """The live record whose rewritten series has another tree than the
        candidate head: CI must run on the clean head of `riscv64`."""
        world = submitted()
        world.tick(at(1), publications=[world.rewrite_outcome(clean_tree="9" * 40, commits=[
            {"message": REVIEWED_MESSAGE, "tree": "9" * 40}])])
        world.tick(at(1), publications=[world.review_outcome()])
        world.gh.trees[CLEAN] = "9" * 40
        return world

    @staticmethod
    def no_runs(world):
        world.gh.ci_snapshot = lambda _api, name, head: collect.CiSnapshot(
            repository={"id": FORK["repository_id"], "full_name": name}, head_sha=head, runs=[],
            jobs_by_run={}, artifacts_by_run={})

    def test_a_workflow_that_cannot_be_dispatched_waits_for_a_human_at_once(self):
        world = self.other_tree()
        self.no_runs(world)
        receipt = dict(world.run_workflow(), ci_dispatch=handoff.UNSUPPORTED,
                       ci_workflow=".github/workflows/lint.yml")
        out = world.tick(at(2), receipts=[receipt])
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "require_clean_head_ci"])
        self.assertEqual((world.state, world.record["pending_human_action"]["kind"]),
                         ("handoff.waiting_human", "clean_head_ci_missing"))
        build = next(op for op in world.operations.values() if op["kind"] == "upstream_branch")
        self.assertEqual(build["external_ids"]["ci_dispatch"], handoff.UNSUPPORTED)
        # Reminders apply; the resume observes CI on the clean head again.
        out = world.tick(at(0, "2026-11-27"))
        self.assertEqual(world.kinds(out), ["record_reminder"])
        world.gh.ci_snapshot = FakeGitHub.ci_snapshot.__get__(world.gh)
        out = world.tick(at(1, "2026-11-27"), comments=[comment(
            f'/upstreaming resume revision={world.record["revision"]} reason="ran CI"', cid=991)])
        self.assertEqual(world.kinds(out), ["resume_ci"])
        out = world.tick(at(2, "2026-11-27"))
        self.assertEqual(world.kinds(out)[0], "accept_clean_head_ci")
        self.assertEqual(world.state, "handoff.preparing_submission")

    def test_a_dispatched_run_that_never_completes_waits_after_the_bounded_wait(self):
        world = self.other_tree()
        self.no_runs(world)
        out = world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head"])
        since = world.record["clean_head_ci_since"]
        self.assertEqual(world.kinds(world.tick(at(2 + 11 * 60))), [])
        out = world.tick(at(3 + 12 * 60))
        self.assertEqual(world.kinds(out), ["require_clean_head_ci"])
        out = world.tick(at(4 + 12 * 60), comments=[comment(
            f'/upstreaming resume revision={world.record["revision"]} reason="x"', cid=992)])
        self.assertEqual(world.kinds(out), ["resume_ci"])
        self.assertNotEqual(world.record["clean_head_ci_since"], since)
        self.assertEqual(world.state, "handoff.waiting_ci")


class TestCleanHeadRerun(unittest.TestCase):
    def test_another_tree_reruns_on_riscv64_fenced_by_the_clean_head(self):
        world = TestCleanHeadCi.other_tree(None)
        world.gh.ci_state = "cancelled"
        out = world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "rerun_interrupted_ci"])
        op = world.active
        self.assertEqual((op["target"]["ref"], op["target"]["expected_head"]), ("riscv64", CLEAN))
        # A moved `riscv64-dev` does not block it; a moved `riscv64` is stale.
        world.gh.branches["riscv64-dev"] = "b" * 40
        self.assertEqual(world.run_rerun()["status"], "rerun")
        world.gh.ci_attempt = 1
        world.gh.branches["riscv64"] = "e" * 40
        self.assertEqual(world.run_rerun()["status"], "stale")


    def test_only_the_current_clean_head_intent_runs(self):
        world = reviewed()
        world.gh.ci_state = "cancelled"
        world.tick(at(2), receipts=[world.run_workflow()])
        op_id = world.record["active_operation_id"]
        good = copy.deepcopy(world.record)
        op = copy.deepcopy(world.active)
        with tempfile.TemporaryDirectory() as directory:
            root = world.root(Path(directory))
            self.assertEqual(ci_rerun.load_request(root, op_id, world.policy, phase="handoff")[1],
                             op)
            with self.assertRaisesRegex(ci_rerun.RerunError, "current authorized"):
                ci_rerun.load_request(root, op_id, world.policy)
        refusals = [
            ({"paused": True, "paused_reason": "x", "paused_by": "human", "paused_at": T0}, {}),
            ({"revision": good["revision"] + 1}, {}),
            ({"phase": "handoff", "activity": "preparing_submission"}, {}),
            ({}, {"expected_head": CLEAN}),
            ({}, {"ref": "riscv64"}),
        ]
        for change, target in refusals:
            with self.subTest(change=sorted(change), target=sorted(target)):
                world.record = dict(good, **change)
                world.operations[op_id] = dict(op, target=dict(op["target"], **target))
                with tempfile.TemporaryDirectory() as directory:
                    root = world.root(Path(directory))
                    with self.assertRaises(ci_rerun.RerunError):
                        ci_rerun.load_request(root, op_id, world.policy, phase="handoff")
        world.record, world.operations[op_id] = good, op

    def test_the_controller_closes_a_rerun_that_no_longer_matches(self):
        import controller

        world = reviewed()
        world.gh.ci_state = "cancelled"
        world.tick(at(2), receipts=[world.run_workflow()])
        op = world.active
        spent = lambda record, attempts=1, waited=False: controller._spent_rerun(  # noqa: E731
            record, op, attempts, waited, world.policy, world.evidence)
        self.assertIsNone(spent(world.record))
        self.assertEqual(spent(dict(world.record, paused=True)), "the contribution is paused")
        self.assertEqual(spent(world.record, attempts=3, waited=True),
                         "operation_launch is spent without a receipt")
        self.assertEqual(spent(dict(world.record, clean_head_sha="e" * 40,
                                    submission_review_id=None)),
                         "the operation no longer matches the record")
        # `riscv64` moved: `return_for_repair`, and the open rerun is closed
        # in the same tick.
        world.gh.branches["riscv64"] = "e" * 40
        out = world.tick(at(3))
        self.assertEqual(world.kinds(out)[0], "return_for_repair")
        self.assertEqual(world.operations[op["operation_id"]]["status"], "failed")
        self.assertEqual(out.report["controller"]["ci_rerun_closures"][0]["closed"],
                         "the operation no longer matches the record")


class TestLockedWait(unittest.TestCase):
    def test_the_table_gives_it_exactly_three_exits(self):
        exits = sorted(e.verb for e in lifecycle.state_edges("handoff.waiting_upstream_pr")
                       if e.target != "handoff.waiting_upstream_pr")
        self.assertEqual(exits, ["refresh_planning", "retract_submission", "verify_upstream_pr"])
        self.assertEqual(lifecycle.LIFECYCLE["handoff.waiting_upstream_pr"].kind, "locked")

    def test_nothing_else_leaves_it(self):
        world = through("handoff.waiting_upstream_pr")
        before = copy.deepcopy(world.record)
        revision = before["revision"]
        refused = {
            f'/upstreaming park revision={revision} reason="x"': "does not apply",
            f'/upstreaming abandon revision={revision} reason="x"': "does not apply",
            f'/upstreaming resume revision={revision} reason="x"': "not paused",
            f"/upstreaming submit-upstream revision={revision} head={HEAD}": "does not apply",
            f"/upstreaming retract-pr revision={revision} head={HEAD}": "not the clean head",
            f'/upstreaming refresh revision={revision} reason="x"': "not the clean head",
        }
        for n, (body, why) in enumerate(refused.items()):
            with self.subTest(body=body):
                out = world.tick(at(30 + n), comments=[comment(body, cid=960 + n)])
                receipt = next(r for k, r in out.staged if k == "commands")
                self.assertEqual(receipt["result"], "rejected")
                self.assertIn(why, receipt["reason"])
                self.assertEqual(world.record, before)
        world.gh.branches["riscv64"] = "f" * 40
        world.gh.upstream = [upstream_pr(user={"id": STRANGER["id"], "login": "stranger"}),
                             upstream_pr(number=206, base={"ref": "main"})]
        out = world.tick(at(40))
        self.assertEqual(world.kinds(out), ["record_audit_observation"])
        self.assertEqual(world.record, before)
        self.assertEqual(world.kinds(world.tick(at(41))), [])
        for key in ("handoff_requests", "requested_effects", "dev_pr_requests"):
            self.assertEqual(world.reports[-1][key], [], key)
        self.assertIsNone(engine.refusal(world.record, "record_late_effect", policy=world.policy,
                                         now=T0, facts={"result_mismatch"}))

    def test_retract_leaves_riscv64_and_the_next_submission_replaces_it(self):
        world = through("handoff.waiting_upstream_pr")
        out = world.tick(at(30), comments=[comment(
            f"/upstreaming retract-pr revision=7 head={CLEAN}", cid=970)])
        self.assertEqual(world.kinds(out), ["retract_submission", "request_patch"])
        self.assertEqual((world.state, world.record["revision"]), ("development.agent_running", 8))
        for name in ("pending_human_action", "handoff_request", "handoff_approval"):
            self.assertIsNone(world.record[name])
        self.assertEqual(world.gh.branches, {"riscv64-dev": HEAD, "riscv64": CLEAN})
        self.assertNotIn("reminders", world.tick(at(0, "2026-12-01")).report["controller"])
        self.assertEqual([c[0] for c in world.gh.calls if c[0] != "GET"], ["PUSH", "POST"])

        world = through("handoff.waiting_upstream_pr")
        out = world.tick(at(30), comments=[comment(
            f'/upstreaming refresh revision=7 head={CLEAN} reason="new base"', cid=971)])
        self.assertEqual(world.kinds(out), ["refresh_planning", "begin_planning"])
        self.assertEqual(world.record["refresh"]["head_sha"], HEAD)
        self.assertEqual(world.record["fork"], FORK)
        self.assertEqual(world.gh.branches["riscv64"], CLEAN)

    def test_a_second_submission_replaces_riscv64_under_the_lease(self):
        world = through("handoff.waiting_upstream_pr")
        world.tick(at(30), comments=[comment(
            f"/upstreaming retract-pr revision=7 head={CLEAN}", cid=970)])
        # Development continues on `riscv64-dev`, CI passes again, and a new
        # submission is reviewed and built.
        self.assertEqual(world.state, "development.agent_running")
        world.record = engine.apply_transition(
            world.record, "record_publish_success", policy=world.policy, now=T0,
            facts={"result_published", "no_review_contract"}, values={
                "proposal_artifact": {"work_id": "w", "claim_id": "c", "artifact_id": 1,
                                      "name": "n", "digest": "sha256:" + "0" * 64, "run_id": "1"},
                "publication_operation_id": "op-x"}).record
        out = world.tick(at(31))
        self.assertEqual(world.state, "handoff.awaiting_submission", world.kinds(out))
        out = world.tick(at(32), comments=[comment(
            f"/upstreaming submit-upstream revision={world.record['revision']} head={HEAD}", cid=972)])
        second = "c2" * 20
        world.gh.messages[second] = REVIEWED_MESSAGE + "\n\nSigned-off-by: a <a@b.c>"
        world.gh.trees[second] = TREE
        out = world.tick(at(33), publications=[world.rewrite_outcome(
            head=second, commits=[{"message": world.gh.messages[second], "tree": TREE}])])
        self.assertEqual(world.kinds(out), ["accept_submission_rewrite", "request_submission_review"])
        out = world.tick(at(33), publications=[world.review_outcome()])
        self.assertEqual(world.kinds(out), ["accept_submission_review"])
        out = world.tick(at(34), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out)[0], "confirm_clean_head")
        self.assertEqual(world.gh.fork_writes()[-1], ("PUSH", f"riscv64:{CLEAN}->{second}"))

    def test_pause_keeps_the_wait_and_suppresses_reminders(self):
        world = through("handoff.waiting_upstream_pr")
        out = world.tick(at(30), comments=[comment('/upstreaming pause reason="away"', cid=972)])
        self.assertEqual((world.kinds(out), world.state), (["pause"], "handoff.waiting_upstream_pr"))
        self.assertNotIn("reminders", world.tick(at(0, "2026-12-03")).report["controller"])
        world.gh.upstream = [upstream_pr()]
        out = world.tick(at(1, "2026-12-03"))
        self.assertEqual(world.kinds(out), ["verify_upstream_pr"])


class TestReminders(unittest.TestCase):
    def test_the_locked_wait_reminder_carries_the_package_and_the_head(self):
        world = through("handoff.waiting_upstream_pr")
        package_url = world.operations[world.record["handoff_request"]["command_id"]]["external_ids"]["comment_url"]
        self.assertEqual(world.kinds(world.tick(at(0, "2026-11-25"))), [])
        out = world.tick(at(5, "2026-11-26"))
        self.assertEqual(world.kinds(out), ["record_reminder"])
        event = world.events[-1]
        self.assertEqual((event["details"]["package_url"], event["details"]["submitter"],
                          event["details"]["mismatch"]), (package_url, OWNER["login"], None))
        self.assertEqual(event["details"]["bound_to"], {"revision": 7, "head": CLEAN})
        text = status_ledger.render(event, world.record, "https://run")
        self.assertIn(f"Submitter: @{OWNER['login']}.", text)
        self.assertIn(f"]({package_url})", text)
        self.assertIn(f"`{CLEAN}`", text)
        self.assertIn("Pending human action: `open_upstream_pr`", text)
        self.assertNotIn("Observed:", text)
        self.assertIn(f"/upstreaming retract-pr revision=7 head={CLEAN}", text)
        world.gh.branches["riscv64"] = "f" * 40
        world.tick(at(0, "2026-12-02"))
        out = world.tick(at(10, "2026-12-01"))
        reminder = [e for e in world.events if e["kind"] == "record_reminder"][-1]
        self.assertIn("`riscv64` resolves to", reminder["details"]["mismatch"] or "")
        text = status_ledger.render(reminder, world.record, "https://run")
        self.assertIn("Observed: `", text)
        self.assertIn("'riscv64' resolves to", text)
        self.assertNotIn(chr(0x2014), text)


class TestPullRequestDiscovery(unittest.TestCase):
    def setUp(self):
        self.record = through("handoff.waiting_upstream_pr").record
        self.authority = policy()["command_authority"]["actor_ids"]

    def test_each_filter(self):
        cases = {
            "repository": upstream_pr(base={"repo": {"id": 1}}),
            "base": upstream_pr(base={"ref": "master2"}),
            "fork": upstream_pr(head={"repo": {"id": 2}}),
            "development branch": upstream_pr(head={"ref": "riscv64-dev", "sha": HEAD}),
            "ref": upstream_pr(head={"ref": "main"}),
            "head": upstream_pr(head={"sha": HEAD}),
            "author": upstream_pr(user={"id": STRANGER["id"], "login": "stranger", "type": "User"}),
            "bot": upstream_pr(user={"id": OWNER["id"], "login": "x[bot]", "type": "Bot"}),
            "closed": upstream_pr(state="closed"),
            "merged": upstream_pr(merged_at="2026-11-24T00:00:00Z"),
        }
        for name, pr in cases.items():
            with self.subTest(rejected=name):
                matching, rejected = handoff.matching_upstream_prs(
                    self.record, [handoff.normalize_pull(pr)], self.authority)
                self.assertEqual((matching, len(rejected)), ([], 1))
        matching, rejected = handoff.matching_upstream_prs(
            self.record, [handoff.normalize_pull(upstream_pr())], self.authority)
        self.assertEqual((len(matching), rejected), (1, []))
        self.assertEqual(handoff.upstream_pr_value(matching[0]), {
            "id": 777, "number": 205, "url": "https://github.com/closeio/ciso8601/pull/205",
            "head_repository_id": FORK["repository_id"], "head_ref": "riscv64", "head_sha": CLEAN,
            "base_ref": "master", "registered_by": {"id": OWNER["id"], "login": OWNER["login"]}})

    def test_ambiguous_candidates_verify_nothing(self):
        world = through("handoff.waiting_upstream_pr")
        world.gh.upstream = [upstream_pr(), upstream_pr(id=778, number=206)]
        out = world.tick(at(30))
        self.assertEqual(world.kinds(out), ["record_audit_observation"])
        self.assertEqual(world.state, "handoff.waiting_upstream_pr")
        self.assertIn("2 pull requests match", world.events[-1]["details"]["mismatch"])

    def test_a_draft_or_another_authorized_human_is_a_compatible_author(self):
        other = {"id": 4242, "login": "co-maintainer", "type": "User"}
        authority = list(self.authority) + [other["id"]]
        for name, pr in (("draft", upstream_pr(draft=True)),
                         ("not the requester", upstream_pr(user=other))):
            with self.subTest(name):
                matching, rejected = handoff.matching_upstream_prs(
                    self.record, [handoff.normalize_pull(pr)], authority)
                self.assertEqual((len(matching), rejected), (1, []))
        matching, rejected = handoff.matching_upstream_prs(
            self.record, [handoff.normalize_pull(upstream_pr(user=other))], self.authority)
        self.assertEqual((matching, len(rejected)), ([], 1))


class TestApprovalKept(unittest.TestCase):
    """A return to development and a failed review keep `handoff_approval`:
    their rows clear the clean head, the review, the request and the pending
    action only, and the next `submit_upstream` replaces the approval."""

    def test_the_rows_do_not_clear_the_approval(self):
        for state, verb in (("handoff.reviewing", "reject_submission_review"),
                            ("handoff.building_upstream_branch", "return_for_repair"),
                            ("handoff.waiting_ci", "return_for_repair"),
                            ("handoff.preparing_submission", "return_for_repair")):
            with self.subTest(state=state, verb=verb):
                [row] = [t for (v, _), t in lifecycle.LIFECYCLE[state].transitions.items()
                         if v == verb]
                self.assertNotIn("handoff_approval", row.effects.clears)
                self.assertIn("handoff_request", row.effects.clears)

    def test_a_failed_review_and_a_moved_clean_head_keep_it(self):
        world = rewritten()
        approval = copy.deepcopy(world.record["handoff_approval"])
        world.tick(at(2), publications=[world.review_outcome("fail", ["no"])])
        self.assertEqual(world.state, "development.agent_running")
        self.assertEqual(world.record["handoff_approval"], approval)
        world = reviewed()
        approval = copy.deepcopy(world.record["handoff_approval"])
        world.gh.ci_state = "failure"
        world.gh.ci_attempt = 2
        world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.state, "development.agent_running")
        self.assertEqual(world.record["handoff_approval"], approval)


def package_args(record, review_data=None, commits=None, **over):
    """The inputs of the package for the live record: the reviewed series is
    the series of `riscv64` whose messages the review names."""
    compare = json.loads((FIXTURES / "fork-compare.json").read_text())
    commits = commits or [{"sha": CLEAN, "message": REVIEWED_MESSAGE}]
    data = {"verdict": "pass", "reasons": [], "approved_head": HEAD, "base_sha": BASE,
            "title": TITLE, "body": BODY, "head": CLEAN, "clean_tree": TREE,
            "candidate_tree": TREE, "series_digest": "5" * 64,
            "commits": [{"message": c["message"], "tree": TREE} for c in commits],
            "artifact": {"run_id": "91010", "artifact_id": 9101}}
    data.update(review_data or {})
    args = {"plan": load("plan.json"), "ci": load("evidence-ci.json"),
            "review": {"evidence_id": record.get("submission_review_id") or "r", "data": data},
            "commits": commits, "files": [f["filename"] for f in compare["files"]],
            "operation_id": PACKAGE_OP}
    args.update(over)
    return args


class TestPackage(unittest.TestCase):
    def inputs(self, **over):
        record = through("handoff.preparing_submission").record
        return record, package_args(record, **over)

    def test_deterministic_and_complete(self):
        record, args = self.inputs()
        one, two = handoff.build_package(record, **args), handoff.build_package(copy.deepcopy(record), **args)
        self.assertEqual(one, two)
        self.assertEqual((one["title"], one["body"]), (TITLE, BODY))
        comment = one["comment"]
        for part in (f"`closeio/ciso8601` (id `{UPSTREAM_ID}`), base `master`",
                     f"`riseproject-upstreaming/ciso8601` (id `{FORK['repository_id']}`), branch "
                     f"`riscv64`, built from the reviewed series of the approved head `{HEAD}` of "
                     "`riscv64-dev` (development PR #2, which stays open)",
                     f"Exact head: `{CLEAN}`", f"Package operation: `{PACKAGE_OP}`",
                     f"plan evidence: `{PLAN}`",
                     f"review evidence: `{record['submission_review_id']}`",
                     "19 required jobs executed and passed, including "
                     "`Build - ubuntu-24.04-riscv (riscv64)`",
                     f"on `{HEAD}`, the candidate head with the same tree",
                     "> [!WARNING]", "The compare form resolves the branch `riscv64`",
                     f"/upstreaming retract-pr revision=7 head={CLEAN}",
                     "use the revision of the latest entry on this issue",
                     "```text\nNo CLA/DCO", "`CHANGELOG.md`", "If GitHub does not let your account",
                     "written after the project's pull request template", one["marker"]):
            self.assertIn(part, comment)
        for part in ("Open the pull request at exactly this head.",
                     "Do not add commits to `riscv64` or edit files in the form.",
                     "do not submit: withdraw this package with the `retract-pr` command above",
                     "returns the contribution to development for a repair",
                     "Only steps outside the series (such as signing a CLA)"):
            self.assertIn(part, comment)
        for retired in ("closed", "copy its required parts", "riscv64-dev:"):
            self.assertNotIn(retired, comment)
        self.assertIn("approval: `comment-900`", comment)
        self.assertTrue(comment.endswith(one["marker"]))
        self.assertEqual(one["marker"], f"<!-- upstreaming-submission:{CID}:r6:{CLEAN} -->")
        self.assertEqual(one["digest"], hashlib.sha256(comment.encode()).hexdigest())
        self.assertNotIn(chr(0x2014), comment)

    def test_the_series_must_be_the_reviewed_series(self):
        record, args = self.inputs()
        with self.assertRaisesRegex(handoff.HandoffError, "not the reviewed series"):
            handoff.build_package(record, **dict(args, commits=[{"sha": CLEAN, "message": "Other"}]))
        with self.assertRaises(handoff.HandoffError):
            handoff.build_package(record, **dict(args, commits=[]))
        with self.assertRaises(handoff.HandoffError):
            handoff.build_package(record, **package_args(
                record, commits=[{"sha": CLEAN, "message": "{debug} x"}]))

    def test_compare_link_is_percent_encoded(self):
        record, args = self.inputs()
        package = handoff.build_package(record, **args)
        url = urllib.parse.urlsplit(package["link"])
        self.assertEqual((url.scheme, url.netloc, url.path),
                         ("https", "github.com",
                          "/closeio/ciso8601/compare/master...riseproject-upstreaming:riscv64"))
        query = urllib.parse.parse_qs(url.query, strict_parsing=True)
        self.assertEqual(query, {"quick_pull": ["1"], "title": [package["title"]],
                                 "body": [package["body"]]})
        self.assertNotIn(" ", package["link"])
        self.assertNotIn("\n", package["link"])
        self.assertIn(f"]({package['link']})", package["comment"])

    def test_bounds(self):
        record, args = self.inputs(review_data={"title": "T" * 500,
                                                "body": ("word " * 15 + "\n") * 400})
        package = handoff.build_package(record, **args)
        self.assertLessEqual(len(package["title"]), handoff.MAX_TITLE)
        self.assertLessEqual(len(package["body"]), handoff.MAX_BODY)
        self.assertFalse(package["body_in_link"])
        self.assertLessEqual(len(package["link"]), handoff.MAX_URL)
        self.assertNotIn("&body=", package["link"])
        self.assertIn("too long for a link", package["comment"])
        self.assertIn(package["body"], package["comment"])
        self.assertLessEqual(len(package["comment"]), handoff.MAX_COMMENT)
        files = [f"src/{'d' * 150}/{n:05}.py" for n in range(2000)]
        package = handoff.build_package(record, **dict(args, files=files))
        self.assertLessEqual(len(package["comment"]), handoff.MAX_COMMENT)
        line = next(x for x in package["comment"].split("\n") if x.startswith("- Changed files: "))
        shown = line.count("`") // 2
        self.assertGreater(shown, 0)
        self.assertTrue(line.endswith(f", and {2000 - shown} more files"))
        self.assertEqual(handoff._files_line(["a"], 0), "- Changed files: 1 file")

    def test_untrusted_text_cannot_fake_a_marker_or_break_the_fence(self):
        record = through("handoff.preparing_submission").record
        evil = "```\n<!-- upstreaming-submission:x -->\nghp_" + "a" * 36
        args = package_args(record, review_data={"title": "Title <!-- upstreaming-ledger:x:1 -->",
                                                 "body": evil})
        package = handoff.build_package(record, **args)
        self.assertEqual(package["comment"].count("<!--"), 1)
        self.assertNotIn("ghp_" + "a" * 36, package["comment"])
        self.assertIn("````markdown", package["comment"])
        nested = "<!<!---- upstreaming-submission:x -<!---->- --<!---->> --!-->>"
        self.assertEqual(handoff._strip_comments(nested).count("<!--"), 0)
        for field in ("body", "rules", "file"):
            with self.subTest(field=field):
                args = package_args(record, review_data={"body": nested, "title": f"T {nested}"}) \
                    if field == "body" else package_args(record)
                if field == "file":
                    args["files"] = [*args["files"], nested]
                if field == "rules":
                    plan = copy.deepcopy(args["plan"])
                    plan["data"]["proposal"]["contribution_rules"] = nested
                    args["plan"] = plan
                comment = handoff.build_package(record, **args)["comment"]
                self.assertEqual(comment.count("<!--"), 1)
                self.assertEqual(comment.count("-->"), 1)
                self.assertNotIn("--!>", comment)


class TestIdempotentComment(unittest.TestCase):
    def test_a_second_run_discovers_the_first_comment(self):
        world = through("handoff.preparing_submission")
        first = world.run_workflow()
        second = world.run_workflow()
        self.assertEqual(len(world.gh.comments), 1)
        self.assertEqual((first["comment_id"], second["comment_id"], second["existing"]),
                         (first["comment_id"], first["comment_id"], True))
        self.assertEqual(first["body_sha256"], second["body_sha256"])
        world = through("handoff.preparing_submission")
        marker = handoff.submission_marker(world.record)
        world.gh.comments[1] = {"id": 1, "body": marker, "user": {"login": "someone"}}
        world.gh.comments[2] = {"id": 2, "body": f"Quoted:\n```\n{marker}\n```\n{marker}",
                                "user": {"login": handoff.APP_LOGIN}}
        receipt = world.run_workflow()
        self.assertEqual((receipt["result"], receipt["existing"]), ("applied", False))
        self.assertNotIn(receipt["comment_id"], (1, 2))
        self.assertEqual(len(world.gh.comments), 3)

    def test_a_package_of_another_attempt_of_the_same_approval_is_discovered(self):
        record, args = TestPackage().inputs()
        earlier = handoff.build_package(record, **dict(args, operation_id="op-" + "b" * 24))
        self.assertTrue(handoff.is_package_of(earlier["comment"], record, dict(
            earlier, digest="0" * 64)))
        other = copy.deepcopy(record)
        other["handoff_approval"]["command_id"] = "comment-999"
        self.assertFalse(handoff.is_package_of(earlier["comment"], other, dict(
            earlier, digest="0" * 64)))
        spoof = earlier["comment"].replace("approval: `comment-900`", "approval: `x`")
        self.assertFalse(handoff.is_package_of(spoof, record, dict(earlier, digest="0" * 64)))
        self.assertFalse(handoff.is_package_of(earlier["marker"] + "\n" + "text", record,
                                               dict(earlier, digest="0" * 64)))

    def test_a_failed_post_is_a_failed_attempt(self):
        world = through("handoff.preparing_submission")
        world.gh.fail_post = True
        receipt = world.run_workflow()
        self.assertEqual((receipt["result"], receipt["failure"]), ("failed", handoff.INFRASTRUCTURE))
        out = world.tick(at(30), receipts=[receipt])
        self.assertEqual(world.kinds(out), ["retry_submission_comment"])


class TestValidationBeforeAnyWrite(unittest.TestCase):
    """The trusted workflow checks the operation against committed state
    before any effect, and refuses without one."""

    def check(self, world, op_id=None, p=None):
        with tempfile.TemporaryDirectory() as directory:
            root = world.root(Path(directory))
            return handoff.load_request(root, op_id or world.record["active_operation_id"],
                                        p or world.policy)

    def test_refusals(self):
        world = reviewed()
        self.check(world)
        op_id = world.record["active_operation_id"]
        review_id = world.record["submission_review_id"]
        variants = {
            "paused": lambda w: w.record.update(paused=True),
            "not active": lambda w: w.record.update(active_operation_id=None),
            "other state": lambda w: w.record.update(activity="preparing_submission"),
            "other approved head": lambda w: w.record["handoff_approval"].update(head="c" * 40),
            "no review": lambda w: w.record.update(submission_review_id=None),
            "failed review": lambda w: w.evidence[review_id]["data"].update(verdict="fail"),
            "another reviewed head": lambda w: w.operations[op_id]["proposed"].update(commit="c" * 40),
            "another reviewed tree": lambda w: w.operations[op_id]["proposed"].update(tree="c" * 40),
            "another series": lambda w: w.operations[op_id]["proposed"].update(content_digest="6" * 64),
            "development branch": lambda w: w.operations[op_id]["target"].update(ref="riscv64-dev"),
            "other fork": lambda w: w.operations[op_id]["target"].update(repository="riseproject-upstreaming/x"),
            "closed": lambda w: w.operations[op_id].update(status="failed"),
            "no approval": lambda w: w.record.update(handoff_approval=None),
        }
        for name, change in variants.items():
            with self.subTest(variant=name):
                w = copy.deepcopy(world)
                change(w)
                with self.assertRaises(handoff.HandoffRefused):
                    self.check(w, op_id)
        with self.assertRaises(handoff.HandoffRefused):
            self.check(world, op_id, policy(read_only=True))
        with self.assertRaises(handoff.HandoffRefused):
            self.check(world, "op-../../x")
        w = copy.deepcopy(world)
        w.record["paused"] = True
        receipt = w.run_workflow()
        self.assertEqual(receipt["result"], "refused")
        self.assertEqual(w.gh.fork_writes(), [])

    def test_the_series_comes_from_the_accepted_review_run(self):
        world = reviewed()
        op = world.active
        op["proposed"]["content_digest"] = hashlib.sha256(b"From x\n").hexdigest()
        meta = {"id": op["external_ids"]["publication_artifact_id"], "name": "porting-publication",
                "expired": False, "workflow_run": {"id": int(op["external_ids"]["publication_run_id"])}}
        with tempfile.TemporaryDirectory() as directory:
            root = world.root(Path(directory))
            series = root / "patches.mbox"
            series.write_bytes(b"From x\n")
            meta_path = root / "artifact.json"
            for name, change in {"ok": {}, "other artifact": {"id": 1},
                                 "other name": {"name": "porting-out"}, "expired": {"expired": True},
                                 "other run": {"workflow_run": {"id": 1}}}.items():
                series.write_bytes(b"From x\n")
                with self.subTest(meta=name):
                    meta_path.write_text(json.dumps(dict(meta, **change)))
                    if name == "ok":
                        self.assertEqual(handoff.reviewed_series(root, op["operation_id"], series,
                                                                 meta_path), b"From x\n")
                    elif name == "expired":
                        # Not a verdict on the series: an infrastructure
                        # failure; the controller leads the expired rewrite
                        # back to a fresh one.
                        with self.assertRaises(handoff.HandoffError) as caught:
                            handoff.reviewed_series(root, op["operation_id"], series, meta_path)
                        self.assertNotIsInstance(caught.exception, handoff.HandoffInvalid)
                    else:
                        with self.assertRaises(handoff.HandoffInvalid):
                            handoff.reviewed_series(root, op["operation_id"], series, meta_path)
            # Another series than the accepted one is refused before any token.
            meta_path.write_text(json.dumps(meta))
            series.write_bytes(b"From y\n")
            with self.assertRaisesRegex(handoff.HandoffInvalid, "does not match"):
                handoff.reviewed_series(root, op["operation_id"], series, meta_path)


class TestNoReceipt(unittest.TestCase):
    """Every handoff work state has a working exit when its attempts never
    report: a cancelled or timed-out run, a lost runner, a dispatch that
    fails, or a pause in between."""

    STATES = {"handoff.building_upstream_branch": ("upstream_branch_failed",
                                                   "resume_upstream_branch",
                                                   "exhaust_upstream_branch_retries"),
              "handoff.preparing_submission": ("submission_comment_failed",
                                               "resume_submission_preparation",
                                               "exhaust_comment_retries")}

    def drain(self, world, state, minute, pause_at=None):
        rows = []
        while world.state == state:
            if minute == pause_at:
                out = world.tick(at(minute), comments=[comment("/upstreaming pause", cid=930)])
                self.assertEqual(world.kinds(out), ["pause"])
                for later in (minute + 20, minute + 40):
                    out = world.tick(at(later))
                    self.assertEqual((world.kinds(out), out.report["handoff_requests"]), ([], []))
                revision = world.record["revision"]
                out = world.tick(at(minute + 41), comments=[comment(
                    f'/upstreaming resume revision={revision} reason="x"', cid=931)])
                self.assertEqual(world.kinds(out)[0], "resume_pause")
                rows += world.kinds(out)[1:]
                minute += 42
                continue
            rows += world.kinds(world.tick(at(minute)))
            minute += 6
            self.assertLess(minute, 600, f"stuck in {state}")
        return rows, minute

    def test_every_work_state_has_an_exit(self):
        for state, (pending, resume, exhaust) in self.STATES.items():
            for pause_at in (None, 48):
                with self.subTest(state=state, pause=pause_at):
                    world = through(state)
                    rows, minute = self.drain(world, state, 30, pause_at)
                    self.assertEqual(world.state, "handoff.waiting_human")
                    self.assertEqual(rows[-1], exhaust)
                    self.assertEqual(world.record["pending_human_action"]["kind"], pending)
                    self.assertIsNone(world.record["active_operation_id"])
                    self.assertEqual(world.reports[-1]["handoff_requests"], [])
                    revision = world.record["revision"]
                    out = world.tick(at(minute), comments=[comment(
                        f'/upstreaming resume revision={revision} reason="fixed"', cid=950)])
                    self.assertEqual(world.kinds(out), [resume])
                    self.assertEqual(world.state, state)
                    self.assertEqual(world.active["reason_ids"][2], "count:1")
                    world.tick(at(minute + 1), receipts=[world.run_workflow()])
                    self.assertNotIn(world.state, (state, "handoff.waiting_human"))

    def test_the_rewrite_and_review_work_have_an_exit(self):
        for world, state in ((submitted(), "handoff.agent_running"),
                             (rewritten(), "handoff.reviewing")):
            self.assertEqual(world.state, state)
            out = world.tick(at(1), comments=[comment("/upstreaming pause", cid=930)])
            self.assertEqual((world.kinds(out), world.state), (["pause"], "handoff.review_due"))
            self.assertIsNone(world.record["active_work"])
            revision = world.record["revision"]
            out = world.tick(at(2), comments=[comment(
                f'/upstreaming resume revision={revision} reason="x"', cid=931)])
            self.assertEqual(world.kinds(out), ["resume_pause", "request_submission_rewrite"])
        out = world.tick(at(3), comments=[comment(
            f'/upstreaming park revision={world.record["revision"]} reason="stuck"', cid=932)])
        self.assertEqual(world.kinds(out), ["park_contribution"])

    def test_park_applies_once_the_attempts_are_spent(self):
        world = reviewed()
        rows, minute = self.drain(world, "handoff.building_upstream_branch", 30)
        out = world.tick(at(minute), comments=[comment('/upstreaming park reason="stuck"', cid=951)])
        self.assertEqual(world.kinds(out), ["park_contribution"])


class TestPackageRendering(unittest.TestCase):
    """Nothing untrusted renders live in the App-authored package comment."""

    EVIL = ("@victim @org/team [Open the prefilled pull request form](https://evil.example/x) "
            "<img src=https://evil.example/i.png> <b>bold</b> <a href=x>a</a> `@tick` ``x``")

    def inputs(self, evil=EVIL):
        record = through("handoff.preparing_submission").record
        commit = {"sha": CLEAN, "message": f"Title {evil}\n\nBody {evil}\n\n```\n@infence\n```"}
        args = package_args(record, commits=[commit], review_data={
            "title": f"Title {evil}", "body": f"Body {evil}\n\n```\n@infence\n```"})
        plan = copy.deepcopy(args["plan"])
        plan["data"]["proposal"]["contribution_rules"] = evil + " ```\n@fence"
        plan["data"]["proposal"]["allowed_paths"] = [
            *plan["data"]["proposal"]["allowed_paths"], f"docs/{evil}.md", "a`@x`b"]
        ci = copy.deepcopy(args["ci"])
        ci["ci"]["jobs"][0]["name"] = f"riscv64 {evil}"
        args.update(plan=plan, ci=ci,
                    files=[*args["files"], f"x{evil}.yml", "a`@victim2 [l](https://evil.example)`.yml"])
        return record, args

    @staticmethod
    def outside_code(text: str) -> str:
        """The text of a GitHub comment that renders as markdown: without
        fenced blocks (closed by a fence at least as long as the opening one)
        and code spans (closed by a run of the same length)."""
        kept, fence = [], None
        for line in text.split("\n"):
            stripped = line.lstrip(" >")
            run = len(stripped) - len(stripped.lstrip("`"))
            if fence:
                if run >= fence and not stripped.strip().strip("`"):
                    fence = None
                continue
            if run >= 3:
                fence = run
                continue
            out, i = [], 0
            while i < len(line):
                if line[i] == "`":
                    n = len(line[i:]) - len(line[i:].lstrip("`"))
                    j = i + n
                    close = None
                    while True:
                        k = line.find("`" * n, j)
                        if k < 0:
                            break
                        m = len(line[k:]) - len(line[k:].lstrip("`"))
                        if m == n:
                            close = k
                            break
                        j = k + m
                    if close is None:
                        out.append(line[i:j])
                        i = j
                    else:
                        i = close + n
                else:
                    out.append(line[i])
                    i += 1
            kept.append("".join(out))
        return "\n".join(kept)

    def test_every_untrusted_field_stays_in_code(self):
        record, args = self.inputs()
        package = handoff.build_package(record, **args)
        # The prefilled link carries the proposal percent-encoded.
        rendered = self.outside_code(package["comment"]).replace(package["link"], "LINK")
        for bad in ("victim", "@org", "evil.example", "<img", "<b>", "<a ", "@tick", "@fence",
                    "@infence", "@x"):
            self.assertNotIn(bad, rendered, bad)
        # The one mention is the human who requested the submission; the one
        # link of the form is the real compare link.
        self.assertEqual(re.findall(r"@[A-Za-z0-9/-]+", rendered), [f"@{OWNER['login']}"])
        self.assertEqual(rendered.count("Open the prefilled pull request form"), 1)
        self.assertIn("**[Open the prefilled pull request form](LINK)**", rendered)
        # The untrusted text is shown, not dropped.
        for shown in ("@victim", "evil.example", "<img"):
            self.assertIn(shown, package["comment"])

    def test_a_backtick_cannot_open_a_code_span_or_fence(self):
        for evil in ("`", "``", "```", "````` @a", "x` @b `y", "@c`"):
            with self.subTest(evil=evil):
                record, args = self.inputs(evil)
                rendered = self.outside_code(handoff.build_package(record, **args)["comment"])
                self.assertEqual(re.findall(r"@[A-Za-z0-9/-]+", rendered), [f"@{OWNER['login']}"])
                self.assertIn("Open the prefilled pull request form", rendered)
                self.assertTrue(rendered.rstrip().endswith("-->"))

    def test_an_unusual_requester_login_is_not_a_mention(self):
        record, args = TestPackage().inputs()
        record = copy.deepcopy(record)
        record["handoff_approval"]["actor"]["login"] = "x [y](z)"
        rendered = self.outside_code(handoff.build_package(record, **args)["comment"])
        self.assertNotIn("[y](z)", rendered)
        self.assertEqual(re.findall(r"@[A-Za-z0-9/-]+", rendered), [])

    def test_mentions_in_the_proposed_title_and_body_notify_nobody(self):
        record, args = TestPackage().inputs(review_data={
            "title": "Fix @user's report",
            "body": "Thanks @org/team and @bob.\nMail a@b.example. Code `@kept` stays.\n\n"
                    "```\n@fenced\n```\n    @indented"})
        package = handoff.build_package(record, **args)
        wj = handoff.WORD_JOINER
        self.assertEqual(package["title"], f"Fix @{wj}user's report")
        for part in (f"Thanks @{wj}org/team and @{wj}bob.", "Mail a@b.example.",
                     f"Code `@{wj}kept` stays.", f"```\n@{wj}fenced\n```", f"\n@{wj}indented"):
            self.assertIn(part, package["body"])
        self.assertIn("notifies nobody upstream", package["comment"])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(package["link"]).query)
        self.assertEqual(query["title"], [package["title"]])
        self.assertEqual(handoff.neutralize_mentions("no mention, a@b.c"), ("no mention, a@b.c", 0))
        plain = handoff.build_package(record, **package_args(record))
        self.assertNotIn("notifies nobody upstream", plain["comment"])
        # An entity of an at sign alone is a change the package shows.
        package = handoff.build_package(record, **package_args(
            record, review_data={"title": "Fix", "body": "mail a&#64;b.example"}))
        self.assertIn("mail a@b.example", package["body"])
        self.assertIn("notifies nobody upstream", package["comment"])

    def test_an_empty_reviewed_title_falls_back_to_a_neutral_title(self):
        record, args = TestPackage().inputs(review_data={"title": "  \n ", "body": "Body."})
        self.assertEqual(handoff.build_package(record, **args)["title"], "Add riscv64 support")

    def test_no_markdown_context_keeps_a_mention_live(self):
        wj = handoff.WORD_JOINER
        cases = {
            "_@victim_": f"_@{wj}victim_",
            "*@victim*": f"*@{wj}victim*",
            "\\`@victim`": f"\\`@{wj}victim`",
            "```x`y\n@victim": f"```x`y\n@{wj}victim",
            "`multi\n@victim line`": f"`multi\n@{wj}victim line`",
            "&#64;victim": f"@{wj}victim",
            "&#064;victim": f"@{wj}victim",
            "&#x40;victim": f"@{wj}victim",
            "&#X0040;victim": f"@{wj}victim",
            "&commat;victim": f"@{wj}victim",
            "&#64victim": f"@{wj}victim",
            "a/@x": f"a/@{wj}x",
            "x-@x": f"x-@{wj}x",
            "x+@x": f"x+@{wj}x",
            ".@x": f".@{wj}x",
            "@@x": f"@{wj}@{wj}x",
            "@&#117;ser": f"@{wj}&#117;ser",
            "@&#x75;ser": f"@{wj}&#x75;ser",
            "&#64;&#x76;ictim": f"@{wj}&#x76;ictim",
            "@\\user": f"@{wj}\\user",
            "x_@x": f"x_@{wj}x",
            "@org/team": f"@{wj}org/team",
            "\uff20victim": f"\uff20{wj}victim",
            "\ufe6bvictim": f"\ufe6b{wj}victim",
            "&#xff20;victim": f"\uff20{wj}victim",
            "\u200b@victim": f"\u200b@{wj}victim",
            "a\u200b@victim": f"a\u200b@{wj}victim",
            "\u200d@victim": f"\u200d@{wj}victim",
            "<b>@victim</b>": f"<b>@{wj}victim</b>",
            "[@victim](https://x)": f"[@{wj}victim](https://x)",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                out, changes = handoff.neutralize_mentions(text)
                self.assertEqual(out, expected)
                self.assertGreater(changes, 0)
                self.assertIsNone(re.search(r"(?<![A-Za-z0-9])[@\uff20\ufe6b][A-Za-z0-9_-]", out))
        # E-mail addresses, an at sign followed by nothing a login starts
        # with, and the text around them are kept; neutralizing is stable.
        for text in ("a@b.example", "x&#645;", "@ alone", "&amp;#64;victim", "@\u2060victim"):
            with self.subTest(text=text):
                self.assertEqual(handoff.neutralize_mentions(text), (text, 0))
        once, _ = handoff.neutralize_mentions("_@a_ &#64;b `@c`")
        self.assertEqual(handoff.neutralize_mentions(once), (once, 0))


class TestPackageBranchCheck(unittest.TestCase):
    """The package is posted only while `riscv64` resolves to the clean head;
    a branch read moved, deleted, or renamed returns to development."""

    def assert_returned(self, world, receipt):
        before = copy.deepcopy(world.record)
        writes = world.gh.fork_writes()
        op_id = world.record["active_operation_id"]
        self.assertEqual((receipt["result"], receipt["failure"]), ("failed", handoff.MOVED))
        self.assertEqual(world.gh.comments, {})
        self.assertNotIn("POST", [c[0] for c in world.gh.calls])
        out = world.tick(at(30), receipts=[receipt])
        self.assertEqual(world.kinds(out), ["return_for_repair", "request_patch"])
        self.assertEqual(world.state, "development.agent_running")
        record = world.record
        self.assertEqual(record["revision"], before["revision"] + 1)
        for name in ("clean_head_sha", "submission_rewrite_id", "submission_review_id",
                     "handoff_request", "active_operation_id"):
            self.assertIsNone(record[name], name)
        self.assertEqual(record["last_ci_evidence_id"], before["last_ci_evidence_id"])
        op = world.operations[op_id]
        self.assertEqual((op["status"], op["external_ids"]["failure"]), ("failed", "moved"))
        returned = next(e for e in world.events if e["kind"] == "return_for_repair")
        self.assertFalse(returned["details"].get("charged"))
        self.assertEqual((returned["details"]["clean_head"], returned["details"]["kept_evidence_id"]),
                         (CLEAN, before["last_ci_evidence_id"]))
        self.assertEqual(world.gh.fork_writes(), writes)
        return returned

    def test_a_moved_branch_is_not_posted(self):
        world = through("handoff.preparing_submission")
        world.gh.branches["riscv64"] = "d" * 40
        receipt = world.run_workflow()
        self.assertIn("riscv64 resolves to", receipt["reason"])
        returned = self.assert_returned(world, receipt)
        self.assertEqual(returned["details"]["head"], "d" * 40)
        # The development branch is not what the package names.
        world = through("handoff.preparing_submission")
        world.gh.branches["riscv64-dev"] = "d" * 40
        self.assertEqual(world.run_workflow()["result"], "applied")
        world = through("handoff.preparing_submission")
        first = world.run_workflow()
        world.gh.branches["riscv64"] = "d" * 40
        again = world.run_workflow()
        self.assertEqual((again["result"], again["comment_id"]), ("applied", first["comment_id"]))

    def test_a_deleted_branch_returns_for_repair(self):
        world = through("handoff.preparing_submission")
        world.gh.branches["riscv64"] = None
        returned = self.assert_returned(world, world.run_workflow())
        self.assertEqual(returned["details"]["head"], handoff.ABSENT)

    def test_a_renamed_branch_returns_for_repair(self):
        world = through("handoff.preparing_submission")
        branch_head = world.gh.branch_head

        def renamed(api, full_name, ref):
            if ref == "riscv64":
                raise collect.BranchRenamed(ref, "renamed")
            return branch_head(api, full_name, ref)

        with mock.patch.object(world.gh, "branch_head", renamed):
            receipt = world.run_workflow()
            self.assert_returned(world, receipt)

    def test_a_missing_branch_of_an_unread_fork_is_retried(self):
        original = FakeGitHub.get

        def another_id(self, path):
            if path == f"/repos/{FORK['name']}":
                return dict(original(self, path), id=1)
            return original(self, path)

        def missing(self, path):
            if path == f"/repos/{FORK['name']}":
                raise collect.CollectError(f"GET {path}: HTTP 404")
            return original(self, path)

        for name, get in (("another id", another_id), ("not found", missing)):
            with self.subTest(name):
                world = through("handoff.preparing_submission")
                world.gh.branches["riscv64"] = None
                with mock.patch.object(FakeGitHub, "get", get):
                    receipt = world.run_workflow()
                    self.assertEqual((receipt["result"], receipt["failure"]),
                                     ("failed", handoff.INFRASTRUCTURE))
                    out = world.tick(at(30), receipts=[receipt])
                self.assertEqual(world.kinds(out), ["retry_submission_comment"])
                self.assertEqual(world.state, "handoff.preparing_submission")
                self.assertEqual(world.gh.comments, {})

    def test_a_branch_read_back_at_the_clean_head_is_retried(self):
        world = through("handoff.preparing_submission")
        limit = lifecycle.budget_limit("submission_comment", world.policy)
        minute = 30
        for attempt in range(1, limit + 1):
            world.gh.branches["riscv64"] = "d" * 40
            receipt = world.run_workflow()
            self.assertEqual(receipt["failure"], handoff.MOVED)
            world.gh.branches["riscv64"] = CLEAN
            op_id = world.record["active_operation_id"]
            out = world.tick(at(minute), receipts=[receipt])
            minute += 1
            op = world.operations[op_id]
            self.assertEqual(op["external_ids"]["failure"], handoff.INFRASTRUCTURE)
            if attempt < limit:
                self.assertEqual(world.kinds(out), ["retry_submission_comment"])
                self.assertEqual(world.state, "handoff.preparing_submission")
                self.assertEqual(world.events[-1]["details"]["charged"], ["submission_comment"])
                self.assertTrue(world.events[-1]["details"]["reason"].startswith(
                    "riscv64 is not read back away from the clean head; "))
            else:
                self.assertEqual(world.kinds(out), ["exhaust_comment_retries"])
        self.assertEqual(world.state, "handoff.waiting_human")
        self.assertEqual(world.gh.comments, {})

    def test_moved_is_a_package_class_only(self):
        world = reviewed()
        out = world.tick(at(2), receipts=[failed(world, handoff.MOVED)])
        self.assertEqual(world.kinds(out), ["retry_upstream_branch"])
        self.assertEqual(world.state, "handoff.building_upstream_branch")


class TestUpstreamBranchReadback(unittest.TestCase):
    """The build of `riscv64` is read back as the reviewed head, tree, and
    series on the frozen base."""

    def test_the_reviewed_head_tree_and_series(self):
        world = reviewed()
        op = world.active
        receipt = {"operation_id": op["operation_id"], "clean_head": CLEAN, "run_id": "1"}
        gh = world.gh
        gh.branches["riscv64"] = CLEAN

        def readback(**over):
            with mock.patch("collect.branch_head", gh.branch_head), \
                    mock.patch("collect.compare_commits", gh.compare_commits):
                return handoff.readback(None, world.record, op, dict(receipt, **over),
                                        world.evidence)

        ok = readback()
        self.assertEqual(ok[1], {"clean_head_sha": CLEAN})
        self.assertIsInstance(readback(clean_head="d" * 40), str)
        gh.trees[CLEAN] = "8" * 40
        self.assertIsInstance(readback(), str)
        gh.trees[CLEAN] = TREE
        gh.messages[CLEAN] = "Another message"
        self.assertIsInstance(readback(), str)
        gh.messages[CLEAN] = REVIEWED_MESSAGE
        gh.branches["riscv64"] = None
        self.assertIsInstance(readback(), str)


class TestTrustedCurrentState(unittest.TestCase):
    """Before a job mints a token, the state it checked out is the head of
    main and still runs the operation."""

    def repo(self, directory):
        from tests.helpers import git
        git(directory, "init", "-q")
        git(directory, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
            "-m", "state")
        return git(directory, "rev-parse", "HEAD")

    def test_the_checked_out_state_must_be_the_head_of_main(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head = self.repo(root)
            api = mock.Mock()
            api.get.return_value = {"object": {"sha": head}}
            self.assertEqual(handoff.current_state(root, api), head)
            api.get.assert_called_once_with(f"/repos/{CONTROL_REPO}/git/ref/heads/main")
            api.get.return_value = {"object": {"sha": "f" * 40}}
            with self.assertRaises(handoff.HandoffRefused):
                handoff.current_state(root, api)
            api.get.side_effect = collect.CollectError("HTTP 502")
            with self.assertRaises(handoff.HandoffRefused):
                handoff.current_state(root, api)

    def test_a_refused_check_is_its_receipt_and_stops_the_job(self):
        world = reviewed()
        op_id = world.record["active_operation_id"]
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": CONTROL_REPO,
               "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "5"}
        with tempfile.TemporaryDirectory() as directory:
            root = world.root(Path(directory))
            head = self.repo(root)
            out = root / "handoff/outcome.json"
            with mock.patch.dict("os.environ", env), mock.patch("os.getcwd", return_value=str(root)), \
                    mock.patch("common.load_policy", return_value=world.policy), \
                    mock.patch("collect.Api.get", return_value={"object": {"sha": head}}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(handoff.main(["check", "--operation-id", op_id, "--kind",
                                               "upstream_branch", "--out", str(out)]), 0)
                self.assertFalse(out.exists())
                self.assertEqual(handoff.main(["check", "--operation-id", op_id, "--kind",
                                               "submission_comment", "--out", str(out)]), 1)
                self.assertEqual(json.loads(out.read_text())["result"], "refused")
                out.unlink()
            with mock.patch.dict("os.environ", env), mock.patch("os.getcwd", return_value=str(root)), \
                    mock.patch("common.load_policy", return_value=world.policy), \
                    mock.patch("collect.Api.get", return_value={"object": {"sha": "f" * 40}}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(handoff.main(["check", "--operation-id", op_id, "--kind",
                                               "upstream_branch", "--out", str(out)]), 1)
                self.assertIn("not the head of main", json.loads(out.read_text())["reason"])
                # The job's failure step keeps the refusal as its receipt.
                self.assertEqual(handoff.main(["failed", "--operation-id", op_id, "--out", str(out)]), 0)
                self.assertEqual(json.loads(out.read_text())["result"], "refused")


def refuse(*_args):
    raise handoff.HandoffRefused("the state of the operation changed on main after the checkout: x")


class TestFinalCheckAtHeadOfMain(unittest.TestCase):
    """Immediately before each write the head of main is read again: what
    the operation is authorized by and built from must be unchanged there,
    or the write is refused."""

    HEAD = "e" * 40

    def setUp(self):
        from tests.helpers import git

        self.world = reviewed()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.world.root(Path(self.tmp.name))
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "state")
        self.checked = git(self.root, "rev-parse", "HEAD")
        self.record, self.op = handoff.load_request(self.root, self.world.record["active_operation_id"],
                                                    self.world.policy)
        self.record_path = f".state/contributions/{contribution_file_id(CID)}.json"
        self.op_path = f".state/operations/{self.op['operation_id']}.json"
        self.at_head = self.op

    def tearDown(self):
        self.tmp.cleanup()

    def api(self, head=None):
        import base64

        def get(path):
            if path == f"/repos/{CONTROL_REPO}/git/ref/heads/main":
                return {"object": {"sha": head or self.HEAD}}
            if path == (f"/repos/{CONTROL_REPO}/contents/{self.op_path}?ref={head or self.HEAD}"):
                if self.at_head is None:
                    raise collect.CollectError("HTTP 404")
                return {"encoding": "base64",
                        "content": base64.b64encode(canonical_json(self.at_head).encode()).decode()}
            raise AssertionError(path)
        api = mock.Mock()
        api.get.side_effect = get
        return api

    def check(self, files=None, cmp=None, api=None):
        cmp = cmp if cmp is not None else {
            "status": "ahead", "ahead_by": 1, "behind_by": 0,
            "files": [{"filename": f} if isinstance(f, str) else f for f in files or []]}
        compare = cmp if callable(cmp) else mock.Mock(return_value=cmp)
        with mock.patch("collect.compare_commits", compare):
            return handoff.unchanged_on_main(self.root, self.record, self.op, api or self.api())

    def test_the_same_commit_passes_with_one_read(self):
        api = self.api(head=self.checked)
        with mock.patch("collect.compare_commits", side_effect=AssertionError("compared")):
            self.assertEqual(handoff.unchanged_on_main(self.root, self.record, self.op, api),
                             self.checked)
        api.get.assert_called_once_with(f"/repos/{CONTROL_REPO}/git/ref/heads/main")

    def test_unrelated_commits_pass(self):
        self.assertEqual(self.check([".state/contributions/other--proj--1.json",
                                     ".state/events/closeio--ciso8601--1/000099.json",
                                     "controller.py", ".github/workflows/handoff.yml"]), self.HEAD)

    def test_a_change_to_what_authorizes_the_operation_refuses(self):
        evidence = [f".state/evidence/{self.record[name]}.json"
                    for name in ("plan_evidence_id", "last_ci_evidence_id", "submission_rewrite_id",
                                 "submission_review_id")]
        self.assertEqual(len(evidence), 4)
        for path in ["policy.json", self.record_path, *evidence]:
            with self.subTest(path=path):
                with self.assertRaisesRegex(handoff.HandoffRefused, re.escape(path)):
                    self.check(["controller.py", path])
        # A renamed record (its previous name).
        with self.assertRaisesRegex(handoff.HandoffRefused, re.escape(self.record_path)):
            self.check([{"filename": ".state/contributions/x.json",
                         "previous_filename": self.record_path, "status": "renamed"}])

    def test_a_pause_committed_after_the_checkout_refuses(self):
        # The record at the head of main is paused: only the path is
        # rendered, never what changed.
        with self.assertRaises(handoff.HandoffRefused) as caught:
            self.check([self.record_path])
        self.assertIn("changed on main after the checkout", str(caught.exception))

    def test_a_relaunch_count_is_not_a_change(self):
        self.at_head = recovery.charge_launch(recovery.charge_launch(self.op, T0), T0)
        self.assertEqual(self.check([self.op_path]), self.HEAD)
        for name, changed in {
                "closed": dict(self.op, status="failed"),
                "other target": dict(self.op, target=dict(self.op["target"], ref="riscv64-dev")),
                "other proposal": dict(self.op, proposed=dict(self.op["proposed"], tree="9" * 40)),
                "removed": None}.items():
            with self.subTest(name):
                self.at_head = changed
                with self.assertRaises(handoff.HandoffRefused):
                    self.check([self.op_path])
        self.at_head = {"not": "an operation"}
        with self.assertRaises(handoff.HandoffRefused):
            self.check([self.op_path])

    def test_what_cannot_be_read_refuses(self):
        def failing(*_a):
            raise collect.CollectError("HTTP 502")

        cases = {
            "diverged": {"status": "diverged", "ahead_by": 1, "behind_by": 1, "files": []},
            "behind": {"status": "ahead", "ahead_by": 1, "behind_by": 1, "files": []},
            "no files": {"status": "ahead", "ahead_by": 1, "behind_by": 0},
            "malformed files": {"status": "ahead", "behind_by": 0, "files": [{"name": "x"}]},
            "truncated": {"status": "ahead", "behind_by": 0,
                          "files": [{"filename": f"x/{i}.py"} for i in range(300)]},
            "compare fails": failing,
        }
        for name, cmp in cases.items():
            with self.subTest(name):
                with self.assertRaises(handoff.HandoffRefused):
                    self.check(cmp=cmp)
        api = mock.Mock()
        api.get.side_effect = collect.CollectError("HTTP 502")
        with self.assertRaisesRegex(handoff.HandoffRefused, "head of main cannot be read"):
            self.check([], api=api)
        api.get.side_effect = None
        api.get.return_value = {"object": {"sha": "not a sha"}}
        with self.assertRaises(handoff.HandoffRefused):
            self.check([], api=api)

    def test_the_final_check_has_no_default(self):
        import inspect

        for function in (handoff.build_upstream_branch, handoff.post_package):
            parameter = inspect.signature(function).parameters["final_check"]
            self.assertIs(parameter.default, inspect.Parameter.empty, function.__name__)
            self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY, function.__name__)

    def test_the_workflow_passes_it_to_each_effect(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": CONTROL_REPO,
               "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "5",
               "UPSTREAMING_ISSUE_TOKEN": "t"}
        op_id = self.op["operation_id"]
        seen = []

        def effect(root, operation_id, **kwargs):
            seen.append(operation_id)
            kwargs["final_check"](self.record, self.op)
            raise AssertionError("written")

        out = self.root / "handoff/outcome.json"
        for action, name in (("branch", "build_upstream_branch"), ("comment", "post_package")):
            with self.subTest(action), mock.patch.dict("os.environ", env), \
                    mock.patch("os.getcwd", return_value=str(self.root)), \
                    mock.patch("common.load_policy", return_value=self.world.policy), \
                    mock.patch(f"handoff.{name}", effect), \
                    mock.patch("handoff.reviewed_series", return_value=b""), \
                    mock.patch("handoff.unchanged_on_main", side_effect=refuse) as final, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(handoff.main([action, "--operation-id", op_id, "--out", str(out)]), 0)
                result = json.loads(out.read_text())
                self.assertEqual((result["result"], result["failure"]), ("refused", None))
                final.assert_called_once()
        self.assertEqual(seen, [op_id, op_id])


class TestFinalCheckBeforeEachWrite(unittest.TestCase):
    """The final check runs immediately before each write, and a refusal
    there is an uncharged receipt that is not folded."""

    def test_the_package_is_not_posted_when_it_refuses(self):
        world = through("handoff.preparing_submission")
        receipt = world.run_workflow(final_check=refuse)
        self.assertEqual((receipt["result"], receipt["failure"]), ("refused", None))
        self.assertEqual(world.gh.comments, {})
        self.assertNotIn("POST", [c[0] for c in world.gh.calls])
        # A package already posted is discovered and read back: no write, no
        # check.
        first = world.run_workflow()
        again = world.run_workflow(final_check=lambda *_a: self.fail("checked without a write"))
        self.assertEqual((again["result"], again["comment_id"]), ("applied", first["comment_id"]))
        # The moved branch is checked first: it still returns for repair.
        world = through("handoff.preparing_submission")
        world.gh.branches["riscv64"] = "d" * 40
        receipt = world.run_workflow(final_check=refuse)
        self.assertEqual((receipt["result"], receipt["failure"]), ("failed", handoff.MOVED))

    def test_a_refusal_is_relaunched_and_a_paused_record_waits(self):
        world = reviewed()
        op_id = world.record["active_operation_id"]
        receipt = world.run_workflow(final_check=refuse)
        self.assertEqual(receipt["result"], "refused")
        self.assertEqual(world.gh.fork_writes(), [])
        out = world.tick(at(2), receipts=[receipt])
        self.assertEqual(world.kinds(out), [])
        self.assertEqual((world.state, world.operations[op_id]["status"]),
                         ("handoff.building_upstream_branch", "planned"))
        # The pause the final check saw: nothing is launched or closed while
        # paused, however long it lasts.
        out = world.tick(at(3), comments=[comment("/upstreaming pause", cid=930)])
        self.assertEqual(world.kinds(out), ["pause"])
        for minute in (20, 40, 60, 120):
            out = world.tick(at(minute))
            self.assertEqual((world.kinds(out), out.report["handoff_requests"]), ([], []))
            self.assertEqual(world.operations[op_id]["status"], "planned")
        # After the resume the same operation is launched again and confirms.
        out = world.tick(at(121), comments=[comment(
            f'/upstreaming resume revision={world.record["revision"]} reason="x"', cid=931)])
        self.assertEqual(world.kinds(out), ["resume_pause"])
        self.assertEqual(world.record["active_operation_id"], op_id)
        self.assertEqual([r["operation_id"] for r in out.report["handoff_requests"]], [op_id])
        world.tick(at(122), receipts=[world.run_workflow()])
        self.assertEqual(world.operations[op_id]["status"], "confirmed")
        self.assertNotEqual(world.state, "handoff.building_upstream_branch")

    def test_the_rerun_request_is_not_made_when_it_refuses(self):
        world = TestCleanHeadCi.other_tree(None)
        world.gh.ci_state = "cancelled"
        world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.active["kind"], "ci_rerun")
        before = list(world.gh.calls)
        with self.assertRaises(handoff.HandoffRefused):
            world.run_rerun(final_check=refuse)
        self.assertEqual([c for c in world.gh.calls if c not in before and c[0] == "POST"], [])
        self.assertEqual(world.run_rerun()["status"], "rerun")

    def test_the_rerun_job_writes_no_receipt_when_it_refuses(self):
        world = TestCleanHeadCi.other_tree(None)
        world.gh.ci_state = "cancelled"
        world.tick(at(2), receipts=[world.run_workflow()])
        op_id = world.record["active_operation_id"]
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": CONTROL_REPO,
               "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "5", "UPSTREAMING_FORK_TOKEN": "t"}
        calls = []

        def run_operation(root, operation_id, api, p, run_id, *, phase, final_check):
            calls.append(phase)
            if phase == "handoff":
                final_check(world.record, world.active)
            else:
                self.assertIsNone(final_check)
            return {"status": "rerun"}

        with tempfile.TemporaryDirectory() as directory:
            root = world.root(Path(directory))
            out = root / "ci-rerun/outcome.json"
            with mock.patch.dict("os.environ", env), mock.patch("os.getcwd", return_value=str(root)), \
                    mock.patch("common.load_policy", return_value=world.policy), \
                    mock.patch("ci_rerun.run_operation", run_operation), \
                    mock.patch("handoff.unchanged_on_main", side_effect=refuse) as final, \
                    contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(ci_rerun.main(["operation", "--operation-id", op_id,
                                                "--phase", "handoff", "--out", str(out)]), 1)
                self.assertFalse(out.exists())
                self.assertEqual(json.loads(printed.getvalue())["result"], "refused")
                final.assert_called_once()
                # The development phase passes no final check (out of scope).
                self.assertEqual(ci_rerun.main(["operation", "--operation-id", op_id,
                                                "--out", str(out)]), 0)
                self.assertTrue(out.exists())
        self.assertEqual(calls, ["handoff", "development"])


class TestOperationTable(unittest.TestCase):
    def test_fires_and_counts(self):
        for (state, kind), fires in recovery.FIRES.items():
            if kind not in handoff.STATE_OF:
                continue
            self.assertEqual(handoff.STATE_OF[kind], state)
            for verbs in fires.values():
                for verb in verbs:
                    self.assertIn(verb, engine.PIPELINE_VERBS)
        self.assertEqual(engine.HELD_COMMANDS, {})
        for verb in ("submit_upstream", "retract_submission", "accept_clean_head_ci",
                     "verify_upstream_pr", "record_audit_observation", "request_submission_review",
                     "accept_submission_review", "reject_submission_review"):
            self.assertIn(verb, engine.PIPELINE_VERBS)
        # The interrupted-CI rows are taken by the observation of
        # development CI only; CI on the clean head does not take them yet.
        for verb in ("rerun_interrupted_ci", "exhaust_interrupted_reruns"):
            self.assertEqual(engine.PIPELINE_VERBS[verb], "observe_ci")
        # Nothing closes the development PR.
        self.assertEqual(set(handoff.KINDS.values()), {"upstream_branch", "submission_comment"})
        for name in ("close_development_pr", "DEVELOPMENT_MARKER", "cleanup"):
            self.assertFalse(hasattr(handoff, name), name)
        self.assertNotIn("closing_internal_pr", lifecycle.ACTIVITIES)

    def test_retract_takes_no_reason(self):
        import commands
        self.assertNotIn("reason", lifecycle.find_edge("handoff.waiting_upstream_pr",
                                                       "retract_submission").transition.guard)
        with self.assertRaises(commands.CommandError):
            commands.parse(f'/upstreaming retract-pr revision=6 head={CLEAN} reason="x"')


class TestUpstreamBranchWithGit(unittest.TestCase):
    """The build of `riscv64` against a local bare fork: the reviewed series
    is rebuilt and checked again, `riscv64` is created or replaced under the
    exact-head lease, never while an upstream PR is open from it, and
    `riscv64-dev` is only read."""

    def setUp(self):
        import fork_publish
        from tests.helpers import commit_files, contribution, git, init_repo

        self.git, self.commit_files = git, commit_files
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (fork_publish.URL_TEMPLATE, list(fork_publish.TEST_EXTRA_CONFIG))
        fork_publish.URL_TEMPLATE = f"file://{root}/{{}}.git"
        fork_publish.TEST_EXTRA_CONFIG[:] = ["-c", "protocol.file.allow=always"]
        self.work = init_repo(root / "work")
        self.base = commit_files(self.work, {"setup.py": "a\n", "ci.yml": "jobs: []\n"}, msg="base")
        self.bare = root / "riseproject-upstreaming/proj.git"
        self.bare.parent.mkdir(parents=True)
        git(root, "init", "-q", "--bare", "-b", "main", str(self.bare))
        (root / "example").mkdir()
        git(root, "clone", "-q", "--bare", str(self.bare), str(root / "example/proj.git"))
        self.root = root
        self.contribution = contribution
        self.upstream_prs: list[dict] = []
        self.fork_prs: list[dict] = []

    def tearDown(self):
        import fork_publish
        fork_publish.URL_TEMPLATE, extra = self.saved
        fork_publish.TEST_EXTRA_CONFIG[:] = extra
        self.tmp.cleanup()

    def development(self, *commits):
        """The approved series on `riscv64-dev`, and the base on the upstream."""
        for message, files in commits:
            self.commit_files(self.work, files, msg=message)
        head = self.git(self.work, "rev-parse", "HEAD")
        self.git(self.work, "push", "-q", "-f", str(self.bare), "HEAD:refs/heads/riscv64-dev")
        self.git(self.work, "push", "-q", "-f", str(self.root / "example/proj.git"),
                 f"{self.base}:refs/heads/main")
        return head

    def reviewed_series(self, approved, *messages, squash=False):
        """The reviewed series: the non-debug commits of the approved series
        cherry-picked on the base with new messages."""
        work = self.root / f"review-{len(messages)}-{squash}"
        self.git(self.root, "clone", "-q", str(self.bare), str(work))
        self.git(work, "checkout", "-q", "-B", "review", self.base)
        picks = [s for s in self.git(work, "rev-list", "--reverse", f"{self.base}..{approved}").split()
                 if not self.git(work, "log", "-1", "--format=%s", s).startswith("{debug}")]
        for sha, message in zip(picks, messages):
            self.git(work, "-c", "user.name=t", "-c", "user.email=t@t", "cherry-pick", sha)
            self.git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--amend",
                     "-m", message)
        if squash:
            self.git(work, "reset", "-q", "--soft", self.base)
            self.git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m",
                     messages[0])
        return self.git(work, "format-patch", "--stdout", self.base).encode() + b"\n"

    def branch(self, ref="riscv64"):
        out = self.git(self.root, "--git-dir", str(self.bare), "for-each-ref", "--format=%(objectname)",
                       f"refs/heads/{ref}")
        return out or None

    def state(self, approved, mbox, *, scope=("setup.py", "ci.yml")):
        """The committed state of a record whose review passed: the check the
        worker's publish job runs, folded, and the build operation planned."""
        task = {"task": "submission_rewrite", "contribution_id": "example--proj/12", "revision": 4,
                "expected_head": approved, "base_sha": self.base, "fork": "riseproject-upstreaming/proj",
                "upstream": "example/proj", "allowed_paths": list(scope), "context": {}}
        rewrite = {"schema_version": 1, "contribution_id": "example--proj/12", "expected_revision": 4,
                   "expected_head": approved, "verdict": "pass", "reasons": [], "title": "Build",
                   "body": "Body", "series_digest": hashlib.sha256(mbox).hexdigest()}
        checked = submission.check_rewrite(task, rewrite, mbox,
                                           workdir=self.root / f"check-{approved[:6]}")
        self.assertEqual((checked["verdict"], checked["reasons"]), ("pass", []))
        record = self.contribution(phase="handoff", activity="agent_running", revision=4,
                                   scope_paths=list(scope),
                                   upstream={"repository_id": 1, "full_name": "example/proj",
                                             "base_ref": "main", "base_sha": self.base})
        record["fork"]["head_sha"] = approved
        record["handoff_approval"] = {"command_id": "c-1", "actor": dict(OWNER), "head": approved,
                                      "revision": 4, "evidence_ids": []}
        record["active_work"] = {"dedupe_key": "k", "task": "submission_rewrite", "generation": 0,
                                 "revision": 4, "attempt": 1, "requested_at": T0,
                                 "deadline_at": T0, "dispatched_run_id": None, "reason_ids": [], "feedback_evidence_ids": []}
        record, staged = reconcile.fold_submission_rewrite(
            record, dict(checked, publication_artifact_id=77), now=T0, policy=policy(),
            worker_run_id="7001")
        self.assertEqual(engine.state_key(record), "handoff.review_pending")
        evidence = {r["evidence_id"]: r for k, r in staged if k == "evidence"}
        record = dict(record, activity="reviewing", active_work=dict(
            record.get("active_work") or {}, dedupe_key="k2", task="submission_review",
            generation=0, revision=record["revision"], attempt=1, requested_at=T0,
            deadline_at=T0, dispatched_run_id=None, reason_ids=[]))
        review_task = {"task": "submission_review", "contribution_id": "example--proj/12",
                       "revision": record["revision"], "expected_head": approved,
                       "base_sha": self.base,
                       "context": {"rewrite": work_plan.rewrite_reference(
                           evidence[record["submission_rewrite_id"]])}}
        reviewed = submission.check_review(review_task, {
            "schema_version": 1, "contribution_id": "example--proj/12",
            "expected_revision": record["revision"], "expected_head": approved, "verdict": "pass",
            "reasons": [], "series_digest": checked["series_digest"],
            "text_digest": checked["text_digest"],
            "expectations": [{"expectation": "The text follows the template", "met": True}]})
        record, staged = reconcile.fold_submission_review(
            record, dict(reviewed, publication_artifact_id=78), now=T0, policy=policy(),
            worker_run_id="7002", evidence=evidence)
        self.assertEqual(engine.state_key(record), "handoff.building_upstream_branch")
        evidence.update({r["evidence_id"]: r for k, r in staged if k == "evidence"})
        op = handoff.plan_attempt(record, {}, policy=policy(), now=T0, evidence=evidence)
        record["active_operation_id"] = op["operation_id"]
        state = self.root / f"state-{op['operation_id']}"
        for rel, value in ((f".state/contributions/{contribution_file_id(record['contribution_id'])}.json",
                            record), (f".state/operations/{op['operation_id']}.json", op),
                           *((f".state/evidence/{eid}.json", ev) for eid, ev in evidence.items())):
            (state / rel).parent.mkdir(parents=True, exist_ok=True)
            (state / rel).write_text(canonical_json(value))
        return state, op, checked

    def build(self, state, op, mbox, *, observed=None, push=None, final_check=None):
        def branch_head(_api, _name, ref):
            if observed:
                return {"head": observed.pop(0)}
            head = self.branch(ref)
            return {"head": head} if head else None

        def guarded(api, name, ref):
            # `riscv64-dev` is read by the guard before the lease's reads.
            if ref == "riscv64-dev":
                head = self.branch(ref)
                return {"head": head} if head else None
            return branch_head(api, name, ref)

        with mock.patch("collect.branch_head", guarded), \
                mock.patch("collect.upstream_pull_requests", lambda *_a, **_k: list(self.upstream_prs)), \
                mock.patch("collect.fork_pull_requests", lambda *_a, **_k: list(self.fork_prs)):
            return handoff.build_upstream_branch(state, op["operation_id"], policy=policy(),
                                                 read_api=object(), token="t", series=mbox, push=push,
                                                 final_check=final_check or (lambda record, op: None))

    def test_riscv64_is_not_written_while_it_heads_a_fork_pr_or_without_riscv64_dev(self):
        approved = self.development(("Build riscv64", {"setup.py": "b\n"}))
        mbox = self.reviewed_series(approved, "ci: Build riscv64 wheels")
        state, op, _checked = self.state(approved, mbox)
        # A pull request of the fork is open from `riscv64`.
        self.fork_prs = [{"number": 1, "state": "open", "head": {"ref": "riscv64"}}]
        with self.assertRaisesRegex(handoff.HandoffBlocked, "open from riscv64: #1"):
            self.build(state, op, mbox)
        self.fork_prs = []
        # `riscv64-dev` is missing.
        self.git(self.root, "--git-dir", str(self.bare), "update-ref", "-d", "refs/heads/riscv64-dev")
        with self.assertRaisesRegex(handoff.HandoffBlocked, "riscv64-dev is missing"):
            self.build(state, op, mbox)
        self.git(self.root, "--git-dir", str(self.bare), "update-ref", "refs/heads/riscv64-dev",
                 approved)
        # The record names another development branch.
        path = next((state / ".state/contributions").iterdir())
        record = json.loads(path.read_text())
        record["fork"]["head_ref"] = "riscv64"
        path.write_text(canonical_json(record))
        with self.assertRaisesRegex(handoff.HandoffBlocked, "development branch is 'riscv64'"):
            self.build(state, op, mbox)
        self.assertIsNone(self.branch())
        # Each refusal is a receipt that fires `require_branch_intervention`.
        receipt = handoff.receipt({"operation_id": op["operation_id"]},
                                  error=handoff.HandoffBlocked("riscv64-dev is missing"))
        self.assertEqual((receipt["result"], receipt["failure"]), ("failed", handoff.BLOCKED))

    def test_riscv64_is_created_from_the_reviewed_series(self):
        approved = self.development(("Build riscv64", {"setup.py": "b\n"}),
                                    ("{debug} trace", {"ci.yml": "jobs: [debug]\n"}),
                                    ("Test riscv64", {"setup.py": "c\n"}))
        mbox = self.reviewed_series(approved, "ci: Build riscv64 wheels", "ci: Test riscv64 wheels")
        state, op, checked = self.state(approved, mbox)
        result = self.build(state, op, mbox)
        self.assertEqual((result["clean_head"], result["written"], result["replaced"]),
                         (checked["head"], True, None))
        self.assertEqual(self.branch(), checked["head"])
        self.assertEqual(self.branch("riscv64-dev"), approved)
        log = self.git(self.root, "--git-dir", str(self.bare), "log", "--format=%s|%an",
                       f"{self.base}..riscv64").splitlines()
        self.assertEqual(log, ["ci: Test riscv64 wheels|riseproject-upstreaming[bot]",
                               "ci: Build riscv64 wheels|riseproject-upstreaming[bot]"])
        self.assertEqual(self.git(self.root, "--git-dir", str(self.bare), "show", "riscv64:ci.yml"),
                         "jobs: []")
        # A second run finds it written and reads it back without a write.
        pushes = []
        # An upstream PR open from `riscv64` blocks a write only: the branch
        # already at the reviewed head is read back without it.
        self.upstream_prs = [{"number": 9}]
        again = self.build(state, op, mbox, push=lambda *a: pushes.append(a))
        self.assertEqual((again["written"], pushes), (False, []))

    def test_the_final_check_runs_immediately_before_the_push(self):
        approved = self.development(("Build riscv64", {"setup.py": "b\n"}))
        mbox = self.reviewed_series(approved, "Build riscv64 wheels")
        state, op, checked = self.state(approved, mbox)
        pushes = []
        with self.assertRaises(handoff.HandoffRefused) as caught:
            self.build(state, op, mbox, push=lambda *a: pushes.append(a), final_check=refuse)
        self.assertEqual((pushes, self.branch()), ([], None))
        receipt = handoff.receipt({"operation_id": op["operation_id"]}, error=caught.exception)
        self.assertEqual((receipt["result"], receipt["failure"]), ("refused", None))
        # It runs after the lease read and the open-PR check, directly before
        # the push, with the record and operation the effect loaded.
        events = []
        real_head = self.branch

        def final_check(record, loaded):
            self.assertEqual(loaded["operation_id"], op["operation_id"])
            self.assertEqual(record["active_operation_id"], op["operation_id"])
            events.append("check")

        def push(work, args, auth):
            events.append("push")
            import fork_publish
            return handoff._git(work, "push", "--porcelain", fork_publish._url("riseproject-upstreaming/proj"),
                                *args, check=False, env=auth)

        def lease(_api, _name, ref):
            events.append(f"read {ref}")
            head = real_head(ref)
            return {"head": head} if head else None

        with mock.patch("collect.branch_head", lease), \
                mock.patch("collect.upstream_pull_requests",
                           lambda *_a, **_k: events.append("prs") or []), \
                mock.patch("collect.fork_pull_requests", lambda *_a, **_k: []):
            handoff.build_upstream_branch(state, op["operation_id"], policy=policy(), read_api=object(),
                                          token="t", series=mbox, push=push, final_check=final_check)
        self.assertEqual(events, ["read riscv64-dev", "read riscv64", "prs", "check", "push",
                                  "read riscv64"])
        self.assertEqual(self.branch(), checked["head"])
        # Already at the reviewed head: read back, nothing written, no check.
        again = self.build(state, op, mbox, final_check=lambda *_a: self.fail("checked without a write"))
        self.assertFalse(again["written"])

    def test_the_ci_dispatch_has_its_own_final_check(self):
        approved = self.development(("Build riscv64", {"setup.py": "b\n"}),
                                    ("{debug} trace", {"ci.yml": "jobs: [debug]\n"}))
        mbox = self.reviewed_series(approved, "Build riscv64 wheels")
        state, op, checked = self.state(approved, mbox)
        checks, dispatched = [], []

        def second_refuses(record, loaded):
            checks.append(len(checks))
            if len(checks) == 2:
                refuse()

        def fake_dispatch_ci(_api, *, fork, head, workflow_path, dispatch):
            # A dispatch is really made.
            dispatch(f"/repos/{fork}/actions/workflows/build.yml/dispatches",
                     {"ref": "refs/heads/riscv64"})
            return {"ci_dispatch": handoff.DISPATCHED, "ci_workflow": ".github/workflows/build.yml"}

        with mock.patch("handoff.dispatch_clean_head_ci", fake_dispatch_ci):
            with self.assertRaises(handoff.HandoffRefused) as caught, \
                    mock.patch("collect.branch_head",
                               lambda _a, _n, ref: {"head": self.branch(ref)} if self.branch(ref) else None), \
                    mock.patch("collect.upstream_pull_requests", lambda *_a, **_k: []), \
                    mock.patch("collect.fork_pull_requests", lambda *_a, **_k: []):
                handoff.build_upstream_branch(
                    state, op["operation_id"], policy=policy(), read_api=object(), token="t",
                    series=mbox, final_check=second_refuses,
                    dispatch=lambda path, body: dispatched.append(path))
        # The push happened, the dispatch did not, and the receipt is an
        # uncharged refusal.
        self.assertEqual((self.branch(), dispatched, checks), (checked["head"], [], [0, 1]))
        receipt = handoff.receipt({"operation_id": op["operation_id"]}, error=caught.exception)
        self.assertEqual((receipt["result"], receipt["failure"]), ("refused", None))

    def test_a_squashed_series_with_the_same_tree_is_built(self):
        approved = self.development(("Build riscv64", {"setup.py": "b\n"}),
                                    ("Test riscv64", {"setup.py": "c\n"}))
        mbox = self.reviewed_series(approved, "Build and test riscv64 wheels", "x", squash=True)
        state, op, checked = self.state(approved, mbox)
        self.assertEqual(len(checked["commits"]), 1)
        self.assertEqual(self.build(state, op, mbox)["clean_head"], self.branch())

    def test_riscv64_is_replaced_under_the_lease_and_never_under_an_open_pr(self):
        approved = self.development(("Build riscv64", {"setup.py": "b\n"}))
        mbox = self.reviewed_series(approved, "Build riscv64 wheels")
        old = self.git(self.work, "rev-parse", "HEAD")
        self.git(self.work, "push", "-q", str(self.bare), f"{self.base}:refs/heads/riscv64")
        state, op, checked = self.state(approved, mbox)
        self.upstream_prs = [{"number": 9}]
        with self.assertRaises(handoff.HandoffBlocked):
            self.build(state, op, mbox)
        self.assertEqual(self.branch(), self.base)
        self.upstream_prs = []
        # The branch moves between the final check and the push: the lease
        # refuses the write.
        moved = self.development(("Other", {"ci.yml": "jobs: [x]\n"}))
        self.git(self.work, "push", "-q", "-f", str(self.bare), f"{moved}:refs/heads/riscv64")
        with self.assertRaises(handoff.HandoffInvalid):
            self.build(state, op, mbox, observed=[self.base, self.base])
        self.assertEqual(self.branch(), moved)
        result = self.build(state, op, mbox)
        self.assertEqual((result["replaced"], self.branch()), (moved, checked["head"]))
        self.assertEqual(self.branch("riscv64-dev"), moved)
        del old

    def test_a_series_that_is_not_the_reviewed_one_is_invalid(self):
        approved = self.development(("Build riscv64", {"setup.py": "b\n"}))
        mbox = self.reviewed_series(approved, "Build riscv64 wheels")
        state, op, _checked = self.state(approved, mbox)
        with self.assertRaisesRegex(handoff.HandoffInvalid, "accepted review"):
            self.build(state, op, mbox + b"\n")
        self.assertIsNone(self.branch())

    def test_the_token_never_reaches_a_command_line(self):
        import subprocess as sp

        import fork_publish
        approved = self.development(("Build riscv64", {"setup.py": "b\n"}))
        mbox = self.reviewed_series(approved, "Build riscv64 wheels")
        state, op, _checked = self.state(approved, mbox)
        calls, real = [], sp.run

        def run(argv, *args, **kwargs):
            calls.append((argv, kwargs.get("env") or {}))
            return real(argv, *args, **kwargs)

        with mock.patch.object(fork_publish.subprocess, "run", run):
            self.build(state, op, mbox)
        secret = fork_publish._auth_env("t")["GIT_CONFIG_VALUE_0"]
        self.assertTrue(calls)
        self.assertFalse([argv for argv, _ in calls if any("extraheader" in a or secret in a for a in argv)])
        pushes = [argv for argv, env in calls if "push" in argv]
        self.assertTrue(all(env.get("GIT_CONFIG_VALUE_0") == secret
                            for argv, env in calls if "push" in argv))
        self.assertEqual(len(pushes), 1)
        self.assertIn("--force-with-lease=refs/heads/riscv64:", pushes[0])
        # The write token is never a read token: every fetch is anonymous.
        self.assertTrue([argv for argv, _ in calls if "fetch" in argv])
        self.assertFalse([argv for argv, env in calls if "push" not in argv
                          and env.get("GIT_CONFIG_VALUE_0") == secret])

    def test_the_series_is_checked_before_the_token_and_only_pushed_after(self):
        """The workflow's series step rebuilds and checks the series without
        any token; after the mint the branch step only verifies the digest,
        head, and tree and pushes, fetching nothing."""
        import fork_publish
        approved = self.development(("Build riscv64", {"setup.py": "b\n"}),
                                    ("Test riscv64", {"setup.py": "c\n"}))
        mbox = self.reviewed_series(approved, "Build riscv64 wheels", "Test riscv64 wheels")
        state, op, checked = self.state(approved, mbox)
        work = self.root / "prepared"
        real_auth = fork_publish._auth_env

        def anonymous(token):
            self.assertIsNone(token)
            return real_auth(token)

        with mock.patch("fork_publish._auth_env", anonymous):
            out = handoff.check_upstream_series(state, op["operation_id"], policy=policy(),
                                                series=mbox, workdir=work)
        self.assertEqual(out, {"head": checked["head"], "needs_ci": False})
        # A series that is not the reviewed one, or a work directory the
        # series step did not prepare, is not pushed.
        with self.assertRaisesRegex(handoff.HandoffInvalid, "accepted review"):
            self.build_prepared(state, op, mbox + b"x", work)
        with self.assertRaisesRegex(handoff.HandoffError, "not prepared"):
            self.build_prepared(state, op, mbox, self.root / "nothing")
        with mock.patch("fork_publish.rebuild", side_effect=AssertionError("fetched")):
            result = self.build_prepared(state, op, mbox, work)
        self.assertEqual((result["clean_head"], self.branch()), (checked["head"], checked["head"]))

    def build_prepared(self, state, op, mbox, work):
        def branch_head(_api, _name, ref):
            head = self.branch(ref)
            return {"head": head} if head else None

        with mock.patch("collect.branch_head", branch_head), \
                mock.patch("collect.upstream_pull_requests", lambda *_a, **_k: []), \
                mock.patch("collect.fork_pull_requests", lambda *_a, **_k: []):
            return handoff.build_upstream_branch(state, op["operation_id"], policy=policy(),
                                                 read_api=object(), token="t", series=mbox,
                                                 workdir=work, prepared=True,
                                                 final_check=lambda record, op: None)


class TestCollector(unittest.TestCase):
    def test_compare_accepts_exact_commits_only(self):
        class Api:
            base = "https://api.github.com"

            def __init__(self):
                self.urls = []

            def _request(self, url):
                self.urls.append(url)
                return {"commits": []}, {}

        api = Api()
        self.assertEqual(collect.compare_commits(api, FORK["name"], BASE, HEAD), {"commits": []})
        self.assertEqual(api.urls, [f"https://api.github.com{FORK_PATH}/compare/{BASE}...{HEAD}?per_page=100"])
        for name, base, head in ((FORK["name"], "main", HEAD), (FORK["name"], BASE, "../x"),
                                 ("a/../b", BASE, HEAD), ("riseproject-upstreaming", BASE, HEAD)):
            with self.subTest(name=name, base=base, head=head), self.assertRaises(collect.CollectError):
                collect.compare_commits(api, name, base, head)

    def test_upstream_candidates_are_read_from_the_upstream_repository(self):
        record = migrated()
        api = mock.Mock()
        api.get.return_value = [upstream_pr()]
        prs = collect.upstream_pull_requests(api, record)
        api.get.assert_called_once_with(
            "/repos/closeio/ciso8601/pulls?state=open&head=riseproject-upstreaming%3Ariscv64&per_page=100")
        self.assertEqual(prs, [handoff.normalize_pull(upstream_pr())])
        # Whoever opened it: a pull request of another account is returned too.
        api.get.return_value = [upstream_pr(user={"id": 4242, "login": "someone", "type": "User"})]
        self.assertEqual(collect.upstream_pull_requests(api, record),
                         [handoff.normalize_pull(upstream_pr(
                             user={"id": 4242, "login": "someone", "type": "User"}))])
        api.get.return_value = {"message": "x"}
        with self.assertRaises(collect.CollectError):
            collect.upstream_pull_requests(api, record)
