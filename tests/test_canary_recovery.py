# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Failure injection for the one exact canonical canary recovery operation."""

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import canary
import canary_recovery
import capacity
import controller
import reconcile
import state
from tests.helpers import NOW, policy

ROOT = Path(__file__).parents[1]
LIVE_RECORD = json.loads(
    (ROOT / "tests/fixtures/canary-defect-state.json").read_text()
)
LIVE_EVENT = json.loads(
    (ROOT / "tests/fixtures/canary-defect-event.json").read_text()
)
LATER = "2026-11-22T02:00:00+00:00"


def issue(body=canary.INCOMPLETE_BODY):
    return {
        "id": canary.ISSUE_ID,
        "number": canary.ISSUE_NUMBER,
        "title": canary.ISSUE_TITLE,
        "body": body,
        "labels": [{"name": "upstreaming-request"}],
        "user": {"id": 660779, "login": "luhenry"},
    }


class FakeApi:
    def __init__(
        self,
        body=canary.INCOMPLETE_BODY,
        *,
        fail_before_effect=False,
        fail_after_effect=False,
        mutate_readback=False,
    ):
        self.body = body
        self.fail_before_effect = fail_before_effect
        self.fail_after_effect = fail_after_effect
        self.mutate_readback = mutate_readback
        self.calls = []
        self.gets = 0
        self.patches = 0

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        self.assert_path(path)
        if method == "GET":
            self.gets += 1
            value = self.body
            if self.mutate_readback and self.gets >= 2:
                value = "mutated after effect"
            return issue(value)
        if method != "PATCH":
            raise AssertionError(method)
        self.patches += 1
        if self.fail_before_effect:
            raise canary_recovery.RecoveryError("injected failure before effect")
        self.body = body["body"]
        if self.fail_after_effect:
            raise canary_recovery.RecoveryError("injected lost response after effect")
        return issue(self.body)

    def assert_path(self, path):
        if path != canary_recovery.IssueApi.path:
            raise AssertionError(path)


