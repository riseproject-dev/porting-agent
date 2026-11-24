# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Read-only collectors.

Every network call here is a GET. Nothing writes. Results are normalized
for ``controller.tick`` and ``ci_evidence``.

``Api`` wraps the GitHub REST API with the token in ``GH_TOKEN``/
``GITHUB_TOKEN`` (``requests``), handles pagination and refuses anything but
a GET. When ``PORTING_HTTP_CACHE`` names a file, reads go through an HTTP
cache that is always revalidated (see ``http_cache_path``).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
import requests_cache

import ci_evidence
import commands
import intake
import lifecycle
from common import CONTROL_REPO, DEV_REF, INTAKE_LABEL, UPSTREAM_REF

API = "https://api.github.com"
_PATH_RE = re.compile(r"^/[A-Za-z0-9_./%?&=,:+-]*$")


class CollectError(RuntimeError):
    """A failed GitHub read. ``status`` is the HTTP status of a read that
    GitHub answered with an error, else ``None``."""

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status


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


# --------------------------------------------------------------------------
# Transport

# The environment variable that names the HTTP cache file of the reads. Unset,
# nothing is cached and no file is created.
HTTP_CACHE_ENV = "PORTING_HTTP_CACHE"
# Entries older than this are dropped when the cache is opened, and a file
# larger than the size bound is emptied: the cache is only an optimization.
HTTP_CACHE_MAX_AGE = timedelta(days=7)
HTTP_CACHE_MAX_BYTES = 64 * 1024 * 1024
# Never part of a cache key and never stored in a cached request.
_SECRET_PARAMETERS = ("Authorization", "Proxy-Authorization", "Cookie", "Set-Cookie",
                      "X-API-Key", "X-Auth-Token", "X-API-Token", "X-Access-Token",
                      "access_token", "api_key", "apikey")


def _checked_redirect(old_url: str, new_url: str, headers) -> None:
    """The rule for a redirect: staying on the origin keeps the request; a
    cross-origin redirect is followed only to blob storage over https, and
    drops ``Authorization``."""
    old = urllib.parse.urlsplit(old_url)
    new = urllib.parse.urlsplit(new_url)
    if (old.scheme, old.netloc) != (new.scheme, new.netloc):
        host = (new.hostname or "").lower()
        if new.scheme != "https" or not (host.endswith(".blob.core.windows.net") or
                                           host.endswith(".githubusercontent.com")):
            raise CollectError("refusing artifact redirect to an unexpected origin")
        headers.pop("Authorization", None)


class _ArtifactRedirect:
    """Follow GitHub's artifact redirect without leaking its bearer token.
    ``requests`` calls ``rebuild_auth`` for every redirect it follows; it also
    would re-read ``~/.netrc`` there, which is never wanted."""

    def rebuild_auth(self, prepared_request, response):
        _checked_redirect(response.request.url, prepared_request.url, prepared_request.headers)


class _Session(_ArtifactRedirect, requests.Session):
    def __init__(self):
        super().__init__()
        # No ``~/.netrc`` credentials: the token is the only credential.
        self.auth = lambda request: request


class _CachedSession(_ArtifactRedirect, requests_cache.CachedSession):
    pass


def http_cache_path() -> str | None:
    """The file of the HTTP cache of the reads, from ``PORTING_HTTP_CACHE``,
    or ``None`` when reads are not cached."""
    return os.environ.get(HTTP_CACHE_ENV) or None


def _storable(response) -> bool:
    """Only a response that came straight from its request is stored: one that
    followed a redirect holds the request headers of every hop in its history,
    where a credential is not redacted."""
    return not response.history and not response.is_redirect


def _usable_row(response, now: datetime) -> bool:
    """A stored response is trusted only when it holds a validator and a
    recorded expiry that is already past: such a row is revalidated, never
    served as it is."""
    return bool(response.headers.get("ETag") or response.headers.get("Last-Modified")) \
        and response.expires is not None and response.expires <= now


def _prune(session: requests.Session) -> None:
    """Drop what is too old, what cannot be read or revalidated, and the
    redirect aliases, so that nothing restored from a file is ever served
    without the server being asked."""
    cache = session.cache
    now = datetime.now(timezone.utc)
    for key in list(cache.responses.keys()):
        try:
            response = cache.responses[key]
            keep = _usable_row(response, now) and now - response.created_at <= HTTP_CACHE_MAX_AGE
        except Exception:  # an entry of any other shape is dropped
            keep = False
        if not keep:
            del cache.responses[key]
    cache.redirects.clear()
    cache.responses.vacuum()


