# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The wake of the controller: every wait a tick decides names what ends it.

A wait ends at a time (``note``) or by an event that triggers the controller
by itself (``note_on``: the completion of a pipeline workflow, a push, a
command). A tick that leaves a timed wait pending reports the earliest time
in ``report["wake_at"]``, and the controller workflow dispatches
``controller-wake.yml`` for it, which sleeps until then and dispatches the
controller again. A wait this module knows is woken, so the schedule of the
controller repairs only an event that was missed and what has no wait to
name: a pipeline run or a release that GitHub or PyPI never announces, which
the observation of every tick reads. A tick that could not read what it
decides from (the dispatcher runs or the queue, a contribution it skipped) notes
a wake ``READ_RETRY`` ahead, so a later report never replaces a sooner wake.

A time that is not later than the tick's own time is no wake: the wait should
already have ended, so it becomes an alert instead, and a defect cannot loop a
controller run every minute.

``python -m wake sleep-seconds --at ISO`` prints how long the wake workflow
sleeps."""

from __future__ import annotations

import argparse
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# The wake workflow sleeps at least this long, and at most this long: its job
# runs for at most 355 minutes, and the sleep ends well before. A wait beyond
# the horizon is carried by the controller run the wake dispatches at the end
# of the sleep, which reports the wait again.
MIN_SLEEP = 60
MAX_SLEEP = 20700

AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
AT_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def parse(text: Any) -> datetime | None:
    """``text`` as an aware UTC time, or ``None`` when it is not one."""
    if isinstance(text, datetime):
        value = text
    else:
        try:
            value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def stamp(at: datetime) -> str:
    """``at`` as the ISO UTC time of ``controller-wake.yml``, rounded up to
    the next second."""
    at = at.astimezone(timezone.utc)
    if at.microsecond:
        at = at.replace(microsecond=0) + timedelta(seconds=1)
    return at.strftime(AT_FORMAT)


def note(report: dict, at: Any, why: str, *, now: datetime,
         contribution: str | None = None) -> bool:
    """Record in ``report["waits"]`` that something ends at ``at``. Returns
    whether it was recorded as a wake: a time that is not later than ``now``
    (or is no time) is recorded in ``report["overdue_waits"]`` instead."""
    when = parse(at)
    entry = {"why": why} | ({"contribution": contribution} if contribution else {})
    if when is None or when <= now:
        report.setdefault("overdue_waits", []).append(entry | {"at": str(at)})
        return False
    report.setdefault("waits", []).append(entry | {"wake_at": stamp(when)})
    return True


def note_on(report: dict, event: str, why: str, *, contribution: str | None = None) -> None:
    """Record that something ends by ``event``, which triggers the controller
    by itself."""
    report.setdefault("waits", []).append(
        {"why": why, "wake_on": event} | ({"contribution": contribution} if contribution else {}))


def harvest(report: dict, entries: list[dict], *, now: datetime, why: str) -> None:
    """Record the ``wake_at`` or ``wake_on`` of every entry of a step's
    report that carries one."""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        what = f"{why}: {entry.get('decision') or entry.get('status') or ''}".rstrip(": ")
        if entry.get("wake_at"):
            note(report, entry["wake_at"], what, now=now, contribution=entry.get("contribution"))
        if entry.get("wake_on"):
            note_on(report, entry["wake_on"], what, contribution=entry.get("contribution"))


def _reports(report: dict) -> list[dict]:
    return [report] + [r for r in (report.get("controller"),) if isinstance(r, dict)]


def earliest(report: dict, now: datetime) -> str | None:
    """The earliest wake time later than ``now`` of the ``waits`` of the
    report and of its ``controller`` report, as ``AT_FORMAT``; ``None`` when
    no wait is timed."""
    times = [when for part in _reports(report) for wait in part.get("waits") or []
             if isinstance(wait, dict) and (when := parse(wait.get("wake_at"))) is not None
             and when > now]
    return stamp(min(times)) if times else None


def finish(report: dict, now: datetime) -> None:
    """Set ``report["wake_at"]`` and raise an alert for every wait whose time
    was not later than ``now``."""
    alerts = ((report.get("controller") or {}).get("alerts")
              if isinstance(report.get("controller"), dict) else None)
    for part in _reports(report):
        for wait in part.pop("overdue_waits", None) or []:
            alert = {"alert": f"a wait that is already due is not a wake: {wait['why']} "
                              f"(at {wait['at']})"}
            if wait.get("contribution"):
                alert["contribution"] = wait["contribution"]
            if alerts is not None:
                alerts.append(alert)
            else:
                report.setdefault("wake_alerts", []).append(alert)
    report["wake_at"] = earliest(report, now)


def seconds(at: Any, now: datetime, *, maximum: int = MAX_SLEEP) -> int:
    """How long to sleep until ``at``: at least ``MIN_SLEEP`` and at most
    ``maximum`` seconds."""
    when = parse(at)
    if when is None:
        raise ValueError(f"not an ISO UTC time: {at!r}")
    return max(MIN_SLEEP, min(maximum, math.ceil((when - now).total_seconds())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wake")
    commands = parser.add_subparsers(dest="command", required=True)
    sleep = commands.add_parser("sleep-seconds")
    sleep.add_argument("--at", required=True)
    sleep.add_argument("--now", help="the current time, for tests")
    args = parser.parse_args(argv)
    if not AT_PATTERN.fullmatch(args.at):
        print(f"not an ISO UTC time: {args.at!r}", file=sys.stderr)
        return 2
    now = parse(args.now) if args.now else datetime.now(timezone.utc)
    print(seconds(args.at, now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
