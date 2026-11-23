# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The trusted rerun of the failed or interrupted CI jobs of the exact head.

A rerun is an operation of kind ``ci_rerun`` of the operation table: the
runs it reruns and the attempt each was at are its marker
(``ci-rerun:<run>@<attempt>,...``), and the readback of a higher attempt of
every run on the expected head is its receipt. The run attempt is a
monotonic counter, so a retry first reads it: a rerun that already landed is
confirmed, never requested again, and a rerun operation is never left
``uncertain``.

Two paths use it, and no workflow of its own:

* the flaky path: a development worker reports ``no_change`` (a failure
  not caused by the patch); the trusted publication job of the worker run, or
  of its publish-only retry, checks the Result (``preflight``, no token),
  then requests ``rerun-failed-jobs`` with a token for the one fork that
  carries only ``actions: write`` and reads the new attempt back
  (``publish``). The confirmed operation is delivered in the outcome, and
  the reconciler takes ``request_ci_rerun`` only for an outcome that
  carries the receipt;
* the interrupted path: the CI observation in ``development.waiting_ci``
  or ``handoff.waiting_ci`` (``INTERRUPTED_STATES``) takes
  ``rerun_interrupted_ci`` and records the ``ci_rerun`` operation before
  the effect; the controller launches it through the development PR
  workflow in development, and through the ``ci-rerun`` job of the handoff
  workflow in handoff (``operation --phase``, after ``check`` before any
  token), and the reconciler reads the new attempt back before it closes
  the operation. In handoff the operation reruns CI on the clean head on
  ``riscv64`` (or on the approved candidate head on ``riscv64-dev`` when
  the reviewed series has its tree, ``handoff.ci_head``).

    python -m ci_rerun preflight --record R --task T --proposal P --out O
    python -m ci_rerun publish --record R --task T --out O
    python -m ci_rerun check --operation-id ID --phase handoff
    python -m ci_rerun operation --operation-id ID [--phase PHASE] --out O

The fork token comes from ``UPSTREAMING_FORK_TOKEN`` (``publish`` and
``operation``) and is used for the rerun request only; the fork is read with
the job's own token (``GH_TOKEN``). Annotation text and every other worker
or GitHub string is only compared against constants, never stored or
rendered.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

import ci_evidence
import collect
import engine
import lifecycle
import recovery
import schemas
from common import CONTROL_REPO, DEV_REF, FORK_ORG, UPSTREAM_REF, contribution_file_id

# The states whose CI observation reruns interrupted jobs
# (``rerun_interrupted_ci``), and the phase of the workflow that executes the
# operation of each.
INTERRUPTED_STATES = ("development.waiting_ci", "handoff.waiting_ci")
PHASES = {"development": "development.waiting_ci", "handoff": "handoff.waiting_ci"}
# The conclusions of a job that ``rerun-failed-jobs`` reruns.
RERUN_CONCLUSIONS = ("failure", "cancelled", "timed_out")
# The runs one request reruns at most.
MAX_RUNS = 5
# The readback after a rerun request: polls and the seconds between them.
READBACK_POLLS = 6
READBACK_SECONDS = 5
# The failure-level annotations GitHub writes on a job whose runner was lost
# (the runner service received a shutdown signal, or a hosted or self-hosted
# runner lost communication with the server). Anchored: a message matches
# only from its start.
RUNNER_LOST = (
    re.compile(r"The runner has received a shutdown signal\."),
    re.compile(r"The self-hosted runner: [^\n]{1,200}? lost communication with the server\."),
    re.compile(r"The hosted runner: [^\n]{1,200}? lost communication with the server\."),
)
# The statuses of a rerun receipt.
STATUSES = ("rerun", "superseded", "nothing_to_rerun", "stale", "refused", "unconfirmed")
# The statuses of a flaky receipt that let `request_ci_rerun` be taken.
ANSWERED = ("rerun", "superseded", "nothing_to_rerun")
# The Work of `development.agent_running` whose `no_change` Result may take
# `request_ci_rerun` (the row refuses Work that carries feedback): the
# reconciler requires the receipt for exactly the Results this admits.
DEVELOPMENT_WRITE_TASKS = ("development_patch", "development_repair")
_MARKER = re.compile(r"^ci-rerun:([0-9]{1,20}@[0-9]{1,6}(?:,[0-9]{1,20}@[0-9]{1,6}){0,4})$")
_EVIDENCE = re.compile(r"^ci-[0-9]+-([0-9]+)-([0-9]+)$")
_RUN_PATH = re.compile(r"^/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+/rerun-failed-jobs$")


