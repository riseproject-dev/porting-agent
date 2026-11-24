# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Resolve an exact Claim's verified Result and proposal artifact (plan 15.3).

The publish job must not trust a generic successful job, Completion, or an
artifact chosen by name. It runs the pinned gh-aw v1.2.2 CLI:

    gh aw work-queue --repo OWNER/REPO replay --json

which validates the whole ledger and prints the replayed ``Projection``
(``pkg/workqueue/types.go``). From it we require, for the assignment's
exact Work and Claim:

* ``works[work_id].claim_id == claim_id`` and ``barrier == "verified"``;
  gh-aw sets ``barrier``/``result``/``result_commit_id`` together only when
  the Result's evidence is ``delivery``/``verified_receipts`` for the
  dispatch's bound run, attempt 1 (``settleVerifiedDelivery``);
* ``claims[claim_id].state == "completed"`` and the claim belongs to that
  work;
* ``dispatches[dispatch_id].run`` is this workflow run, attempt 1, this
  workflow file;
* ``result`` (the canonical descriptor) holds exactly one
  ``upload_artifact`` output whose resource is an artifact of this run.

That output's artifact ID, name and digest are the only artifact the
publisher may download, and its bytes must match the digest.

The claimed Work is written as ``claim.json`` and resolved into ``task.json``:
its payload names the task file the controller committed
(``task_file.resolve``), which is read at the pinned commit and checked
against the claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

import schemas
import task_file

# Native safe-output tool accepts path, not a name override. Uploading the
# porting-out directory yields claim-<hash>-porting-out.
PROPOSAL_ARTIFACT_SUFFIX = "porting-out"
GH_AW_VERSION = "v1.2.2"


class ResultError(ValueError):
    """No usable verified Result: the publisher must not push."""


@dataclass(frozen=True)
class ProposalArtifact:
    work_id: str
    claim_id: str
    artifact_id: int
    name: str
    digest: str            # "sha256:<hex>" as reported by the artifact API
    run_id: str
    run_attempt: int
    result_commit: str | None


def resolve(projection: dict, *, work_id: str, claim_id: str, dispatch_id: str, run_id: str,
            workflow: str) -> ProposalArtifact:
    """Check the replayed projection and return the Result's artifact."""
    work = (projection.get("works") or {}).get(work_id)
    if not work:
        raise ResultError("Work not found in the replayed queue")
    if work.get("claim_id") != claim_id:
        raise ResultError("Work is owned by another Claim")
    if work.get("barrier") != "verified" or not work.get("result_commit_id"):
        raise ResultError(f"no verified Result (barrier={work.get('barrier')!r})")
    claim = (projection.get("claims") or {}).get(claim_id) or {}
    if claim.get("work_id") != work_id or claim.get("dispatch_id") != dispatch_id:
        raise ResultError("Claim does not belong to this Work/dispatch")
    if claim.get("state") != "completed":
        raise ResultError(f"Claim is {claim.get('state')!r}, not completed")
    dispatch = (projection.get("dispatches") or {}).get(dispatch_id) or {}
    run = dispatch.get("run") or {}
    if str(run.get("run_id")) != str(run_id) or run.get("run_attempt") != 1:
        raise ResultError("dispatch is bound to another run or attempt")
    if run.get("workflow") != workflow:
        raise ResultError("dispatch is bound to another workflow")
    descriptor = work.get("result")
    if isinstance(descriptor, str):
        descriptor = json.loads(descriptor)
    outputs = (descriptor or {}).get("outputs") or []
    arts = [o for o in outputs if o.get("type") == "upload_artifact"]
    if len(arts) != 1:
        raise ResultError(f"expected exactly one verified artifact output, found {len(arts)}")
    res = arts[0].get("resource") or {}
    if res.get("kind") != "artifact" or str(res.get("target_run_id")) != str(run_id):
        raise ResultError("artifact resource is not from this run")
    name = res.get("name") or ""
    if not name.endswith(PROPOSAL_ARTIFACT_SUFFIX):
        raise ResultError(f"unexpected artifact name {name!r}")
    digest = res.get("digest") or ""
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ResultError("artifact digest missing")
    try:
        aid = int(res["id"])
    except (KeyError, ValueError):
        raise ResultError("artifact ID missing") from None
    return ProposalArtifact(work_id=work_id, claim_id=claim_id, artifact_id=aid, name=name, digest=digest,
                            run_id=str(run_id), run_attempt=1, result_commit=work.get("result_commit_id"))


