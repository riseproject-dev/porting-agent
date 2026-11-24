# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Same-fork development PR creation is exact and idempotent."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import capacity
import commands
import controller
import dev_pr
import engine
import lifecycle
import reconcile
import recovery
from tests.helpers import (
    NOW,
    SHA_A,
    SHA_B,
    SHA_C,
    apply_command,
    contribution,
    policy,
    refusal,
    take,
)

FORK = "riseproject-upstreaming/proj"
MARKER = f"<!-- upstreaming-development:example--proj/12:r2:{SHA_B} -->"


def records(root: Path):
    rec = contribution(phase="development", activity="waiting_ci", revision=2,
                       plan_evidence_id="plan-1", scope_paths=[".github/workflows/build.yml"])
    rec["fork"] = {"repository_id": 9, "name": FORK, "head_ref": "riscv64-dev", "head_sha": SHA_B, "pr_number": None}
    op = recovery.plan(contribution_id=rec["contribution_id"], revision=2, kind="dev_pr_create",
                       policy_version="test", reason_ids=["plan-1", "result-1"],
                       target={"repository": FORK, "repository_id": 9, "ref": "riscv64-dev",
                               "expected_head": SHA_B, "base_sha": SHA_C},
                       proposed={"marker": MARKER, "content_digest": "sha256:x"}, now=NOW)
    rec["active_operation_id"] = op["operation_id"]
    for kind, name, value in (("operations", op["operation_id"], op),
                              ("contributions", "example--proj--12", rec)):
        path = root / ".state" / kind / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value))
    return rec, op


class FakeApi:
    def __init__(self, rec, op, existing=False):
        self.rec, self.op, self.existing = rec, op, existing
        self.writes = []
        self.workflow_active = existing
        self.workflows_registered = existing
        self.actions_enabled = True
        self.pr = {"id": 77, "number": 4, "html_url": "https://github.com/riseproject-upstreaming/proj/pull/4",
                   "state": "open", "body": dev_pr.body(rec, op),
                   "head": {"sha": SHA_B, "ref": "riscv64-dev", "repo": {"id": 9}},
                   "base": {"ref": "main"}}

    def get(self, path):
        if path == f"/repos/{FORK}":
            return {"id": 9, "full_name": FORK, "default_branch": "main", "archived": False,
                    "fork": True, "parent": {"id": 1, "full_name": "example/proj"}}
        if path == f"/repos/{FORK}/branches/riscv64-dev":
            return {"name": "riscv64-dev", "commit": {"sha": SHA_B, "commit": {"tree": {"sha": "d" * 40}}, "parents": []}}
        if path == f"/repos/{FORK}/contents/.github/workflows?ref=riscv64-dev":
            return [{"type": "file", "name": "build.yml", "path": ".github/workflows/build.yml"}]
        if path == f"/repos/{FORK}/contents/.github/workflows/build.yml?ref=riscv64-dev":
            import base64
            text = "name: CI\npermissions: {contents: read}\njobs: {}\n"
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if path == f"/repos/{FORK}/actions/workflows?per_page=100":
            workflows = ([{"path": ".github/workflows/build.yml",
                           "state": "active" if self.workflow_active else "disabled_manually"}]
                         if self.workflows_registered else [])
            return {"total_count": len(workflows), "workflows": workflows}
        if path.startswith(f"/repos/{FORK}/pulls?"):
            return [self.pr] if self.existing else []
        if path == f"/repos/{FORK}/pulls/4":
            return self.pr
        raise AssertionError(path)

    def write(self, method, path, value):
        self.writes.append((method, path, value))
        if path.endswith("/actions/permissions"):
            was_enabled = self.actions_enabled
            self.actions_enabled = value["enabled"]
            if was_enabled is False and self.actions_enabled is True:
                self.workflows_registered = True
                self.workflow_active = True
        if path.endswith("/enable"):
            self.workflow_active = True
        return {}

    def mutate(self, method, path, value):
        self.writes.append((method, path, value))
        if method == "PATCH":
            self.pr["state"] = value["state"]
        return self.pr


