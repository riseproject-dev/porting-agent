# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The provisioning shapes the lifecycle table replaced become its states.

The fixtures under ``tests/fixtures/legacy-provisioning`` are records in the
shapes an earlier controller wrote: the provisioning capacity wait with its
planned fork intent (the canary record of that time), the provisioning wait
without a pending action or with the retired ``provision_fork``,
development without a fork (a fork intent retired while destination writes
were disabled, a patch proposed before the fork), and a proposal held on the
provisioned fork without having been published, in the capacity wait or in
the development wait without a pending action; and records the migration
refuses. The migration is evented, exact-shape, idempotent, and fail-closed,
and runs in its own commit before the ordinary tick.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import compat
import engine
import lifecycle
import schemas
import state as store
from common import canonical_json
from tests.helpers import NOW, commit_files, init_repo, policy

FIXTURES = Path(__file__).parent / "fixtures" / "legacy-provisioning"
PATH = ".state/contributions/closeio--ciso8601--1.json"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


PLANNED = load("operation-planned")
CONFIRMED = load("operation-confirmed")
# fixture -> (state after the migration, revision moves, fields cleared)
MIGRATED = {
    "waiting-capacity": ("provisioning.idle", False, []),
    "waiting-capacity-paused": ("provisioning.idle", False, []),
    "waiting-human-without-pending": ("provisioning.idle", False, []),
    "waiting-human-provision-fork": ("provisioning.idle", False, ["pending_human_action"]),
    "development-without-fork": ("provisioning.idle", True, []),
    "development-pre-fork-proposal": ("provisioning.idle", True, ["proposal_artifact"]),
    "development-held-proposal": ("development.fix_needed", True,
                                  ["proposal_artifact", "pending_publication"]),
    # The live record at e0c4a3d6 (and, one retry later, 08f9bf57): the code
    # before the table moved the held proposal's capacity wait to a wait
    # without a pending action.
    "development-waiting-human-held-proposal": ("development.fix_needed", True,
                                                ["proposal_artifact", "pending_publication"]),
}
# fixture -> the operation the store holds for it
OPERATIONS = {"waiting-capacity": PLANNED, "waiting-capacity-paused": PLANNED,
              "refused-confirmed-operation": CONFIRMED}
REFUSED = ("refused-confirmed-operation", "refused-investigation-waiting-capacity",
           "refused-provisioning-agent-running", "refused-active-work",
           "refused-held-proposal-upstream-review", "refused-pending-publication-of-another-run")