def check_artifact_metadata(meta: dict, art: ProposalArtifact) -> None:
    """``meta`` is GET /repos/{repo}/actions/artifacts/{id}."""
    if meta.get("id") != art.artifact_id or meta.get("name") != art.name:
        raise ResultError("artifact identity changed")
    if meta.get("expired"):
        raise ResultError("artifact expired")
    if str((meta.get("workflow_run") or {}).get("id")) != art.run_id:
        raise ResultError("artifact belongs to another run")
    if meta.get("digest") != art.digest:
        raise ResultError("artifact digest changed")


def check_bytes(data: bytes, art: ProposalArtifact) -> None:
    if "sha256:" + hashlib.sha256(data).hexdigest() != art.digest:
        raise ResultError("downloaded artifact does not match the verified digest")


def replay(gh_aw: str, repo: str, timeout: int = 600) -> dict:
    """Run the pinned gh-aw CLI's validating replay of the live ledger."""
    v = subprocess.run([gh_aw, "version"], capture_output=True, text=True, timeout=60)
    if GH_AW_VERSION not in v.stdout:
        raise ResultError(f"expected gh-aw {GH_AW_VERSION}, got {v.stdout.strip()!r}")
    p = subprocess.run([gh_aw, "work-queue", "--repo", repo, "replay", "--json"],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        # Network/auth failures and ledger validation failures look alike here;
        # either way nothing may be published now, and retrying is safe.
        raise OSError(f"gh aw work-queue replay failed: {p.stderr.strip()[-300:]}")
    return replay_output(p)


def replay_output(result: subprocess.CompletedProcess) -> dict:
    """The v1.2.2 CLI emits replay JSON on stderr; accept stdout on versions
    that use it, but only a complete JSON object, never mixed diagnostics."""
    value = json.loads(result.stdout if result.stdout.strip() else result.stderr)
    if not isinstance(value, dict) or not isinstance(value.get("works"), dict):
        raise ValueError("replay output is not a queue projection")
    return value


def queue_projection(gh_aw: str, repo: str, timeout: int = 600) -> dict | None:
    """The validating replay of the live queue, or None when the queue does
    not exist yet. Any other failure raises OSError: the caller must treat
    the queue as unknown."""
    p = subprocess.run([gh_aw, "work-queue", "--repo", repo, "replay", "--json"],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        if "queue_missing" in p.stderr:
            return None
        raise OSError(f"gh aw work-queue replay failed: {p.stderr.strip()[-300:]}")
    return replay_output(p)


def work_states(projection: dict | None) -> dict[str, str]:
    """``graph_id -> state`` for every Work of a replayed queue; a queue that
    does not exist yet is empty."""
    works = ((projection or {}).get("works") or {}).values()
    return {w["graph_id"]: w.get("state") or "unknown" for w in works if w.get("graph_id")}


def work_retries(projection: dict | None,
                 skipped: list[str] | None = None) -> dict[str, dict]:
    """``graph_id -> retry`` for every available Work of a replayed queue
    whose earlier Claim ended without a Result: the worker run was
    cancelled, timed out or lost its runner, and gh-aw cancelled its Claim
    and holds the Work for another attempt (``attempts`` Claims so far, at
    most ``max_attempts``, the retry Policy of its pool) once ``retry_at``
    (its ``retry_not_before``) has passed. Once the attempts are spent gh-aw
    cancels the Work itself, so it is not listed. A Work whose
    ``retry_not_before`` is no representable time is left out, and
    ``skipped``, when given, receives one line that says why, so one
    malformed entry never costs the dispatch recovery of the others."""
    projection = projection or {}
    pools = ((projection.get("policy") or {}).get("pools")) or {}
    retries: dict[str, dict] = {}
    for item in (projection.get("works") or {}).values():
        attempts, at = item.get("attempts"), item.get("retry_not_before")
        if item.get("state") != "available" or not item.get("graph_id") \
                or not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1 \
                or not isinstance(at, int) or isinstance(at, bool) or at < 0:
            continue
        try:
            retry_at = datetime.fromtimestamp(at / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError) as error:
            if skipped is not None:
                skipped.append(f"queue retry of {item['graph_id']} skipped: its retry_not_before "
                               f"{at} is no representable time ({error})")
            continue
        limit = ((pools.get(item.get("pool")) or {}).get("retry") or {}).get("max_attempts")
        retries[item["graph_id"]] = {
            "attempts": attempts,
            "max_attempts": limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
            "retry_at": retry_at,
        }
    return retries


# A worker run id the reconciler reads: it is put in an API path.
_RUN_ID = re.compile(r"^[1-9][0-9]{0,19}$")


def work_runs(projection: dict | None) -> dict[str, dict]:
    """``graph_id -> run`` for every completed Work of a replayed queue: the
    worker run the queue bound to it, ``{"run_id", "run_attempt",
    "workflow"}``. The run is the run of the dispatch of the Work's Claim,
    else the run named by the single ``upload_artifact`` output of its
    Result (``target_run_id``). A completed Work with neither, or whose run
    id is not a plain decimal id, maps to ``{"run_id": None}``."""
    projection = projection or {}
    claims = projection.get("claims") or {}
    dispatches = projection.get("dispatches") or {}
    runs: dict[str, dict] = {}
    for item in (projection.get("works") or {}).values():
        if not isinstance(item, dict) or item.get("state") != "completed" \
                or not isinstance(item.get("graph_id"), str) or not item["graph_id"]:
            continue
        claim = claims.get(item.get("claim_id")) if isinstance(item.get("claim_id"), str) else None
        dispatch = dispatches.get((claim or {}).get("dispatch_id")) \
            if isinstance((claim or {}).get("dispatch_id"), str) else None
        run = (dispatch or {}).get("run") if isinstance(dispatch, dict) else None
        found = None
        if isinstance(run, dict) and run.get("run_id") is not None:
            found = {"run_id": str(run.get("run_id")), "run_attempt": run.get("run_attempt"),
                     "workflow": run.get("workflow")}
        else:
            found = _result_run(item.get("result"))
        if found is None or not _RUN_ID.fullmatch(found["run_id"]):
            found = {"run_id": None}
        if runs.get(item["graph_id"], {}).get("run_id") is None:
            runs[item["graph_id"]] = found
    return runs


def work_claim_runs(projection: dict | None) -> dict[str, list[str]]:
    """``graph_id -> run ids`` of the dispatches of every Claim of the Work
    of a replayed queue, newest first (the largest run id). A Claim without
    a dispatch run, or whose run id is not a plain decimal id, is left out."""
    projection = projection or {}
    dispatches = projection.get("dispatches") or {}
    graphs = {work_id: item.get("graph_id")
              for work_id, item in (projection.get("works") or {}).items()
              if isinstance(item, dict) and isinstance(item.get("graph_id"), str)}
    runs: dict[str, list[str]] = {}
    for claim in (projection.get("claims") or {}).values():
        if not isinstance(claim, dict):
            continue
        graph = graphs.get(claim.get("work_id"))
        dispatch = dispatches.get(claim.get("dispatch_id")) \
            if isinstance(claim.get("dispatch_id"), str) else None
        run = (dispatch or {}).get("run") if isinstance(dispatch, dict) else None
        run_id = str(run.get("run_id")) if isinstance(run, dict) and run.get("run_id") is not None \
            else None
        if graph and run_id and _RUN_ID.fullmatch(run_id) and run_id not in runs.get(graph, []):
            runs.setdefault(graph, []).append(run_id)
    for ids in runs.values():
        ids.sort(key=int, reverse=True)
    return runs


def _result_run(descriptor) -> dict | None:
    """The run named by the single ``upload_artifact`` output of a Result
    descriptor, as ``resolve`` reads it, or ``None``."""
    if isinstance(descriptor, str):
        try:
            descriptor = json.loads(descriptor)
        except ValueError:
            return None
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("outputs"), list):
        return None
    arts = [o for o in descriptor["outputs"]
            if isinstance(o, dict) and o.get("type") == "upload_artifact"]
    if len(arts) != 1:
        return None
    res = arts[0].get("resource") or {}
    if not isinstance(res, dict) or res.get("kind") != "artifact" \
            or res.get("target_run_id") is None:
        return None
    return {"run_id": str(res["target_run_id"]), "run_attempt": None, "workflow": None}


def queue_work_states(gh_aw: str, repo: str, timeout: int = 600) -> dict[str, str]:
    """``graph_id -> state`` for every Work in the live queue, from the same
    validating replay. A queue that does not exist yet is empty. Any other
    failure raises OSError: the caller must treat the queue as unknown."""
    return work_states(queue_projection(gh_aw, repo, timeout))


def _write_status(out, status: str, failure_class: str, reason: str) -> None:
    from pathlib import Path
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "outcome.json").write_text(json.dumps(
        {"status": status, "failure_class": failure_class, "reason": reason[:2000], "stage": "result"}, indent=2))