def _cached_session(path: str) -> requests.Session:
    """A session whose GETs are cached in the SQLite file ``path``.

    Every request is revalidated: nothing is served without asking the
    server. ``expire_after=0`` makes every stored response stale, so a
    request is sent with ``If-None-Match``/``If-Modified-Since`` taken from
    the stored ``ETag``/``Last-Modified`` and a 304 reuses the stored body;
    a response without a validator is not stored and is simply fetched again.
    ``always_revalidate`` states the same for the entries that are stored,
    ``cache_control`` is off so that no ``max-age`` of a response ever keeps
    one fresh, and ``stale_if_error`` is off so that a failed request is never
    answered from the cache. Only successful GETs that followed no redirect
    are stored; credentials are ignored in keys and redacted from what is
    stored; entries are JSON, never pickles. The file comes from the previous
    controller run, so it is checked when it is opened (``_prune``)."""
    file = Path(path)

    def open_session() -> requests.Session:
        return _CachedSession(
            str(file), backend="sqlite", serializer="json", use_cache_dir=False,
            expire_after=0, always_revalidate=True, cache_control=False, stale_if_error=False,
            allowable_methods=("GET",), allowable_codes=(200,), filter_fn=_storable,
            ignored_parameters=_SECRET_PARAMETERS)
    for attempt in (0, 1):
        session = None
        try:
            if file.exists() and file.stat().st_size > HTTP_CACHE_MAX_BYTES:
                file.unlink()
            session = open_session()
            session.auth = lambda request: request
            _prune(session)
            return session
        except Exception:  # whatever is wrong with the file: it is dropped, once
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
            if attempt:
                raise
            for name in (file, Path(f"{file}-journal")):
                name.unlink(missing_ok=True)
    raise AssertionError("unreachable")


_CACHED_SESSIONS: dict[str, requests.Session] = {}
_CACHE_DISABLED = False


def _disable_cache() -> None:
    """Stop caching for the rest of the process after the cache failed."""
    global _CACHE_DISABLED
    _CACHE_DISABLED = True
    for session in _CACHED_SESSIONS.values():
        try:
            session.close()
        except Exception:
            pass
    _CACHED_SESSIONS.clear()


def _read_session(cache: bool | None = None) -> requests.Session:
    """The session of a read: cached when ``PORTING_HTTP_CACHE`` is set,
    unless ``cache`` is false (the clients that write never cache) or the
    cache failed earlier in this process."""
    path = http_cache_path() if cache is not False and not _CACHE_DISABLED else None
    if not path:
        return _Session()
    if path not in _CACHED_SESSIONS:
        try:
            _CACHED_SESSIONS[path] = _cached_session(path)
        except Exception:  # the cache never fails a run
            _disable_cache()
            return _Session()
    return _CACHED_SESSIONS[path]