class TestProvisioningMigration(unittest.TestCase):
    def test_each_legacy_shape_becomes_the_state_of_the_table(self):
        for name, (target, bump, cleared) in MIGRATED.items():
            with self.subTest(fixture=name):
                record = load(name)
                schemas.validate("contribution", record)
                self.assertTrue(compat.needs_provisioning_migration(record))
                self.assertTrue(compat.awaits_provisioning_migration(record))
                new, event = compat.migrate_provisioning(record, operation=OPERATIONS.get(name),
                                                         now=NOW)
                self.assertEqual(engine.state_key(new), target)
                self.assertIn(target, lifecycle.LIFECYCLE)
                self.assertEqual(new["revision"], record["revision"] + (1 if bump else 0))
                self.assertIsNone(compat.state_problem(new))
                schemas.validate("contribution", new)
                # Only the state, the revision, and the cleared fields change.
                expected = dict(record, phase=new["phase"], activity=new["activity"],
                                revision=new["revision"], updated_at=NOW,
                                **{field: None for field in cleared})
                self.assertEqual(new, expected)
                for kept in ("active_operation_id", "fork", "dossier_evidence_id",
                             "admission_class", "scope_paths", "paused", "budget",
                             "processed_publications"):
                    self.assertEqual(new[kept], record[kept], kept)
                self.assertEqual((event["kind"], event["cause"], event["revision_bumped"]),
                                 (compat.PROVISIONING_MIGRATED,
                                  {"type": "migration", "id": compat.PROVISIONING_MIGRATION},
                                  bump))
                self.assertEqual(event["details"]["from_state"], engine.state_key(record))
                self.assertEqual(event["details"]["to_state"], target)
                self.assertEqual(event["details"]["changed"], cleared)
                schemas.validate("event", event)
                # Idempotent: the migrated record is not selected again.
                self.assertFalse(compat.needs_provisioning_migration(new))
                with self.assertRaises(lifecycle.TransitionError):
                    compat.migrate_provisioning(new, operation=None, now=NOW)

    def test_other_shapes_are_refused(self):
        for name in REFUSED:
            with self.subTest(fixture=name):
                record = load(name)
                self.assertTrue(compat.needs_provisioning_migration(record))
                with self.assertRaises(lifecycle.TransitionError):
                    compat.migrate_provisioning(record, operation=OPERATIONS.get(name), now=NOW)

    def test_the_held_proposal_wait_with_a_pending_action_or_without_a_fork_is_refused(self):
        record = load("development-waiting-human-held-proposal")
        waiting = dict(record, pending_human_action={
            "kind": "publication_infrastructure_failure", "requested_at": NOW,
            "reminded_at": None, "escalated_at": None, "counter": "publish_recovery"})
        for refused in (waiting, dict(record, fork=None)):
            with self.subTest(refused=refused["fork"] is None):
                self.assertTrue(compat.needs_provisioning_migration(refused))
                with self.assertRaises(lifecycle.TransitionError):
                    compat.migrate_provisioning(refused, operation=None, now=NOW)

    def test_no_legacy_investigation_or_provisioning_state_is_admitted(self):
        self.assertFalse({s for s in compat.LEGACY_STATES
                          if s.split(".")[0] in ("investigation", "provisioning")})
        for name in ("refused-investigation-waiting-capacity",
                     "refused-provisioning-agent-running"):
            self.assertIsNotNone(compat.state_problem(load(name)))

    def test_table_states_and_the_live_record_need_no_migration(self):
        live = json.loads((Path(__file__).parent / "fixtures/live-handoff-r6/contribution.json")
                          .read_text())
        self.assertFalse(compat.needs_provisioning_migration(live))
        for pending in ("provisioning_launch_exhausted", "provisioning_operation_failed"):
            wait = dict(load("waiting-human-provision-fork"),
                        pending_human_action={"kind": pending, "requested_at": NOW,
                                              "reminded_at": None, "escalated_at": None,
                                              "counter": None})
            self.assertFalse(compat.needs_provisioning_migration(wait))
        idle = dict(load("waiting-capacity"), activity="idle")
        self.assertFalse(compat.needs_provisioning_migration(idle))

    def store_with(self, root: Path, name: str) -> Path:
        path = root / PATH
        path.parent.mkdir(parents=True)
        path.write_bytes((FIXTURES / f"{name}.json").read_bytes())
        operation = OPERATIONS.get(name)
        if operation:
            op_path = root / store.record_path("operations", operation)
            op_path.parent.mkdir(parents=True)
            op_path.write_text(canonical_json(operation))
        return path

    def test_the_controller_migrates_in_its_own_commit_and_refuses_unknown_shapes(self):
        p = policy()
        for name, (target, _bump, _cleared) in MIGRATED.items():
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = self.store_with(root, name)
                # The store admits the record only as the migration reads it.
                self.assertEqual(store.check_blob(PATH, path.read_bytes(), p), [])
                self.assertEqual(store.pending_record_migrations(root, p), [PATH])
                written = store.apply_pending_record_migrations(root, p, now=NOW)
                self.assertEqual(sorted(written),
                                 [PATH, ".state/events/closeio--ciso8601--1/00000000.json"])
                migrated = json.loads(path.read_text())
                self.assertEqual(engine.state_key(migrated), target)
                event = json.loads((root / written[1]).read_text())
                self.assertEqual(event["kind"], compat.PROVISIONING_MIGRATED)
                self.assertEqual(store.check_blob(PATH, path.read_bytes(), p), [])
                # A later run writes nothing.
                self.assertEqual(store.pending_record_migrations(root, p), [])
                self.assertEqual(store.apply_pending_record_migrations(root, p, now=NOW), [])
        for name in REFUSED:
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = self.store_with(root, name)
                before = path.read_bytes()
                if name != "refused-confirmed-operation":
                    # The record alone shows it: the store does not admit it.
                    self.assertTrue(store.check_blob(PATH, before, p))
                with self.assertRaises(store.StateError):
                    store.apply_pending_record_migrations(root, p, now=NOW)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((root / ".state/events").exists())

    def test_a_state_diff_never_writes_a_replaced_provisioning_shape(self):
        record = load("development-pre-fork-proposal")
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            base = commit_files(repo, {".state/README.md": "state\n"}, "base")
            head = commit_files(repo, {PATH: canonical_json(record)}, "legacy")
            report = store.validate_state_diff(repo, base, head, policy())
            self.assertFalse(report.ok)
            self.assertIn(f"{PATH}: written in a provisioning shape the lifecycle replaced",
                          report.problems)
            migrated, _event = compat.migrate_provisioning(copy.deepcopy(record), operation=None,
                                                           now=NOW)
            fixed = commit_files(repo, {PATH: canonical_json(migrated)}, "migrated")
            self.assertTrue(store.validate_state_diff(repo, head, fixed, policy()).ok)


if __name__ == "__main__":
    unittest.main()
