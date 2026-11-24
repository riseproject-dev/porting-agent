# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Create or discover one controller-authorized same-fork development PR."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import collect
import provisioning
import schemas
import state
from common import CONTROL_REPO, DEV_REF, contribution_file_id


class DevPRError(ValueError):
    pass


def marker_prefix(contribution_id: str) -> str:
    """The start of the marker every development PR of a contribution
    carries, whatever revision and head it was created at."""
    return f"<!-- upstreaming-development:{contribution_id}:"


def body(record: dict, operation: dict) -> str:
    return "\n".join([
        "## riscv64 development",
        "",
        "This same-fork pull request is the automated development and CI workspace for a bounded riscv64 contribution.",
        "",
        f"Tracking issue: https://github.com/{CONTROL_REPO}/issues/{record['tracking_issue']['number']}",
        f"Verified plan: `{record['plan_evidence_id']}`",
        f"Allowed paths: {', '.join(record['scope_paths'] or [])}",
        "",
        "This is not an upstream submission. Human approval is required before upstream handoff.",
        "",
        operation["proposed"]["marker"],
    ])


# The states whose rows launch each kind of development PR operation: the
# PR of a published head is opened before CI is awaited (from the state the
# head is in; in `development.waiting_ci` for a record that waits for CI
# without one, which `launch_development_pr` and `confirm_development_pr`
# heal), and `development.waiting_ci`, which requires the PR, runs the CI
# bootstrap of the recorded PR.
INTENT_STATES = {"dev_pr_create": ("development.fix_needed", "development.agent_running",
                                   "development.waiting_ci"),
                 "dev_pr_update": ("development.waiting_ci",)}


def pushed_head(record: dict, operations) -> str | None:
    """The commit a worker pushed to the development branch in the run whose
    Result is not folded yet (it is folded once the development PR is
    recorded), or ``None``: the commit of the newest branch operation of the
    record's revision that is confirmed and whose lease is the recorded head,
    when it is not that head."""
    fork = record.get("fork") or {}
    found = [op for op in operations
             if op.get("contribution_id") == record["contribution_id"]
             and op.get("kind") in ("branch_update", "branch_rewrite")
             and op.get("status") == "confirmed" and op.get("revision") == record["revision"]
             and (op.get("target") or {}).get("ref") == DEV_REF
             and (op.get("target") or {}).get("expected_head") == fork.get("head_sha")]
    if not found:
        return None
    commit = (max(found, key=lambda op: str(op.get("created_at") or ""))
              .get("proposed") or {}).get("commit")
    return commit if isinstance(commit, str) and commit != fork.get("head_sha") else None


def intent_problem(record: dict, op: dict, operations=()) -> str | None:
    """Why ``op`` is not the current development PR intent of ``record``,
    or ``None``: a state whose rows launch its kind, the record's revision,
    fork and base, and the head the intent names: the recorded head for the
    CI bootstrap of the recorded PR; for the PR of a published head, no PR
    recorded and a head that is not the base, which is the commit a worker
    pushed in `development.agent_running` (``pushed_head`` of
    ``operations``). The trusted workflow reads the branch and refuses any
    other head."""
    fork = record.get("fork") or {}
    target = op.get("target") or {}
    state = f"{record['phase']}.{record['activity']}"
    head = target.get("expected_head")
    if state not in INTENT_STATES.get(op["kind"], ()) or record["revision"] != op["revision"]:
        return "the operation is not of the record's state and revision"
    if target.get("repository") != fork.get("name") \
            or target.get("repository_id") != fork.get("repository_id") \
            or target.get("ref") != DEV_REF \
            or target.get("base_sha") != record["upstream"].get("base_sha"):
        return "the operation does not target the recorded fork and base"
    if op["kind"] == "dev_pr_update":
        if fork.get("pr_number") is None or head != fork.get("head_sha"):
            return "the CI bootstrap names another head or no PR is recorded"
        return None
    if fork.get("pr_number") is not None or not head or head == record["upstream"].get("base_sha"):
        return "a PR is recorded, or the head is the base"
    if state == "development.agent_running" and head != pushed_head(record, operations):
        return "the head is not the commit the worker pushed"
    if state == "development.waiting_ci" and head != fork.get("head_sha"):
        return "the head is not the recorded head"
    return None


def load_request(root: Path, operation_id: str) -> tuple[dict, dict]:
    if not re.fullmatch(r"op-[0-9a-f]{24}", operation_id):
        raise DevPRError("invalid operation ID")
    op = json.loads((root / ".state/operations" / f"{operation_id}.json").read_text())
    schemas.validate("operation", op)
    cid = contribution_file_id(op["contribution_id"])
    record = state.migrate_names(
        "contributions", json.loads((root / ".state/contributions" / f"{cid}.json").read_text()))[0]
    schemas.validate("contribution", record)
    if op["kind"] not in INTENT_STATES or op["status"] != "planned" \
            or record.get("active_operation_id") != operation_id or record.get("paused"):
        raise DevPRError("operation is not the current authorized development PR intent")
    operations = [json.loads(path.read_text())
                  for path in sorted((root / ".state/operations").glob("op-*.json"))] \
        if record["phase"] == "development" and record["activity"] == "agent_running" else []
    problem = intent_problem(record, op, operations)
    if problem:
        raise DevPRError(f"development PR target does not match current fork/base: {problem}")
    return record, op


