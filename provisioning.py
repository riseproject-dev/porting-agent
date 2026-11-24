# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Trusted workflow-only fork provisioning. Never executes upstream code.

Creation uses a creation-only destination App token. Branch/settings operations
use a separately minted single-fork token. Existing branches are never reset.
The controller records fork_provision intent before dispatching this workflow.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

import collect
import isolation
import schemas
import state
from common import CONTROL_REPO, DEV_REF, FORK_ORG, contribution_file_id

# Why a provisioning run failed definitely, as a closed code: the receipt
# names one of these, never free text the reconciler would act on.
FAILURES = ("source_changed", "fork_mismatch", "branch_conflict", "settings", "workflows",
            "write_refused", "other")
# The step of the workflow a receipt comes from.
STAGES = ("preflight", "create", "initialize")


class ProvisionError(ValueError):
    """A definite failure of the provisioning effect, with its closed code."""

    def __init__(self, message: str = "", failure: str = "other"):
        super().__init__(message)
        self.failure = failure


class ProvisionRefused(ProvisionError):
    """The workflow's own final check did not pass: nothing was attempted,
    so this is no failure of the effect."""


class ProvisionUnconfirmed(ProvisionError):
    """A write whose answer is not a definite refusal: its effect is
    unknown, so the run is launched again rather than reported."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProvisionError("provisioning API redirect refused")


class WriteApi(collect.Api):
    def __init__(self, token: str):
        if not token:
            raise ProvisionError("explicit scoped provisioning token required")
        super().__init__(token=token, opener=urllib.request.build_opener(NoRedirect).open,
                         cache=False, follow_redirects=False)

    def _redirected(self, url: str, status: int) -> Exception:
        return ProvisionError("provisioning API redirect refused")

    def write(self, method: str, path: str, body: dict) -> dict:
        if method not in ("POST", "PUT") or not path.startswith("/repos/") or ".." in path:
            raise ProvisionError("unsupported provisioning API operation")
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method=method,
                                     headers={"Accept": "application/vnd.github+json", "Content-Type": "application/json",
                                              "X-GitHub-Api-Version": "2022-11-28", "Authorization": f"Bearer {self.token}"})
        try:
            with self._open(req, timeout=30) as response:
                data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ProvisionError("oversized provisioning response")
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as e:
            # 403, 404 and 422 refuse the write definitely, unless a 403 is
            # rate limiting; any other answer leaves the effect unknown.
            headers = e.headers or {}
            limited = e.code == 429 or (e.code == 403 and bool(
                headers.get("retry-after") or headers.get("x-ratelimit-remaining") == "0"))
            message = f"provisioning API {method} {path}: HTTP {e.code}"
            if e.code in (403, 404, 422) and not limited:
                raise ProvisionError(message, failure="write_refused") from None
            raise ProvisionUnconfirmed(message) from None


def validate_request(record: dict, operation: dict, policy: dict) -> None:
    schemas.validate("contribution", record)
    schemas.validate("operation", operation)
    if policy.get("read_only", True):
        raise ProvisionRefused("the policy is read-only")
    if record["phase"] != "provisioning" or record.get("paused"):
        raise ProvisionRefused("contribution is not authorized for provisioning")
    if not record.get("plan_evidence_id") or not record.get("admission_class"):
        raise ProvisionRefused("reviewed plan and approved scope required")
    if not record["upstream"].get("repository_id") or not record["upstream"].get("base_sha"):
        raise ProvisionRefused("upstream identity and immutable base must be verified first")
    # The record names the operation it authorizes. A resume of the wait for
    # exhausted launches bumps the record's revision and keeps the planned
    # operation, so the operation may be of an earlier revision.
    if operation["kind"] != "fork_provision" or operation["status"] != "planned" \
            or operation["contribution_id"] != record["contribution_id"] or operation["revision"] > record["revision"] \
            or record.get("active_operation_id") != operation["operation_id"]:
        raise ProvisionRefused("operation is not the current authorized provisioning intent")
    expected = f"{FORK_ORG}/{record['upstream']['full_name'].split('/', 1)[1]}"
    if operation["target"]["repository"] != expected or operation["target"].get("ref") != DEV_REF \
            or operation["target"].get("base_sha") != record["upstream"]["base_sha"]:
        raise ProvisionRefused("operation target does not match approved source/base")


def check_fork(fork: dict, plan: dict) -> None:
    if (fork.get("full_name") or "").lower() != plan["fork"].lower() or not fork.get("fork") \
            or (fork.get("parent") or {}).get("id") != plan["upstream_id"] or fork.get("archived"):
        raise ProvisionError("fork identity/network is not the approved upstream",
                             failure="fork_mismatch")
    if plan.get("fork_id") not in (None, fork["id"]):
        raise ProvisionError("fork repository ID changed", failure="fork_mismatch")


def preflight(api, record: dict, operation: dict, policy: dict) -> dict:
    validate_request(record, operation, policy)
    up = record["upstream"]
    source = api.get(f"/repos/{up['full_name']}")
    if source["id"] != up["repository_id"] or source["full_name"].lower() != up["full_name"].lower():
        raise ProvisionError("upstream repository identity changed", failure="source_changed")
    commit = api.get(f"/repos/{up['full_name']}/git/commits/{up['base_sha']}")
    if commit["sha"] != up["base_sha"]:
        raise ProvisionError("upstream base commit identity changed", failure="source_changed")
    plan = {"operation_id": operation["operation_id"], "contribution_id": record["contribution_id"],
            "revision": record["revision"], "upstream": up["full_name"], "upstream_id": up["repository_id"],
            "fork": operation["target"]["repository"], "fork_name": operation["target"]["repository"].split('/', 1)[1],
            "fork_id": operation["target"].get("repository_id"), "base_sha": up["base_sha"],
            "expected_head": operation["target"].get("expected_head"), "create": False}
    try:
        fork = api.get(f"/repos/{plan['fork']}")
    except collect.CollectError as e:
        if "HTTP 404" not in str(e):
            raise
        plan["create"] = True
        return plan
    check_fork(fork, plan)
    plan["fork_id"] = fork["id"]
    return plan


def create_or_reuse(reader, creator, plan: dict) -> dict:
    try:
        existing = reader.get(f"/repos/{plan['fork']}")
    except collect.CollectError as e:
        if "HTTP 404" not in str(e):
            raise
    else:
        check_fork(existing, plan)
        return dict(plan, fork_id=existing["id"], created=False)
    if not plan["create"] or creator is None:
        raise ProvisionError("fork disappeared; not authorized to recreate it",
                             failure="fork_mismatch")
    result = creator.write("POST", f"/repos/{plan['upstream']}/forks",
                           {"organization": FORK_ORG, "name": plan["fork_name"], "default_branch_only": True})
    check_fork(result, plan)
    return dict(plan, fork_id=result["id"], created=True)


def _literal_false(value) -> bool:
    return value is False or isinstance(value, str) and value.strip().lower() in ("false", "${{ false }}")


def _workflow_unsafe(workflow: dict, text: str) -> bool:
    """Protect the fork itself, not external publication credentials.

    riseproject-upstreaming is a credentialless fork-only organization. OIDC,
    Twine and package-publisher steps receive no configured relying-party trust
    or package credentials there, so they are allowed to fail naturally. GitHub
    repository mutation authority remains forbidden.
    """
    if re.search(r"\bpull_request_target\s*:", text):
        return True
    permissions = workflow.get("permissions", {})
    if not isinstance(permissions, dict) or any(permissions.get(k) == "write" for k in
        ("contents", "issues", "pull-requests", "administration", "workflows")):
        return True
    for job in (workflow.get("jobs") or {}).values():
        if not isinstance(job, dict) or _literal_false(job.get("if")):
            continue
        permissions = job.get("permissions", {})
        if not isinstance(permissions, dict) or any(permissions.get(k) == "write" for k in
            ("contents", "issues", "pull-requests", "administration", "workflows")):
            return True
    return False


def configure_inherited_workflows(api, fork: str, content_ref: str) -> dict[str, list[str]]:
    """Enable safe CI and disable privileged/publication workflows.
    Contents are the authoritative inventory because a fresh public fork can
    report zero Actions workflows until one is explicitly enabled."""
    entries = api.get(f"/repos/{fork}/contents/.github/workflows?ref={urllib.parse.quote(content_ref)}")
    if not isinstance(entries, list) or len(entries) > 100:
        raise ProvisionError("workflow contents inventory is missing or exceeds bounded setup scan",
                             failure="workflows")
    inventory = api.get(f"/repos/{fork}/actions/workflows?per_page=100")
    if inventory.get("total_count", 0) > 100:
        raise ProvisionError("Actions workflow inventory exceeds bounded setup scan",
                             failure="workflows")
    registered_now = False
    if not inventory.get("workflows") and entries:
        # GitHub can leave inherited workflows unregistered on a fresh public
        # fork even when repository Actions is enabled. Reapply the repository
        # Actions switch with the one-fork Administration token, then require
        # the files to appear before opening/reopening a PR.
        api.write("PUT", f"/repos/{fork}/actions/permissions", {"enabled": False})
        api.write("PUT", f"/repos/{fork}/actions/permissions", {"enabled": True, "allowed_actions": "all"})
        inventory = api.get(f"/repos/{fork}/actions/workflows?per_page=100")
        if not inventory.get("workflows"):
            raise ProvisionError("fork workflows remain unregistered after Actions re-enable",
                                 failure="settings")
        registered_now = True
    states = {wf.get("path"): wf.get("state") for wf in inventory.get("workflows", [])}
    names = [entry.get("name") for entry in entries if entry.get("type") == "file"]
    if len(names) != len(set(names)):
        raise ProvisionError("workflow filenames must be unique", failure="workflows")
    changed = {"enabled": [], "disabled": []}
    for entry in entries:
        path = entry.get("path")
        if entry.get("type") != "file" or not isinstance(path, str) \
                or not path.startswith(".github/workflows/") or ".." in path.split("/") \
                or not path.endswith((".yml", ".yaml")):
            raise ProvisionError("unexpected inherited workflow path", failure="workflows")
        data = api.get(f"/repos/{fork}/contents/{urllib.parse.quote(path, safe='/')}?ref={urllib.parse.quote(content_ref)}")
        if data.get("type") != "file" or data.get("encoding") != "base64":
            raise ProvisionError("workflow must be a regular data file", failure="workflows")
        raw = base64.b64decode(data["content"])
        if len(raw) > 1024 * 1024:
            raise ProvisionError("oversized inherited workflow", failure="workflows")
        text = raw.decode()
        workflow = yaml.safe_load(text)
        if not isinstance(workflow, dict):
            raise ProvisionError("invalid inherited workflow", failure="workflows")
        unsafe = _workflow_unsafe(workflow, text)
        state = states.get(path)
        identifier = urllib.parse.quote(entry["name"], safe="")
        if unsafe:
            if state == "active":
                api.write("PUT", f"/repos/{fork}/actions/workflows/{identifier}/disable", {})
            changed["disabled"].append(path)
        elif state != "active":
            api.write("PUT", f"/repos/{fork}/actions/workflows/{identifier}/enable", {})
            changed["enabled"].append(path)
        elif registered_now:
            # Registration itself changed CI eligibility even when GitHub
            # immediately reports the safe workflow as active. The caller must
            # emit one normal PR event for the already-open development PR.
            changed["enabled"].append(path)
    return changed


def initialize(api, plan: dict) -> dict:
    fork = api.get(f"/repos/{plan['fork']}")
    check_fork(fork, plan)
    head = collect.branch_head(api, plan["fork"], DEV_REF)
    if head and head["head"] not in (plan["base_sha"], plan.get("expected_head")):
        raise ProvisionError(f"unexpected existing {DEV_REF} work; never reset it",
                             failure="branch_conflict")
    if not head and plan.get("expected_head"):
        raise ProvisionError("recorded branch disappeared; recreation is not authorized",
                             failure="branch_conflict")
    # Avoid requiring repository-administration permission when the organization
    # and newly created fork already have the fail-closed desired settings. If a
    # setting differs, attempt the narrow write and require exact readback.
    permissions = api.get(f"/repos/{plan['fork']}/actions/permissions/workflow")
    if permissions.get("default_workflow_permissions") != "read" \
            or permissions.get("can_approve_pull_request_reviews"):
        api.write("PUT", f"/repos/{plan['fork']}/actions/permissions/workflow",
                  {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False})
    actions = api.get(f"/repos/{plan['fork']}/actions/permissions")
    if actions.get("enabled") is not True or actions.get("allowed_actions") != "all":
        api.write("PUT", f"/repos/{plan['fork']}/actions/permissions", {"enabled": True, "allowed_actions": "all"})
    permissions = api.get(f"/repos/{plan['fork']}/actions/permissions/workflow")
    actions = api.get(f"/repos/{plan['fork']}/actions/permissions")
    if permissions.get("default_workflow_permissions") != "read" \
            or permissions.get("can_approve_pull_request_reviews") \
            or actions.get("enabled") is not True or actions.get("allowed_actions") != "all":
        raise ProvisionError("fork Actions settings readback failed", failure="settings")
    configured = configure_inherited_workflows(api, plan["fork"], fork["default_branch"])
    if head is None:
        api.write("POST", f"/repos/{plan['fork']}/git/refs", {"ref": f"refs/heads/{DEV_REF}", "sha": plan["base_sha"]})
    observed = collect.branch_head(api, plan["fork"], DEV_REF)
    if not observed or observed["head"] != (head["head"] if head else plan["base_sha"]):
        raise ProvisionError("fork provisioning branch readback failed", failure="branch_conflict")
    return dict(plan, status="provisioned", head_sha=observed["head"], actions_read_only=True,
                enabled_workflows=configured["enabled"], disabled_workflows=configured["disabled"])


def read_request(api, operation_id: str) -> tuple[dict, dict, dict]:
    """The record, the operation and the policy at the head of `main`, not
    yet validated."""
    if not re.fullmatch(r"op-[0-9a-f]{24}", operation_id):
        raise ProvisionRefused("invalid operation ID")
    def read(path):
        data = api.get(f"/repos/{CONTROL_REPO}/contents/{path}?ref=main")
        if data.get("encoding") != "base64" or data.get("type") != "file":
            raise ProvisionRefused("trusted state input is not a regular file")
        raw = base64.b64decode(data["content"])
        if len(raw) > 256 * 1024:
            raise ProvisionRefused("oversized state input")
        return json.loads(raw)
    op = read(f".state/operations/{operation_id}.json")
    cid = contribution_file_id(op["contribution_id"])
    record = state.migrate_names("contributions", read(f".state/contributions/{cid}.json"))[0]
    policy = read("policy.json")
    return record, op, policy


def current_request(api, operation_id: str) -> tuple[dict, dict, dict]:
    record, op, policy = read_request(api, operation_id)
    validate_request(record, op, policy)
    return record, op, policy


def failure_receipt(meta: dict, stage: str, error: BaseException) -> dict | None:
    """The receipt of a run that did not provision: ``failed`` for a definite
    failure of the effect, with its closed code; ``refused`` when the final
    check before the effect did not pass. ``None`` when the failure is not
    definite: no receipt, and the controller launches the run again."""
    if isinstance(error, ProvisionUnconfirmed) or not isinstance(error, ProvisionError):
        return None
    refused = isinstance(error, ProvisionRefused)
    failure = error.failure if error.failure in FAILURES else "other"
    return meta | {"status": "refused" if refused else "failed", "stage": stage,
                   "failure": None if refused else failure,
                   "reason": isolation.clean_text(str(error), 300)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=STAGES)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt", type=Path,
                        help="where a definite failure or a refusal is reported "
                             "(default: outcome.json beside --out)")
    args = parser.parse_args(argv)
    receipt_path = args.receipt or args.out.parent / "outcome.json"
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != CONTROL_REPO \
            or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ProvisionError("fork mutations are allowed only in trusted control-repository workflows on main")
    reader = collect.Api()
    record, operation, policy = read_request(reader, args.operation_id)
    meta = {"operation_id": args.operation_id, "contribution_id": record.get("contribution_id"),
            "revision": record.get("revision"), "run_id": os.environ.get("GITHUB_RUN_ID")}
    try:
        validate_request(record, operation, policy)
        plan = preflight(reader, record, operation, policy)
        if args.mode == "create":
            creator = WriteApi(token=os.environ.get("UPSTREAMING_CREATION_TOKEN")) if plan["create"] else None
            plan = create_or_reuse(reader, creator, plan)
        elif args.mode == "initialize":
            previous = json.loads(args.plan.read_text())
            if any(previous[k] != plan[k] for k in ("operation_id", "contribution_id", "revision", "upstream_id",
                                                     "base_sha", "fork")):
                raise ProvisionRefused("provisioning preconditions changed")
            plan["fork_id"] = previous["fork_id"]
            plan["created"] = previous["created"]
            plan = initialize(WriteApi(token=os.environ["UPSTREAMING_FORK_TOKEN"]), plan)
    except ProvisionError as error:
        receipt = failure_receipt(meta, args.mode, error)
        if receipt is not None:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(json.dumps(receipt, indent=2))
        print(json.dumps({"operation_id": args.operation_id,
                          "status": receipt["status"] if receipt else "unconfirmed",
                          "stage": args.mode, "failure": receipt["failure"] if receipt else None}))
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, indent=2))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as f:
            f.write(f"create={str(plan['create']).lower()}\nfork_name={plan['fork_name']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
