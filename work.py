# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Native work-queue plans for upstreaming tasks (plan sections 5.1, 15.1).

``controller.tick`` produces ``submit_work`` requests. This module turns
them into the immutable gh-aw Work payload and a staged plan for the
dispatcher (``{"version": 1, "nodes": [...], "dispatch": {...}}``, the
format accepted by gh-aw's ``stagePlanIntents``).

Effect contracts. gh-aw v1.2.2 verifies only a fixed set of output types
inside a queue Claim; a branch push to another repository is not one of
them (``push_to_pull_request_branch`` has no Claim verifier). So the
agent's only resource output is ``upload_artifact`` (verified by the
Actions artifact API), carrying the proposal and patch series. The trusted
``publish`` job of the worker workflow reads that artifact after the Claim
is verified and performs the fork push itself, recorded as a pipeline
operation. Planning runs are no-write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import commit_rules
import lifecycle
import work_history
from common import (
    CONTROL_REPO,
    STATE_ROOT,
    canonical_json,
    load_policy,
    repository_root,
)

POOL = "default"
# Three worker profiles in one pool: planning runs (read and report, cheaper
# model, no publish job), patch work (stronger model, trusted publish job),
# and the review of a held development patch (its own model, read and diff
# only, a publish job that only checks the review).
WORKER_PROFILE = "worker"
PLANNER_PROFILE = "planner"
REVIEWER_PROFILE = "reviewer"
PROFILE_WORKFLOWS = {WORKER_PROFILE: ".github/workflows/worker.lock.yml",
                     PLANNER_PROFILE: ".github/workflows/planner.lock.yml",
                     REVIEWER_PROFILE: ".github/workflows/reviewer.lock.yml"}
PROPOSAL_ARTIFACT = "porting-out"

WRITE_TASKS = {"development_patch", "development_repair", "upstream_revision"}
# The two Work tasks at submission, run one after the other in fresh
# contexts: the rewrite reshapes the series and writes the pull request
# text; the review reviews exactly that series and text and can change
# nothing. Both are uncredentialed like every Work and write nothing: the
# rewrite is checked by trusted code in the worker's publish job without a
# token, and only the trusted handoff workflow builds `riscv64` from it.
SUBMISSION_REWRITE = "submission_rewrite"
SUBMISSION_REVIEW = "submission_review"
SUBMISSION_TASKS = (SUBMISSION_REWRITE, SUBMISSION_REVIEW)
# The review of a held development patch: it reads and diffs exactly the
# patch the worker's trusted publication job verified and held, and writes
# nothing. Development patch Work carries the review contract (``review``),
# so its worker run verifies the patch and never pushes it.
DEVELOPMENT_REVIEW = "development_review"
REVIEWED_TASKS = ("development_patch", "development_repair")
ALLOWED_OPERATIONS = {
    "development_patch": ["append", "rewrite"],
    "development_repair": ["append", "rewrite"],
    "upstream_revision": ["append"],   # history policy may widen this per project
    "planning": [],
    SUBMISSION_REWRITE: [],
    SUBMISSION_REVIEW: [],
    DEVELOPMENT_REVIEW: [],
}

# Diagnostic no-write outputs are always allowed alongside.
_DIAG = [{"type": t, "min": 0, "max": 1} for t in ("noop", "report_incomplete", "report_missing_tool",
                                                   "report_missing_data")]


def effect_contract(task: str) -> dict:
    if task == "planning":
        # The plan comes back as an artifact too, but planning runs must
        # not need a write-capable Claim: they report via artifact only.
        return {"version": 1, "outputs": _DIAG + [{"type": "upload_artifact", "min": 1, "max": 1}]}
    if task in WRITE_TASKS or task in SUBMISSION_TASKS or task == DEVELOPMENT_REVIEW:
        return {"version": 1, "outputs": _DIAG + [{"type": "upload_artifact", "min": 1, "max": 1}]}
    raise ValueError(f"unknown task {task}")


