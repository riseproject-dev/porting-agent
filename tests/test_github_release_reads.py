# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The collector reads of the GitHub releases of a repository."""

import unittest

import collect

SHA_M = "a" * 40
SHA_T = "b" * 40


class Api:
    def __init__(self, pages=(), routes=None, compare="ahead"):
        self.pages, self.routes, self.compare, self.base = pages, routes or {}, compare, "https://api.github.com"
        self.paths = []

    def paginate(self, path, **kwargs):
        self.paths.append(path)
        return list(self.pages)

    def get(self, path):
        self.paths.append(path)
        if path not in self.routes:
            raise collect.CollectError(f"GET {path}: HTTP 404", status=404)
        return self.routes[path]

    def _request(self, url):
        self.paths.append(url)
        return {"status": self.compare}, {}


class TestGithubReleases(unittest.TestCase):
    def test_drafts_and_unpublished_are_dropped_and_assets_are_names(self):
        api = Api([
            {"tag_name": "v1", "published_at": "2026-11-24T00:00:00Z", "draft": False,
             "prerelease": False, "html_url": "https://github.com/o/r/releases/tag/v1",
             "assets": [{"name": "a_linux_riscv64.tgz", "state": "uploaded"},
                        {"name": "partial_linux_riscv64.tgz", "state": "starter"}]},
            {"tag_name": "v2", "published_at": None, "draft": True, "assets": []},
            {"tag_name": "v3", "published_at": "2026-11-25T00:00:00Z", "draft": True},
            {"tag_name": "v4-rc", "published_at": "2026-11-26T00:00:00Z", "draft": False,
             "prerelease": True, "assets": []}])
        out = collect.github_releases(api, "o/r")
        self.assertEqual([r["tag"] for r in out], ["v1", "v4-rc"])
        self.assertEqual(out[0]["assets"], ["a_linux_riscv64.tgz"])
        self.assertTrue(out[1]["prerelease"])
        self.assertEqual(api.paths, ["/repos/o/r/releases?per_page=100"])

    def test_an_unexpected_repository_is_refused(self):
        with self.assertRaises(collect.CollectError):
            collect.github_releases(Api(), "../x")

    def test_a_tag_contains_the_merge_when_it_is_ahead_or_identical(self):
        route = "/repos/o/r/commits/refs%2Ftags%2Fv1%2Frc"
        for status, want in (("ahead", True), ("identical", True), ("behind", False),
                             ("diverged", False)):
            api = Api(routes={route: {"sha": SHA_T}}, compare=status)
            self.assertIs(collect.tag_contains(api, "o/r", "v1/rc", SHA_M), want)
            self.assertIn(f"https://api.github.com/repos/o/r/compare/{SHA_M}...{SHA_T}?per_page=100",
                          api.paths)

    def test_the_merge_commit_itself_and_a_missing_tag(self):
        route = "/repos/o/r/commits/refs%2Ftags%2Fv1"
        self.assertTrue(collect.tag_contains(Api(routes={route: {"sha": SHA_M}}), "o/r", "v1", SHA_M))
        self.assertIsNone(collect.tag_contains(Api(), "o/r", "v1", SHA_M))

    def test_a_tag_that_is_not_a_plain_name_is_refused(self):
        for tag in ("../x", "v1 2", "v1?x=1", ""):
            with self.assertRaises(collect.CollectError):
                collect.tag_contains(Api(), "o/r", tag, SHA_M)


if __name__ == "__main__":
    unittest.main()