def _guarded(session, call):
    """``call(session)``, with the read session when ``session`` is None. A
    failure of the cache itself (``sqlite3.Error``: locked, full, read-only)
    disables it for the rest of the process and repeats the read uncached."""
    used = session or _read_session()
    try:
        return call(used)
    except sqlite3.Error:
        if session is not None or not isinstance(used, _CachedSession):
            raise
        _disable_cache()
        return call(_Session())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The transport of the write clients follows no redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Api:
    """The GitHub REST API. Reads (``get``, ``paginate``, ``get_bytes``) go
    through ``requests``; ``session`` replaces its transport (tests). The
    write clients that subclass it send their writes with ``opener`` (a
    urllib transport that follows no redirect) and read without the HTTP
    cache (``cache=False``)."""

    def __init__(self, token: str | None = None, base: str = API, opener=None, *,
                 session=None, cache: bool | None = None, follow_redirects: bool = True):
        self.token = token if token is not None else (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
        self.base = base
        self._open = opener or urllib.request.build_opener(_NoRedirect).open
        # A response of a cached read is a stored one; downloads are never cached.
        self._session = session or _read_session(cache)
        self._downloads = session or _Session()
        self._follow = follow_redirects

    def _redirected(self, url: str, status: int) -> Exception:
        """What a redirect answer is when the client follows none."""
        return CollectError(f"GET {url}: HTTP {status}", status=status)

    def _headers(self, accept: str, *extra: tuple[str, str]) -> dict:
        return {"Accept": accept, "User-Agent": "riseproject-porting-agent", **dict(extra),
                **({"Authorization": f"Bearer {self.token}"} if self.token else {})}

    def _request(self, url: str) -> tuple[Any, dict]:
        headers = self._headers("application/vnd.github+json",
                                ("X-GitHub-Api-Version", "2022-11-28"))
        # Every failure to read the response, including a body that is cut
        # off or is not JSON, is a ``CollectError``: the collector could not
        # read GitHub. Only the transport and the decoding of the body are
        # converted; what the caller does with a well-formed response is not.
        def read(session):
            with session.get(url, headers=headers, timeout=30, allow_redirects=self._follow) as r:
                if 300 <= r.status_code < 400 and not self._follow:
                    raise self._redirected(url, r.status_code)
                if not 200 <= r.status_code < 300:
                    raise CollectError(f"GET {url}: HTTP {r.status_code}", status=r.status_code)
                return r.content, dict(r.headers)
        try:
            try:
                body, response_headers = read(self._session)
            except sqlite3.Error:
                if not isinstance(self._session, _CachedSession):
                    raise
                _disable_cache()
                self._session = _Session()
                body, response_headers = read(self._session)
        except CollectError:
            raise
        except _TRANSPORT_ERRORS as e:
            raise _transport_error(url, e) from e
        if not body:
            return None, response_headers
        try:
            return json.loads(body), response_headers
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise CollectError(f"GET {url}: response body is not JSON: {type(e).__name__}") from None

    def get(self, path: str) -> Any:
        if not _PATH_RE.match(path) or ".." in path:
            raise CollectError(f"refusing unexpected API path {path!r}")
        return self._request(self.base + path)[0]

    def get_bytes(self, path: str, *, max_bytes: int = 8 * 1024 * 1024) -> bytes:
        """GET a binary resource (e.g. an artifact zip), never cached. The API
        redirects to blob storage; the redirect is followed only to an
        allowed origin and drops Authorization cross-origin."""
        if not _PATH_RE.match(path) or ".." in path:
            raise CollectError(f"refusing unexpected API path {path!r}")
        headers = self._headers("application/vnd.github+json")
        try:
            with self._downloads.get(self.base + path, headers=headers, timeout=60,
                                     stream=True) as r:
                if not 200 <= r.status_code < 300:
                    raise CollectError(f"GET {path}: HTTP {r.status_code}", status=r.status_code)
                data = b""
                for chunk in r.iter_content(64 * 1024):
                    data += chunk
                    if len(data) > max_bytes:
                        break
        except CollectError:
            raise
        except _TRANSPORT_ERRORS as e:
            raise _transport_error(path, e) from e
        if len(data) > max_bytes:
            raise CollectError("download exceeds size limit")
        return data

    def paginate(self, path: str, *, key: str | None = None, max_pages: int = 10,
                 newest: bool = False, report: dict | None = None) -> list:
        """Follow Link: rel=next up to ``max_pages``. ``key`` selects the list
        inside an object response (e.g. ``check_runs``). With ``newest``, a
        list longer than ``max_pages`` is read from its last page backwards
        (Link: rel=last, then rel=prev), so the newest items are the ones
        read; a list that names no last page is read forwards, keeping its
        ``max_pages`` newest pages, up to ``NEWEST_SCAN_PAGES`` pages, and a
        longer one raises ``CollectError`` rather than return its oldest
        pages. The result is in the order of the list either way. When
        ``report`` is a dict, ``report["truncated"]`` is set to whether the
        list holds pages that were not read (a rel=next left after the last
        page read, a rel=last past ``max_pages``, or pages dropped from the
        front), whatever the number of items on its last page. A page with an
        empty body raises ``CollectError``: the list could not be read."""
        if report is not None:
            report["truncated"] = False
        if not _PATH_RE.match(path) or ".." in path:
            raise CollectError(f"refusing unexpected API path {path!r}")
        url = self.base + path
        pages: list[list] = []
        limit = max_pages
        for index in range(max(max_pages, NEWEST_SCAN_PAGES)):
            data, headers = self._request(url)
            link = headers.get("Link") or headers.get("link") or ""
            nxt = _link(link, "next")
            if newest and index == 0:
                last = _link(link, "last")
                number = re.search(r"[?&]page=([0-9]+)", last or "")
                if last and number and int(number.group(1)) > max_pages:
                    if report is not None:
                        report["truncated"] = True
                    return self._backwards(last, key=key, max_pages=max_pages)
                if nxt and not last:
                    # No last page is named: the newest pages are found by
                    # reading forwards, within a bound.
                    limit = NEWEST_SCAN_PAGES
            pages.append(list(_page(url, data, key)))
            if len(pages) > max_pages:
                pages.pop(0)
                if report is not None:
                    report["truncated"] = True
            if not nxt:
                break
            if index == limit - 1:
                if limit != max_pages:
                    raise CollectError(f"GET {url}: the list names no last page and is longer "
                                       f"than {NEWEST_SCAN_PAGES} pages")
                if report is not None:
                    report["truncated"] = True
                break
            if not nxt.startswith(self.base + "/"):
                raise CollectError("pagination left the API host")
            url = nxt
        return [item for page in pages for item in page]

    def _backwards(self, url: str, *, key: str | None, max_pages: int) -> list:
        """``max_pages`` pages read from ``url`` (the last page) backwards,
        in the order of the list."""
        pages: list[list] = []
        for _ in range(max_pages):
            if not url.startswith(self.base + "/"):
                raise CollectError("pagination left the API host")
            data, headers = self._request(url)
            pages.append(list(_page(url, data, key)))
            url = _link(headers.get("Link") or headers.get("link") or "", "prev")
            if not url:
                break
        return [item for page in reversed(pages) for item in page]


# How many pages a list read for its newest items is read forwards at most
# when GitHub names no last page (``Api.paginate``).
NEWEST_SCAN_PAGES = 50


def _page(url: str, data: Any, key: str | None) -> list:
    """The items of one page of a list read. A page GitHub answered with an
    empty body is a failed read (the collector could not read the list),
    never an empty list or a crash; a well-formed body of another shape stays
    an error of the caller, as for every other read."""
    if data is None:
        raise CollectError(f"GET {url}: the response body is empty, not a list")
    return data[key] if key else data


def _link(link: str, rel: str) -> str | None:
    for part in link.split(","):
        m = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', part)
        if m and rel in m.group(2).split():
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
                                check_pull: bool = False, operation_open: bool = False) -> dict | None:
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
    mismatch = development_pull_problem(api, record, operation_open=operation_open) if check_pull else None
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


def _renamed_base_in_pr(api: Api, c: dict | None, repo: dict, ref: str, renamed_to: str) -> dict | None:
    """The verified base of the open upstream PR of ``c`` after the upstream
    renamed its base branch ``ref`` to ``renamed_to``: ``None`` when ``c``
    is not in upstream review on that branch, or the PR it names is not open,
    does not target ``renamed_to`` of the same repository, or the new name's
    history does not contain the recorded base commit. A read that fails
    raises ``CollectError``: whether the PR followed the rename is unknown."""
    if c is None or c["phase"] != "upstream_review" or c["upstream"].get("base_ref") != ref \
            or not (c.get("upstream_pr") or {}).get("number") or not c["upstream"].get("base_sha"):
        return None
    pr = upstream_pull(api, c)
    if pr["state"] != "open" or pr["merged"] or pr["base_ref"] != renamed_to \
            or pr["base_repository_id"] != repo["id"]:
        return None
    if not _branch_contains(api, c["upstream"]["full_name"], renamed_to, c["upstream"]["base_sha"]):
        return None
    return {"pr_id": pr["id"], "pr_number": pr["number"], "base_ref": renamed_to,
            "base_sha": c["upstream"]["base_sha"]}


