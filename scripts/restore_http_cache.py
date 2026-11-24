# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Restore the HTTP cache of the controller's reads from the artifact of an
earlier Controller run.

Only a run of ``controller.yml`` on a commit that is on ``main`` is used, so
the file was written by the controller as it is on ``main``; a missing,
expired, oversized, or malformed artifact leaves the cache empty. The
command never fails: a controller run does not depend on its cache.

Usage: restore_http_cache.py <artifact name> <directory>
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

MEMBER = "http-cache.sqlite"
MAX_BYTES = 64 * 1024 * 1024
MAX_RUNS = 30
EVENTS = {"push", "schedule", "workflow_run", "workflow_dispatch", "issues", "issue_comment"}


def gh(*args: str) -> bytes:
    """``gh api <args>`` with the job's token, or an ``OSError``."""
    done = subprocess.run(["gh", "api", *args], capture_output=True, check=False)
    if done.returncode:
        raise OSError(f"gh api {args[0]}: exit {done.returncode}")
    return done.stdout


def candidate_runs(repository: str, current_run: str, current_sha: str,
                   api: Callable[..., bytes] = gh) -> list[int]:
    """The ids of the completed earlier Controller runs on ``main`` of
    ``repository``, newest first, whose commit is the current one or an
    ancestor of it and whose trigger is one of ``EVENTS``."""
    listing = json.loads(api(f"/repos/{repository}/actions/workflows/controller.yml/runs"
                             f"?branch=main&status=completed&per_page={MAX_RUNS}"))
    found = []
    for run in listing.get("workflow_runs", []):
        if run.get("head_branch") != "main" or str(run.get("id")) == current_run \
                or (run.get("head_repository") or {}).get("full_name") != repository \
                or run.get("event") not in EVENTS:
            continue
        try:
            # A branch named main and a tag named main report the same
            # head_branch: only a commit that is on this main is accepted.
            status = api(f"/repos/{repository}/compare/{run['head_sha']}...{current_sha}",
                         "--jq", ".status").decode().strip()
        except OSError:
            continue
        if status in ("identical", "ahead"):
            found.append(run["id"])
    return found


def artifact_id(repository: str, run: int, name: str, api: Callable[..., bytes] = gh) -> int | None:
    """The newest unexpired artifact ``name`` of ``run`` that is small enough."""
    listing = json.loads(api(f"/repos/{repository}/actions/runs/{run}/artifacts?name={name}"))
    for artifact in listing.get("artifacts", []):
        if not artifact.get("expired") and artifact.get("name") == name \
                and 0 < artifact.get("size_in_bytes", MAX_BYTES + 1) <= MAX_BYTES:
            return artifact["id"]
    return None


def extract(archive: bytes, directory: Path) -> bool:
    """Write ``MEMBER`` of the zip ``archive`` to ``directory``, only if it
    is one regular file of at most ``MAX_BYTES`` (read with that bound,
    whatever the archive declares)."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            matches = [i for i in zf.infolist() if i.filename == MEMBER]
            if len(matches) != 1:
                return False
            info = matches[0]
            kind = stat.S_IFMT(info.external_attr >> 16)  # 0 when the archive names none
            if info.is_dir() or kind not in (0, stat.S_IFREG) or info.file_size > MAX_BYTES:
                return False
            directory.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source:
                data = source.read(MAX_BYTES + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError):
        return False
    if len(data) > MAX_BYTES:
        return False
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(temporary, directory / MEMBER)
    except OSError:
        Path(temporary).unlink(missing_ok=True)
        return False
    return True


def restore(repository: str, current_run: str, current_sha: str, name: str, directory: Path,
            api: Callable[..., bytes] = gh) -> int | None:
    """The run whose artifact was restored, or ``None`` (an empty cache)."""
    try:
        runs = candidate_runs(repository, current_run, current_sha, api)
    except (OSError, ValueError, AttributeError):
        return None
    for run in runs:
        try:
            found = artifact_id(repository, run, name, api)
            if found is not None and extract(
                    api(f"/repos/{repository}/actions/artifacts/{found}/zip"), directory):
                return run
        except (OSError, ValueError, AttributeError):
            continue
    return None


def main(argv: list[str]) -> int:
    name, directory = argv[1], Path(argv[2])
    directory.mkdir(parents=True, exist_ok=True)
    run = restore(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"],
                  os.environ["GITHUB_SHA"], name, directory)
    print(f"HTTP cache: restored {name} of run {run}" if run
          else "HTTP cache: no usable earlier artifact, starting empty")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
