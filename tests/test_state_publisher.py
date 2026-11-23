# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Section 21: state publisher final-tree checks, hostile publisher inputs."""

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import controller
import publisher
import reconcile
import state
from common import canonical_json
from tests.helpers import commit_files, contribution, git, init_repo, policy


class TestStateDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = init_repo(Path(self.tmp.name) / "control")
        self.base = commit_files(self.repo, {"README.md": "x\n", ".github/workflows/a.yml": "on: push\n"})
        self.p = policy()
        self.rec = contribution()
        self.path = state.record_path("contributions", self.rec)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, cand):
        return state.validate_state_diff(self.repo, self.base, cand, self.p)

    def observe_migration(self, *, expected_sha: str | None = None):
        dash = chr(0x2014)
        evidence = {
            "schema_version": 1,
            "evidence_id": "legacy-observe-rehearsal",
            "contribution_id": self.rec["contribution_id"],
            "kind": "rehearsal",
            "policy_version": "test",
            "collected_at": self.rec["created_at"],
            "summary": f"prepared{dash}single commit",
        }
        path = state.record_path("evidence", evidence)
        data = canonical_json(evidence).encode()
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        migration = state.LegacyTextMigration(
            path=path,
            sha256=expected_sha or hashlib.sha256(data).hexdigest(),
            contribution_id=self.rec["contribution_id"],
            migration_id="test-observe-u2014-migration",
        )
        return migration, path, data

    def run_observe(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        args = SimpleNamespace(root=str(self.repo), offline=False)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = controller.cmd_observe(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_valid_state_commit(self):
        c = commit_files(self.repo, {self.path: canonical_json(self.rec)})
        rep = self.check(c)
        self.assertTrue(rep.ok, rep.problems)

    def test_rejects_non_state_and_mixed(self):
        c = commit_files(self.repo, {self.path: canonical_json(self.rec),
                                     ".github/workflows/a.yml": "on: [push, schedule]\n"})
        rep = self.check(c)
        self.assertIn(".github/workflows/a.yml: outside .state/", rep.problems)

    def test_rejects_invalid_schema_and_identity_mismatch(self):
        bad = dict(self.rec, phase="shipping")
        c = commit_files(self.repo, {self.path: canonical_json(bad)})
        self.assertFalse(self.check(c).ok)
        git(self.repo, "reset", "-q", "--hard", self.base)
        c = commit_files(self.repo, {".state/contributions/other.json": canonical_json(self.rec)})
        self.assertTrue(any("identity" in p for p in self.check(c).problems))

    def test_rejects_symlink_mode_and_secret(self):
        link = self.repo / ".state/contributions/link.json"
        link.parent.mkdir(parents=True)
        link.symlink_to("../../README.md")
        git(self.repo, "add", str(link.relative_to(self.repo)))
        git(self.repo, "commit", "-q", "-m", "l")
        self.assertTrue(any("mode 120000" in p for p in self.check(git(self.repo, "rev-parse", "HEAD")).problems))
        git(self.repo, "reset", "-q", "--hard", self.base)
        c = commit_files(self.repo, {self.path: canonical_json(self.rec)}, mode=0o755)
        self.assertTrue(any("mode 100755" in p for p in self.check(c).problems))
        git(self.repo, "reset", "-q", "--hard", self.base)
        leaked = dict(self.rec, blocked_reason="token ghp_" + "A" * 36)
        c = commit_files(self.repo, {self.path: canonical_json(leaked)})
        self.assertTrue(any("credential" in p for p in self.check(c).problems))

    def test_rejects_delete_rename_escape_and_stale_base(self):
        c1 = commit_files(self.repo, {self.path: canonical_json(self.rec)})
        base2 = c1
        (self.repo / "docs").mkdir()
        git(self.repo, "mv", self.path, "docs/escaped.json")
        git(self.repo, "commit", "-q", "-m", "mv")
        rep = state.validate_state_diff(self.repo, base2, git(self.repo, "rev-parse", "HEAD"), self.p)
        self.assertTrue(any("deletion" in p for p in rep.problems))
        self.assertTrue(any("docs/escaped.json: outside" in p for p in rep.problems))
        # Candidate not directly on top of the trusted base (concurrent main commit).
        rep = state.validate_state_diff(self.repo, self.base, git(self.repo, "rev-parse", "HEAD"), self.p)
        self.assertTrue(any("single commit" in p for p in rep.problems))

    def test_committed_events_are_append_only(self):
        event = {
            "schema_version": 1,
            "contribution_id": self.rec["contribution_id"],
            "sequence": 0,
            "at": self.rec["created_at"],
            "kind": "created",
            "from": {},
            "to": {},
            "cause": {"type": "observation", "id": None},
            "revision_bumped": False,
        }
        path = state.record_path("events", event)
        first = commit_files(self.repo, {path: canonical_json(event)})
        changed = dict(event, kind="rewritten")
        second = commit_files(self.repo, {path: canonical_json(changed)})
        report = state.validate_state_diff(self.repo, first, second, self.p)
        self.assertIn(f"{path}: committed events are append-only", report.problems)

    def test_case_collision(self):
        other = dict(self.rec, contribution_id="Example--proj/12")
        c = commit_files(self.repo, {self.path: canonical_json(self.rec),
                                     state.record_path("contributions", other): canonical_json(other)})
        self.assertTrue(any("collides" in p for p in self.check(c).problems))

    def test_store_rejects_symlinked_roots_and_case_collisions(self):
        outside = self.repo.parent / "outside"
        outside.mkdir()
        state_root = self.repo / ".state"
        state_root.mkdir()
        (state_root / "contributions").symlink_to(outside, target_is_directory=True)
        store = state.StateStore(self.repo, self.p)
        with self.assertRaises(state.StateError):
            store.load_all("contributions")
        (state_root / "contributions").unlink()
        directory = state_root / "contributions"
        directory.mkdir()
        first = self.rec
        second = dict(self.rec, contribution_id=self.rec["contribution_id"].upper())
        (directory / "example--proj--12.json").write_text(canonical_json(first))
        (directory / "EXAMPLE--PROJ--12.json").write_text(canonical_json(second))
        with self.assertRaises(state.StateError):
            store.load_all("contributions")

    def test_store_normalizes_all_untrusted_state_text_paths(self):
        dash = chr(0x2014)
        st = state.StateStore(self.repo, self.p)
        evidence_kinds = ("dossier", "rehearsal", "ci", "human_relay")
        paths = []
        for index, evidence_kind in enumerate(evidence_kinds):
            evidence = {
                "schema_version": 1,
                "evidence_id": f"evidence-{index}",
                "contribution_id": self.rec["contribution_id"],
                "kind": evidence_kind,
                "policy_version": "test",
                "collected_at": self.rec["created_at"],
                "summary": f"model{dash}summary",
                "limitations": [f"limit{dash}one"],
                "data": {"proposal": {"diagnosis": f"proposal{dash}text"}},
            }
            paths.append(st.stage("evidence", evidence))
            self.assertIn(dash, evidence["summary"], "staging must not mutate its input")
        event_path = st.stage("events", {
            "schema_version": 1,
            "contribution_id": self.rec["contribution_id"],
            "sequence": 0,
            "at": self.rec["created_at"],
            "kind": "receipt",
            "from": {"detail": f"before{dash}receipt"},
            "to": {},
            "cause": {"type": "observation", "id": f"receipt{dash}1"},
            "revision_bumped": False,
        })
        operation_path = st.stage("operations", {
            "schema_version": 1,
            "operation_id": "operation-1",
            "contribution_id": self.rec["contribution_id"],
            "revision": self.rec["revision"],
            "kind": "notification",
            "policy_version": "test",
            "target": {"repository": "example/proj"},
            "proposed": {"marker": f"proposal{dash}receipt"},
            "status": "confirmed",
            "discovery": [{"at": self.rec["created_at"], "result": "confirmed",
                           "detail": f"receipt{dash}accepted"}],
            "created_at": self.rec["created_at"],
            "updated_at": self.rec["created_at"],
        })
        st.write_staged()
        for path in paths + [event_path, operation_path]:
            text = (self.repo / path).read_text()
            self.assertNotIn(dash, text)
            self.assertIn("--", text)

    def test_final_tree_rejects_forbidden_text_even_when_json_escapes_it(self):
        dash = chr(0x2014)
        bad = dict(self.rec, blocked_reason=f"model{dash}text")
        encoded = json.dumps(bad, ensure_ascii=True) + "\n"
        candidate = commit_files(self.repo, {self.path: encoded})
        problems = self.check(candidate).problems
        self.assertTrue(any("U+2014" in problem for problem in problems), problems)

    def test_reviewed_legacy_text_migration_is_exact_and_evented(self):
        dash = chr(0x2014)
        evidence = {
            "schema_version": 1,
            "evidence_id": "legacy-rehearsal",
            "contribution_id": self.rec["contribution_id"],
            "kind": "rehearsal",
            "policy_version": "test",
            "collected_at": self.rec["created_at"],
            "summary": f"prepared{dash}single commit",
            "limitations": [],
            "data": {},
        }
        path = state.record_path("evidence", evidence)
        data = canonical_json(evidence).encode()
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        unrelated = self.repo / ".state/projects/unrelated.json"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_text("unchanged\n")
        migration = state.LegacyTextMigration(
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            contribution_id=self.rec["contribution_id"],
            migration_id="test-u2014-migration",
        )
        with patch.object(state, "LEGACY_TEXT_MIGRATIONS", (migration,)):
            written = state.apply_pending_text_migration(
                self.repo, self.p, now=self.rec["created_at"]
            )
        self.assertEqual(len(written), 2)
        self.assertIn(path, written)
        self.assertEqual(unrelated.read_text(), "unchanged\n")
        self.assertNotIn(dash, target.read_text())
        events = state.StateStore(self.repo, self.p).load_all("events")
        event = next(iter(events.values()))
        self.assertEqual(event["kind"], "state_text_normalized")
        self.assertEqual(event["cause"], {"type": "migration", "id": migration.migration_id})

    def test_live_observe_reports_exact_pending_migration_without_loading_state(self):
        migration, path, original = self.observe_migration()
        with patch.object(state, "LEGACY_TEXT_MIGRATIONS", (migration,)), patch.object(
            state.StateStore,
            "load_all",
            side_effect=AssertionError("pending migration must short-circuit state loading"),
        ):
            result, stdout, stderr = self.run_observe()
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        report = json.loads(stdout)
        self.assertEqual(report["state_text_migration"]["pending"], [path])
        self.assertEqual(report["state_text_migration"]["written"], [])
        self.assertEqual((self.repo / path).read_bytes(), original)
        self.assertFalse((self.repo / ".state/events").exists())

    def test_live_observe_rejects_mismatched_pending_blob(self):
        migration, path, original = self.observe_migration(expected_sha="0" * 64)
        with patch.object(state, "LEGACY_TEXT_MIGRATIONS", (migration,)), patch.object(
            state.StateStore,
            "load_all",
            side_effect=AssertionError("a mismatched migration must not load state"),
        ):
            result, stdout, stderr = self.run_observe()
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn(f"legacy state text migration refused: {path}", stderr)
        self.assertIn("expected", stderr)
        self.assertEqual((self.repo / path).read_bytes(), original)

    def test_live_observe_without_pending_migration_runs_normal_observation(self):
        migration = state.LegacyTextMigration(
            path=".state/evidence/absent.json",
            sha256="0" * 64,
            contribution_id=self.rec["contribution_id"],
            migration_id="test-absent-migration",
        )
        api = object()
        with patch.object(state, "LEGACY_TEXT_MIGRATIONS", (migration,)), \
             patch.object(controller.collect, "Api", return_value=api), \
             patch.object(controller.collect, "usage", side_effect=RuntimeError("offline")), \
             patch.object(controller.collect, "intake_issues", return_value=[]), \
             patch.object(controller.collect, "command_comments", return_value=[]), \
             patch.object(controller.collect, "tracking_issues", return_value={}) as loaded, \
             patch.object(controller.collect, "upstream_snapshots", return_value={}):
            result, stdout, stderr = self.run_observe()
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn("state_text_migration", json.loads(stdout))
        loaded.assert_called_once()

    def test_live_observe_pending_report_is_workflow_compatible(self):
        migration, path, _original = self.observe_migration()
        with patch.object(state, "LEGACY_TEXT_MIGRATIONS", (migration,)):
            result, stdout, stderr = self.run_observe()
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        report = json.loads(stdout)
        self.assertEqual(report["state_text_migration"], {"written": [], "pending": [path]})
        self.assertEqual(report["written"], [])
        self.assertEqual(report["controller"], {})
        for key in (
            "requested_effects",
            "dispatch_relaunch",
            "provision_requests",
            "publication_retries",
            "dev_pr_requests",
        ):
            self.assertEqual(report[key], [], key)

    def test_controller_migration_report_has_no_downstream_effects(self):
        dash = chr(0x2014)
        evidence = {
            "schema_version": 1,
            "evidence_id": "legacy-rehearsal",
            "contribution_id": self.rec["contribution_id"],
            "kind": "rehearsal",
            "policy_version": "test",
            "collected_at": self.rec["created_at"],
            "summary": f"prepared{dash}single commit",
        }
        path = state.record_path("evidence", evidence)
        data = canonical_json(evidence).encode()
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        migration = state.LegacyTextMigration(
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            contribution_id=self.rec["contribution_id"],
            migration_id="test-u2014-migration",
        )
        report_path = self.repo / "report.json"
        args = SimpleNamespace(root=str(self.repo), report=str(report_path))
        with patch.object(state, "LEGACY_TEXT_MIGRATIONS", (migration,)):
            self.assertEqual(reconcile.cmd_reconcile(args), 0)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["state_text_migration"]["written"], report["written"])
        self.assertEqual(len(report["written"]), 2)
        self.assertIn(path, report["written"])
        event_path = next(item for item in report["written"] if item != path)
        self.assertRegex(event_path, r"^\.state/events/.+/[0-9]{8}\.json$")
        self.assertEqual(report["controller"], {})
        for key in (
            "requested_effects",
            "dispatch_relaunch",
            "provision_requests",
            "publication_retries",
            "dev_pr_requests",
        ):
            self.assertEqual(report[key], [], key)

    def test_controller_migration_refuses_unrelated_state_changes(self):
        dash = chr(0x2014)
        evidence = {
            "schema_version": 1,
            "evidence_id": "legacy-rehearsal",
            "contribution_id": self.rec["contribution_id"],
            "kind": "rehearsal",
            "policy_version": "test",
            "collected_at": self.rec["created_at"],
            "summary": f"prepared{dash}single commit",
        }
        path = state.record_path("evidence", evidence)
        data = canonical_json(evidence).encode()
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        unrelated_path = self.repo / self.path
        unrelated_path.parent.mkdir(parents=True, exist_ok=True)
        unrelated_path.write_text(canonical_json(self.rec))
        migration = state.LegacyTextMigration(
            path=path,
            sha256=hashlib.sha256(data).hexdigest(),
            contribution_id=self.rec["contribution_id"],
            migration_id="test-u2014-migration",
        )
        report = self.repo / "report.json"
        args = SimpleNamespace(root=str(self.repo), report=str(report))
        with patch.object(state, "LEGACY_TEXT_MIGRATIONS", (migration,)):
            self.assertEqual(reconcile.cmd_reconcile(args), 1)
        self.assertFalse(report.exists())
        self.assertIn(dash, data.decode())
        self.assertNotIn(dash, target.read_text())

    def test_legacy_text_migration_fails_closed_on_any_blob_change(self):
        dash = chr(0x2014)
        evidence = {
            "schema_version": 1,
            "evidence_id": "legacy-rehearsal",
            "contribution_id": self.rec["contribution_id"],
            "kind": "rehearsal",
            "policy_version": "test",
            "collected_at": self.rec["created_at"],
            "summary": f"unexpected{dash}text",
        }
        path = state.record_path("evidence", evidence)
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(canonical_json(evidence))
        migration = state.LegacyTextMigration(
            path=path,
            sha256="0" * 64,
            contribution_id=self.rec["contribution_id"],
            migration_id="test-u2014-migration",
        )
        with patch.object(state, "LEGACY_TEXT_MIGRATIONS", (migration,)):
            with self.assertRaisesRegex(state.StateError, "expected"):
                state.apply_pending_text_migration(
                    self.repo, self.p, now=self.rec["created_at"]
                )
        self.assertIn(dash, target.read_text())
        self.assertFalse((self.repo / ".state/events").exists())

    def test_store_roundtrip_and_event_sequence(self):
        st = state.StateStore(self.repo, self.p)
        st.stage("contributions", self.rec)
        ev = {"schema_version": 1, "contribution_id": self.rec["contribution_id"], "sequence": 0,
              "at": self.rec["created_at"], "kind": "x", "from": {}, "to": {},
              "cause": {"type": "observation", "id": None}, "revision_bumped": False}
        p1 = st.stage("events", ev)
        p2 = st.stage("events", ev)
        self.assertNotEqual(p1, p2)
        st.write_staged()
        self.assertEqual(len(st.load_all("events")), 2)
        self.assertEqual(st.next_event_sequence(self.rec["contribution_id"]), 2)
        (self.repo / self.path).write_text(json.dumps(dict(self.rec, schema_version=9)))
        with self.assertRaises(state.StateError):
            st.load_all("contributions")


class TestPublisherValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = init_repo(Path(self.tmp.name) / "fork")
        self.base = commit_files(self.repo, {
            ".github/workflows/wheels.yml": "on: [push]\njobs: {}\n",
            "pyproject.toml": "[project]\nname='p'\n",
            "src/a.c": "int a;\n"})
        self.scope = publisher.Scope(allowed_globs=[".github/workflows/*", "pyproject.toml", "src/*"])

    def tearDown(self):
        self.tmp.cleanup()

    def validate(self, cand, expected=None, **kw):
        sc = publisher.Scope(**{**self.scope.__dict__, **kw})
        return publisher.validate_update(self.repo, base=self.base, expected_head=expected or self.base,
                                         candidate=cand, scope=sc)

    def test_ok_append(self):
        c = commit_files(self.repo, {".github/workflows/wheels.yml": "on: [push]\njobs: {riscv: {}}\n"},
                         msg="ci: build riscv64 wheels")
        rep = self.validate(c)
        self.assertTrue(rep.ok, rep.problems)
        self.assertTrue(rep.touches_workflows)

    def test_outside_scope_and_hostile_paths(self):
        c = commit_files(self.repo, {"setup.py": "import os\n"})
        self.assertTrue(any("outside the approved" in p for p in self.validate(c).problems))

    def test_symlink_submodule_exec(self):
        (self.repo / "src/link").symlink_to("/etc/passwd")
        git(self.repo, "add", "src/link")
        git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{self.base},src/sub")
        git(self.repo, "commit", "-q", "-m", "x")
        c = git(self.repo, "rev-parse", "HEAD")
        probs = self.validate(c).problems
        self.assertTrue(any("symlink" in p for p in probs))
        self.assertTrue(any("submodule" in p for p in probs))
        git(self.repo, "reset", "-q", "--hard", self.base)
        c = commit_files(self.repo, {"src/run.sh": "#!/bin/sh\n"}, mode=0o755)
        self.assertTrue(any("executable" in p for p in self.validate(c).problems))

    def test_forbidden_workflow_content(self):
        for content, what in [
            ("on: pull_request_target\n", "pull_request_target"),
            ("env:\n  T: ${{ secrets.PYPI_TOKEN }}\n", "secret"),
            ("run: echo \"${{ github.event.issue.body }}\"\n", "untrusted"),
        ]:
            with self.subTest(what):
                git(self.repo, "reset", "-q", "--hard", self.base)
                c = commit_files(self.repo, {".github/workflows/wheels.yml": "on: [push]\n" + content})
                self.assertTrue(any(what in p for p in self.validate(c).problems), what)

    def test_credentialless_fork_does_not_reject_oidc_or_publish_syntax(self):
        content = ("on: [push]\npermissions:\n  id-token: write\nsteps:\n"
                   "  - uses: pypa/gh-action-pypi-publish@release/v1\n"
                   "  - run: twine upload dist/*\n")
        c = commit_files(self.repo, {".github/workflows/wheels.yml": content})
        problems = self.validate(c).problems
        self.assertFalse(any("OIDC" in p or "publication" in p for p in problems))

    def test_debug_commits_and_upstream_facing(self):
        commit_files(self.repo, {".github/workflows/wheels.yml": "on: [push]\n# fork\n"},
                     msg="{debug} run wheel jobs on the fork")
        c = commit_files(self.repo, {"src/a.c": "int a = 1;\n"}, msg="fix riscv64 build")
        self.assertEqual(len(publisher.debug_commits(self.repo, self.base, c)), 1)
        self.assertTrue(self.validate(c).ok)
        self.assertTrue(any("{debug}" in p for p in self.validate(c, upstream_facing=True).problems))

    def test_history_modes(self):
        h1 = commit_files(self.repo, {"src/a.c": "int a = 1;\n"})
        git(self.repo, "reset", "-q", "--hard", self.base)
        h2 = commit_files(self.repo, {"src/a.c": "int a = 2;\n"})
        self.assertTrue(any("does not descend from the expected head" in p
                            for p in self.validate(h2, expected=h1).problems))
        self.assertTrue(self.validate(h2, expected=h1, history="rewrite").ok)
        git(self.repo, "checkout", "-q", "--orphan", "x")
        orphan = commit_files(self.repo, {"src/a.c": "x\n"})
        self.assertTrue(any("approved upstream base" in p
                            for p in self.validate(orphan, history="rewrite").problems))

    def test_lease(self):
        args = publisher.lease_refspec("1" * 40, "2" * 40)
        self.assertEqual(args[0], "--force-with-lease=refs/heads/riscv64-dev:" + "2" * 40)
        with self.assertRaises(ValueError):
            publisher.lease_refspec("1" * 40, "2" * 40, ref="main")
        with self.assertRaises(ValueError):
            publisher.lease_refspec("1" * 40, "")
        # Only the upstream branch is created, with a lease that it does not
        # exist; the development branch is never created by a publication.
        self.assertEqual(publisher.lease_refspec("1" * 40, None, "riscv64"),
                         ["--force-with-lease=refs/heads/riscv64:", "1" * 40 + ":refs/heads/riscv64"])
        with self.assertRaises(ValueError):
            publisher.lease_refspec("1" * 40, None)

    def test_safe_env_ignores_user_config(self):
        env = publisher.safe_env()
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)


if __name__ == "__main__":
    unittest.main()