def _branch_contains(api: Api, full_name: str, ref: str, sha: str) -> bool:
    """Whether the history of the branch ``ref`` contains the commit
    ``sha``. A branch that does not exist, or a read that fails, raises
    ``CollectError``: it was just named by GitHub, so its absence is not an
    answer."""
    head = branch_head(api, full_name, ref)
    if head is None:
        raise CollectError(f"GET branch {ref!r}: HTTP 404 after the rename")
    return head["head"] == sha or compare_commits(api, full_name, sha, head["head"]).get(
        "status") == "ahead"


def base_redirect(api: Api, record: dict) -> str | None:
    """The name GitHub now redirects the recorded base branch of ``record``
    to: a read of ``upstream.base_ref`` answers with another branch, the
    one it was renamed to. ``None`` when the recorded base is read as it is
    or is missing; a read that fails raises ``CollectError``. Whether the
    new name still contains the recorded base commit is
    ``base_contained``."""
    upstream = record["upstream"]
    if not upstream.get("base_ref") or not upstream.get("base_sha"):
        return None
    try:
        branch_head(api, upstream["full_name"], upstream["base_ref"])
    except BranchRenamed as e:
        return e.name if e.name != upstream["base_ref"] else None
    return None


def base_contained(api: Api, record: dict, ref: str) -> bool:
    """Whether the history of the branch ``ref`` contains the recorded base
    commit of ``record`` (the same check as ``_renamed_base_in_pr``)."""
    upstream = record["upstream"]
    return _branch_contains(api, upstream["full_name"], ref, upstream["base_sha"])


# The bound of every list read for steering: pages of 100, the newest.
STEERING_PAGES = 5
# The ``omitted`` key suffix of a steering list read in full: the list may
# hold older items than were read (an earlier collection read them while the
# list was shorter, unless it grew past the bound between two collections).
STEERING_CAP_SUFFIX = "_list_cap_reached"
# The HTTP statuses of a steering list that no retry will read: the
# repository or pull request is gone, private to the App, or unavailable for
# legal reasons. Such a list is recorded as unreadable instead of holding the
# contribution on every tick.
STEERING_GONE = (404, 410, 451)
# Review states kept with an empty body: the verdict itself is the feedback.
STEERING_REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
# Accounts of the upstream repository whose reviews on the upstream PR are
# recorded as maintainer feedback (never as steering).
MAINTAINER_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")


# The steering sources on which accounts outside the command authority can
# edit the text of an authority account: the upstream PR, whose maintainers
# have write access to the upstream repository.
UNVERIFIED_EDIT_PREFIX = "upstream_pr_"


def edits_unverified(source: str) -> bool:
    """Whether an edit of the text of an authority account on ``source``
    may be by an account outside the command authority, so that the edited
    version never steers."""
    return str(source).startswith(UNVERIFIED_EDIT_PREFIX)


def _steering_item(source: str, item: dict, *, authority: bool, body_key: str = "body",
                   created_key: str = "created_at", updated_key: str = "updated_at") -> dict:
    user = item.get("user") or {}
    return {"source": source, "id": item.get("id"), "author": user.get("login"),
            "author_id": user.get("id"), "authority": authority,
            "author_association": item.get("author_association"),
            "created_at": item.get(created_key), "updated_at": item.get(updated_key),
            "url": item.get("html_url"), "commit_id": item.get("commit_id"),
            "path": item.get("path"), "line": item.get("line") or item.get("original_line"),
            "in_reply_to": item.get("in_reply_to_id"), "review_state": item.get("state"),
            "body": item.get(body_key) or ""}


