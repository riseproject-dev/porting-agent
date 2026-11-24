# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The review loop of a worker run, and how trusted code reads it.

Every Work that delivers a series (a development patch or repair, an upstream
revision, a submission rewrite) reviews it in its own run with a review loop
that its prompt describes and that runs on the ``subagent`` tool of
``pi-subagents``: at most 3 rounds of fresh read-only reviewers, each followed
by a fix pass while a P0 finding or a P1 fix worth doing is found. The worker
records the loop in its report (``review_loop`` of
``schemas/worker_report.schema.json``): the rounds that ran, why the loop
stopped, and every finding with what became of it. The report is the agent's
own account and untrusted; trusted code can check its shape and its P0
findings, not that the reviews happened.

* ``check`` is the trusted check of the loop of a delivered series: at least
  one round ran, the last round reviewed exactly the delivered series (its
  digest), and no P0 finding is left open. A P0 finding is closed only
  by a later round than the one that raised it (the fresh reviewers of that
  round confirmed it fixed or withdrew it), never by the worker itself; a
  P0 finding that is deferred is not closed. A loop that fails the check
  makes the Result a rejected proposal, so nothing is pushed.
* ``comment_body`` is the summary of the loop posted, once and without retry,
  as a comment on the commit the worker's publication pushed; it shows in the
  development PR of that commit.

    python -m review_loop comment --task T --report R --fork F --head SHA
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import worker_report
from common import APP_COMMIT_NAME

COMMENT_MARKER = "<!-- upstreaming-review-loop:{} -->"
COMMENT_AUTHOR = APP_COMMIT_NAME
_OPEN = ("open", "deferred")


class LoopError(ValueError):
    """The review loop of a delivered series is missing, inconsistent, or left a P0
    finding open."""


def check(report: dict | None, *, series_digest: str | None,
          delivers_series: bool = True) -> dict | None:
    """The review loop of ``report`` when it is usable, ``None`` when the
    Work delivers no series and records none. ``series_digest`` is the
    SHA-256 of the series the Result delivers (``None`` only when it delivers
    none). Raises ``LoopError`` when a delivered series has no loop, no round
    ran, the last round did not review exactly the delivered series, the loop
    contradicts itself, or a P0 finding is open."""
    loop = (report or {}).get("review_loop")
    if loop is None:
        if delivers_series:
            raise LoopError("the worker report records no review loop")
        return None
    if not delivers_series:
        return loop
    rounds = loop["rounds"]
    if rounds < 1:
        raise LoopError("no review round ran on the delivered series")
    if loop["stopped_because"] == "decision_needed":
        raise LoopError("the review loop stopped for a decision, so no series is delivered")
    digests = loop["round_digests"]
    if len(digests) != rounds:
        raise LoopError(f"the review loop names {len(digests)} reviewed series for {rounds} rounds")
    if not series_digest or digests[-1] != series_digest:
        raise LoopError("the last review round did not review the delivered series")
    open_p0 = []
    for finding in loop["findings"]:
        title = finding["title"]
        if finding["round"] > rounds:
            raise LoopError(f"the finding {title!r} is of round {finding['round']}, after the "
                            f"last round {rounds}")
        closed = finding["closed_in_round"]
        if finding["status"] == "open" and closed is not None:
            raise LoopError(f"the finding {title!r} is open and closed in round {closed}")
        if finding["status"] in ("fixed", "withdrawn") and closed is None:
            raise LoopError(f"the finding {title!r} is {finding['status']} in no round")
        if closed is not None and closed > rounds:
            raise LoopError(f"the finding {title!r} is closed in round {closed}, after the "
                            f"last round {rounds}")
        if finding["severity"] != "P0":
            continue
        if finding["status"] in _OPEN:
            open_p0.append(title)
        elif closed <= finding["round"]:
            # Only the fresh reviewers of a later round close a P0 finding.
            raise LoopError(f"the P0 finding {title!r} of round {finding['round']} is closed "
                            f"in round {closed}, not by a later round")
    if open_p0:
        raise LoopError("the review loop ended with a P0 finding open: "
                        + "; ".join(repr(title) for title in open_p0[:5]))
    return loop


