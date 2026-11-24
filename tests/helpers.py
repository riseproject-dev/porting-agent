# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Fixtures shared by the upstreaming tests."""

from __future__ import annotations

import copy
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import collect
import lifecycle
from common import load_policy

# The list reads of ``collect.steering``: the comments of the tracking issue
# and the reviews and comments of the development and upstream PRs.
STEERING_LIST = re.compile(r"^/repos/[^/]+/[^/]+/(?:issues|pulls)/[0-9]+/(?:comments|reviews)"
                           r"\?per_page=100(?:&sort=created&direction=desc)?$")


def steering_list(path: str) -> list | None:
    """``[]`` for a steering list read (no human text by default), ``None``
    for any other path."""
    return [] if STEERING_LIST.match(path) else None


class NoReadApi:
    """An API for a tick that reads nothing from GitHub but the (empty)
    steering lists of the Work it allocates."""

    def __init__(self):
        self.calls: list[str] = []

    def paginate(self, path, **_kwargs):
        self.calls.append(path)
        found = steering_list(path)
        if found is None:
            raise collect.CollectError(f"GET {path}: not expected in this test")
        return found

    def get(self, path):
        raise collect.CollectError(f"GET {path}: not expected in this test")

def committed_history(record: dict, policy_: dict | None = None) -> dict[str, dict]:
    """The committed ``work_history`` evidence of the record's requested
    Work, by evidence id, recorded when the Work was requested: what the
    tick that allocated it committed with it."""
    import work_history
    aw = record["active_work"]
    history = work_history.build(record, aw, {}, policy=policy_ or policy(),
                                 now=aw["requested_at"])
    return {history["evidence_id"]: history}


NOW = "2026-11-18T12:00:00+00:00"
OWNER = {"id": 1001, "login": "owner"}
BACKUP = {"id": 1002, "login": "backup"}
STRANGER = {"id": 9999, "login": "stranger"}
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def holding_work(record: dict, task: str = "development_repair") -> tuple[dict, dict]:
    """``record`` holding requested Work of ``task`` at its generation and
    revision, and the Work identity a publication of that Work names
    (``lifecycle.publication_check``'s ``expected_generation`` and
    ``work_key``)."""
    import work
    generation = int(record.get("generation", 0))
    key = work.work_key(record["contribution_id"], generation, record["revision"], task)
    held = dict(record, active_work={
        "dedupe_key": key, "task": task, "revision": record["revision"],
        "generation": generation, "attempt": 1, "requested_at": NOW,
        "deadline_at": "2026-11-19T12:00:00+00:00", "dispatched_run_id": None,
        "reason_ids": [], "feedback_evidence_ids": []})
    return held, {"expected_generation": generation, "work_key": key}


def result_missing(*graphs: str) -> dict[str, dict]:
    """``work_results`` readings (``reconcile.work_result_readings``) of the
    completed Work ``graphs``: each worker run ended without a publication
    artifact, so the graph is terminal without a usable Result."""
    return {graph: {"run_id": "4242", "status": "completed", "conclusion": "success",
                    "ended_at": "2026-01-01T00:00:00Z", "publication": False}
            for graph in graphs}


def completed_results(queue_works: dict[str, str] | None) -> dict[str, dict]:
    """``result_missing`` for every graph ``queue_works`` reports completed."""
    return result_missing(*[g for g, s in (queue_works or {}).items() if s == "completed"])


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
        # A record past planning holds its verified plan and admitted scope
        # (the invariant `plan` of its states).
        **({"plan_evidence_id": "plan-1", "scope_paths": ["a"]}
           if phase not in ("queued", "planning") else {}),
        # A record of a merged contribution holds its merge (the invariant
        # `merge` of its states).
        **({"merge": {"merge_sha": SHA_C, "merged_at": NOW, "target_ref": "main",
                      "evidence_id": "merge-1"}}
           if phase in ("release_wait", "released") else {}),
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


@dataclass
class Taken:
    """What a test reads of a row it took or that was refused: the record
    (the same record when refused), whether it was applied, the reason of a
    refusal, the event, the verb, and the refusal itself."""

    record: dict
    accepted: bool
    reason: str | None
    event: dict | None
    verb: str | None
    charged: tuple = ()
    restarted: dict | None = None
    refused: Any = None


def taken(record: dict, outcome) -> Taken:
    """The test view of an outcome of the engine or of a command."""
    import controller
    import engine

    if isinstance(outcome, engine.Applied):
        return Taken(outcome.record, True, None, outcome.event, outcome.verb, outcome.charged,
                     outcome.restarted)
    if isinstance(outcome, controller.Rejected):
        return Taken(record, False, outcome.reason, None, None, refused=outcome)
    verb = outcome.verb if isinstance(outcome, engine.Refused) else None
    return Taken(record, False, outcome.detail, None, verb, refused=outcome)


def take(record: dict, verb: str, **kwargs) -> Taken:
    """``engine.try_apply_transition``, read as a test reads it."""
    import engine

    return taken(record, engine.try_apply_transition(record, verb, **kwargs))


def refusal(record: dict, verb: str, **kwargs) -> str | None:
    """Why the guards of ``verb`` do not hold, or ``None`` (the effects,
    and the values they need, are not evaluated)."""
    import engine

    found = engine.row_admitted(record, verb, **kwargs)
    return found.detail if found else None


def command(record: dict, cmd, *, command_id: str = "c", **kwargs) -> Taken:
    """``engine.try_apply_command``, read as a test reads it."""
    import engine

    return taken(record, engine.try_apply_command(record, cmd, cause_id=command_id, **kwargs))


def resolve_command(record: dict, cmd, **kwargs) -> tuple[str | None, str | None]:
    """The one verb a command takes now by its guards, or the reasons none
    does (``engine.row_admitted`` over ``engine.command_verbs``)."""
    import engine

    verbs = engine.command_verbs(record, cmd)
    if not verbs:
        return None, engine.not_applicable(cmd.verb, engine.state_key(record))
    reasons, admitted = [], []
    for verb in verbs:
        why = engine.row_admitted(record, verb, command=cmd, **kwargs)
        (reasons if why else admitted).append(why.detail if why else verb)
    if len(admitted) > 1:
        raise AssertionError(f"{admitted} apply together")
    if admitted:
        return admitted[0], None
    return None, "; ".join(dict.fromkeys(reasons))


def apply_command(record, cmd, *, command_id="c", actor=OWNER, now=NOW, policy_=None,
                  operations=None, upstream_pr=None):
    """A command applied as the controller applies it: through the engine
    where the table defines it, through the compatibility layer otherwise."""
    import controller

    receipt = {"command_id": command_id, "actor": dict(actor)}
    return taken(record, controller.apply_command(record, cmd, receipt,
                                                  operations=operations or {},
                                                  policy=policy_ or policy(), now=now,
                                                  upstream_pr=upstream_pr))


def runner_loss(lost=()):
    """A stand-in for ``collect.classify_runner_loss`` without annotation
    reads: the jobs whose ids are in ``lost`` lost their runner."""
    lost = frozenset(lost)

    def classify(_api, _repository, snap):
        for jobs in snap.jobs_by_run.values():
            for job in jobs:
                job["runner_lost"] = job["id"] in lost
    return classify


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