def steering(api: Api, record: dict, authority: Iterable[int], *, issue: dict | None,
             degrade: bool = False) -> dict:
    """The human text a worker is steered by, read for one contribution.

    Read, each list bounded to its newest ``STEERING_PAGES`` pages of 100
    (review comments sorted newest first, every other list read from its
    last page backwards): the current
    tracking issue description (``issue``, the collected issue; only when
    it differs from the description frozen at intake and the issue author is
    in the command authority), the tracking issue comments, and the reviews,
    review comments and conversation comments of the development PR and of
    the upstream PR when the record names them.

    Accounts whose immutable id is in ``authority`` are kept with
    ``authority`` true, on every source. On the upstream PR only, accounts
    whose association is OWNER, MEMBER or COLLABORATOR are kept with
    ``authority`` false: maintainer review feedback, never steering.
    On the upstream PR (``edits_unverified``), a comment of an authority
    account that was edited after it was written (``updated_at`` differs
    from ``created_at``) is kept with ``authority`` false and
    ``edit_unverified`` true: the upstream maintainers, who are not in the
    command authority, can edit it, and GitHub does not say who edited it
    last, so it is shown as data and never steers. On the control
    repository and the fork, whose write access is part of the command
    authority's trust (as for the edited tracking issue description), an
    edited comment steers like any other.
    A review with an empty body is kept when its state is a verdict
    (``STEERING_REVIEW_STATES``); its version is its submission time and the
    digest of its state and body, since GitHub gives an edited review no new
    time. Everything else (other accounts, bots, empty bodies) is counted
    per source in ``omitted``; a list that holds more pages than were read
    (whatever the number of items on its last page) is flagged there with
    ``STEERING_CAP_SUFFIX``, since its older items are unread.
    Returns ``{"items": [...], "omitted": {...}, "unreadable": [...],
    "failed": {...}}``; the text is untrusted and is bounded where it is
    stored. A list GitHub answers with a status of ``STEERING_GONE`` is
    named in ``unreadable``; any other failed read raises ``CollectError``,
    unless ``degrade`` is true (the task has waited for its history too
    long): then the list is named in ``unreadable`` too, and in ``failed``
    the HTTP status GitHub answered, or, for a read GitHub never answered,
    what failed (``no HTTP answer: ...``)."""
    from common import sha256_text

    allowed = {int(value) for value in authority}
    items: list[dict] = []
    omitted: dict[str, int] = {}
    unreadable: list[str] = []
    failed: dict[str, int | str] = {}

    def keep(source: str, raw: dict, *, maintainers: bool, review: bool = False,
             **keys) -> None:
        user = raw.get("user") or {}
        body = raw.get(keys.get("body_key", "body")) or ""
        trusted = user.get("id") in allowed
        maintainer = maintainers and raw.get("author_association") in MAINTAINER_ASSOCIATIONS \
            and user.get("type") != "Bot"
        verdict = review and raw.get("state") in STEERING_REVIEW_STATES
        if not isinstance(body, str) or not (body.strip() or verdict) \
                or not (trusted or maintainer):
            omitted[source] = omitted.get(source, 0) + 1
            return
        edited = trusted and not review and edits_unverified(source) \
            and raw.get("updated_at") and raw.get("updated_at") != raw.get("created_at")
        item = _steering_item(source, raw, authority=trusted and not edited, **keys)
        if edited:
            item["edit_unverified"] = True
        if review:
            digest = sha256_text(f"{raw.get('state')}\n{body}")[:16]
            item["version"] = f"{raw.get('submitted_at')}#{digest}"
        items.append(item)

    def read(source: str, path: str, *, newest: bool = True) -> list:
        # The newest pages of each list are read; one that fills every page
        # may hold older items than were read, which is recorded, never
        # silent.
        pagination: dict = {}
        try:
            raws = api.paginate(path, max_pages=STEERING_PAGES, newest=newest,
                                report=pagination)
        except CollectError as error:
            status = getattr(error, "status", None)
            if status not in STEERING_GONE and not degrade:
                raise
            unreadable.append(source)
            if status not in STEERING_GONE:
                # What failed: the HTTP status GitHub answered, or the
                # failure of a read GitHub never answered (a transport
                # error, a body that is not the list).
                failed[source] = status if isinstance(status, int) \
                    else f"no HTTP answer: {str(error)[:200]}"
            return []
        # A client that does not report truncation is judged by the number
        # of items read.
        if pagination.get("truncated", len(raws) >= STEERING_PAGES * 100):
            omitted[source + STEERING_CAP_SUFFIX] = 1
        return raws

    tracking = record["tracking_issue"]
    if isinstance(issue, dict) and issue.get("id") == tracking.get("id"):
        body = issue.get("body") or ""
        if isinstance(body, str) and sha256_text(body) != tracking.get("description_digest"):
            if (issue.get("user") or {}).get("id") in allowed and body.strip():
                item = _steering_item("tracking_issue_description", issue, authority=True)
                item["version"] = sha256_text(body)
                items.append(item)
            else:
                omitted["tracking_issue_description"] = 1
    for raw in read("tracking_issue_comment",
                    f"/repos/{CONTROL_REPO}/issues/{int(tracking['number'])}/comments"
                    "?per_page=100"):
        keep("tracking_issue_comment", raw, maintainers=False)
    pulls = []
    fork = record.get("fork") or {}
    if fork.get("name") and fork.get("pr_number"):
        pulls.append(("development_pr", fork["name"], int(fork["pr_number"]), False))
    upstream_pr = record.get("upstream_pr") or {}
    if upstream_pr.get("number"):
        pulls.append(("upstream_pr", record["upstream"]["full_name"], int(upstream_pr["number"]),
                      True))
    for prefix, repository, number, maintainers in pulls:
        for raw in read(f"{prefix}_review",
                        f"/repos/{repository}/pulls/{number}/reviews?per_page=100"):
            keep(f"{prefix}_review", raw, maintainers=maintainers, review=True,
                 created_key="submitted_at", updated_key="submitted_at")
        for raw in read(f"{prefix}_review_comment",
                        f"/repos/{repository}/pulls/{number}/comments?per_page=100"
                        "&sort=created&direction=desc", newest=False):
            keep(f"{prefix}_review_comment", raw, maintainers=maintainers)
        for raw in read(f"{prefix}_comment",
                        f"/repos/{repository}/issues/{number}/comments?per_page=100"):
            keep(f"{prefix}_comment", raw, maintainers=maintainers)
    return {"items": items, "omitted": omitted, "unreadable": unreadable, "failed": failed}