class TestCanaryRecovery(unittest.TestCase):
    def setUp(self):
        self.policy = policy(read_only=False)
        canary.validate_mismatch(LIVE_RECORD, operation_id=None)

    def test_the_investigation_is_recognized_by_either_work_id(self):
        """The defective record names its Work as it was named before the
        Work id carried the generation; the same request named now is the
        same defect, and any other Work id is not."""
        renamed = copy.deepcopy(LIVE_RECORD)
        renamed["active_work"]["dedupe_key"] = "closeio--ciso8601/1@g0.r0:investigation"
        canary.validate_mismatch(renamed, operation_id=None)
        renamed["active_work"]["dedupe_key"] = "closeio--ciso8601/1@g1.r0:investigation"
        with self.assertRaises(canary.CanaryError):
            canary.validate_mismatch(renamed, operation_id=None)

    def prepared(self, directory):
        root = Path(directory)
        operation = canary.operation_for(LIVE_RECORD, policy=self.policy, now=NOW)
        bound = canary.bind_operation(LIVE_RECORD, operation, NOW)
        store = state.StateStore(root, self.policy)
        store.stage("contributions", bound)
        store.stage("operations", operation)
        store.stage("events", LIVE_EVENT)
        store.stage("events", canary.planned_event(bound, operation, NOW))
        store.write_staged()
        return root, operation

    def loaded(self, root):
        store = state.StateStore(root, self.policy)
        contribution = next(iter(store.load_all("contributions").values()))
        operation = next(iter(store.load_all("operations").values()))
        events = list(store.load_all("events").values())
        return contribution, operation, events

    def test_controller_persists_one_intent_before_effect_and_reuses_it(self):
        current = issue()
        first = controller.tick(
            contributions={"c": copy.deepcopy(LIVE_RECORD)},
            operations={},
            consumed={},
            projects={},
            policy=self.policy,
            inputs=controller.Inputs(
                now=datetime.fromisoformat(NOW),
                pressure=capacity.Pressure(ok=False, reason="offline"),
                tracking_issues={1: current},
            ),
        )
        requests = first.report["canary_recovery_requests"]
        self.assertEqual(len(requests), 1)
        operation = next(record for kind, record in first.staged if kind == "operations")
        bound = [record for kind, record in first.staged if kind == "contributions"][-1]
        self.assertEqual(operation["status"], "planned")
        self.assertEqual(bound["active_operation_id"], operation["operation_id"])
        self.assertTrue(any(
            kind == "events" and record["kind"] == "canary:recovery_planned"
            for kind, record in first.staged
        ))

        second = controller.tick(
            contributions={"c": bound},
            operations={operation["operation_id"]: operation},
            consumed={},
            projects={},
            policy=self.policy,
            inputs=controller.Inputs(
                now=datetime.fromisoformat(LATER),
                pressure=capacity.Pressure(ok=False, reason="offline"),
                tracking_issues={1: current},
            ),
        )
        self.assertEqual(second.report["canary_recovery_requests"], requests)
        self.assertFalse(any(kind == "operations" for kind, _record in second.staged))
        self.assertFalse(any(
            kind == "events" and record["kind"] == "canary:recovery_planned"
            for kind, record in second.staged
        ))

    def test_generic_timeout_cannot_hand_unblock_the_exact_defect(self):
        result = reconcile.tick(
            None,
            reconcile.Snapshot({"c": copy.deepcopy(LIVE_RECORD)}, {}, {}, {}, {}),
            policy=self.policy,
            now=datetime.fromisoformat("2026-11-22T08:00:00+00:00"),
            publications=[],
            pressure=capacity.Pressure(ok=False, reason="offline"),
            issues=[],
            comments=[],
            observe=False,
            tracking_issues={1: issue()},
        )
        self.assertEqual(result.report["timed_out_work"], [])
        self.assertEqual(len(result.report["controller"]["canary_recovery_requests"]), 1)
        final = [record for kind, record in result.staged if kind == "contributions"][-1]
        self.assertIsNotNone(final["active_work"])
        self.assertIsNotNone(final["active_operation_id"])

    def test_failure_before_effect_preserves_intent_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root, operation = self.prepared(directory)
            failing = FakeApi(fail_before_effect=True)
            with self.assertRaises(canary_recovery.RecoveryError):
                canary_recovery.apply(
                    root, operation["operation_id"], failing, now=LATER
                )
            contribution, durable, events = self.loaded(root)
            self.assertEqual(durable["status"], "planned")
            self.assertEqual(contribution["tracking_issue"]["description_digest"],
                             canary.INCOMPLETE_DIGEST)
            self.assertEqual(len(events), 2)

            recovered = FakeApi()
            receipt = canary_recovery.apply(
                root, operation["operation_id"], recovered, now=LATER
            )
            self.assertEqual(receipt["status"], "confirmed")
            self.assertEqual(recovered.patches, 1)

    def test_lost_response_after_effect_is_discovered_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root, operation = self.prepared(directory)
            uncertain = FakeApi(fail_after_effect=True)
            with self.assertRaises(canary_recovery.RecoveryError):
                canary_recovery.apply(
                    root, operation["operation_id"], uncertain, now=LATER
                )
            self.assertEqual(uncertain.body, canary.EXPECTED_BODY)
            contribution, durable, _events = self.loaded(root)
            self.assertEqual(durable["status"], "planned")
            self.assertEqual(contribution["active_operation_id"], operation["operation_id"])

            discovered = FakeApi(body=uncertain.body)
            receipt = canary_recovery.apply(
                root, operation["operation_id"], discovered, now=LATER
            )
            self.assertEqual(receipt["effect"], "discovered")
            self.assertEqual(discovered.patches, 0)
            self.assertEqual(discovered.gets, 2)

    def test_stale_or_mutated_body_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root, operation = self.prepared(directory)
            stale = FakeApi(body="some other edit")
            with self.assertRaisesRegex(canary_recovery.RecoveryError, "changed unexpectedly"):
                canary_recovery.apply(root, operation["operation_id"], stale, now=LATER)
            self.assertEqual(stale.patches, 0)

            mutated = FakeApi(mutate_readback=True)
            with self.assertRaisesRegex(canary_recovery.RecoveryError, "exact readback failed"):
                canary_recovery.apply(root, operation["operation_id"], mutated, now=LATER)
            contribution, durable, events = self.loaded(root)
            self.assertEqual(durable["status"], "planned")
            self.assertEqual(contribution["tracking_issue"]["description_digest"],
                             canary.INCOMPLETE_DIGEST)
            self.assertEqual(len(events), 2)

    def test_exact_readback_repairs_state_append_only_and_repeat_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root, operation = self.prepared(directory)
            api = FakeApi()
            receipt = canary_recovery.apply(
                root, operation["operation_id"], api, now=LATER
            )
            self.assertEqual((api.gets, api.patches), (2, 1))
            self.assertEqual(receipt["body_digest"], canary.EXPECTED_DIGEST)
            contribution, confirmed, events = self.loaded(root)
            self.assertTrue(canary.is_repaired(contribution))
            self.assertEqual(confirmed["status"], "confirmed")
            self.assertEqual(confirmed["external_ids"]["body_digest"],
                             canary.EXPECTED_DIGEST)
            self.assertEqual([event["sequence"] for event in events], [0, 1, 2])
            self.assertEqual(events[-1]["kind"], "canary:intake_repaired")

            repeat_api = FakeApi(body=canary.EXPECTED_BODY)
            repeat = canary_recovery.apply(
                root, operation["operation_id"], repeat_api, now=LATER
            )
            self.assertEqual(repeat["status"], "already_confirmed")
            self.assertEqual(repeat_api.calls, [])

            tick = controller.tick(
                contributions={"c": contribution},
                operations={confirmed["operation_id"]: confirmed},
                consumed={},
                projects={},
                policy=self.policy,
                inputs=controller.Inputs(
                    now=datetime.fromisoformat(LATER),
                    pressure=capacity.Pressure(ok=False, reason="offline"),
                    tracking_issues={1: issue(canary.EXPECTED_BODY)},
                ),
            )
            self.assertEqual(tick.report["canary_recovery_requests"], [])


class TestCanaryIssueApiScope(unittest.TestCase):
    def test_client_rejects_every_resource_except_issue_one_get_and_patch(self):
        api = canary_recovery.IssueApi("token")
        for method, path in (
            ("POST", canary_recovery.IssueApi.path),
            ("GET", "/repos/riseproject-dev/porting-agent/issues/2"),
            ("PATCH", "/repos/other/repo/issues/1"),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(
                canary_recovery.RecoveryError
            ):
                api.request(method, path, {})


class TestFreshCanaryIntakeFence(unittest.TestCase):
    def test_incomplete_or_wrong_identity_canary_is_rejected_before_state(self):
        import intake

        incomplete = issue(canary.INCOMPLETE_BODY)
        with self.assertRaisesRegex(intake.IntakeError, "does not match the reviewed fixture"):
            intake.parse_request(incomplete, policy())
        wrong_identity = dict(issue(canary.EXPECTED_BODY), id=123)
        with self.assertRaisesRegex(intake.IntakeError, "identity"):
            intake.parse_request(wrong_identity, policy())


if __name__ == "__main__":
    unittest.main()
