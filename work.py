# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Native work-queue plans for upstreaming tasks (plan sections 5.1, 15.1).

``controller.tick`` produces ``submit_work`` requests and the task of each
Work, which it commits as a task file (``work_task``). This module turns the
requests into the immutable gh-aw Work payload, which names that task file
by reference, and a staged plan for the dispatcher (``{"version": 1,
"nodes": [...], "dispatch": {...}}``, the format accepted by gh-aw's
``stagePlanIntents``).

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
from typing import Callable

import commit_rules
import lifecycle
import task_file
import work_history
from common import (
    CONTROL_REPO,
    canonical_json,
    load_policy,
    repository_root,
)

POOL = "default"
# Two worker profiles in one pool: planning runs (read and report, cheaper
# model, no publish job) and patch work (stronger model, trusted publish
# job), which reviews its own series with the review loop before it
# delivers it.
WORKER_PROFILE = "worker"
PLANNER_PROFILE = "planner"
PROFILE_WORKFLOWS = {WORKER_PROFILE: ".github/workflows/worker.lock.yml",
                     PLANNER_PROFILE: ".github/workflows/planner.lock.yml"}
PROPOSAL_ARTIFACT = "porting-out"

WRITE_TASKS = {"development_patch", "development_repair", "upstream_revision"}
# The Work task at submission: the rewrite reshapes the series, writes the
# pull request text, and reviews both with the review loop. It is
# uncredentialed like every Work and writes nothing: the rewrite is checked by
# trusted code in the worker's publish job without a token, and only the
# trusted handoff workflow builds `riscv64` from it.
SUBMISSION_REWRITE = "submission_rewrite"
SUBMISSION_TASKS = (SUBMISSION_REWRITE,)
ALLOWED_OPERATIONS = {
    "development_patch": ["append", "rewrite"],
    "development_repair": ["append", "rewrite"],
    "upstream_revision": ["append"],   # history policy may widen this per project
    "planning": [],
    SUBMISSION_REWRITE: [],
}

# Diagnostic no-write outputs are always allowed alongside.
_DIAG = [{"type": t, "min": 0, "max": 1} for t in ("noop", "report_incomplete", "report_missing_tool",
                                                   "report_missing_data")]


def effect_contract(task: str) -> dict:
    if task == "planning":
        # The plan comes back as an artifact too, but planning runs must
        # not need a write-capable Claim: they report via artifact only.
        return {"version": 1, "outputs": _DIAG + [{"type": "upload_artifact", "min": 1, "max": 1}]}
    if task in WRITE_TASKS or task in SUBMISSION_TASKS:
        return {"version": 1, "outputs": _DIAG + [{"type": "upload_artifact", "min": 1, "max": 1}]}
    raise ValueError(f"unknown task {task}")


def task_data(request: dict, record: dict, *, allowed_paths: list[str],
              context: dict | None = None) -> dict:
    """Everything the agent and the trusted jobs need to know about one
    controller request: the content of the task file the controller commits
    (``work_task``). Nothing is looked up from mutable state at execution
    time except re-checks."""
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
    }
    p.update(_identity(request))
    return p


def payload(request: dict, record: dict, *, control_repo_id: str, allowed_paths: list[str],
            context: dict | None = None) -> dict:
    """The Work as the trusted jobs read it, once resolved
    (``task_file.resolve``): the task (``task_data``) with the resource scope
    and the effect contract the queue's Claim scope reads. A Work queued
    before task files existed carried exactly this inline."""
    return {**task_data(request, record, allowed_paths=allowed_paths, context=context),
            "resource_scope": {"version": 1, "resources": [
                {"host": "github.com", "repository": CONTROL_REPO,
                 "repository_id": str(control_repo_id)}]},
            "effect_contract": effect_contract(request["task"])}


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


def queue_payload(request: dict, *, control_repo_id: str, reference: dict) -> dict:
    """The payload the queue carries for one requested Work: its identity (the
    fields the reconciler keys a Result on: task, contribution, revision,
    generation, and Work id), the reference to its task
    file, and the two fields gh-aw's Claim scope reads (fixed per task kind). Its size is bounded
    (``MAX_QUEUE_PAYLOAD_BYTES``), whatever the task."""
    pl = {"version": 1, "task": request["task"], "contribution_id": request["contribution_id"],
          "revision": request["revision"], "generation": request["generation"],
          "dedupe_key": request["dedupe_key"], "task_file": reference,
          "resource_scope": {"version": 1, "resources": [
              {"host": "github.com", "repository": CONTROL_REPO,
               "repository_id": str(control_repo_id)}]},
          "effect_contract": effect_contract(request["task"])}
    return pl


def queue_node(request: dict, pl: dict) -> dict:
    """The queue node of a requested Work with its queue payload ``pl``."""
    return {
        "graph_id": graph_id(request["dedupe_key"]),
        "node_key": "task",
        "pool": POOL,
        "priority": 2 if request["task"] in ("upstream_revision", "development_repair") else 3,
        "fairness_key": "",
        "worker_profile": profile_for(request["task"]),
        "payload": pl,
        "depends_on": [],
    }