class TestDevelopmentPR(unittest.TestCase):
    def test_controller_records_pr_intent_before_launch(self):
        rec = contribution(phase="development", activity="waiting_ci", revision=2,
                           plan_evidence_id="plan-1", scope_paths=[".github/workflows/build.yml"])
        rec["fork"] = {"repository_id": 9, "name": FORK, "head_ref": "riscv64-dev", "head_sha": SHA_B, "pr_number": None}
        rec["proposal_artifact"] = {"work_id": "w", "claim_id": "c", "artifact_id": 1, "name": "n",
                                    "run_id": "5", "outcome": "published",
                                    "result_commit": "q_result", "digest": "sha256:" + "0" * 64}
        branch = recovery.plan(contribution_id=rec["contribution_id"], revision=2, kind="branch_update",
                               policy_version="test", reason_ids=["result"],
                               target={"repository": FORK, "repository_id": 9, "ref": "riscv64-dev",
                                       "expected_head": SHA_A, "base_sha": SHA_A}, now=NOW)
        branch = recovery.fold_receipt(branch, external_ids=None, detail="head verified", now=NOW)
        rec["publication_operation_id"] = branch["operation_id"]
        tick = controller.tick(contributions={"c": rec}, operations={branch["operation_id"]: branch},
                               consumed={}, projects={}, policy=policy(read_only=False),
                               inputs=controller.Inputs(now=datetime.fromisoformat(NOW), pressure=capacity.Pressure(ok=True)))
        self.assertEqual(len(tick.report["dev_pr_requests"]), 1)
        op = next(value for kind, value in tick.staged if kind == "operations")
        self.assertEqual(op["kind"], "dev_pr_create")
        current = [value for kind, value in tick.staged if kind == "contributions"][-1]
        self.assertEqual(current["active_operation_id"], op["operation_id"])
        # The intent and its launch are one tick: the launch is the row.
        [event] = [value for kind, value in tick.staged if kind == "events"]
        self.assertEqual(event["kind"], "launch_development_pr")

    def test_verified_receipt_records_pr_and_confirms_operation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rec, op = records(root); api = FakeApi(rec, op, existing=True)
            snap = reconcile.Snapshot({"c": rec}, {op["operation_id"]: op}, {}, {}, {})
            receipt = {"status": "published", "operation_id": op["operation_id"],
                       "contribution_id": rec["contribution_id"], "revision": 2,
                       "repository": FORK, "repository_id": 9, "pr_id": 77, "pr_number": 4,
                       "head_sha": SHA_B, "base_ref": "main", "development_pr_run_id": 100}
            result = reconcile.tick(api, snap, policy=policy(read_only=False),
                                    now=datetime.fromisoformat(NOW), publications=[],
                                    pressure=capacity.Pressure(ok=True), observe=False,
                                    development_prs=[receipt])
            current = [value for kind, value in result.staged if kind == "contributions"][-1]
            self.assertEqual(current["fork"]["pr_number"], 4)
            self.assertIsNone(current["active_operation_id"])
            self.assertEqual(current["revision"], 2)
            self.assertTrue(any(kind == "operations" and value["status"] == "confirmed"
                                for kind, value in result.staged))
            [event] = [value for kind, value in result.staged if kind == "events"]
            self.assertEqual((event["kind"], event["details"]["pr_number"]),
                             ("confirm_development_pr", 4))

    def test_create_and_read_back_exact_same_fork_pr(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rec, op = records(root); api = FakeApi(rec, op)
            with patch("dev_pr.Api", return_value=api), \
                 patch("dev_pr.provisioning.WriteApi", return_value=api):
                receipt = dev_pr.publish(root, op["operation_id"], "token", "99")
            self.assertEqual((receipt["pr_number"], receipt["head_sha"]), (4, SHA_B))
            pr_write = next(call for call in api.writes if call[0] == "POST")
            self.assertEqual(pr_write[2]["head"], "riscv64-dev")
            self.assertEqual(pr_write[2]["base"], "main")

    def test_new_registration_reopens_existing_marked_pr_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rec, op = records(root); api = FakeApi(rec, op, existing=True)
            api.workflows_registered = False; api.workflow_active = False
            with patch("dev_pr.Api", return_value=api), \
                 patch("dev_pr.provisioning.WriteApi", return_value=api):
                receipt = dev_pr.publish(root, op["operation_id"], "token", "101")
            self.assertEqual(receipt["pr_number"], 4)
            self.assertEqual([call[2]["state"] for call in api.writes if call[0] == "PATCH"], ["closed", "open"])

    def test_the_development_pr_names_no_wheel(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rec, op = records(root); api = FakeApi(rec, op)
            with patch("dev_pr.Api", return_value=api), \
                 patch("dev_pr.provisioning.WriteApi", return_value=api):
                dev_pr.publish(root, op["operation_id"], "token", "99")
            pr_write = next(call for call in api.writes if call[0] == "POST")
            self.assertEqual(pr_write[2]["title"], "CI: develop riscv64 support")
            self.assertIn("## riscv64 development", pr_write[2]["body"])
            self.assertNotIn("wheel", pr_write[2]["body"])

    def test_a_retried_create_accepts_the_body_its_marker_wrote(self):
        """A create started before a change of the body wording finds its
        own marked PR on the retry and accepts that PR's body."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rec, op = records(root); api = FakeApi(rec, op, existing=True)
            api.pr["body"] = "## riscv64 wheel development\n\n" + op["proposed"]["marker"]
            with patch("dev_pr.Api", return_value=api), \
                 patch("dev_pr.provisioning.WriteApi", return_value=api):
                receipt = dev_pr.publish(root, op["operation_id"], "token", "100")
            self.assertEqual(receipt["pr_number"], 4)
            self.assertEqual(api.writes, [])

    def test_existing_marked_pr_is_reused_without_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rec, op = records(root); api = FakeApi(rec, op, existing=True)
            with patch("dev_pr.Api", return_value=api), \
                 patch("dev_pr.provisioning.WriteApi", return_value=api):
                receipt = dev_pr.publish(root, op["operation_id"], "token", "100")
            self.assertEqual(receipt["pr_number"], 4)
            self.assertEqual(api.writes, [])


def published(**over):
    """A record in `development.waiting_ci` whose published head has no
    recorded development PR, with the confirmed publication operation."""
    rec = contribution(phase="development", activity="waiting_ci", revision=2,
                       plan_evidence_id="plan-1", scope_paths=[".github/workflows/build.yml"])
    rec["fork"] = {"repository_id": 9, "name": FORK, "head_ref": "riscv64-dev", "head_sha": SHA_B,
                   "pr_number": None}
    rec["proposal_artifact"] = {"work_id": "w", "claim_id": "c", "artifact_id": 1, "name": "n",
                                "run_id": "5", "outcome": "published",
                                "result_commit": "q_result", "digest": "sha256:" + "0" * 64}
    branch = recovery.plan(contribution_id=rec["contribution_id"], revision=2, kind="branch_update",
                           policy_version="test", reason_ids=["result"],
                           target={"repository": FORK, "repository_id": 9, "ref": "riscv64-dev",
                                   "expected_head": SHA_A, "base_sha": SHA_A}, now=NOW)
    branch = recovery.fold_receipt(branch, external_ids=None, detail="head verified", now=NOW)
    rec["publication_operation_id"] = branch["operation_id"]
    rec.update(over)
    return rec, {branch["operation_id"]: branch}


T0 = datetime.fromisoformat(NOW)


def run(rec, ops, minutes=0, runs=None, **inputs):
    """One controller tick at ``minutes`` after ``NOW``: the report, the
    record, the operations, and the event kinds it staged. ``runs`` is the
    development PR run list (read and empty by default)."""
    ops = dict(ops)
    res = controller.tick(contributions={"c": rec}, operations=ops, consumed={}, projects={},
                          policy=policy(read_only=False),
                          inputs=controller.Inputs(now=T0 + timedelta(minutes=minutes),
                                                   pressure=capacity.Pressure(ok=True),
                                                   development_pr_runs={} if runs is None else runs,
                                                   **inputs))
    for kind, value in res.staged:
        if kind == "operations":
            ops[value["operation_id"]] = value
    current = ([value for kind, value in res.staged if kind == "contributions"] or [rec])[-1]
    events = [value for kind, value in res.staged if kind == "events"]
    return res, current, ops, events


def receipt_for(rec, op, pr_number=4):
    return {"status": "published", "operation_id": op["operation_id"],
            "contribution_id": rec["contribution_id"], "revision": op["revision"],
            "repository": FORK, "repository_id": 9, "pr_id": 77, "pr_number": pr_number,
            "head_sha": op["target"]["expected_head"], "base_ref": "main",
            "development_pr_run_id": 100}


class ReadbackApi:
    """The readback of the reconciler: the open PR ``number`` with ``body``."""

    def __init__(self, body, number=4):
        self.pr = {"id": 77, "number": number, "state": "open", "body": body,
                   "html_url": f"https://github.com/{FORK}/pull/{number}",
                   "head": {"sha": SHA_B, "ref": "riscv64-dev", "repo": {"id": 9}},
                   "base": {"ref": "main"}}

    def get(self, path):
        if path == f"/repos/{FORK}/pulls/{self.pr['number']}":
            return self.pr
        raise AssertionError(path)


def fold(rec, ops, receipt, body):
    snap = reconcile.Snapshot({"c": rec}, dict(ops), {}, {}, {})
    return reconcile.tick(ReadbackApi(body, receipt["pr_number"]), snap,
                          policy=policy(read_only=False), now=T0, publications=[],
                          pressure=capacity.Pressure(ok=True), observe=False,
                          development_prs=[receipt])


class TestDevelopmentPRRows(unittest.TestCase):
    """The development PR operation is taken through the rows of
    `development.waiting_ci`."""

    def exhaust(self):
        rec, ops = published()
        res, rec, ops, events = run(rec, ops)
        self.assertEqual(events[0]["kind"], "launch_development_pr")
        for n, minutes in ((2, 6), (3, 12)):
            res, rec, ops, events = run(rec, ops, minutes)
            self.assertEqual([e["kind"] for e in events], ["launch_development_pr"])
            self.assertEqual(res.report["dev_pr_requests"][0]["attempt"], n)
        res, rec, ops, events = run(rec, ops, 18)
        return res, rec, ops, events

    def test_the_intent_and_its_launch_are_one_row(self):
        rec, ops = published()
        res, current, ops, events = run(rec, ops)
        [event] = events
        self.assertEqual((event["kind"], event["details"]["attempt"]),
                         ("launch_development_pr", 1))
        self.assertEqual(res.report["dev_pr_requests"][0]["attempt"], 1)
        op = ops[current["active_operation_id"]]
        self.assertEqual((op["kind"], op["status"], recovery.launch_attempts(op)),
                         ("dev_pr_create", "planned", 1))
        self.assertEqual(engine.state_key(current), "development.waiting_ci")
        out = take(rec, "launch_development_pr", policy=policy(), now=NOW,
                                      facts={"development_pr_operation_planned"},
                                      counts={"operation_launch": 0})
        self.assertEqual(out.charged, ("operation_launch",))

    def test_a_relaunch_waits_the_interval_and_nothing_launches_while_paused(self):
        rec, ops = published()
        _res, rec, ops, _events = run(rec, ops)
        res, same, _ops, events = run(rec, ops, 2)
        self.assertEqual((events, res.report["dev_pr_requests"]), ([], []))
        res, _rec, _ops, events = run(rec, ops, 6)
        self.assertEqual([e["kind"] for e in events], ["launch_development_pr"])
        # A paused contribution launches nothing: its planned operation is
        # closed and released without a row.
        paused = dict(rec, paused=True)
        res, current, closed, events = run(paused, ops, 12)
        self.assertEqual((events, res.report["dev_pr_requests"]), ([], []))
        self.assertIsNone(current["active_operation_id"])
        self.assertEqual(closed[rec["active_operation_id"]]["status"], "failed")
        self.assertEqual(res.report["development_pr_closures"][0]["closed"],
                         "the contribution is paused")
        # A fresh paused record plans nothing.
        fresh, fresh_ops = published(paused=True)
        res, current, _ops, events = run(fresh, fresh_ops)
        self.assertEqual((events, res.report["dev_pr_requests"], current["active_operation_id"]),
                         ([], [], None))

    def test_spent_launches_wait_for_a_human_with_the_operation_closed(self):
        res, rec, ops, events = self.exhaust()
        self.assertEqual([e["kind"] for e in events], ["exhaust_development_pr_launches"])
        self.assertEqual(engine.state_key(rec), "development.waiting_human")
        self.assertEqual((rec["pending_human_action"]["kind"], rec["pending_human_action"]["counter"]),
                         (controller.DEVELOPMENT_PR_LAUNCH_EXHAUSTED, "operation_launch"))
        self.assertIsNone(rec["active_operation_id"])
        [op] = [o for o in ops.values() if o["kind"] == "dev_pr_create"]
        self.assertEqual(op["status"], "failed")
        self.assertEqual(events[0]["details"]["launches"], 3)
        # The wait is quiescent: park applies (refresh needs a recorded PR,
        # see the CI bootstrap below).
        out = apply_command(rec, commands.parse(
            f"/upstreaming park revision={rec['revision']} reason=stuck"), operations=ops)
        self.assertTrue(out.accepted, out.reason)
        # The reminder names the operation from trusted values only.
        details = controller.reminder_details(rec, ops)
        self.assertEqual((details["operation"], details["launches"]), (op["operation_id"], 3))

    def test_the_resume_returns_to_the_ci_wait_and_plans_a_new_operation(self):
        _res, rec, ops, _events = self.exhaust()
        [old] = [o for o in ops.values() if o["kind"] == "dev_pr_create"]
        out = apply_command(rec, commands.parse(
            f"/upstreaming resume revision={rec['revision']} reason=fixed"), operations=ops)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.event["kind"], "resume_ci")
        self.assertEqual(engine.state_key(out.record), "development.waiting_ci")
        self.assertEqual(out.record["revision"], rec["revision"] + 1)
        self.assertEqual(out.restarted["counter"], "operation_launch")
        res, current, ops2, events = run(out.record, ops, 30)
        self.assertEqual([e["kind"] for e in events], ["launch_development_pr"])
        new = ops2[current["active_operation_id"]]
        self.assertNotEqual(new["operation_id"], old["operation_id"])
        self.assertEqual((new["kind"], recovery.launch_attempts(new)), ("dev_pr_create", 1))

    def test_each_resume_applies_to_its_own_pending_action(self):
        _res, rec, ops, _events = self.exhaust()
        self.assertIsNotNone(refusal(rec, "resume_development", policy=policy(), now=NOW))
        for kind in ("ci_interrupted", "development_pr_mismatch"):
            with self.subTest(kind=kind):
                other = dict(rec, pending_human_action=dict(rec["pending_human_action"],
                                                            kind=kind, counter=None))
                self.assertIsNotNone(refusal(other, "resume_ci",
                                                    policy=policy(), now=NOW))

    def test_a_verified_receipt_confirms_also_while_paused(self):
        for paused in (False, True):
            with self.subTest(paused=paused):
                rec, ops = published()
                _res, rec, ops, _events = run(rec, ops)
                op = ops[rec["active_operation_id"]]
                held = dict(rec, paused=paused)
                out = fold(held, ops, receipt_for(rec, op), dev_pr_body(rec, op))
                # The first write is the row; the controller of the same tick
                # then plans the CI bootstrap unless the record is paused.
                current = [v for k, v in out.staged if k == "contributions"][-1]
                self.assertEqual((current["fork"]["pr_number"], current["revision"]),
                                 (4, rec["revision"]))
                staged = {v["operation_id"]: v for k, v in out.staged if k == "operations"}
                self.assertEqual(
                    None if paused else staged[current["active_operation_id"]]["kind"],
                    current["active_operation_id"] if paused else "dev_pr_update")
                events = [v["kind"] for k, v in out.staged if k == "events"]
                self.assertEqual(events, ["confirm_development_pr"]
                                 + ([] if paused else ["launch_development_pr"]))
                confirmed = [v for k, v in out.staged if k == "operations"][0]
                self.assertEqual((confirmed["operation_id"], confirmed["status"]),
                                 (op["operation_id"], "confirmed"))

    def test_a_receipt_outside_the_ci_wait_closes_and_keeps_the_state(self):
        rec, ops = published()
        _res, rec, ops, _events = run(rec, ops)
        op = ops[rec["active_operation_id"]]
        moved = dict(rec, activity="fix_needed", last_ci_evidence_id=None)
        out = fold(moved, ops, receipt_for(rec, op), dev_pr_body(rec, op))
        # The state is kept (the controller of the same tick then requests
        # the repair Work that is due).
        current = [v for k, v in out.staged if k == "contributions"][-1]
        self.assertEqual((current["phase"], current["fork"]["pr_number"],
                          current["active_operation_id"]), ("development", None, None))
        self.assertEqual([v["kind"] for k, v in out.staged if k == "events"], ["request_patch"])
        self.assertNotIn("confirm_development_pr",
                         [v["kind"] for k, v in out.staged if k == "events"])
        self.assertEqual([v["status"] for k, v in out.staged if k == "operations"][0],
                         "confirmed")

    def test_the_readback_accepts_this_contributions_marker_only(self):
        rec, ops = published()
        _res, rec, ops, _events = run(rec, ops)
        op = ops[rec["active_operation_id"]]
        older = f"x\n<!-- upstreaming-development:{rec['contribution_id']}:r1:{SHA_A} -->"
        out = fold(rec, ops, receipt_for(rec, op), older)
        self.assertEqual(out.report["development_prs"][0]["status"], "confirmed")
        other = "x\n<!-- upstreaming-development:example--other/3:r2:" + SHA_B + " -->"
        out = fold(rec, ops, receipt_for(rec, op), other)
        self.assertEqual(out.report["development_prs"][0]["status"], "PR readback mismatch")
        # The CI bootstrap no longer accepts another contribution's marker.
        boot = dict(op, kind="dev_pr_update")
        out = fold(rec, {**ops, op["operation_id"]: boot}, receipt_for(rec, boot), other)
        self.assertEqual(out.report["development_prs"][0]["status"], "PR readback mismatch")

    def test_an_operation_that_no_longer_matches_is_closed_without_a_row(self):
        rec, ops = published()
        _res, rec, ops, _events = run(rec, ops)
        oid = rec["active_operation_id"]
        changes = {
            "head": dict(rec, revision=3, fork=dict(rec["fork"], head_sha=SHA_A)),
            "pause": dict(rec, paused=True),
            "ci failure": dict(rec, activity="fix_needed", last_ci_evidence_id="ci-2-7-1"),
        }
        for why, changed in changes.items():
            with self.subTest(change=why):
                res, current, closed, _events = run(changed, ops, 1)
                self.assertEqual(closed[oid]["status"], "failed")
                self.assertNotEqual(current.get("active_operation_id"), oid)
                self.assertEqual(res.report["development_pr_closures"][0]["operation_id"], oid)
        # Work is due again once the operation is released.
        _res, current, _ops, _events = run(changes["ci failure"], ops, 1)
        self.assertIsNone(current["active_operation_id"])
        self.assertEqual(controller._actionable(current), "development_repair")

    def test_a_run_in_flight_holds_the_relaunch_and_the_exhaustion(self):
        rec, ops = published()
        _res, rec, ops, _events = run(rec, ops)
        oid = rec["active_operation_id"]
        for minutes in (6, 12):
            _res, rec, ops, _events = run(rec, ops, minutes)
        for runs in ({oid: 55}, None):
            with self.subTest(runs=runs):
                res, held, _ops, events = controller_tick_with_runs(rec, ops, 20, runs)
                self.assertEqual((events, res.report["dev_pr_requests"]), ([], []))
                self.assertEqual(engine.state_key(held), "development.waiting_ci")
                # After `DEV_PR_RUN_SECONDS` the hold ends: the wait.
                res, done, _ops, events = controller_tick_with_runs(
                    rec, ops, 12 + controller.DEV_PR_RUN_SECONDS // 60, runs)
                self.assertEqual([e["kind"] for e in events], ["exhaust_development_pr_launches"])

    def test_the_ci_bootstrap_goes_through_the_same_rows(self):
        rec, ops = published()
        _res, rec, ops, _events = run(rec, ops)
        op = ops[rec["active_operation_id"]]
        out = fold(rec, ops, receipt_for(rec, op), dev_pr_body(rec, op))
        self.assertEqual([v["kind"] for k, v in out.staged if k == "events"],
                         ["confirm_development_pr", "launch_development_pr"])
        rec = [v for k, v in out.staged if k == "contributions"][-1]
        ops = {**ops, **{v["operation_id"]: v for k, v in out.staged if k == "operations"}}
        boot = ops[rec["active_operation_id"]]
        self.assertEqual(boot["kind"], "dev_pr_update")
        for minutes in (7, 13):
            _res, rec, ops, _events = run(rec, ops, minutes)
        res, rec, ops, events = run(rec, ops, 19)
        self.assertEqual([e["kind"] for e in events], ["exhaust_development_pr_launches"])
        self.assertEqual(ops[boot["operation_id"]]["status"], "failed")
        refreshed = apply_command(rec, commands.parse(
            f"/upstreaming refresh revision={rec['revision']} reason=again"), operations=ops)
        self.assertTrue(refreshed.accepted, refreshed.reason)
        resumed = apply_command(rec, commands.parse(
            f"/upstreaming resume revision={rec['revision']} reason=fixed"), operations=ops)
        self.assertTrue(resumed.accepted, resumed.reason)
        res, rec, ops, events = run(resumed.record, ops, 25)
        again = ops[rec["active_operation_id"]]
        self.assertEqual(again["kind"], "dev_pr_update")
        self.assertNotEqual(again["operation_id"], boot["operation_id"])
        self.assertIn("replan:1", again["reason_ids"])

    def test_no_code_path_records_the_pr_beside_a_row(self):
        self.assertNotIn("development_pr_recorded", engine.BESIDE_ROW)
        setters = {e.verb for e in lifecycle.table_edges()
                   if any(s.split("=")[0] in ("fork", "fork.pr_number")
                          for s in e.transition.effects.sets)}
        self.assertEqual(setters, {"confirm_development_pr", "confirm_provisioning"})


def controller_tick_with_runs(rec, ops, minutes, runs):
    """``run`` with the run list exactly as given (``None``: not read)."""
    ops = dict(ops)
    res = controller.tick(contributions={"c": rec}, operations=ops, consumed={}, projects={},
                          policy=policy(read_only=False),
                          inputs=controller.Inputs(now=T0 + timedelta(minutes=minutes),
                                                   pressure=capacity.Pressure(ok=True),
                                                   development_pr_runs=runs))
    current = ([value for kind, value in res.staged if kind == "contributions"] or [rec])[-1]
    events = [value for kind, value in res.staged if kind == "events"]
    return res, current, ops, events


def dev_pr_body(rec, op):
    return dev_pr.body(dict(rec, plan_evidence_id="plan-1"), op)


class TestDevelopmentPRAdoption(unittest.TestCase):
    """A create adopts a PR this contribution's marker names, of any revision
    or head, and still refuses anything else."""

    def publish(self, body=None, second=None):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rec, op = records(root); api = FakeApi(rec, op, existing=True)
            if body is not None:
                api.pr["body"] = body
            if second is not None:
                extra = dict(api.pr, id=78, number=5, body=second)
                api.get = (lambda orig: lambda path: [api.pr, extra]
                           if path.startswith(f"/repos/{FORK}/pulls?") else orig(path))(api.get)
            with patch("dev_pr.Api", return_value=api), \
                 patch("dev_pr.provisioning.WriteApi", return_value=api):
                return dev_pr.publish(root, op["operation_id"], "token", "100"), api

    def test_a_pr_of_another_revision_or_head_is_adopted(self):
        body = f"older wording\n<!-- upstreaming-development:example--proj/12:r1:{SHA_A} -->"
        receipt, api = self.publish(body)
        self.assertEqual(receipt["pr_number"], 4)
        self.assertFalse([w for w in api.writes if w[0] == "POST"])

    def test_anything_else_is_refused(self):
        other = f"<!-- upstreaming-development:example--other/3:r2:{SHA_B} -->"
        for body, second in (("a foreign pull request", None), (other, None),
                             (MARKER, MARKER.replace("r2", "r1"))):
            with self.subTest(body=body, second=second):
                with self.assertRaises(dev_pr.DevPRError):
                    self.publish(body, second)

    def test_the_request_is_only_loaded_in_the_ci_wait(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); rec, op = records(root)
            moved = dict(rec, activity="fix_needed")
            (root / ".state/contributions/example--proj--12.json").write_text(json.dumps(moved))
            with self.assertRaises(dev_pr.DevPRError):
                dev_pr.load_request(root, op["operation_id"])