def payload(request: dict, record: dict, *, control_repo_id: str, allowed_paths: list[str],
            context: dict | None = None) -> dict:
    """Immutable Work payload for one controller request. Everything the
    agent and the trusted publish job need is in here; nothing is looked up
    from mutable state at execution time except re-checks."""
    task = request["task"]
    context = dict(context or {})
    issue = (record.get("active_work") or {}).get("tracking_issue")
    if issue is not None:
        context["tracking_issue"] = issue
    if task == "planning" and record.get("refresh"):
        context["refresh"] = dict(record["refresh"])
        context["existing_fork"] = dict(record.get("fork") or {})
    fork = record.get("fork") or {}
    up = record["upstream"]
    p = {
        "task": task,
        "instructions": _INSTRUCTIONS[task],
        "contribution_id": record["contribution_id"],
        "revision": record["revision"],
        "tracking_issue": record["tracking_issue"]["number"],
        "upstream": up["full_name"],
        "upstream_repository_id": up.get("repository_id"),
        "base_ref": up.get("base_ref"),
        "base_sha": up.get("base_sha"),
        "fork": fork.get("name"),
        "fork_name": (fork.get("name") or "/").split("/", 1)[1] or None,
        "fork_repository_id": fork.get("repository_id"),
        "ref": lifecycle.work_ref(record),
        "expected_head": lifecycle.recorded_head(record),
        "upstream_facing": record.get("upstream_pr") is not None,
        "allowed_operations": ALLOWED_OPERATIONS[task],
        "allowed_paths": allowed_paths,
        "review_scope_extension": task in ("development_repair", "upstream_revision") and any(
            item.get("kind") == "human_relay" for item in context.get("reason_evidence") or []),
        "legacy_scope_extension": task in ("development_repair", "upstream_revision")
        and record.get("scope_coverage_complete") is not True,
        "distributions": record.get("distributions") or [],
        "admission_class": record.get("admission_class"),
        "reason_ids": request.get("reason_ids") or [],
        "context": context or {},
        "resource_scope": {"version": 1, "resources": [
            {"host": "github.com", "repository": CONTROL_REPO, "repository_id": str(control_repo_id)}]},
        "effect_contract": effect_contract(task),
    }
    if task in REVIEWED_TASKS:
        # The review contract: the patch is held for its review and published
        # only after it (``fork_publish.review_required``).
        p["review"] = "required"
    p.update(_identity(request))
    return p


def _identity(request: dict) -> dict:
    """The Work identity a payload carries: a Result is folded only when the
    generation, the revision, and the Work id it carries back match the
    requested Work. The Work also carries the pending feedback ids recorded
    when it was allocated, which the published patch answers."""
    return {"generation": request["generation"], "dedupe_key": request["dedupe_key"],
            "feedback_evidence_ids": list(request.get("feedback_evidence_ids") or [])}


def profile_for(task: str) -> str:
    if task == "planning":
        return PLANNER_PROFILE
    if task == DEVELOPMENT_REVIEW:
        return REVIEWER_PROFILE
    if task in WRITE_TASKS or task in SUBMISSION_TASKS:
        return WORKER_PROFILE
    raise ValueError(f"unknown task {task}")


def graph_id(dedupe_key: str) -> str:
    return "porting:" + hashlib.sha256(dedupe_key.encode()).hexdigest()[:32]


def work_key(contribution_id: str, generation: int, revision: int, task: str,
             reruns: int = 0) -> str:
    """The Work id of one allocation: the contribution, the generation, the
    revision, the flaky CI reruns requested for the head so far
    (``budget.reruns.flaky``, from the first one on), and the task. The
    queue graph is derived from it (``graph_id``), so Work of another
    generation or revision is another graph, and a terminal graph is never
    submitted again. ``request_ci_rerun`` keeps the revision, so the rerun
    count tells apart the repair Work requested after a rerun from the Work
    whose Result requested it."""
    rerun = f".rerun{reruns}" if reruns else ""
    return f"{contribution_id}@g{generation}.r{revision}{rerun}:{task}"