class RerunError(ValueError):
    pass


def runner_lost(message: Any) -> bool:
    """Whether an annotation message is one of ``RUNNER_LOST``."""
    return isinstance(message, str) and any(p.match(message) for p in RUNNER_LOST)


# --------------------------------------------------------------------------
# What to rerun


def snapshot(api: collect.Api, repository: str, head: str) -> collect.CiSnapshot:
    """The workflow runs on ``head`` and the jobs of their latest attempts:
    what ``targets`` reads. No check run, status, or artifact is read."""
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise RerunError("head must be a full SHA")
    snap = collect.CiSnapshot(repository={"full_name": repository}, head_sha=head)
    runs = api.paginate(f"/repos/{repository}/actions/runs?head_sha={head}&per_page=50",
                        key="workflow_runs", max_pages=2)
    snap.runs = [collect._norm_run(r) for r in runs][:30]
    for r in ci_evidence.latest_attempts(snap.runs):
        jobs = api.paginate(
            f"/repos/{repository}/actions/runs/{r['id']}/attempts/{r['run_attempt']}/jobs"
            "?per_page=100", key="jobs", max_pages=5)
        snap.jobs_by_run[r["id"]] = [collect._norm_job(j) for j in jobs]
    return snap


def targets(snap: collect.CiSnapshot, head: str, *, interrupted_only: bool
            ) -> tuple[list[dict], bool]:
    """The latest attempts of the completed runs on exactly ``head`` that
    hold a job to rerun (a failed, cancelled, or timed-out job that is not
    a publishing job the fork has no credentials for; with
    ``interrupted_only`` an interrupted one, ``ci_evidence.job_interrupted``),
    lowest run id first, at most ``MAX_RUNS``, each as ``{run_id,
    from_attempt}``, and whether a run on the head is still in progress."""
    latest = [r for r in ci_evidence.latest_attempts(snap.runs) if r.get("head_sha") == head]
    in_progress = any(r.get("status") != "completed" for r in latest)
    out = []
    for run in sorted(latest, key=lambda r: r["id"]):
        if run.get("status") != "completed":
            continue
        jobs = snap.jobs_by_run.get(run["id"]) or []
        if interrupted_only:
            wanted = any(ci_evidence.job_interrupted(j) for j in jobs)
        else:
            wanted = any(j.get("status") == "completed" and j.get("conclusion") in RERUN_CONCLUSIONS
                         and not any(m in str(j.get("name") or "").lower()
                                     for m in ci_evidence.CREDENTIALLESS_JOB_MARKERS)
                         for j in jobs)
        if wanted:
            out.append({"run_id": int(run["id"]), "from_attempt": int(run["run_attempt"])})
    return out[:MAX_RUNS], in_progress


