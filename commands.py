# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Parser for the explicit ``/upstreaming`` command grammar (plan section 7.3).

Only the first nonblank line of a comment is considered. It must be a single
unquoted, non-fenced command. Anything else (quoted examples, fenced blocks,
multiple commands, unknown verbs or fields, malformed values) is rejected.
The parser never interprets commands embedded in other text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

COMMAND_VERSION = 1
PREFIX = "/upstreaming"

_SHA = r"[0-9a-f]{40}"
_ID = r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}"
_INT = r"[0-9]{1,9}"
_REASON = r".{1,2000}"
_CLASS = r"configuration_integration|architecture_correctness|optimized_backend"
_PATHS = r"[A-Za-z0-9_.*/?\[\]{},-]{1,500}"

# verb -> {field: regex}; every listed field is required unless it is in
# OPTIONAL for that verb; no other fields are allowed.
#
# ``contribution`` is always optional: a command is posted on the
# contribution's own tracking issue, and the controller resolves it from that
# issue. If given, it must match the issue's contribution.
#
# ``revision`` is required for commands that change or approve work (so an
# approval cannot apply to work that moved since you read it) and optional
# for stop commands (stopping is always safe, a stale stop is honored).
GRAMMAR: dict[str, dict[str, str]] = {
    # The commands of the lifecycle table.
    "pause": {"contribution": _ID, "revision": _INT, "reason": _REASON},
    # In `parked.idle` a resume starts a new generation; ``class`` and
    # ``paths`` are then recorded as the admission override.
    "resume": {"contribution": _ID, "revision": _INT, "reason": _REASON,
               "class": _CLASS, "paths": _PATHS},
    "park": {"contribution": _ID, "revision": _INT, "reason": _REASON},
    # Re-run investigation on an existing development PR. In
    # `handoff.waiting_upstream_pr` it names the exact clean head.
    "refresh": {"contribution": _ID, "revision": _INT, "reason": _REASON, "head": _SHA},
    # Only from `parked.idle`; the contribution is then closed.
    "abandon": {"contribution": _ID, "revision": _INT, "reason": _REASON},
    # The one submission command: it names the exact revision and the exact
    # candidate head of `handoff.awaiting_submission`.
    "submit-upstream": {"contribution": _ID, "revision": _INT, "head": _SHA},
    # Withdraws the submission package of `handoff.waiting_upstream_pr`; it
    # names the exact revision and the exact clean head.
    "retract-pr": {"contribution": _ID, "revision": _INT, "head": _SHA},
    # Intake commands, outside the lifecycle table.
    "resolve-operation": {
        "operation": _ID,
        "disposition": r"confirmed|not-launched|failed",
        "evidence": _ID,
    },
    "authorize-followup": {"contribution": _ID, "revision": _INT, "proposal": _ID},
    # Deprecated: see DEPRECATED.
    "force-develop": {"contribution": _ID, "revision": _INT, "class": _CLASS, "paths": _PATHS,
                      "reason": _REASON},
}

# Commands outside the lifecycle table: they act on intake and on operations.
INTAKE_COMMANDS = frozenset({"authorize-followup", "resolve-operation"})

SUBMIT_FORM = "/upstreaming submit-upstream revision=N head=FULL_SHA"
RESUME_FORM = '/upstreaming resume revision=N reason="..."'
OVERRIDE_FORM = ('/upstreaming resume revision=N class=configuration_integration '
                 'paths=".github/workflows/*" reason="..."')
REFRESH_FORM = '/upstreaming refresh revision=N reason="..."'

# Commands the lifecycle no longer has, with what replaced them. They are
# rejected with this message instead of being reported as unknown.
RETIRED: dict[str, str] = {
    verb: (f"`{verb}` is retired. Upstream submission is requested with one command, "
           f"`{SUBMIT_FORM}`, while the contribution is in `handoff.awaiting_submission`; "
           "the upstream pull request is then verified by observation")
    for verb in ("request-handoff", "approve-handoff", "register-upstream-pr")
}
RETIRED["block"] = (
    "`block` is retired. A stop is a pause: "
    '`/upstreaming pause revision=N reason="..."` records the reason and who set it, and '
    "`/upstreaming resume revision=N reason=\"...\"` ends it")
RETIRED["approve-scope"] = (
    "`approve-scope` is retired. The verified investigation dossier decides admission and "
    "provisioning starts automatically. A contribution parked by its dossier is resumed from "
    f"`parked.idle` with `{RESUME_FORM}`; `class=` and `paths=` record an admission override")
RETIRED["provision"] = (
    "`provision` is retired. The fork is provisioned automatically. When provisioning waits "
    f"for a human in `provisioning.waiting_human`, correct the prerequisite, then post "
    f"`{RESUME_FORM}`: the resume states that the prerequisite is corrected, and provisioning "
    "starts again")
RETIRED["restart"] = (
    "`restart` is retired. A new generation starts from `parked.idle` with "
    f"`{RESUME_FORM}`; `closed.idle` is final")