def upstream_snapshots(api: Api, contributions: Iterable[dict], issues: list[dict], policy: dict,
                       comments: Iterable[dict] = (),
                       consumed: Iterable[int] = ()) -> dict[str, dict]:
    """Verify source IDs and initial immutable bases before task admission.
    Errors are data: urgent commands must still run when an upstream is absent.

    A snapshot that could not be read is ``{"error": ..., "ref": ...,
    "scope": ...}``: ``ref`` is the branch asked for (``None`` for the
    default branch), so the controller never applies the answer for one
    branch to a record of another, and ``scope`` is ``repository`` when the
    repository itself could not be read (also when GitHub now names it
    otherwise: renamed or transferred, with ``renamed_repository`` and the
    id GitHub answered for, ``renamed_repository_id``) and
    ``branch`` otherwise. When the upstream answered definitively that the
    base branch is not there (it does not exist, or GitHub redirects it to
    the branch it was renamed to), the snapshot also carries ``base_ref``,
    ``missing: True``, the ``repository_id`` read, and for a rename
    ``renamed_to``; the controller then takes ``require_base_branch`` at
    once. For a rename of the base branch of
    a contribution in upstream review, the upstream PR is read too:
    ``pr_base`` is its verified base when it follows the rename
    (``_renamed_base_in_pr``), and a failed read of it makes the snapshot an
    ordinary failed read, never a definitive answer.

    A contribution waiting for a human because its upstream is unavailable
    (``pending_human_action`` ``base_branch_unavailable`` or
    ``upstream_unavailable``) is not read, unless its tracking issue carries
    a resume in ``comments``, failed reads caused the wait
    (``pending_human_action.counter`` ``source_reads``: the controller ends
    it once the upstream is read again), or it waits in upstream review for a
    base branch renamed while the upstream PR is open (the controller ends
    that wait once the PR follows the rename). A parked contribution is read only
    when its tracking issue carries a refresh: a restart clears the identity
    and base, and the controller binds them again in the tick that restarts
    it. A command is counted only when ``commands.parse`` accepts it, it is
    by an actor of ``command_authority``, and its comment id is not in
    ``consumed`` (the command ledger); whether its row then applies is
    decided by the controller. A parked upstream is read at the ``branch``
    of the refresh, else at the target branch the request asked for at
    intake, else at its default branch. An intake request for an upstream
    that already has a contribution that is not terminal reads nothing: the
    controller refuses the duplicate without it."""
    contributions = list(contributions)
    posted = _commands_by_issue(comments, policy, consumed)
    owned = {c["upstream"]["full_name"] for c in contributions
             if c["phase"] not in lifecycle.TERMINAL}
    refs: dict[str, str | None] = {}
    active: dict[str, dict] = {}
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
        pending = c.get("pending_human_action") or {}
        renamed_wait = pending.get("kind") == lifecycle.BASE_BRANCH_UNAVAILABLE \
            and c["phase"] == "upstream_review" and bool(c.get("upstream_pr"))
        # A wait that failed reads caused is read on every tick, so that it
        # ends once the upstream can be read again (`resume_upstream_read`),
        # and so is the wait a renamed repository caused (no counter), which
        # ends once GitHub answers for it under its recorded name again
        # (`resume_upstream_read`).
        reads_wait = pending.get("counter") == "source_reads"
        moved_wait = pending.get("kind") == lifecycle.UPSTREAM_UNAVAILABLE \
            and pending.get("counter") is None
        if pending.get("kind") in (lifecycle.BASE_BRANCH_UNAVAILABLE,
                                   lifecycle.UPSTREAM_UNAVAILABLE) \
                and "resume" not in verbs and not renamed_wait and not reads_wait \
                and not moved_wait:
            continue
        refs[name] = c["upstream"].get("base_ref")
        active[name] = c
    known = {c["tracking_issue"]["id"] for c in contributions}
    for issue in issues:
        if issue["id"] in known:
            continue
        try:
            req = intake.parse_request(issue, policy)
        except intake.IntakeError:
            continue
        if req.upstream not in owned:
            refs.setdefault(req.upstream, req.target_branch)
    snapshots = {}
    for name, ref in refs.items():
        scope = "repository"
        try:
            repo = repository(api, name)
            if repo["full_name"].lower() != name.lower():
                snapshots[name] = {"error": f"GitHub names the upstream repository "
                                            f"{repo['full_name'][:200]!r} now",
                                   "ref": ref, "scope": "repository",
                                   "renamed_repository": repo["full_name"][:200],
                                   "renamed_repository_id": repo["id"]}
                continue
            scope = "branch"
            base_ref = ref or repo["default_branch"]
            try:
                base = branch_head(api, name, base_ref)
            except BranchRenamed as e:
                snapshot = {"error": f"upstream base branch {base_ref!r} was renamed",
                            "ref": ref, "base_ref": base_ref, "missing": True,
                            "renamed_to": e.name[:255], "repository_id": repo["id"]}
                try:
                    pr_base = _renamed_base_in_pr(api, active.get(name), repo, base_ref,
                                                  e.name[:255])
                except CollectError as error:
                    snapshots[name] = {"error": f"upstream base branch {base_ref!r} was renamed, "
                                                f"and its upstream PR could not be read: {error}",
                                       "ref": ref, "scope": "branch"}
                    continue
                if pr_base:
                    snapshot["pr_base"] = pr_base
                snapshots[name] = snapshot
                continue
            if base is None:
                snapshots[name] = {"error": f"upstream base branch {base_ref!r} does not exist",
                                   "ref": ref, "base_ref": base_ref, "missing": True,
                                   "repository_id": repo["id"]}
                continue
            snapshots[name] = {"repository_id": repo["id"], "base_ref": base_ref, "base_sha": base["head"]}
        except CollectError as e:
            snapshots[name] = {"error": str(e), "ref": ref, "scope": scope}
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


