# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The operation table (``lifecycle.OPERATIONS``) and its engine in
``recovery``: every row, the single write path of an operation's status, the
lifecycle rows an operation fires, the launch budget, the fence of an open
operation, and ``resolve-operation``."""

import ast
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import capacity
import collect
import commands
import controller
import engine
import lifecycle as lc
import reconcile
import recovery
import schemas
from tests.helpers import NOW, OWNER, SHA_A, SHA_C, STRANGER, apply_command, contribution, policy

ROOT = Path(__file__).resolve().parents[1]
POLICY = policy(read_only=False)
T = datetime.fromisoformat(NOW)
EVIDENCE = {"schema_version": 1, "evidence_id": "ev-9", "kind": "resolution",
            "policy_version": "p", "collected_at": NOW, "summary": "the run never started"}


def operation(status="planned", kind="branch_update", cid="example--proj/12", **over):
    op = recovery.plan(contribution_id=cid, revision=3, kind=kind, policy_version="p",
                       reason_ids=["r-1"], target={"repository": "riseproject-upstreaming/proj",
                                                   "ref": "riscv64-dev", "expected_head": SHA_A},
                       proposed={"commit": "b" * 40}, now=NOW)
    op.update(status=status, **over)
    schemas.validate("operation", op)
    return op


def resolve_command(op, disposition="not-launched", evidence="ev-9"):
    return commands.parse(f"/upstreaming resolve-operation operation={op['operation_id']} "
                          f"disposition={disposition} evidence={evidence}")


# The facts and arguments that establish every guard of a row.
ROW_FACTS = {
    "begin_effect": {"capacity_available", "contribution_not_paused", "final_check_passed"},
    "record_response": {"response_not_failed"},
    "record_failure": {"response_failed"},
    "confirm_by_discovery": {"resource_found"},
    "record_discovery": {"resource_not_found"},
}
ROW_VALUES = {
    "record_response": {"external_ids": {"run": 5}},
    "record_failure": {"external_ids": {"run": 5}},
    "confirm_by_discovery": {"discovery": {"result": "confirmed", "detail": "found"}},
    "record_discovery": {"discovery": {"result": "absent", "detail": "none"}},
}
DISPOSITIONS = {"resolve_confirmed": "confirmed", "resolve_failed": "failed",
                "resolve_not_launched": "not-launched"}


def take(edge, op=None, **over):
    op = op or operation(edge.source)
    kwargs = dict(now=NOW, facts=ROW_FACTS.get(edge.verb, set()),
                  values=ROW_VALUES.get(edge.verb))
    if edge.verb in DISPOSITIONS:
        kwargs.update(command=resolve_command(op, DISPOSITIONS[edge.verb]), evidence=EVIDENCE,
                      actor=OWNER, command_id="cmd-1")
    kwargs.update(over)
    return recovery.apply_operation(op, edge.verb, **kwargs)


class TestOperationTable(unittest.TestCase):
    def test_states_are_the_table_and_the_schema(self):
        self.assertEqual(recovery.STATUSES, tuple(lc.OPERATIONS))
        self.assertEqual(recovery.OPEN, {k for k, s in lc.OPERATIONS.items() if s.open})
        schema = json.loads((ROOT / "schemas/operation.schema.json").read_text())
        self.assertEqual(set(schema["properties"]["status"]["enum"]), set(lc.OPERATIONS))

    def test_every_guard_has_one_predicate(self):
        self.assertEqual(recovery.table_guards() - set(recovery.PREDICATES), set())
        self.assertEqual(set(recovery.PREDICATES) - recovery.table_guards(), set())
        self.assertEqual({f for pair in recovery.CONTRADICTORY_FACTS for f in pair}
                         - recovery.FACT_NAMES, set())

    def test_record_intent_is_planned(self):
        op = operation()
        self.assertEqual(op["status"], lc.OPERATION_INITIAL_STATE)
        self.assertEqual((op["discovery"], op["resolution"], op["external_ids"]), ([], None, {}))

    def test_every_row_is_taken_with_its_effects(self):
        edges = list(lc.table_edges(lc.OPERATIONS))
        self.assertEqual(len(edges), 14)
        for edge in edges:
            with self.subTest(source=edge.source, verb=edge.verb):
                out = take(edge)
                self.assertTrue(out.accepted, out.reason)
                new = out.operation
                self.assertEqual(new["status"], edge.target)
                self.assertEqual(new["updated_at"], NOW)
                sets = set(edge.transition.effects.sets)
                if "external_ids" in sets:
                    self.assertEqual(new["external_ids"], {"run": 5})
                if "discovery" in sets:
                    self.assertEqual(len(new["discovery"]), 1)
                if "resolution" in sets:
                    self.assertEqual(new["resolution"], {
                        "actor": OWNER, "disposition": DISPOSITIONS[edge.verb],
                        "evidence_id": "ev-9", "command_id": "cmd-1"})
                for name in ("external_ids", "discovery", "resolution"):
                    if name not in sets:
                        self.assertEqual(new[name], operation(edge.source)[name])

    def test_every_row_is_refused_without_each_guard(self):
        for edge in lc.table_edges(lc.OPERATIONS):
            for fact in ROW_FACTS.get(edge.verb, set()):
                with self.subTest(verb=edge.verb, missing=fact):
                    out = take(edge, facts=ROW_FACTS[edge.verb] - {fact})
                    self.assertFalse(out.accepted)
                    self.assertEqual(out.operation, operation(edge.source))
            if edge.verb in DISPOSITIONS:
                op = operation(edge.source)
                for over in ({"evidence": None}, {"evidence": dict(EVIDENCE, kind="ci")},
                             {"command": resolve_command(operation("planned", kind="ci_rerun"),
                                                         DISPOSITIONS[edge.verb])}):
                    with self.subTest(verb=edge.verb, over=list(over)):
                        self.assertFalse(take(edge, op, **over).accepted)
                other = next(d for d in DISPOSITIONS.values() if d != DISPOSITIONS[edge.verb])
                self.assertFalse(take(edge, op, command=resolve_command(op, other)).accepted)

    def test_rows_the_table_does_not_have_raise(self):
        for status, verb in (("uncertain", "begin_effect"), ("planned", "confirm_by_discovery"),
                             ("confirmed", "resolve_failed"), ("failed", "record_discovery")):
            with self.subTest(status=status, verb=verb), self.assertRaises(recovery.RecoveryError):
                recovery.apply_operation(operation(status), verb, now=NOW,
                                         facts=ROW_FACTS.get(verb, set()))
        edge = lc.find_edge("applying", "record_response", lc.OPERATIONS)
        with self.assertRaises(recovery.RecoveryError):
            take(edge, values={"discovery": {"result": "absent"}})
        with self.assertRaises(recovery.RecoveryError):
            take(edge, facts={"response_not_failed", "response_failed"})
        with self.assertRaises(recovery.RecoveryError):
            take(lc.find_edge("uncertain", "confirm_by_discovery", lc.OPERATIONS),
                 values={"discovery": {"result": "absent", "detail": ""}})

    def test_named_helpers_take_the_rows(self):
        op = recovery.mark_applying(operation(), NOW, facts=recovery.RECEIPT_FACTS)
        self.assertEqual(op["status"], "applying")
        self.assertEqual(recovery.mark_result(op, response_ok=False, external_ids=None,
                                              now=NOW)["status"], "failed")
        lost = recovery.mark_result(op, response_ok=None, external_ids=None, now=NOW)
        self.assertEqual(lost["status"], "uncertain")
        for result in ("absent", "unrelated"):
            self.assertEqual(recovery.record_discovery(lost, result, "", NOW)["status"], "uncertain")
        with self.assertRaises(recovery.RecoveryError):
            recovery.record_discovery(lost, "error", "", NOW)
        self.assertEqual(recovery.record_discovery(lost, "confirmed", "", NOW)["status"],
                         "confirmed")
        folded = recovery.fold_receipt(operation(), external_ids={"pr": 1}, detail="read back",
                                       now=NOW)
        self.assertEqual((folded["status"], folded["external_ids"], len(folded["discovery"])),
                         ("confirmed", {"pr": 1}, 1))
        with self.assertRaises(recovery.RecoveryError):
            recovery.mark_applying(operation(), NOW)  # no fact: refused

    def test_resolve_takes_the_one_row_of_the_disposition(self):
        for status in sorted(recovery.OPEN):
            for disposition, target in (("confirmed", "confirmed"), ("failed", "failed"),
                                        ("not-launched", "not_launched")):
                op = operation(status)
                done = recovery.resolve(op, resolve_command(op, disposition), actor=OWNER,
                                        command_id="c", evidence=EVIDENCE, now=NOW)
                self.assertEqual(done["status"], target)
        op = operation("uncertain")
        with self.assertRaisesRegex(recovery.RecoveryError, "evidence record not found"):
            recovery.resolve(op, resolve_command(op, evidence="ev-8"), actor=OWNER,
                             command_id="c", evidence=EVIDENCE, now=NOW)
        with self.assertRaisesRegex(recovery.RecoveryError, "already confirmed"):
            closed = operation("confirmed")
            recovery.resolve(closed, resolve_command(closed), actor=OWNER, command_id="c",
                             evidence=EVIDENCE, now=NOW)


class TestStoreFence(unittest.TestCase):
    def test_status_changes_only_along_rows(self):
        for before in lc.OPERATIONS:
            for after in lc.OPERATIONS:
                why = recovery.transition_problem(operation(before), operation(after))
                legal = after in recovery.reachable(before)
                with self.subTest(before=before, after=after):
                    self.assertEqual(why is None, legal)
        for end in ("confirmed", "failed", "not_launched"):
            self.assertEqual(recovery.reachable(end), {end})
        self.assertIsNone(recovery.transition_problem(None, operation("confirmed")))

    def test_the_state_diff_refuses_a_closed_operation_that_changes(self):
        import state
        from tests.helpers import git
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@example.com")
            git(repo, "config", "user.name", "t")
            op = operation("confirmed")
            path = repo / state.record_path("operations", op)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(op))
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD").strip()
            path.write_text(json.dumps(dict(op, status="planned")))
            git(repo, "commit", "-qam", "candidate")
            report = state.validate_state_diff(repo, base, "HEAD", POLICY)
            self.assertTrue(any("cannot become planned" in p for p in report.problems),
                            report.problems)


# --------------------------------------------------------------------------
# Single write path of an operation's status

# Status values only an operation has.
OPERATION_ONLY = frozenset(recovery.STATUSES) - {"confirmed", "failed"}
# Names that hold a receipt or a report, never an operation.
RECEIPT_NAMES = {"plan"}


def status_writes(path: Path) -> list[str]:
    """Places where a module writes the status of an operation."""
    found = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        where = f"{path.name}:{getattr(node, 'lineno', 0)}"
        targets = node.targets if isinstance(node, ast.Assign) else \
            [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant) \
                        and sub.slice.value == "status":
                    found.append(f"{where}: ['status'] is assigned")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else \
                node.func.attr if isinstance(node.func, ast.Attribute) else ""
            first = node.args[0] if node.args else None
            receipt = isinstance(first, ast.Name) and first.id in RECEIPT_NAMES
            if name in ("dict", "update", "replace", "setdefault") and not receipt \
                    and "status" in {k.arg for k in node.keywords}:
                found.append(f"{where}: {name}(..., status=...)")
            if name in ("setdefault", "__setitem__") and isinstance(first, ast.Constant) \
                    and first.value == "status":
                found.append(f"{where}: .{name}('status')")
            if name == "validate" and isinstance(first, ast.Constant) and first.value == "operation" \
                    and len(node.args) > 1 and not isinstance(node.args[1], ast.Name):
                found.append(f"{where}: an operation is built and validated")
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "status":
                    if any(k is None for k in node.keys):
                        found.append(f"{where}: a record is copied with its status replaced")
                    elif isinstance(value, ast.Constant) and value.value in OPERATION_ONLY:
                        found.append(f"{where}: a status {value.value!r} is written")
    return found


def pipeline_modules() -> list[Path]:
    return sorted(p for p in (*ROOT.glob("*.py"), *ROOT.glob("scripts/*.py")))


class TestSingleWritePath(unittest.TestCase):
    def test_only_the_operation_engine_writes_an_operation_status(self):
        offenders = [w for path in pipeline_modules() if path.name != "recovery.py"
                     for w in status_writes(path)]
        self.assertEqual(offenders, [])

    def test_the_scan_finds_each_kind_of_write(self):
        samples = ('op["status"] = "failed"', 'new = dict(op, status="planned")',
                   'op.update(status="confirmed")', 'new = {**op, "status": "failed"}',
                   'op = {"operation_id": "x", "status": "uncertain"}',
                   'op.setdefault("status", "planned")',
                   'schemas.validate("operation", dict(op, x=1))')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.py"
            for sample in samples:
                path.write_text(sample + "\n")
                with self.subTest(sample=sample):
                    self.assertEqual(len(status_writes(path)), 1)
            path.write_text('report = {"status": "confirmed"}\nx = dict(plan, status="provisioned")\n'
                            'schemas.validate("operation", op)\nok = op["status"] == "planned"\n')
            self.assertEqual(status_writes(path), [])


# --------------------------------------------------------------------------
# The fork provisioning operation and the lifecycle rows it takes


def provisioning_record(**over):
    rec = contribution(phase="provisioning", activity="idle", fork=None,
                       plan_evidence_id="plan-1", scope_paths=[".github/workflows/*"])
    rec.update(over)
    return rec


def inputs(at=T, ok=True, comments=()):
    return controller.Inputs(now=at, pressure=capacity.Pressure(ok=ok), comments=list(comments))


def tick(rec, operations, at=T, ok=True, comments=(), resolution=None):
    i = inputs(at, ok, comments)
    if resolution:
        i.resolution_evidence = resolution
    return controller.tick(contributions={"c": copy.deepcopy(rec)},
                           operations=copy.deepcopy(operations), consumed={}, projects={},
                           policy=POLICY, inputs=i)


def staged(result, kind):
    return [r for k, r in result.staged if k == kind]


class TestProvisioningLaunch(unittest.TestCase):
    def first(self):
        result = tick(provisioning_record(), {})
        [op] = staged(result, "operations")
        rec = staged(result, "contributions")[-1]
        return result, rec, op

    def test_the_intent_is_recorded_and_launched_by_start_operation(self):
        result, rec, op = self.first()
        self.assertEqual((op["kind"], op["status"], recovery.launch_attempts(op)),
                         ("fork_provision", "planned", 1))
        self.assertEqual(engine.state_key(rec), "provisioning.idle")
        self.assertEqual(rec["active_operation_id"], op["operation_id"])
        self.assertEqual(rec["revision"], provisioning_record()["revision"])
        [event] = staged(result, "events")
        self.assertEqual((event["kind"], event["cause"]["type"]), ("start_operation", "tick"))
        self.assertEqual(event["details"]["charged"], ["operation_launch"])
        self.assertEqual(result.report["provision_requests"],
                         [{"operation_id": op["operation_id"], "contribution_id": rec["contribution_id"],
                           "attempt": 1}])

    def test_launches_are_counted_until_operation_launch_is_exhausted(self):
        _result, rec, op = self.first()
        limit = lc.budget_limit("operation_launch", POLICY)
        at = T
        ops = {op["operation_id"]: op}
        # Not again before the launch interval.
        self.assertEqual(tick(rec, ops, at + timedelta(seconds=60)).report["provision_requests"], [])
        for attempt in range(2, limit + 1):
            at += timedelta(seconds=controller.LAUNCH_INTERVAL_SECONDS)
            result = tick(rec, ops, at)
            [op] = staged(result, "operations")
            ops = {op["operation_id"]: op}
            rec = staged(result, "contributions")[-1]
            self.assertEqual(result.report["provision_requests"][0]["attempt"], attempt)
            self.assertEqual(recovery.launch_attempts(op), attempt)
        at += timedelta(seconds=controller.LAUNCH_INTERVAL_SECONDS)
        result = tick(rec, ops, at)
        self.assertEqual(result.report["provision_requests"], [])
        self.assertEqual(staged(result, "operations"), [])
        [event] = staged(result, "events")
        self.assertEqual((event["kind"], event["cause"]["type"]),
                         ("require_provisioning_intervention", "operation"))
        waiting = staged(result, "contributions")[-1]
        self.assertEqual(engine.state_key(waiting), "provisioning.waiting_human")
        self.assertEqual((waiting["pending_human_action"]["kind"],
                          waiting["pending_human_action"]["counter"]),
                         (controller.LAUNCH_EXHAUSTED, "operation_launch"))
        # The wait is left only by its rows: no launch, and the open operation
        # fences park and the stale resume.
        later = tick(waiting, ops, at + timedelta(days=1))
        self.assertEqual(later.report["provision_requests"], [])
        for text in ('/upstreaming park reason="x"',
                     f'/upstreaming resume revision={waiting["revision"]} reason="x"'):
            out = apply_command(waiting, commands.parse(text), operations=ops, policy_=POLICY,
                                now=(at + timedelta(days=30)).isoformat())
            self.assertFalse(out.accepted)
            self.assertIn(op["operation_id"], out.reason)

    def test_no_launch_while_paused_or_without_capacity(self):
        _result, rec, op = self.first()
        ops = {op["operation_id"]: op}
        at = T + timedelta(seconds=controller.LAUNCH_INTERVAL_SECONDS)
        self.assertEqual(tick(rec, ops, at, ok=False).report["provision_requests"], [])
        paused = dict(rec, paused=True, paused_at=NOW, paused_by="human", paused_reason="x")
        exhausted = dict(op, external_ids={recovery.LAUNCH_COUNTER: 3})
        result = tick(paused, {op["operation_id"]: exhausted}, at)
        self.assertEqual((result.report["provision_requests"], staged(result, "events")), ([], []))

    def test_a_resume_that_names_operation_launch_starts_the_launches_again(self):
        rec = provisioning_record(activity="waiting_human", pending_human_action={
            "kind": controller.LAUNCH_EXHAUSTED, "requested_at": NOW, "reminded_at": None,
            "escalated_at": None, "counter": "operation_launch"})
        op = operation(kind="fork_provision")
        for _ in range(3):
            op = recovery.charge_launch(op, NOW)
        rec["active_operation_id"] = op["operation_id"]
        ops = {op["operation_id"]: op}
        cmd = commands.parse(f'/upstreaming resume revision={rec["revision"]} reason="fixed"')
        # The human resume of the wait asserts that the prerequisite is
        # corrected.
        out = apply_command(rec, cmd, operations=ops, policy_=POLICY)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(out.verb, "resume_provisioning")
        self.assertEqual(out.restarted, {"counter": "operation_launch", "count": 3})
        res = controller.TickResult()
        controller._restart_launches(out, ops, res, NOW)
        [restarted] = staged(res, "operations")
        self.assertEqual(recovery.launch_attempts(restarted), 0)
        self.assertEqual(restarted["status"], "planned")


class ReceiptWorld:
    def receipt_world(self, rec):
        op = recovery.charge_launch(recovery.plan(
            contribution_id=rec["contribution_id"], revision=rec["revision"], kind="fork_provision",
            policy_version="test", reason_ids=["plan-1"],
            target={"repository": "riseproject-upstreaming/proj", "repository_id": None,
                    "ref": "riscv64-dev", "base_sha": SHA_C, "expected_head": None},
            proposed={"commit": SHA_C, "marker": "provision"}, now=NOW), NOW)
        rec = dict(rec, active_operation_id=op["operation_id"])
        receipt = {"contribution_id": rec["contribution_id"], "operation_id": op["operation_id"],
                   "revision": rec["revision"], "status": "provisioned",
                   "fork": "riseproject-upstreaming/proj", "upstream_id": 1, "base_sha": SHA_C,
                   "actions_read_only": True, "head_sha": SHA_C, "fork_id": 2,
                   "provision_run_id": "77", "provision_artifact_id": 88}

        class Api:
            def get(self, path):
                if path.endswith("/branches/riscv64-dev"):
                    return {"name": "riscv64-dev", "commit": {"sha": SHA_C, "commit": {"tree": {"sha": "d" * 40}},
                                       "parents": []}}
                return {"id": 2, "full_name": "riseproject-upstreaming/proj", "fork": True,
                        "parent": {"id": 1}, "default_branch": "main", "archived": False}
        return rec, op, receipt, Api()

    def fold(self, rec, op, receipt, api):
        snap = reconcile.Snapshot({"c": rec}, {op["operation_id"]: op}, {}, {}, {})
        original = collect.repository
        try:
            collect.repository = lambda a, name: {"id": 2, "full_name": name, "parent_id": 1}
            return reconcile.tick(api, snap, policy=POLICY, now=T, publications=[],
                                  pressure=capacity.Pressure(ok=True), provisions=[receipt],
                                  observe=False)
        finally:
            collect.repository = original


class TestProvisioningReceipt(ReceiptWorld, unittest.TestCase):
    def test_the_confirmed_operation_fires_confirm_provisioning(self):
        rec, op, receipt, api = self.receipt_world(provisioning_record())
        result = self.fold(rec, op, receipt, api)
        self.assertEqual(result.report["provisions"], [{"contribution": rec["contribution_id"],
                                                        "status": "provisioned"}])
        [confirmed] = [r for k, r in result.staged if k == "operations"]
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["external_ids"], {"launch_attempts": 1, "provision_run_id": "77",
                                                     "provision_artifact_id": 88})
        self.assertEqual([d["result"] for d in confirmed["discovery"]], ["confirmed"])
        events = [r for k, r in result.staged if k == "events"]
        self.assertEqual(events[0]["kind"], "confirm_provisioning")
        self.assertEqual(events[0]["cause"]["type"], "operation")
        self.assertFalse(events[0]["revision_bumped"])
        final = [r for k, r in result.staged if k == "contributions"][-1]
        self.assertEqual(final["fork"]["name"], "riseproject-upstreaming/proj")
        self.assertIsNone(final["active_operation_id"])
        # No revision bump: `confirm_provisioning` enters the state where the
        # first patch is due, and that patch is requested at this revision.
        self.assertEqual(final["revision"], rec["revision"])
        self.assertIn(engine.state_key(final), ("development.fix_needed",
                                                "development.agent_running"))

    def test_a_receipt_in_the_human_wait_closes_the_operation_only(self):
        rec, op, receipt, api = self.receipt_world(provisioning_record(
            activity="waiting_human", pending_human_action={
                "kind": controller.LAUNCH_EXHAUSTED, "requested_at": NOW, "reminded_at": None,
                "escalated_at": None, "counter": "operation_launch"}))
        result = self.fold(rec, op, receipt, api)
        [confirmed] = [r for k, r in result.staged if k == "operations"]
        self.assertEqual(confirmed["status"], "confirmed")
        final = [r for k, r in result.staged if k == "contributions"][-1]
        self.assertEqual(engine.state_key(final), "provisioning.waiting_human")
        self.assertIsNone(final["active_operation_id"])
        self.assertEqual([r for k, r in result.staged if k == "events"], [])


# --------------------------------------------------------------------------
# Every wait of provisioning has a human exit


class TestProvisioningFailureReceipts(ReceiptWorld, unittest.TestCase):
    HOSTILE = "@everyone **pwned** [x](https://evil.example) " + "<!-- -->" * 80

    def fold_all(self, rec, op, receipts, api):
        snap = reconcile.Snapshot({"c": rec}, {op["operation_id"]: op}, {}, {}, {})
        original = collect.repository
        try:
            collect.repository = lambda a, name: {"id": 2, "full_name": name, "parent_id": 1}
            return reconcile.tick(api, snap, policy=POLICY, now=T, publications=[],
                                  pressure=capacity.Pressure(ok=True), provisions=receipts,
                                  observe=False)
        finally:
            collect.repository = original

    def failure(self, receipt, run, **over):
        return receipt | {"status": "failed", "failure": "branch_conflict", "stage": "initialize",
                          "reason": "unexpected work", "provision_run_id": str(run)} | over

    def test_a_refusal_or_an_unknown_status_is_not_folded(self):
        rec, op, receipt, api = self.receipt_world(provisioning_record())
        for status in ("refused", "exploded"):
            with self.subTest(status=status):
                result = self.fold_all(rec, op, [dict(receipt, status=status, failure=None,
                                                      reason="paused")], api)
                self.assertEqual(staged(result, "operations"), [])
                self.assertEqual(staged(result, "contributions"), [])
                [entry] = result.report["provisions"]
                self.assertEqual(entry["status"], f"not folded: {status}")

    def test_an_unknown_code_is_other_and_the_reason_is_never_stored(self):
        rec, op, receipt, api = self.receipt_world(provisioning_record())
        result = self.fold_all(rec, op, [self.failure(receipt, 77, failure="../../x",
                                                       stage="evil", reason=self.HOSTILE)], api)
        [failed] = staged(result, "operations")
        self.assertEqual(failed["external_ids"]["provision_failure"], "other")
        [event] = staged(result, "events")
        self.assertEqual((event["details"]["failure"], event["details"]["stage"]), ("other", None))
        stored = json.dumps(result.staged)
        for fragment in ("@everyone", "pwned", "evil.example", "../../x"):
            self.assertNotIn(fragment, stored)

    def test_the_reminder_names_the_failed_operation_and_its_code(self):
        rec, op, receipt, api = self.receipt_world(provisioning_record())
        result = self.fold_all(rec, op, [self.failure(receipt, 77)], api)
        [failed] = staged(result, "operations")
        waiting = staged(result, "contributions")[-1]
        details = controller.reminder_details(waiting, {failed["operation_id"]: failed})
        self.assertEqual((details["pending_human_action"], details["operation"],
                          details["failure"]),
                         (controller.PROVISIONING_FAILED, failed["operation_id"],
                          "branch_conflict"))

    def test_only_the_newest_run_is_folded(self):
        rec, op, receipt, api = self.receipt_world(provisioning_record())
        result = self.fold_all(rec, op, [self.failure(receipt, 76),
                                         dict(receipt, provision_run_id="77")], api)
        self.assertEqual([o["status"] for o in staged(result, "operations")], ["confirmed"])
        self.assertEqual(sorted(e["status"] for e in result.report["provisions"]),
                         ["provisioned", "superseded"])
        result = self.fold_all(rec, op, [dict(receipt, provision_run_id="76"),
                                         self.failure(receipt, 77)], api)
        self.assertEqual([o["status"] for o in staged(result, "operations")], ["failed"])
        # A newer refusal does not hide an older foldable receipt.
        result = self.fold_all(rec, op, [self.failure(receipt, 76),
                                         dict(receipt, status="refused", provision_run_id="77")],
                               api)
        self.assertEqual([o["status"] for o in staged(result, "operations")], ["failed"])


def exhausted_wait():
    """Launches without a receipt until `operation_launch` is exhausted: the
    record in `provisioning.waiting_human`, its planned operation open."""
    result = tick(provisioning_record(), {})
    [op] = staged(result, "operations")
    rec = staged(result, "contributions")[-1]
    at = T
    while engine.state_key(rec) != "provisioning.waiting_human":
        at += timedelta(seconds=controller.LAUNCH_INTERVAL_SECONDS)
        result = tick(rec, {op["operation_id"]: op}, at)
        op = (staged(result, "operations") or [op])[-1]
        rec = staged(result, "contributions")[-1]
    return rec, op, at


def human(text, cid=50):
    return {"issue_number": 12, "comment": {"id": cid, "user": dict(OWNER), "created_at": NOW,
                                            "body": text}}


def command_tick(rec, ops, at, text):
    """One tick that applies the command ``text`` from the tracking issue."""
    result = tick(rec, ops, at, comments=[human(text)])
    [receipt] = staged(result, "commands")
    ops = dict(ops)
    for op in staged(result, "operations"):
        ops[op["operation_id"]] = op
    records = staged(result, "contributions")
    return receipt, (records[-1] if records else rec), ops, result


class TestProvisioningWaitExits(ReceiptWorld, unittest.TestCase):
    def receipt_for(self, rec, op, status="provisioned"):
        _rec, _op, receipt, api = self.receipt_world(rec)
        receipt = dict(receipt, operation_id=op["operation_id"], revision=rec["revision"],
                       status=status)
        return receipt, api

    def test_exhausted_launches_resume_relaunch_and_confirm(self):
        import provisioning
        rec, op, at = exhausted_wait()
        self.assertEqual((rec["pending_human_action"]["kind"], rec["pending_human_action"]["counter"]),
                         (controller.LAUNCH_EXHAUSTED, "operation_launch"))
        self.assertEqual(recovery.launch_attempts(op), lc.budget_limit("operation_launch", POLICY))
        ops = {op["operation_id"]: op}
        # Park is fenced by the open operation; the resume is the exit.
        out = apply_command(rec, commands.parse('/upstreaming park reason="x"'), operations=ops,
                            policy_=POLICY, now=(at + timedelta(days=1)).isoformat())
        self.assertFalse(out.accepted)
        self.assertIn(op["operation_id"], out.reason)
        at += timedelta(days=1)
        receipt, resumed, ops, result = command_tick(
            rec, ops, at, f'/upstreaming resume revision={rec["revision"]} reason="workflow fixed"')
        self.assertEqual(receipt["result"], "accepted", receipt)
        self.assertEqual([e["kind"] for e in staged(result, "events")][:2],
                         ["resume_provisioning", "start_operation"])
        self.assertEqual(engine.state_key(resumed), "provisioning.idle")
        self.assertEqual(resumed["revision"], rec["revision"] + 1)
        self.assertIsNone(resumed["pending_human_action"])
        # The same open operation, launched again from a restarted count.
        relaunched = ops[op["operation_id"]]
        self.assertEqual(list(ops), [op["operation_id"]])
        self.assertEqual((relaunched["status"], recovery.launch_attempts(relaunched)), ("planned", 1))
        self.assertEqual(result.report["provision_requests"][0]["attempt"], 1)
        self.assertEqual(resumed["active_operation_id"], op["operation_id"])
        # The trusted workflow accepts the operation of the earlier revision
        # that the record names.
        provisioning.validate_request(resumed, relaunched, POLICY)
        receipt, api = self.receipt_for(resumed, relaunched)
        folded = self.fold(resumed, relaunched, receipt, api)
        [confirmed] = [r for k, r in folded.staged if k == "operations"]
        self.assertEqual(confirmed["status"], "confirmed")
        final = [r for k, r in folded.staged if k == "contributions"][-1]
        self.assertEqual([r["kind"] for k, r in folded.staged if k == "events"][0],
                         "confirm_provisioning")
        self.assertIn(engine.state_key(final), ("development.fix_needed",
                                                "development.agent_running"))

    def test_a_definite_failure_receipt_fails_the_operation_and_waits(self):
        rec, op, receipt, api = self.receipt_world(provisioning_record())
        result = self.fold(rec, op, dict(receipt, status="failed"), api)
        # A receipt that names no closed failure code is stored as `other`.
        self.assertEqual(result.report["provisions"],
                         [{"contribution": rec["contribution_id"], "status": "failed",
                           "failure": "other", "detail": ""}])
        [failed] = [r for k, r in result.staged if k == "operations"]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["external_ids"], {"launch_attempts": 1, "provision_run_id": "77",
                                                  "provision_artifact_id": 88,
                                                  "provision_failure": "other"})
        [event] = [r for k, r in result.staged if k == "events"]
        self.assertEqual((event["kind"], event["cause"]["type"]),
                         ("require_provisioning_intervention", "operation"))
        waiting = [r for k, r in result.staged if k == "contributions"][-1]
        self.assertEqual(engine.state_key(waiting), "provisioning.waiting_human")
        self.assertEqual((waiting["pending_human_action"]["kind"],
                          waiting["pending_human_action"]["counter"]),
                         (controller.PROVISIONING_FAILED, None))
        self.assertIsNone(waiting["active_operation_id"])
        ops = {failed["operation_id"]: failed}
        # Quiescent: park is admitted.
        out = apply_command(waiting, commands.parse('/upstreaming park reason="x"'), operations=ops,
                            policy_=POLICY)
        self.assertTrue(out.accepted, out.reason)
        self.assertEqual(engine.state_key(out.record), "parked.idle")
        # The resume plans a fresh operation with a new id and launches it.
        receipt, resumed, ops, result = command_tick(
            waiting, ops, T, f'/upstreaming resume revision={waiting["revision"]} reason="x"')
        self.assertEqual(receipt["result"], "accepted", receipt)
        self.assertEqual(resumed["revision"], waiting["revision"] + 1)
        self.assertEqual(engine.state_key(resumed), "provisioning.idle")
        [fresh] = [o for o in ops.values() if o["operation_id"] != failed["operation_id"]]
        self.assertEqual((fresh["status"], fresh["revision"], recovery.launch_attempts(fresh)),
                         ("planned", resumed["revision"], 1))
        self.assertEqual(ops[failed["operation_id"]], failed)
        self.assertEqual(resumed["active_operation_id"], fresh["operation_id"])

    def waits(self):
        """Every wait of provisioning the pipeline reaches, with its store."""
        rec, op, at = exhausted_wait()
        yield "launches exhausted", rec, {op["operation_id"]: op}, True
        for status in ("provisioned", "failed"):
            receipt, api = self.receipt_for(rec, op, status)
            folded = self.fold(rec, op, receipt, api)
            [closed] = [r for k, r in folded.staged if k == "operations"]
            later = [r for k, r in folded.staged if k == "contributions"][-1]
            self.assertEqual(engine.state_key(later), "provisioning.waiting_human")
            yield f"launches exhausted, then {closed['status']}", later, \
                {closed["operation_id"]: closed}, False
        first, op1, receipt, api = self.receipt_world(provisioning_record())
        folded = self.fold(first, op1, dict(receipt, status="failed"), api)
        [failed] = [r for k, r in folded.staged if k == "operations"]
        yield "operation failed", [r for k, r in folded.staged if k == "contributions"][-1], \
            {failed["operation_id"]: failed}, False

    def test_every_provisioning_wait_has_a_human_exit(self):
        for name, rec, ops, operation_open in self.waits():
            with self.subTest(wait=name):
                self.assertEqual(engine.state_key(rec), "provisioning.waiting_human")
                resume = f'/upstreaming resume revision={rec["revision"]} reason="x"'
                receipt, resumed, after, result = command_tick(rec, ops, T + timedelta(days=1),
                                                               resume)
                self.assertEqual(receipt["result"], "accepted", receipt)
                self.assertEqual(engine.state_key(resumed), "provisioning.idle")
                self.assertEqual(len(result.report["provision_requests"]), 1)
                launched = after[result.report["provision_requests"][0]["operation_id"]]
                self.assertEqual((launched["status"], recovery.launch_attempts(launched)),
                                 ("planned", 1))
                self.assertEqual(launched["operation_id"] in ops, operation_open)
                # Paused in the wait: one resume clears the flag, the next one
                # leaves the wait.
                paused = apply_command(rec, commands.parse('/upstreaming pause reason="x"'),
                                       operations=ops, policy_=POLICY).record
                first = apply_command(paused, commands.parse(resume), operations=ops,
                                      policy_=POLICY)
                self.assertEqual((first.accepted, first.verb), (True, "resume_pause"))
                second = apply_command(first.record, commands.parse(
                    f'/upstreaming resume revision={first.record["revision"]} reason="x"'),
                    operations=ops, policy_=POLICY)
                self.assertEqual((second.accepted, second.verb), (True, "resume_provisioning"))
                park = apply_command(rec, commands.parse('/upstreaming park reason="x"'),
                                     operations=ops, policy_=POLICY)
                stale = apply_command(rec, commands.parse(resume), operations=ops, policy_=POLICY,
                                      now=(T + timedelta(days=30)).isoformat())
                if operation_open:
                    self.assertFalse(park.accepted)
                    self.assertFalse(stale.accepted)
                else:
                    self.assertTrue(park.accepted, park.reason)
                    self.assertEqual((stale.accepted, stale.verb), (True, "resume_stale"))


class TestOperationIdentity(unittest.TestCase):
    """An operation id is never reused once the operation closed."""

    def bootstrap_world(self, *prior_status):
        rec = contribution(activity="waiting_ci", proposal_artifact={
            "work_id": "w", "claim_id": "c", "artifact_id": 1, "name": "n", "digest": "sha256:" + "d" * 64,
            "run_id": "5", "result_commit": "e" * 40, "outcome": "published"})
        created = operation("confirmed", kind="dev_pr_create")
        created["target"]["expected_head"] = SHA_A
        ops = {created["operation_id"]: created}
        for status in prior_status:
            result = tick(rec, ops)
            [planned] = staged(result, "operations")
            self.assertEqual((planned["kind"], planned["operation_id"] in ops),
                             ("dev_pr_update", False))
            closed = dict(planned, status=status)
            ops[closed["operation_id"]] = closed
        return rec, ops

    def test_a_ci_bootstrap_replan_after_a_closed_operation_is_a_new_operation(self):
        rec, ops = self.bootstrap_world("failed", "not_launched")
        result = tick(rec, ops)
        [fresh] = staged(result, "operations")
        self.assertNotIn(fresh["operation_id"], ops)
        self.assertEqual((fresh["kind"], fresh["status"]), ("dev_pr_update", "planned"))
        self.assertIn("replan:2", fresh["reason_ids"])
        for op in staged(result, "operations"):
            self.assertIsNone(recovery.transition_problem(ops.get(op["operation_id"]), op))
        # The first plan keeps the identity it always had.
        first, _ops = self.bootstrap_world()
        [original] = staged(tick(first, _ops), "operations")
        self.assertEqual(original["reason_ids"], [f"ci-bootstrap-event-v2:{SHA_A}"])


class TestBranchDiscovery(unittest.TestCase):
    def observe(self, op, head):
        from unittest import mock

        import observe_ci
        rec = contribution(activity="waiting_ci", publication_operation_id=op["operation_id"])
        operations = {op["operation_id"]: op}
        with mock.patch("observe_ci.collect.branch_head", return_value={"head": head}), \
                mock.patch("observe_ci.collect.development_review_feedback", return_value=None), \
                mock.patch("observe_ci.collect.ci_snapshot", side_effect=collect.CollectError("x")):
            try:
                observe_ci.observe(object(), contributions={"c": rec}, operations=operations,
                                   contracts={}, policy=POLICY, now=NOW)
            except Exception:
                pass
        return operations[op["operation_id"]]

    def test_uncertain_and_applying_branch_operations_are_discovered(self):
        for status in ("uncertain", "applying"):
            with self.subTest(status=status):
                found = self.observe(operation(status), "b" * 40)
                self.assertEqual(found["status"], "confirmed")
                self.assertEqual([d["result"] for d in found["discovery"]], ["confirmed"])
                absent = self.observe(operation(status), SHA_A)
                self.assertEqual((absent["status"], absent["discovery"][-1]["result"]),
                                 ("uncertain", "absent"))
        self.assertEqual(self.observe(operation("planned"), "b" * 40)["status"], "planned")


class TestFiresBindings(unittest.TestCase):
    def test_each_binding_names_operation_rows_of_its_state(self):
        for (state, kind), fires in recovery.FIRES.items():
            self.assertIn(kind, json.loads(
                (ROOT / "schemas/operation.schema.json").read_text())["properties"]["kind"]["enum"])
            for status, verbs in fires.items():
                self.assertIn(status, ("confirmed", "failed"))
                for verb in verbs:
                    edge = lc.find_edge(state, verb)
                    self.assertEqual(edge.transition.trigger, lc.Trigger.OPERATION)
                    self.assertIn(verb, engine.PIPELINE_VERBS)

    def test_the_development_pr_operation_fires_only_its_confirmation(self):
        for kind in ("dev_pr_create", "dev_pr_update"):
            self.assertEqual(recovery.FIRES[("development.waiting_ci", kind)],
                             {"confirmed": ("confirm_development_pr",)})
            self.assertNotIn(("dev_pr_create", "confirmed"), controller.RESOLUTION_FIRES)


# --------------------------------------------------------------------------
# The fence of an open operation and resolve-operation


class TestOpenOperationFence(unittest.TestCase):
    def test_park_refresh_and_stale_resume_name_the_open_operation(self):
        for status in sorted(recovery.OPEN):
            op = operation(status)
            ops = {op["operation_id"]: op}
            cases = [
                (contribution(activity="waiting_ci"), '/upstreaming park reason="x"'),
                (contribution(activity="waiting_ci"), '/upstreaming refresh revision=0 reason="x"'),
                (contribution(activity="fix_needed", paused=True, paused_at="2026-10-01T00:00:00+00:00",
                              paused_by="human", paused_reason="x"),
                 '/upstreaming resume revision=0 reason="x"'),
            ]
            for rec, text in cases:
                with self.subTest(status=status, state=engine.state_key(rec), command=text):
                    out = apply_command(rec, commands.parse(text), operations=ops, policy_=POLICY)
                    self.assertFalse(out.accepted)
                    self.assertIn(op["operation_id"], out.reason)
            closed = dict(op, status="confirmed")
            out = apply_command(contribution(activity="waiting_ci"),
                                commands.parse('/upstreaming park reason="x"'),
                                operations={op["operation_id"]: closed}, policy_=POLICY)
            self.assertTrue(out.accepted, out.reason)

    def test_no_second_writer_while_an_operation_is_open(self):
        rec = contribution(activity="waiting_ci", active_operation_id="op-" + "1" * 24)
        why = lc.publication_check(rec, expected_revision=0, expected_head=SHA_A,
                                   observed_head=SHA_A, ref="riscv64-dev",
                                   repository="riseproject-upstreaming/proj")
        self.assertIn("another writer operation", why)


class TestResolveOperation(unittest.TestCase):
    def comment(self, op, disposition="not-launched", actor=OWNER, cid=7):
        return {"issue_number": 12, "comment": {
            "id": cid, "user": dict(actor), "created_at": NOW,
            "body": f"/upstreaming resolve-operation operation={op['operation_id']} "
                    f"disposition={disposition} evidence=ev-9"}}

    def run_resolve(self, rec, op, **kw):
        result = tick(rec, {op["operation_id"]: op}, comments=[self.comment(op, **kw)],
                      resolution={"ev-9": EVIDENCE})
        return result, staged(result, "commands")[0]

    def test_an_authorized_resolution_closes_the_operation_and_releases_the_record(self):
        rec = contribution(activity="waiting_ci")
        op = operation("uncertain")
        rec["active_operation_id"] = op["operation_id"]
        result, receipt = self.run_resolve(rec, op)
        self.assertEqual(receipt["result"], "accepted", receipt)
        [closed] = staged(result, "operations")
        self.assertEqual((closed["status"], closed["resolution"]["disposition"]),
                         ("not_launched", "not-launched"))
        self.assertIsNone(staged(result, "contributions")[-1]["active_operation_id"])

    def test_an_unauthorized_actor_is_refused_at_intake(self):
        op = operation("uncertain")
        result, receipt = self.run_resolve(contribution(activity="waiting_ci"), op, actor=STRANGER)
        self.assertEqual((receipt["result"], receipt["reason"]), ("rejected", "unauthorized actor"))
        self.assertEqual(staged(result, "operations"), [])

    def test_a_failed_provisioning_operation_fires_the_intervention(self):
        op = operation("planned", kind="fork_provision")
        rec = provisioning_record(active_operation_id=op["operation_id"])
        result, receipt = self.run_resolve(rec, op, disposition="failed")
        self.assertEqual(receipt["result"], "accepted", receipt)
        [event] = staged(result, "events")
        self.assertEqual(event["kind"], "require_provisioning_intervention")
        final = staged(result, "contributions")[-1]
        self.assertEqual(engine.state_key(final), "provisioning.waiting_human")
        self.assertEqual((final["pending_human_action"]["kind"],
                          final["pending_human_action"]["counter"]),
                         ("provisioning_operation_failed", None))
        self.assertIsNone(final["active_operation_id"])

    def test_a_confirmed_resolution_does_not_confirm_provisioning_without_readback(self):
        op = operation("planned", kind="fork_provision")
        rec = provisioning_record(active_operation_id=op["operation_id"])
        result, receipt = self.run_resolve(rec, op, disposition="confirmed")
        self.assertEqual(receipt["result"], "accepted", receipt)
        self.assertNotIn("confirm_provisioning", [e["kind"] for e in staged(result, "events")])
        self.assertEqual(staged(result, "operations")[0]["status"], "confirmed")
        released = staged(result, "contributions")[0]
        self.assertEqual(engine.state_key(released), "provisioning.idle")
        self.assertIsNone(released["active_operation_id"])

    def test_a_resolved_development_pr_operation_only_closes_and_releases(self):
        for disposition, status in (("confirmed", "confirmed"), ("failed", "failed")):
            with self.subTest(disposition=disposition):
                op = operation("planned", kind="dev_pr_create")
                rec = contribution(activity="waiting_ci", active_operation_id=op["operation_id"])
                result, receipt = self.run_resolve(rec, op, disposition=disposition)
                self.assertEqual(receipt["result"], "accepted", receipt)
                self.assertEqual(staged(result, "events"), [])
                self.assertEqual(staged(result, "operations")[0]["status"], status)
                released = staged(result, "contributions")[0]
                self.assertEqual((engine.state_key(released), released["active_operation_id"],
                                  released["fork"]["pr_number"]),
                                 ("development.waiting_ci", None, rec["fork"]["pr_number"]))


if __name__ == "__main__":
    unittest.main()
