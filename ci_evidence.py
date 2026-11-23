# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""CI observation and evidence normalization (plan sections 6.2, 11.4, 14.3).

Only executed, matching jobs count. Missing, skipped, neutral, cancelled,
approval-required (``action_required``) and stale jobs, and jobs that lost
their runner, never count as riscv64 build or test success. Reruns are resolved to the latest attempt per job name and matrix
identity; duplicate check names are kept apart by job ID.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

SUCCESS = "success"
NOT_EXECUTED = frozenset({"skipped", "neutral", "cancelled", "action_required", "stale", None})
# Disposable forks intentionally lack package publishing credentials and OIDC
# trust. Those jobs may fail naturally and do not invalidate build/test CI.
CREDENTIALLESS_JOB_MARKERS = ("publish", "upload_pypi", "upload to pypi", "validate-release-request")


@dataclass
class CiVerdict:
    state: str                     # passed | failed | pending | not_executed | access_wait | missing
    executed_jobs: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # The required jobs that were interrupted (``job_interrupted``) and the
    # other blocking jobs that lost their runner, and the
    # latest attempt of each workflow run on the head, by run id.
    interrupted_jobs: list[dict] = field(default_factory=list)
    attempts: dict[int, int] = field(default_factory=dict)


def job_executed(job: dict) -> bool:
    """Whether ``job`` executed. A job that lost its runner (``runner_lost``,
    set only when the snapshot classified it) did not."""
    return job.get("status") == "completed" and job.get("conclusion") not in NOT_EXECUTED \
        and bool(job.get("started_at")) and not job.get("runner_lost")


def job_interrupted(job: dict) -> bool:
    """Whether ``job`` was interrupted: completed and cancelled, or failed
    after it lost its runner. A ``timed_out`` job executed."""
    return job.get("status") == "completed" and (
        job.get("conclusion") == "cancelled" or bool(job.get("runner_lost")))


def latest_attempts(runs: Iterable[dict]) -> list[dict]:
    """Newest attempt of each exact workflow run.

    GitHub may register the same workflow more than once for one head (for
    example, once for a push and again when a pull request is opened). Those
    are independent runs and all remain relevant. A rerun keeps its run ID and
    increments ``run_attempt``.
    """
    best: dict[int, dict] = {}
    for r in runs:
        k = r["id"]
        if k not in best or r["run_attempt"] > best[k]["run_attempt"]:
            best[k] = r
    return list(best.values())


def evaluate(*, runs: list[dict], jobs_by_run: dict[int, list[dict]], head_sha: str,
             required_job_patterns: list[str], expected_wheels: list[str],
             produced_wheels: list[str], required_runner_label: str | None = None) -> CiVerdict:
    """Decide what the observed CI establishes for ``head_sha``.

    ``required_job_patterns`` are substrings identifying the riscv64 build
    and test jobs the target contract requires (from the plan), in any
    language; ``expected_wheels`` applies to a contract that names Python
    wheels only. A run for another SHA is
    ignored: evidence is bound to the exact head that ran.
    """
    v = CiVerdict(state="missing")
    relevant = [r for r in latest_attempts(runs) if r.get("head_sha") == head_sha]
    v.attempts = {r["id"]: r["run_attempt"] for r in relevant}
    if not relevant:
        v.problems.append("no workflow run for this head")
        return v
    if any(r.get("conclusion") == "action_required" for r in relevant):
        v.state = "access_wait"
        v.problems.append("a run needs approval; wait for the responsible human")
        return v
    all_jobs = [j for r in relevant for j in jobs_by_run.get(r["id"], [])]
    pending = ([r for r in relevant if r.get("status") != "completed"]
               + [j for j in all_jobs if j.get("status") != "completed"])
    blocking = [j for j in all_jobs
                if not any(marker in j.get("name", "").lower() for marker in CREDENTIALLESS_JOB_MARKERS)]
    blocking_failures = [j for j in blocking
                         if job_executed(j) and j.get("conclusion") != SUCCESS]
    for j in blocking_failures:
        v.problems.append(f"job {j['name']!r} failed ({j.get('conclusion')})")
    # A blocking job that failed after it lost its runner did not execute,
    # but it is not a pass either: unclassified, the same CI reads as failed.
    # It is interrupted, whether or not a required pattern names it.
    required = [j for j in all_jobs if any(pat in j["name"] for pat in required_job_patterns)]
    for j in blocking:
        if j.get("status") == "completed" and j.get("runner_lost") \
                and not any(j is r for r in required):
            v.problems.append(f"job {j['name']!r} did not execute (lost its runner)")
            v.interrupted_jobs.append(j)
    # When the contract names a runner label, success means the job ran on
    # it: a job only matches when its name matches and it ran on that label.
    for pat in required_job_patterns:
        matching = [j for j in all_jobs if pat in j["name"]]
        if not matching:
            v.problems.append(f"required job {pat!r} absent")
            continue
        for j in matching:
            if j.get("status") != "completed":
                continue
            if not job_executed(j):
                v.problems.append(f"job {j['name']!r} did not execute ({j.get('conclusion')})")
                if job_interrupted(j):
                    v.interrupted_jobs.append(j)
            elif j["conclusion"] != SUCCESS:
                v.problems.append(f"job {j['name']!r} failed ({j['conclusion']})")
            else:
                v.executed_jobs.append(j)
            labels = j.get("labels") or []
            if required_runner_label and required_runner_label not in labels:
                # Runner topology is project-specific. Dedicated native rows
                # pin a label; shared QEMU/cross matrices deliberately do not.
                v.problems.append(
                    f"job {j['name']!r} failed runner-label check {required_runner_label!r}: labels {labels}")
    missing_wheels = sorted(set(expected_wheels) - set(produced_wheels))
    if missing_wheels:
        v.problems.append(f"expected wheels not produced: {', '.join(missing_wheels)}")
    # Do not launch a repair while any exact-head run is incomplete. GitHub's
    # jobs and artifact registration are eventually consistent, so absence is
    # missing evidence rather than a model-actionable failure. Only a terminal,
    # executed job failure (or a terminal executed runner-contract mismatch)
    # can produce ``failed``.
    if pending:
        v.state = "pending"
    elif blocking_failures or any("failed runner-label check" in p for p in v.problems):
        v.state = "failed"
    elif any("did not execute" in p for p in v.problems):
        v.state = "not_executed"
    elif v.problems:
        v.state = "missing"
    else:
        v.state = "passed"
    return v


