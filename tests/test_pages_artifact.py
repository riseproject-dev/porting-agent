# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Bounded self-healing discovery for eventually consistent Pages artifacts."""

import unittest

from scripts import pages_artifact

RUN_ID = 44974125010


def artifact(artifact_id=7, *, run_id=RUN_ID, expired=False, name="github-pages"):
    return {
        "id": artifact_id,
        "name": name,
        "expired": expired,
        "workflow_run": {"id": run_id},
    }


class TestPagesArtifactDiscovery(unittest.TestCase):
    def test_eventual_visibility_is_retried_with_a_bound(self):
        responses = iter(
            [
                {"artifacts": []},
                {"artifacts": [artifact(name="other")]},
                {"artifacts": [artifact()]},
            ]
        )
        sleeps = []
        result = pages_artifact.discover(
            RUN_ID,
            lambda: next(responses),
            attempts=6,
            sleep=sleeps.append,
        )
        self.assertEqual(result, 7)
        self.assertEqual(sleeps, [5, 10])

    def test_missing_duplicate_expired_and_wrong_run_fail_closed(self):
        with self.assertRaises(pages_artifact.PagesArtifactError):
            pages_artifact.discover(
                RUN_ID,
                lambda: {"artifacts": []},
                attempts=3,
                sleep=lambda _delay: None,
            )
        with self.assertRaisesRegex(pages_artifact.PagesArtifactError, "multiple"):
            pages_artifact.discover(
                RUN_ID,
                lambda: {"artifacts": [artifact(7), artifact(8)]},
                attempts=1,
            )
        for candidate in (
            artifact(expired=True),
            artifact(run_id=RUN_ID + 1),
            artifact(name="other"),
        ):
            with self.subTest(candidate=candidate), self.assertRaises(
                pages_artifact.PagesArtifactError
            ):
                pages_artifact.discover(
                    RUN_ID,
                    lambda candidate=candidate: {"artifacts": [candidate]},
                    attempts=1,
                )

    def test_retry_requires_exact_artifact_readback(self):
        sleeps = []
        responses = iter([{}, artifact()])
        self.assertEqual(
            pages_artifact.verify(
                7,
                RUN_ID,
                lambda: next(responses),
                attempts=3,
                sleep=sleeps.append,
            ),
            7,
        )
        self.assertEqual(sleeps, [10])
        with self.assertRaises(pages_artifact.PagesArtifactError):
            pages_artifact.verify(
                7,
                RUN_ID,
                lambda: artifact(8),
                attempts=2,
                sleep=lambda _delay: None,
            )


if __name__ == "__main__":
    unittest.main()