def task_identity(task: dict) -> tuple[int | None, str | None]:
    """The generation and Work id a Work payload (``task.json``) names.

    A payload names its Work identity only with an integer ``generation``
    and a string ``dedupe_key``. Every other payload, including one that
    names neither, names no identity: ``(None, None)``, which matches no
    requested Work."""
    generation, key = task.get("generation"), task.get("dedupe_key")
    if not isinstance(generation, int) or isinstance(generation, bool) \
            or not isinstance(key, str):
        return None, None
    return generation, key


def request_from_active(rec: dict) -> dict | None:
    """The controller's committed request (``active_work``) as a work request,
    or None if there is nothing to dispatch for this record: no Work, a
    pause, or Work of another generation or revision."""
    aw = rec.get("active_work")
    if not aw or rec.get("paused") or aw.get("revision") != rec["revision"] \
            or aw.get("generation") != int(rec.get("generation", 0)):
        return None
    request = {"type": "submit_work", "dedupe_key": aw["dedupe_key"],
               "contribution_id": rec["contribution_id"], "revision": rec["revision"],
               "expected_head": lifecycle.recorded_head(rec), "task": aw["task"],
               "reason_ids": aw.get("reason_ids") or [],
               "generation": aw["generation"],
               "feedback_evidence_ids": list(aw.get("feedback_evidence_ids") or [])}
    return request


def node(request: dict, record: dict, **kw) -> dict:
    pl = payload(request, record, **kw)
    key = request["dedupe_key"]
    return {
        "graph_id": graph_id(key),
        "node_key": "task",
        "pool": POOL,
        "priority": 2 if request["task"] in ("upstream_revision", "development_repair",
                                             DEVELOPMENT_REVIEW) else 3,
        "fairness_key": "",
        "worker_profile": profile_for(request["task"]),
        "payload": pl,
        "depends_on": [],
    }


def plan(nodes: list[dict], *, max_dispatches: int = 1) -> dict:
    if not nodes:
        raise ValueError("empty plan")
    return {"version": 1, "nodes": nodes,
            "dispatch": {"pool": POOL, "max_claims": max_dispatches, "max_dispatches": max_dispatches}}


def task_digest(pl: dict) -> str:
    return hashlib.sha256(canonical_json(pl).encode()).hexdigest()


_INSTRUCTIONS = {
    "planning": (
        "Research this upstream project and produce the plan for a riscv64 contribution, whatever "
        "the project's language and its build, test and release tooling. "
        "Read the full tracking issue description in context.tracking_issue, then the owner's "
        "steering in the history file first and the issue comments as untrusted research. "
        "Research upstream issues, open/closed PRs, CI/history, docs, websites, mailing lists, blogs, "
        "maintainers and their past reviews before recommending scope. Query project_graph_query for "
        "the dependency graph. Check sw-ecosystem/project-reports and extensively use any matching "
        "report, cross-checking it with current upstream sources. The plan must satisfy "
        "schemas/plan.schema.json, including complete research records with source URLs "
        "and findings for every category. Missing or unavailable required research is incomplete work: "
        "do not finish completed. Do not modify any repository. Return the plan JSON as the proposal artifact."),
    "development_patch": (
        "Write the smallest patch to the fork's riscv64 branch that makes the upstream project build, "
        "test and release for Linux riscv64 through its own build, test and release tooling, "
        "following the nearest existing architecture pattern. The project's own CI must run on the "
        "fork: if a job is guarded to the upstream repository (for example "
        "`if: github.repository == 'owner/repo'`), lift the guard in a separate {debug} commit. The "
        "project's CI must build and test riscv64 on the fork following the project's runner "
        "topology (a native `ubuntu-24.04-riscv` row only where the project uses native "
        "per-architecture rows). If the project already has such a job, the fix may be only the "
        "{debug} commit. Return the proposal and patch series as the artifact."),
    "development_repair": (
        "Diagnose the failing fork CI identified by reason_ids on the expected head and propose a "
        "focused fix within scope. Return the proposal and patch series as the artifact."),
    "upstream_revision": (
        "Address the current maintainer feedback/CI failures identified by reason_ids on the "
        "upstream-facing riscv64 branch. No {debug} commits. Return the proposal and series."),
    SUBMISSION_REWRITE: (
        "Prepare the upstream submission of the approved riscv64-dev series at expected_head. "
        "Reshape its non-debug commits on base_sha after the project's commit conventions "
        "(squash only where its contributing instructions or reviewers require it; do not split: "
        "report a split the project requires as a reason; keep the final tree identical and "
        "every intermediate tree a state of the approved series), and write the upstream PR title and description following the project's "
        "pull request template exactly and its contributing instructions. Change no code. "
        "Return the rewrite and the reshaped series as the artifact."),
    SUBMISSION_REVIEW: (
        "Review exactly the rewritten series and pull request text in context.rewrite, which "
        "the trusted pre-agent step downloaded and verified, against the project's pull "
        "request template, contributing instructions, commit conventions, and the reviewers' "
        "expectations. Report pass or fail with one reason per problem. Change nothing. "
        "Return the review as the artifact."),
    DEVELOPMENT_REVIEW: (
        "Review exactly the held development patch in context.patch, which the trusted "
        "pre-agent step downloaded, verified and rebuilt in ./fork on expected_head, before it "
        "is published. Read the worker's report, the verified plan, the reason evidence and the "
        "history first: the owner's steering comes first, then every earlier report and review. "
        "Check the patch for correctness, scope, the plan, the steering, the upstream "
        "conventions, the CI coverage of riscv64, its tests, security, and whether the report "
        "says what the patch does. Report every finding with its severity; a blocker or major "
        "finding fails the review. Run no reproduction and change nothing. Return the review "
        "as the artifact."),
}