def main(argv=None) -> int:
    """python -m queue_result --gh-aw BIN --assignment A.json --run-id N
    --workflow PATH --repo OWNER/REPO --out DIR [--projection FILE]

    Resolves the Result, checks artifact metadata via the API, downloads the
    archive by ID and verifies its digest. Exit 1 (no publish) otherwise.
    ``--projection`` reads a saved ``replay --json`` output instead of
    running the CLI (used by the retry path, which must re-run the replay
    anyway when it has no saved projection)."""
    import argparse
    import sys
    from pathlib import Path

    from collect import Api, CollectError
    ap = argparse.ArgumentParser()
    ap.add_argument("--gh-aw")
    ap.add_argument("--projection", type=Path)
    ap.add_argument("--assignment", required=True, type=Path)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    assignment = json.loads(a.assignment.read_text())
    claims = assignment.get("claims") or []
    if assignment.get("version") != 3 or len(claims) != 1:
        print("no publish: expected a version-3 singleton assignment", file=sys.stderr)
        return 1
    m = claims[0]
    try:
        if a.projection:
            projection = json.loads(a.projection.read_text())
        elif a.gh_aw:
            projection = replay(a.gh_aw, a.repo)
        else:
            raise ResultError("need --gh-aw or --projection")
        art = resolve(projection, work_id=m["work_id"], claim_id=m["claim_id"],
                      dispatch_id=assignment["dispatch_id"], run_id=a.run_id, workflow=a.workflow)
        api = Api()
        check_artifact_metadata(api.get(f"/repos/{a.repo}/actions/artifacts/{art.artifact_id}"), art)
        data = api.get_bytes(f"/repos/{a.repo}/actions/artifacts/{art.artifact_id}/zip")
        check_bytes(data, art)
        # The task of the claimed Work: read from the task file its payload
        # names, at the pinned commit, and checked against the claim.
        task = task_file.resolve(m["work"], validate=lambda t: schemas.validate("task", t))
    except (ResultError, task_file.Refused) as e:
        print(f"no publish: {e}", file=sys.stderr)
        _write_status(a.out, "rejected", "validation", str(e))
        return 1
    except (CollectError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        # (``task_file.Unavailable`` is an OSError)
        # Could not reach GitHub or the CLI failed: retry later, same artifact.
        print(f"no publish (infrastructure): {e}", file=sys.stderr)
        _write_status(a.out, "infra_error", "infrastructure", str(e))
        return 3
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "proposal.zip").write_bytes(data)
    (a.out / "result.json").write_text(json.dumps(art.__dict__, indent=2))
    (a.out / "claim.json").write_text(json.dumps(m["work"], indent=2))
    (a.out / "task.json").write_text(json.dumps(task, indent=2))
    print(json.dumps(art.__dict__))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