def evidence_record(*, evidence_id: str, contribution_id: str, policy_version: str,
                    collected_at: str, repository: dict, run: dict, jobs: list[dict],
                    base_sha: str | None, test_merge_sha: str | None,
                    expected_wheels: list[str], artifacts: list[dict], verdict: CiVerdict,
                    debug_present: bool, limitations: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "contribution_id": contribution_id,
        "kind": "ci",
        "policy_version": policy_version,
        "collected_at": collected_at,
        "summary": f"{run['path']} run {run['id']} attempt {run['run_attempt']}: {verdict.state}",
        "limitations": limitations + verdict.problems,
        "debug_present": debug_present,
        "ci": {
            "repository_id": repository["id"],
            "repository": repository["full_name"],
            "head_sha": run["head_sha"],
            "base_sha": base_sha,
            "test_merge_sha": test_merge_sha,
            "workflow_id": run["workflow_id"],
            "workflow_path": run["path"],
            "workflow_sha": run.get("workflow_sha"),
            "event": run["event"],
            "run_id": run["id"],
            "run_attempt": run["run_attempt"],
            "jobs": [_evidence_job(j) for j in sorted(jobs, key=lambda j: j["id"])],
            "expected_wheels": expected_wheels,
            "artifacts": artifacts,
        },
    }


def _evidence_job(job: dict) -> dict[str, Any]:
    """A job of CI evidence. A job the snapshot classified (it carries
    ``runner_lost``) and that was interrupted is marked ``interrupted``; an
    unclassified snapshot records exactly the fields it always did."""
    out = {"job_id": job["id"], "name": job["name"], "status": job["status"],
           "conclusion": job.get("conclusion"), "executed": job_executed(job),
           "runner_labels": job.get("labels") or []}
    if "runner_lost" in job and job_interrupted(job):
        out["interrupted"] = True
    return out


_RUNNER_LABEL_PROBLEM = re.compile(r"^job '(.+)' failed runner-label check ")


def for_clean_head(evidence: dict, *, candidate: str, ref: str, dev_ref: str) -> dict:
    """The failing evidence of CI on a clean head, as evidence of the
    development head ``candidate`` on ``dev_ref`` it was built from; CI ran
    on ``ref``.

    Evidence of ``candidate`` itself is returned unchanged. Otherwise a copy
    records ``ci.candidate_head_sha`` and a first limitation, written by the
    pipeline from commit ids and branch names only, that says where CI ran,
    so the repair Work learns it; ``evidence`` is not changed."""
    if evidence["ci"]["head_sha"] == candidate:
        return evidence
    out = copy.deepcopy(evidence)
    out["ci"]["candidate_head_sha"] = candidate
    out["limitations"] = [
        f"CI ran on the clean head {out['ci']['head_sha']} of `{ref}`: the series of "
        f"{candidate} on `{dev_ref}` without its debug commits",
        *(out.get("limitations") or [])]
    return out


def failing_job(evidence: dict | None, head_sha: str | None) -> str | None:
    """The failing CI job recorded for ``head_sha``: the first executed
    blocking job in ``evidence`` of that head, or of the clean head built
    from it (``ci.candidate_head_sha``, see ``for_clean_head``), that did not
    succeed, else the first job the evidence names as failing the
    runner-label check. ``None`` for evidence of another head, for passing
    evidence, and for evidence that is not CI evidence (review feedback, a
    rehearsal). This name is the cluster that ``repair_attempts_cluster``
    counts."""
    if not evidence or evidence.get("kind") != "ci" or not head_sha:
        return None
    ci = evidence.get("ci") or {}
    if head_sha not in (ci.get("head_sha"), ci.get("candidate_head_sha")):
        return None
    for job in ci.get("jobs") or []:
        name = str(job.get("name") or "")
        if job.get("executed") and job.get("conclusion") != SUCCESS \
                and not any(marker in name.lower() for marker in CREDENTIALLESS_JOB_MARKERS):
            return name or "unnamed job"
    for problem in evidence.get("limitations") or []:
        match = _RUNNER_LABEL_PROBLEM.match(str(problem))
        if match:
            return match.group(1)
    return None
