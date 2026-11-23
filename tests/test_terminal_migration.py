# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The terminal phases before the lifecycle table become ``closed.idle``.

The fixtures under ``tests/fixtures/legacy-terminal`` are records in the
shapes the earlier controller wrote (``abandoned`` by command and by the
removal of the intake label) and the shapes its schema admitted
(``no_work``, ``deferred``, ``already_released``), and records the migration
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

FIXTURES = Path(__file__).parent / "fixtures" / "legacy-terminal"
PATH = ".state/contributions/example--proj--12.json"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


MIGRATED = {"abandoned-by-command": "abandoned", "abandoned-label-removed": "withdrawn",
            "abandoned-paused": "abandoned", "no-work": "no_work", "deferred": "deferred",
            "already-released": "already_released"}
REFUSED = ("refused-open-operation", "refused-active-work", "refused-blocked", "refused-activity")


class TestTerminalMigration(unittest.TestCase):
    def test_each_legacy_terminal_record_becomes_closed_with_its_reason(self):
        for name, reason in MIGRATED.items():
            with self.subTest(fixture=name):
                record = load(name)
                self.assertTrue(compat.needs_terminal_migration(record))
                self.assertTrue(compat.awaits_terminal_migration(record))
                self.assertTrue(schemas.errors("contribution", record))
                new, event = compat.migrate_terminal(record, now=NOW)
                self.assertEqual(engine.state_key(new), "closed.idle")
                self.assertEqual(new["terminal_reason"], reason)
                self.assertEqual(new["updated_at"], NOW)
                # Nothing else changes.
                self.assertEqual(dict(new, phase=record["phase"], terminal_reason=None,
                                      updated_at=None),
                                 dict(record, terminal_reason=None, updated_at=None))
                schemas.validate("contribution", new)
                self.assertIsNone(compat.state_problem(new))
                self.assertEqual((event["kind"], event["cause"], event["revision_bumped"]),
                                 (compat.TERMINAL_MIGRATED,
                                  {"type": "migration", "id": compat.TERMINAL_MIGRATION}, False))
                self.assertEqual(event["details"], {
                    "from_state": f"{record['phase']}.idle", "to_state": "closed.idle",
                    "terminal_reason": {"from": record["terminal_reason"], "to": reason}})
                schemas.validate("event", event)
                # Idempotent: the migrated record is not selected again.
                self.assertFalse(compat.needs_terminal_migration(new))
                with self.assertRaises(lifecycle.TransitionError):
                    compat.migrate_terminal(new, now=NOW)

    def test_paused_record_stays_paused(self):
        new, _event = compat.migrate_terminal(load("abandoned-paused"), now=NOW)
        self.assertEqual((new["paused"], new["paused_by"], new["paused_reason"]),
                         (True, "human", "hold"))

    def test_other_shapes_are_refused(self):
        for name in REFUSED:
            with self.subTest(fixture=name):
                record = load(name)
                self.assertTrue(compat.needs_terminal_migration(record))
                self.assertFalse(compat.awaits_terminal_migration(record))
                with self.assertRaises(lifecycle.TransitionError):
                    compat.migrate_terminal(record, now=NOW)

    def test_no_legacy_terminal_state_is_admitted_otherwise(self):
        self.assertEqual(lifecycle.TERMINAL, {"released", "closed"})
        for phase in compat.LEGACY_TERMINAL_PHASES:
            self.assertNotIn(phase, lifecycle.PHASES)
            record = dict(load("abandoned-by-command"), phase=phase)
            self.assertIsNotNone(compat.state_problem(record))

    def test_the_controller_migrates_in_its_own_commit_and_refuses_unknown_shapes(self):
        p = policy()
        for name, reason in MIGRATED.items():
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / PATH
                path.parent.mkdir(parents=True)
                path.write_bytes((FIXTURES / f"{name}.json").read_bytes())
                # The store admits the record only as the migration reads it.
                self.assertEqual(store.check_blob(PATH, path.read_bytes(), p), [])
                self.assertEqual(store.pending_record_migrations(root, p), [PATH])
                written = store.apply_pending_record_migrations(root, p, now=NOW)
                self.assertEqual(sorted(written),
                                 [PATH, ".state/events/example--proj--12/00000000.json"])
                migrated = json.loads(path.read_text())
                self.assertEqual((engine.state_key(migrated), migrated["terminal_reason"]),
                                 ("closed.idle", reason))
                event = json.loads((root / written[1]).read_text())
                self.assertEqual(event["kind"], compat.TERMINAL_MIGRATED)
                self.assertEqual(store.check_blob(PATH, path.read_bytes(), p), [])
                # A later run writes nothing.
                self.assertEqual(store.pending_record_migrations(root, p), [])
                self.assertEqual(store.apply_pending_record_migrations(root, p, now=NOW), [])
        for name in REFUSED:
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / PATH
                path.parent.mkdir(parents=True)
                path.write_bytes((FIXTURES / f"{name}.json").read_bytes())
                before = path.read_bytes()
                self.assertTrue(store.check_blob(PATH, before, p))
                with self.assertRaises(store.StateError):
                    store.apply_pending_record_migrations(root, p, now=NOW)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse((root / ".state/events").exists())

    def test_a_state_diff_never_writes_a_legacy_terminal_phase(self):
        record = load("abandoned-by-command")
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            base = commit_files(repo, {".state/README.md": "state\n"}, "base")
            head = commit_files(repo, {PATH: canonical_json(record)}, "legacy")
            report = store.validate_state_diff(repo, base, head, policy())
            self.assertFalse(report.ok)
            self.assertIn(f"{PATH}: written in a terminal phase the lifecycle replaced",
                          report.problems)
            migrated, _event = compat.migrate_terminal(copy.deepcopy(record), now=NOW)
            closed = commit_files(repo, {PATH: canonical_json(migrated)}, "closed")
            self.assertTrue(store.validate_state_diff(repo, head, closed, policy()).ok)


if __name__ == "__main__":
    unittest.main()