# The artifact a worker run's publish job uploads.
PUBLICATION_ARTIFACT = "porting-publication"


def worker_run_result(api: Api, run_id: str) -> dict:
    """Whether the worker run ``run_id`` of the control repository ended,
    and with a publication: ``{"run_id", "status", "conclusion", "ended_at",
    "publication"}``. ``ended_at`` is the run's ``updated_at``.
    ``publication`` is whether an unexpired ``porting-publication`` artifact
    exists on an ended run, ``None`` while the run has not ended. A run
    GitHub no longer has (HTTP 404) is ``{"status": "gone", "publication":
    False}``; any other failed read raises ``CollectError``."""
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
        raise CollectError(f"unexpected worker run id {str(run_id)[:40]!r}")
    try:
        run = api.get(f"/repos/{CONTROL_REPO}/actions/runs/{run_id}")
    except CollectError as error:
        if error.status == 404:
            return {"run_id": str(run_id), "status": "gone", "conclusion": None,
                    "ended_at": None, "publication": False}
        raise
    if not isinstance(run, dict):
        raise CollectError(f"GET worker run {run_id}: the response is not a run")
    status = run.get("status")
    reading = {"run_id": str(run_id), "status": status if isinstance(status, str) else None,
               "created_at": run.get("created_at") if isinstance(run.get("created_at"), str)
               else None,
               "conclusion": run.get("conclusion") if isinstance(run.get("conclusion"), str)
               else None,
               "ended_at": run.get("updated_at") if isinstance(run.get("updated_at"), str)
               else None,
               "publication": None}
    if reading["status"] == "completed":
        artifacts = api.paginate(f"/repos/{CONTROL_REPO}/actions/runs/{run_id}/artifacts?per_page=100",
                                 key="artifacts", max_pages=2)
        reading["publication"] = any(
            isinstance(a, dict) and a.get("name") == PUBLICATION_ARTIFACT and not a.get("expired")
            for a in artifacts)
    return reading


def agent_run_failure(api: Api, run_id: str) -> dict | None:
    """Whether the agent workflow run ``run_id`` failed before its agent ran
    for a step of the pipeline (``pipeline_failure.classify``): ``{"pipeline":
    True, "job", "step", "head_sha"}``, else ``None``. A run that has not
    failed, or that GitHub no longer has (HTTP 404), is ``None``; any other
    failed read raises ``CollectError``."""
    import pipeline_failure
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
        raise CollectError(f"unexpected run id {str(run_id)[:40]!r}")
    try:
        run = api.get(f"/repos/{CONTROL_REPO}/actions/runs/{run_id}")
    except CollectError as error:
        if error.status == 404:
            return None
        raise
    if not isinstance(run, dict):
        raise CollectError(f"GET run {run_id}: the response is not a run")
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        return None
    attempt = run.get("run_attempt")
    attempt = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) \
        and attempt >= 1 else 1
    jobs = api.paginate(f"/repos/{CONTROL_REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs"
                        "?per_page=100", key="jobs", max_pages=2)
    return pipeline_failure.classify(run, jobs)


_TREE_VERSIONS: dict[str, str | None] = {}


def pipeline_version_at(api: Api, sha: str) -> str | None:
    """The version of the pipeline code at the commit ``sha`` of the control
    repository, as ``reconcile.pipeline_version`` reads it from a checkout
    (``pipeline_failure.tree_version`` of the top-level tree without
    `.state`), cached per commit. ``None`` for a commit without a tree."""
    import pipeline_failure
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CollectError(f"unexpected commit {str(sha)[:60]!r}")
    if sha not in _TREE_VERSIONS:
        tree = api.get(f"/repos/{CONTROL_REPO}/git/trees/{sha}")
        entries = tree.get("tree") if isinstance(tree, dict) else None
        if not isinstance(entries, list) or tree.get("truncated"):
            raise CollectError(f"GET tree {sha}: the response is not a complete tree")
        _TREE_VERSIONS[sha] = pipeline_failure.tree_version(
            f"{e['mode']} {e['type']} {e['sha']}\t{e['path']}" for e in entries)
    return _TREE_VERSIONS[sha]


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


# Bounds of a series whose changed paths are read for the series requirements
# of the plan: the compare API lists at most 300 files, and each non-debug
# commit of a series with debug commits is read on its own.
SERIES_MAX_COMMITS = 50
SERIES_MAX_FILES = 300


def _filenames(files: Any) -> set[str] | None:
    if not isinstance(files, list):
        return None
    names = {f.get("filename") for f in files if isinstance(f, dict)}
    if not all(isinstance(n, str) and n for n in names):
        return None
    return names


