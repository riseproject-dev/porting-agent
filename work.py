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
operation. Investigations are no-write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from common import (
    CONTROL_REPO,
    DEV_REF,
    STATE_ROOT,
    canonical_json,
    load_policy,
    repository_root,
)

POOL = "porting"
# Two worker profiles in one pool: investigations (read and report, cheaper
# model, no publish job) and patch work (stronger model, trusted publish job).
WORKER_PROFILE = "worker"
INVESTIGATOR_PROFILE = "investigator"
PROFILE_WORKFLOWS = {WORKER_PROFILE: ".github/workflows/worker.lock.yml",
                     INVESTIGATOR_PROFILE: ".github/workflows/investigator.lock.yml"}
PROPOSAL_ARTIFACT = "porting-out"

WRITE_TASKS = {"development_patch", "development_repair", "upstream_revision"}
ALLOWED_OPERATIONS = {
    "development_patch": ["append", "rewrite"],
    "development_repair": ["append", "rewrite"],
    "upstream_revision": ["append"],   # history policy may widen this per project
    "investigation": [],
}

# Diagnostic no-write outputs are always allowed alongside.
_DIAG = [{"type": t, "min": 0, "max": 1} for t in ("noop", "report_incomplete", "report_missing_tool",
                                                   "report_missing_data")]


def effect_contract(task: str) -> dict:
    if task == "investigation":
        # The dossier comes back as an artifact too, but investigations must
        # not need a write-capable Claim: they report via artifact only.
        return {"version": 1, "outputs": _DIAG + [{"type": "upload_artifact", "min": 1, "max": 1}]}
    if task in WRITE_TASKS:
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
    if task == "investigation" and record.get("refresh"):
        context["refresh"] = dict(record["refresh"])
        context["existing_fork"] = dict(record.get("fork") or {})
    fork = record.get("fork") or {}
    up = record["upstream"]
    p = {
        "task": task,
        "plan": _PLANS[task],
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
        "ref": DEV_REF,
        "expected_head": fork.get("head_sha"),
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
    return p


def profile_for(task: str) -> str:
    if task == "investigation":
        return INVESTIGATOR_PROFILE
    if task in WRITE_TASKS:
        return WORKER_PROFILE
    raise ValueError(f"unknown task {task}")


def graph_id(dedupe_key: str) -> str:
    return "porting:" + hashlib.sha256(dedupe_key.encode()).hexdigest()[:32]


def request_from_active(rec: dict) -> dict | None:
    """The controller's committed request (``active_work``) as a work request,
    or None if there is nothing to dispatch for this record."""
    aw = rec.get("active_work")
    if not aw or rec.get("paused") or aw.get("revision") != rec["revision"]:
        return None
    fork = rec.get("fork") or {}
    return {"type": "submit_work", "dedupe_key": aw["dedupe_key"], "contribution_id": rec["contribution_id"],
            "revision": rec["revision"], "expected_head": fork.get("head_sha"), "task": aw["task"],
            "reason_ids": aw.get("reason_ids") or []}


def node(request: dict, record: dict, **kw) -> dict:
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


_PLANS = {
    "investigation": (
        "Investigate this upstream project and produce the dossier for a riscv64 wheel contribution. "
        "Read the full tracking issue description in context.tracking_issue and fetch its comments. "
        "Research upstream issues, open/closed PRs, CI/history, docs, websites, mailing lists, blogs, "
        "maintainers and their past reviews before recommending scope. Query project_graph_query for "
        "the dependency graph. Check sw-ecosystem/project-reports and extensively use any matching "
        "report, cross-checking it with current upstream sources. The dossier must satisfy "
        "schemas/dossier.schema.json, including complete research records with source URLs "
        "and findings for every category. Missing or unavailable required research is incomplete work: "
        "do not finish completed. Do not modify any repository. Return the dossier JSON as the proposal artifact."),
    "development_patch": (
        "Write the smallest patch to the fork's riscv64 branch that makes the upstream project build, "
        "test and publish Linux riscv64 wheels through its normal release process, following the "
        "nearest existing architecture pattern. The project's own CI must run on the fork: if a job is "
        "guarded to the upstream repository (for example `if: github.repository == 'owner/repo'`), "
        "lift the guard in a separate {debug} commit. CI must execute natively on `ubuntu-24.04-riscv` "
        "and upload the built riscv64 wheel as an artifact. If the project already has such a job, "
        "the fix may be only the {debug} commit. Return the proposal and patch series as the artifact."),
    "development_repair": (
        "Diagnose the failing fork CI identified by reason_ids on the expected head and propose a "
        "focused fix within scope. Return the proposal and patch series as the artifact."),
    "upstream_revision": (
        "Address the current maintainer feedback/CI failures identified by reason_ids on the "
        "upstream-facing riscv64 branch. No {debug} commits. Return the proposal and series."),
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


def _dossier_data(data: dict) -> dict:
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


def _dossier_reference(root: Path, evidence_id: str, control_ref: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", evidence_id) or "/" in evidence_id:
        raise ValueError("invalid dossier evidence ID")
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


def _reason_item(evidence: dict) -> dict:
    item = {key: evidence.get(key) for key in ("evidence_id", "kind")}
    item["summary"] = _short(evidence.get("summary") or "", 1600)
    item["limitations"] = [
        _short(value, 500) for value in (evidence.get("limitations") or [])[:4]
    ]
    if evidence.get("kind") == "human_relay":
        item["data"] = _feedback_data(evidence.get("data") or {})
    elif evidence.get("kind") == "dossier":
        item["data"] = _dossier_data(evidence.get("data") or {})
    return item


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
    contracts = {
        evidence["evidence_id"]: evidence
        for evidence in snapshot.evidence.values()
        if evidence.get("kind") == "target_contract"
    }
    nodes = []
    selected = []
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
        if not record["upstream"].get("repository_id") or not record["upstream"].get(
            "base_sha"
        ):
            raise ValueError(
                "requested task lacks verified upstream identity/base; request a fresh revision"
            )
        if not (record["active_work"].get("tracking_issue") or {}).get("url"):
            raise ValueError(
                "requested task lacks a frozen tracking-issue description; request a fresh revision"
            )
        contract = (
            contracts.get(record.get("target_contract_id") or "") or {}
        ).get("data") or {}
        reason_evidence = []
        for evidence_id in request.get("reason_ids") or []:
            evidence = snapshot.evidence.get(evidence_id)
            if evidence:
                item = _reason_item(evidence)
                if evidence.get("kind") == "dossier":
                    item["reference"] = _dossier_reference(
                        root, evidence["evidence_id"], control_ref
                    )
                reason_evidence.append(item)
        nodes.append(
            node(
                request,
                record,
                control_repo_id=args.control_repo_id,
                allowed_paths=record.get("scope_paths")
                or contract.get("allowed_paths")
                or [".github/workflows/*"],
                context={
                    "requested_at": record["active_work"]["requested_at"],
                    "reason_evidence": reason_evidence,
                },
            )
        )
        payload_bytes = len(
            json.dumps(
                nodes[-1]["payload"], separators=(",", ":"), ensure_ascii=False
            ).encode()
        )
        if payload_bytes > 15_500:
            raise ValueError(
                f"requested task payload exceeds safe queue budget: {payload_bytes} bytes"
            )
        selected.append(
            {
                "contribution": record["contribution_id"],
                "task": request["task"],
                "worker_profile": nodes[-1]["worker_profile"],
            }
        )
    print(json.dumps({"selected": selected}, indent=2))
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