def _rounds(count: int) -> str:
    return f"{count} round" + ("" if count == 1 else "s")


def comment_body(loop: dict, head: str) -> str:
    """The comment on the commit the worker pushed: plain words, the loop and
    its findings as inert text, and the marker. Every text is bounded by the
    schema, fenced, and made free of mentions; the findings are at most 10."""
    import handoff

    stopped = {"clean": "no reviewer found a problem worth fixing",
               "optional_only": "only optional notes were left",
               "cap_reached": "the cap of 3 rounds was reached",
               "deadline": "no time was left for another round",
               "decision_needed": "a decision was needed"}.get(loop["stopped_because"],
                                                                loop["stopped_because"])
    lines = [
        f"The patch at commit `{head}` was reviewed before it was pushed, in "
        f"{_rounds(loop['rounds'])} of fresh reviewers: {stopped}.",
        "",
    ]
    for f in loop["findings"]:
        status = f["status"] + (f" in round {f['closed_in_round']}" if f["closed_in_round"] else "")
        lines.append(f"- **{f['severity']}**, round {f['round']}, {status}: "
                     + handoff._code(f["title"], 150))
        if f["detail"]:
            lines.extend("  " + line for line in handoff._fenced(f["detail"]))
        if f["resolution"]:
            lines.append("  What became of it:")
            lines.extend("  " + line for line in handoff._fenced(f["resolution"]))
    if loop["findings"]:
        lines.append("")
    lines += ["The reviewers' summary:", "", *handoff._fenced(loop["summary"]), "",
              COMMENT_MARKER.format(head)]
    return handoff.neutralize_mentions("\n".join(lines))[0]


def comment(api: Any, fork: str, head: str, body: str) -> str:
    """Post ``body`` on commit ``head`` of ``fork`` once: a comment of the App
    that carries the marker of the commit is found instead of posted again.
    Returns ``posted`` or ``found``."""
    marker = COMMENT_MARKER.format(head)
    page = 1
    while True:
        items = api.call("GET", f"/repos/{fork}/commits/{head}/comments?per_page=100&page={page}") \
            or []
        for item in items:
            if (item.get("user") or {}).get("login") == COMMENT_AUTHOR \
                    and marker in (item.get("body") or ""):
                return "found"
        if len(items) < 100:
            break
        page += 1
    # One attempt: a retry after a lost answer could post the comment twice.
    api.call("POST", f"/repos/{fork}/commits/{head}/comments", {"body": body}, attempts=1)
    return "posted"


def main(argv: list[str] | None = None) -> int:
    import re

    import review_response

    parser = argparse.ArgumentParser(prog="python -m review_loop")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("comment", help="post the summary of the review loop on the pushed commit")
    c.add_argument("--task", type=Path, required=True)
    c.add_argument("--report", type=Path, required=True)
    c.add_argument("--fork", required=True)
    c.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    token = os.environ.pop("UPSTREAMING_FORK_TOKEN", None)
    if not re.fullmatch(r"riseproject-upstreaming/[A-Za-z0-9_.-]+", args.fork) \
            or not re.fullmatch(r"[0-9a-f]{40}", args.head) or not token:
        print("no review loop comment: bad fork, bad head or no token", file=sys.stderr)
        return 1
    # The post is made once and never retried: a failure is only logged.
    try:
        task = json.loads(args.task.read_text())
        report = worker_report.check_path(task, args.report)
        loop = check(report, series_digest=None, delivers_series=False)
        if loop is None:
            print("no review loop to post")
            return 0
        loop = worker_report.clean_tree(loop)
        print(comment(review_response.Api(token), args.fork, args.head,
                      comment_body(loop, args.head)))
    except Exception as error:  # noqa: BLE001 - never fails the publication
        print(f"the review loop comment was not posted: {worker_report.clean(str(error), 300)}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