def series_changed_paths(api: Api, fork_full_name: str, base_sha: str, head: str
                         ) -> set[str] | None:
    """The paths the non-debug commits of the series ``base_sha..head`` of
    the fork change, as a set; ``None`` when the series cannot be read in
    full (more than ``SERIES_MAX_COMMITS`` commits, ``SERIES_MAX_FILES``
    files or more, ``head`` behind ``base_sha``, or a response of another
    shape). A path a later commit reverts is not changed by the series, and
    a path changed only by ``{debug}`` commits does not count: they are
    dropped at submission. A failed read raises ``CollectError``."""
    import commit_rules

    cmp = compare_commits(api, fork_full_name, base_sha, head)
    commits = cmp.get("commits")
    ahead, behind = cmp.get("ahead_by"), cmp.get("behind_by")
    names = _filenames(cmp.get("files"))
    if not isinstance(commits, list) or names is None or behind != 0 \
            or not isinstance(ahead, int) or isinstance(ahead, bool) \
            or ahead != len(commits) or ahead > SERIES_MAX_COMMITS \
            or len(names) >= SERIES_MAX_FILES:
        return None
    messages = []
    for commit in commits:
        sha = commit.get("sha") if isinstance(commit, dict) else None
        message = ((commit.get("commit") or {}).get("message")
                   if isinstance(commit, dict) else None)
        if not isinstance(sha, str) or not _FULL_SHA.match(sha) or not isinstance(message, str):
            return None
        messages.append((sha, message))
    if not any(commit_rules.is_debug(m) for _sha, m in messages):
        return names
    changed: set[str] = set()
    for sha, message in messages:
        if commit_rules.is_debug(message):
            continue
        data = api.get(f"/repos/{fork_full_name}/commits/{sha}")
        own = _filenames((data or {}).get("files") if isinstance(data, dict) else None)
        if own is None or (data or {}).get("sha") != sha:
            raise CollectError(f"commit {sha[:12]} of the series is malformed")
        if len(own) >= SERIES_MAX_FILES:
            return None
        changed |= own
    return changed & names


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


def development_pull_problem(api: Api, record: dict, *, operation_open: bool = False) -> str | None:
    """Why the development PR on GitHub does not match the fork, the ref,
    and the PR the record names, or ``None`` when it matches. A PR that
    GitHub does not have does not match, nor does a closed or merged one,
    unless an operation of the contribution is open: the trusted CI
    bootstrap closes and reopens the PR."""
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
    if pr.get("state") == "closed" and not operation_open:
        return f"development PR {number} is closed"
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
            "runner_id": j.get("runner_id"), "head_sha": j.get("head_sha")}


# The failed jobs whose annotations one snapshot reads to classify them.
MAX_CLASSIFIED_JOBS = 20


def ci_snapshot(api: Api, full_name: str, head_sha: str, *, max_runs: int = 30) -> CiSnapshot:
    """Everything CI says about one exact head SHA: workflow runs (all
    attempts resolve to the latest by run_attempt), their jobs for that
    attempt, check runs and commit statuses, and artifacts. Jobs that lost
    their runner are classified separately (``classify_runner_loss``)."""
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


def classify_runner_loss(api: Api, full_name: str, snap: CiSnapshot) -> None:
    """Mark each job of the latest attempts of ``snap`` with ``runner_lost``:
    true for a completed job that failed with a failure-level annotation
    matching ``ci_rerun.RUNNER_LOST`` (the annotations of at most
    ``MAX_CLASSIFIED_JOBS`` failed jobs are read). The annotation text is
    only matched against those constants, never stored or rendered. A read
    error propagates like any other snapshot read."""
    import ci_rerun

    failed = [j for jobs in snap.jobs_by_run.values() for j in jobs
              if j.get("status") == "completed" and j.get("conclusion") == "failure"]
    read = {j["id"] for j in sorted(failed, key=lambda j: j["id"])[:MAX_CLASSIFIED_JOBS]}
    for jobs in snap.jobs_by_run.values():
        for job in jobs:
            lost = False
            if job["id"] in read:
                annotations = api.paginate(
                    f"/repos/{full_name}/check-runs/{int(job['id'])}/annotations?per_page=100",
                    max_pages=1)
                lost = any(ci_rerun.runner_lost(a.get("message"))
                           for a in annotations if isinstance(a, dict)
                           and a.get("annotation_level") == "failure")
            job["runner_lost"] = lost


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


def pypi_release(distribution: str, session=None) -> dict:
    """The PyPI JSON document of ``distribution``. A failed read is a
    ``CollectError`` (with ``status`` for an HTTP error)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", distribution):
        raise CollectError("invalid distribution name")
    url = f"https://pypi.org/pypi/{distribution}/json"

    def read(used):
        with used.get(url, timeout=30) as r:
            if not 200 <= r.status_code < 300:
                raise CollectError(f"PyPI {distribution}: HTTP {r.status_code}", status=r.status_code)
            return json.loads(r.content)
    try:
        return _guarded(session, read)
    except CollectError:
        raise
    except (*_TRANSPORT_ERRORS, ValueError) as e:
        raise CollectError(f"PyPI {distribution}: {e}") from e


def pypi_project(distribution: str, session=None) -> dict | None:
    """The PyPI JSON document of ``distribution``, or ``None`` when PyPI has
    no such project. Any other failure is a collector failure."""
    try:
        return pypi_release(distribution, session=session)
    except CollectError as e:
        if e.status == 404:
            return None
        raise


def usage(url: str, session=None) -> tuple[Any, datetime]:
    """A live reading, never cached."""
    try:
        with (session or _Session()).get(url, timeout=30) as r:
            r.raise_for_status()
            return json.loads(r.content), datetime.now(timezone.utc)
    except requests.RequestException as e:
        raise CollectError(f"usage {url}: {e}") from e


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
