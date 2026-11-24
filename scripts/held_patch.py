#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Fetch a pinned development patch series for an agent, and verify it.

A trusted pre-agent step runs this before the model starts, with the job's
read-only token. The task names the series by reference
(``context.patch`` for the review of a held patch, ``context.previous_patch``
for the Work that amends a patch its review rejected): the publication
artifact that carries it (``artifact_id``), the run that owns that artifact
(``artifact_run_id``: the worker run, or the publish-only retry that verified
the patch again), and the SHA-256 of the series (``patch_digest``). The
artifact must be the ``porting-publication`` artifact of exactly that run,
and its ``patches.mbox`` must have exactly that digest; ``proposal.json`` is
copied beside it as the worker's own (untrusted) account of the patch.

A missing reference, an expired or deleted artifact, is fatal for the review
(``--required``): the review cannot run without its patch, and the Work ends
without a Result. For a previous patch it only means there is none to read:
``status.json`` says so and the step succeeds. A wrong run, a wrong artifact
name, a missing file, or a digest mismatch is always fatal.

Standard library only: the pre-agent step runs it with the runner's
``python3`` and ``gh``.

    python3 scripts/held_patch.py --task T --key patch --out DIR --required
    python3 scripts/held_patch.py --task T --key previous_patch --out DIR
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable

ARTIFACT = "porting-publication"
KEYS = ("patch", "previous_patch")
_RUN = re.compile(r"^[1-9][0-9]{0,19}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# The files of the artifact the agent reads.
FILES = ("patches.mbox", "proposal.json")
MAX_ZIP_BYTES = 64 * 1024 * 1024


class HeldPatchError(ValueError):
    """The pinned series cannot be given to the agent."""


class Gone(HeldPatchError):
    """The artifact no longer exists or expired."""


def gh_get(path: str) -> bytes:
    """``gh api PATH`` with the job's token; a 404 or 410 is ``Gone``."""
    done = subprocess.run(["gh", "api", path], capture_output=True, timeout=300)
    if done.returncode != 0:
        error = done.stderr.decode(errors="replace")[-500:]
        if "HTTP 404" in error or "HTTP 410" in error:
            raise Gone(f"{path}: gone")
        raise HeldPatchError(f"{path}: {error.strip()}")
    return done.stdout


def reference(task: dict, key: str) -> dict | None:
    """The reference ``key`` of the task's context, checked, or ``None``."""
    if key not in KEYS:
        raise HeldPatchError(f"unknown reference {key!r}")
    ref = (task.get("context") or {}).get(key)
    if ref is None:
        return None
    if not isinstance(ref, dict):
        raise HeldPatchError(f"bad {key}")
    if not _RUN.match(str(ref.get("artifact_run_id") or "")):
        raise HeldPatchError(f"bad {key} artifact run")
    artifact = ref.get("artifact_id")
    if not isinstance(artifact, int) or isinstance(artifact, bool) or artifact <= 0:
        raise HeldPatchError(f"bad {key} artifact")
    if not _DIGEST.match(str(ref.get("patch_digest") or "")):
        raise HeldPatchError(f"bad {key} digest")
    return ref


def fetch(task: dict, key: str, out: Path, *, repository: str,
          get: Callable[[str], bytes] = gh_get, required: bool = False) -> dict:
    """Write the series ``key`` names to ``out`` and return its status:
    ``verified``, or ``absent`` when no series can be read and none is
    ``required``. Raises ``HeldPatchError`` otherwise."""
    if not _REPOSITORY.match(repository):
        raise HeldPatchError("bad repository")
    ref = reference(task, key)
    out.mkdir(parents=True, exist_ok=True)
    try:
        if ref is None:
            raise Gone(f"the task names no {key}")
        meta = json.loads(get(f"/repos/{repository}/actions/artifacts/{ref['artifact_id']}"))
        if not isinstance(meta, dict) or meta.get("name") != ARTIFACT \
                or str((meta.get("workflow_run") or {}).get("id")) != str(ref["artifact_run_id"]):
            raise HeldPatchError(f"the artifact is not the {key} series of run "
                                 f"{ref['artifact_run_id']}")
        if meta.get("expired"):
            raise Gone(f"the artifact of the {key} series expired")
        raw = get(f"/repos/{repository}/actions/artifacts/{ref['artifact_id']}/zip")
    except Gone as gone:
        if required:
            raise
        status = {"status": "absent", "key": key, "reason": str(gone)}
        (out / "status.json").write_text(json.dumps(status, indent=2))
        return status
    if len(raw) > MAX_ZIP_BYTES:
        raise HeldPatchError("the artifact is too large")
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for info in archive.infolist():
                # Only regular files at the top of the archive; a symbolic
                # link (an external attribute of type 0o12) is refused.
                if info.filename in FILES:
                    if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise HeldPatchError(f"{info.filename} is not a regular file")
                    files[info.filename] = archive.read(info)
    except (zipfile.BadZipFile, EOFError) as error:
        raise HeldPatchError(f"the artifact is not a readable archive: {error}") from None
    missing = [name for name in FILES if name not in files]
    if missing:
        raise HeldPatchError(f"the {key} series is incomplete: {', '.join(missing)}")
    digest = hashlib.sha256(files["patches.mbox"]).hexdigest()
    if digest != ref["patch_digest"]:
        raise HeldPatchError(f"{key} digest mismatch")
    for name, data in files.items():
        (out / name).write_bytes(data)
    status = {"status": "verified", "key": key, "patch_digest": digest,
              "artifact_run_id": str(ref["artifact_run_id"]),
              "artifact_id": ref["artifact_id"]}
    (out / "status.json").write_text(json.dumps(status, indent=2))
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--key", choices=KEYS, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args(argv)
    try:
        status = fetch(json.loads(args.task.read_text()), args.key, args.out,
                       repository=os.environ.get("GITHUB_REPOSITORY", ""),
                       required=args.required)
    except HeldPatchError as error:
        print(f"{args.key}: {error}", file=sys.stderr)
        return 1
    # Only the status is printed, never the series.
    print(json.dumps(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
