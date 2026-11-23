# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Artifact redirects drop credentials and reject unexpected origins."""
import unittest
import urllib.request
from email.message import Message

import collect


class TestArtifactRedirect(unittest.TestCase):
    def request(self):
        return urllib.request.Request("https://api.github.com/repos/o/r/actions/artifacts/1/zip",
                                      headers={"Authorization": "Bearer secret"})

    def test_cross_origin_blob_redirect_drops_authorization(self):
        redirected = collect._ArtifactRedirect().redirect_request(
            self.request(), None, 302, "Found", Message(),
            "https://productionresults.blob.core.windows.net/actions-results/file.zip?sig=x")
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_unexpected_cross_origin_is_rejected(self):
        with self.assertRaises(collect.CollectError):
            collect._ArtifactRedirect().redirect_request(
                self.request(), None, 302, "Found", Message(), "https://example.com/file.zip")

    def test_same_origin_keeps_authorization(self):
        redirected = collect._ArtifactRedirect().redirect_request(
            self.request(), None, 302, "Found", Message(), "https://api.github.com/next")
        self.assertEqual(redirected.get_header("Authorization"), "Bearer secret")


class TestRenamedBranch(unittest.TestCase):
    """GitHub answers a renamed branch with a redirect to its new name: the
    head of another branch is never read as the head of the one asked for."""

    class Api:
        def __init__(self, name):
            self.name = name

        def get(self, path):
            return {"name": self.name, "commit": {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}},
                                                  "parents": []}}

    def test_a_redirect_to_another_branch_is_refused(self):
        with self.assertRaisesRegex(collect.CollectError, "names another branch 'riscv64-dev'"):
            collect.branch_head(self.Api("riscv64-dev"), "riseproject-upstreaming/proj", "riscv64")
        with self.assertRaises(collect.CollectError):
            collect.branch_head(self.Api(None), "riseproject-upstreaming/proj", "riscv64")

    def test_the_branch_asked_for_is_read(self):
        self.assertEqual(collect.branch_head(self.Api("riscv64"), "riseproject-upstreaming/proj",
                                             "riscv64")["head"], "a" * 40)
