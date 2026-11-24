# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Which artifact restores the HTTP cache, and what is taken out of it."""

import io
import json
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from scripts import restore_http_cache as restore

REPO = "riseproject-dev/porting-agent"
CURRENT = "main-head"


def run(run_id, *, branch="main", sha=None, event="schedule", repository=REPO):
    return {"id": run_id, "head_branch": branch, "head_sha": sha or f"sha{run_id}", "event": event,
            "head_repository": {"full_name": repository}}


def archive(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf, warnings.catch_warnings():
        warnings.simplefilter("ignore")  # a duplicate member is the point of a case
        for info, data in members:
            zf.writestr(info, data)
    return out.getvalue()


def symlink():
    info = zipfile.ZipInfo(restore.MEMBER)
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


class Github:
    """A GitHub with ``runs``, the comparison ``status`` of each run's commit,
    and the artifacts ``artifacts[run]`` (``(id, expired, size, zip bytes)``)."""

    def __init__(self, runs, status=None, artifacts=None):
        self.runs, self.status, self.artifacts, self.calls = runs, status or {}, artifacts or {}, []

    def __call__(self, path, *flags):
        self.calls.append(path)
        if "/workflows/controller.yml/runs" in path:
            return json.dumps({"workflow_runs": self.runs}).encode()
        if "/compare/" in path:
            sha = path.split("/compare/")[1].split("...")[0]
            if sha not in self.status:
                raise OSError("compare failed")
            return self.status[sha].encode()
        if path.endswith("/zip"):
            ident = int(path.split("/artifacts/")[1].split("/")[0])
            for items in self.artifacts.values():
                for item in items:
                    if item[0] == ident:
                        return item[3]
        if "/artifacts?name=" in path:
            items = self.artifacts.get(int(path.split("/runs/")[1].split("/")[0]), [])
            return json.dumps({"artifacts": [
                {"id": i, "name": "porting-http-cache", "expired": e, "size_in_bytes": n}
                for i, e, n, _z in items]}).encode()
        raise AssertionError(path)


GOOD = archive([(restore.MEMBER, b"SQLite data")])


class TestRestore(unittest.TestCase):
    def restore(self, github):
        with tempfile.TemporaryDirectory() as tmp:
            result = restore.restore(REPO, "99", CURRENT, "porting-http-cache", Path(tmp) / "d",
                                     github)
            target = Path(tmp) / "d" / restore.MEMBER
            return result, target.read_bytes() if target.exists() else None

    def test_the_newest_run_on_main_with_an_artifact_is_used(self):
        github = Github([run(5), run(4)], {"sha5": "ahead", "sha4": "ahead"},
                        {5: [(50, False, 100, GOOD)], 4: [(40, False, 100, archive([(restore.MEMBER, b"old")]))]})
        self.assertEqual(self.restore(github), (5, b"SQLite data"))

    def test_runs_that_are_not_trusted_are_skipped(self):
        runs = [run(9, branch="feature"), run(8, repository="someone/fork"), run(7, event="pull_request"),
                run(6, sha="tagged"), run(99), run(5, sha="ancestor")]
        status = {"sha9": "ahead", "sha8": "ahead", "sha7": "ahead", "tagged": "diverged",
                  "sha99": "ahead", "ancestor": "identical"}
        artifacts = {n: [(n * 10, False, 100, archive([(restore.MEMBER, str(n).encode())]))]
                     for n in (9, 8, 7, 6, 99, 5)}
        self.assertEqual(self.restore(Github(runs, status, artifacts)), (5, b"5"))

    def test_a_commit_that_is_not_an_ancestor_is_refused(self):
        for status in ("behind", "diverged", "weird"):
            with self.subTest(status):
                github = Github([run(5)], {"sha5": status}, {5: [(50, False, 100, GOOD)]})
                self.assertEqual(self.restore(github), (None, None))
        github = Github([run(5)], {}, {5: [(50, False, 100, GOOD)]})  # the comparison fails
        self.assertEqual(self.restore(github), (None, None))

    def test_up_to_thirty_runs_are_listed(self):
        github = Github([])
        self.restore(github)
        self.assertIn("per_page=30", github.calls[0])

    def test_an_expired_oversized_or_missing_artifact_is_skipped(self):
        status = {"sha5": "ahead", "sha4": "ahead", "sha3": "ahead", "sha2": "ahead"}
        github = Github([run(5), run(4), run(3), run(2)], status, {
            5: [(50, True, 100, GOOD)],
            4: [(40, False, restore.MAX_BYTES + 1, GOOD)],
            3: [],
            2: [(20, False, 100, archive([(restore.MEMBER, b"older")]))]})
        self.assertEqual(self.restore(github), (2, b"older"))

    def test_no_run_and_a_failing_listing_leave_the_cache_empty(self):
        self.assertEqual(self.restore(Github([])), (None, None))

        def broken(*_a):
            raise OSError("gh failed")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(restore.restore(REPO, "1", CURRENT, "n", Path(tmp), broken))


class TestExtract(unittest.TestCase):
    def extract(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            ok = restore.extract(data, Path(tmp) / "d")
            target = Path(tmp) / "d" / restore.MEMBER
            return ok, target.read_bytes() if target.exists() else None

    def test_one_regular_member_is_extracted(self):
        self.assertEqual(self.extract(GOOD), (True, b"SQLite data"))

    def test_anything_else_is_refused(self):
        cases = {
            "not a zip": b"PK nonsense",
            "no such member": archive([("other", b"x")]),
            "a nested path": archive([("dir/" + restore.MEMBER, b"x")]),
            "a symlink": archive([(symlink(), "/etc/passwd")]),
            "a directory": archive([(restore.MEMBER + "/", b"")]),
            "twice": archive([(restore.MEMBER, b"a"), (restore.MEMBER, b"b")]),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertEqual(self.extract(data), (False, None))

    def test_a_member_over_the_bound_is_refused(self):
        from unittest import mock
        with mock.patch.object(restore, "MAX_BYTES", 5):
            self.assertEqual(self.extract(archive([(restore.MEMBER, b"123456")])), (False, None))
            self.assertEqual(self.extract(archive([(restore.MEMBER, b"12345")])), (True, b"12345"))


if __name__ == "__main__":
    unittest.main()