def node(request: dict, record: dict, **kw) -> dict:
    """The queue node of a request with its payload inline (``payload``),
    the form of Work queued before task files existed."""
    pl = payload(request, record, **kw)
    key = request["dedupe_key"]
    return {
        "graph_id": graph_id(key),
        "node_key": "task",
        "pool": POOL,
        "priority": 2 if request["task"] in ("upstream_revision", "development_repair") else 3,
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
        "{debug} commit. Review the committed series with the review loop of the worker prompt "
        "before you return it. Return the proposal and patch series as the artifact."),
    "development_repair": (
        "Diagnose the failing fork CI identified by reason_ids on the expected head and propose a "
        "focused fix within scope. Review the committed series with the review loop of the worker "
        "prompt before you return it. Return the proposal and patch series as the artifact."),
    "upstream_revision": (
        "Address the current maintainer feedback/CI failures identified by reason_ids on the "
        "upstream-facing riscv64 branch. No {debug} commits. Review the committed series with the "
        "review loop of the worker prompt before you return it. Return the proposal and series."),
    SUBMISSION_REWRITE: (
        "Prepare the upstream submission of the approved riscv64-dev series at expected_head. "
        "Reshape its non-debug commits on base_sha after the project's commit conventions "
        "(squash only where its contributing instructions or reviewers require it; do not split: "
        "report a split the project requires as a reason; keep the final tree identical and "
        "every intermediate tree a state of the approved series), and write the upstream PR title and description following the project's "
        "pull request template exactly and its contributing instructions. Change no code. "
        "Review the committed series and the text with the review loop of the worker prompt "
        "before you return them. Return the rewrite and the reshaped series as the artifact."),
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


# The bytes of a queue payload at most: gh-aw accepts 16384, and a payload that
# names its task file takes about a kilobyte, whatever the task holds.
MAX_QUEUE_PAYLOAD_BYTES = 2048


def payload_inputs(request: dict, record: dict, evidence: dict[str, dict], *,
                   plan_reference: Callable[[str], dict]) -> tuple[list[str], dict]:
    """The allowed paths and the context of the task of ``request``, as
    the controller builds them (``work_task``) from the committed ``evidence``: the scope
    of the record (else the paths of its target contract), the reason
    evidence of the request (a plan named by ``plan_reference``, a callable
    of its evidence id), the commit rules and template sections of the
    verified plan. The history reference
    is added by the caller. Raises ``ValueError`` when a part cannot be
    built."""
    target = evidence.get(record.get("target_contract_id") or "") or {}
    contract = (target.get("data") or {}) if target.get("kind") == "target_contract" else {}
    plan_document = (evidence.get(record.get("plan_evidence_id") or "") or {}).get("data")
    rules = commit_rules.from_plan(plan_document) if request["task"] != "planning" else None
    sections = (commit_rules.sections_from_plan(plan_document)
                if request["task"] in SUBMISSION_TASKS else None)
    requirements = (series_requirements_context(record, evidence)
                    if request["task"] in WRITE_TASKS else None)
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
        # The unmet series requirements of the plan this Work answers, which
        # the trusted worker gate checks.
        **({"series_requirements": requirements} if requirements is not None else {}),
    }
    return allowed_paths, context


# The digits of the longest GitHub repository id (a signed 64-bit integer,
# at most 9223372036854775807).
CONTROL_REPO_ID_MAX_DIGITS = 19


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


def read_task_file(root: Path, request: dict, control_ref: str) -> dict:
    """The reference to the committed task file of the requested Work, at the
    commit ``control_ref``, from the file under ``root``: its SHA-256, after
    checking that it is the task file of exactly this Work (its kind, its
    evidence id and the Work identity it names). Raises ``ValueError`` when
    the file is missing, unreadable or another Work's."""
    if not re.fullmatch(r"[0-9a-f]{40}", control_ref):
        raise ValueError("invalid immutable control ref")
    reference = task_file.reference(request["dedupe_key"], control_ref, "0" * 64)
    try:
        raw = (root / reference["path"]).read_bytes()
        document = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ValueError(f"the task file cannot be read: {error}") from error
    data = (document or {}).get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict) or document.get("kind") != task_file.KIND \
            or document.get("evidence_id") != reference["evidence_id"] \
            or (data.get("dedupe_key"), data.get("generation"), data.get("task"),
                data.get("contribution_id"), data.get("revision")) != (
                request["dedupe_key"], request["generation"], request["task"],
                request["contribution_id"], request["revision"]):
        raise ValueError("the task file is not the task file of the requested Work")
    return dict(reference, sha256=hashlib.sha256(raw).hexdigest())


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
        # Every Work carries its history and its task file, which the
        # controller tick records in the commit that allocates the Work. Work
        # whose history or task file is not committed yet is not dispatched in
        # this run; the next tick records it.
        recorded = [(work_history.evidence_id(request["dedupe_key"]), "history"),
                    (task_file.evidence_id(request["dedupe_key"]), "task file")]
        missing = [name for eid, name in recorded if eid not in snapshot.evidence]
        if missing:
            deferred.append({"contribution": record["contribution_id"], "task": request["task"],
                             "reason": f"{' and '.join(missing)} not recorded yet"})
            continue
        # A Work whose task file cannot be read at the pinned commit is
        # reported and skipped: one such record never blocks the dispatch of
        # every other contribution. Its Work stays unclaimed, so the
        # controller's dispatch recovery decides it.
        try:
            reference = read_task_file(root, request, control_ref)
            pl = queue_payload(request, control_repo_id=args.control_repo_id,
                               reference=reference)
            payload_bytes = len(json.dumps(pl, separators=(",", ":"), ensure_ascii=False).encode())
            if payload_bytes > MAX_QUEUE_PAYLOAD_BYTES:
                raise ValueError(f"the queue payload takes {payload_bytes} bytes, over "
                                 f"{MAX_QUEUE_PAYLOAD_BYTES}")
            nodes.append(queue_node(request, pl))
        except ValueError as error:
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
