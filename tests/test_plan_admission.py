# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The verified plan decides through the table's rows of
``planning.agent_running``: ``admit_development``,
``readmit_development``, ``park_planning``, ``close_planning``, or
``reject_plan`` (``exhaust_planning_recovery`` once the attempts are
spent). The matrix covers every decision, with and without a recorded
``admission_override``, with and without a recorded refresh."""

from __future__ import annotations

import copy
import fnmatch
import itertools
import unittest
from datetime import datetime

import capacity
import collect
import commands
import engine
import provisioning
import reconcile
import schemas
import status_ledger
from tests.helpers import NOW, SHA_A, SHA_C, apply_command, contribution, plan, policy
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
RATIONALE = "The plan explains its decision from the sources it read."


def planning(*, override=None, refresh="none", attempts=0):
    """A record whose planning Work runs, as the table leaves it."""
    refresh_value, fork = REFRESHES[refresh]
    record = contribution(phase="planning", activity="agent_running", revision=4,
                          fork=copy.deepcopy(fork), admission_class=None,
                          refresh=copy.deepcopy(refresh_value),
                          admission_override=copy.deepcopy(override))
    record["active_work"] = {"dedupe_key": "example--proj/12@0.r4:planning",
                             "task": "planning", "generation": 0, "revision": 4,
                             "attempt": 1, "requested_at": NOW,
                             "deadline_at": "2026-11-24T09:00:00+00:00",
                             "dispatched_run_id": None, "reason_ids": [], "feedback_evidence_ids": []}
    record["budget"]["planning_attempts"] = attempts
    schemas.validate("contribution", record)
    return record


def result(record, research):
    out = {"status": "planning", "worker_run_id": "77", "publication_artifact_id": 5,
           "task": {"contribution_id": record["contribution_id"], "revision": record["revision"],
                    "generation": 0, "dedupe_key": record["active_work"]["dedupe_key"],
                    "task": "planning"},
           "proposal": {"diagnosis": "complete research"},
           "artifact": {"digest": "sha256:" + "0" * 64}}
    if research is not None:
        out["plan"] = research
    return out


def pypi_document(version, *files, source="https://github.com/example/proj"):
    """A PyPI JSON document whose latest release is ``version`` with ``files``
    (a name, or a ``(name, yanked)`` pair), whose metadata names the
    repository ``source``."""
    urls = [{"filename": f, "yanked": False} if isinstance(f, str)
            else {"filename": f[0], "yanked": f[1]} for f in files]
    return {"info": {"version": version, "project_urls": {"Source": source}}, "urls": urls}


RISCV_WHEEL = "proj-2.4.1-cp312-cp312-manylinux_2_39_riscv64.whl"
RISCV = pypi_document("2.4.1", "proj-2.4.1.tar.gz", RISCV_WHEEL,
                      "proj-2.4.1-cp312-cp312-manylinux_2_17_x86_64.whl")
PURE = pypi_document("2.4.1", "proj-2.4.1-py3-none-any.whl", "proj-2.4.1.tar.gz")
X86_ONLY = pypi_document("2.4.1", "proj-2.4.1-cp312-cp312-manylinux_2_17_x86_64.whl")


class Reader:
    """A PyPI reader that serves fixed documents and counts its calls; no
    test reaches the network."""

    def __init__(self, documents=None, error=None):
        self.documents = documents if documents is not None else {"proj": RISCV}
        self.error = error
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        if self.error:
            raise self.error
        return self.documents.get(name)


def fold(record, research, pypi=None):
    return reconcile.apply_publication(record, result(record, research), now=NOW,
                                       policy=POLICY, worker_run_id="77",
                                       pypi=pypi or Reader())


def verb_of(staged):
    kinds = [r["kind"] for kind, r in staged if kind == "events"]
    assert len(kinds) == 1, kinds
    return kinds[0]


# The research of every decision, and what an invalid plan looks like.
DECISIONS = {
    "develop": plan("develop", rationale=RATIONALE),
    "develop_incomplete_scope": plan("develop", covered=("a", "docs/index.md"),
                                        rationale=RATIONALE),
    "park": plan("park", rationale=RATIONALE),
    "close": plan("close", rationale=RATIONALE),
    "unknown_decision": plan("maybe", rationale=RATIONALE),
    "no_plan": None,
}
OVERRIDES = {"none": None, "full": OVERRIDE,
             "class_only": {"class": "optimized_backend", "reason": "r"}}


def expected(decision, override, refresh):
    """The verb the table takes, by the decision, the override and the refresh."""
    readmit = refresh == "matching"
    if decision in ("unknown_decision", "no_plan"):
        return "reject_plan"
    if decision == "close":
        return "close_planning"
    if decision == "park" and override == "none":
        return "park_planning"
    if decision == "park" and override == "class_only":
        return "reject_plan"  # no scope paths: the admitted scope is incomplete
    if decision == "develop_incomplete_scope" and not readmit:
        return "reject_plan"
    return "readmit_development" if readmit else "admit_development"


TARGETS = {"admit_development": "provisioning.idle",
           "readmit_development": "development.fix_needed",
           "park_planning": "parked.idle",
           "close_planning": "closed.idle",
           "reject_plan": "planning.idle",
           "exhaust_planning_recovery": "planning.waiting_human"}


class TestPlanDecisionMatrix(unittest.TestCase):
    def test_every_decision_with_and_without_override_and_refresh(self):
        count = 0
        for (decision, research), (override_name, override), refresh in itertools.product(
                DECISIONS.items(), OVERRIDES.items(), REFRESHES):
            record = planning(override=override, refresh=refresh)
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
            self.assertEqual(new["plan_evidence_id"], event["details"]["evidence_id"])
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
            # The coverage of the plan is recorded for the admitted scope; an
            # override's paths are the human's scope and are not refused for it.
            covered = [p for area in decision["path_coverage"] for p in area["paths"]]
            complete = all(any(fnmatch.fnmatchcase(p, g) for g in new["scope_paths"])
                           for p in covered)
            self.assertEqual(new["scope_coverage_complete"], complete)
            if verb == "readmit_development":
                self.assertEqual(new["fork"], record["fork"])
        elif verb == "park_planning":
            self.assertEqual(new["revision"], record["revision"])
            self.assertEqual((new["parked_reason"], new["previous_phase"]),
                             (RATIONALE, "planning"))
            self.assertIsNone(new["pending_publication"])
        elif verb == "close_planning":
            # The reason is derived from PyPI: the reader serves a riscv64 wheel.
            self.assertEqual((new["terminal_reason"], new["revision"]),
                             ("already_released", record["revision"]))
            self.assertEqual(event["details"]["rationale"], RATIONALE)
            self.assertEqual(event["details"]["pypi"],
                             {"proj": {"version": "2.4.1", "served_by": "riscv64",
                                       "wheel": RISCV_WHEEL}})
            if override:
                # The override is not applied against a decision close.
                self.assertEqual(event["details"]["admission_override_not_applied"], override)
            else:
                self.assertNotIn("admission_override_not_applied", event["details"])
        else:
            self.assertEqual(new["revision"], record["revision"] + 1)
            self.assertEqual(new["budget"]["planning_attempts"], 1)
            # A rejected plan changes neither the override nor the refresh.
            self.assertEqual((new["admission_override"], new["refresh"]),
                             (record["admission_override"], record["refresh"]))

    def test_exhausted_attempts_wait_for_a_human(self):
        limit = POLICY["retries"]["planning_attempts"]
        for decision in ("unknown_decision", "no_plan", "develop_incomplete_scope"):
            with self.subTest(decision=decision):
                record = planning(attempts=limit)
                new, staged = fold(record, copy.deepcopy(DECISIONS[decision]))
                self.assertEqual(verb_of(staged), "exhaust_planning_recovery")
                self.assertEqual(engine.state_key(new), "planning.waiting_human")
                self.assertEqual(new["pending_human_action"]["counter"],
                                 "planning_attempts")
                resumed = apply_command(new, commands.parse(
                    f'/upstreaming resume revision={new["revision"]} reason="looked"'))
                self.assertEqual((resumed.verb, engine.state_key(resumed.record),
                                  resumed.record["budget"]["planning_attempts"]),
                                 ("resume_planning", "planning.idle", 0))

    def test_a_result_outside_the_running_planning_is_never_folded_by_the_plan(self):
        record = planning()
        for state in ("planning.idle", "planning.waiting_human"):
            phase, activity = state.split(".")
            moved = dict(record, phase=phase, activity=activity)
            with self.subTest(state=state), self.assertRaises(engine.TransitionError):
                reconcile.fold_plan(moved, result(moved, DECISIONS["develop"]), now=NOW,
                                       policy=POLICY, worker_run_id="77")

    def test_the_ledger_states_what_each_decision_did(self):
        for decision, override in (("park", OVERRIDE), ("close", OVERRIDE), ("park", None),
                                   ("develop", None)):
            record = planning(override=override)
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


class TestCloseConfirmedFromPyPI(unittest.TestCase):
    """A decision close is taken only when PyPI, read by the reconciler,
    confirms it; the reason is derived, never taken from the plan."""

    def test_pure_python_wheels_only_close_with_no_work(self):
        new, staged = fold(planning(), copy.deepcopy(DECISIONS["close"]),
                           pypi=Reader({"proj": PURE}))
        self.assertEqual(verb_of(staged), "close_planning")
        self.assertEqual((engine.state_key(new), new["terminal_reason"]),
                         ("closed.idle", "no_work"))

    def test_an_unconfirmed_close_is_rejected_within_the_attempts(self):
        for name, documents in (("x86_64 only", {"proj": X86_ONLY}), ("no project", {})):
            with self.subTest(pypi=name):
                record = planning(override=OVERRIDE, refresh="matching")
                new, staged = fold(record, copy.deepcopy(DECISIONS["close"]),
                                   pypi=Reader(documents))
                event = [r for kind, r in staged if kind == "events"][0]
                self.assertEqual(verb_of(staged), "reject_plan")
                self.assertEqual(engine.state_key(new), "planning.idle")
                self.assertEqual(new["revision"], record["revision"] + 1)
                self.assertEqual(new["budget"]["planning_attempts"], 1)
                self.assertIsNone(new["terminal_reason"])
                self.assertEqual(len([r for kind, r in staged if kind == "evidence"]), 1)
                self.assertEqual((new["admission_override"], new["refresh"]),
                                 (record["admission_override"], record["refresh"]))
                self.assertTrue(event["details"]["why"].startswith(
                    "PyPI does not confirm the decision close: "), event["details"]["why"])

    def test_an_unconfirmed_close_with_spent_attempts_waits_for_a_human(self):
        limit = POLICY["retries"]["planning_attempts"]
        new, staged = fold(planning(attempts=limit), copy.deepcopy(DECISIONS["close"]),
                           pypi=Reader({"proj": X86_ONLY}))
        self.assertEqual(verb_of(staged), "exhaust_planning_recovery")
        self.assertEqual(engine.state_key(new), "planning.waiting_human")
        self.assertEqual(new["pending_human_action"]["kind"], "planning_failed")

    def test_the_reader_is_called_for_a_decision_close_only(self):
        for decision in ("develop", "park", "unknown_decision", "no_plan"):
            with self.subTest(decision=decision):
                reader = Reader()
                fold(planning(), copy.deepcopy(DECISIONS[decision]), pypi=reader)
                self.assertEqual(reader.calls, [])

    def test_packages_of_other_registries_never_confirm_a_close(self):
        """The recorded packages may name any registry: an npm or Go module
        name is never read from PyPI, and a same-named PyPI project of
        another repository confirms nothing."""
        for names, documents in (
                (["@example/proj"], {}),
                (["github.com/example/proj"], {}),
                (["proj"], {"proj": pypi_document("2.4.1", "proj-2.4.1-py3-none-any.whl",
                                                  source="https://github.com/someone/proj")}),
                (["proj"], {"proj": dict(PURE, info={"version": "2.4.1"})})):
            with self.subTest(names=names):
                record = planning()
                record["distributions"] = names
                reader = Reader(documents)
                new, staged = fold(record, copy.deepcopy(DECISIONS["close"]), pypi=reader)
                self.assertEqual(verb_of(staged), "reject_plan")
                self.assertIsNone(new["terminal_reason"])
                self.assertEqual(reader.calls, [n for n in names if "/" not in n and "@" not in n])

    def test_a_develop_plan_of_a_go_project_is_admitted(self):
        record = planning()
        record["distributions"] = []
        research = plan("develop", allowed_paths=[".goreleaser.yaml", "internal/arch/*"],
                        admission_class="architecture_correctness",
                        covered=(".goreleaser.yaml", "internal/arch/riscv64.go"))
        research["project"] = {"languages": ["Go"], "build_systems": ["go", "goreleaser"],
                               "build_and_test": "go build ./... and go test ./... in CI.",
                               "release_channels": [], "riscv64_toolchain": "Go supports GOARCH=riscv64."}
        reader = Reader()
        new, staged = fold(record, research, pypi=reader)
        self.assertEqual(verb_of(staged), "admit_development")
        self.assertEqual(engine.state_key(new), "provisioning.idle")
        self.assertEqual((new["admission_class"], new["scope_paths"]),
                         ("architecture_correctness", [".goreleaser.yaml", "internal/arch/*"]))
        self.assertEqual(reader.calls, [])

    def test_no_recorded_distribution_rejects_without_a_read(self):
        record = planning()
        record["distributions"] = []
        reader = Reader()
        new, staged = fold(record, copy.deepcopy(DECISIONS["close"]), pypi=reader)
        self.assertEqual(verb_of(staged), "reject_plan")
        self.assertEqual(reader.calls, [])

    def test_a_failed_read_skips_the_tick_and_the_same_result_closes_later(self):
        record = planning()
        with self.assertRaises(collect.CollectError):
            fold(record, copy.deepcopy(DECISIONS["close"]),
                 pypi=Reader(error=collect.CollectError("PyPI proj: HTTP 503")))
        publication = result(record, copy.deepcopy(DECISIONS["close"]))

        def tick(reader):
            snapshot = reconcile.Snapshot({"c": copy.deepcopy(record)}, {}, {}, {}, {})
            return reconcile.tick(None, snapshot, policy=POLICY,
                                  now=datetime.fromisoformat("2026-11-23T10:00:00+00:00"),
                                  publications=[copy.deepcopy(publication)],
                                  pressure=capacity.Pressure(ok=False, reason="x"),
                                  observe=False, dispatcher_runs=[], queue_works={},
                                  pypi=reader)

        failed = tick(Reader(error=collect.CollectError("PyPI proj: HTTP 503")))
        self.assertEqual(failed.staged, [])
        self.assertEqual([entry["contribution"] for entry in
                          failed.report["skipped_contributions"]],
                         [record["contribution_id"]])
        closed = tick(Reader())
        records = [r for kind, r in closed.staged if kind == "contributions"]
        self.assertEqual((engine.state_key(records[-1]), records[-1]["terminal_reason"]),
                         ("closed.idle", "already_released"))
        self.assertIn(5, records[-1]["processed_publications"])


class TestOverrideEndToEnd(unittest.TestCase):
    """A plan park, `refresh class= paths=` from `parked.idle`, a new
    planning that decides park again, and the admission that the
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
        record = planning()
        parked, staged = fold(record, copy.deepcopy(DECISIONS["park"]))
        self.assertEqual(engine.state_key(parked), "parked.idle")
        lines = status_ledger._command_lines(parked)
        self.assertTrue(any(line.startswith("/upstreaming refresh revision=4 class=")
                            for line in lines), lines)
        self.assertFalse(any(line.startswith("/upstreaming resume") for line in lines), lines)
        resumed = apply_command(parked, commands.parse(
            '/upstreaming refresh revision=4 class=optimized_backend paths="src/*,setup.py" '
            'reason="reviewed"'))
        self.assertEqual((resumed.verb, engine.state_key(resumed.record)),
                         ("restart_generation", "queued.idle"))
        self.assertEqual(resumed.record["admission_override"], OVERRIDE)
        # The new generation's planning is requested, then answered by a park.
        running, out = self.tick(resumed.record)
        self.assertEqual(engine.state_key(running), "planning.agent_running")
        task = {"contribution_id": running["contribution_id"], "revision": running["revision"],
                "generation": running["generation"],
                "dedupe_key": running["active_work"]["dedupe_key"], "task": "planning"}
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
