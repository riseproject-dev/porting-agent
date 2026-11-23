# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Read-only collectors.

Every network call here is a GET. Nothing writes. Results are normalized
for ``controller.tick`` and ``ci_evidence``.

``Api`` wraps the GitHub REST API with the token in ``GH_TOKEN``/
``GITHUB_TOKEN`` (urllib, no third-party client), handles pagination and
refuses anything but a GET.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import ci_evidence
import commands
import intake
import lifecycle
from common import CONTROL_REPO, DEV_REF, INTAKE_LABEL, UPSTREAM_REF

API = "https://api.github.com"
_PATH_RE = re.compile(r"^/[A-Za-z0-9_./%?&=,:+-]*$")


class CollectError(RuntimeError):
    pass


class BranchRenamed(CollectError):
    """GitHub answered the read of a branch with the branch it was renamed
    to (``name``): the branch asked for no longer exists under its name."""

    def __init__(self, ref: str, name: str):
        super().__init__(f"GET branch {ref!r}: the response names another branch {name[:100]!r}")
        self.name = name


# Bound of the transport error text that a ``CollectError`` quotes.
_MAX_ERROR_TEXT = 200

# What reading a response can raise besides ``HTTPError``: the HTTP layer
# (``IncompleteRead``, ``RemoteDisconnected``, a bad status line) and the
# transport (``URLError``, TLS errors, socket timeouts, reset connections;
# all ``OSError``).
_TRANSPORT_ERRORS = (http.client.HTTPException, OSError)


def _transport_error(url: str, error: BaseException) -> CollectError:
    """A failed read as a ``CollectError`` with a bounded one-line message."""
    text = " ".join(str(error).split())
    if len(text) > _MAX_ERROR_TEXT:
        text = text[: _MAX_ERROR_TEXT - 3] + "..."
    return CollectError(f"GET {url}: {type(error).__name__}: {text}" if text
                        else f"GET {url}: {type(error).__name__}")