def answered_run(record: dict) -> tuple[int, int] | None:
    """The run and attempt of the CI evidence the record names
    (``last_ci_evidence_id``), or ``None``."""
    match = _EVIDENCE.match(str(record.get("last_ci_evidence_id") or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


# --------------------------------------------------------------------------
# The effect


class Api(collect.Api):
    """The fork API of a rerun: the one write ``rerun-failed-jobs`` with the
    fork token, and reads through ``reader`` (the job's own token, which
    reads the public fork) when one is given, else with the fork token."""

    def __init__(self, token: str | None = None, *, reader: collect.Api | None = None,
                 **kwargs: Any):
        super().__init__(token=token, **kwargs)
        self.reader = reader

    def get(self, path: str) -> Any:
        return self.reader.get(path) if self.reader is not None else super().get(path)

    def paginate(self, path: str, *, key: str | None = None, max_pages: int = 10) -> list:
        if self.reader is not None:
            return self.reader.paginate(path, key=key, max_pages=max_pages)
        return super().paginate(path, key=key, max_pages=max_pages)

    def mutate(self, method: str, path: str) -> None:
        if method != "POST" or ".." in path or not _RUN_PATH.match(path):
            raise RerunError("unsupported CI rerun API operation")
        req = urllib.request.Request(self.base + path, data=b"", method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Accept": "application/vnd.github+json",
                                              "User-Agent": "riseproject-porting-agent",
                                              "X-GitHub-Api-Version": "2022-11-28"})
        with self._open(req, timeout=30) as response:
            response.read(64 * 1024)


def read_run(api: collect.Api, repository: str, run_id: int) -> dict:
    run = api.get(f"/repos/{repository}/actions/runs/{int(run_id)}")
    if not isinstance(run, dict) or run.get("id") != run_id:
        raise collect.CollectError(f"run {run_id}: unexpected response")
    return {"id": run["id"], "head_sha": run.get("head_sha"),
            "run_attempt": int(run.get("run_attempt") or 1), "status": run.get("status")}


def request(api: Api, repository: str, head: str, runs: list[dict], *,
            sleep: Callable[[float], None] = time.sleep) -> dict:
    """Rerun the failed jobs of each run of ``runs`` on exactly ``head``.

    Each run is read first: an attempt above ``from_attempt`` on ``head`` is
    a rerun that already landed (no request), a run on another head is
    ``stale``; otherwise ``rerun-failed-jobs`` is requested and the run read
    again until its attempt is above ``from_attempt`` on ``head``. The
    receipt is ``{status, runs: [{run_id, from_attempt, run_attempt}]}``
    with status ``rerun`` (every run read back), ``stale``, ``refused`` (a
    definite refusal: HTTP 403, 404, or 422), or ``unconfirmed`` (any other
    failure, or an attempt that was not read back)."""
    done: list[dict] = []

    def receipt(status: str) -> dict:
        return {"status": status, "runs": done}

    for item in runs[:MAX_RUNS]:
        run_id, before = int(item["run_id"]), int(item["from_attempt"])
        try:
            run = read_run(api, repository, run_id)
            if run["head_sha"] != head:
                return receipt("stale")
            if run["run_attempt"] <= before:
                try:
                    api.mutate("POST", f"/repos/{repository}/actions/runs/{run_id}"
                                       "/rerun-failed-jobs")
                except urllib.error.HTTPError as error:
                    if error.code in (403, 404, 422):
                        return receipt("refused")
                    return receipt("unconfirmed")
                for _ in range(READBACK_POLLS):
                    sleep(READBACK_SECONDS)
                    run = read_run(api, repository, run_id)
                    if run["head_sha"] != head or run["run_attempt"] > before:
                        break
                if run["head_sha"] != head:
                    return receipt("stale")
                if run["run_attempt"] <= before:
                    return receipt("unconfirmed")
        except (collect.CollectError, urllib.error.URLError, OSError, ValueError, KeyError):
            return receipt("unconfirmed")
        done.append({"run_id": run_id, "from_attempt": before, "run_attempt": run["run_attempt"]})
    return receipt("rerun")


# --------------------------------------------------------------------------
# The operation


def marker(runs: Iterable[dict]) -> str:
    return "ci-rerun:" + ",".join(f"{int(r['run_id'])}@{int(r['from_attempt'])}" for r in runs)


def marker_runs(op: dict) -> list[dict]:
    """The runs a ``ci_rerun`` operation reruns, from its marker."""
    match = _MARKER.match(str((op.get("proposed") or {}).get("marker") or ""))
    if op.get("kind") != "ci_rerun" or not match:
        raise RerunError("not a ci_rerun operation marker")
    return [{"run_id": int(run), "from_attempt": int(attempt)}
            for run, attempt in (part.split("@") for part in match.group(1).split(","))]


def target_ref(record: dict, head: str) -> str:
    """The branch the CI of ``head`` runs on: ``riscv64`` for the clean head
    in handoff, else ``riscv64-dev`` (the development head, or the approved
    candidate head whose tree the clean head has)."""
    if record.get("phase") == "handoff" and head == record.get("clean_head_sha"):
        return UPSTREAM_REF
    return DEV_REF


def expected_head(record: dict, evidence: dict) -> str | None:
    """The head the CI a ``ci_rerun`` operation of ``record`` reruns is on:
    the development head, or in ``handoff.waiting_ci`` the head whose CI is
    CI on the clean head (``handoff.ci_head``, from the submission review
    in ``evidence``)."""
    if record.get("phase") == "handoff":
        import handoff

        if not record.get("clean_head_sha"):
            return None
        return handoff.ci_head(record, evidence.get(record.get("submission_review_id") or "")
                               or {})
    return (record.get("fork") or {}).get("head_sha")


def operation(record: dict, head: str, runs: list[dict], reason_ids: list[str], policy: dict,
              now: str, *, ref: str = DEV_REF) -> dict:
    """The planned ``ci_rerun`` operation of ``runs`` on ``head`` of the
    record's fork, on the branch ``ref``."""
    if not runs:
        raise RerunError("a rerun names at least one run")
    fork = record["fork"]
    return recovery.plan(
        contribution_id=record["contribution_id"], revision=record["revision"], kind="ci_rerun",
        policy_version=policy["policy_version"], reason_ids=reason_ids,
        target={"repository": fork["name"], "repository_id": fork.get("repository_id"),
                "ref": ref, "expected_head": head},
        proposed={"marker": marker(runs)}, now=now)


def attempt_ids(runs: list[dict]) -> dict[str, int]:
    """The attempts read back, as ``external_ids``."""
    return {f"attempt:{int(r['run_id'])}": int(r["run_attempt"]) for r in runs}


def discover(op: dict, runs: list[dict]) -> str:
    """``confirmed`` when every run of the operation's marker shows an
    attempt above the one it was at, on the expected head, in ``runs`` (each
    ``{id, run_attempt, head_sha}``); ``absent`` otherwise. Absence never
    proves that nothing was rerun."""
    seen = {r.get("id"): r for r in runs}
    for item in marker_runs(op):
        run = seen.get(item["run_id"])
        if not run or run.get("head_sha") != op["target"].get("expected_head") \
                or int(run.get("run_attempt") or 0) <= item["from_attempt"]:
            return "absent"
    return "confirmed"


def awaited(operations: dict, contribution_id: str, head: str, attempts: dict) -> bool:
    """Whether a confirmed ``ci_rerun`` operation of this contribution on
    ``head`` recorded an attempt of a run that ``attempts`` (the latest
    attempt of each run of a CI snapshot, by run id) shows older: the
    snapshot is behind the rerun, and is read again. A run the snapshot does
    not show is not awaited."""
    for op in operations.values():
        if op.get("kind") != "ci_rerun" or op.get("contribution_id") != contribution_id \
                or op.get("status") != "confirmed" \
                or (op.get("target") or {}).get("expected_head") != head:
            continue
        for key, value in (op.get("external_ids") or {}).items():
            if not key.startswith("attempt:") or not isinstance(value, int):
                continue
            seen = attempts.get(int(key.split(":", 1)[1]))
            if seen is not None and seen < value:
                return True
    return False


def receipt_problem(outcome: dict, record: dict) -> str | None:
    """Why the outcome of a ``no_change`` Result carries no valid flaky
    rerun receipt for ``record``, or ``None``."""
    rerun = outcome.get("rerun")
    if outcome.get("stage") != "ci_rerun" or not isinstance(rerun, dict):
        return "the Result carries no CI rerun receipt"
    if rerun.get("status") not in ANSWERED or not isinstance(rerun.get("runs"), list):
        return "the CI rerun receipt is not a rerun"
    if len(rerun["runs"]) > MAX_RUNS or not all(
            isinstance(r, dict) and all(type(r.get(k)) is int and r[k] >= 0
                                        for k in ("run_id", "from_attempt", "run_attempt"))
            and r["run_attempt"] > r["from_attempt"] for r in rerun["runs"]):
        return "the CI rerun receipt names a run it does not describe"
    if rerun["status"] != "rerun":
        return None if not rerun["runs"] else "the CI rerun receipt names runs it did not rerun"
    op = outcome.get("operation") or {}
    try:
        schemas.validate("operation", op)
        runs = marker_runs(op)
    except (schemas.SchemaError, RerunError):
        return "the CI rerun receipt names no ci_rerun operation"
    fork = record.get("fork") or {}
    expected = {f"attempt:{r['run_id']}": r["run_attempt"] for r in rerun["runs"]}
    if op["status"] != "confirmed" or op["contribution_id"] != record["contribution_id"] \
            or op["target"].get("repository") != fork.get("name") \
            or op["target"].get("expected_head") != fork.get("head_sha") \
            or [(r["run_id"], r["from_attempt"]) for r in runs] \
            != [(r["run_id"], r["from_attempt"]) for r in rerun["runs"]] \
            or any((op.get("external_ids") or {}).get(k) != v for k, v in expected.items()) \
            or len(expected) != len(runs):
        return "the CI rerun operation does not match the receipt or the record"
    return None


# --------------------------------------------------------------------------
# The flaky path: the trusted publication job


def preflight(*, record: dict, task: dict, proposal: dict, policy: dict, now: str,
              fork_lookup: Callable[[str], dict]) -> dict:
    """Before any token: whether the publication job reruns the failed jobs
    for this ``no_change`` Result, and the one fork to mint a token for."""
    import fork_publish

    def no(reason: str) -> dict:
        return {"rerun": False, "fork_name": "", "reason": reason}

    if task.get("task") not in DEVELOPMENT_WRITE_TASKS or proposal.get("outcome") != "no_change":
        return no("not a no_change Result of development Work")
    try:
        fork_publish.check_proposal(proposal, record, task, allow_no_change=True)
    except (fork_publish.PublishRejected, schemas.SchemaError) as error:
        return no(f"proposal check: {error}")
    if policy.get("read_only"):
        return no("policy is read-only")
    why = engine.refusal(record, "request_ci_rerun", policy=policy, now=now,
                         facts={"failure_not_caused_by_patch"})
    if why:
        return no(f"request_ci_rerun does not apply: {why}")
    try:
        repo = fork_lookup(task["fork"])
    except Exception as error:  # noqa: BLE001 - any read failure is reported
        return no(f"could not read the fork: {type(error).__name__}")
    if repo.get("id") != record["fork"]["repository_id"] \
            or not str(repo.get("full_name", "")).startswith(FORK_ORG + "/") \
            or repo.get("full_name") != task["fork"] or repo.get("archived"):
        return no("fork repository identity changed")
    return {"rerun": True, "fork_name": repo["full_name"].split("/", 1)[1], "reason": None}


def _outcome(status: str, reason: str, *, failure_class: str | None = None,
             rerun: dict | None = None, op: dict | None = None) -> dict:
    out: dict[str, Any] = {"status": status, "failure_class": failure_class,
                           "reason": reason, "stage": "ci_rerun"}
    if rerun is not None:
        out["rerun"] = rerun
    if op is not None:
        out["operation"] = op
    return out


def publish(*, record: dict, task: dict, api: Api, policy: dict, now: str,
            sleep: Callable[[float], None] = time.sleep) -> dict:
    """Rerun the failed jobs of the exact head for a ``no_change`` Result,
    after the final check before a fork write (``lifecycle.publication_check``),
    and return the outcome the reconciler folds."""
    from work import task_identity

    fork, head = task["fork"], task["expected_head"]
    try:
        observed = (collect.branch_head(api, fork, task.get("ref")) or {}).get("head")
    except collect.CollectError as error:
        return _outcome("infra_error", f"could not read the fork head: {error}",
                        failure_class="infrastructure")
    generation, work_key = task_identity(task)
    if generation is None:
        return _outcome("rejected", "publication check: the task names no Work identity",
                        failure_class="stale")
    why = lifecycle.publication_check(record, expected_revision=task["revision"],
                                      expected_head=head, observed_head=observed,
                                      ref=task.get("ref"), repository=fork,
                                      expected_generation=generation, work_key=work_key)
    if why:
        return _outcome("rejected", f"publication check: {why}", failure_class="stale")
    try:
        snap = snapshot(api, fork, head)
    except (collect.CollectError, RerunError) as error:
        return _outcome("infra_error", f"could not read CI on the head: {error}",
                        failure_class="infrastructure")
    runs, _in_progress = targets(snap, head, interrupted_only=False)
    answered = answered_run(record)
    newer = answered is not None and any(
        r["id"] == answered[0] and r["head_sha"] == head and r["run_attempt"] > answered[1]
        for r in snap.runs)
    # CI is already running again when the run the record answered has a
    # newer attempt (that run is not rerun again), or a run on the head is
    # in progress on a later attempt (a rerun of a run that held a failed
    # job), and no other completed run on the head still holds a failed job:
    # an earlier attempt of this publication that reran only some of the
    # runs is completed by rerunning the others. Another run still in
    # progress (a fresh or unrelated workflow) does not hold the failed jobs
    # back: the completed failed runs are rerun.
    if newer:
        runs = [r for r in runs if r["run_id"] != answered[0]]
    rerunning = any(r.get("head_sha") == head and r.get("status") != "completed"
                    and int(r.get("run_attempt") or 1) > 1
                    for r in ci_evidence.latest_attempts(snap.runs))
    if (newer or rerunning) and not runs:
        return _outcome("no_change", "CI on the head is already running again",
                        rerun={"status": "superseded", "runs": []})
    if not runs:
        return _outcome("no_change", "no failed job to rerun on the latest attempts",
                        rerun={"status": "nothing_to_rerun", "runs": []})
    receipt = request(api, fork, head, runs, sleep=sleep)
    reason_ids = [str(record.get("last_ci_evidence_id") or "no-ci-evidence"),
                  f"work:{work_key or task.get('revision')}",
                  f"publish-attempt:{max(int(os.environ.get('UPSTREAMING_PUBLISH_ATTEMPT', '1')), 1)}"]
    op = operation(record, head, runs, reason_ids, policy, now)
    if receipt["status"] == "rerun":
        op = recovery.fold_receipt(op, external_ids=attempt_ids(receipt["runs"]),
                                   detail=f"new attempts read back on {head}", now=now)
        return _outcome("no_change", "the failed jobs are rerun on the exact head",
                        rerun=receipt, op=op)
    if receipt["status"] == "stale":
        return _outcome("rejected", "the run is no longer on the expected head",
                        failure_class="stale", rerun=receipt)
    if receipt["status"] == "refused":
        op = recovery.fold_failure_receipt(op, external_ids={}, now=now)
        return _outcome("infra_error", "GitHub refused the rerun", failure_class="infrastructure",
                        rerun=receipt, op=op)
    return _outcome("infra_error", "the new attempt was not read back",
                    failure_class="infrastructure", rerun=receipt)


# --------------------------------------------------------------------------
# The interrupted path: the operation the collector planned


def load_request(root: Path, operation_id: str, policy: dict, *,
                 phase: str = "development") -> tuple[dict, dict]:
    """The planned ``ci_rerun`` operation and its record, when the operation
    is the record's current authorized intent in the state of ``phase``
    (``PHASES``) and targets the head and branch the CI of that state is
    on."""
    if phase not in PHASES:
        raise RerunError("unknown CI rerun phase")
    if not re.fullmatch(r"op-[0-9a-f]{24}", operation_id):
        raise RerunError("invalid operation ID")
    op = json.loads((root / ".state/operations" / f"{operation_id}.json").read_text())
    schemas.validate("operation", op)
    cid = contribution_file_id(op["contribution_id"])
    record = json.loads((root / ".state/contributions" / f"{cid}.json").read_text())
    schemas.validate("contribution", record)
    fork = record.get("fork") or {}
    if op["kind"] != "ci_rerun" or op["status"] != "planned" \
            or engine.state_key(record) != PHASES[phase] \
            or record.get("active_operation_id") != operation_id \
            or record["revision"] != op["revision"] or record.get("paused") \
            or policy.get("read_only"):
        raise RerunError("operation is not the current authorized CI rerun intent")
    if op["target"].get("repository") != fork.get("name") \
            or op["target"].get("repository_id") != fork.get("repository_id"):
        raise RerunError("CI rerun target does not match the recorded fork")
    if phase == "handoff":
        review_id = record.get("submission_review_id") or ""
        path = root / ".state/evidence" / f"{review_id}.json"
        review = json.loads(path.read_text()) if review_id and path.is_file() else {}
        head = expected_head(record, {review_id: review})
        if not head or op["target"].get("expected_head") != head \
                or op["target"].get("ref") != target_ref(record, head):
            raise RerunError("CI rerun target is not CI on the clean head")
    elif op["target"].get("ref") != DEV_REF:
        raise RerunError("CI rerun target does not match the recorded fork")
    marker_runs(op)
    return record, op


def run_operation(root: Path, operation_id: str, api: Api, policy: dict, run_id: str, *,
                  phase: str = "development",
                  sleep: Callable[[float], None] = time.sleep) -> dict:
    """Execute a planned ``ci_rerun`` operation and return its receipt.

    The stale fence before the request: in development the development head
    is the recorded head and ``riscv64-dev`` still resolves to it; in
    handoff ``riscv64`` still resolves to the clean head (the guard of the
    observation), and a moved ``riscv64-dev`` does not block it. Every run
    is read on the expected head before it is rerun (``request``)."""
    record, op = load_request(root, operation_id, policy, phase=phase)
    fork = op["target"]["repository"]
    head = op["target"]["expected_head"]
    base = {"schema_version": 1, "operation_id": operation_id,
            "contribution_id": record["contribution_id"], "revision": record["revision"],
            "repository": fork, "repository_id": op["target"]["repository_id"],
            "head_sha": head, "ci_rerun_run_id": int(run_id)}
    if phase == "development" and head != record["fork"].get("head_sha"):
        return base | {"status": "stale", "runs": []}
    repo = collect.repository(api, fork)
    if repo["id"] != op["target"]["repository_id"] \
            or repo.get("parent_id") != record["upstream"]["repository_id"]:
        raise RerunError("fork identity changed")
    if phase == "handoff":
        observed = collect.branch_head(api, fork, UPSTREAM_REF)
        fence = record["clean_head_sha"]
    else:
        observed = collect.branch_head(api, fork, DEV_REF)
        fence = head
    if not observed or observed["head"] != fence:
        return base | {"status": "stale", "runs": []}
    return base | request(api, fork, head, marker_runs(op), sleep=sleep)


def _trusted_workflow() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != CONTROL_REPO \
            or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise RerunError("CI reruns require the trusted control workflow on main")


def main(argv: list[str] | None = None) -> int:
    import argparse
    from datetime import datetime, timezone

    from common import load_policy

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["preflight", "publish", "check", "operation"])
    ap.add_argument("--record", type=Path)
    ap.add_argument("--task", type=Path)
    ap.add_argument("--proposal", type=Path)
    ap.add_argument("--operation-id")
    ap.add_argument("--phase", choices=sorted(PHASES), default="development")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    policy = load_policy()
    now = datetime.now(timezone.utc).isoformat()
    if a.cmd == "check":
        # Before the handoff job mints its token: the checked-out state is
        # the head of main and still runs this operation. A refusal writes
        # no receipt (it can be transient); the launch budget relaunches the
        # operation, then closes it.
        import handoff
        import isolation

        _trusted_workflow()
        try:
            handoff.current_state(Path.cwd(), collect.Api(token=os.environ.get("GH_TOKEN") or None))
            load_request(Path.cwd(), a.operation_id or "", policy, phase=a.phase)
        except (handoff.HandoffRefused, RerunError, OSError, ValueError,
                schemas.SchemaError) as error:
            print(json.dumps({"operation_id": a.operation_id, "result": "refused",
                              "reason": isolation.clean_text(error, 200)}))
            return 1
        return 0
    if a.out is None:
        ap.error("--out is required")
    if a.cmd == "operation":
        _trusted_workflow()
        token = os.environ.pop("UPSTREAMING_FORK_TOKEN", "")
        result = run_operation(Path.cwd(), a.operation_id or "",
                               Api(token=token, reader=collect.Api()), policy,
                               os.environ["GITHUB_RUN_ID"], phase=a.phase)
        code = 0
    elif a.cmd == "preflight":
        result = preflight(record=json.loads(a.record.read_text()),
                           task=json.loads(a.task.read_text()),
                           proposal=json.loads(a.proposal.read_text()), policy=policy, now=now,
                           fork_lookup=lambda name: collect.repository(collect.Api(), name))
        code = 0
    else:
        token = os.environ.pop("UPSTREAMING_FORK_TOKEN", "")
        result = publish(record=json.loads(a.record.read_text()),
                         task=json.loads(a.task.read_text()),
                         api=Api(token=token, reader=collect.Api()), policy=policy, now=now)
        result["attempt"] = max(int(os.environ.get("UPSTREAMING_PUBLISH_ATTEMPT", "1")), 1)
        code = {"no_change": 0, "infra_error": 3}.get(result["status"], 1)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
