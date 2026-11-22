#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Bounded readback of the Pages artifact through the lookup deploy-pages uses.

actions/deploy-pages does not read the REST artifacts API. It lists artifacts
through the Actions Results service (Twirp ``ArtifactService/ListArtifacts``)
with the job's runtime token, scoped to the workflow run and job backend IDs
carried in that token, and keeps the artifacts named ``github-pages``. That
listing can lag behind a finished upload even while the REST API already shows
the artifact, so this readback queries the same service with the same request
and requires exactly one ``github-pages`` artifact with the uploaded ID.

The runtime token is only present in the environment of actions, so the
workflow starts this script from an action step. The token is never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

ARTIFACT_NAME = "github-pages"
LIST_ARTIFACTS_PATH = "/twirp/github.actions.results.api.v1.ArtifactService/ListArtifacts"


class PagesArtifactError(RuntimeError):
    pass


def backend_ids(runtime_token: str) -> tuple[str, str]:
    """Return the run and job backend IDs from the runtime token's scope claim."""
    parts = runtime_token.split(".")
    if len(parts) != 3:
        raise PagesArtifactError("runtime token is not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError) as error:
        raise PagesArtifactError("runtime token claims are unreadable") from error
    scope = claims.get("scp") if isinstance(claims, dict) else None
    if not isinstance(scope, str):
        raise PagesArtifactError("runtime token has no scope claim")
    for item in scope.split(" "):
        fields = item.split(":")
        if fields[0] == "Actions.Results" and len(fields) == 3 and all(fields[1:]):
            return fields[1], fields[2]
    raise PagesArtifactError("runtime token has no Actions.Results scope")


def list_request(results_url: str, runtime_token: str) -> urllib.request.Request:
    """Build the ListArtifacts request deploy-pages sends for this job."""
    origin = urllib.parse.urlsplit(results_url)
    if origin.scheme != "https" or not origin.netloc:
        raise PagesArtifactError("results service URL is not an https origin")
    run_id, job_id = backend_ids(runtime_token)
    body = json.dumps(
        {"workflow_run_backend_id": run_id, "workflow_job_run_backend_id": job_id}
    ).encode()
    return urllib.request.Request(
        f"{origin.scheme}://{origin.netloc}{LIST_ARTIFACTS_PATH}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {runtime_token}",
            "Content-Type": "application/json",
        },
    )


def _artifact_id(artifact: dict) -> int | None:
    value = artifact.get("database_id", artifact.get("databaseId"))
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def await_visible(
    artifact_id: int,
    fetch: Callable[[], dict],
    *,
    wait_seconds: int,
    attempts: int,
    sleep: Callable[[float], None] = time.sleep,
    delay_seconds: int = 5,
) -> int:
    """Wait, then poll until deploy-pages' lookup returns exactly this artifact.

    The schedule is fixed: ``wait_seconds`` once, then up to ``attempts``
    lookups separated by 5, 10, 15, ... seconds. A transient lookup failure or
    an empty listing is retried; a duplicate or a different artifact fails at
    once, because deploy-pages would refuse or deploy the wrong content.
    """
    if wait_seconds:
        sleep(wait_seconds)
    for attempt in range(1, attempts + 1):
        try:
            payload = fetch()
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            payload = {"artifacts": []}
        artifacts = payload.get("artifacts", []) if isinstance(payload, dict) else None
        if not isinstance(artifacts, list):
            raise PagesArtifactError("artifact listing is malformed")
        named = [
            item
            for item in artifacts
            if isinstance(item, dict) and item.get("name") == ARTIFACT_NAME
        ]
        if len(named) > 1:
            raise PagesArtifactError("multiple github-pages artifacts for this run")
        if len(named) == 1:
            if _artifact_id(named[0]) != artifact_id:
                raise PagesArtifactError(
                    "deploy-pages would see a different github-pages artifact"
                )
            return artifact_id
        if attempt < attempts:
            sleep(attempt * delay_seconds)
    raise PagesArtifactError(
        f"github-pages artifact {artifact_id} is not visible to the deploy-pages "
        f"lookup after {attempts} checks"
    )


def _fetch(request: urllib.request.Request) -> dict:
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise PagesArtifactError("artifact listing is not an object")
    return value


def main(argv: list[str] | None = None, environ: dict | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("await",))
    parser.add_argument("--artifact-id", required=True, type=int)
    parser.add_argument("--wait", type=int, default=0)
    parser.add_argument("--attempts", type=int, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.attempts <= 10:
        parser.error("attempts must be in 1..10")
    if not 0 <= args.wait <= 300:
        parser.error("wait must be in 0..300 seconds")
    environ = os.environ if environ is None else environ
    try:
        results_url = environ.get("ACTIONS_RESULTS_URL", "")
        runtime_token = environ.get("ACTIONS_RUNTIME_TOKEN", "")
        if not results_url or not runtime_token:
            raise PagesArtifactError(
                "ACTIONS_RESULTS_URL and ACTIONS_RUNTIME_TOKEN are required; "
                "run this from an action step"
            )
        request = list_request(results_url, runtime_token)
        artifact_id = await_visible(
            args.artifact_id,
            lambda: _fetch(request),
            wait_seconds=args.wait,
            attempts=args.attempts,
        )
    except PagesArtifactError as error:
        print(f"::error::{error}")
        return 1
    print(f"github-pages artifact {artifact_id} is visible to deploy-pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
