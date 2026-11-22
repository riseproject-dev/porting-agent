# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Regression contract for the exact rendered body of canonical issue #1."""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import capacity
import intake
import reconcile
import state
from tests.helpers import NOW, OWNER, SHA_C, STRANGER, policy

ROOT = Path(__file__).parents[1]
BODY_PATH = ROOT / "tests/fixtures/issue-1-body.md"
REQUEST_FIXTURE = json.loads((ROOT / "tests/fixtures/ciso8601-request.json").read_text())
BODY_SHA256 = "9f58bffa65d7e7df0206ae1130ce6eb5e218181b966839f2e609c05e8baa7ebb"


def canonical_issue() -> dict:
    return {
        "id": 9001,
        "number": 1,
        "title": "[upstreaming] ciso8601 riscv64 wheel support",
        "body": BODY_PATH.read_text(),
        "user": dict(STRANGER),
        "labels": [{"name": "upstreaming-request"}],
    }


class TestCanonicalIssueBody(unittest.TestCase):
    def setUp(self):
        self.policy = policy(read_only=False)

    def test_current_form_fields_target_notes_and_digest_are_exact(self):
        body = BODY_PATH.read_text()
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), BODY_SHA256)
        self.assertEqual(body, REQUEST_FIXTURE["renderedBody"])
        fields = intake.parse_issue_form(body)
        self.assertEqual(
            fields,
            {
                "upstream": "https://github.com/closeio/ciso8601",
                "distributions": "ciso8601 (import: ciso8601)",
                "target_branch": "master",
                "notes": REQUEST_FIXTURE["fields"]["additional-notes"],
            },
        )
        request = intake.parse_request(canonical_issue(), self.policy)
        self.assertEqual(request.upstream, "closeio/ciso8601")
        self.assertEqual(request.distributions, ("ciso8601",))
        self.assertEqual(request.target_branch, "master")
        self.assertEqual(request.notes, REQUEST_FIXTURE["fields"]["additional-notes"])
        self.assertEqual(request.body, body)
        self.assertEqual(request.digest, BODY_SHA256)

    def test_current_and_legacy_headings_cannot_be_mixed(self):
        mixed = BODY_PATH.read_text() + "\n### Source repository\n\nhttps://github.com/x/y\n"
        with self.assertRaisesRegex(intake.IntakeError, "mixes current and legacy"):
            intake.parse_request(dict(canonical_issue(), body=mixed), self.policy)

    def test_target_branch_is_optional_but_fail_closed_when_malformed(self):
        self.assertIsNone(intake.canonical_branch(""))
        self.assertEqual(intake.canonical_branch("release/2.x"), "release/2.x")
        for branch in (
            "../main",
            "refs//main",
            "main.lock",
            "main branch",
            ".hidden",
            "release/.hidden",
        ):
            with self.subTest(branch=branch), self.assertRaises(intake.IntakeError):
                intake.canonical_branch(branch)

    def test_reconciliation_accepts_and_durably_freezes_issue_one(self):
        issue = canonical_issue()
        snapshot = reconcile.Snapshot({}, {}, {}, {}, {})
        result = reconcile.tick(
            None,
            snapshot,
            policy=self.policy,
            now=datetime.fromisoformat(NOW),
            publications=[],
            pressure=capacity.Pressure(ok=True),
            issues=[issue],
            comments=[],
            acceptances={issue["id"]: OWNER["id"]},
            observe=False,
            tracking_issues={issue["number"]: issue},
            upstream_snapshots={
                "closeio/ciso8601": {
                    "repository_id": 4242,
                    "base_ref": "master",
                    "base_sha": SHA_C,
                }
            },
        )
        self.assertEqual(result.report["controller"]["intake"][0]["decision"], "accept")
        contributions = [record for kind, record in result.staged if kind == "contributions"]
        record = contributions[-1]
        self.assertEqual(record["upstream"]["full_name"], "closeio/ciso8601")
        self.assertEqual(record["upstream"]["base_ref"], "master")
        self.assertEqual(record["distributions"], ["ciso8601"])
        self.assertEqual(record["tracking_issue"]["description"], issue["body"])
        self.assertEqual(record["tracking_issue"]["description_digest"], BODY_SHA256)
        self.assertEqual(
            record["tracking_issue"]["notes"],
            REQUEST_FIXTURE["fields"]["additional-notes"],
        )
        self.assertEqual(
            record["active_work"]["tracking_issue"]["description_digest"], BODY_SHA256
        )
        issue["body"] = "edited after intake"
        self.assertEqual(record["tracking_issue"]["description"], BODY_PATH.read_text())

        with tempfile.TemporaryDirectory() as directory:
            store = state.StateStore(Path(directory), self.policy)
            for kind, staged in result.staged:
                store.stage(kind, staged)
            store.write_staged()
            loaded = list(store.load_all("contributions").values())
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["tracking_issue"]["description_digest"], BODY_SHA256)


if __name__ == "__main__":
    unittest.main()
