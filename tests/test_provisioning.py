# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Administrative fork provisioning is workflow-only, scoped and recoverable."""
import ast
import base64
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

import capacity
import collect
import controller
import provisioning
import reconcile
import recovery
from common import CONTROL_REPO, contribution_file_id
from tests.helpers import NOW, SHA_A, SHA_B, SHA_C, contribution, policy, steering_list

FORK = "riseproject-upstreaming/proj"
UP = "example/proj"


def request():
    rec = contribution(phase="provisioning", activity="idle", plan_evidence_id="plan-1",
                       scope_paths=[".github/workflows/*"], fork=None)
    op = recovery.plan(contribution_id=rec["contribution_id"], revision=rec["revision"], kind="fork_provision",
                       policy_version="test", reason_ids=["plan-1"],
                       target={"repository": FORK, "repository_id": None, "ref": "riscv64-dev", "base_sha": SHA_C, "expected_head": None},
                       proposed={"commit": SHA_C, "marker": "provision"}, now=NOW)
    rec["active_operation_id"] = op["operation_id"]
    return rec, op


class Api:
    def __init__(self, exists=True, head=None):
        self.exists, self.head = exists, head
        self.writes = []
        self.parent = 1
        self.defaults = {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
        self.actions = {"enabled": True, "allowed_actions": "all"}
        self.workflows_registered = False
        self.deny_settings = False

    def fork(self):
        return {"id": 2, "full_name": FORK, "fork": True, "archived": False, "default_branch": "main",
                "parent": {"id": self.parent, "full_name": UP}}

    def get(self, path):
        if path == f"/repos/{UP}":
            return {"id": 1, "full_name": UP, "default_branch": "main"}
        if path == f"/repos/{UP}/git/commits/{SHA_C}":
            return {"sha": SHA_C}
        if path == f"/repos/{FORK}":
            if not self.exists:
                raise collect.CollectError("HTTP 404")
            return self.fork()
        if path == f"/repos/{FORK}/branches/riscv64-dev":
            if self.head is None:
                raise collect.CollectError("HTTP 404")
            return {"name": "riscv64-dev", "commit": {"sha": self.head, "commit": {"tree": {"sha": "d" * 40}}, "parents": []}}
        if path == f"/repos/{FORK}/actions/permissions/workflow":
            return self.defaults
        if path == f"/repos/{FORK}/actions/permissions":
            return self.actions
        if path == f"/repos/{FORK}/actions/workflows?per_page=100":
            workflows = ([{"path": ".github/workflows/wheels.yml", "state": "disabled_manually"},
                          {"path": ".github/workflows/publish.yml", "state": "active"}]
                         if self.workflows_registered else [])
            return {"total_count": len(workflows), "workflows": workflows}
        if path == f"/repos/{FORK}/contents/.github/workflows?ref=main":
            return [{"type": "file", "name": "wheels.yml", "path": ".github/workflows/wheels.yml"},
                    {"type": "file", "name": "publish.yml", "path": ".github/workflows/publish.yml"}]
        if path.startswith(f"/repos/{FORK}/contents/"):
            text = "name: Wheel tests\npermissions: {contents: read}\njobs: {}\n"
            if "publish.yml" in path:
                text = "permissions: {contents: write}\njobs: {}\n"
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        raise AssertionError(path)

    def paginate(self, path, **_kwargs):
        # The steering lists read for the Work the controller allocates.
        found = steering_list(path)
        if found is None:
            raise AssertionError(path)
        return found

    def write(self, method, path, body):
        self.writes.append((method, path, copy.deepcopy(body)))
        if self.deny_settings and "/actions/permissions" in path:
            raise provisioning.ProvisionError("HTTP 403")
        if path == f"/repos/{UP}/forks":
            self.exists = True
            return self.fork()
        if path == f"/repos/{FORK}/git/refs":
            self.head = body["sha"]
        if path == f"/repos/{FORK}/actions/permissions/workflow":
            self.defaults = dict(body)
        if path == f"/repos/{FORK}/actions/permissions":
            was_enabled = self.actions.get("enabled")
            self.actions = dict(body)
            if was_enabled is False and body.get("enabled") is True:
                self.workflows_registered = True
        return {}


class TestProvisioning(unittest.TestCase):
    def test_credentialless_fork_allows_oidc_publishers_but_not_repo_writes(self):
        workflow = {"permissions": {"contents": "read"}, "jobs": {"build": {"steps": []},
                    "publish": {"permissions": {"id-token": "write"},
                                "steps": [{"uses": "pypa/gh-action-pypi-publish@v1"},
                                          {"run": "twine upload dist/*"}]}}}
        self.assertFalse(provisioning._workflow_unsafe(workflow, "jobs: {}"))
        workflow["jobs"]["publish"]["permissions"] = {"contents": "write"}
        self.assertTrue(provisioning._workflow_unsafe(workflow, "jobs: {}"))

    def test_create_and_exact_base_without_project_execution(self):
        rec, op = request()
        api = Api(exists=False)
        plan = provisioning.preflight(api, rec, op, policy(read_only=False))
        self.assertTrue(plan["create"])
        plan = provisioning.create_or_reuse(api, api, plan)
        self.assertTrue(plan["created"])
        outcome = provisioning.initialize(api, plan)
        self.assertEqual((outcome["status"], outcome["head_sha"], outcome["actions_read_only"]), ("provisioned", SHA_C, True))
        self.assertIn(("POST", f"/repos/{UP}/forks", {"organization": "riseproject-upstreaming", "name": "proj", "default_branch_only": True}), api.writes)
        self.assertIn(("POST", f"/repos/{FORK}/git/refs", {"ref": "refs/heads/riscv64-dev", "sha": SHA_C}), api.writes)
        self.assertIn(".github/workflows/publish.yml", outcome["disabled_workflows"])
        self.assertIn(".github/workflows/wheels.yml", outcome["enabled_workflows"])
        self.assertFalse(any(path == f"/repos/{FORK}/actions/permissions/workflow"
                             for _, path, _ in api.writes))
        toggles = [body["enabled"] for method, path, body in api.writes
                   if path == f"/repos/{FORK}/actions/permissions"]
        self.assertEqual(toggles, [False, True])

    def test_existing_fork_is_adopted_not_claimed_as_created(self):
        rec, op = request()
        api = Api(head=SHA_C)
        plan = provisioning.preflight(api, rec, op, policy(read_only=False))
        self.assertFalse(plan["create"])
        plan = provisioning.create_or_reuse(api, None, plan)
        self.assertFalse(plan["created"])
        provisioning.initialize(api, plan)
        self.assertFalse(any(p.endswith("/forks") or p.endswith("/git/refs") for _, p, _ in api.writes))

    def test_collision_and_unexpected_work_rejected_before_write(self):
        rec, op = request()
        api = Api(head=SHA_B)
        plan = provisioning.preflight(api, rec, op, policy(read_only=False))
        with self.assertRaises(provisioning.ProvisionError):
            provisioning.initialize(api, plan)
        self.assertEqual(api.writes, [])
        api.parent = 99
        with self.assertRaises(provisioning.ProvisionError):
            provisioning.preflight(api, rec, op, policy(read_only=False))

    def test_permissions_or_missing_scope_never_use_a_fallback(self):
        rec, op = request()
        api = Api()
        plan = provisioning.preflight(api, rec, op, policy(read_only=False))
        api.defaults["default_workflow_permissions"] = "write"
        api.deny_settings = True
        with self.assertRaises(provisioning.ProvisionError):
            provisioning.initialize(api, plan)
        self.assertIsNone(api.head)
        for changed in (dict(rec, paused=True), dict(rec, phase="planning"), dict(rec, plan_evidence_id=None)):
            with self.assertRaises(provisioning.ProvisionError):
                provisioning.preflight(api, changed, op, policy(read_only=False))
        with self.assertRaises(provisioning.ProvisionError):
            provisioning.preflight(api, rec, op, policy(read_only=True))

    def test_controller_records_intent_once_and_no_model_for_provisioning(self):
        rec, _ = request()
        rec["active_operation_id"] = None
        rec["owners"] = {"primary": None, "backup": None, "submitter": None, "handoff_reviewer": None}
        args = dict(operations={}, consumed={}, projects={}, policy=policy(read_only=False),
                    inputs=controller.Inputs(now=datetime.fromisoformat(NOW), pressure=capacity.Pressure(ok=True)))
        tick = controller.tick(contributions={"c": rec}, **args)
        self.assertEqual(tick.requested_effects, [])
        self.assertEqual(len(tick.report["provision_requests"]), 1)
        op = next(r for k, r in tick.staged if k == "operations")
        current = [r for k, r in tick.staged if k == "contributions"][-1]
        self.assertEqual(current["active_operation_id"], op["operation_id"])
        args["operations"] = {op["operation_id"]: op}
        again = controller.tick(contributions={"c": current}, **args)
        self.assertEqual(again.report["provision_requests"], [])

    def test_verified_setup_advances_and_admits_initial_patch_without_base_ci(self):
        rec, op = request()
        api = Api(exists=False)
        plan = provisioning.create_or_reuse(api, api, provisioning.preflight(api, rec, op, policy(read_only=False)))
        receipt = provisioning.initialize(api, plan)
        snap = reconcile.Snapshot({"c": rec}, {op["operation_id"]: op}, {}, {}, {})
        result = reconcile.tick(api, snap, policy=policy(read_only=False), now=datetime.fromisoformat(NOW), publications=[],
                                pressure=capacity.Pressure(ok=True), provisions=[receipt])
        self.assertEqual(result.report["provisions"][0]["status"], "provisioned")
        self.assertEqual(result.report["requested_effects"][0]["task"], "development_patch")
        self.assertTrue(any(k == "operations" and r["status"] == "confirmed" for k, r in result.staged))

    def test_source_snapshots_bind_only_unspecified_fields(self):
        rec = contribution(phase="planning", activity="idle")
        rec["upstream"]["repository_id"] = None
        rec["upstream"]["base_ref"] = None
        rec["upstream"]["base_sha"] = None
        source = {"repository_id": 1, "base_ref": "main", "base_sha": SHA_C}
        args = dict(contributions={"c": rec}, operations={}, consumed={}, projects={}, policy=policy(read_only=False),
                    inputs=controller.Inputs(now=datetime.fromisoformat(NOW), pressure=capacity.Pressure(ok=True), upstream_snapshots={UP: source}))
        tick = controller.tick(**args)
        bound = [r for k, r in tick.staged if k == "contributions"][-1]
        self.assertEqual(bound["upstream"], {"full_name": UP, **source})
        source["base_sha"] = SHA_A
        args["contributions"] = {"c": dict(bound, active_work=None, activity="idle")}
        next_tick = controller.tick(**args)
        current = [r for k, r in next_tick.staged if k == "contributions"][-1]
        self.assertEqual(current["upstream"]["base_sha"], SHA_C)


class StateApi(Api):
    """The fake GitHub API with the trusted state the workflow reads at the
    head of `main`."""

    def __init__(self, rec, op, policy_, **kwargs):
        super().__init__(**kwargs)
        cid = contribution_file_id(rec["contribution_id"])
        self.state = {f".state/operations/{op['operation_id']}.json": op,
                      f".state/contributions/{cid}.json": rec, "policy.json": policy_}
        self.fail_reads = False

    def get(self, path):
        prefix = f"/repos/{CONTROL_REPO}/contents/"
        if path.startswith(prefix):
            data = json.dumps(self.state[path[len(prefix):].removesuffix("?ref=main")]).encode()
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(data).decode()}
        if self.fail_reads:
            raise collect.CollectError("HTTP 502")
        return super().get(path)