def dumps(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _short(value, limit: int):
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _feedback_data(data: dict) -> dict:
    result = {}
    for key in ("reviews", "comments", "conversation_comments"):
        items = []
        for original in data.get(key) or []:
            item = {
                name: original.get(name)
                for name in (
                    "id",
                    "url",
                    "reviewer",
                    "commit_id",
                    "review_id",
                    "path",
                    "line",
                    "created_at",
                )
                if original.get(name) is not None
            }
            item["body"] = _short(original.get("body") or "", 1200)
            items.append(item)
        result[key] = items
    for key in ("review_ids", "comment_ids", "conversation_comment_ids"):
        result[key] = list(data.get(key) or [])
    return result


def _plan_data(data: dict) -> dict:
    proposal = data.get("proposal") or {}
    compact = {
        key: proposal.get(key)
        for key in (
            "decision",
            "admission_class",
            "allowed_paths",
            "architecture_precedent",
            "build_backend",
            "target_matrix",
            "fork_guards",
            "risks",
        )
        if proposal.get(key) is not None
    }
    for key in (
        "architecture_precedent",
        "build_backend",
        "target_matrix",
        "fork_guards",
        "risks",
    ):
        if key in compact:
            compact[key] = _short(compact[key], 800)
    compact["path_coverage"] = [
        {
            "area": item.get("area"),
            "paths": item.get("paths") or [],
            "assessment": _short(item.get("assessment") or "", 600),
        }
        for item in proposal.get("path_coverage") or []
    ]
    dependency = data.get("dependency_graph") or {}
    return {
        "proposal": compact,
        "dependency_graph": {
            "assessment": _short(dependency.get("assessment") or "", 1600)
        },
    }


def _plan_reference(root: Path, evidence_id: str, control_ref: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", evidence_id) or "/" in evidence_id:
        raise ValueError("invalid plan evidence ID")
    if not re.fullmatch(r"[0-9a-f]{40}", control_ref):
        raise ValueError("invalid immutable control ref")
    relative = Path(STATE_ROOT) / "evidence" / f"{evidence_id}.json"
    raw = (root / relative).read_bytes()
    return {
        "repository": CONTROL_REPO,
        "ref": control_ref,
        "path": relative.as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "url": (
            f"https://raw.githubusercontent.com/{CONTROL_REPO}/{control_ref}/"
            f"{relative.as_posix()}"
        ),
    }


# The bytes of a Work payload the queue accepts with a margin (its limit is
# 16384).
MAX_PAYLOAD_BYTES = 15_500
_HISTORY_PATH = re.compile(r"\.state/evidence/history-[0-9a-f]{32}\.json")


def payload_inputs(request: dict, record: dict, evidence: dict[str, dict], *,
                   plan_reference: Any) -> tuple[list[str], dict]:
    """The allowed paths and the context of the payload of ``request``, as
    the dispatcher builds them from the committed ``evidence``: the scope
    of the record (else the paths of its target contract), the reason
    evidence of the request (a plan named by ``plan_reference``, a callable
    of its evidence id), the commit rules and template sections of the
    verified plan, and the rewrite a review reviews. The history reference
    is added by the caller. Raises ``ValueError`` when a part cannot be
    built."""
    target = evidence.get(record.get("target_contract_id") or "") or {}
    contract = (target.get("data") or {}) if target.get("kind") == "target_contract" else {}
    plan_document = (evidence.get(record.get("plan_evidence_id") or "") or {}).get("data")
    rules = commit_rules.from_plan(plan_document) if request["task"] != "planning" else None
    sections = (commit_rules.sections_from_plan(plan_document)
                if request["task"] in SUBMISSION_TASKS else None)
    rewrite = (rewrite_reference(evidence.get(record.get("submission_rewrite_id") or ""))
               if request["task"] == SUBMISSION_REVIEW else None)
    requirements = (series_requirements_context(record, evidence)
                    if request["task"] in WRITE_TASKS else None)
    patch = held_patch_reference(record) if request["task"] == DEVELOPMENT_REVIEW else None
    previous = (previous_patch_reference(request, evidence,
                                         requested_at=record["active_work"]["requested_at"])
                if request["task"] in REVIEWED_TASKS else None)
    report = None
    if patch is not None and patch.get("report_evidence_id"):
        if patch["report_evidence_id"] not in evidence:
            raise ValueError("the report of the held patch is not committed")
        report = plan_reference(patch["report_evidence_id"])
        report.pop("url", None)
        report["evidence_id"] = patch["report_evidence_id"]
    reason_evidence = []
    for evidence_id in request.get("reason_ids") or []:
        item_evidence = evidence.get(evidence_id)
        if item_evidence:
            item = _reason_item(item_evidence)
            if item_evidence.get("kind") == "plan":
                item["reference"] = plan_reference(item_evidence["evidence_id"])
            reason_evidence.append(item)
    allowed_paths = (record.get("scope_paths") or contract.get("allowed_paths")
                     or [".github/workflows/*"])
    context = {
        "requested_at": record["active_work"]["requested_at"],
        "reason_evidence": reason_evidence,
        # The measurable commit conventions of the verified plan, which the
        # trusted worker gate checks. A plan of schema version 1 records
        # none.
        **({"commit_rules": rules} if rules is not None else {}),
        # The required sections of the pull request template, which the
        # submission check requires in the reviewed description.
        **({"template_sections": sections} if sections is not None else {}),
        # The rewrite a review reviews: the trusted pre-agent step downloads
        # its series and text from this artifact and checks them against
        # these digests.
        **({"rewrite": rewrite} if rewrite is not None else {}),
        # The unmet series requirements of the plan this Work answers, which
        # the trusted worker gate checks.
        **({"series_requirements": requirements} if requirements is not None else {}),
        # The held patch a review reviews: the trusted pre-agent step
        # downloads its series from this artifact, checks it against this
        # digest, and rebuilds it to this commit; and the worker's report,
        # at the pinned commit with its SHA-256.
        **({"patch": patch} if patch is not None else {}),
        **({"report": report} if report is not None else {}),
        # The patch the newest review of this Work's feedback rejected: the
        # trusted pre-agent step downloads its series from this artifact and
        # checks it against this digest, so the worker amends that patch.
        **({"previous_patch": previous} if previous is not None else {}),
    }
    return allowed_paths, context


# The digits of the longest GitHub repository id (a signed 64-bit integer,
# at most 9223372036854775807).
CONTROL_REPO_ID_MAX_DIGITS = 19


def payload_floor(record: dict, evidence: dict[str, dict] | None = None) -> int | None:
    """The fewest bytes the payload of the record's requested Work can take
    (as ``cmd_plan`` measures it): the payload built from the committed
    ``evidence`` as the dispatcher builds it (``payload_inputs``: the
    allowed paths, the reason evidence, the commit rules), with references
    to the history and the plan of their exact size and the control
    repository id at its longest (``CONTROL_REPO_ID_MAX_DIGITS``: the
    dispatcher passes the real id, which the controller does not read), so
    the floor is never below the payload the dispatcher builds. ``None`` when the
    record holds no Work it could dispatch, or the payload cannot be built.
    The controller names it when a dispatch fails, so that a payload over
    ``MAX_PAYLOAD_BYTES`` (a long frozen tracking issue description) is a
    visible cause."""
    request = request_from_active(record)
    if request is None:
        return None
    zero = "0" * 40
    history = {"repository": CONTROL_REPO, "ref": zero,
               "path": f".state/evidence/history-{'0' * 32}.json", "sha256": "0" * 64,
               "evidence_id": f"history-{'0' * 32}"}

    def plan_reference(evidence_id: str) -> dict:
        path = f"{STATE_ROOT}/evidence/{evidence_id}.json"
        return {"repository": CONTROL_REPO, "ref": zero, "path": path, "sha256": "0" * 64,
                "url": f"https://raw.githubusercontent.com/{CONTROL_REPO}/{zero}/{path}"}
    try:
        allowed_paths, context = payload_inputs(request, record, evidence or {},
                                                plan_reference=plan_reference)
        if "generation" in request:
            context["history"] = history
        built = payload(request, record, control_repo_id="9" * CONTROL_REPO_ID_MAX_DIGITS,
                        allowed_paths=allowed_paths, context=context)
        return len(json.dumps(built, separators=(",", ":"), ensure_ascii=False).encode())
    except (KeyError, TypeError, ValueError):
        return None


def _history_reference(root: Path, evidence_id: str, control_ref: str) -> dict:
    """The history of a Work as its payload names it: the committed
    ``work_history`` evidence at the pinned control commit, with its
    SHA-256, which the trusted pre-agent step fetches and verifies."""
    reference = _plan_reference(root, evidence_id, control_ref)
    if not _HISTORY_PATH.fullmatch(reference["path"]):
        raise ValueError("invalid history evidence path")
    # The pre-agent step reads it by repository, ref and path: no URL, so
    # the reference takes as few bytes of the payload as it can.
    reference.pop("url", None)
    return dict(reference, evidence_id=evidence_id)


def _reason_item(evidence: dict) -> dict:
    item = {key: evidence.get(key) for key in ("evidence_id", "kind")}
    item["summary"] = _short(evidence.get("summary") or "", 1600)
    item["limitations"] = [
        _short(value, 500) for value in (evidence.get("limitations") or [])[:4]
    ]
    if evidence.get("kind") == "human_relay":
        item["data"] = _feedback_data(evidence.get("data") or {})
    elif evidence.get("kind") == "plan":
        item["data"] = _plan_data(evidence.get("data") or {})
    elif evidence.get("kind") == "series_requirements":
        data = evidence.get("data") or {}
        item["data"] = {"head": data.get("head"),
                        "unmet": [{"id": u.get("id"), "paths": list(u.get("paths") or [])}
                                  for u in (data.get("unmet") or [])[:6]]}
    elif evidence.get("kind") == "patch_review":
        item["data"] = _patch_review_data(evidence.get("data") or {})
    return item


def series_requirements_context(record: dict, evidence: dict[str, dict]) -> list[dict] | None:
    """The series requirements a write Work carries for the trusted worker
    gate: the unmet ones of the newest ``series_requirements`` evidence
    among the Work's feedback, as ``{id, paths}``, and only while the fork
    head is the head that evidence names (a push since then may already
    meet them). ``None`` when the Work carries none."""
    work = record.get("active_work") or {}
    head = (record.get("fork") or {}).get("head_sha")
    for evidence_id in reversed(list(work.get("feedback_evidence_ids") or [])):
        found = evidence.get(evidence_id) or {}
        if found.get("kind") != "series_requirements":
            continue
        if not head or (found.get("digests") or {}).get("head") != head:
            return None
        return [{"id": u["id"], "paths": list(u["paths"])}
                for u in ((found.get("data") or {}).get("unmet") or [])[:6]]
    return None


# The findings of a failed review a payload carries as reason evidence: the
# blocking ones first, each cut short. The history holds the whole review.
REVIEW_FINDINGS_IN_PAYLOAD = 8


def _patch_review_data(data: dict) -> dict:
    findings = sorted((f for f in data.get("findings") or [] if isinstance(f, dict)),
                      key=lambda f: ("blocker", "major", "minor", "nit").index(f.get("severity"))
                      if f.get("severity") in ("blocker", "major", "minor", "nit") else 4)
    return {
        "verdict": data.get("verdict"), "disposition": data.get("disposition"),
        "candidate": (data.get("patch") or {}).get("candidate"),
        "findings": [{"id": f.get("id"), "severity": f.get("severity"),
                      "category": f.get("category"), "file": f.get("file"), "line": f.get("line"),
                      "title": _short(f.get("title") or "", 200),
                      "detail": _short(f.get("detail") or "", 400)}
                     for f in findings[:REVIEW_FINDINGS_IN_PAYLOAD]],
        "findings_not_listed": max(0, len(findings) - REVIEW_FINDINGS_IN_PAYLOAD),
    }


def held_patch_reference(record: dict) -> dict:
    """The held patch a review Work reviews, as the payload names it. Raises
    ``ValueError`` when the record holds none."""
    held = record.get("held_patch") or {}
    if not held or not re.fullmatch(r"[1-9][0-9]{0,19}", str(held.get("worker_run_id"))) \
            or not re.fullmatch(r"[1-9][0-9]{0,19}", str(held.get("artifact_run_id"))) \
            or not isinstance(held.get("artifact_id"), int):
        raise ValueError("the review Work names no held patch")
    return {key: held.get(key) for key in (
        "worker_run_id", "artifact_run_id", "artifact_id", "work", "revision", "patch_digest", "candidate", "tree",
        "expected_head", "operation", "changed_files", "report_evidence_id")}


def previous_patch_reference(request: dict, evidence: dict[str, dict], *,
                             requested_at: str) -> dict | None:
    """The patch the newest review among the Work's feedback returned for
    repair, as the payload names it for the worker that amends it: the
    publication artifact that carries its series, the run that owns it, and
    the SHA-256 of the series. ``None`` when the Work carries no such review,
    the review names no artifact, or the artifact expired (or expires within
    a day) when the Work was requested."""
    import patch_review

    for evidence_id in reversed(list(request.get("feedback_evidence_ids") or [])):
        found = evidence.get(evidence_id) or {}
        if found.get("kind") != patch_review.STATUS:
            continue
        data = found.get("data") or {}
        held = data.get("patch") or {}
        if data.get("disposition") != "return_for_repair" \
                or not re.fullmatch(r"[1-9][0-9]{0,19}", str(held.get("artifact_run_id"))) \
                or not isinstance(held.get("artifact_id"), int) \
                or not re.fullmatch(r"[0-9a-f]{64}", str(held.get("patch_digest"))) \
                or not held.get("held_at"):
            return None
        # Fixed by the request, so a replay of the same Work names the same
        # payload.
        if patch_review.held_patch_expired({"held_patch": held}, requested_at):
            return None
        return {"evidence_id": evidence_id, "artifact_run_id": str(held["artifact_run_id"]),
                "artifact_id": held["artifact_id"], "patch_digest": held["patch_digest"],
                "candidate": held.get("candidate"), "expected_head": held.get("expected_head"),
                "operation": held.get("operation")}
    return None


def rewrite_reference(evidence: dict | None) -> dict:
    """The accepted rewrite a review Work reviews, as the payload names it.
    Raises ``ValueError`` when the record names no accepted rewrite."""
    data = (evidence or {}).get("data") or {}
    artifact = data.get("artifact") or {}
    if (evidence or {}).get("kind") != SUBMISSION_REWRITE or data.get("verdict") != "pass" \
            or not re.fullmatch(r"[1-9][0-9]{0,19}", str(artifact.get("run_id"))) \
            or not isinstance(artifact.get("artifact_id"), int):
        raise ValueError("the review Work names no accepted submission rewrite")
    return {"evidence_id": evidence["evidence_id"], "head": data["head"],
            "series_digest": data["series_digest"], "text_digest": data["text_digest"],
            "run_id": str(artifact["run_id"]), "artifact_id": int(artifact["artifact_id"])}


def cmd_plan(args: argparse.Namespace) -> int:
    import reconcile

    policy = load_policy()
    root = Path(args.root) if args.root else repository_root()
    control_ref = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _store, snapshot = reconcile.load_snapshot(root, policy)
    nodes = []
    selected = []
    skipped = []
    deferred = []
    ordered = sorted(
        snapshot.contributions.values(),
        key=lambda record: (record["active_work"] or {}).get("requested_at", "")
        if record.get("active_work")
        else "",
    )
    for record in ordered:
        request = request_from_active(record)
        if request is None:
            continue
        if len(nodes) >= policy["concurrency"]["coding_workers"]:
            break
        # Every Work carries its history, which the controller tick records
        # when it allocates the Work. Work whose history is not committed
        # yet is not dispatched in this run; the next tick records it.
        history_id = work_history.evidence_id(request["dedupe_key"])
        if history_id not in snapshot.evidence:
            deferred.append({"contribution": record["contribution_id"], "task": request["task"],
                             "reason": "history not recorded yet"})
            continue
        # A requested task that cannot be replayed is reported and skipped:
        # one such record never blocks the dispatch of every other
        # contribution. Its Work stays unclaimed, so the controller's
        # dispatch recovery decides it.
        if not record["upstream"].get("repository_id") or not record["upstream"].get(
            "base_sha"
        ):
            skipped.append({"contribution": record["contribution_id"], "task": request["task"],
                            "reason": "the upstream identity or base is not bound"})
            continue
        if not (record["active_work"].get("tracking_issue") or {}).get("url"):
            skipped.append({"contribution": record["contribution_id"], "task": request["task"],
                            "reason": "no frozen tracking-issue description"})
            continue
        # A Work this run cannot build (an evidence file missing at the
        # pinned commit, a payload over the queue budget) is skipped and
        # reported: one such record never fails the dispatch of the others.
        try:
            allowed_paths, context = payload_inputs(
                request, record, snapshot.evidence,
                plan_reference=lambda eid: _plan_reference(root, eid, control_ref))
            if history_id is not None:
                # Everything tried before and the owner's steering: the
                # trusted pre-agent step fetches it at the pinned commit and
                # checks its digest.
                context["history"] = _history_reference(root, history_id, control_ref)
            nodes.append(node(request, record, control_repo_id=args.control_repo_id,
                              allowed_paths=allowed_paths, context=context))
            payload_bytes = len(
                json.dumps(
                    nodes[-1]["payload"], separators=(",", ":"), ensure_ascii=False
                ).encode()
            )
            if payload_bytes > MAX_PAYLOAD_BYTES:
                raise ValueError(
                    f"requested task payload exceeds safe queue budget: {payload_bytes} bytes"
                )
        except (ValueError, OSError) as error:
            del nodes[len(selected):]
            skipped.append({"contribution": record["contribution_id"], "task": request["task"],
                            "reason": str(error)[:300]})
            continue
        selected.append(
            {
                "contribution": record["contribution_id"],
                "task": request["task"],
                "worker_profile": nodes[-1]["worker_profile"],
            }
        )
    print(json.dumps({"selected": selected, **({"skipped": skipped} if skipped else {}),
                      **({"deferred": deferred} if deferred else {})},
                     indent=2))
    if not nodes:
        return 0
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan(nodes), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m work")
    subcommands = parser.add_subparsers(dest="command", required=True)
    plan_parser = subcommands.add_parser("plan")
    plan_parser.add_argument("--root")
    plan_parser.add_argument("--out", required=True)
    plan_parser.add_argument("--control-repo-id", required=True)
    plan_parser.set_defaults(handler=cmd_plan)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
