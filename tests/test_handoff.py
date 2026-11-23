# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The handoff operations on the live closeio--ciso8601/1 record.

`/upstreaming submit-upstream` on the migrated live record runs the whole
handoff by the table: the debug cleanup (a verified no-op on the live branch),
CI on the clean head, closing the development PR, the submission package,
and the locked wait for the upstream PR with its three exits. The fake GitHub
below answers the reads of the controller and the trusted workflow, records
every call, and holds the resources the workflow changes.
"""

import copy
import hashlib
import json
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import capacity
import collect
import engine
import handoff
import lifecycle
import reconcile
import recovery
import status_ledger
from common import CONTROL_REPO, canonical_json, contribution_file_id
from tests.helpers import OWNER, STRANGER, policy
from tests.test_live_handoff_migration import (
    CI_EVIDENCE,
    CID,
    FIXTURES,
    FORK,
    HEAD,
    SUBMIT,
    comment,
    load,
    migrated,
)

DOSSIER = "dossier-closeio--ciso8601--1-r2"
BASE = "cbe05770dc95966a9bf0bff32bbcfa8ba0a3cbf8"
UPSTREAM_ID = 20633992
FORK_PATH = "/repos/riseproject-upstreaming/ciso8601"
APP = {"id": 41898282, "login": "riseproject-dev[bot]", "type": "Bot"}
DEV_MARKER = f"<!-- upstreaming-development:{CID}:r5:{HEAD} -->"
T0 = "2026-11-23T09:00:00+00:00"


def at(minutes: int, day: str = "2026-11-23") -> datetime:
    return datetime.fromisoformat(f"{day}T09:00:00+00:00") + timedelta(minutes=minutes)


class FakeGitHub:
    """The resources the handoff reads and changes, and every call made."""

    def __init__(self):
        self.branch = HEAD
        self.compare = json.loads((FIXTURES / "fork-compare.json").read_text())
        self.dev_pr = {"id": 5437693983, "number": 1, "state": "open", "merged": False,
                       "html_url": "https://github.com/riseproject-upstreaming/ciso8601/pull/1",
                       "head": {"sha": HEAD, "ref": "riscv64", "repo": {"id": FORK["repository_id"]}},
                       "base": {"ref": "master"}, "body": "development\n\n" + DEV_MARKER}
        self.comments: dict[int, dict] = {}
        self.upstream: list[dict] = []
        self.ci_state = "success"
        self.calls: list[tuple[str, str]] = []
        self.fail_post = False

    # Reads of the controller (collect.Api.get) and of the workflow.
    def get(self, path: str):
        self.calls.append(("GET", path))
        if path == f"{FORK_PATH}/pulls/1":
            return copy.deepcopy(self.dev_pr)
        if path.startswith(f"/repos/{CONTROL_REPO}/issues/comments/"):
            return copy.deepcopy(self.comments[int(path.rsplit("/", 1)[1])])
        raise collect.CollectError(f"GET {path}: HTTP 404")

    def branch_head(self, _api, full_name, ref):
        self.calls.append(("GET", f"/repos/{full_name}/branches/{ref}"))
        return {"head": self.branch, "tree": "t" * 40, "parents": [BASE]}

    def compare_commits(self, _api, full_name, base, head):
        self.calls.append(("GET", f"/repos/{full_name}/compare/{base}...{head}"))
        if head != HEAD:
            raise collect.CollectError("unknown head")
        return copy.deepcopy(self.compare)

    def upstream_pull_requests(self, _api, record, ref="riscv64"):
        self.calls.append(("GET", f"/repos/{record['upstream']['full_name']}/pulls?state=open&"
                                  f"head=riseproject-upstreaming:{ref}&per_page=100"))
        return [handoff.normalize_pull(pr) for pr in self.upstream]

    def ci_snapshot(self, _api, full_name, head):
        self.calls.append(("GET", f"/repos/{full_name}/actions/runs?head_sha={head}"))
        run = {"id": 45008094068, "head_sha": head, "run_attempt": 1, "status": "completed",
               "conclusion": self.ci_state, "workflow_id": 426522359, "event": "pull_request",
               "path": ".github/workflows/lint.yml"}
        jobs = [{"id": 1, "run_id": run["id"], "name": "Build - ubuntu-24.04-riscv (riscv64)",
                 "status": "completed", "conclusion": self.ci_state, "started_at": "x",
                 "labels": ["ubuntu-24.04-riscv"]}]
        return collect.CiSnapshot(repository={"id": FORK["repository_id"], "full_name": FORK["name"]},
                                  head_sha=head, runs=[run], jobs_by_run={run["id"]: jobs},
                                  artifacts_by_run={})

    # The writes of the trusted workflow.
    def mutate(self, method, path, value):
        self.calls.append((method, path))
        assert method == "PATCH" and path == f"{FORK_PATH}/pulls/1" and value == {"state": "closed"}
        self.dev_pr["state"] = "closed"
        return copy.deepcopy(self.dev_pr)

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


class World:
    """The live record under the controller, with the trusted workflow run
    against the state the controller committed."""

    def __init__(self, record=None, p=None):
        self.record = record or migrated()
        self.operations: dict[str, dict] = {}
        self.evidence = {CI_EVIDENCE: load("evidence-ci.json"), DOSSIER: load("dossier.json")}
        self.consumed: dict[int, dict] = {}
        self.events: list[dict] = []
        self.gh = FakeGitHub()
        self.policy = p or policy()
        self.reports: list[dict] = []

    def audited(self):
        keys = {(e.get("details") or {}).get("observation_key") for e in self.events
                if e["kind"] == "record_audit_observation"}
        return {CID: {k for k in keys if k}}

    def tick(self, now: datetime, *, comments=(), receipts=()):
        gh = self.gh
        snap = reconcile.Snapshot({"c": self.record}, copy.deepcopy(self.operations),
                                  dict(self.consumed), {}, dict(self.evidence), self.audited())
        with mock.patch("collect.branch_head", gh.branch_head), \
                mock.patch("collect.compare_commits", gh.compare_commits), \
                mock.patch("collect.ci_snapshot", gh.ci_snapshot), \
                mock.patch("collect.upstream_pull_requests", gh.upstream_pull_requests), \
                mock.patch("collect.development_review_feedback", return_value=None):
            out = reconcile.tick(gh, snap, policy=self.policy, now=now, publications=[],
                                 comments=list(comments), handoffs=list(receipts),
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

    def run_workflow(self, op_id: str | None = None) -> dict:
        """The receipt the trusted workflow uploads for the operation."""
        op = self.operations[op_id or self.record["active_operation_id"]]
        meta = {"operation_id": op["operation_id"], "kind": op["kind"],
                "contribution_id": CID, "run_id": "777"}
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(Path(directory))
            try:
                if op["kind"] == "debug_cleanup":
                    # The live branch has no debug commit: the cleanup reads the
                    # branch back at the approved head and writes nothing.
                    handoff.load_request(root, op["operation_id"], self.policy)
                    if self.gh.branch != op["target"]["expected_head"]:
                        raise handoff.HandoffInvalid("the exact-head lease conflicts")
                    result = {"result": "applied", "clean_head": self.gh.branch,
                              "rewritten": False, "removed_commits": []}
                elif op["kind"] == "internal_pr_closure":
                    result = handoff.close_development_pr(root, op["operation_id"],
                                                          policy=self.policy, api=self.gh)
                else:
                    with mock.patch("collect.compare_commits", self.gh.compare_commits):
                        result = handoff.post_package(root, op["operation_id"], policy=self.policy,
                                                      read_api=self.gh, issue_api=self.gh)
                return handoff.receipt(meta, result)
            except Exception as error:
                return handoff.receipt(meta, error=error)


def failed(world: World, failure=handoff.INFRASTRUCTURE) -> dict:
    op = world.active
    return {"schema_version": 1, "operation_id": op["operation_id"], "kind": op["kind"],
            "contribution_id": CID, "run_id": "778", "result": "failed", "failure": failure,
            "reason": "HTTP 502"}


def submitted(world: World | None = None) -> World:
    world = world or World()
    world.tick(at(0), comments=[comment(SUBMIT)])
    return world


def through(target: str) -> World:
    """The live record taken along the success path up to ``target``."""
    world = submitted()
    minute = 1
    while world.state != target:
        receipt = world.run_workflow()
        world.tick(at(minute), receipts=[receipt])
        minute += 1
        if minute > 10:
            raise AssertionError(f"stuck in {world.state}")
    return world


class TestLivePath(unittest.TestCase):
    def test_submit_to_the_upstream_pr_wait_and_verification(self):
        world = World()
        out = world.tick(at(0), comments=[comment(SUBMIT)])
        self.assertEqual(world.kinds(out), ["submit_upstream"])
        self.assertEqual(world.state, "handoff.cleaning_debug")
        approval = world.record["handoff_approval"]
        self.assertEqual((approval["head"], approval["revision"], approval["actor"]),
                         (HEAD, 6, {"id": OWNER["id"], "login": OWNER["login"]}))
        self.assertEqual(approval["evidence_ids"], [CI_EVIDENCE, DOSSIER])
        cleanup = world.active
        self.assertEqual((cleanup["kind"], cleanup["status"], cleanup["reason_ids"][1:]),
                         ("debug_cleanup", "planned", ["attempt:1", "count:1"]))
        self.assertEqual(cleanup["target"], {"repository": FORK["name"],
                                             "repository_id": FORK["repository_id"],
                                             "ref": "riscv64", "expected_head": HEAD,
                                             "base_sha": BASE})
        self.assertEqual(out.report["handoff_requests"],
                         [{"operation_id": cleanup["operation_id"], "contribution_id": CID,
                           "kind": "debug_cleanup", "attempt": 1}])
        # No Work, no other launch, nothing remote yet.
        self.assertEqual(out.report["requested_effects"], [])
        self.assertEqual(world.gh.calls.count(("PATCH", f"{FORK_PATH}/pulls/1")), 0)

        # The cleanup receipt: the branch is read back at the same head; the
        # same tick observes CI on it and accepts the evidence already there.
        out = world.tick(at(1), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "accept_clean_head_ci"])
        self.assertEqual(world.operations[cleanup["operation_id"]]["status"], "confirmed")
        self.assertEqual((world.record["clean_head_sha"], world.record["fork"]["head_sha"]),
                         (HEAD, HEAD))
        self.assertEqual(world.record["last_ci_evidence_id"], CI_EVIDENCE)
        self.assertEqual(world.state, "handoff.closing_internal_pr")
        closure = world.active
        self.assertEqual((closure["kind"], closure["external_ids"]),
                         ("internal_pr_closure", {"pr_number": 1, "launch_attempts": 1}))
        self.assertEqual([r["kind"] for r in out.report["handoff_requests"]], ["internal_pr_closure"])

        out = world.tick(at(2), receipts=[world.run_workflow()])
        self.assertEqual(world.gh.dev_pr["state"], "closed")
        self.assertEqual(world.kinds(out), ["confirm_internal_pr_closed"])
        self.assertEqual(world.state, "handoff.preparing_submission")
        package_op = world.active
        self.assertEqual(package_op["target"]["ref"], "issues/1")

        out = world.tick(at(3), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_submission_package"])
        self.assertEqual(world.state, "handoff.waiting_upstream_pr")
        self.assertEqual(world.record["pending_human_action"]["kind"], "open_upstream_pr")
        self.assertEqual(world.record["handoff_request"],
                         {"command_id": package_op["operation_id"], "head": HEAD, "revision": 6})
        self.assertIsNone(world.record["active_operation_id"])
        [posted] = world.gh.comments.values()
        self.assertEqual(posted["body"], handoff.build_package(
            world.record, dossier=load("dossier.json"), ci=world.evidence[CI_EVIDENCE],
            commits=[{"sha": HEAD, "message": world.gh.compare["commits"][0]["commit"]["message"]}],
            files=[".github/workflows/build-wheels.yml", "pyproject.toml"])["comment"])
        self.assertEqual((world.record["revision"], world.record["fork"], world.record["generation"]),
                         (6, FORK, 0))
        # The ledger entry links the package.
        entry = status_ledger.render(world.events[-1], world.record, "https://run")
        self.assertIn(posted["html_url"], entry)
        self.assertIn(f"/upstreaming retract-pr revision=6 head={HEAD}", entry)

        # The human opens the PR; observation verifies it.
        world.gh.upstream = [upstream_pr()]
        out = world.tick(at(4))
        self.assertEqual(world.kinds(out), ["verify_upstream_pr"])
        self.assertEqual(world.state, "upstream_review.waiting_ci")
        self.assertEqual(world.record["upstream_pr"]["registered_by"],
                         {"id": OWNER["id"], "login": OWNER["login"]})
        self.assertIsNone(world.record["pending_human_action"])
        # Only the fork PR and the tracking issue were written; nothing upstream.
        writes = [c for c in world.gh.calls if c[0] != "GET"]
        self.assertEqual(writes, [("PATCH", f"{FORK_PATH}/pulls/1"),
                                  ("POST", f"/repos/{CONTROL_REPO}/issues/1/comments")])

    def test_ticks_without_receipts_change_nothing_but_relaunch_within_the_budget(self):
        world = submitted()
        op_id = world.record["active_operation_id"]
        out = world.tick(at(2))
        self.assertEqual((out.report["handoff_requests"], world.kinds(out)), ([], []))
        out = world.tick(at(6))
        self.assertEqual([r["attempt"] for r in out.report["handoff_requests"]], [2])
        world.tick(at(12))
        out = world.tick(at(18))
        self.assertEqual(out.report["handoff_requests"], [])
        self.assertIn({"contribution": CID, "operation": op_id,
                       "alert": "debug_cleanup launches spent without a receipt"},
                      out.report["controller"]["alerts"])
        self.assertEqual(world.state, "handoff.cleaning_debug")

    def test_a_refused_attempt_is_not_folded(self):
        world = submitted()
        receipt = dict(failed(world), result="refused", failure=None)
        out = world.tick(at(1), receipts=[receipt])
        self.assertEqual(world.kinds(out), [])
        self.assertEqual(world.active["status"], "planned")
        self.assertEqual(out.report["handoffs"][0]["outcome"], "not folded: refused")

    def test_a_receipt_of_another_operation_or_a_mismatched_readback_is_not_folded(self):
        world = submitted()
        other = dict(world.run_workflow(), operation_id="op-" + "0" * 24)
        self.assertEqual(world.kinds(world.tick(at(1), receipts=[other])), [])
        world.gh.branch = "d" * 40
        receipt = {"schema_version": 1, "operation_id": world.active["operation_id"],
                   "kind": "debug_cleanup", "contribution_id": CID, "run_id": "1",
                   "result": "applied", "clean_head": HEAD, "rewritten": False}
        out = world.tick(at(2), receipts=[receipt])
        self.assertEqual(world.kinds(out), [])
        self.assertEqual(out.report["handoffs"][0]["outcome"], "readback mismatch")
        self.assertEqual(world.state, "handoff.cleaning_debug")


def upstream_pr(**over):
    pr = {"id": 777, "number": 205, "state": "open", "merged_at": None,
          "html_url": "https://github.com/closeio/ciso8601/pull/205",
          "base": {"ref": "master", "repo": {"id": UPSTREAM_ID}},
          "head": {"ref": "riscv64", "sha": HEAD, "repo": {"id": FORK["repository_id"]}},
          "user": {"id": OWNER["id"], "login": OWNER["login"], "type": "User"}}
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(pr.get(key), dict):
            pr[key] = dict(pr[key], **value)
        else:
            pr[key] = value
    return pr


class TestInfrastructureRetries(unittest.TestCase):
    """Each handoff operation: a failed attempt is retried within its budget,
    the exhaustion names the budget in a human wait, and the resume starts
    that budget again."""

    CASES = (("handoff.cleaning_debug", "debug_cleanup", "debug_cleanup_failed",
              "retry_debug_cleanup", "exhaust_debug_cleanup_retries", "resume_debug_cleanup"),
             ("handoff.closing_internal_pr", "internal_pr_closure", "internal_pr_closure_failed",
              "retry_internal_pr_closure", "exhaust_internal_pr_retries",
              "resume_internal_pr_closure"),
             ("handoff.preparing_submission", "submission_comment", "submission_comment_failed",
              "retry_submission_comment", "exhaust_comment_retries",
              "resume_submission_preparation"))

    def test_retry_exhaustion_and_resume(self):
        limit = lifecycle.budget_limit("debug_cleanup", policy())
        self.assertEqual(limit, 3)
        for state, kind, pending, retry, exhaust, resume in self.CASES:
            with self.subTest(kind=kind):
                world = through(state)
                minute = 20
                for attempt in range(1, limit + 1):
                    op = world.active
                    self.assertEqual(op["reason_ids"][1:], [f"attempt:{attempt}", f"count:{attempt}"])
                    out = world.tick(at(minute), receipts=[failed(world)])
                    minute += 1
                    self.assertEqual(world.operations[op["operation_id"]]["status"], "failed")
                    if attempt < limit:
                        self.assertEqual(world.kinds(out), [retry])
                        self.assertEqual(out.staged and world.state, state)
                        self.assertEqual(world.events[-1]["details"]["charged"], [kind])
                        # The next attempt is planned and launched in the same tick.
                        self.assertNotEqual(world.record["active_operation_id"], op["operation_id"])
                        self.assertEqual(len(out.report["handoff_requests"]), 1)
                    else:
                        self.assertEqual(world.kinds(out), [exhaust])
                self.assertEqual(world.state, "handoff.waiting_human")
                self.assertEqual((world.record["pending_human_action"]["kind"],
                                  world.record["pending_human_action"]["counter"]), (pending, kind))
                self.assertIsNone(world.record["active_operation_id"])
                self.assertEqual(out.report["handoff_requests"], [])
                # Nothing is launched while the human wait lasts.
                self.assertEqual(world.tick(at(minute + 10)).report["handoff_requests"], [])
                revision = world.record["revision"]
                out = world.tick(at(minute + 11), comments=[comment(
                    f'/upstreaming resume revision={revision} reason="fixed"', cid=950)])
                self.assertEqual(world.kinds(out), [resume])
                self.assertEqual(world.events[-1]["details"]["restarted"],
                                 {"counter": kind, "count": limit})
                self.assertEqual((world.state, world.record["revision"]), (state, revision + 1))
                op = world.active
                self.assertEqual(op["reason_ids"][1:], [f"attempt:{limit + 1}", "count:1"])
                self.assertEqual(len(out.report["handoff_requests"]), 1)
                # The resumed operation completes the path.
                out = world.tick(at(minute + 12), receipts=[world.run_workflow()])
                self.assertNotEqual(world.state, state)

    def test_a_cleanup_that_cannot_be_admitted_returns_to_development(self):
        world = submitted()
        out = world.tick(at(1), receipts=[failed(world, handoff.INVALID)])
        self.assertEqual(world.kinds(out), ["return_for_repair"])
        self.assertEqual(world.state, "development.fix_needed")
        self.assertEqual(world.record["revision"], 7)
        for name in ("clean_head_sha", "handoff_request", "pending_human_action",
                     "active_operation_id"):
            self.assertIsNone(world.record[name], name)
        # The live branch moved before the cleanup: the lease conflicts.
        world = submitted()
        world.gh.branch = "d" * 40
        receipt = world.run_workflow()
        self.assertEqual((receipt["result"], receipt["failure"]), ("failed", handoff.INVALID))
        world.gh.branch = HEAD
        self.assertEqual(world.kinds(world.tick(at(1), receipts=[receipt])), ["return_for_repair"])

    def test_an_invalid_failure_is_never_charged_to_the_budget(self):
        world = submitted()
        for minute in (1, 2):
            world.tick(at(minute), receipts=[failed(world)])
        self.assertEqual(world.active["reason_ids"][2], "count:3")
        out = world.tick(at(3), receipts=[failed(world, handoff.INVALID)])
        self.assertEqual(world.kinds(out), ["return_for_repair"])


class TestCleanHeadCi(unittest.TestCase):
    def test_a_failure_or_a_moved_head_returns_for_repair(self):
        world = submitted()
        world.gh.ci_state = "failure"
        out = world.tick(at(1), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head", "return_for_repair"])
        self.assertEqual(world.state, "development.fix_needed")
        world = submitted()
        receipt = world.run_workflow()
        # No CI run is observed for the clean head yet: the wait is kept.
        with mock.patch.object(FakeGitHub, "ci_snapshot", lambda self, a, f, h: collect.CiSnapshot(
                repository={"id": 1, "full_name": f}, head_sha=h, runs=[], jobs_by_run={},
                artifacts_by_run={})):
            out = world.tick(at(1), receipts=[receipt])
        self.assertEqual(world.kinds(out), ["confirm_clean_head"])
        self.assertEqual(world.state, "handoff.waiting_ci")
        world.gh.branch = "e" * 40
        out = world.tick(at(2))
        self.assertEqual(world.kinds(out), ["return_for_repair"])
        self.assertEqual(world.events[-1]["details"]["head"], "e" * 40)

    def test_a_cancelled_job_keeps_the_wait_and_reruns_nothing(self):
        world = submitted()
        world.gh.ci_state = "cancelled"
        out = world.tick(at(1), receipts=[world.run_workflow()])
        self.assertEqual(world.kinds(out), ["confirm_clean_head"])
        self.assertEqual(world.state, "handoff.waiting_ci")
        self.assertEqual(world.tick(at(2)).report["handoff_requests"], [])


class TestLockedWait(unittest.TestCase):
    def test_the_table_gives_it_exactly_three_exits(self):
        exits = sorted(e.verb for e in lifecycle.state_edges("handoff.waiting_upstream_pr")
                       if e.target != "handoff.waiting_upstream_pr")
        self.assertEqual(exits, ["refresh_investigation", "retract_submission", "verify_upstream_pr"])
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
            f"/upstreaming retract-pr revision={revision} head={'c' * 40}": "not the clean head",
            f'/upstreaming refresh revision={revision} reason="x"': "not the clean head",
        }
        for n, (body, why) in enumerate(refused.items()):
            with self.subTest(body=body):
                out = world.tick(at(30 + n), comments=[comment(body, cid=960 + n)])
                receipt = next(r for k, r in out.staged if k == "commands")
                self.assertEqual(receipt["result"], "rejected")
                self.assertIn(why, receipt["reason"])
                self.assertEqual(world.record, before)
        # Observation of everything that does not match records audit events
        # only, once each, and never moves the record or launches anything.
        world.gh.branch = "f" * 40
        world.gh.upstream = [upstream_pr(user={"id": STRANGER["id"], "login": "stranger"}),
                             upstream_pr(number=206, base={"ref": "main"})]
        out = world.tick(at(40))
        self.assertEqual(world.kinds(out), ["record_audit_observation"])
        self.assertEqual(world.record, before)
        self.assertEqual(world.kinds(world.tick(at(41))), [])
        for key in ("handoff_requests", "requested_effects", "dev_pr_requests"):
            self.assertEqual(world.reports[-1][key], [], key)
        # A Result that arrives now is late.
        self.assertIsNone(engine.refusal(world.record, "record_late_effect", policy=world.policy,
                                         now=T0, facts={"result_mismatch"}))

    def test_retract_and_refresh_with_the_exact_head(self):
        world = through("handoff.waiting_upstream_pr")
        out = world.tick(at(30), comments=[comment(
            f"/upstreaming retract-pr revision=6 head={HEAD}", cid=970)])
        self.assertEqual(world.kinds(out), ["retract_submission"])
        self.assertEqual((world.state, world.record["revision"]), ("development.fix_needed", 7))
        for name in ("pending_human_action", "handoff_request", "handoff_approval"):
            self.assertIsNone(world.record[name])
        # No reminder follows a retraction.
        self.assertNotIn("reminders", world.tick(at(0, "2026-12-01")).report["controller"])
        # The upstream resources are untouched by the retraction.
        self.assertEqual([c for c in world.gh.calls if c[0] not in ("GET",)][-1][0], "POST")

        world = through("handoff.waiting_upstream_pr")
        out = world.tick(at(30), comments=[comment(
            f'/upstreaming refresh revision=6 head={HEAD} reason="new base"', cid=971)])
        self.assertEqual(world.kinds(out), ["refresh_investigation"])
        self.assertEqual(world.state, "queued.idle")
        self.assertEqual(world.record["refresh"]["head_sha"], HEAD)
        self.assertEqual(world.record["fork"], FORK)

    def test_pause_keeps_the_wait_and_suppresses_reminders(self):
        world = through("handoff.waiting_upstream_pr")
        out = world.tick(at(30), comments=[comment('/upstreaming pause reason="away"', cid=972)])
        self.assertEqual((world.kinds(out), world.state), (["pause"], "handoff.waiting_upstream_pr"))
        self.assertNotIn("reminders", world.tick(at(0, "2026-12-03")).report["controller"])
        world.gh.upstream = [upstream_pr()]
        # Observation still verifies the PR: the mask does not cover observation.
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
        self.assertEqual(event["details"]["bound_to"], {"revision": 6, "head": HEAD})
        text = status_ledger.render(event, world.record, "https://run")
        self.assertIn(f"Reminder @{OWNER['login']}", text)
        self.assertIn(f"]({package_url})", text)
        self.assertIn(f"`{HEAD}`", text)
        self.assertIn("still resolves to that head", text)
        self.assertIn(f"/upstreaming retract-pr revision=6 head={HEAD}", text)
        # A mismatch is stated, with the retraction offered instead.
        world.gh.branch = "f" * 40
        world.tick(at(0, "2026-12-02"))
        out = world.tick(at(10, "2026-12-01"))
        reminder = [e for e in world.events if e["kind"] == "record_reminder"][-1]
        self.assertIn("resolves to", reminder["details"]["mismatch"] or "")
        text = status_ledger.render(reminder, world.record, "https://run")
        self.assertIn("Do not submit from the package", text)
        self.assertNotIn(chr(0x2014), text)


class TestPullRequestDiscovery(unittest.TestCase):
    def setUp(self):
        self.record = through("handoff.waiting_upstream_pr").record
        self.authority = policy()["command_authority"]["actor_ids"]

    def test_each_filter(self):
        cases = {
            "repository": upstream_pr(base={"repo": {"id": 1}}),
            "base": upstream_pr(base={"ref": "main"}),
            "fork": upstream_pr(head={"repo": {"id": 2}}),
            "ref": upstream_pr(head={"ref": "main"}),
            "head": upstream_pr(head={"sha": "c" * 40}),
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
            "head_repository_id": FORK["repository_id"], "head_ref": "riscv64", "head_sha": HEAD,
            "base_ref": "master", "registered_by": {"id": OWNER["id"], "login": OWNER["login"]}})

    def test_ambiguous_candidates_verify_nothing(self):
        world = through("handoff.waiting_upstream_pr")
        world.gh.upstream = [upstream_pr(), upstream_pr(id=778, number=206)]
        out = world.tick(at(30))
        self.assertEqual(world.kinds(out), ["record_audit_observation"])
        self.assertEqual(world.state, "handoff.waiting_upstream_pr")
        self.assertIn("2 pull requests match", world.events[-1]["details"]["mismatch"])


class TestPackage(unittest.TestCase):
    def inputs(self, **over):
        record = through("handoff.preparing_submission").record
        compare = json.loads((FIXTURES / "fork-compare.json").read_text())
        args = {"dossier": load("dossier.json"), "ci": load("evidence-ci.json"),
                "commits": [{"sha": c["sha"], "message": c["commit"]["message"]}
                            for c in compare["commits"]],
                "files": [f["filename"] for f in compare["files"]]}
        args.update(over)
        return record, args

    def test_deterministic_and_complete(self):
        record, args = self.inputs()
        one, two = handoff.build_package(record, **args), handoff.build_package(copy.deepcopy(record), **args)
        self.assertEqual(one, two)
        self.assertEqual(one["title"], "Build riscv64 wheels on Linux")
        self.assertTrue(one["body"].startswith("Add a native riscv64 row"))
        self.assertIn("## Testing", one["body"])
        self.assertIn("19 jobs executed and passed", one["body"])
        self.assertIn("`Build - ubuntu-24.04-riscv (riscv64)`", one["body"])
        self.assertIn("Related: #181", one["body"])
        comment = one["comment"]
        for part in ("`closeio/ciso8601`, base `master`", "`riseproject-upstreaming/ciso8601`, branch `riscv64`",
                     f"Exact head: `{HEAD}`", "> [!WARNING]", one["title"], one["body"],
                     f"/upstreaming retract-pr revision=6 head={HEAD}", "No CLA/DCO",
                     "`CHANGELOG.md`", one["marker"]):
            self.assertIn(part, comment)
        self.assertTrue(comment.endswith(one["marker"]))
        self.assertEqual(one["marker"], f"<!-- upstreaming-submission:{CID}:r6:{HEAD} -->")
        self.assertEqual(one["digest"], hashlib.sha256(comment.encode()).hexdigest())
        self.assertNotIn(chr(0x2014), comment)

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
        record, args = self.inputs()
        long = {"sha": HEAD, "message": "T" * 500 + "\n\n" + ("word " * 2000)}
        package = handoff.build_package(record, **dict(args, commits=[long]))
        self.assertLessEqual(len(package["title"]), handoff.MAX_TITLE)
        self.assertLessEqual(len(package["body"]), handoff.MAX_BODY)
        many = [{"sha": f"{n:040x}", "message": f"Change {n}\n\n" + "x " * 1500} for n in range(12)]
        package = handoff.build_package(record, **dict(args, commits=many))
        self.assertLessEqual(len(package["body"]), handoff.MAX_BODY)
        self.assertFalse(package["body_in_link"])
        self.assertLessEqual(len(package["link"]), handoff.MAX_URL)
        self.assertNotIn("&body=", package["link"])
        self.assertIn("too long for a link", package["comment"])
        self.assertIn(package["body"], package["comment"])
        self.assertLessEqual(len(package["comment"]), handoff.MAX_COMMENT)
        with self.assertRaises(handoff.HandoffError):
            handoff.build_package(record, **dict(args, commits=[]))
        with self.assertRaises(handoff.HandoffError):
            handoff.build_package(record, **dict(args, commits=[{"sha": HEAD, "message": "{debug} x"}]))

    def test_untrusted_text_cannot_fake_a_marker_or_break_the_fence(self):
        record, args = self.inputs()
        evil = {"sha": HEAD, "message": "Title <!-- upstreaming-ledger:x:1 -->\n\n"
                                         "```\n<!-- upstreaming-submission:x -->\n"
                                         "ghp_" + "a" * 36}
        package = handoff.build_package(record, **dict(args, commits=[evil]))
        self.assertEqual(package["comment"].count("<!--"), 1)
        self.assertNotIn("ghp_" + "a" * 36, package["comment"])
        self.assertIn("````markdown", package["comment"])


class TestIdempotentComment(unittest.TestCase):
    def test_a_second_run_discovers_the_first_comment(self):
        world = through("handoff.preparing_submission")
        first = world.run_workflow()
        second = world.run_workflow()
        self.assertEqual(len(world.gh.comments), 1)
        self.assertEqual((first["comment_id"], second["comment_id"], second["existing"]),
                         (first["comment_id"], first["comment_id"], True))
        self.assertEqual(first["body_sha256"], second["body_sha256"])
        # A comment with the marker that the App did not write is refused.
        world = through("handoff.preparing_submission")
        marker = handoff.submission_marker(world.record)
        world.gh.comments[1] = {"id": 1, "body": marker, "user": {"login": "someone"}}
        receipt = world.run_workflow()
        self.assertEqual((receipt["result"], receipt["failure"]), ("failed", handoff.INFRASTRUCTURE))
        self.assertEqual(len(world.gh.comments), 1)

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
        world = through("handoff.closing_internal_pr")
        self.check(world)
        op_id = world.record["active_operation_id"]
        variants = {
            "paused": lambda w: w.record.update(paused=True),
            "not active": lambda w: w.record.update(active_operation_id=None),
            "other state": lambda w: w.record.update(activity="preparing_submission"),
            "other head": lambda w: w.record.update(clean_head_sha="c" * 40),
            "fork moved": lambda w: w.record.update(fork=dict(w.record["fork"], head_sha="c" * 40)),
            "other PR": lambda w: w.record.update(fork=dict(w.record["fork"], pr_number=2)),
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
        # A refused closure writes nothing.
        w = copy.deepcopy(world)
        w.record["paused"] = True
        receipt = w.run_workflow()
        self.assertEqual(receipt["result"], "refused")
        self.assertEqual(w.gh.dev_pr["state"], "open")
        self.assertNotIn("PATCH", [c[0] for c in w.gh.calls])

    def test_the_closure_touches_only_the_exact_marked_pr(self):
        for name, change in {"unmarked": {"body": "x"}, "other head": {"head": {"sha": "c" * 40, "ref": "riscv64", "repo": {"id": FORK["repository_id"]}}},
                             "merged": {"merged": True}}.items():
            with self.subTest(pr=name):
                world = through("handoff.closing_internal_pr")
                world.gh.dev_pr.update(change)
                receipt = world.run_workflow()
                self.assertEqual(receipt["result"], "failed")
                self.assertNotIn("PATCH", [c[0] for c in world.gh.calls])

    def test_a_closed_pr_is_read_back_without_a_write(self):
        world = through("handoff.closing_internal_pr")
        world.gh.dev_pr["state"] = "closed"
        receipt = world.run_workflow()
        self.assertEqual(receipt["result"], "applied")
        self.assertNotIn("PATCH", [c[0] for c in world.gh.calls])


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
                     "verify_upstream_pr", "record_audit_observation"):
            self.assertIn(verb, engine.PIPELINE_VERBS)
        self.assertEqual({"rerun_interrupted_ci", "exhaust_interrupted_reruns"}
                         & set(engine.PIPELINE_VERBS), set())


if __name__ == "__main__":
    unittest.main()


class TestCleanupWithGit(unittest.TestCase):
    """The debug cleanup against a local bare fork: the exact series without
    its debug commits, published under the exact-head lease."""

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
        self.root = root
        self.contribution = contribution

    def tearDown(self):
        import fork_publish
        fork_publish.URL_TEMPLATE, extra = self.saved
        fork_publish.TEST_EXTRA_CONFIG[:] = extra
        self.tmp.cleanup()

    def series(self, *commits):
        for message, files in commits:
            self.commit_files(self.work, files, msg=message)
        head = self.git(self.work, "rev-parse", "HEAD")
        self.git(self.work, "push", "-q", "-f", str(self.bare), "HEAD:refs/heads/riscv64")
        return head

    def branch(self):
        return self.git(self.root, "--git-dir", str(self.bare), "rev-parse", "refs/heads/riscv64")

    def run_cleanup(self, head, *, observed=None, push=None):
        record = self.contribution(phase="handoff", activity="cleaning_debug", revision=4,
                                   scope_paths=["setup.py", "ci.yml"],
                                   upstream={"repository_id": 1, "full_name": "example/proj",
                                             "base_ref": "main", "base_sha": self.base})
        record["fork"]["head_sha"] = head
        record["handoff_approval"] = {"command_id": "c-1", "actor": dict(OWNER), "head": head,
                                      "revision": 4, "evidence_ids": []}
        op = handoff.plan_attempt(record, {}, policy=policy(), now=T0)
        record["active_operation_id"] = op["operation_id"]
        state = Path(self.tmp.name) / "state"
        for rel, value in ((f".state/contributions/{contribution_file_id(record['contribution_id'])}.json",
                            record), (f".state/operations/{op['operation_id']}.json", op)):
            (state / rel).parent.mkdir(parents=True, exist_ok=True)
            (state / rel).write_text(canonical_json(value))
        reads = []

        def branch_head(_api, full_name, ref):
            reads.append(full_name)
            return {"head": observed.pop(0) if observed else self.branch()}

        with mock.patch("collect.branch_head", branch_head):
            return handoff.cleanup(state, op["operation_id"], policy=policy(), read_api=object(),
                                   token="t", push=push)

    def test_no_debug_commit_is_a_readback_of_the_same_head(self):
        head = self.series(("Build riscv64", {"setup.py": "b\n"}))
        pushes = []
        result = self.run_cleanup(head, push=lambda *a: pushes.append(a))
        self.assertEqual(result, {"result": "applied", "clean_head": head, "rewritten": False,
                                  "removed_commits": []})
        self.assertEqual((pushes, self.branch()), ([], head))

    def test_debug_commits_are_removed_under_the_lease(self):
        head = self.series(("Build riscv64", {"setup.py": "b\n"}),
                           ("{debug} trace", {"ci.yml": "jobs: [debug]\n"}),
                           ("Test riscv64", {"setup.py": "c\n"}))
        result = self.run_cleanup(head)
        clean = result["clean_head"]
        self.assertTrue(result["rewritten"])
        self.assertEqual(len(result["removed_commits"]), 1)
        self.assertEqual(self.branch(), clean)
        log = self.git(self.root, "--git-dir", str(self.bare), "log", "--format=%s|%an",
                       f"{self.base}..{clean}").splitlines()
        self.assertEqual(log, ["Test riscv64|riseproject-upstreaming[bot]",
                               "Build riscv64|riseproject-upstreaming[bot]"])
        self.assertEqual(self.git(self.root, "--git-dir", str(self.bare), "show",
                                  f"{clean}:ci.yml"), "jobs: []")
        # The same series always gives the same clean head: a second run
        # finds it written and reads it back without a write.
        pushes = []
        again = self.run_cleanup(head, observed=[clean, clean], push=lambda *a: pushes.append(a))
        self.assertEqual((again["clean_head"], pushes), (clean, []))

    def test_a_conflict_without_the_debug_commit_is_invalid(self):
        head = self.series(("{debug} trace", {"setup.py": "debug\n"}),
                           ("Build riscv64", {"setup.py": "debug\nb\n"}))
        with self.assertRaises(handoff.HandoffInvalid):
            self.run_cleanup(head)
        self.assertEqual(self.branch(), head)

    def test_a_moved_branch_is_a_lease_conflict(self):
        head = self.series(("Build riscv64", {"setup.py": "b\n"}),
                           ("{debug} trace", {"ci.yml": "x\n"}))
        with self.assertRaises(handoff.HandoffInvalid):
            self.run_cleanup(head, observed=["d" * 40])
        # The branch moves between the final check and the push: the lease
        # refuses the write.
        moved = self.series(("Other", {"setup.py": "z\n"}))
        with self.assertRaises(handoff.HandoffInvalid):
            self.run_cleanup(head, observed=[head, head])
        self.assertEqual(self.branch(), moved)

    def test_only_debug_commits_leave_nothing_to_submit(self):
        head = self.series(("{debug} only", {"ci.yml": "x\n"}))
        with self.assertRaises(handoff.HandoffInvalid):
            self.run_cleanup(head)


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
        api.get.return_value = {"message": "x"}
        with self.assertRaises(collect.CollectError):
            collect.upstream_pull_requests(api, record)
