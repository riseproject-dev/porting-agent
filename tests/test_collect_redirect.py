# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Artifact redirects drop credentials and reject unexpected origins."""
import unittest

import requests

import collect


class TestArtifactRedirect(unittest.TestCase):
    """``requests`` calls ``rebuild_auth`` for each redirect it follows."""

    def follow(self, location):
        request = requests.Request("GET", "https://api.github.com/repos/o/r/actions/artifacts/1/zip",
                                   headers={"Authorization": "Bearer secret"}).prepare()
        response = requests.Response()
        response.request = request
        redirected = requests.Request("GET", location, headers=dict(request.headers)).prepare()
        collect._Session().rebuild_auth(redirected, response)
        return redirected

    def test_cross_origin_blob_redirect_drops_authorization(self):
        for location in ("https://productionresults.blob.core.windows.net/actions-results/file.zip?sig=x",
                         "https://objects.githubusercontent.com/file.zip"):
            with self.subTest(location=location):
                self.assertNotIn("Authorization", self.follow(location).headers)

    def test_unexpected_cross_origin_is_rejected(self):
        for location in ("https://example.com/file.zip",
                         "http://productionresults.blob.core.windows.net/file.zip",
                         "https://blob.core.windows.net.evil.example/file.zip"):
            with self.subTest(location=location), self.assertRaises(collect.CollectError):
                self.follow(location)

    def test_same_origin_keeps_authorization(self):
        self.assertEqual(self.follow("https://api.github.com/next").headers["Authorization"],
                         "Bearer secret")

    def test_both_sessions_guard_their_redirects(self):
        self.assertIs(collect._CachedSession.rebuild_auth, collect._ArtifactRedirect.rebuild_auth)
        self.assertIs(collect._Session.rebuild_auth, collect._ArtifactRedirect.rebuild_auth)


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


class TestRenamedBranchOverHttp(unittest.TestCase):
    """The real ``Api`` follows GitHub's 301 for a renamed branch, and the
    branch it lands on is reported as the rename, never read as the branch
    asked for."""

    def setUp(self):
        import http.server
        import json
        import os
        import threading
        from unittest import mock

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/repos/o/r/branches/master":
                    self.send_response(301)
                    self.send_header("Location", "/repositories/1/branches/main")
                    self.end_headers()
                    return
                if self.path == "/repositories/1/branches/main":
                    body = json.dumps({"name": "main", "commit": {
                        "sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}},
                        "parents": []}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        # The loopback server is reached directly, not through a proxy.
        env = mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"})
        env.start()
        self.addCleanup(env.stop)
        self.api = collect.Api(token="t", base=f"http://127.0.0.1:{self.server.server_port}")

    def test_the_redirect_of_a_renamed_branch_is_followed_and_reported(self):
        with self.assertRaises(collect.BranchRenamed) as raised:
            collect.branch_head(self.api, "o/r", "master")
        self.assertEqual(raised.exception.name, "main")

    def test_a_missing_branch_is_none(self):
        self.assertIsNone(collect.branch_head(self.api, "o/r", "gone"))