RETIRED["clarify"] = (
    "`clarify` is retired. A contribution that waits for a human continues with "
    f"`{RESUME_FORM}` once the cause is corrected")
RETIRED["revise-scope"] = (
    "`revise-scope` is retired. A new scope comes from a new investigation: "
    f"`{REFRESH_FORM}` keeps the fork and development PR, and a contribution in "
    f"`parked.idle` resumes with `{OVERRIDE_FORM}` to record an admission override")

# Commands still accepted while their replacement is not in effect, with the
# reply that names it.
DEPRECATED: dict[str, str] = {
    "force-develop": (
        "`force-develop` is deprecated. Its replacement is a resume from `parked.idle` with an "
        f"admission override, `{OVERRIDE_FORM}`; until the dossier admission applies that "
        "override, `force-develop` from `parked.idle` remains the override that takes effect"),
}

# Commands that stop work; processed before everything else in a tick.
URGENT = frozenset({"pause", "park", "abandon"})

OPTIONAL: dict[str, frozenset[str]] = {
    verb: frozenset({"contribution"} | ({"revision"} if verb in URGENT else set())
                    | ({"reason"} if verb == "pause" else set()))
    for verb in GRAMMAR if verb != "resolve-operation"
}
OPTIONAL["resolve-operation"] = frozenset()
OPTIONAL["resume"] = OPTIONAL["resume"] | {"class", "paths"}
OPTIONAL["refresh"] = OPTIONAL["refresh"] | {"head"}

_TOKEN_RE = re.compile(r'\s*([a-z][a-z-]*)=("((?:[^"\\]|\\.)*)"|[^\s"]+)')


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    verb: str
    args: dict[str, str] = field(default_factory=dict)
    version: int = COMMAND_VERSION

    @property
    def contribution(self) -> str | None:
        return self.args.get("contribution")

    @property
    def revision(self) -> int | None:
        r = self.args.get("revision")
        return int(r) if r is not None else None


def looks_like_command(body: str) -> bool:
    """Cheap pre-filter used before entering the serialized controller."""
    first = _first_nonblank(body)
    return first is not None and first.startswith(PREFIX + " ")


def _first_nonblank(body: str) -> str | None:
    for line in body.splitlines():
        if line.strip():
            return line
    return None


def parse(body: str) -> Command:
    """Parse a comment body into a Command or raise CommandError."""
    if not isinstance(body, str):
        raise CommandError("comment body is not text")
    if len(body.encode("utf-8")) > 16384:
        raise CommandError("comment too large")
    first = _first_nonblank(body)
    if first is None:
        raise CommandError("empty comment")
    if first != first.lstrip():
        # Indented lines render as code blocks in Markdown.
        raise CommandError("command must not be indented")
    if first.startswith((">", "```", "~~~")):
        raise CommandError("quoted or fenced text is not a command")
    if not first.startswith(PREFIX + " "):
        raise CommandError("first nonblank line is not an /upstreaming command")
    rest_lines = body.splitlines()
    idx = rest_lines.index(first)
    for line in rest_lines[idx + 1:]:
        if line.lstrip().startswith(PREFIX):
            raise CommandError("multiple commands in one comment")

    text = first[len(PREFIX):].strip()
    verb, _, tail = text.partition(" ")
    if verb in RETIRED:
        raise CommandError(RETIRED[verb])
    if verb not in GRAMMAR:
        raise CommandError(f"unknown command {verb!r}")
    spec = GRAMMAR[verb]
    args: dict[str, str] = {}
    pos = 0
    tail = tail.rstrip()
    while pos < len(tail):
        m = _TOKEN_RE.match(tail, pos)
        if not m:
            raise CommandError(f"malformed arguments near {tail[pos:pos + 20]!r}")
        key = m.group(1)
        value = m.group(3) if m.group(3) is not None else m.group(2)
        if m.group(3) is not None:
            value = re.sub(r"\\(.)", r"\1", value)
        if key in args:
            raise CommandError(f"duplicate field {key!r}")
        if key == "version":
            if value != str(COMMAND_VERSION):
                raise CommandError(f"unsupported command version {value!r}")
        elif key not in spec:
            raise CommandError(f"unknown field {key!r} for {verb}")
        else:
            if not re.fullmatch(spec[key], value, flags=re.DOTALL):
                raise CommandError(f"invalid value for {key!r}")
            args[key] = value
        pos = m.end()
    missing = set(spec) - set(args) - OPTIONAL.get(verb, frozenset())
    if missing:
        raise CommandError(f"missing fields: {', '.join(sorted(missing))}")
    return Command(verb=verb, args=args)


def cmd_parse(args: argparse.Namespace) -> int:
    body = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    try:
        command = parse(body)
    except CommandError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps({"ok": True, "verb": command.verb, "args": command.args}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m commands")
    subcommands = parser.add_subparsers(dest="command", required=True)
    parse_command = subcommands.add_parser("parse")
    parse_command.add_argument("file")
    parse_command.set_defaults(handler=cmd_parse)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
