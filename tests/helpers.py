# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Fixtures shared by the upstreaming tests."""

from __future__ import annotations

import copy
import os
import subprocess
from pathlib import Path

import lifecycle
from common import load_policy

NOW = "2026-11-18T12:00:00+00:00"
OWNER = {"id": 1001, "login": "owner"}
BACKUP = {"id": 1002, "login": "backup"}
STRANGER = {"id": 9999, "login": "stranger"}
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def policy(**over):
    p = load_policy()
    p = copy.deepcopy(p)
    p["command_authority"]["actor_ids"] = [OWNER["id"], BACKUP["id"]]
    p.update(over)
    return p


def contribution(phase="development", **over):
    rec = lifecycle.new_contribution(
        contribution_id="example--proj/12", project_id="example--proj",
        issue={"id": 5000, "number": 12}, upstream="example/proj", distributions=["proj"],
        base_ref="main", policy_id="test", now=NOW)
    rec.update({
        "phase": phase,
        # A development record defaults to the state of the table that
        # observes its published head.
        **({"activity": "waiting_ci"} if phase == "development" else {}),
        "owners": {"primary": OWNER, "backup": BACKUP, "submitter": None, "handoff_reviewer": None},
        "admission_class": "configuration_integration",
        "upstream": {"repository_id": 1, "full_name": "example/proj", "base_ref": "main",
                     "base_sha": SHA_C},
        "fork": {"repository_id": 2, "name": "riseproject-upstreaming/proj", "head_ref": "riscv64-dev",
                 "head_sha": SHA_A, "pr_number": 1},
    })
    rec.update(over)
    return rec


def awaiting_submission(**over):
    """A record in ``handoff.awaiting_submission`` as the table enters it."""
    rec = contribution(phase="handoff", activity="awaiting_submission",
                       last_ci_evidence_id="ci-2-7-1")
    rec.update({
        "handoff_request": {"command_id": "observation-ci-2-7-1", "head": rec["fork"]["head_sha"],
                            "revision": rec["revision"]},
        "pending_human_action": {"kind": "submit_upstream", "requested_at": NOW,
                                 "reminded_at": None, "escalated_at": None, "counter": None},
    })
    rec.update(over)
    return rec


def comment(body, actor=OWNER, cid=1):
    return {"id": cid, "body": body, "user": dict(actor)}


def git(repo: Path, *args: str, env_extra=None) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@example.com",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    env.update(env_extra or {})
    return subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null", *args],
                          check=True, capture_output=True, text=True, env=env).stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    return path


def commit_files(repo: Path, files: dict[str, str | None], msg="c", mode=None) -> str:
    for rel, content in files.items():
        p = repo / rel
        if content is None:
            git(repo, "rm", "-q", rel)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        if mode:
            p.chmod(mode)
        git(repo, "add", rel)
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def apply_command(record, cmd, *, command_id="c", actor=OWNER, now=NOW, policy_=None,
                  operations=None, upstream_pr=None):
    """A command applied as the controller applies it: through the engine
    where the table defines it, through the compatibility layer otherwise."""
    import controller

    receipt = {"command_id": command_id, "actor": dict(actor)}
    return controller.apply_command(record, cmd, receipt, operations=operations or {},
                                    policy=policy_ or policy(), now=now, upstream_pr=upstream_pr)


def ci_record(evidence_id: str, head: str, *, failing: str | None = None,
                contribution_id: str = "example--proj/12") -> dict:
    """CI evidence of ``head``: one job, failing when ``failing`` names it."""
    name = failing or "build riscv64"
    return {"schema_version": 1, "evidence_id": evidence_id, "contribution_id": contribution_id,
            "kind": "ci", "policy_version": "test", "collected_at": NOW,
            "summary": f".github/workflows/ci.yml run 7 attempt 1: {'failed' if failing else 'passed'}",
            "limitations": [], "debug_present": False,
            "ci": {"repository_id": 2, "repository": "riseproject-upstreaming/proj",
                   "head_sha": head, "base_sha": None, "test_merge_sha": None,
                   "workflow_id": 1, "workflow_path": ".github/workflows/ci.yml",
                   "workflow_sha": None, "event": "pull_request", "run_id": 7, "run_attempt": 1,
                   "jobs": [{"job_id": 1, "name": name, "status": "completed",
                             "conclusion": "failure" if failing else "success",
                             "executed": True, "runner_labels": ["ubuntu-24.04-riscv"]}],
                   "expected_wheels": [], "artifacts": []}}


COVERAGE_AREAS = ("build", "test", "release", "packaging", "documentation", "native_sources")


def plan(decision="develop", *, allowed_paths=None, admission_class="configuration_integration",
            covered=("a",), rationale="r"):
    """A verified plan as the planner delivers it: a decision, its
    admission class and allowed paths (for develop), and the path coverage
    of every required area (``covered`` paths in ``build``)."""
    if allowed_paths is None:
        allowed_paths = ["a"] if decision == "develop" else []
    coverage = [{"area": area, "paths": list(covered) if area == "build" else [],
                 "assessment": "assessed from the repository"} for area in COVERAGE_AREAS]
    return {"proposal": {"decision": decision, "rationale": rationale,
                         "admission_class": admission_class if decision == "develop" else None,
                         "allowed_paths": list(allowed_paths), "path_coverage": coverage}}
