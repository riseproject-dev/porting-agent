# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The verified dossier decides through the table's rows of
``investigation.agent_running``: ``admit_development``,
``readmit_development``, ``park_investigation``, ``close_investigation``, or
``reject_dossier`` (``exhaust_investigation_recovery`` once the attempts are
spent). The matrix covers every decision, with and without a recorded
``admission_override``, with and without a recorded refresh."""

from __future__ import annotations

import copy
import fnmatch
import itertools
import unittest
from datetime import datetime

import capacity
import commands
import engine
import provisioning
import reconcile
import schemas
import status_ledger
from tests.helpers import NOW, SHA_A, SHA_C, apply_command, contribution, dossier, policy
from tests.test_provisioning import Api as ProvisioningApi

POLICY = policy(read_only=False)
OVERRIDE = {"class": "optimized_backend", "paths": ["src/*", "setup.py"], "reason": "reviewed"}
# Refreshes: none, one that began with the fork and development PR the record
# still holds, and one whose development PR no longer matches.
FORK = {"repository_id": 2, "name": "riseproject-upstreaming/proj", "head_ref": "riscv64-dev",
        "head_sha": SHA_A, "pr_number": 1}
REFRESHES = {
    "none": (None, None),
    "matching": ({"command_id": "c", "reason": "r", "from_phase": "development",
                  "head_sha": SHA_A, "pr_number": 1}, FORK),
    "stale": ({"command_id": "c", "reason": "r", "from_phase": "development",
               "head_sha": SHA_A, "pr_number": 7}, FORK),
}
# The verified source identity and base the controller binds each generation.
SOURCE = {"repository_id": 1, "base_ref": "main", "base_sha": SHA_C}
RATIONALE = "The dossier explains its decision from the sources it read."


def investigating(*, override=None, refresh="none", attempts=0):
    """A record whose investigation Work runs, as the table leaves it."""
    refresh_value, fork = REFRESHES[refresh]
    record = contribution(phase="investigation", activity="agent_running", revision=4,
                          fork=copy.deepcopy(fork), admission_class=None,
                          refresh=copy.deepcopy(refresh_value),
                          admission_override=copy.deepcopy(override))
    record["active_work"] = {"dedupe_key": "example--proj/12@0.r4:investigation",
                             "task": "investigation", "generation": 0, "revision": 4,
                             "attempt": 1, "requested_at": NOW,
                             "deadline_at": "2026-11-24T09:00:00+00:00",
                             "dispatched_run_id": None, "reason_ids": [], "feedback_evidence_ids": []}
    record["budget"]["investigation_attempts"] = attempts
    schemas.validate("contribution", record)
    return record


def result(record, research):
    out = {"status": "investigation", "worker_run_id": "77", "publication_artifact_id": 5,
           "task": {"contribution_id": record["contribution_id"], "revision": record["revision"],
                    "generation": 0, "dedupe_key": record["active_work"]["dedupe_key"],
                    "task": "investigation"},
           "proposal": {"diagnosis": "complete research"},
           "artifact": {"digest": "sha256:" + "0" * 64}}
    if research is not None:
        out["dossier"] = research
    return out


def fold(record, research):
    return reconcile.apply_publication(record, result(record, research), now=NOW,
                                       policy=POLICY, worker_run_id="77")


def verb_of(staged):
    kinds = [r["kind"] for kind, r in staged if kind == "events"]
    assert len(kinds) == 1, kinds
    return kinds[0]


# The research of every decision, and what an invalid dossier looks like.
DECISIONS = {
    "develop": dossier("develop", rationale=RATIONALE),
    "develop_incomplete_scope": dossier("develop", covered=("a", "docs/index.md"),
                                        rationale=RATIONALE),
    "park": dossier("park", rationale=RATIONALE),
    "close": dossier("close", rationale=RATIONALE),
    "unknown_decision": dossier("maybe", rationale=RATIONALE),
    "no_dossier": None,
}
OVERRIDES = {"none": None, "full": OVERRIDE,
             "class_only": {"class": "optimized_backend", "reason": "r"}}


def expected(decision, override, refresh):
    """The verb the table takes, by the decision, the override and the refresh."""
    readmit = refresh == "matching"
    if decision in ("unknown_decision", "no_dossier"):
        return "reject_dossier"
    if decision == "close":
        return "close_investigation"
    if decision == "park" and override == "none":
        return "park_investigation"
    if decision == "park" and override == "class_only":
        return "reject_dossier"  # no scope paths: the admitted scope is incomplete
    if decision == "develop_incomplete_scope" and not readmit:
        return "reject_dossier"
    return "readmit_development" if readmit else "admit_development"


TARGETS = {"admit_development": "provisioning.idle",
           "readmit_development": "development.fix_needed",
           "park_investigation": "parked.idle",
           "close_investigation": "closed.idle",
           "reject_dossier": "investigation.idle",
           "exhaust_investigation_recovery": "investigation.waiting_human"}


class TestDossierDecisionMatrix(unittest.TestCase):
    def test_every_decision_with_and_without_override_and_refresh(self):
        count = 0
        for (decision, research), (override_name, override), refresh in itertools.product(
                DECISIONS.items(), OVERRIDES.items(), REFRESHES):
            record = investigating(override=override, refresh=refresh)
            verb = expected(decision, override_name, refresh)
            with self.subTest(decision=decision, override=override_name, refresh=refresh):
                new, staged = fold(record, copy.deepcopy(research))
                schemas.validate("contribution", new)
                event = [r for kind, r in staged if kind == "events"][0]
                self.assertEqual(verb_of(staged), verb)
                self.assertEqual(engine.state_key(new), TARGETS[verb])
                self.assertEqual(event["details"]["verb"], verb)
                self.assertIsNone(new["active_work"])
                evidence = [r for kind, r in staged if kind == "evidence"]
                self.assertEqual(len(evidence), 0 if research is None else 1)
                self.check(verb, record, new, event, research, override_name, override)
                count += 1
        self.assertEqual(count, len(DECISIONS) * len(OVERRIDES) * len(REFRESHES))

    def check(self, verb, record, new, event, research, override_name, override):
        decision = (research or {}).get("proposal") or {}
        if verb in ("admit_development", "readmit_development"):
            self.assertEqual(new["revision"], record["revision"] + 1)
            self.assertIsNone(new["admission_override"])
            self.assertIsNone(new["refresh"])
            self.assertEqual(new["dossier_evidence_id"], event["details"]["evidence_id"])
            if decision["decision"] == "park":
                # The override applies against a decision park.
                self.assertEqual((new["admission_class"], new["scope_paths"]),
                                 (override["class"], override["paths"]))
                self.assertEqual(event["details"]["admission_override"], override)
            else:
                self.assertEqual((new["admission_class"], new["scope_paths"]),
                                 (decision["admission_class"], decision["allowed_paths"]))
                if override:
                    self.assertEqual(event["details"]["admission_override_cleared"], override)
            # The coverage of the dossier is recorded for the admitted scope; an
            # override's paths are the human's scope and are not refused for it.
            covered = [p for area in decision["path_coverage"] for p in area["paths"]]
            complete = all(any(fnmatch.fnmatchcase(p, g) for g in new["scope_paths"])
                           for p in covered)
            self.assertEqual(new["scope_coverage_complete"], complete)
            if verb == "readmit_development":
                self.assertEqual(new["fork"], record["fork"])
        elif verb == "park_investigation":
            self.assertEqual(new["revision"], record["revision"])
            self.assertEqual((new["parked_reason"], new["previous_phase"]),
                             (RATIONALE, "investigation"))
            self.assertIsNone(new["pending_publication"])
        elif verb == "close_investigation":
            self.assertEqual((new["terminal_reason"], new["revision"]),
                             ("no_work", record["revision"]))
            self.assertEqual(event["details"]["rationale"], RATIONALE)
            if override:
                # The override is not applied against a decision close.
                self.assertEqual(event["details"]["admission_override_not_applied"], override)
            else:
                self.assertNotIn("admission_override_not_applied", event["details"])
        else:
            self.assertEqual(new["revision"], record["revision"] + 1)
            self.assertEqual(new["budget"]["investigation_attempts"], 1)
            # A rejected dossier changes neither the override nor the refresh.
            self.assertEqual((new["admission_override"], new["refresh"]),
                             (record["admission_override"], record["refresh"]))

    def test_exhausted_attempts_wait_for_a_human(self):
        limit = POLICY["retries"]["investigation_attempts"]
        for decision in ("unknown_decision", "no_dossier", "develop_incomplete_scope"):
            with self.subTest(decision=decision):
                record = investigating(attempts=limit)
                new, staged = fold(record, copy.deepcopy(DECISIONS[decision]))
                self.assertEqual(verb_of(staged), "exhaust_investigation_recovery")
                self.assertEqual(engine.state_key(new), "investigation.waiting_human")
                self.assertEqual(new["pending_human_action"]["counter"],
                                 "investigation_attempts")
                resumed = apply_command(new, commands.parse(
                    f'/upstreaming resume revision={new["revision"]} reason="looked"'))
                self.assertEqual((resumed.verb, engine.state_key(resumed.record),
                                  resumed.record["budget"]["investigation_attempts"]),
                                 ("resume_investigation", "investigation.idle", 0))

    def test_a_result_outside_the_running_investigation_is_never_folded_by_the_dossier(self):
        record = investigating()
        for state in ("investigation.idle", "investigation.waiting_human"):
            phase, activity = state.split(".")
            moved = dict(record, phase=phase, activity=activity)
            with self.subTest(state=state), self.assertRaises(engine.TransitionError):
                reconcile.fold_dossier(moved, result(moved, DECISIONS["develop"]), now=NOW,
                                       policy=POLICY, worker_run_id="77")

    def test_the_ledger_states_what_each_decision_did(self):
        for decision, override in (("park", OVERRIDE), ("close", OVERRIDE), ("park", None),
                                   ("develop", None)):
            record = investigating(override=override)
            new, staged = fold(record, copy.deepcopy(DECISIONS[decision]))
            event = dict([r for kind, r in staged if kind == "events"][0], sequence=5)
            body = status_ledger.render(event, new, "https://github.com/run")
            with self.subTest(decision=decision, override=bool(override)):
                self.assertIn(status_ledger.table_message(event, new), body)
                self.assertIn(f"`{event['kind']}` by the Result of requested Work", body)
                if decision == "park" and override:
                    self.assertIn("Admission override: class", body)
                if decision == "close" and override:
                    self.assertIn("override was not applied", body)


class TestOverrideEndToEnd(unittest.TestCase):
    """A dossier park, `resume class= paths=` from `parked.idle`, a new
    investigation that decides park again, and the admission that the
    recorded override makes: the fork is provisioned with the override's
    class and paths."""

    def tick(self, record, publications=(), comments=(), now="2026-11-23T10:00:00+00:00"):
        snapshot = reconcile.Snapshot({"c": copy.deepcopy(record)}, {}, {}, {}, {})
        out = reconcile.tick(None, snapshot, policy=POLICY, now=datetime.fromisoformat(now),
                             publications=list(publications), comments=list(comments),
                             pressure=capacity.Pressure(ok=True), observe=False,
                             dispatcher_runs=[], queue_works={},
                             upstream_snapshots={"example/proj": SOURCE})
        staged = [r for kind, r in out.staged if kind == "contributions"]
        return (staged[-1] if staged else record), out

    def test_park_override_admit_provision(self):
        record = investigating()
        parked, staged = fold(record, copy.deepcopy(DECISIONS["park"]))
        self.assertEqual(engine.state_key(parked), "parked.idle")
        lines = status_ledger._command_lines(parked)
        self.assertTrue(any(line.startswith("/upstreaming resume revision=4 class=")
                            for line in lines), lines)
        resumed = apply_command(parked, commands.parse(
            '/upstreaming resume revision=4 class=optimized_backend paths="src/*,setup.py" '
            'reason="reviewed"'))
        self.assertEqual((resumed.verb, engine.state_key(resumed.record)),
                         ("restart_generation", "queued.idle"))
        self.assertEqual(resumed.record["admission_override"], OVERRIDE)
        # The new generation's investigation is requested, then answered by a park.
        running, out = self.tick(resumed.record)
        self.assertEqual(engine.state_key(running), "investigation.agent_running")
        task = {"contribution_id": running["contribution_id"], "revision": running["revision"],
                "generation": running["generation"],
                "dedupe_key": running["active_work"]["dedupe_key"], "task": "investigation"}
        publication = dict(result(running, copy.deepcopy(DECISIONS["park"])), task=task,
                           publication_artifact_id=6)
        admitted, out = self.tick(running, [publication])
        kinds = [r["kind"] for kind, r in out.staged if kind == "events"]
        self.assertEqual(kinds[:2], ["admit_development", "start_operation"])
        self.assertEqual(engine.state_key(admitted), "provisioning.idle")
        self.assertEqual((admitted["admission_class"], admitted["scope_paths"],
                          admitted["admission_override"]),
                         ("optimized_backend", ["src/*", "setup.py"], None))
        self.assertEqual(len(out.report["provision_requests"]), 1)
        self.assertEqual(out.report["requested_effects"], [])
        operation = [r for kind, r in out.staged if kind == "operations"][-1]
        self.provision(admitted, operation)

    def provision(self, admitted, operation):
        """The trusted workflow's receipt, read back exactly, fires
        `confirm_provisioning`; then patch Work on the fork is due."""
        api = ProvisioningApi(exists=False)
        plan = provisioning.create_or_reuse(
            api, api, provisioning.preflight(api, admitted, operation, POLICY))
        receipt = dict(provisioning.initialize(api, plan), provision_run_id="91")
        operations = {operation["operation_id"]: operation}
        # A readback that does not match leaves everything as it was.
        moved = ProvisioningApi(exists=True, head=SHA_A)
        out = reconcile.tick(moved, reconcile.Snapshot({"c": copy.deepcopy(admitted)},
                                                       dict(operations), {}, {}, {}),
                             policy=POLICY, now=datetime.fromisoformat("2026-11-23T10:10:00+00:00"),
                             publications=[], pressure=capacity.Pressure(ok=False, reason="x"),
                             observe=False, provisions=[receipt], dispatcher_runs=[],
                             queue_works={})
        self.assertEqual(out.report["provisions"],
                         [{"contribution": admitted["contribution_id"],
                           "status": "fork identity/ref mismatch"}])
        self.assertFalse([r for kind, r in out.staged if kind in ("operations", "contributions")])
        out = reconcile.tick(api, reconcile.Snapshot({"c": copy.deepcopy(admitted)},
                                                     dict(operations), {}, {}, {}),
                             policy=POLICY, now=datetime.fromisoformat("2026-11-23T10:10:00+00:00"),
                             publications=[], pressure=capacity.Pressure(ok=True),
                             observe=False, provisions=[receipt], dispatcher_runs=[],
                             queue_works={})
        kinds = [r["kind"] for kind, r in out.staged if kind == "events"]
        self.assertEqual(kinds[:2], ["confirm_provisioning", "request_patch"])
        confirmed = [r for kind, r in out.staged if kind == "operations"][0]
        self.assertEqual(confirmed["status"], "confirmed")
        final = [r for kind, r in out.staged if kind == "contributions"][-1]
        self.assertEqual(engine.state_key(final), "development.agent_running")
        self.assertEqual((final["fork"]["name"], final["active_operation_id"]),
                         ("riseproject-upstreaming/proj", None))
        effect = out.report["requested_effects"][0]
        self.assertEqual((effect["task"], effect["effect_contract"]["kind"],
                          effect["expected_head"]),
                         ("development_patch", "fork_branch", SHA_C))


if __name__ == "__main__":
    unittest.main()
