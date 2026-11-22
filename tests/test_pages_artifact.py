# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Bounded readback of the Pages artifact through the deploy-pages lookup."""

import base64
import io
import json
import unittest
import unittest.mock
from contextlib import redirect_stdout

from scripts import pages_artifact

RUN_BACKEND = "ce7f54c7-61c7-4aae-887f-30da475f5f1a"
JOB_BACKEND = "ca395085-040a-526b-2ce8-bdc85f692774"


def token(scope=f"Actions.GenericRead:1 Actions.Results:{RUN_BACKEND}:{JOB_BACKEND}"):
    claims = base64.urlsafe_b64encode(json.dumps({"scp": scope}).encode()).decode()
    return f"e30.{claims.rstrip('=')}.signature"


def artifact(artifact_id=7, name="github-pages"):
    # The Results service encodes int64 IDs as JSON strings.
    return {"name": name, "database_id": str(artifact_id), "size": "10"}


class TestDeployPagesLookup(unittest.TestCase):
    def test_request_matches_the_deploy_pages_list_artifacts_call(self):
        request = pages_artifact.list_request(
            "https://results-receiver.actions.githubusercontent.com/some/path", token()
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://results-receiver.actions.githubusercontent.com"
            "/twirp/github.actions.results.api.v1.ArtifactService/ListArtifacts",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "workflow_run_backend_id": RUN_BACKEND,
                "workflow_job_run_backend_id": JOB_BACKEND,
            },
        )
        self.assertEqual(request.get_header("Authorization"), f"Bearer {token()}")

    def test_token_without_results_scope_or_https_origin_fails_closed(self):
        for bad in ("not-a-jwt", token("Actions.GenericRead:1"), token(f"Actions.Results:{RUN_BACKEND}")):
            with self.subTest(token=bad), self.assertRaises(pages_artifact.PagesArtifactError):
                pages_artifact.list_request("https://results.example", bad)
        with self.assertRaises(pages_artifact.PagesArtifactError):
            pages_artifact.list_request("http://results.example", token())


class TestAwaitVisible(unittest.TestCase):
    def test_lagging_listing_is_polled_with_a_fixed_bounded_schedule(self):
        responses = iter(
            [
                OSError("transient"),
                {"artifacts": []},
                {"artifacts": [artifact(name="other")]},
                {"artifacts": [artifact()]},
            ]
        )

        def fetch():
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        sleeps = []
        result = pages_artifact.await_visible(
            7, fetch, wait_seconds=45, attempts=6, sleep=sleeps.append
        )
        self.assertEqual(result, 7)
        self.assertEqual(sleeps, [45, 5, 10, 15])

    def test_camel_case_ids_are_accepted(self):
        listing = {"artifacts": [{"name": "github-pages", "databaseId": "7"}]}
        self.assertEqual(
            pages_artifact.await_visible(7, lambda: listing, wait_seconds=0, attempts=1), 7
        )

    def test_exhaustion_fails_clearly_after_the_bound(self):
        sleeps = []
        with self.assertRaisesRegex(pages_artifact.PagesArtifactError, "after 3 checks"):
            pages_artifact.await_visible(
                7, lambda: {"artifacts": []}, wait_seconds=15, attempts=3, sleep=sleeps.append
            )
        self.assertEqual(sleeps, [15, 5, 10])

    def test_duplicate_different_or_malformed_listing_fails_at_once(self):
        for listing, message in (
            ({"artifacts": [artifact(7), artifact(8)]}, "multiple"),
            ({"artifacts": [artifact(8)]}, "different"),
            ({"artifacts": [{"name": "github-pages"}]}, "different"),
            ({"artifacts": "nope"}, "malformed"),
        ):
            with self.subTest(listing=listing), self.assertRaisesRegex(
                pages_artifact.PagesArtifactError, message
            ):
                pages_artifact.await_visible(
                    7,
                    lambda listing=listing: listing,
                    wait_seconds=0,
                    attempts=5,
                    sleep=lambda _delay: self.fail("must not retry"),
                )


class TestMain(unittest.TestCase):
    def test_missing_runtime_environment_fails_with_an_error_annotation(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = pages_artifact.main(
                ["await", "--artifact-id", "7", "--attempts", "1"], environ={}
            )
        self.assertEqual(code, 1)
        self.assertIn("::error::", output.getvalue())
        self.assertIn("ACTIONS_RUNTIME_TOKEN", output.getvalue())

    def test_bounds_are_enforced(self):
        for argv in (
            ["await", "--artifact-id", "7", "--attempts", "11"],
            ["await", "--artifact-id", "7", "--attempts", "1", "--wait", "301"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), redirect_stdout(
                io.StringIO()
            ), unittest.mock.patch("sys.stderr", io.StringIO()):
                pages_artifact.main(argv, environ={})


if __name__ == "__main__":
    unittest.main()