class _ArtifactRedirect(urllib.request.HTTPRedirectHandler):
    """Follow GitHub's artifact redirect without leaking its bearer token."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            host = (new.hostname or "").lower()
            if new.scheme != "https" or not (host.endswith(".blob.core.windows.net") or
                                               host.endswith(".githubusercontent.com")):
                raise CollectError("refusing artifact redirect to an unexpected origin")
            redirected.remove_header("Authorization")
        return redirected


class Api:
    def __init__(self, token: str | None = None, base: str = API, opener=None):
        self.token = token if token is not None else (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
        self.base = base
        self._open = opener or urllib.request.build_opener(_ArtifactRedirect()).open

    def _request(self, url: str) -> tuple[Any, dict]:
        req = urllib.request.Request(url, method="GET", headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "riseproject-porting-agent",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        # Every failure to read the response, including a body that is cut
        # off or is not JSON, is a ``CollectError``: the collector could not
        # read GitHub. Only the transport and the decoding of the body are
        # converted; what the caller does with a well-formed response is not.
        try:
            with self._open(req, timeout=30) as r:
                body = r.read()
                headers = dict(r.headers)
        except urllib.error.HTTPError as e:
            raise CollectError(f"GET {url}: HTTP {e.code}") from None
        except _TRANSPORT_ERRORS as e:
            raise _transport_error(url, e) from e
        if not body:
            return None, headers
        try:
            return json.loads(body), headers
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise CollectError(f"GET {url}: response body is not JSON: {type(e).__name__}") from None

    def get(self, path: str) -> Any:
        if not _PATH_RE.match(path) or ".." in path:
            raise CollectError(f"refusing unexpected API path {path!r}")
        return self._request(self.base + path)[0]

    def get_bytes(self, path: str, *, max_bytes: int = 8 * 1024 * 1024) -> bytes:
        """GET a binary resource (e.g. an artifact zip). The API redirects to
        blob storage; urllib follows it and drops Authorization cross-host."""
        if not _PATH_RE.match(path) or ".." in path:
            raise CollectError(f"refusing unexpected API path {path!r}")
        req = urllib.request.Request(self.base + path, method="GET", headers={
            "Accept": "application/vnd.github+json", "User-Agent": "riseproject-porting-agent",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with self._open(req, timeout=60) as r:
                data = r.read(max_bytes + 1)
        except urllib.error.HTTPError as e:
            raise CollectError(f"GET {path}: HTTP {e.code}") from None
        except _TRANSPORT_ERRORS as e:
            raise _transport_error(path, e) from e
        if len(data) > max_bytes:
            raise CollectError("download exceeds size limit")
        return data

    def paginate(self, path: str, *, key: str | None = None, max_pages: int = 10) -> list:
        """Follow Link: rel=next up to ``max_pages``. ``key`` selects the list
        inside an object response (e.g. ``check_runs``)."""
        if not _PATH_RE.match(path) or ".." in path:
            raise CollectError(f"refusing unexpected API path {path!r}")
        url = self.base + path
        out: list = []
        for _ in range(max_pages):
            data, headers = self._request(url)
            out.extend(data[key] if key else data)
            nxt = _next_link(headers.get("Link") or headers.get("link") or "")
            if not nxt:
                break
            if not nxt.startswith(self.base + "/"):
                raise CollectError("pagination left the API host")
            url = nxt
        return out


def _next_link(link: str) -> str | None:
    for part in link.split(","):
        m = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------
# Control-repository inputs


def intake_issues(api: Api, max_pages: int = 3) -> list[dict]:
    items = api.paginate(f"/repos/{CONTROL_REPO}/issues?labels={INTAKE_LABEL}&state=open&per_page=50",
                         max_pages=max_pages)
    return [i for i in items if "pull_request" not in i]


def tracking_issue_context(issue: dict, expected: dict) -> dict:
    """Freeze the whole issue description into a task; never truncate it.
    The URL is constructed from the verified control-repository issue identity.
    Comments remain available for the agent to read through that URL/API."""
    from common import sha256_text
    if issue.get("id") != expected["id"] or issue.get("number") != expected["number"]:
        raise CollectError("tracking issue identity changed")
    body = issue.get("body") or ""
    if not isinstance(body, str) or len(body.encode("utf-8")) > 16384:
        raise CollectError("tracking issue description exceeds the intake limit; not truncated")
    return {"id": issue["id"], "number": issue["number"],
            "url": f"https://github.com/{CONTROL_REPO}/issues/{issue['number']}",
            "description": body, "description_digest": sha256_text(body)}


def tracking_issues(api: Api, contributions: Iterable[dict], intake_issues: list[dict]) -> dict[int, dict]:
    """Fresh descriptions for every live tracked issue (not just labeled intake)."""
    issues = {i["number"]: i for i in intake_issues}
    for rec in contributions:
        n = rec["tracking_issue"]["number"]
        if rec["phase"] not in lifecycle.TERMINAL and n not in issues:
            issues[n] = api.get(f"/repos/{CONTROL_REPO}/issues/{n}")
    return issues


_EXPLANATION_WORDS = re.compile(
    r"(?:^|\b)(?:why|what happened|how did|explain|explanation|diagnose|diagnosis)(?:\b|$)", re.IGNORECASE)
_REPAIR_WORDS = re.compile(
    r"(?:^|\b)(?:fix|update|add|remove|revert|disable|enable|implement|rename|delete|replace|set|use)(?:\b|$)",
    re.IGNORECASE)
_NO_CHANGE = re.compile(
    r"(?:do not|don't|dont|please do not|please don't)\s+(?:make\s+(?:any\s+)?)?(?:change|changes|modify|edit|push|commit|fix)\b|"
    r"\bwithout\s+(?:changing|modifying|editing|pushing|committing|fixing)\b|"
    r"\bno\s+(?:code\s+)?changes?\b",
    re.IGNORECASE)


def review_comment_intent(body: str) -> str:
    """Conservatively distinguish an explanation request from repair feedback.

    An explicit no-change instruction is authoritative. Otherwise only a clear
    explanatory question with no repair verb is treated as inquiry-only.
    """
    text = " ".join(body.split())
    if _NO_CHANGE.search(text):
        return "inquiry"
    if _EXPLANATION_WORDS.search(text) and not _REPAIR_WORDS.search(text):
        return "inquiry"
    return "repair"


def development_review_feedback(api: Api, record: dict, authority: Iterable[int], *,
                                check_pull: bool = False) -> dict | None:
    """Return new authorized review feedback or inquiries bound to the current fork head.

    With ``check_pull`` the development PR is read first: when it does not
    match the fork, the ref, and the PR the record names
    (``development_pull_problem``), only ``{"pr_mismatch": <why>}`` is
    returned and no feedback is read."""
    fork = record.get("fork") or {}
    number = fork.get("pr_number")
    head = fork.get("head_sha")
    if record.get("phase") not in ("development", "handoff") or not fork.get("name") or not number or not head:
        return None
    mismatch = development_pull_problem(api, record) if check_pull else None
    if mismatch:
        return {"pr_mismatch": mismatch}
    return _review_feedback(api, record, fork["name"], int(number), head, authority)


def upstream_review_feedback(api: Api, record: dict, authority: Iterable[int]) -> dict | None:
    """New authorized review feedback or inquiries on the upstream PR, bound
    to its recorded head (``upstream_pr.head_sha``). Authorized means written
    by an account in the command authority, as on the development PR."""
    pr = record.get("upstream_pr") or {}
    if record.get("phase") != "upstream_review" or not pr.get("number") or not pr.get("head_sha"):
        return None
    return _review_feedback(api, record, record["upstream"]["full_name"], int(pr["number"]),
                            pr["head_sha"], authority)


def _review_feedback(api: Api, record: dict, repository: str, number: int, head: str,
                     authority: Iterable[int]) -> dict | None:
    """The new authorized review feedback and inquiries on the pull request
    ``number`` of ``repository`` bound to ``head``, or ``None``."""
    allowed = {int(value) for value in authority}
    seen_reviews = set(record.get("processed_review_ids") or [])
    seen_comments = set(record.get("processed_review_comment_ids") or [])
    seen_conversation = set(record.get("processed_pr_conversation_comment_ids") or [])
    reviews = api.paginate(f"/repos/{repository}/pulls/{number}/reviews?per_page=100", max_pages=3)
    comments = api.paginate(f"/repos/{repository}/pulls/{number}/comments?per_page=100", max_pages=5)
    conversation = api.paginate(f"/repos/{repository}/issues/{number}/comments?per_page=100", max_pages=5)
    head_at = None
    if conversation:
        commit = api.get(f"/repos/{repository}/commits/{head}")
        head_at = datetime.fromisoformat(
            ((commit.get("commit") or {}).get("committer") or {})["date"].replace("Z", "+00:00"))
    review_items = []
    for review in reviews:
        body = (review.get("body") or "").strip()
        if review.get("id") in seen_reviews or review.get("commit_id") != head \
                or (review.get("user") or {}).get("id") not in allowed \
                or review.get("state") != "CHANGES_REQUESTED" or not body:
            continue
        review_items.append({"id": review["id"], "body": body[:8000], "url": review.get("html_url"),
                             "reviewer": (review.get("user") or {}).get("login"), "commit_id": head})
    comment_items = []
    for comment in comments:
        body = (comment.get("body") or "").strip()
        if comment.get("id") in seen_comments or comment.get("commit_id") != head \
                or (comment.get("user") or {}).get("id") not in allowed or not body:
            continue
        comment_items.append({"id": comment["id"], "body": body[:8000], "url": comment.get("html_url"),
                              "review_id": comment.get("pull_request_review_id"),
                              "reviewer": (comment.get("user") or {}).get("login"),
                              "path": comment.get("path"), "line": comment.get("line") or comment.get("original_line"),
                              "commit_id": head, "intent": review_comment_intent(body)})
    conversation_items = []
    for comment in conversation:
        body = (comment.get("body") or "").strip()
        created = comment.get("created_at")
        if comment.get("id") in seen_conversation or (comment.get("user") or {}).get("id") not in allowed \
                or not body or not created or head_at is None \
                or datetime.fromisoformat(created.replace("Z", "+00:00")) < head_at:
            continue
        conversation_items.append({"id": comment["id"], "body": body[:8000],
                                   "url": comment.get("html_url"),
                                   "reviewer": (comment.get("user") or {}).get("login"),
                                   "created_at": created, "commit_id": head,
                                   "intent": review_comment_intent(body)})
    if not review_items and not comment_items and not conversation_items:
        return None
    inquiry_comments = [item for item in comment_items if item["intent"] == "inquiry"]
    inquiry_conversation = [item for item in conversation_items if item["intent"] == "inquiry"]
    repair_comments = [item for item in comment_items if item["intent"] == "repair"]
    repair_conversation = [item for item in conversation_items if item["intent"] == "repair"]
    return {"reviews": review_items, "comments": repair_comments,
            "conversation_comments": repair_conversation,
            "review_ids": [item["id"] for item in review_items],
            "comment_ids": [item["id"] for item in repair_comments],
            "conversation_comment_ids": [item["id"] for item in repair_conversation],
            "inquiry_comments": inquiry_comments,
            "inquiry_conversation_comments": inquiry_conversation,
            "inquiry_comment_ids": [item["id"] for item in inquiry_comments],
            "inquiry_conversation_comment_ids": [item["id"] for item in inquiry_conversation]}


def _commands_by_issue(comments: Iterable[dict], policy: dict,
                       consumed: Iterable[int]) -> dict[int, dict[str, commands.Command]]:
    """The commands on each tracking issue the controller can still take, by
    verb: a comment that ``commands.parse`` accepts, by an actor of
    ``command_authority``, whose comment id is not in ``consumed`` (the
    command ledger). The latest comment of a verb wins."""
    authority = set(policy["command_authority"]["actor_ids"])
    seen = set(consumed)
    out: dict[int, dict[str, commands.Command]] = {}
    for item in comments:
        comment = item.get("comment") or {}
        if comment.get("id") in seen or (comment.get("user") or {}).get("id") not in authority:
            continue
        try:
            command = commands.parse(comment.get("body") or "")
        except commands.CommandError:
            continue
        out.setdefault(int(item["issue_number"]), {})[command.verb] = command
    return out


def _refresh_branch(c: dict, command: commands.Command) -> str | None | bool:
    """The branch a refresh from ``parked.idle`` binds: its ``branch``, else
    the target branch the request asked for at intake, else ``None`` (the
    default branch). ``False`` for a ``branch`` the controller refuses."""
    if "branch" not in command.args:
        return intake.requested_target_branch(c["tracking_issue"])
    try:
        return intake.canonical_branch(command.args["branch"])
    except intake.IntakeError:
        return False


def upstream_snapshots(api: Api, contributions: Iterable[dict], issues: list[dict], policy: dict,
                       comments: Iterable[dict] = (),
                       consumed: Iterable[int] = ()) -> dict[str, dict]:
    """Verify source IDs and initial immutable bases before task admission.
    Errors are data: urgent commands must still run when an upstream is absent.

    A snapshot that could not be read is ``{"error": ...}``. When the
    upstream answered definitively that the base branch is not there (it does
    not exist, or GitHub redirects it to the branch it was renamed to), the
    snapshot also carries ``base_ref``, ``missing: True``, and for a rename
    ``renamed_to``; the controller then takes ``require_base_branch`` at once.

    A contribution waiting for a human because its base branch is
    unavailable (``pending_human_action`` ``base_branch_unavailable``) is not
    read, unless its tracking issue carries a resume in ``comments``. A
    parked contribution is read only when its tracking issue carries a
    refresh: a restart clears the identity and base, and the controller binds
    them again in the tick that restarts it. A command is counted only when
    ``commands.parse`` accepts it, it is by an actor of
    ``command_authority``, and its comment id is not in ``consumed`` (the
    command ledger); whether its row then applies is decided by the
    controller. A parked upstream is read at the ``branch`` of the refresh,
    else at the target branch the request asked for at intake, else at its
    default branch."""
    contributions = list(contributions)
    posted = _commands_by_issue(comments, policy, consumed)
    parked = {c["upstream"]["full_name"] for c in contributions if c["phase"] == "parked"}
    refs: dict[str, str | None] = {}
    for c in contributions:
        if c["phase"] in lifecycle.TERMINAL:
            continue
        verbs = posted.get(c["tracking_issue"]["number"], {})
        name = c["upstream"]["full_name"]
        if c["phase"] == "parked":
            if "refresh" in verbs:
                branch = _refresh_branch(c, verbs["refresh"])
                if branch is not False:
                    refs.setdefault(name, branch)
            continue
        waiting = (c.get("pending_human_action") or {}).get("kind") \
            == lifecycle.BASE_BRANCH_UNAVAILABLE
        if waiting and "resume" not in verbs:
            continue
        refs[name] = c["upstream"].get("base_ref")
    known = {c["tracking_issue"]["id"] for c in contributions}
    for issue in issues:
        if issue["id"] in known:
            continue
        try:
            req = intake.parse_request(issue, policy)
        except intake.IntakeError:
            continue
        if req.upstream not in parked:
            refs.setdefault(req.upstream, req.target_branch)
    snapshots = {}
    for name, ref in refs.items():
        try:
            repo = repository(api, name)
            if repo["full_name"].lower() != name.lower():
                raise CollectError("upstream repository name changed")
            base_ref = ref or repo["default_branch"]
            try:
                base = branch_head(api, name, base_ref)
            except BranchRenamed as e:
                snapshots[name] = {"error": f"upstream base branch {base_ref!r} was renamed",
                                   "base_ref": base_ref, "missing": True,
                                   "renamed_to": e.name[:255]}
                continue
            if base is None:
                snapshots[name] = {"error": f"upstream base branch {base_ref!r} does not exist",
                                   "base_ref": base_ref, "missing": True}
                continue
            snapshots[name] = {"repository_id": repo["id"], "base_ref": base_ref, "base_sha": base["head"]}
        except CollectError as e:
            snapshots[name] = {"error": str(e)}
    return snapshots


def label_acceptances(api: Api, issues: Iterable[dict], label: str, authority: Iterable[int]) -> dict[int, int]:
    """issue id -> the authorized actor who added ``label`` (from the issue's
    events). Adding the intake label is how a RISE owner accepts a request
    filed by someone else; anyone else adding it accepts nothing."""
    auth = set(authority)
    out: dict[int, int] = {}
    for i in issues:
        if i["user"]["id"] in auth:
            continue
        for ev in api.paginate(f"/repos/{CONTROL_REPO}/issues/{int(i['number'])}/events?per_page=100", max_pages=3):
            if ev.get("event") == "labeled" and (ev.get("label") or {}).get("name") == label \
                    and (ev.get("actor") or {}).get("id") in auth:
                out[i["id"]] = ev["actor"]["id"]
    return out


def dispatcher_runs(api: Api, workflow_file: str = "dispatcher.lock.yml") -> list[dict]:
    """Recent runs of the dispatcher workflow (newest first)."""
    return api.paginate(f"/repos/{CONTROL_REPO}/actions/workflows/{workflow_file}/runs?per_page=30",
                        key="workflow_runs", max_pages=1)


def command_comments(api: Api, issue_numbers: Iterable[int], since: str | None) -> list[dict]:
    """Comments on tracking issues that look like commands, for controller.Inputs.comments."""
    out = []
    for n in issue_numbers:
        q = f"?per_page=100&since={urllib.parse.quote(since)}" if since else "?per_page=100"
        for c in api.paginate(f"/repos/{CONTROL_REPO}/issues/{int(n)}/comments{q}", max_pages=3):
            if commands.looks_like_command(c.get("body") or ""):
                out.append({"issue_number": int(n), "comment": {"id": c["id"], "body": c["body"],
                                                                  "user": {"id": c["user"]["id"], "login": c["user"]["login"]}}})
    return out


def pull_request(api: Api, url: str) -> dict:
    """Verified PR resource for ``register-upstream-pr``."""
    parts = url.rstrip("/").split("/")
    if len(parts) != 7 or parts[:3] != ["https:", "", "github.com"] or parts[5] != "pull":
        raise CollectError("not a PR URL")
    pr = api.get(f"/repos/{parts[3]}/{parts[4]}/pulls/{int(parts[6])}")
    return {
        "id": pr["id"], "number": pr["number"], "html_url": pr["html_url"], "state": pr["state"],
        "base_repository": pr["base"]["repo"]["full_name"], "base_ref": pr["base"]["ref"],
        "head_repository": (pr["head"].get("repo") or {}).get("full_name"),
        "head_repository_id": (pr["head"].get("repo") or {}).get("id") or 0,
        "head_ref": pr["head"]["ref"], "head_sha": pr["head"]["sha"],
        "merged": pr.get("merged", False), "merge_commit_sha": pr.get("merge_commit_sha"),
    }


def upstream_pull(api: Api, record: dict) -> dict:
    """The upstream PR the record names, as GitHub reports it now: its state,
    whether it is merged (with the merge commit and time), its base, and its
    head. A pull request whose id is not the recorded one is refused."""
    pr = record.get("upstream_pr") or {}
    if not pr.get("number"):
        raise CollectError("the record names no upstream PR")
    data = api.get(f"/repos/{record['upstream']['full_name']}/pulls/{int(pr['number'])}")
    if not isinstance(data, dict) or data.get("id") != pr.get("id"):
        raise CollectError(f"upstream PR #{pr['number']} is not the recorded pull request")
    head, base = data.get("head") or {}, data.get("base") or {}
    merged_at = data.get("merged_at")
    return {"id": data["id"], "number": data.get("number"), "state": data.get("state"),
            "merged": bool(data.get("merged") or merged_at), "merged_at": merged_at,
            "merge_commit_sha": data.get("merge_commit_sha"),
            "base_ref": base.get("ref"), "base_repository_id": (base.get("repo") or {}).get("id"),
            "head_ref": head.get("ref"), "head_sha": head.get("sha"),
            "head_repository_id": (head.get("repo") or {}).get("id")}


def release_contains(api: Api, full_name: str, version: str, commit: str) -> bool | None:
    """Whether the release tag of ``version`` in ``full_name`` (``v<version>``
    or ``<version>``) contains ``commit``: ``True`` or ``False`` for the first
    tag that exists, ``None`` when neither exists."""
    if not _REPO_RE.match(full_name) or not _FULL_SHA.match(commit) \
            or not re.fullmatch(r"[A-Za-z0-9._+!-]{1,64}", version):
        raise CollectError("refusing an unexpected release provenance check")
    for tag in (f"v{version}", version):
        try:
            data = api.get(f"/repos/{full_name}/commits/{urllib.parse.quote('refs/tags/' + tag, safe='')}")
        except CollectError as e:
            if "HTTP 404" in str(e) or "HTTP 422" in str(e):
                continue
            raise
        sha = (data or {}).get("sha") if isinstance(data, dict) else None
        if not isinstance(sha, str) or not _FULL_SHA.match(sha):
            raise CollectError(f"tag {tag!r} resolves to no commit")
        if sha == commit:
            return True
        status = compare_commits(api, full_name, commit, sha).get("status")
        return status in ("ahead", "identical")
    return None


def repository(api: Api, full_name: str) -> dict:
    r = api.get(f"/repos/{full_name}")
    return {"id": r["id"], "full_name": r["full_name"], "default_branch": r["default_branch"],
            "fork": r.get("fork", False), "archived": r.get("archived", False),
            "parent": (r.get("parent") or {}).get("full_name"),
            "parent_id": (r.get("parent") or {}).get("id")}


def branch_head(api: Api, full_name: str, ref: str) -> dict | None:
    """{"head", "tree", "parents"} for a branch, or None if it does not exist."""
    try:
        b = api.get(f"/repos/{full_name}/branches/{urllib.parse.quote(ref, safe='')}")
    except CollectError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    # GitHub answers the branch of a renamed one with a redirect to the new
    # name: a branch whose name is not the one asked for is another branch.
    if not isinstance(b, dict):
        raise CollectError(f"GET branch {ref!r}: the response is not a branch")
    if b.get("name") != ref:
        if isinstance(b.get("name"), str) and b["name"]:
            raise BranchRenamed(ref, b["name"])
        raise CollectError(f"GET branch {ref!r}: the response names no branch")
    c = b["commit"]
    return {"head": c["sha"], "tree": c["commit"]["tree"]["sha"], "parents": [p["sha"] for p in c["parents"]]}


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def compare_commits(api: Api, full_name: str, base: str, head: str) -> dict:
    """The comparison of two exact commits of a repository: its commits
    (oldest first), files, and how far ``head`` is ahead of and behind
    ``base``. Only full SHAs are accepted, so the ``...`` of the route is
    the one dot sequence in the path."""
    if not _REPO_RE.match(full_name) or ".." in full_name \
            or not _FULL_SHA.match(base) or not _FULL_SHA.match(head):
        raise CollectError("refusing an unexpected comparison")
    data = api._request(f"{api.base}/repos/{full_name}/compare/{base}...{head}?per_page=100")[0]
    if not isinstance(data, dict):
        raise CollectError("comparison response is malformed")
    return data


def upstream_pull_requests(api: Api, record: dict, ref: str = UPSTREAM_REF) -> list[dict]:
    """The open pull requests of the upstream repository whose head is a
    branch ``ref`` of the fork's owner: the candidates the submission
    package can lead to. They are filtered by ``handoff.matching_upstream_prs``."""
    import handoff

    owner = record["fork"]["name"].split("/", 1)[0]
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{ref}", "per_page": 100})
    pulls = api.get(f"/repos/{record['upstream']['full_name']}/pulls?{query}")
    if not isinstance(pulls, list):
        raise CollectError("pull request inventory is malformed")
    return [handoff.normalize_pull(pr) for pr in pulls]


def fork_pull_requests(api: Api, fork: str, ref: str) -> list[dict]:
    """The open pull requests of the fork itself whose head is its branch
    ``ref``."""
    owner = fork.split("/", 1)[0]
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{ref}", "per_page": 100})
    pulls = api.get(f"/repos/{fork}/pulls?{query}")
    if not isinstance(pulls, list):
        raise CollectError("pull request inventory is malformed")
    # The API filters by the head; every listed pull request counts, so an
    # unexpected entry blocks rather than admits.
    return [pr for pr in pulls if isinstance(pr, dict)]


def dev_pull_request(api: Api, fork: str, ref: str = DEV_REF) -> dict | None:
    owner = fork.split("/")[0]
    prs = api.get(f"/repos/{fork}/pulls?state=open&head={owner}:{ref}&per_page=10")
    return prs[0] if prs else None


def development_pull_problem(api: Api, record: dict) -> str | None:
    """Why the development PR on GitHub does not match the fork, the ref,
    and the PR the record names, or ``None`` when it matches. A PR that
    GitHub does not have does not match."""
    fork = record.get("fork") or {}
    number = fork.get("pr_number")
    try:
        pr = api.get(f"/repos/{fork['name']}/pulls/{int(number)}")
    except CollectError as e:
        if "HTTP 404" in str(e):
            return f"development PR {number} does not exist"
        raise
    head = pr.get("head") or {}
    if pr.get("number") != number:
        return f"development PR {number} reads back as PR {pr.get('number')}"
    if (head.get("repo") or {}).get("id") != fork.get("repository_id"):
        return f"development PR {number} has its head in another repository"
    if head.get("ref") != DEV_REF:
        return f"development PR {number} has the head ref {head.get('ref')!r}"
    base = (record.get("upstream") or {}).get("base_ref")
    if base and (pr.get("base") or {}).get("ref") != base:
        return f"development PR {number} targets {(pr.get('base') or {}).get('ref')!r}, not {base!r}"
    return None


# --------------------------------------------------------------------------
# Live CI


@dataclass
class CiSnapshot:
    repository: dict
    head_sha: str
    runs: list[dict] = field(default_factory=list)
    jobs_by_run: dict[int, list[dict]] = field(default_factory=dict)
    check_runs: list[dict] = field(default_factory=list)
    statuses: list[dict] = field(default_factory=list)
    artifacts_by_run: dict[int, list[dict]] = field(default_factory=dict)


def _norm_run(r: dict) -> dict:
    return {"id": r["id"], "head_sha": r["head_sha"], "run_attempt": r.get("run_attempt", 1),
            "status": r["status"], "conclusion": r.get("conclusion"), "workflow_id": r["workflow_id"],
            "event": r["event"], "path": r.get("path", ""), "name": r.get("name", ""),
            "created_at": r.get("created_at"), "head_branch": r.get("head_branch"),
            "pull_requests": [p["number"] for p in r.get("pull_requests") or []],
            "workflow_sha": None}


def _norm_job(j: dict) -> dict:
    return {"id": j["id"], "run_id": j["run_id"], "run_attempt": j.get("run_attempt", 1),
            "name": j["name"], "status": j["status"], "conclusion": j.get("conclusion"),
            "started_at": j.get("started_at"), "completed_at": j.get("completed_at"),
            "labels": j.get("labels") or [], "runner_name": j.get("runner_name"),
            "head_sha": j.get("head_sha")}


def ci_snapshot(api: Api, full_name: str, head_sha: str, *, max_runs: int = 30) -> CiSnapshot:
    """Everything CI says about one exact head SHA: workflow runs (all
    attempts resolve to the latest by run_attempt), their jobs for that
    attempt, check runs and commit statuses, and artifacts."""
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise CollectError("head_sha must be a full SHA")
    repo = repository(api, full_name)
    snap = CiSnapshot(repository=repo, head_sha=head_sha)
    runs = api.paginate(f"/repos/{full_name}/actions/runs?head_sha={head_sha}&per_page=50",
                        key="workflow_runs", max_pages=2)
    snap.runs = [_norm_run(r) for r in runs][:max_runs]
    for r in ci_evidence.latest_attempts(snap.runs):
        jobs = api.paginate(f"/repos/{full_name}/actions/runs/{r['id']}/attempts/{r['run_attempt']}/jobs?per_page=100",
                            key="jobs", max_pages=5)
        snap.jobs_by_run[r["id"]] = [_norm_job(j) for j in jobs]
        if r["status"] == "completed":
            arts = api.paginate(f"/repos/{full_name}/actions/runs/{r['id']}/artifacts?per_page=100",
                                key="artifacts", max_pages=2)
            snap.artifacts_by_run[r["id"]] = [{"id": a["id"], "name": a["name"], "size": a["size_in_bytes"],
                                               "digest": a.get("digest"), "expired": a.get("expired", False)}
                                              for a in arts]
    snap.check_runs = [{"id": c["id"], "name": c["name"], "status": c["status"], "conclusion": c.get("conclusion"),
                        "app": (c.get("app") or {}).get("slug")}
                       for c in api.paginate(f"/repos/{full_name}/commits/{head_sha}/check-runs?per_page=100",
                                             key="check_runs", max_pages=5)]
    snap.statuses = [{"context": s["context"], "state": s["state"]}
                     for s in api.paginate(f"/repos/{full_name}/commits/{head_sha}/statuses?per_page=100", max_pages=2)]
    return snap


def wheel_names_from_artifacts(snap: CiSnapshot) -> list[str]:
    """Upload-artifact names are not file names; wheel jobs in this ecosystem
    conventionally name artifacts after the wheel or ``wheels-<tag>``, or
    ``<python>-<platform>-...``. This returns names ending in .whl and the
    names of artifacts that mention riscv64; callers that need exact files
    download the artifact in a sandbox (never in a credentialed job)."""
    return sorted({a["name"] for arts in snap.artifacts_by_run.values() for a in arts
                   if not a["expired"] and (a["name"].endswith(".whl") or "riscv64" in a["name"])
                   and a.get("size", 1) > 0})


def evaluate_snapshot(snap: CiSnapshot, *, required_job_patterns: list[str], expected_wheels: list[str],
                      produced_wheels: list[str] | None = None,
                      required_runner_label: str | None = None) -> ci_evidence.CiVerdict:
    produced = produced_wheels if produced_wheels is not None else wheel_names_from_artifacts(snap)
    return ci_evidence.evaluate(runs=snap.runs, jobs_by_run=snap.jobs_by_run, head_sha=snap.head_sha,
                                required_job_patterns=required_job_patterns,
                                expected_wheels=expected_wheels, produced_wheels=produced,
                                required_runner_label=required_runner_label)


# --------------------------------------------------------------------------
# Other sources


def pypi_release(distribution: str, opener=None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", distribution):
        raise CollectError("invalid distribution name")
    with (opener or urllib.request.urlopen)(f"https://pypi.org/pypi/{distribution}/json", timeout=30) as r:
        return json.load(r)


def pypi_project(distribution: str, opener=None) -> dict | None:
    """The PyPI JSON document of ``distribution``, or ``None`` when PyPI has
    no such project. Any other failure is a collector failure."""
    try:
        return pypi_release(distribution, opener=opener)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise CollectError(f"PyPI {distribution}: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise CollectError(f"PyPI {distribution}: {e}") from e


def usage(url: str, opener=None) -> tuple[Any, datetime]:
    with (opener or urllib.request.urlopen)(url, timeout=30) as r:
        return json.load(r), datetime.now(timezone.utc)


def ecosystem_report(api: Api, project: str) -> str | None:
    """The sw-ecosystem project report for ``project``, if one exists (plan step 2)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", project):
        raise CollectError("invalid project name")
    try:
        data = api.get(f"/repos/riseproject-dev/sw-ecosystem/contents/project-reports/{project}.md")
    except CollectError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    import base64
    return base64.b64decode(data["content"]).decode("utf-8", "replace")