class Api(collect.Api):
    def __init__(self, *args, **kwargs):
        # A client that writes never reads through the HTTP cache.
        super().__init__(*args, **{**kwargs, "cache": False})

    def mutate(self, method: str, path: str, value: dict) -> dict:
        create = method == "POST" and path.endswith("/pulls")
        update = method == "PATCH" and re.search(r"/pulls/[0-9]+$", path)
        if not path.startswith("/repos/") or ".." in path or not (create or update):
            raise DevPRError("unsupported development PR API operation")
        req = urllib.request.Request(self.base + path, data=json.dumps(value).encode(), method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Accept": "application/vnd.github+json",
                                              "Content-Type": "application/json",
                                              "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with self._open(req, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise DevPRError(f"development PR API HTTP {exc.code}") from None
        if len(raw) > 2 * 1024 * 1024:
            raise DevPRError("oversized development PR response")
        return json.loads(raw)


def publish(root: Path, operation_id: str, token: str, run_id: str) -> dict:
    record, op = load_request(root, operation_id)
    api = Api(token=token)
    repo = collect.repository(api, op["target"]["repository"])
    if repo["id"] != op["target"]["repository_id"] or repo.get("parent_id") != record["upstream"]["repository_id"]:
        raise DevPRError("fork identity changed")
    head = collect.branch_head(api, repo["full_name"], DEV_REF)
    if not head or head["head"] != op["target"]["expected_head"]:
        raise DevPRError("development branch head changed")
    configured = provisioning.configure_inherited_workflows(
        provisioning.WriteApi(token=token), repo["full_name"], DEV_REF)
    base_ref = record["upstream"]["base_ref"]
    owner = repo["full_name"].split("/", 1)[0]
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{DEV_REF}", "base": base_ref, "per_page": 100})
    pulls = api.get(f"/repos/{repo['full_name']}/pulls?{query}")
    # Every development PR of this contribution carries its marker, whatever
    # revision and head it was created at.
    prefix = marker_prefix(record["contribution_id"])
    if op["kind"] == "dev_pr_update":
        matching = [pr for pr in pulls if pr.get("number") == (record.get("fork") or {}).get("pr_number")
                    and prefix in (pr.get("body") or "")]
        unrelated = [pr for pr in pulls if pr not in matching]
        if len(matching) != 1 or unrelated:
            raise DevPRError("recorded development PR is missing, ambiguous or unrelated")
        expected_body = matching[0]["body"]
    else:
        matching = [pr for pr in pulls if prefix in (pr.get("body") or "")]
        unrelated = [pr for pr in pulls if pr not in matching]
        if len(matching) > 1 or unrelated:
            raise DevPRError("existing development-head PR is ambiguous or unrelated")
        # A create that finds a PR this contribution's marker names was
        # started earlier, by this operation or by one a lost launch, a head
        # change or a resume left: it is adopted, and its body is the one
        # that launch wrote, whatever wording the code had then, so a retry
        # stays idempotent.
        expected_body = matching[0]["body"] if matching else body(record, op)
    pr = matching[0] if matching else api.mutate("POST", f"/repos/{repo['full_name']}/pulls", {
        "title": "CI: develop riscv64 support",
        "head": DEV_REF,
        "base": base_ref,
        "body": expected_body,
        "draft": False,
        "maintainer_can_modify": False,
    })
    if matching and (configured["enabled"] or op["kind"] == "dev_pr_update"):
        api.mutate("PATCH", f"/repos/{repo['full_name']}/pulls/{pr['number']}", {"state": "closed"})
        pr = api.mutate("PATCH", f"/repos/{repo['full_name']}/pulls/{pr['number']}", {"state": "open"})
    observed = api.get(f"/repos/{repo['full_name']}/pulls/{pr['number']}")
    if observed.get("state") != "open" or observed.get("body") != expected_body \
            or (observed.get("head") or {}).get("sha") != op["target"]["expected_head"] \
            or (observed.get("head") or {}).get("ref") != DEV_REF \
            or (observed.get("base") or {}).get("ref") != base_ref:
        raise DevPRError("development PR readback mismatch")
    return {"schema_version": 1, "status": "published", "operation_id": operation_id,
            "contribution_id": record["contribution_id"], "revision": record["revision"],
            "repository": repo["full_name"], "repository_id": repo["id"],
            "pr_id": observed["id"], "pr_number": observed["number"], "pr_url": observed["html_url"],
            "head_sha": op["target"]["expected_head"], "base_ref": base_ref,
            "enabled_workflows": configured["enabled"], "disabled_workflows": configured["disabled"],
            "development_pr_run_id": int(run_id)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != CONTROL_REPO \
            or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise DevPRError("development PRs require the trusted control workflow on main")
    result = publish(Path.cwd(), args.operation_id, os.environ.get("UPSTREAMING_FORK_TOKEN", ""), os.environ["GITHUB_RUN_ID"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
