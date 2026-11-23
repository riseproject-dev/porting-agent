# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The measurable contribution rules a verified dossier records, and their
deterministic checks.

The investigator measures the project's commit conventions from its history
and records them as ``contribution_guidelines.commit_conventions.rules`` in
the dossier (schema version 2), and the required sections of the project's
pull request template as ``contribution_guidelines.pull_request_template``.
Trusted code applies them: the worker gate to every non-debug commit a
worker proposes, and the submission check to the reviewed series and the
reviewed pull request description.

Only rules a machine can measure are here. Qualitative expectations (the
imperative mood, the scope of a change, a reviewer's style asks) are checked
by the model review at submission.

Nothing here runs a pattern from the dossier: prefixes are compared as
literal strings, so a hostile dossier cannot make a check slow.
"""

from __future__ import annotations

import email
import email.policy
import re
import unicodedata
from typing import Any

from common import APP_COMMIT_EMAIL, APP_COMMIT_NAME, DEBUG_PREFIX

# The rules, their types, and their bounds.
RULE_KEYS = ("title_max_length", "title_prefixes", "title_prefix_required",
             "title_capitalization", "title_trailing_period", "body_required",
             "body_line_max_length", "signed_off_by_required", "max_commits")
CAPITALIZATION = ("upper", "lower", "any")
TRAILING_PERIOD = ("forbidden", "any")
MAX_PREFIXES = 20
MAX_SECTIONS = 30
_SIGNED_OFF = re.compile(r"^Signed-off-by: \S.* <[^<>\s]+@[^<>\s]+>$")
# An attribution trailer: any ``<token>-by:`` line (Signed-off-by,
# Co-authored-by, Reviewed-by, Acked-by, Tested-by, Reported-by,
# Suggested-by, and every other), matched on the line's lookalike-folded
# form (``_fold``): a key of any letters without white space that ends in
# ``-by``, then optional white space and a colon.
_ATTRIBUTION = re.compile(r"^\s*([^\s:]*-by)\s*:(.*)$")
# Characters folded to a hyphen or a colon before a line is matched, after
# NFKC (which already folds the fullwidth and small forms of most of them).
_HYPHENS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d_"
_COLONS = "\uff1a\ufe55\ufe13\u2236\ua789\u02d0\u02f8\u0589\u05c3\u1361\u1804\u205a\u2e2c"
_FOLD = str.maketrans({**{ch: "-" for ch in _HYPHENS}, **{ch: ":" for ch in _COLONS}})
APP_IDENTITY = f"{APP_COMMIT_NAME} <{APP_COMMIT_EMAIL}>"
_PATCH_PREFIX = re.compile(r"^\[PATCH[^\]]*\]\s*")
_MBOX_FROM = re.compile(rb"^From [0-9a-f]{40} Mon Sep 17 00:00:00 2001$", re.M)
# An ATX heading opener; its text (the rest of the line, without a closing
# sequence of ``#`` and trailing white space) is cut in Python, so no pattern
# backtracks over a long line.
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]|$)")


class RuleError(ValueError):
    """The rules or the template sections a dossier records are malformed."""


def _int(value: Any, low: int, high: int, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise RuleError(f"{name} must be null or an integer in [{low}, {high}]")
    return value


def _printable(value: Any, limit: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit \
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise RuleError(f"{name} must be a printable string of at most {limit} characters")
    return value


def normalize(rules: Any) -> dict:
    """The rules with their types and bounds checked, every key present."""
    if not isinstance(rules, dict):
        raise RuleError("the commit rules must be an object")
    unknown = sorted(set(rules) - set(RULE_KEYS))
    if unknown:
        raise RuleError(f"unknown commit rules {unknown}")
    prefixes = rules.get("title_prefixes") or []
    if not isinstance(prefixes, list) or len(prefixes) > MAX_PREFIXES:
        raise RuleError(f"title_prefixes must be a list of at most {MAX_PREFIXES} prefixes")
    prefixes = [_printable(p, 40, "a title prefix") for p in prefixes]
    out = {
        "title_max_length": _int(rules.get("title_max_length"), 20, 200, "title_max_length"),
        "title_prefixes": prefixes,
        "title_prefix_required": bool(rules.get("title_prefix_required", False)),
        "title_capitalization": rules.get("title_capitalization", "any"),
        "title_trailing_period": rules.get("title_trailing_period", "any"),
        "body_required": bool(rules.get("body_required", False)),
        "body_line_max_length": _int(rules.get("body_line_max_length"), 50, 200,
                                     "body_line_max_length"),
        "signed_off_by_required": bool(rules.get("signed_off_by_required", False)),
        "max_commits": _int(rules.get("max_commits"), 1, 50, "max_commits"),
    }
    if out["title_capitalization"] not in CAPITALIZATION:
        raise RuleError(f"title_capitalization must be one of {list(CAPITALIZATION)}")
    if out["title_trailing_period"] not in TRAILING_PERIOD:
        raise RuleError(f"title_trailing_period must be one of {list(TRAILING_PERIOD)}")
    for key in ("title_prefix_required", "body_required", "signed_off_by_required"):
        if not isinstance(rules.get(key, False), bool):
            raise RuleError(f"{key} must be a boolean")
    if out["title_prefix_required"] and not prefixes:
        raise RuleError("title_prefix_required needs at least one title prefix")
    return out


def normalize_sections(sections: Any) -> list[str]:
    """The required sections of a pull request template, checked."""
    if not isinstance(sections, list) or len(sections) > MAX_SECTIONS:
        raise RuleError(f"required_sections must be a list of at most {MAX_SECTIONS} headings")
    return [_printable(s, 200, "a required section").strip() for s in sections]


def guidelines(dossier: Any) -> dict | None:
    """The contribution guidelines of a dossier, or ``None`` for a dossier
    written before they were part of the contract (schema version 1)."""
    if not isinstance(dossier, dict) or dossier.get("schema_version") != 2:
        return None
    value = dossier.get("contribution_guidelines")
    return value if isinstance(value, dict) else None


def from_dossier(dossier: Any) -> dict | None:
    """The measurable commit rules a dossier records, or ``None`` when it
    records none (a dossier of schema version 1)."""
    found = guidelines(dossier)
    if found is None:
        return None
    return normalize((found.get("commit_conventions") or {}).get("rules"))


def sections_from_dossier(dossier: Any) -> list[str] | None:
    """Every required section of every pull request template the dossier
    records, in order and without repeats; ``None`` for a dossier of schema
    version 1."""
    found = guidelines(dossier)
    if found is None:
        return None
    template = found.get("pull_request_template") or {}
    out: list[str] = []
    for item in template.get("templates") or []:
        for section in normalize_sections(item.get("required_sections") or []):
            if section not in out:
                out.append(section)
    return out


def split_message(message: str) -> tuple[str, list[str]]:
    """The title and the body lines of a commit message."""
    lines = str(message or "").replace("\r\n", "\n").rstrip("\n").split("\n")
    title, body = lines[0], lines[1:]
    while body and not body[0].strip():
        body = body[1:]
    return title, body


def is_debug(message: str) -> bool:
    return split_message(message)[0].startswith(DEBUG_PREFIX)


def message_problems(message: str, rules: dict) -> list[str]:
    """Why one commit message breaks the rules; empty when it follows them."""
    title, body = split_message(message)
    shown = title if len(title) <= 80 else title[:77] + "..."
    problems = []
    limit = rules.get("title_max_length")
    if limit is not None and len(title) > limit:
        problems.append(f"the title {shown!r} has {len(title)} characters, more than {limit}")
    rest = title
    prefixes = rules.get("title_prefixes") or []
    matched = next((p for p in sorted(prefixes, key=len, reverse=True) if title.startswith(p)), None)
    if matched is not None:
        rest = title[len(matched):].lstrip()
    elif rules.get("title_prefix_required"):
        problems.append(f"the title {shown!r} starts with none of the prefixes {prefixes}")
    first = next((ch for ch in rest if ch.isalpha()), "")
    case = rules.get("title_capitalization", "any")
    if first and case == "upper" and not first.isupper():
        problems.append(f"the title {shown!r} does not start with a capital letter")
    if first and case == "lower" and not first.islower():
        problems.append(f"the title {shown!r} does not start with a lower-case letter")
    if rules.get("title_trailing_period") == "forbidden" and title.rstrip().endswith("."):
        problems.append(f"the title {shown!r} ends with a period")
    trailers = [line for line in body if _SIGNED_OFF.match(line.strip())]
    text = [line for line in body if line.strip() and line not in trailers]
    if rules.get("body_required") and not text:
        problems.append(f"the commit {shown!r} has no body")
    width = rules.get("body_line_max_length")
    if width is not None:
        long = [line for line in text if len(line) > width and " " in line.strip()
                and "://" not in line]
        if long:
            problems.append(f"the commit {shown!r} has {len(long)} body line"
                            f"{'s' if len(long) != 1 else ''} longer than {width} characters")
    if rules.get("signed_off_by_required") and not trailers:
        problems.append(f"the commit {shown!r} has no Signed-off-by trailer")
    return problems


def series_problems(messages: list[str], rules: dict | None, *,
                    count_commits: bool = True) -> list[str]:
    """Why a series of commit messages breaks the rules. ``{debug}`` commits
    are development-only and are not checked; ``count_commits`` also checks
    ``max_commits`` against the other commits."""
    if not rules:
        return []
    kept = [m for m in messages if not is_debug(m)]
    problems = [p for m in kept for p in message_problems(m, rules)]
    limit = rules.get("max_commits")
    if count_commits and limit is not None and len(kept) > limit:
        problems.append(f"the series has {len(kept)} commits, more than {limit}")
    return problems


def _fold(line: str) -> str:
    """A line in the form attribution trailers are matched on: NFKC,
    case-folded, with every hyphen-like character and the underscore as
    ``-`` and every colon-like character as ``:``."""
    return unicodedata.normalize("NFKC", line).casefold().translate(_FOLD)


def _is_attribution(line: str) -> bool:
    return bool(_ATTRIBUTION.match(_fold(line)))


def attribution_trailers(message: str) -> list[str]:
    """The attribution trailer lines of a commit message, exactly as written:
    the title when it is one, and any line of its body."""
    title, body = split_message(message)
    return [line.rstrip("\r") for line in [title, *body] if _is_attribution(line)]


def _continued(message: str) -> list[str]:
    """The attribution trailers of a message that a white-space-led line
    continues: ``git interpret-trailers`` folds that line into the trailer's
    value, so it could name someone the trailer as written does not."""
    _title, body = split_message(message)
    return [line.rstrip("\r") for line, after in zip(body, body[1:])
            if _is_attribution(line) and after[:1].isspace() and after.strip()]


def attribution_problems(messages: list[str], approved: list[str]) -> list[str]:
    """Why the attribution trailers of a series are not admitted. Each one
    must name the App's commit identity, or be byte-identical to a trailer of
    the ``approved`` commit messages: no commit attributes work to anyone the
    approved series does not already name. A trailer is recognized under
    Unicode lookalikes of its key and colon and as a title, and no trailer
    may be continued on a white-space-led line."""
    allowed = {t for m in approved for t in attribution_trailers(m)}
    problems = []
    for message in messages:
        title = split_message(message)[0]
        shown = title if len(title) <= 80 else title[:77] + "..."
        for trailer in attribution_trailers(message):
            raw = re.match(r"^\s*([A-Za-z0-9-]+-by)\s*:(.*)$", trailer, re.IGNORECASE)
            value = raw.group(2).strip() if raw else None
            if value == APP_IDENTITY or trailer in allowed:
                continue
            problems.append(f"the commit {shown!r} carries the attribution trailer "
                            f"{trailer[:200]!r}, which is neither the App's commit identity nor "
                            "a trailer of the approved commits")
        for trailer in _continued(message):
            problems.append(f"the commit {shown!r} continues the attribution trailer "
                            f"{trailer[:200]!r} on a line that starts with white space")
    return problems


def mbox_messages(data: bytes) -> list[str]:
    """The commit messages of a ``git format-patch`` mbox, in order: the
    subject without its ``[PATCH ...]`` prefix, a blank line, and the body up
    to the first ``---`` line, as ``git am`` reads them."""
    starts = [m.start() for m in _MBOX_FROM.finditer(data)]
    messages = []
    for i, start in enumerate(starts):
        chunk = data[start: starts[i + 1] if i + 1 < len(starts) else len(data)]
        msg = email.message_from_bytes(chunk, policy=email.policy.default)
        subject = _PATCH_PREFIX.sub("", " ".join(str(msg.get("Subject") or "").split()))
        payload = msg.get_payload(decode=True) if not msg.is_multipart() else b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            text = (payload or b"").decode(charset, "replace")
        except LookupError:
            text = (payload or b"").decode("utf-8", "replace")
        body = []
        for line in text.replace("\r\n", "\n").split("\n"):
            if line == "---":
                break
            body.append(line)
        body_text = "\n".join(body).strip("\n")
        messages.append(subject + ("\n\n" + body_text if body_text else ""))
    return messages


def headings(markdown: str) -> list[str]:
    """The ATX headings of a Markdown text, normalized for comparison."""
    found = []
    fence = None
    for line in str(markdown or "").split("\n"):
        stripped = line.strip()
        marker = stripped[:3]
        if marker in ("```", "~~~"):
            fence = None if fence == marker else (fence or marker)
            continue
        if fence:
            continue
        match = _HEADING.match(line)
        if match:
            found.append(_section_key(_heading_text(line[match.end():])))
    return found


def _heading_text(rest: str) -> str:
    """The text of an ATX heading after its opener: without trailing white
    space and an optional closing sequence of ``#`` preceded by white space."""
    text = rest.strip(" \t")
    stripped = text.rstrip("#")
    if stripped != text and (not stripped or stripped[-1] in " \t"):
        text = stripped.rstrip(" \t")
    return text


def _section_key(text: str) -> str:
    return " ".join(str(text).replace("*", "").replace("_", " ").split()).casefold()


def body_problems(body: str, sections: list[str] | None) -> list[str]:
    """The required template sections a pull request description lacks: a
    required section is present when the description has a Markdown heading
    with the same words (case and emphasis ignored)."""
    if not sections:
        return []
    present = set(headings(body))
    return [f"the description has no section {section!r} of the pull request template"
            for section in sections if _section_key(section.lstrip("#").strip()) not in present]