class HttpError(urllib.error.HTTPError):
    def __init__(self, code, headers=None):
        super().__init__("https://api.github.com/x", code, "x", headers or {}, None)


class TestFailureReceipts(unittest.TestCase):
    META = {"operation_id": "op-" + "a" * 24, "contribution_id": "example--proj/12",
            "revision": 3, "run_id": "77"}

    def test_each_class_of_failure(self):
        receipt = provisioning.failure_receipt(
            self.META, "initialize",
            provisioning.ProvisionError("x", failure="branch_conflict"))
        self.assertEqual(receipt, self.META | {"status": "failed", "stage": "initialize",
                                               "failure": "branch_conflict", "reason": "x"})
        refused = provisioning.failure_receipt(self.META, "preflight",
                                               provisioning.ProvisionRefused("paused"))
        self.assertEqual((refused["status"], refused["failure"]), ("refused", None))
        for error in (provisioning.ProvisionUnconfirmed("HTTP 502"),
                      collect.CollectError("HTTP 502"), OSError("lost")):
            with self.subTest(error=error):
                self.assertIsNone(provisioning.failure_receipt(self.META, "create", error))
        # An unknown code is reported as `other`; the reason is bounded.
        receipt = provisioning.failure_receipt(
            self.META, "create", provisioning.ProvisionError("y" * 900, failure="nope"))
        self.assertEqual(receipt["failure"], "other")
        self.assertLessEqual(len(receipt["reason"]), 300)

    def test_only_a_definite_refusal_of_a_write_is_definite(self):
        cases = {403: "write_refused", 404: "write_refused", 422: "write_refused",
                 500: None, 502: None, 429: None}
        for code, failure in cases.items():
            for headers in ({}, {"retry-after": "60"}, {"x-ratelimit-remaining": "0"}):
                if headers and code != 403:
                    continue
                api = provisioning.WriteApi("t")

                def opener(req, timeout=None, code=code, headers=headers):
                    raise HttpError(code, headers)
                api._open = opener
                with self.subTest(code=code, headers=headers), \
                        self.assertRaises(provisioning.ProvisionError) as raised:
                    api.write("PUT", f"/repos/{FORK}/actions/permissions", {})
                limited = bool(headers)
                if failure and not limited:
                    self.assertNotIsInstance(raised.exception, provisioning.ProvisionUnconfirmed)
                    self.assertEqual(raised.exception.failure, failure)
                else:
                    self.assertIsInstance(raised.exception, provisioning.ProvisionUnconfirmed)

    def test_the_final_check_refuses(self):
        rec, op = request()
        open_policy = policy(read_only=False)
        for changed, policy_, operation in (
                (dict(rec, paused=True), open_policy, op),
                (rec, policy(read_only=True), op),
                (rec, open_policy, dict(op, status="applying")),
                (dict(rec, active_operation_id=None), open_policy, op)):
            with self.subTest(), self.assertRaises(provisioning.ProvisionRefused):
                provisioning.validate_request(changed, operation, policy_)

    def test_every_definite_failure_names_a_closed_code(self):
        tree = ast.parse(Path(provisioning.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "ProvisionError":
                codes = [k.value.value for k in node.keywords if k.arg == "failure"]
                for code in codes:
                    self.assertIn(code, provisioning.FAILURES)

    def run_main(self, api, directory, mode, *extra):
        out = Path(directory) / ("fork.json" if mode != "initialize" else "outcome.json")
        argv = [mode, "--operation-id", api.op_id, "--out", str(out),
                "--receipt", str(Path(directory) / "outcome.json"), *extra]
        env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": CONTROL_REPO,
               "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "77",
               "UPSTREAMING_FORK_TOKEN": "t"}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(collect, "Api", lambda *a, **k: api), \
                mock.patch.object(provisioning, "WriteApi", lambda token: api):
            os.environ.pop("GITHUB_OUTPUT", None)
            return provisioning.main(argv)

    def world(self, rec=None, **kwargs):
        default, op = request()
        api = StateApi(rec or default, op, policy(read_only=False), **kwargs)
        api.op_id = op["operation_id"]
        return api, rec or default

    def test_main_reports_a_definite_failure(self):
        api, rec = self.world(head=SHA_B)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_main(api, directory, "create"), 0)
            self.assertFalse((Path(directory) / "outcome.json").exists())
            fork = str(Path(directory) / "fork.json")
            self.assertEqual(self.run_main(api, directory, "initialize", "--plan", fork), 1)
            receipt = json.loads((Path(directory) / "outcome.json").read_text())
        self.assertEqual({k: receipt[k] for k in ("status", "failure", "stage", "operation_id",
                                                  "contribution_id", "revision", "run_id")},
                         {"status": "failed", "failure": "branch_conflict", "stage": "initialize",
                          "operation_id": api.op_id, "contribution_id": rec["contribution_id"],
                          "revision": rec["revision"], "run_id": "77"})
        self.assertEqual(api.writes, [])

    def test_main_reports_a_refusal_and_nothing_for_a_failed_read(self):
        default, _ = request()
        api, _rec = self.world(rec=dict(default, paused=True))
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_main(api, directory, "preflight"), 1)
            receipt = json.loads((Path(directory) / "outcome.json").read_text())
        self.assertEqual((receipt["status"], receipt["failure"], receipt["stage"]),
                         ("refused", None, "preflight"))
        api, _rec = self.world()
        api.fail_reads = True
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(collect.CollectError):
                self.run_main(api, directory, "preflight")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_main_success_writes_the_provisioned_receipt(self):
        api, rec = self.world(head=SHA_C)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.run_main(api, directory, "create"), 0)
            fork = str(Path(directory) / "fork.json")
            self.assertEqual(self.run_main(api, directory, "initialize", "--plan", fork), 0)
            receipt = json.loads((Path(directory) / "outcome.json").read_text())
        self.assertEqual((receipt["status"], receipt["head_sha"], receipt["operation_id"]),
                         ("provisioned", SHA_C, api.op_id))

    def test_the_failure_receipt_main_writes_is_folded(self):
        api, rec = self.world(head=SHA_B)
        op = next(v for k, v in api.state.items() if k.startswith(".state/operations/"))
        with tempfile.TemporaryDirectory() as directory:
            self.run_main(api, directory, "create")
            self.run_main(api, directory, "initialize", "--plan", str(Path(directory) / "fork.json"))
            receipt = json.loads((Path(directory) / "outcome.json").read_text())
        receipt = dict(receipt, provision_run_id="77", provision_artifact_id=88)
        snap = reconcile.Snapshot({"c": rec}, {op["operation_id"]: op}, {}, {}, {})
        result = reconcile.tick(api, snap, policy=policy(read_only=False),
                                now=datetime.fromisoformat(NOW), publications=[],
                                pressure=capacity.Pressure(ok=True), provisions=[receipt],
                                observe=False)
        [failed] = [r for k, r in result.staged if k == "operations"]
        self.assertEqual((failed["status"], failed["external_ids"]["provision_failure"]),
                         ("failed", "branch_conflict"))
        [event] = [r for k, r in result.staged if k == "events"]
        self.assertEqual(event["kind"], "require_provisioning_intervention")
        self.assertEqual({k: event["details"][k] for k in ("receipt_status", "failure", "stage")},
                         {"receipt_status": "failed", "failure": "branch_conflict",
                          "stage": "initialize"})
        self.assertNotIn("never reset", json.dumps(event))
        waiting = [r for k, r in result.staged if k == "contributions"][-1]
        self.assertEqual(waiting["pending_human_action"]["kind"], controller.PROVISIONING_FAILED)
        self.assertNotIn("never reset", json.dumps(waiting))
