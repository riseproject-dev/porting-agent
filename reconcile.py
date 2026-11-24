# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""One trusted reconciliation tick (plan section 9), producing state only.

Inputs, all read-only:
* committed ``.state/``;
* GitHub: intake issues, command comments, verified PRs, fork branch heads,
  live CI on the exact head;
* worker publication outcomes (``porting-publication`` artifacts of
  finished worker runs), which carry the native Result's artifact identity
  and the independent branch operation receipt;
* runner pressure.

Output: the set of state records to commit (by ``state_commit.publish``)
and a report. Remote effects are never performed here: dispatching work is
the dispatcher's job, pushing is the worker publish job's job.

A queue Result alone never advances the fork head or enters ``waiting_ci``;
only the publication receipt plus an observed branch head does.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import capacity
import ci_rerun
import collect
import controller
import dev_pr
import engine
import fork_publish
import handoff
import isolation
import lifecycle
import observe_ci
import patch_review
import plan_gate
import provisioning
import queue_policy as queue_policy_facts
import recovery
import schemas
import state
import steering
import submission
import wake
import work
import work_history
import worker_report
from common import (
    DEV_REF,
    INTAKE_LABEL,
    UPSTREAM_REF,
    canonical_json,
    contribution_file_id,
    load_policy,
    repository_root,
    safe_id,
    scope_covers,
    widen_scope,
)


@dataclass
class Snapshot:
    contributions: dict[str, dict]
    operations: dict[str, dict]
    consumed: dict[int, dict]
    projects: dict[str, dict]
    evidence: dict[str, dict]
    # What the locked upstream-PR wait already recorded for audit, by
    # contribution: the observation keys of its `record_audit_observation`
    # events, so each observation is recorded once.
    audited: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class TickOutput:
    staged: list[tuple[str, dict]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    # The contributions this tick did not process, with the exception of each.
    failures: list[isolation.ContributionFailure] = field(default_factory=list)


# The wait before the next publish-only retry, by the publications that
# failed so far (`pending_publication.attempts`): 15 minutes after the first,
# 60 after the second, 240 after every later one.
RETRY_BACKOFF_MINUTES = (15, 60, 240)
# A launched publish-only retry whose Result is not folded by then counts as a
# failed publication. The retry workflow's jobs time out after 30 minutes.
PUBLISH_RESULT_WAIT_MINUTES = 60
# A publication artifact of an ended worker run that no tick collected by
# then (its download failed, the trusted recovery rejected it, or the run is
# outside the run inventory) is no usable Result: the completed Work graph is
# then retried. It reuses the wait of a launched publish-only retry.
RESULT_COLLECTION_GRACE_MINUTES = PUBLISH_RESULT_WAIT_MINUTES
# A dispatcher run that has not completed is waited for this long after it was
# created: a run stuck queued or waiting does not hold dispatch recovery
# forever.
DISPATCHER_RUN_WAIT = timedelta(minutes=60)
# A wait for a read that failed this tick looks again after this long.
READ_RETRY = timedelta(minutes=10)


def publish_retry_delay(attempts: int) -> int:
    """Minutes before the next publish-only retry once ``attempts``
    publications of the held Result have failed."""
    return RETRY_BACKOFF_MINUTES[min(max(attempts, 1), len(RETRY_BACKOFF_MINUTES)) - 1]


# While the installed work-queue Policy refuses every dispatch and the queue
# is quiescent, the dispatcher is launched to reinstall the compiled policy at
# most once in this many minutes.
POLICY_REINSTALL_LAUNCH_MINUTES = 30
DISPATCHER_WORKFLOW = ".github/workflows/dispatcher.lock.yml"


def state_text_migration_report(
    written: list[str], *, pending: list[str] | None = None
) -> dict[str, Any]:
    """Return a workflow-safe no-op report for a migration-only tick."""
    return migration_report("state_text_migration", written, pending=pending)


def migration_report(
    key: str, written: list[str], *, pending: list[str] | None = None
) -> dict[str, Any]:
    """A workflow-safe report of a tick that only migrated state: it requests
    no effect, so every launcher of the controller workflow does nothing."""
    migration = {"written": written}
    if pending is not None:
        migration["pending"] = pending
    return {
        key: migration,
        "written": written,
        "controller": {},
        "requested_effects": [],
        "dispatch_relaunch": [],
        "provision_requests": [],
        "publication_retries": [],
        "review_comment_requests": [],
        "dev_pr_requests": [],
        "handoff_requests": [],
        "wake_at": None,
    }


def dispatch_recovery(contributions: dict[str, dict], *, runs: list[dict] | None,
                      queue_works: dict[str, str] | None, now: datetime, policy: dict,
                      skip: frozenset[str] = frozenset(), queue_policy: dict | None = None,
                      evidence: dict[str, dict] | None = None,
                      queue_retries: dict[str, dict] | None = None,
                      work_results: dict[str, dict] | None = None,
                      collected: frozenset[str] = frozenset(),
                      ) -> tuple[list[dict], list[tuple[str, dict]], list[dict]]:
    """Requested tasks whose dispatch never reached the queue (plan 15.3).

    ``runs`` are recent dispatcher workflow runs (GitHub API objects);
    ``queue_works`` maps a Work's ``graph_id`` to its queue state, from the
    pinned gh-aw CLI's validating replay ({} when the queue does not exist
    yet). Either being None means "unknown": nothing is decided this tick.

    For each ``active_work`` (oldest first) whose Work is absent from the
    queue, or admitted but still unclaimed:

    * a dispatcher run is still queued/running, whenever it was created: wait.
      It claims the Work it finds available when it stages, and its
      completion wakes the controller;
    * no dispatcher run after the request: launch again at once (the launch
      itself was lost; no run ended, so no row is taken). The launch step of
      the controller confirms that its run is listed before it ends, and
      launches are serialized, so a run launched by an earlier tick is listed;
    * otherwise a dispatcher run ended without its Work being claimed. The
      table's ``retry_dispatch`` launches the dispatcher again within
      ``dispatch_recovery`` (the dispatcher never submits Work the queue
      already holds again: it dispatches the queued Work as it was first
      submitted), and ``exhaust_recovery`` waits for a human once
      that budget is spent. Its count is the number of
      dispatcher runs that ended since the request. Each
      ended run is decided once: the record names the last one it decided
      in ``active_work.dispatched_run_id``.

    ``queue_policy`` is ``queue_policy.status`` of the replayed queue. While
    the installed Policy drops a worker profile of the compiled policy, gh-aw
    refuses every dispatch; the dispatcher reinstalls the compiled policy only
    on a quiescent queue. Until the queue is quiescent, a dispatch is not
    decided at all: nothing is charged and nothing is launched.

    A graph the queue reports completed or cancelled takes
    ``allocate_work_retry`` or ``exhaust_recovery`` once it is terminal
    without a usable Result (``_result_missing``): a cancelled graph at
    once; a completed graph when a publication collected in this tick
    (``collected``, graph ids) names it, or once ``work_results``
    (``work_result_readings``, by graph id) shows that its worker run ended
    or is gone without an unexpired ``porting-publication`` artifact, that
    the queue binds no run to it, or that the artifact of the ended run will
    never be collected (the trusted recovery rejected it, it is ambiguous, or
    the run is outside the run inventory of the tick), or was not collected
    within ``RESULT_COLLECTION_GRACE_MINUTES`` (its download failed). While the
    worker run has not ended, or was not read, nothing is decided and
    nothing is charged. A record in a state the table does not have is left
    unchanged and reported.

    Work that carries a history is dispatched only once its history is
    committed (``evidence``, the committed evidence; ``None`` skips this
    check). While it is not, and for at most ``HISTORY_WAIT`` since the
    request, the dispatch is not decided: nothing is charged and nothing is
    launched, since the dispatcher defers that Work by design. After that it
    is decided as above, so a history that is never recorded ends in the
    human wait of ``exhaust_recovery``. Once the history is
    committed, the dispatcher runs are counted from the later of the request
    and the time the history was recorded.

    ``queue_retries`` (``queue_result.work_retries``) names the available
    Work whose earlier Claim ended without a Result (a worker run that was
    cancelled, timed out or lost its runner): the queue retries it, within
    the retry attempts of its Policy, once its backoff has passed. Such a
    retry is not a lost dispatch. The dispatcher runs are counted from the
    retry time, and when none was created since it, the dispatcher is
    launched without a row and nothing is charged. A retry the queue exhausts
    cancels the Work, which ``allocate_work_retry`` or
    ``exhaust_recovery`` then decides.

    Returns (relaunch requests, staged records, report). No model is involved
    in deciding; a relaunch is an ordinary dispatcher run.
    """
    import work
    relaunch: list[dict] = []
    staged: list[tuple[str, dict]] = []
    report: list[dict] = []
    if runs is None or queue_works is None:
        return relaunch, staged, report
    iso = now.isoformat()
    pending = sorted(((k, c) for k, c in contributions.items() if c.get("active_work")),
                     key=lambda kc: kc[1]["active_work"]["requested_at"])
    for key, c in pending:
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "dispatch_recovery"):
            aw = c["active_work"]
            if c["phase"] == "handoff" and engine.state_key(c) not in SUBMISSION_WORK_STATES:
                new = engine.record_beside(c, "handoff_work_dropped", now=iso, active_work=None)
                contributions[key] = new
                staged.append(("contributions", new))
                report.append({"contribution": c["contribution_id"], "task": aw["task"],
                               "decision": "cleared obsolete Work at handoff"})
                continue
            state = queue_works.get(work.graph_id(aw["dedupe_key"]))
            if state not in (None, "available", "completed", "cancelled"):
                continue      # a live Claim is resolved by the worker outcome
            requested = datetime.fromisoformat(aw["requested_at"])
            history = _committed_history(aw, evidence)
            if history is False and state is None \
                    and now - requested < HISTORY_WAIT:
                report.append({"contribution": c["contribution_id"], "task": aw["task"],
                               "queue_state": state,
                               "decision": "waiting for the history of the Work",
                               "wake_at": (requested + HISTORY_WAIT).isoformat()})
                continue
            if isinstance(history, dict):
                try:
                    recorded = datetime.fromisoformat(str(history.get("collected_at")))
                except ValueError:
                    recorded = requested
                requested = max(requested, recorded)
            retry = (queue_retries or {}).get(work.graph_id(aw["dedupe_key"])) \
                if state == "available" else None
            if retry is not None:
                requested = max(requested, retry["retry_at"])
            after = [r for r in runs if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) >= requested]
            entry = {"contribution": c["contribution_id"], "task": aw["task"], "queue_state": state,
                     "dispatcher_runs": [r["id"] for r in after]}
            if retry is not None:
                entry["queue_retry"] = {"attempts": retry["attempts"],
                                        "max_attempts": retry.get("max_attempts"),
                                        "retry_at": retry["retry_at"].isoformat()}
            if state in ("completed", "cancelled"):
                # Work graphs are immutable and terminal. Reusing the same graph
                # can only fail: once the graph is terminal without a usable
                # Result, the table allocates a fresh graph at a new revision
                # within `work_recovery` and `work_total`
                # (`allocate_work_retry`), and waits for a human otherwise
                # (`exhaust_recovery`).
                decided, waiting, details = _result_missing(
                    state, work.graph_id(aw["dedupe_key"]), work_results, collected, now)
                if details.get("worker_run"):
                    entry["worker_run"] = details["worker_run"]
                if not decided:
                    report.append(entry | {"decision": waiting} | {
                        k: v for k, v in details.items() if k in ("wake_at", "wake_on")})
                    continue
                new, event, decision = _terminal_work(c, aw, state, now=now, policy=policy,
                                                      details=details)
                if new is None:
                    report.append(entry | {"decision": decision})
                    continue
                contributions[key] = new
                staged.extend([("contributions", new), ("events", event)])
                if new.get("active_work"):
                    relaunch.append({"type": "relaunch_dispatcher",
                                     "contribution_id": new["contribution_id"]})
                report.append(entry | {"decision": decision})
                continue
            running = _dispatchers_in_flight(runs, now)
            if running:
                report.append(entry | {
                    "decision": "dispatcher running", "wake_on": "dispatcher run completion",
                    "wake_at": (min(_created(r) for r in running)
                                + DISPATCHER_RUN_WAIT).isoformat()})
                continue
            if queue_policy and not queue_policy["preserved"] and not queue_policy["quiescent"]:
                report.append(entry | {"decision": "waiting for the queue to drain: the installed "
                                                   "Policy refuses every dispatch until the "
                                                   "dispatcher reinstalls it",
                                       "wake_on": "dispatcher run completion"})
                continue
            if not after and retry is not None and now < retry["retry_at"]:
                report.append(entry | {"decision": "waiting for the queue's retry backoff",
                                       "wake_at": retry["retry_at"].isoformat()})
                continue
            if not after:
                # With a retry, `requested` is the retry time: the worker run
                # of the last Claim ended without a Result and the queue holds
                # the Work for its next attempt, which only a dispatcher run
                # claims. A launch of an earlier tick is listed by now: the
                # launch step confirms its run before it ends.
                relaunch.append({"type": "relaunch_dispatcher", "contribution_id": c["contribution_id"]})
                report.append(entry | {"decision": "relaunch: the queue retries the Work after "
                                                   "its worker run ended without a Result"
                                       if retry is not None else "relaunch: no dispatcher run"})
                continue
            latest = max(after, key=lambda r: r["created_at"])
            if str(latest["id"]) == str(aw.get("dispatched_run_id")):
                # This run was decided in an earlier tick and the relaunch it
                # led to is not listed, so the launch was lost.
                relaunch.append({"type": "relaunch_dispatcher",
                                 "contribution_id": c["contribution_id"]})
                report.append(entry | {"decision": "relaunch: the requested run did not start"})
                continue
            # A dispatcher run ended since the request with the Work unclaimed.
            new, event, decision = _lost_dispatch(c, aw, latest, len(after), now=now,
                                                  policy=policy,
                                                  history_missing=history is False,
                                                  evidence=evidence, retry=retry)
            if new is None:
                report.append(entry | {"decision": decision})
                continue
            contributions[key] = new
            staged.extend([("contributions", new), ("events", event)])
            if new.get("active_work"):
                relaunch.append({"type": "relaunch_dispatcher", "contribution_id": c["contribution_id"]})
            report.append(entry | {"decision": decision})
    return relaunch[: max(1, policy["concurrency"]["coding_workers"])], staged, report


def _created(run: dict) -> datetime:
    return datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))


def _dispatchers_in_flight(runs: list[dict], now: datetime) -> list[dict]:
    """The dispatcher runs that have not completed and were created within
    ``DISPATCHER_RUN_WAIT``: they claim the Work they find available."""
    return [r for r in runs if r.get("status") != "completed"
            and now - _created(r) < DISPATCHER_RUN_WAIT]


def _result_missing(state: str, graph: str, work_results: dict[str, dict] | None,
                    collected: frozenset[str], now: datetime) -> tuple[bool, str, dict]:
    """Whether the Work graph ``graph``, which the queue reports ``state``
    (``completed`` or ``cancelled``), is terminal without a usable Result:
    (decided, the report decision while it is not, the event details once
    it is).

    * ``cancelled``: at once. The queue cancels Work only after its last
      Claim ended without a Result, and the publish job of a worker runs
      only for a completed outcome, so no Result can follow.
    * ``completed`` and a publication collected in this tick names the graph
      (``collected``): at once. Its Result was folded, recorded late or
      already processed, and the Work still held has no usable Result.
    * ``completed`` otherwise, from the reading of the worker run the queue
      bound to the graph (``work_results``): no reading, or a run that has
      not ended, decides nothing; a run that ended (or is gone) without an
      unexpired ``porting-publication`` artifact, or no bound run at all,
      decides at once, and so does an artifact the collector marked as never
      collectable (``never_collected`` in the reading); an artifact on an
      ended run whose download failed is waited for
      ``RESULT_COLLECTION_GRACE_MINUTES`` after the run ended.

    While nothing is decided, the details name what ends the wait:
    ``wake_at`` (a time) or ``wake_on`` (an event)."""
    if state == "cancelled":
        return True, "", {"result": "cancelled"}
    if graph in collected:
        return True, "", {"result": "collected"}
    reading = (work_results or {}).get(graph)
    if not isinstance(reading, dict):
        # A failed read: look again soon.
        return (False, "waiting: the worker run of the completed Work was not read",
                {"wake_at": (now + READ_RETRY).isoformat()})
    status = reading.get("status")
    if status == "unbound":
        return True, "", {"result": "unbound"}
    run_id = reading.get("run_id")
    details = {"worker_run": run_id, "run_conclusion": reading.get("conclusion")}
    if status == "gone" or (status == "completed" and reading.get("publication") is False):
        return True, "", details | {"result": "missing"}
    if status != "completed":
        return (False, f"waiting for worker run {run_id} to publish its Result",
                {"wake_on": "worker run completion"})
    if reading.get("never_collected"):
        return True, "", details | {"result": "uncollected",
                                    "never_collected": reading["never_collected"]}
    try:
        ended = datetime.fromisoformat(str(reading.get("ended_at")).replace("Z", "+00:00"))
    except ValueError:
        ended = None
    if ended is not None and ended.tzinfo is not None \
            and now - ended < timedelta(minutes=RESULT_COLLECTION_GRACE_MINUTES):
        return False, (f"waiting for the Result of worker run {run_id} to be collected "
                       f"(at most {RESULT_COLLECTION_GRACE_MINUTES} minutes after the run "
                       "ended)"), {"wake_at": (ended + timedelta(
            minutes=RESULT_COLLECTION_GRACE_MINUTES)).isoformat()}
    return True, "", details | {"result": "uncollected"}


def publication_collection(directory: str | None
                           ) -> tuple[dict[str, str], frozenset[str] | None, datetime | None]:
    """What the collector of ``directory`` (``--publications``) wrote about
    the runs it could not collect: ``(uncollected, inventory)``.
    ``uncollected`` maps a run id to ``rejected`` (the trusted recovery
    rejected its artifact, or it is incomplete), ``ambiguous`` (more than one
    publication artifact), or ``download_failed``, from the lines
    ``<run> <reason>`` of ``.uncollected``. ``inventory`` is the run ids of
    every run the collector listed (``.inventory``), ``None`` when it did not
    list them all, and ``listed_at`` is when it started listing
    (``.listed-at``): a run created after it is not in the inventory for want
    of being listed."""
    if not directory:
        return {}, None, None
    root = Path(directory)
    uncollected: dict[str, str] = {}
    path = root / ".uncollected"
    for line in path.read_text().splitlines() if path.is_file() else []:
        run, _, reason = line.partition(" ")
        if re.fullmatch(r"[1-9][0-9]{0,19}", run) \
                and reason in ("rejected", "ambiguous", "download_failed"):
            uncollected[run] = reason
    path = root / ".inventory"
    inventory = frozenset(line.strip() for line in path.read_text().splitlines()
                          if line.strip()) if path.is_file() else None
    path = root / ".listed-at"
    listed_at = None
    if path.is_file():
        try:
            listed_at = datetime.fromisoformat(path.read_text().strip().replace("Z", "+00:00"))
        except ValueError:
            listed_at = None
    return uncollected, inventory, listed_at


def work_result_readings(api: collect.Api, contributions: dict[str, dict],
                         queue_works: dict[str, str] | None,
                         queue_runs: dict[str, dict] | None,
                         collection: tuple[dict[str, str], frozenset[str] | None,
                                           datetime | None] | None = None,
                         ) -> dict[str, dict]:
    """``graph_id -> reading`` of the worker run of every completed Work
    graph a record still holds (``dispatch_recovery`` decides from it):
    ``{"status": "unbound"}`` when the queue binds no run to the graph
    (``queue_runs``, ``queue_result.work_runs``), else
    ``collect.worker_run_result``. ``collection`` (``publication_collection``)
    marks the artifact of an ended run that no tick can collect
    (``never_collected``: ``rejected``, ``ambiguous``, or ``outside the run
    inventory``). A failed read of one graph is printed and leaves that
    graph without a reading; the others are still read."""
    uncollected, inventory, listed_at = collection or ({}, None, None)
    readings: dict[str, dict] = {}
    for c in contributions.values():
        aw = c.get("active_work")
        if not isinstance(aw, dict) or not isinstance(aw.get("dedupe_key"), str):
            continue
        graph = work.graph_id(aw["dedupe_key"])
        if graph in readings or (queue_works or {}).get(graph) != "completed":
            continue
        run_id = ((queue_runs or {}).get(graph) or {}).get("run_id")
        if run_id is None:
            readings[graph] = {"status": "unbound"}
            continue
        try:
            readings[graph] = reading = collect.worker_run_result(api, run_id)
            if reading.get("status") == "completed" and reading.get("publication"):
                reason = uncollected.get(str(run_id))
                if reason in ("rejected", "ambiguous"):
                    reading["never_collected"] = reason
                elif inventory is not None and listed_at is not None \
                        and str(run_id) not in inventory \
                        and _created_before(reading.get("created_at"), listed_at):
                    # A run created after the listing started is not "out of
                    # reach": the next tick lists it.
                    reading["never_collected"] = "outside the run inventory"
        except (collect.CollectError, OSError) as error:
            print(f"worker run {run_id} of completed Work {graph} not read this tick: {error}",
                  file=sys.stderr)
    return readings


def _created_before(created: Any, moment: datetime) -> bool:
    """Whether the run time ``created`` is known and not after ``moment``."""
    try:
        return datetime.fromisoformat(str(created).replace("Z", "+00:00")) <= moment
    except ValueError:
        return False


def _committed_history(aw: dict, evidence: dict[str, dict] | None) -> dict | bool | None:
    """The committed ``work_history`` evidence of the requested Work ``aw``,
    ``False`` when it carries a history that is not committed yet, ``None``
    when it names no Work id or ``evidence`` is not known."""
    if evidence is None or not aw.get("dedupe_key"):
        return None
    return evidence.get(work_history.evidence_id(aw["dedupe_key"])) or False


def queue_policy_recovery(contributions: dict[str, dict], *, queue_policy: dict | None,
                          runs: list[dict] | None, now: datetime, policy: dict,
                          skip: frozenset[str] = frozenset(),
                          ) -> tuple[list[dict], list[tuple[str, dict]], list[dict]]:
    """Recovery from a work-queue Policy that refused every dispatch.

    * A record that waits with ``pending_human_action`` ``dispatch_failed``
      takes the table's ``rearm_dispatch`` once the installed Policy epoch
      was published after the failure and the compiled policy preserves it:
      the dispatch failed for a cause the dispatcher has since removed.
    * While the installed Policy drops a compiled worker profile and the
      queue is quiescent, and a record holds or waits for dispatched Work,
      the dispatcher is launched to reinstall it, at most once every
      ``POLICY_REINSTALL_LAUNCH_MINUTES`` and never while one runs.

    Returns (launch requests, staged records, report)."""
    launch: list[dict] = []
    staged: list[tuple[str, dict]] = []
    report: list[dict] = []
    if queue_policy is None:
        return launch, staged, report
    iso = now.isoformat()
    installed = queue_policy.get("installed_at")
    waiting = False
    for key, c in sorted(contributions.items()):
        if c["contribution_id"] in skip:
            continue
        pending = c.get("pending_human_action") or {}
        if c.get("active_work"):
            waiting = True
        if pending.get("kind") != "dispatch_failed":
            continue
        waiting = True
        with isolation.contribution(c["contribution_id"], "queue_policy_recovery"):
            if not queue_policy["preserved"] or not installed or \
                    datetime.fromisoformat(installed) <= datetime.fromisoformat(pending["requested_at"]):
                report.append({"contribution": c["contribution_id"],
                               "decision": "no Policy reinstall since the dispatch failed"})
                continue
            match engine.dispatch(engine.try_apply_transition(
                    c, "rearm_dispatch", policy=policy, now=iso,
                    cause_id=f"queue-policy-{queue_policy.get('epoch')}",
                    facts={"queue_policy_reinstalled"},
                    details={"policy_epoch": queue_policy.get("epoch"),
                             "installed_at": installed}),
                    ("reconcile", "queue_policy_recovery")):
                case engine.Applied() as out:
                    contributions[key] = out.record
                    staged.extend([("contributions", out.record), ("events", out.event)])
                    report.append({"contribution": c["contribution_id"],
                                   "decision": "dispatch re-armed after the Policy reinstall"})
                case engine.Skip(refused):
                    report.append({"contribution": c["contribution_id"],
                                   "decision": f"not re-armed: {refused.detail}"})
                case engine.Bug(refused):
                    raise engine.TransitionRefused(refused,
                                                   ("reconcile", "queue_policy_recovery"))
    if waiting and not queue_policy["preserved"] and queue_policy["quiescent"] and runs is not None:
        recent = _dispatchers_in_flight(runs, now) + [
            r for r in runs if r.get("status") == "completed" and now - _created(r) < timedelta(
                minutes=POLICY_REINSTALL_LAUNCH_MINUTES)]
        if recent:
            entry = {"decision": "Policy reinstall due; a dispatcher run is recent",
                     "dropped": queue_policy["dropped"]}
            if _dispatchers_in_flight(runs, now):
                entry["wake_on"] = "dispatcher run completion"
            else:
                entry["wake_at"] = (max(datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
                                        for r in recent)
                                    + timedelta(minutes=POLICY_REINSTALL_LAUNCH_MINUTES)).isoformat()
            report.append(entry)
        else:
            launch.append({"type": "reinstall_queue_policy"})
            report.append({"decision": "launch the dispatcher to reinstall the Policy",
                           "dropped": queue_policy["dropped"]})
    return launch, staged, report


def _lost_dispatch(c: dict, aw: dict, run: dict, count: int, *, now: datetime,
                   policy: dict, history_missing: bool = False,
                   evidence: dict[str, dict] | None = None, retry: dict | None = None
                   ) -> tuple[dict | None, dict | None, str]:
    """The table's row for requested Work that a dispatcher run left absent
    or unclaimed: ``retry_dispatch`` while ``dispatch_recovery`` (``count``
    relaunches spent) remains, ``exhaust_recovery`` once it is
    spent. ``count`` is the number of dispatcher runs since the request,
    all ended. The row records the decided run in ``dispatched_run_id``.
    ``evidence`` is the committed evidence the payload is measured from
    (``payload_cause``). ``history_missing`` says that the history of the
    Work was never recorded (``HISTORY_WAIT`` has passed), which the event states as the
    cause in plain words. ``retry`` is the queue's retry of the Work after a
    worker run that ended without a Result (``queue_result.work_retries``),
    which the event states too."""
    iso = now.isoformat()
    if engine.state_key(c) not in lifecycle.LIFECYCLE:
        return None, None, (f"dispatch lost in {engine.state_key(c)}, a state the table does "
                            "not have")
    counts = {"dispatch_recovery": count}
    details = {"dispatcher_run": str(run["id"]), "work": aw["dedupe_key"]}
    causes = [payload_cause(c, evidence)]
    if history_missing:
        details["history"] = "never recorded"
        hours = int(HISTORY_WAIT.total_seconds()) // 3600
        causes.insert(0, (
            f"the history of this Work was never recorded in the {hours} hours since it was "
            "requested, and the dispatcher dispatches no Work without its history, so no "
            "worker ever received it. The controller reports of those ticks say why the "
            "history was not recorded"))
    if retry is not None:
        details["queue_retry"] = {"attempts": retry["attempts"],
                                  "max_attempts": retry.get("max_attempts")}
        causes.append(
            f"the worker run of attempt {retry['attempts']} of this Work ended without a Result "
            "(it was cancelled, timed out or lost its runner) and the queue holds the Work for "
            "its next attempt, but the dispatcher runs since then did not start a worker for it. "
            "The Dispatcher runs of the tracking issue entry show why")
    cause = "; ".join(text for text in causes if text)
    if cause:
        details["why"] = cause
    limit = lifecycle.budget_limit("dispatch_recovery", policy)
    match engine.dispatch(engine.try_apply_transition(
            c, "retry_dispatch", policy=policy, now=iso, cause_id=f"dispatcher-{run['id']}",
            facts={"dispatch_lost"}, counts=counts,
            values={"active_work.dispatched_run_id": str(run["id"])},
            details=details | {"attempt": count + 1, "limit": limit}),
            ("reconcile", "_lost_dispatch")):
        case engine.Applied() as out:
            return out.record, out.event, f"relaunch: dispatch attempt {count + 1} of {limit}"
        case engine.Take(verb="exhaust_recovery"):
            out = engine.applied(engine.try_apply_transition(
                c, "exhaust_recovery", policy=policy, now=iso,
                cause_id=f"dispatcher-{run['id']}", facts={"dispatch_lost"}, counts=counts,
                details=details | {"count": count, "limit": limit}),
                ("reconcile", "_lost_dispatch"))
            return out.record, out.event, "dispatch failed; waiting for a human"
        case engine.Skip(refused):
            return None, None, f"dispatch lost: {refused.detail}"
        case engine.Bug(refused):
            raise engine.TransitionRefused(refused, ("reconcile", "_lost_dispatch"))


def payload_cause(record: dict, evidence: dict[str, dict] | None = None) -> str | None:
    """Why the dispatcher cannot dispatch the record's Work, when the
    controller can tell: its payload is over the queue's budget
    (``work.MAX_PAYLOAD_BYTES``) at the least, since it carries the frozen
    tracking issue description inline. The dispatcher skips such Work and
    dispatches every other; the event of the dispatch recovery names the
    cause, so the tracking issue entry of the human wait says what to do.
    The payload is measured from the committed ``evidence`` as the
    dispatcher builds it (``work.payload_floor``)."""
    floor = work.payload_floor(record, evidence)
    if floor is None or floor <= work.MAX_PAYLOAD_BYTES:
        return None
    description = ((record.get("active_work") or {}).get("tracking_issue") or {}).get(
        "description") or ""
    size = len(json.dumps(description, ensure_ascii=False).encode()) - 2
    return (f"the Work payload takes at least {floor} bytes, over the queue's budget of "
            f"{work.MAX_PAYLOAD_BYTES}, because it carries the frozen tracking issue "
            f"description ({size} bytes) inline: the dispatcher skips this Work. The frozen "
            "description cannot change, so the request must be made again with a description "
            f"of at most about {max(0, size - (floor - work.MAX_PAYLOAD_BYTES))} bytes")


def _terminal_work(c: dict, aw: dict, queue_state: str, *, now: datetime,
                   policy: dict, details: dict | None = None
                   ) -> tuple[dict | None, dict | None, str]:
    """The table's row for requested Work whose graph is terminal without a
    usable Result (``controller.terminal_work``); ``details`` say why."""
    return controller.terminal_work(c, aw, queue_state, now=now, policy=policy, details=details)


def _may_hold_work(rec: dict) -> bool:
    """Whether the record is in a state that can request Work at all: a
    working phase, or the submission review of the handoff phase, and not
    paused."""
    return (rec["phase"] in WORK_PHASES or engine.state_key(rec) in SUBMISSION_WORK_STATES) \
        and not rec.get("paused")


def _holds_dispatched_work(rec: dict) -> bool:
    """Whether a relaunch decided by dispatch recovery still applies once the
    record is observed: it holds requested Work, in a working phase or in a
    submission Work state of the handoff phase."""
    return rec.get("active_work") is not None and (
        rec["phase"] != "handoff" or engine.state_key(rec) in SUBMISSION_WORK_STATES)


# The phase in which the table requests Work of each task.
WORK_PHASE = {"planning": "planning", "development_patch": "development",
              "development_repair": "development", patch_review.TASK: "development",
              "upstream_revision": "upstream_review",
              work.SUBMISSION_REWRITE: "handoff", work.SUBMISSION_REVIEW: "handoff"}
# The states of the handoff phase that hold requested Work: the rewrite and
# the review of the submission.
SUBMISSION_REWRITE_STATE = lifecycle.SUBMISSION_REWRITE
SUBMISSION_REVIEW_STATE = lifecycle.SUBMISSION_REVIEW
SUBMISSION_WORK_STATES = (SUBMISSION_REWRITE_STATE, SUBMISSION_REVIEW_STATE)
# The phases whose states can request Work outside the handoff phase.
WORK_PHASES = frozenset({"planning", "provisioning", "development", "upstream_review"})


def result_mismatch(rec: dict, pub: dict) -> str | None:
    """Why a Result does not match what the record requests, or ``None``.

    A Result is folded only when it matches the Work identity: the current
    generation and revision of the record, and the requested Work, which is
    the Work the record holds (its Work id), or the held Result of the same
    worker run (a publish-only retry). In ``development.publishing`` the
    held patch pins the Result instead: the worker run, the Work, and the
    revision it names. Everything else takes
    ``record_late_effect``. Park, abandon, and
    pause clear the Work and the pending publication, so a matching revision
    alone does not prove a match.
    """
    if rec["phase"] == "handoff" and engine.state_key(rec) not in SUBMISSION_WORK_STATES:
        return "late Result during handoff"
    if not _may_hold_work(rec):
        return "late Result for a stopped contribution"
    if engine.waits_for_human(rec):
        # A wait for a human has no Result row; only its own rows leave it.
        return "late Result while the contribution waits for a human"
    state = engine.state_key(rec)
    if state in lifecycle.LIFECYCLE and not any(
            edge.transition.trigger == lifecycle.Trigger.RESULT
            and edge.verb != "record_late_effect" for edge in lifecycle.state_edges(state)):
        # Only the `agent_running` states fold a Result; Work that a record
        # holds in another state has none of their rows.
        return f"late Result: {state} has no row that folds a Result"
    task = pub.get("task") or {}
    generation, key = work.task_identity(task)
    if generation is None:
        return "late Result: its task names no Work identity"
    current = int(rec.get("generation", 0))
    if generation != current:
        return f"late Result of generation {generation}, current is {current}"
    revision = task.get("revision")
    run = str(pub.get("worker_run_id"))
    pinned = state == lifecycle.PUBLISHING and revision == (rec.get("held_patch") or {}).get(
        "revision")
    if revision != rec["revision"] and not pinned:
        return f"late Result of revision {revision}, current is {rec['revision']}"
    if (pub.get("status") == "planning") != (rec["phase"] == "planning"):
        # A plan answers planning Work only, and planning Work
        # is answered by a plan only.
        return f"late Result: a {pub.get('status')} Result has no row in the {rec['phase']} phase"
    if pub.get("status") == submission.STATUS and rec["phase"] != "handoff":
        return f"late Result: a submission review has no row in the {rec['phase']} phase"
    if pub.get("status") == patch_review.STATUS and state != "development.reviewing":
        # A review of a held patch answers its review Work only.
        return f"late Result: a review of a held patch has no row in {state}"
    if state == lifecycle.PUBLISHING:
        # The publication of the held patch: the Result of the worker run
        # the held patch pins, of the Work and revision it names, whatever
        # revisions its review took since.
        held = rec.get("held_patch") or {}
        if held and rec.get("pending_publication") and run == held.get("worker_run_id") \
                and key == held.get("work") and revision == held.get("revision") \
                and pub.get("status") in PUBLISHING_STATUSES:
            if pub.get("status") in ("rejected", "worker_blocked") \
                    and pub.get("publication_stage") != "publish":
                # A rejection by a run that did not read the record in
                # this state (the worker's verification, or a retry of it
                # made before the review) is not the publication's.
                return ("late Result: a rejection by a run that did not publish the held "
                        "patch")
            return None
        # A verification or a `no_change` report of the same worker run (an
        # artifact collected again) is no publication: it is recorded, never
        # folded.
        return "late Result: not the publication of the held patch"
    requested = rec.get("active_work")
    if requested and WORK_PHASE.get(requested.get("task")) != rec["phase"]:
        # Each phase requests its own Work only: a Result of Work of another
        # phase has no row of this one.
        return (f"late Result: {requested.get('task')} Work is not requested in the "
                f"{rec['phase']} phase")
    if requested and requested.get("generation") == current \
            and requested.get("revision") == revision \
            and requested.get("dedupe_key") == key \
            and task.get("task", requested.get("task")) == requested.get("task") \
            and (pub.get("status") == "planning") == (requested.get("task") == "planning"):
        # The Work id names the task; a plan answers planning Work
        # only, and planning Work is answered by a plan only.
        return None
    if (rec.get("pending_publication") or {}).get("worker_run_id") == run \
            and rec["phase"] in ("development", "upstream_review"):
        # A Result is held for a publish retry only where patch Work runs.
        return None
    return "late Result without matching requested Work"


def _late_effect(rec: dict, pub: dict, why: str, *, policy: dict, now: str,
                 extra: dict | None = None) -> dict:
    """The audit event of ``record_late_effect`` for one Result."""
    task = pub.get("task") or {}
    generation, key = work.task_identity(task)
    return engine.record_late_effect(
        rec, policy=policy, now=now, cause_id=_result_cause(pub, pub.get("worker_run_id")),
        details={"result_status": pub.get("status"), "task_revision": task.get("revision"),
                 "task_generation": generation, "task_work": key, "why": why,
                 **(extra or {})})


class ResultDeferred(Exception):
    """The published Result of a held patch whose development PR is not
    open yet is not folded in this tick: ``development.waiting_ci``
    requires the PR, which the controller opens from
    ``development.publishing`` once the push is confirmed. The Result is
    read again until the PR is recorded; ``staged`` holds the branch
    operation it confirmed."""

    def __init__(self, reason: str, staged: list[tuple[str, dict]]):
        super().__init__(reason)
        self.reason = reason
        self.staged = staged


def late_fold(rec: dict, outcome: dict, refused: engine.Refused | engine.Refusals, *, now: str,
              policy: dict, worker_run_id: str | None,
              staged: list[tuple[str, dict]]) -> tuple[dict, list[tuple[str, dict]]]:
    """A Result whose row the record no longer admits (it is paused, in
    another state, or at another revision: ``engine.LateEffect``) is
    recorded for audit by ``record_late_effect`` and never folded."""
    event = _late_effect(rec, dict(outcome, worker_run_id=worker_run_id),
                         f"the row is refused: {refused.detail}"[:500], policy=policy, now=now)
    return rec, staged + [("contributions", rec), ("events", event)]


def late_report_evidence(rec: dict, pub: dict, *, now: str, policy: dict,
                         known: dict[str, dict], details: dict) -> list[tuple[str, dict]]:
    """The report of a Result that takes ``record_late_effect`` although its
    push landed or may have landed (a ``published`` Result whose head was
    read back, or an ``uncertain`` one that names the commit it pushed): the
    head may be on the branch and CI run on it, so the report is stored as
    ``worker_report`` evidence of the Work it answers, with ``head_after``,
    or with ``head_candidate`` when the push was not read back, and the
    next repair reads what was tried on that head. A Result that published
    nothing stores nothing. A report that cannot be stored is recorded as
    such in ``details`` and never stops the fold of the late Result, which
    changes nothing else; an id already in ``known`` is not stored again."""
    if pub.get("status") not in ("published", "uncertain"):
        return []
    task = pub.get("task") or {}
    landed = ("record_revision_publish_success" if task.get("task") == "upstream_revision"
              else "record_publish_success")
    if worker_report.head_after(pub, landed) is None \
            and worker_report.head_candidate(pub, landed) is None:
        return []
    run = str(pub.get("worker_run_id") or (pub.get("artifact") or {}).get("run_id") or "0")
    revision = task.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        revision = None
    try:
        evidence = worker_report.evidence(rec, pub, verb="record_late_effect", now=now,
                                          policy=policy, run=run, head_verb=landed,
                                          revision=revision)
    except worker_report.ReportError as error:
        details["report"] = f"not stored: {isolation.clean_text(error)}"
        return []
    if evidence is None:
        details["report"] = "absent"
        return []
    details["report_evidence_id"] = evidence["evidence_id"]
    if evidence["evidence_id"] in known:
        return []
    return [("evidence", evidence)]


def _result_cause(outcome: dict, worker_run_id: str | None) -> str | None:
    run = worker_run_id or (outcome.get("artifact") or {}).get("run_id")
    return str(run) if run else None


# The decisions of a verified plan. A Result without a plan, or with a
# decision that is none of these, has no usable plan.
PLAN_DECISIONS = ("develop", "park", "close")
# The `terminal_reason` of a contribution closed by its plan is derived by
# `plan_gate.close_confirmation` from PyPI documents the reconciler reads
# itself, never taken from the plan. PyPI is the one registry a close is
# confirmed from: the recorded distributions may name packages of any
# registry, so each PyPI project must also name the upstream repository.
_DISTRIBUTION_NAME = re.compile(r"[A-Za-z0-9._-]+")


def _reject_plan(rec: dict, outcome: dict, *, now: str, policy: dict,
                    worker_run_id: str | None, evidence: dict | None,
                    why: str) -> tuple[dict, list[tuple[str, dict]]]:
    """Take ``reject_plan``, or ``exhaust_planning_recovery`` once
    ``planning_attempts`` is exhausted, for a planning run Result
    without a usable plan: no plan, an invalid one (an incomplete
    patch scope included), a decision that is none of develop, park, and
    close, or a decision close that PyPI does not confirm."""
    staged: list[tuple[str, dict]] = [("evidence", evidence)] if evidence else []
    facts = {"plan_rejected", "no_usable_plan"}
    details = {"result_status": outcome.get("status"),
               "decision": ((outcome.get("plan") or {}).get("proposal") or {}).get("decision")
               if isinstance(outcome.get("plan"), dict) else None,
               "why": why[:500],
               **({"evidence_id": evidence["evidence_id"]} if evidence else {})}
    cause = _result_cause(outcome, worker_run_id)
    match engine.dispatch(engine.try_apply_transition(
            rec, "reject_plan", policy=policy, now=now, cause_id=cause, facts=facts,
            details=details), ("reconcile", "_reject_plan")):
        case engine.Applied() as out:
            pass
        case engine.Take(verb="exhaust_planning_recovery"):
            out = engine.applied(engine.try_apply_transition(
                rec, "exhaust_planning_recovery", policy=policy, now=now, cause_id=cause,
                facts=facts, details=details), ("reconcile", "_reject_plan"))
        case engine.LateEffect(refused):
            return late_fold(rec, outcome, refused, now=now, policy=policy,
                             worker_run_id=worker_run_id, staged=staged)
        case engine.Bug(refused):
            raise engine.TransitionRefused(refused, ("reconcile", "_reject_plan"))
    staged.extend([("events", out.event), ("contributions", out.record)])
    return out.record, staged


def _admitted_scope(rec: dict, decision: dict) -> tuple[str | None, list | None]:
    """The admission class and scope paths a plan decision admits: the
    plan's own for a decision develop, the recorded ``admission_override``
    for a decision park (the override applies against a park only)."""
    if decision.get("decision") == "park":
        override = rec.get("admission_override") or {}
        return override.get("class"), override.get("paths")
    return decision.get("admission_class"), decision.get("allowed_paths")


def fold_plan(rec: dict, outcome: dict, *, now: str, policy: dict,
                 worker_run_id: str | None, pypi=None) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold a planning run Result through the table's rows of
    ``planning.agent_running``.

    * no plan, a decision that is none of develop, park, and close, a
      patch scope that is incomplete where ``admit_development`` and
      ``readmit_development`` check it, or a decision close that PyPI does
      not confirm: ``reject_plan``
      (``exhaust_planning_recovery`` once ``planning_attempts`` is
      exhausted);
    * develop, or park with a recorded ``admission_override``:
      ``readmit_development`` to the recorded fork when a refresh began with
      the fork and development PR the record still holds, otherwise
      ``admit_development`` to provisioning. Both clear the override;
    * park without an override: ``park_planning``;
    * close, confirmed from PyPI: ``close_planning``. ``pypi`` reads
      the PyPI document of each recorded distribution
      (``collect.pypi_project`` by default), and each must name the
      record's upstream repository; ``terminal_reason`` is
      ``already_released`` when the latest release of a distribution has a
      riscv64 wheel, else ``no_work`` (pure-Python wheels only). A failed
      read raises ``collect.CollectError``: a collector failure, so the tick
      skips the contribution and folds the same Result again. A recorded
      override is not applied, and the event says so.

    The plan is recorded as evidence first. The rows set the plan, the
    admission class, the scope paths, and whether the coverage was complete
    for the admitted scope.
    """
    if engine.state_key(rec) != "planning.agent_running":
        raise lifecycle.TransitionError(
            f"a planning run Result is folded in planning.agent_running only, not in "
            f"{engine.state_key(rec)}")
    plan = outcome.get("plan")
    staged: list[tuple[str, dict]] = []
    evidence = None
    if isinstance(plan, dict):
        eid = safe_id(f"plan-{rec['contribution_id'].replace('/', '--')}-r{rec['revision']}")
        evidence = {"schema_version": 1, "evidence_id": eid, "contribution_id": rec["contribution_id"],
                    "kind": "plan", "policy_version": policy.get("policy_version", "unknown"),
                    "collected_at": now,
                    "summary": str((outcome.get("proposal") or {}).get("diagnosis")
                                   or "planning plan")[:8000],
                    "limitations": [], "data": plan,
                    "digests": {"artifact": (outcome.get("artifact") or {}).get("digest") or ""}}
    decision = (plan.get("proposal") if isinstance(plan, dict) else None)
    decision = decision if isinstance(decision, dict) else {}
    kind = decision.get("decision")
    common = dict(policy=policy, now=now, worker_run_id=worker_run_id, evidence=evidence)
    if evidence is None:
        return _reject_plan(rec, outcome, why="the Result has no plan", **common)
    if kind not in PLAN_DECISIONS:
        return _reject_plan(rec, outcome, why=f"the plan decision {kind!r} is not develop, "
                               "park, or close", **common)
    close_reason = close_finding = None
    if kind == "close":
        # `closed.idle` is final and the plan is worker output: PyPI, read
        # here, confirms the close and gives its reason.
        read = pypi or collect.pypi_project
        names = [n for n in (rec.get("distributions") or []) if isinstance(n, str)]
        documents = {name: read(name) if _DISTRIBUTION_NAME.fullmatch(name) else None
                     for name in names}
        close_reason, problem, close_finding = plan_gate.close_confirmation(
            names, documents, upstream=(rec.get("upstream") or {}).get("full_name"))
        if problem:
            return _reject_plan(
                rec, outcome, why=f"PyPI does not confirm the decision close: {problem}",
                **common)
    staged.append(("evidence", evidence))
    cause = _result_cause(outcome, worker_run_id)
    override = rec.get("admission_override")
    rationale = str(decision.get("rationale") or "")[:2000]
    details = {"decision": kind, "evidence_id": evidence["evidence_id"]}
    if kind == "close":
        if override:
            details["admission_override_not_applied"] = override
        outcome_row = engine.try_apply_transition(
            rec, "close_planning", policy=policy, now=now, cause_id=cause,
            facts={"plan_decision_close", "close_confirmed_by_pypi"},
            values={"terminal_reason": close_reason},
            details=details | {"rationale": rationale, "pypi": close_finding})
    elif kind == "park" and not override:
        outcome_row = engine.try_apply_transition(
            rec, "park_planning", policy=policy, now=now, cause_id=cause,
            facts={"plan_decision_park"},
            values={"parked_reason": rationale or "the plan decided park"},
            details=details)
    else:
        admission_class, paths = _admitted_scope(rec, decision)
        refresh = rec.get("refresh") or {}
        fork = rec.get("fork") or {}
        readmit = bool(refresh and refresh.get("head_sha") and fork.get("repository_id")
                       and fork.get("pr_number") == refresh.get("pr_number"))
        # The scope is a guard of both admissions: a refreshed plan whose
        # scope is incomplete is rejected as a plan for a fresh fork is, so
        # no patch Work is requested without a checked scope. An override is
        # checked whichever row admits it, since nothing else names its
        # class and paths; its paths are the human's scope, so the plan's
        # coverage is recorded for it but does not refuse it.
        problem = plan_gate.scope_problem(decision, admission_class=admission_class,
                                             allowed_paths=paths)
        refused = (plan_gate.scope_problem(decision, admission_class=admission_class,
                                              allowed_paths=paths, coverage=False)
                   if kind == "park" else problem)
        if refused:
            return _reject_plan(
                rec, outcome, why=f"the admitted patch scope is incomplete: {refused}",
                **common)
        if override and kind == "park":
            details["admission_override"] = override
        elif override:
            details["admission_override_cleared"] = override
        values = {"plan_evidence_id": evidence["evidence_id"],
                  "admission_class": admission_class,
                  "scope_paths": list(paths) if isinstance(paths, list) else paths,
                  "scope_coverage_complete": problem is None}
        if readmit:
            outcome_row = engine.try_apply_transition(
                rec, "readmit_development", policy=policy, now=now, cause_id=cause,
                facts={"development_admitted", "patch_scope_complete", "recorded_fork_matches"},
                values=values,
                details=details)
        else:
            outcome_row = engine.try_apply_transition(
                rec, "admit_development", policy=policy, now=now, cause_id=cause,
                facts={"development_admitted", "patch_scope_complete", "fresh_fork_required"},
                values=values, details=details)
    match engine.dispatch(outcome_row, ("reconcile", "fold_plan")):
        case engine.Applied() as out:
            new = out.record
            staged.extend([("events", out.event), ("contributions", new)])
            return new, staged
        case engine.LateEffect(refused):
            return late_fold(rec, outcome, refused, now=now, policy=policy,
                             worker_run_id=worker_run_id, staged=staged)
        case engine.Bug(refused):
            raise engine.TransitionRefused(refused, ("reconcile", "fold_plan"))


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _outcome_basis_problem(rec: dict, outcome: dict, worker_run_id: str | None) -> str | None:
    approval = rec.get("handoff_approval") or {}
    if outcome.get("verdict") not in (submission.PASS, submission.FAIL):
        return "the outcome has no verdict"
    if outcome.get("approved_head") != approval.get("head") \
            or outcome.get("base_sha") != rec["upstream"].get("base_sha"):
        return "the outcome is not for the approved head on the frozen base"
    reasons = outcome.get("reasons")
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons) \
            or len(reasons) > submission.MAX_REASONS:
        return "the outcome's reasons are malformed"
    if outcome["verdict"] == submission.FAIL and not reasons:
        return "a failed outcome names no reason"
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(worker_run_id or "")):
        return "the outcome names no worker run"
    artifact = outcome.get("publication_artifact_id")
    if not isinstance(artifact, int) or isinstance(artifact, bool) or artifact <= 0:
        return "the outcome names no publication artifact"
    return None


def _rewrite_problem(rec: dict, outcome: dict, worker_run_id: str | None) -> str | None:
    """Why the trusted outcome of a submission rewrite is unusable, or ``None``."""
    problem = _outcome_basis_problem(rec, outcome, worker_run_id)
    if problem or outcome["verdict"] == submission.FAIL:
        return problem
    commits = outcome.get("commits")
    if outcome["reasons"] or not isinstance(commits, list) or not commits \
            or len(commits) > handoff.MAX_COMMITS \
            or not all(isinstance(c, dict) and isinstance(c.get("message"), str)
                       and len(c["message"]) <= 20000 and _SHA_RE.match(str(c.get("tree")))
                       for c in commits) \
            or not all(_SHA_RE.match(str(outcome.get(k) or "")) for k in
                       ("head", "clean_tree", "candidate_tree")) \
            or commits[-1]["tree"] != outcome["clean_tree"] \
            or not all(re.fullmatch(r"[0-9a-f]{64}", str(outcome.get(k) or ""))
                       for k in ("series_digest", "text_digest")) \
            or not isinstance(outcome.get("title"), str) or not outcome["title"].strip() \
            or len(outcome["title"]) > submission.MAX_TITLE \
            or not isinstance(outcome.get("body"), str) or len(outcome["body"]) > submission.MAX_BODY \
            or submission.text_digest(outcome["title"], outcome["body"]) != outcome["text_digest"]:
        return "a passing outcome is malformed"
    return None


def _review_problem(rec: dict, outcome: dict, worker_run_id: str | None,
                    rewrite: dict | None) -> str | None:
    """Why the trusted outcome of a submission review is unusable, or
    ``None``: it must review exactly the rewrite the record names."""
    problem = _outcome_basis_problem(rec, outcome, worker_run_id)
    if problem:
        return problem
    data = (rewrite or {}).get("data") or {}
    if not rewrite or outcome.get("rewrite_evidence_id") != rec.get("submission_rewrite_id") \
            or outcome.get("series_digest") != data.get("series_digest") \
            or outcome.get("text_digest") != data.get("text_digest"):
        return "the outcome does not review the rewrite the record names"
    if not isinstance(outcome.get("expectations"), list) or len(outcome["expectations"]) > 60:
        return "the outcome's expectations are malformed"
    return None


def _unusable_submission_result(rec: dict, why: str, *, now: str,
                                policy: dict) -> tuple[dict, list[tuple[str, dict]]]:
    """A rewrite or review Result that is not usable: Work without a usable
    Result (``allocate_work_retry``, then ``exhaust_recovery``)."""
    aw = rec.get("active_work") or {}
    new, event, decision = _terminal_work(rec, aw, "result_unusable",
                                          now=datetime.fromisoformat(now), policy=policy)
    if new is None:
        raise lifecycle.TransitionError(f"an unusable submission Result: {decision}")
    event = dict(event, details=dict(event.get("details") or {},
                                     why=isolation.clean_text(why, 300)))
    return new, [("contributions", new), ("events", event)]


def _submission_evidence(rec: dict, kind: str, outcome: dict, data: dict, summary: str, *,
                         now: str, policy: dict, worker_run_id: str | None) -> dict:
    run = str(worker_run_id or "0")
    eid = safe_id(f"{kind.replace('_', '-')}-{rec['contribution_id'].replace('/', '--')}"
                  f"-r{rec['revision']}-{run}")
    evidence = {"schema_version": 1, "evidence_id": eid, "contribution_id": rec["contribution_id"],
                "kind": kind, "policy_version": policy.get("policy_version", "unknown"),
                "collected_at": now, "summary": summary[:8000], "limitations": [], "data": data,
                "digests": {"series": str(outcome.get("series_digest") or "")}}
    schemas.validate("evidence", evidence)
    return evidence


def fold_submission_rewrite(rec: dict, outcome: dict, *, now: str, policy: dict,
                            worker_run_id: str | None) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold the Result of the submission rewrite Work through the table's
    rows of ``handoff.agent_running``.

    The worker's publish job ran the trusted deterministic check
    (``submission.check_rewrite``) without a token: its outcome is the
    verdict. A pass takes ``accept_submission_rewrite`` with the rewritten
    series, title, and description recorded as rewrite evidence, which the
    review Work reads and the build of `riscv64` writes. A fail takes
    ``reject_submission_rewrite``: the reasons are recorded and become the
    pending feedback of the next development Work; nothing is posted. An
    outcome that is not usable is Work without a usable Result."""
    why = (_rewrite_problem(rec, outcome, worker_run_id)
           if outcome.get("status") == submission.REWRITE
           else f"the rewrite Result is not usable: {outcome.get('status')}: "
                f"{outcome.get('reason') or ''}")
    if why:
        return _unusable_submission_result(rec, why, now=now, policy=policy)
    passed = outcome["verdict"] == submission.PASS
    data = {"verdict": outcome["verdict"], "reasons": list(outcome["reasons"]),
            "approved_head": outcome["approved_head"], "base_sha": outcome["base_sha"],
            "artifact": {"run_id": str(worker_run_id),
                         "artifact_id": outcome.get("publication_artifact_id"),
                         "expires_at": outcome.get("publication_artifact_expires_at")}}
    if passed:
        data.update({k: outcome[k] for k in ("title", "body", "head", "clean_tree",
                                             "candidate_tree", "series_digest", "text_digest")},
                    commits=[{"message": c["message"], "tree": c["tree"]}
                             for c in outcome["commits"]],
                    rules_from=[str(n)[:200] for n in outcome.get("rules_from") or []][:4])
    summary = ("The submission rewrite passed the trusted check." if passed else
               "The submission rewrite did not pass:\n"
               + "\n".join(f"- {r}" for r in data["reasons"]))
    evidence = _submission_evidence(rec, submission.REWRITE, outcome, data, summary, now=now,
                                    policy=policy, worker_run_id=worker_run_id)
    eid = evidence["evidence_id"]
    cause = _result_cause(outcome, worker_run_id)
    details = {"evidence_id": eid, "verdict": outcome["verdict"],
               "reasons": [isolation.clean_text(r, 300) for r in data["reasons"][:8]]}
    reports: list[tuple[str, dict]] = []
    if passed:
        details = dict(details, head=outcome["head"])
        reports = report_evidence(rec, outcome, "accept_submission_rewrite", now=now,
                                  policy=policy, run=worker_run_id, details=details)
        outcome_row = engine.try_apply_transition(
            rec, "accept_submission_rewrite", policy=policy, now=now, cause_id=cause,
            facts={"submission_rewrite_passed"}, values={"submission_rewrite_id": eid},
            details=details)
    else:
        pending = list(dict.fromkeys((rec.get("pending_feedback_evidence_ids") or []) + [eid]))
        outcome_row = engine.try_apply_transition(
            rec, "reject_submission_rewrite", policy=policy, now=now, cause_id=cause,
            facts={"submission_rewrite_failed"},
            values={"pending_feedback_evidence_ids": pending[-64:]}, details=details)
    match engine.dispatch(outcome_row, ("reconcile", "fold_submission_rewrite")):
        case engine.Applied() as out:
            pass
        case engine.LateEffect(refused):
            return late_fold(rec, outcome, refused, now=now, policy=policy,
                             worker_run_id=worker_run_id, staged=[])
        case engine.Bug(refused):
            raise engine.TransitionRefused(refused, ("reconcile", "fold_submission_rewrite"))
    return out.record, [("evidence", evidence), *reports, ("events", out.event),
                        ("contributions", out.record)]


def fold_submission_review(rec: dict, outcome: dict, *, now: str, policy: dict,
                           worker_run_id: str | None,
                           evidence: dict | None = None) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold the Result of the submission review Work through the table's
    rows of ``handoff.reviewing``.

    The worker's publish job checked that the review answers the Work and
    reviews exactly the rewritten series and text
    (``submission.check_review``). A pass takes ``accept_submission_review``:
    the review evidence carries the rewrite it passed (the series, title,
    description, and the artifact that holds the series), which the build of
    `riscv64` and the package read. A fail takes ``reject_submission_review``
    with its reasons as pending feedback. An outcome that is not usable is
    Work without a usable Result. ``evidence`` holds the rewrite evidence the
    record names."""
    rewrite = (evidence or {}).get(rec.get("submission_rewrite_id") or "")
    why = (_review_problem(rec, outcome, worker_run_id, rewrite)
           if outcome.get("status") == submission.REVIEW
           else f"the review Result is not usable: {outcome.get('status')}: "
                f"{outcome.get('reason') or ''}")
    if why:
        return _unusable_submission_result(rec, why, now=now, policy=policy)
    passed = outcome["verdict"] == submission.PASS
    data = dict(copy.deepcopy(rewrite["data"]), verdict=outcome["verdict"],
                reasons=list(outcome["reasons"]),
                rewrite_evidence_id=rewrite["evidence_id"],
                review_artifact={"run_id": str(worker_run_id),
                                 "artifact_id": outcome.get("publication_artifact_id")},
                expectations=[{k: str(e.get(k))[:1000] if k != "met" else bool(e.get("met"))
                               for k in ("expectation", "met", "source", "note") if k in e}
                              for e in outcome["expectations"][:60] if isinstance(e, dict)])
    summary = ("The submission review passed the rewritten series and text." if passed else
               "The submission review did not pass:\n"
               + "\n".join(f"- {r}" for r in data["reasons"]))
    review = _submission_evidence(rec, submission.REVIEW, outcome, data, summary, now=now,
                                  policy=policy, worker_run_id=worker_run_id)
    eid = review["evidence_id"]
    cause = _result_cause(outcome, worker_run_id)
    details = {"evidence_id": eid, "verdict": outcome["verdict"],
               "reasons": [isolation.clean_text(r, 300) for r in data["reasons"][:8]]}
    reports: list[tuple[str, dict]] = []
    if passed:
        details = dict(details, head=data.get("head"))
        reports = report_evidence(rec, outcome, "accept_submission_review", now=now,
                                  policy=policy, run=worker_run_id, details=details)
        outcome_row = engine.try_apply_transition(
            rec, "accept_submission_review", policy=policy, now=now, cause_id=cause,
            facts={"submission_review_passed"}, values={"submission_review_id": eid},
            details=details)
    else:
        pending = list(dict.fromkeys((rec.get("pending_feedback_evidence_ids") or []) + [eid]))
        outcome_row = engine.try_apply_transition(
            rec, "reject_submission_review", policy=policy, now=now, cause_id=cause,
            facts={"submission_review_failed"},
            values={"pending_feedback_evidence_ids": pending[-64:]}, details=details)
    match engine.dispatch(outcome_row, ("reconcile", "fold_submission_review")):
        case engine.Applied() as out:
            pass
        case engine.LateEffect(refused):
            return late_fold(rec, outcome, refused, now=now, policy=policy,
                             worker_run_id=worker_run_id, staged=[])
        case engine.Bug(refused):
            raise engine.TransitionRefused(refused, ("reconcile", "fold_submission_review"))
    return out.record, [("evidence", review), *reports, ("events", out.event),
                        ("contributions", out.record)]


def _patch_review_problem(rec: dict, outcome: dict, worker_run_id: str | None) -> str | None:
    """Why the trusted outcome of a review of the held patch is unusable, or
    ``None``: it must review exactly the patch the record holds."""
    if outcome.get("status") != patch_review.STATUS:
        return (f"the review Result is not usable: {outcome.get('status')}: "
                f"{outcome.get('reason') or ''}")
    held = rec.get("held_patch") or {}
    if not held or outcome.get("patch_digest") != held.get("patch_digest") \
            or outcome.get("candidate") != held.get("candidate"):
        return "the outcome does not review the patch the record holds"
    if outcome.get("verdict") not in (patch_review.PASS, patch_review.FAIL) \
            or not isinstance(outcome.get("findings"), list) \
            or patch_review.derived_verdict(outcome["findings"]) != outcome["verdict"] \
            or not isinstance(outcome.get("summary"), str):
        return "the outcome's verdict or findings are malformed"
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(worker_run_id or "")):
        return "the outcome names no reviewer run"
    return None


def fold_patch_review(rec: dict, outcome: dict, *, now: str, policy: dict,
                      worker_run_id: str | None) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold the Result of the review Work of a held patch through the rows of
    ``development.reviewing``.

    The reviewer's publish job checked the review without a token
    (``patch_review.check``): it reviews exactly the held patch, and its
    verdict is derived from its findings. Every usable review is stored as
    ``patch_review`` evidence, which the history of every later Work and
    review carries. A pass takes ``accept_patch_review``: the held patch is
    pending publication at once, with the feedback its Work carried. A fail
    takes ``reject_patch_review`` while ``dev_reviews`` remains: the review
    becomes pending feedback of the next development Work and the held
    patch is discarded; once ``dev_reviews`` is exhausted it takes
    ``publish_after_failed_reviews``: the held patch is published anyway and
    the pending publication names the review, which trusted code posts on
    the development PR once the patch is published. A review that is not
    usable is Work without a usable Result."""
    why = _patch_review_problem(rec, outcome, worker_run_id)
    if why:
        return _unusable_submission_result(rec, why, now=now, policy=policy)
    held = rec["held_patch"]
    run = str(worker_run_id)
    cause = _result_cause(outcome, worker_run_id)
    passed = outcome["verdict"] == patch_review.PASS
    rejections = int(rec["budget"].get("review_rejections", 0))

    def row(verb: str) -> tuple[engine.Applied | engine.Refused, dict]:
        """The row ``verb`` with the review evidence of its disposition."""
        disposition = {"accept_patch_review": "publish",
                       "reject_patch_review": "return_for_repair"}.get(verb, "publish_anyway")
        review = patch_review.evidence(rec, outcome, now=now, policy=policy, run=run,
                                       disposition=disposition,
                                       rejections=0 if passed else rejections + 1)
        eid = review["evidence_id"]
        blocking = [f for f in outcome["findings"] if f.get("severity") in patch_review.BLOCKING]
        details = {"evidence_id": eid, "verdict": outcome["verdict"], "disposition": disposition,
                   "candidate": held.get("candidate"),
                   "findings": [isolation.clean_text(f"{f['severity']}: {f['title']}", 300)
                                for f in (blocking or outcome["findings"])[:8]]}
        if verb == "reject_patch_review":
            pending = list(dict.fromkeys((rec.get("pending_feedback_evidence_ids") or [])
                                         + [eid]))
            values: dict[str, Any] = {"pending_feedback_evidence_ids": pending[-64:]}
        else:
            values = {"pending_publication": {
                "worker_run_id": held["worker_run_id"], "attempts": 0, "last_reason": "",
                "next_attempt_at": now,
                "feedback_evidence_ids": list(held.get("feedback_evidence_ids") or []),
                "patch_review": {"evidence_id": eid, "verdict": outcome["verdict"],
                                 "published_anyway": verb == "publish_after_failed_reviews"}}}
        facts = {"patch_review_passed"} if passed else {"patch_review_failed"}
        return engine.try_apply_transition(rec, verb, policy=policy, now=now, cause_id=cause,
                                           facts=facts, values=values,
                                           details=details), review

    first, review = row("accept_patch_review" if passed else "reject_patch_review")
    match engine.dispatch(first, ("reconcile", "fold_patch_review")):
        case engine.Applied() as out:
            pass
        case engine.Take(verb="publish_after_failed_reviews"):
            # `dev_reviews` is exhausted: the held patch is published anyway.
            anyway, review = row("publish_after_failed_reviews")
            out = engine.applied(anyway, ("reconcile", "fold_patch_review"))
        case engine.LateEffect(refused):
            return late_fold(rec, outcome, refused, now=now, policy=policy,
                             worker_run_id=worker_run_id, staged=[])
        case engine.Bug(refused):
            raise engine.TransitionRefused(refused, ("reconcile", "fold_patch_review"))
    return out.record, [("evidence", review), ("events", out.event),
                        ("contributions", out.record)]


def apply_publication(rec: dict, outcome: dict, *, now: str, policy: dict | None = None,
                      worker_run_id: str | None = None, evidence: dict | None = None,
                      pypi=None, operations: dict[str, dict] | None = None
                      ) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold one Result (from fork_publish / queue_result) into the record.

    * planning Work: the plan decides, by the table (``fold_plan``);
    * the submission rewrite and review Work: their trusted checks decide
      (``fold_submission_rewrite``, ``fold_submission_review``);
    * the review Work of a held development patch: its trusted check
      decides (``fold_patch_review``);
    * patch Work in development and revision Work in upstream review: the
      table's rows (``fold_patch_result``).

    ``pypi`` reads a PyPI document for the confirmation of a decision close
    (``collect.pypi_project`` by default). ``operations`` is the tick's
    operation store, read to find the branch operation of an earlier attempt
    of a held Result (``held_publication_operation``).
    """
    if outcome.get("status") == "planning":
        return fold_plan(rec, outcome, now=now, policy=policy or {},
                            worker_run_id=worker_run_id, pypi=pypi)
    if engine.state_key(rec) == SUBMISSION_REWRITE_STATE:
        return fold_submission_rewrite(rec, outcome, now=now, policy=policy or {},
                                       worker_run_id=worker_run_id)
    if engine.state_key(rec) == SUBMISSION_REVIEW_STATE:
        return fold_submission_review(rec, outcome, now=now, policy=policy or {},
                                      worker_run_id=worker_run_id, evidence=evidence)
    if engine.state_key(rec) == "development.reviewing":
        return fold_patch_review(rec, outcome, now=now, policy=policy or {},
                                 worker_run_id=worker_run_id)
    return fold_patch_result(rec, outcome, now=now, policy=policy or {},
                             worker_run_id=worker_run_id, operations=operations,
                             evidence=evidence)


_ARTIFACT_KEYS = ("work_id", "claim_id", "artifact_id", "name", "digest", "run_id")

# The rows that fold the Result of a successful worker: its Result was
# accepted and the pipeline acts on it. Only their reports are stored, as
# `worker_report` evidence that every later Work of the contribution
# carries in its history. A Result whose push may have landed stores it too
# (`pushed_report_evidence`); the report of any other fold is discarded
# with the artifact.
REPORT_VERBS = frozenset({"record_publish_success", "record_revision_publish_success",
                          "hold_patch_for_review",
                          "accept_submission_rewrite", "accept_submission_review",
                          "request_ci_rerun", "require_reviewer_answer"})


def report_evidence(rec: dict, outcome: dict, verb: str, *, now: str, policy: dict,
                    run: str | None, details: dict,
                    operation: dict | None = None, head_verb: str | None = None,
                    revision: int | None = None) -> list[tuple[str, dict]]:
    """The report of a Result folded by ``verb``, staged as ``worker_report``
    evidence when ``verb`` is a success row; its id is named in
    ``details``, or that it is absent when the Work carries no history and
    returned no usable report. A success row of Work that carries a history
    never drops its report: ``worker_report.evidence`` raises when it cannot
    be stored, so the contribution pauses and the Result stays unfolded.
    Nothing is staged for any other row. ``operation`` is the branch
    operation the row names, whose commit the report records as the head
    the Work left. ``head_verb`` names the success row whose head the
    report records when ``verb`` holds a patch it did not publish, and
    ``revision`` the revision of the Work when the record has left it (the
    publication of a held patch)."""
    if verb not in REPORT_VERBS:
        return []
    evidence = worker_report.evidence(rec, outcome, verb=verb, now=now, policy=policy,
                                      run=str(run or "0"), operation=operation,
                                      head_verb=head_verb, revision=revision)
    if evidence is None:
        details["report"] = "absent"
        return []
    details["report_evidence_id"] = evidence["evidence_id"]
    return [("evidence", evidence)]


def pushed_report_evidence(rec: dict, outcome: dict, verb: str, *, now: str, policy: dict,
                           run: str, details: dict,
                           operation: dict | None = None,
                           revision: int | None = None) -> list[tuple[str, dict]]:
    """The report of a Result of patch or revision Work that a row other
    than the success row folds although its push may have landed: an
    ``uncertain`` Result held for a publish-only retry, or a retry rejected
    as stale behind the operation of an earlier attempt (``operation``).
    The Result is marked processed by this fold, so its report is stored
    now, as ``worker_report`` evidence with the commit it proposed as
    ``head_candidate``, whatever ends the hold later (a retry, its
    exhaustion, a pause, a park); the success fold of a retry of the same
    worker run stores the same evidence id again with ``head_after``. A
    Result that proposed no commit stores nothing. Like a success row, a
    report the gate accepted that cannot be stored raises
    ``worker_report.ReportError``, so the contribution pauses and the
    Result stays unfolded."""
    landed = WORK_RESULT_STATES[engine.state_key(rec)]["published"]
    if worker_report.head_after(outcome, landed, operation) is None \
            and worker_report.head_candidate(outcome, landed, operation) is None:
        return []
    evidence = worker_report.evidence(rec, outcome, verb=verb, now=now, policy=policy,
                                      run=str(run), head_verb=landed, operation=operation,
                                      revision=revision)
    if evidence is None:
        details["report"] = "absent"
        return []
    details["report_evidence_id"] = evidence["evidence_id"]
    return [("evidence", evidence)]


def _rehearsal_evidence(rec: dict, outcome: dict, run: str, policy: dict, now: str) -> dict:
    """The evidence a worker reports when its reproduction infrastructure
    was blocked."""
    proposal = outcome.get("proposal") or {}
    evidence = {
        "schema_version": 1,
        "evidence_id": safe_id(
            f"rehearsal-{rec['contribution_id'].replace('/', '--')}-r{rec['revision']}-{run}"),
        "contribution_id": rec["contribution_id"],
        "kind": "rehearsal",
        "policy_version": policy.get("policy_version", "unknown"),
        "collected_at": now,
        "summary": str(outcome.get("reason") or "worker reproduction infrastructure blocked")[:8000],
        "limitations": list(proposal.get("validation_not_performed") or [])[:16],
        "data": {
            "worker_run_id": run,
            "failure_class": outcome.get("failure_class"),
            "failure_cluster": proposal.get("failure_cluster"),
            "reproduction": proposal.get("reproduction"),
        },
        "digests": {"artifact": (outcome.get("artifact") or {}).get("digest") or ""},
    }
    schemas.validate("evidence", evidence)
    return evidence


def _fail_publication(rec: dict, run: str, reason: str, *, now: str, policy: dict,
                      cause: str | None, details: dict) -> engine.Applied:
    """A publication of the Result of ``run`` failed: ``schedule_publish_retry``
    holds the Result for a publish-only retry and charges ``publish_recovery``;
    ``exhaust_recovery`` once it is spent."""
    held = rec.get("pending_publication") or {}
    before = int(held.get("attempts", 0))
    pending = {"worker_run_id": run, "attempts": before + 1, "last_reason": reason[:2000],
               "next_attempt_at": (datetime.fromisoformat(now) + timedelta(
                   minutes=publish_retry_delay(before + 1))).isoformat(),
               # The review a held patch was published after stays with it.
               **({"patch_review": held["patch_review"]} if held.get("patch_review") else {})}
    match engine.dispatch(engine.try_apply_transition(
            rec, "schedule_publish_retry", policy=policy, now=now, cause_id=cause,
            facts={"publication_infrastructure_error"}, values={"pending_publication": pending},
            details=details | {"next_attempt_at": pending["next_attempt_at"]}),
            ("reconcile", "_fail_publication")):
        case engine.Applied() as out:
            return out
        case engine.Take(verb="exhaust_recovery"):
            return engine.applied(engine.try_apply_transition(
                rec, "exhaust_recovery", policy=policy, now=now, cause_id=cause,
                facts={"publication_infrastructure_error"},
                details=details | {"attempts": before}), ("reconcile", "_fail_publication"))
        case engine.Bug(refused):
            raise engine.TransitionRefused(refused, ("reconcile", "_fail_publication"))


def _charged_launch(held: dict, outcome: dict) -> bool:
    """Whether the publication failure an ``infra_error`` Result reports is
    already charged to the held publication.

    Every failed publication is charged once (``schedule_publish_retry``):
    the worker's own as attempt 1, and each launched publish-only retry,
    dispatched with attempt ``pending_publication.attempts + 1``, either by
    its Result or, when none is folded within the wait, by the wait. So a
    Result of an attempt not above ``attempts`` is charged already, and so
    is any Result while no retry is launched (``launched_at`` absent): the
    launches before were all charged."""
    attempt = outcome.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 1:
        return attempt <= int(held.get("attempts", 0))
    return not held.get("launched_at")


# The states whose Results ``fold_patch_result`` folds, and the rows that
# differ between them: the success row, the Result statuses it folds, what a
# `no_change` report and a stale proposal take, and the statuses that are a
# failed publication. The success row is taken only for a publication that
# was read back, so in both states a publication whose push was not read back
# (`uncertain`) is a failed publication: the Result stays held, and the
# publish-only retry reads back the head a lost push left.
# The statuses of the publication of a held patch in `development.publishing`.
# The publication artifacts a record remembers as folded: the controller
# collects the 20 newest runs of each of four workflows (planner, worker,
# reviewer, publish-only retry), so 80 artifacts can be collected again; the
# fence keeps more than three times as many.
PROCESSED_PUBLICATIONS = 256
PUBLISHING_STATUSES = ("published", "rejected", "worker_blocked", "infra_error", "uncertain",
                       "proposal")
WORK_RESULT_STATES = {
    "development.agent_running": {"published": "record_publish_success",
                                  "success": ("published",),
                                  "failed": ("infra_error", "uncertain"),
                                  "stale": "replan_development",
                                  "verified": "hold_patch_for_review"},
    # The publication of a held patch after its review: its rejection is
    # the held patch no longer rebuilding to the reviewed commit.
    lifecycle.PUBLISHING: {"published": "record_publish_success",
                           "success": ("published",),
                           "failed": ("infra_error", "uncertain"),
                           "stale": "replan_development",
                           "rejected": "held_patch_rejected"},
    "upstream_review.agent_running": {"published": "record_revision_publish_success",
                                      "success": ("published",),
                                      "failed": ("infra_error", "uncertain"),
                                      "no_change": "require_reviewer_answer"},
}

_SHA = re.compile(r"[0-9a-f]{40}")


def published_head(outcome: dict) -> str:
    """The head a published Result read back on `riscv64`, which
    ``record_revision_publish_success`` records in ``upstream_pr.head_sha``:
    the commit of its branch operation, which the publisher confirmed by
    reading the branch back after the push, or, for a publish-only retry
    that found the push of an earlier attempt on the branch (no operation),
    the candidate it read there. A head that was not read back is refused."""
    op = outcome.get("operation")
    candidate = outcome.get("candidate")
    head = candidate
    if op:
        head = (op.get("proposed") or {}).get("commit")
        if op.get("status") != "confirmed" or op.get("kind") not in (
                "branch_update", "branch_rewrite") \
                or (op.get("target") or {}).get("ref") != UPSTREAM_REF \
                or (candidate is not None and candidate != head):
            head = None
    if not (isinstance(head, str) and _SHA.fullmatch(head)):
        raise lifecycle.TransitionError(
            f"the published revision names no head read back on {UPSTREAM_REF}")
    return head


_BRANCH_KINDS = ("branch_update", "branch_rewrite")


def held_publication_operation(rec: dict, outcome: dict,
                               operations: dict[str, dict] | None) -> dict | None:
    """The branch operation an earlier attempt of the held Result recorded:
    of the contribution and its revision, on the branch of the phase
    (``lifecycle.work_ref``) with the recorded head as its lease, carrying the
    marker of the Result's artifact when the outcome carries one, and
    proposing the outcome's candidate when it names one. A ``confirmed``
    operation is preferred to an open one (``applying``, ``uncertain``); the
    newest wins. None without the operation store, without a held Result, or
    when none matches."""
    if not operations or not rec.get("pending_publication"):
        return None
    artifact_id = (outcome.get("artifact") or {}).get("artifact_id")
    candidate = outcome.get("candidate")
    ref, head = lifecycle.work_ref(rec), lifecycle.recorded_head(rec)
    found = []
    for op in operations.values():
        target, proposed = op.get("target") or {}, op.get("proposed") or {}
        if op.get("contribution_id") != rec["contribution_id"] \
                or op.get("kind") not in _BRANCH_KINDS or op.get("revision") != rec["revision"] \
                or target.get("ref") != ref or target.get("expected_head") != head \
                or op.get("status") not in ("confirmed", "applying", "uncertain"):
            continue
        if artifact_id and proposed.get("marker") != f"artifact:{artifact_id}":
            continue
        if candidate and proposed.get("commit") != candidate:
            continue
        found.append(op)
    if not found:
        return None
    return max(found, key=lambda op: (op["status"] == "confirmed", str(op.get("created_at") or "")))


def _confirm_read_back(op: dict, candidate: str, attempt: Any, *, now: str) -> dict:
    """Close the open branch operation of an earlier attempt that a
    publish-only retry read back on the branch, by the rule the branch
    observation uses (``recovery.discover_branch``): an ``applying``
    operation lost the response of its push and takes ``record_response``
    first. The operation is returned unchanged when the head read back is
    not its commit."""
    if op["status"] == "applying":
        op = recovery.mark_result(op, response_ok=None, external_ids=None, now=now)
    if recovery.discover_branch(op, {"head": candidate}) != "confirmed":
        return op
    return recovery.record_discovery(
        op, "confirmed",
        f"{op['target']['ref']} at {candidate}, read back by publish attempt {attempt}", now)


def _note_due(details: dict, rec: dict, due: list[dict], dropped: list[dict]) -> None:
    """Name in the event the comments a write makes due and those it drops
    to fit them (``patch_review.comment_due``); the tick alerts on each
    comment dropped (``review_comments``)."""
    before = {item["evidence_id"] for item in rec.get("review_comments") or []}
    added = [item["evidence_id"] for item in due if item["evidence_id"] not in before]
    if added:
        details["review_comments_due"] = added
    if dropped:
        details["review_comments_dropped"] = [item["evidence_id"] for item in dropped]


def _landed_due(rec: dict, landed: list[tuple[str, str]], details: dict, *,
                now: str) -> list[dict] | None:
    """The due comments once the reviews of ``landed`` are due, written
    beside the row that folds the Result (whose rows never write
    ``review_comments``), or ``None`` when nothing landed."""
    if not landed:
        return None
    due, dropped = patch_review.comment_due(rec, landed, now=now)
    _note_due(details, rec, due, dropped)
    return due


def extended_scope(scope_paths: list[str] | None, changed_files: list[str] | None
                   ) -> tuple[list[str] | None, list[str], str | None]:
    """The admitted scope after a published patch that changed
    ``changed_files``: (the scope, the paths that joined it, why an
    extension was not recorded). Only the files that no admitted path
    matches join it, as exact paths, once each and in order
    (``common.widen_scope``, with the matching rule of every scope check,
    ``common.scope_covers``). A record without an admitted scope keeps none:
    the payload then falls back to the contract, which a scope made only of
    the published files would narrow. An extension that would leave the
    scope invalid (more than ``common.SCOPE_PATHS_MAX`` paths, or an unsafe
    one) is not recorded and the scope is kept, with the reason; the
    publisher validated it before it pushed, so this is defensive, and the
    fold never fails over a patch that is already published. Never
    raises."""
    if not scope_paths:
        return scope_paths, [], None
    changed = [path for path in changed_files or [] if isinstance(path, str)]
    try:
        widened = widen_scope(scope_paths, changed)
    except ValueError as error:
        added = [path for path in dict.fromkeys(changed) if not scope_covers(path, scope_paths)]
        return scope_paths, [], f"the published files {added} did not join the scope: {error}"
    return widened, widened[len(scope_paths):], None


def fold_patch_result(rec: dict, outcome: dict, *, now: str, policy: dict,
                      worker_run_id: str | None, operations: dict[str, dict] | None = None,
                      evidence: dict[str, dict] | None = None
                      ) -> tuple[dict, list[tuple[str, dict]]]:
    """Fold the Result of patch Work through the rows of
    ``development.agent_running``, or of revision Work through the rows of
    ``upstream_review.agent_running``.

    In both states the success row is taken only for a confirmed
    publication: a published Result (its own branch operation confirmed),
    a publish-only retry that read the push of an earlier attempt back on the
    branch (it confirms the open operation of that attempt, found in
    ``operations`` by ``held_publication_operation``, and the row names it),
    and, in development, a retry rejected as stale while the operation of an
    earlier attempt of the held Result is confirmed. A Result whose push was
    not read back (``uncertain``) is a failed publication like an
    infrastructure error.

    In upstream review the rows are the same but three: a published Result
    takes ``record_revision_publish_success`` (which also clears exactly the
    carried feedback, records the head read back on `riscv64` in
    ``upstream_pr.head_sha``, and bumps the revision), a ``no_change`` report takes
    ``require_reviewer_answer`` (a human answers the reviewer; nothing is
    posted on the upstream PR), and a proposal rejected as stale takes
    ``reject_patch_proposal`` like any other rejection, as no row replans
    there. Upstream CI is never rerun.

    * published (confirmed, as above): ``record_publish_success`` (clears
      exactly the feedback the Work carried); the published files that no
      admitted path matches join ``scope_paths`` (``extended_scope``) beside
      the row, and the row starts ``no_progress`` again;
    * rejected in trusted validation, blocked, or a ``proposal`` (no
      publisher produces one any more): ``reject_patch_proposal``; a blocked
      Result's rehearsal evidence is recorded and named in the event;
    * a ``no_change`` report (the repair worker found a failure not caused
      by the patch): ``request_ci_rerun`` when the Work carries no feedback
      and ``flaky_reruns`` remains and the outcome carries the receipt of
      the trusted rerun (``ci_rerun.receipt_problem``), a failed publication
      when it carries none, ``reject_patch_proposal`` otherwise;
    * rejected as stale (the fork head moved): ``replan_development``,
      unless the Result is held and the operation of an earlier attempt is
      confirmed (the patch landed and the head moved after it): then
      ``record_publish_success``;
    * an infrastructure error of the publication, or an uncertain Result:
      ``schedule_publish_retry`` holds the same Result for
      a publish-only retry within ``publish_recovery``,
      ``exhaust_recovery`` once it is spent; an uncertain Result
      stores its report at the hold (``pushed_report_evidence``), as does a
      stale retry behind the operation of an earlier attempt.

    The Result of a publish-only retry ends the held publication: every row
    that folds it clears ``pending_publication``.
    A refused row raises ``TransitionError``.
    """
    rows = WORK_RESULT_STATES.get(engine.state_key(rec))
    if rows is None:
        raise lifecycle.TransitionError(
            f"the Result of patch or revision Work is folded in "
            f"{' or '.join(WORK_RESULT_STATES)} only, not in {engine.state_key(rec)}")
    status, fclass = outcome.get("status"), outcome.get("failure_class")
    if status == "proposal":
        status, fclass = "rejected", "validation"
    held = rec.get("pending_publication")
    run = str(worker_run_id or (held or {}).get("worker_run_id")
              or (outcome.get("artifact") or {}).get("run_id") or "0")
    cause = _result_cause(outcome, worker_run_id)
    # The revision of the Work the Result answers: the record's, but for the
    # publication of a held patch, whose review took revisions since.
    revision = (outcome.get("task") or {}).get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        revision = None
    staged: list[tuple[str, dict]] = []
    op = outcome.get("operation")
    if op:
        staged.append(("operations", op))
    earlier = None
    if held and not op and status in ("published", "rejected"):
        earlier = held_publication_operation(rec, outcome, operations)
    candidate = outcome.get("candidate")
    if earlier and status == "published" and isinstance(candidate, str) \
            and _SHA.fullmatch(candidate):
        # The publish-only retry read the push of an earlier attempt back on
        # the branch: the open operation of that attempt is confirmed, and
        # the row names it.
        if earlier["status"] != "confirmed":
            earlier = _confirm_read_back(earlier, candidate, outcome.get("attempt"), now=now)
            if earlier["status"] == "confirmed":
                staged.append(("operations", earlier))
        if earlier["status"] == "confirmed":
            op = earlier
    details: dict[str, Any] = {"result_status": status}
    if fclass:
        details["failure_class"] = fclass
    # Trusted branch evidence of a patch published anyway: a branch
    # operation this Result confirms (its own, or the one of an earlier
    # attempt it read back) whose commit is the commit a `publish_anyway`
    # review pinned. Its comment is due in this write, whichever row folds
    # the Result (`patch_review.landed_anyway`).
    landed = patch_review.landed_anyway(
        rec, patch_review.confirmed_heads(rec, [r for k, r in staged if k == "operations"]),
        evidence or {})
    if outcome.get("reason"):
        details["reason"] = str(outcome["reason"])[:500]
    failed = status in rows["failed"]
    if status == "verified" and "verified" in rows \
            and outcome.get("publication_series_digest") != outcome.get("patch_digest"):
        # The patch is held by the artifact that carries the verified
        # series, which the review reads by its run and id; an artifact
        # without it (a retry of older trusted code) is a failed
        # publication, verified again by the next publish-only retry.
        failed = True
        outcome = dict(outcome, reason="the publication artifact does not carry the verified "
                                       "series")
        details["reason"] = outcome["reason"]
    rerun_missing = None
    if status == "no_change" and "no_change" not in rows and not engine.row_admitted(
            rec, "request_ci_rerun", policy=policy, now=now,
            facts={"failure_not_caused_by_patch"}):
        # `request_ci_rerun` reruns the failed jobs: the trusted publication
        # job requests the rerun and reads the new attempt back before the
        # row is taken. A Result without that receipt (a publication job of
        # older trusted code, or one that died) is a failed publication,
        # so the publish-only retry performs the rerun.
        rerun_missing = ci_rerun.receipt_problem(outcome, rec)
        if rerun_missing:
            failed = True
            details["rerun"] = rerun_missing
    if failed and held and not rec.get("active_work") \
            and _charged_launch(held, outcome):
        # The failed publication of this launch is already charged, by the
        # wait for its Result: the late Result is recorded once, for audit,
        # and charges nothing again.
        late = {"result_status": status, "task_revision": rec["revision"],
                "task_generation": int(rec.get("generation", 0)),
                "task_work": (outcome.get("task") or {}).get("dedupe_key"),
                "why": "the failed publication of this publish-only retry was already "
                       "charged when its Result did not arrive within "
                       f"{PUBLISH_RESULT_WAIT_MINUTES} minutes"}
        # The push of an uncertain retry may have landed: its report is kept
        # all the same (`late_report_evidence`, which never fails the fold of
        # the late Result), as the hold of that retry would have kept it.
        reported = late_report_evidence(
            rec, dict(outcome, worker_run_id=run), now=now, policy=policy,
            known=evidence or {}, details=late) if status == "uncertain" else []
        due = _landed_due(rec, landed, late, now=now)
        if due is not None and due != (rec.get("review_comments") or []):
            rec = engine.record_beside(rec, "review_comment_due", now=now, review_comments=due)
        event = engine.record_late_effect(rec, policy=policy, now=now, cause_id=cause,
                                          details=late)
        return rec, staged + reported + [("contributions", rec), ("events", event)]
    landed_due = None
    scope = rec.get("scope_paths")
    if failed:
        landed_due = _landed_due(rec, landed, details, now=now)
        if status == "uncertain":
            # The push may have landed: the report is kept whatever ends
            # the hold (`pushed_report_evidence`).
            holds = "exhaust_recovery" if engine.row_admitted(
                rec, "schedule_publish_retry", policy=policy, now=now,
                facts={"publication_infrastructure_error"}) else "schedule_publish_retry"
            staged.extend(pushed_report_evidence(
                rec, outcome, holds, now=now, policy=policy, run=run, details=details,
                revision=revision))
        out = _fail_publication(rec, run, rerun_missing or str(outcome.get("reason") or ""),
                                now=now, policy=policy, cause=cause, details=details)
    else:
        if status in rows["success"]:
            verb = rows["published"]
            facts = {"result_published" if verb == "record_publish_success" else "revision_published"}
        elif status == "verified" and "verified" in rows:
            # The worker's trusted publication job verified the patch and
            # published nothing: it is held for its review.
            verb, facts = rows["verified"], {"patch_verified"}
        elif status == "rejected" and fclass == "stale" and "stale" in rows \
                and earlier and earlier["status"] == "confirmed":
            # The publication of the held Result landed (an earlier attempt's
            # operation is confirmed) and the head moved after it: the patch
            # is published, not replanned; the later head is recorded by
            # `record_head_change`.
            verb, facts, op = rows["published"], {"result_published"}, earlier
            details["confirmed_by"] = earlier["operation_id"]
        elif status == "rejected" and fclass == "stale" and "stale" in rows:
            verb, facts = rows["stale"], {"proposal_stale"}
            if earlier:
                # The operation of an earlier attempt is still open: its
                # push may have landed, so the report is kept with the
                # commit it proposed.
                staged.extend(pushed_report_evidence(
                    rec, outcome, verb, now=now, policy=policy, run=run, details=details,
                    operation=earlier, revision=revision))
        elif status == "no_change" and "no_change" in rows:
            verb, facts = rows["no_change"], {"no_change_needed"}
        elif status == "no_change":
            # The repair worker reports a failure not caused by the patch:
            # `request_ci_rerun` when the Work carries no feedback and
            # `flaky_reruns` remains (the outcome then carries the receipt of
            # the rerun), else the outcome counts as `reject_patch_proposal`,
            # as both rows state.
            facts = {"failure_not_caused_by_patch"}
            verb = ("reject_patch_proposal" if engine.row_admitted(
                rec, "request_ci_rerun", policy=policy, now=now, facts=facts)
                else "request_ci_rerun")
            if verb == "request_ci_rerun":
                rerun = outcome["rerun"]
                details["rerun"] = {
                    "status": rerun["status"],
                    "runs": [{k: int(r[k]) for k in ("run_id", "from_attempt", "run_attempt")}
                             for r in rerun["runs"][:ci_rerun.MAX_RUNS]],
                    "operation": (op or {}).get("operation_id")}
        elif status in ("rejected", "worker_blocked") and "rejected" in rows:
            # The held patch no longer rebuilds to the reviewed commit, or
            # the trusted validation rejects it: nothing is pushed.
            verb, facts = "reject_patch_proposal", {rows["rejected"]}
        elif status in ("rejected", "worker_blocked"):
            verb, facts = "reject_patch_proposal", {"proposal_rejected_or_blocked"}
            if status == "rejected" and fclass == "stale" and earlier:
                # A revision retry rejected as stale behind the operation of
                # an earlier attempt: that push landed or may have, so the
                # report is kept with the commit it proposed.
                staged.extend(pushed_report_evidence(
                    rec, outcome, verb, now=now, policy=policy, run=run, details=details,
                    operation=earlier, revision=revision))
        else:
            raise lifecycle.TransitionError(f"no row folds a patch Result with status {status!r}")
        if verb == "record_publish_success" and engine.state_key(rec) == "development.agent_running" \
                and not fork_publish.review_required(outcome.get("task") or {}):
            # Work dispatched before development patches were reviewed still
            # publishes its Result here; every other patch is held.
            facts = set(facts) | {"no_review_contract"}
        values: dict[str, Any] = {}
        if verb in ("record_publish_success", "record_revision_publish_success"):
            art = outcome.get("artifact") or {}
            artifact = rec.get("proposal_artifact")
            if all(k in art for k in _ARTIFACT_KEYS):
                artifact = {k: art[k] for k in _ARTIFACT_KEYS} | {
                    "result_commit": art.get("result_commit"), "outcome": "published"}
            values = {"proposal_artifact": artifact,
                      "publication_operation_id": (op or {}).get("operation_id")
                      or rec.get("publication_operation_id")}
        if verb == "record_publish_success" and engine.state_key(rec) == lifecycle.PUBLISHING:
            # A patch published after successive failed reviews: the latest
            # review is due on the development PR, at exactly the reviewed
            # commit the pin names, beside every comment still due, posted
            # by trusted code that the tick launches (`review_comments`)
            # until it is posted or found. The pending publication names the
            # review; the stored review that published this patch anyway
            # names it too (`patch_review.published_anyway`).
            values["review_comments"], dropped = patch_review.comment_due(
                rec, patch_review.pending_published_anyway(rec)
                + patch_review.published_anyway(rec, outcome, evidence or {}) + landed,
                now=now)
            _note_due(details, rec, values["review_comments"], dropped)
            landed = []
        if verb == "record_revision_publish_success":
            head = published_head(outcome)
            values["upstream_pr.head_sha"] = head
            details.update(head=head, previous_head=lifecycle.recorded_head(rec))
        if status == "worker_blocked":
            evidence = _rehearsal_evidence(rec, outcome, run, policy, now)
            staged.append(("evidence", evidence))
            details["evidence_id"] = evidence["evidence_id"]
        # The report of a successful worker is kept for later Work; any
        # other report is discarded. The report of a held patch is stored at
        # the hold with the commit it proposed, and again with the head its
        # publication read back.
        reports = report_evidence(rec, outcome, verb, now=now, policy=policy, run=run,
                                  details=details, operation=op, revision=revision,
                                  head_verb=("record_publish_success"
                                             if verb == "hold_patch_for_review" else None))
        staged.extend(reports)
        if verb == "hold_patch_for_review":
            try:
                values["held_patch"] = patch_review.held_patch(
                    rec, outcome, run=run, now=now,
                    report_evidence_id=reports[0][1]["evidence_id"] if reports else None)
            except ValueError as error:
                raise lifecycle.TransitionError(str(error)) from None
            details.update(candidate=values["held_patch"]["candidate"],
                           patch_digest=values["held_patch"]["patch_digest"])
        landed_due = _landed_due(rec, landed, details, now=now)
        if verb in ("record_publish_success", "record_revision_publish_success"):
            scope, added, problem = extended_scope(
                rec.get("scope_paths"), ((op or {}).get("proposed") or {}).get("changed_files"))
            if added:
                details["scope_extension"] = added
            elif problem:
                details["scope_extension_not_recorded"] = problem[:200]
        match engine.dispatch(engine.try_apply_transition(
                rec, verb, policy=policy, now=now, cause_id=cause, facts=facts, values=values,
                details=details), ("reconcile", "fold_patch_result")):
            case engine.Applied() as out:
                pass
            case engine.Skip(refused) if engine.state_key(rec) == lifecycle.PUBLISHING:
                # The first published patch: its development PR is opened
                # before CI is awaited, and the Result is folded then.
                raise ResultDeferred(
                    f"the Result is folded once the development PR is open: {refused.detail}",
                    [item for item in staged if item[0] == "operations"])
            case engine.Skip(refused):
                # Work dispatched before patches were reviewed published its
                # first patch from its own run; nothing opens its PR here.
                raise engine.TransitionRefused(refused, ("reconcile", "fold_patch_result"))
            case engine.LateEffect(refused):
                return late_fold(rec, outcome, refused, now=now, policy=policy,
                                 worker_run_id=worker_run_id, staged=staged)
            case engine.Bug(refused):
                raise engine.TransitionRefused(refused, ("reconcile", "fold_patch_result"))
    new = out.record
    if landed_due is not None and landed_due != (new.get("review_comments") or []):
        new = engine.record_beside(new, "review_comment_due", now=now,
                                   review_comments=landed_due)
    if out.verb in ("record_publish_success", "record_revision_publish_success"):
        # Beside the row: the published files that no admitted path matches
        # join the admitted scope (the extension the trusted publisher
        # admitted for a repair backed by review feedback, which may change
        # a path the plan missed). The row starts `no_progress` and
        # `dev_reviews` again.
        new = engine.record_beside(new, "patch_published", now=now, scope_paths=scope)
    schemas.validate("contribution", new)
    staged.extend([("contributions", new), ("events", out.event)])
    return new, staged


# The lists of a tick report that name what the tick asks the workflow to launch.
_EFFECT_LISTS = ("requested_effects", "dispatch_relaunch", "provision_requests",
                 "dev_pr_requests", "publication_retries", "handoff_requests",
                 "review_comment_requests")


def _touched(out: TickOutput, contribution_id: str) -> list[str]:
    """What ``out`` stages or requests for one contribution."""
    found = [f"staged {kind}" for kind, record in out.staged
             if record.get("contribution_id") == contribution_id]
    requests = [(name, item) for name in _EFFECT_LISTS for item in out.report.get(name) or []]
    found += [name for name, item in requests if item.get("contribution_id") == contribution_id]
    return found


def tick(api: collect.Api, snap: Snapshot, *, policy: dict, now: datetime,
         run_id: str | None = None, **inputs: Any) -> TickOutput:
    """One reconciliation tick, with each contribution isolated from the others.

    ``inputs`` are the collected inputs of ``_tick``. Staging is all or
    nothing per contribution. When the processing of one contribution fails,
    the whole tick is computed again from the committed state without that
    contribution: nothing is staged for it and no effect is requested for
    it, and it still counts as an existing contribution for the others.

    * A collector failure (``isolation.CollectorFailure``) skips the
      contribution for the tick: no state change, no pause, an entry in the
      report under ``skipped_contributions``.
    * An unexpected error (``isolation.ProcessingError``) in one contribution
      is followed by ``pause_on_processing_error`` on its committed record;
      the record and the event are the only things staged for it. The error
      is listed under ``alerts`` and ``processing_errors``. A contribution
      that is already stopped, or terminal, is not paused again and gets no
      event.
    * A pause requested by trusted code (``isolation.ContributionPause``,
      such as an accepted Result whose report cannot be stored) is handled
      like an unexpected error of its contribution, but never counts toward
      the limit below: any number of them leave the tick going.
    * An unexpected error in a contribution that is already paused (the
      controller's own stop, or a human's) does not count toward the limit
      either: it changes nothing, or only stops it again at the new version
      of the pipeline code.
    * Unexpected errors in two or more contributions, a pause that cannot be
      applied, and every exception outside a contribution stop the tick:
      ``isolation.TickFailure`` or the original exception is raised and
      nothing is returned to commit.
    """
    iso = now.isoformat()
    failures: dict[str, isolation.ContributionFailure] = {}
    while True:
        # Each attempt works on its own copy of the committed state, so a
        # failed attempt leaves nothing behind.
        work = copy.deepcopy(snap)
        try:
            out = _tick(api, work, policy=policy, now=now, skip=frozenset(failures), **inputs)
            check_staged_operations(out.staged, snap.operations)
            break
        except isolation.ContributionFailure as failure:
            if failure.contribution_id in failures:
                raise isolation.TickFailure(
                    f"{failure.contribution_id} was processed although it is skipped: {failure}"
                ) from failure
            failures[failure.contribution_id] = failure
            # A pause requested by trusted code (``ContributionPause``) is
            # specific to its contribution and never stops the tick.
            # A contribution that is already stopped does not count either:
            # its failure changes nothing, or only re-stops it at the new
            # version of the pipeline code.
            errors = [f for f in failures.values() if isinstance(f, isolation.ProcessingError)
                      and not isinstance(f, isolation.ContributionPause)
                      and not _already_stopped(snap, f.contribution_id)]
            if len(errors) > 1:
                raise isolation.TickFailure(
                    "processing failed for more than one contribution in one tick; the tick "
                    "stops, nothing is committed, and no contribution is paused: "
                    + "; ".join(f"{d['contribution']} ({d['step']}: {d['error_class']}: "
                                f"{d['message']})"
                                for d in (isolation.describe(f, run_id) for f in errors)),
                    tuple(errors)) from failure
    # The caller's snapshot follows the attempt that succeeded.
    for name in ("operations", "consumed", "projects"):
        target, source = getattr(snap, name), getattr(work, name)
        target.clear()
        target.update(source)

    out.failures = list(failures.values())
    # Both lists are in the report only when the tick did not process a
    # contribution.
    skipped: list[dict] = []
    errored: list[dict] = []
    alerts = out.report["controller"]["alerts"]
    committed = {c["contribution_id"]: c for c in snap.contributions.values()}
    for cid, failure in failures.items():
        touched = _touched(out, cid)
        if touched:
            raise isolation.TickFailure(
                f"{cid} failed in this tick but the tick still holds {', '.join(touched)} for it")
        description = isolation.describe(failure, run_id)
        if isinstance(failure, isolation.CollectorFailure):
            skipped.append(
                description | {"decision": "skipped for this tick: no state change, no pause"})
            continue
        record = committed.get(cid)
        outcome = None
        if record is not None:
            try:
                # A stop of the controller from an earlier version of the
                # pipeline code, resumed in this tick and failing again, is
                # stopped again at the version that ran.
                # An automatic resume that itself fails is left out: the
                # stop stays as it was and is not stopped again at this
                # version. The failure is in the decision and the alert, and
                # the version that attempted it is recorded on the stop
                # (``restop_attempted``), so the resume is attempted once per
                # version of the pipeline code, not on every tick.
                resume_failure = None
                try:
                    restops = _restop_steps(record, policy=policy, now=iso,
                                            version=inputs.get("pipeline"))
                except Exception as resume_error:
                    restops = []
                    resume_failure = (f"{type(resume_error).__name__}: "
                                      f"{isolation.clean_text(resume_error)}")[:500]
                    attempted = _restop_attempted(record, now=iso, version=inputs.get("pipeline"))
                    if attempted is not None:
                        out.staged.append(("contributions", attempted))
                        record = attempted
                for step in restops:
                    out.staged.extend([("contributions", step.record), ("events", step.event)])
                    record = step.record
                outcome = pause_on_processing_error(record, description, policy=policy, now=iso,
                                                    version=inputs.get("pipeline"))
            except Exception as error:
                raise isolation.TickFailure(
                    f"pause_on_processing_error could not be applied to {cid} after "
                    f"{description['error_class']} in {description['step']}: "
                    f"{isolation.clean_text(error)}") from error
        decision = "not paused: no committed record"
        if outcome is not None:
            # The last resort: no refusal of the pause raises.
            match engine.dispatch(outcome, ("reconcile", "tick"), PROCESSING_ERROR_VERB):
                case engine.Applied() as paused:
                    out.staged.extend([("contributions", paused.record),
                                       ("events", paused.event)])
                    decision = "paused by the controller"
                case engine.Skip(refused):
                    decision = f"not paused again: {refused.detail}"
            if resume_failure:
                decision += f"; the automatic resume of the earlier stop failed: {resume_failure}"
        errored.append(description | {"decision": decision})
        alerts.append({"contribution": cid,
                       "alert": (f"processing error in {description['step']}: "
                                 f"{description['error_class']}: {description['message']}; "
                                 f"everything staged for it was discarded; {decision}")})
    if skipped:
        out.report["skipped_contributions"] = skipped
    if errored:
        out.report["processing_errors"] = errored
    return out


def check_staged_operations(staged: list[tuple[str, dict]], committed: dict[str, dict]) -> None:
    """Refuse, as a processing error of its contribution, an operation the
    tick stages over a committed one along no sequence of rows of the
    operation table (``recovery.transition_problem``), before the state
    publisher refuses the whole commit for it. The publisher keeps the same
    check as the final guard."""
    for kind, op in staged:
        if kind != "operations":
            continue
        problem = recovery.transition_problem(committed.get(op["operation_id"]), op)
        if problem:
            raise isolation.ProcessingError(
                op["contribution_id"], "operation_check",
                recovery.RecoveryError(f"{op['operation_id']}: {problem}"))


PROCESSING_ERROR_VERB = "pause_on_processing_error"


def _already_stopped(snap: Snapshot, cid: str) -> bool:
    """Whether the committed record of ``cid`` is paused: a failure in it
    stops nothing more and never stops the tick."""
    return any(c["contribution_id"] == cid and c.get("paused")
               for c in snap.contributions.values())


def _restop_steps(record: dict, *, policy: dict, now: str,
                  version: str | None) -> list[engine.Applied]:
    """The rows that stop again, at the version of the pipeline code that
    ran, a record the controller paused for an error at an earlier version:
    ``resume_pause`` (the tick variant), whose record the stop then takes
    over. Empty when the record is not such a stop or ran at that version, so
    the stop stays as it is."""
    if version is None or record.get("paused_at_commit") == version:
        return []
    resumed = _resume_after_error(record, policy=policy, now=now, version=version)
    return [resumed] if resumed is not None else []


def _restop_attempted(record: dict, *, now: str, version: str | None) -> dict | None:
    """The record of a controller stop whose automatic resume failed at
    ``version``, with that version as `paused_at_commit` (the write beside a
    row ``restop_attempted``, no row of the table): the resume is attempted
    again only at another version of the pipeline code. ``None`` when the
    stop is not one the controller resumes or the version is unknown."""
    if version is None or not engine.paused_for_error(record) \
            or record.get("paused_at_commit") == version:
        return None
    return engine.record_beside(record, "restop_attempted", now=now, paused_at_commit=version)


def pause_on_processing_error(record: dict, description: dict, *, policy: dict,
                              now: str, version: str | None = None
                              ) -> engine.Applied | engine.Refused:
    """Take the table's ``pause_on_processing_error`` on the committed record.

    It sets ``paused`` with the error as ``paused_reason``, ``paused_by``
    ``controller`` and ``paused_at``, and clears the requested Work and the
    pending publication; from an ``agent_running`` state
    it leads to the state in which the Work is due. ``paused_at_commit``
    records ``version``, the pipeline code that ran, so the controller
    resumes the stop once that code changed. The row is refused,
    and nothing changes, when the contribution is already paused or
    terminal; its caller handles every refusal (``engine.HANDLING``).
    """
    reason = isolation.pause_reason(description)
    details = {k: description[k] for k in ("step", "error_class", "message", "run_id")}
    return engine.try_apply_transition(record, PROCESSING_ERROR_VERB, policy=policy, now=now,
                                       cause_id=description["run_id"],
                                       facts={"processing_error"},
                                       values={"paused_reason": reason,
                                               "paused_at_commit": version},
                                       details=details)


def fold_ci_rerun_receipts(api: collect.Api, receipts: list[dict], contributions: dict[str, dict],
                           by_cid: dict[str, str], snap: Snapshot, out: TickOutput, *,
                           now: str, skip: frozenset[str]) -> list[dict]:
    """Fold the receipts of the ``ci_rerun`` operations the collector planned
    for interrupted CI, which the development PR workflow executes in
    development and the handoff workflow in ``handoff.waiting_ci``.

    A receipt is folded only for the open operation the record names as
    active, at the record's revision. A ``rerun`` receipt is read back with
    the collector's own reads: when every run of the operation shows a
    higher attempt on the expected head (``ci_rerun.discover``), the
    operation is confirmed and released (``ci_rerun_recorded``); otherwise
    nothing is decided in this tick, and the receipt is read again in the
    next one. A ``stale`` or ``refused`` receipt closes the operation as
    failed and releases it. No row of the table is fired: the next
    observation of interrupted CI takes ``rerun_interrupted_ci`` again, or
    its exhaustion row."""
    report = []
    for receipt in receipts:
        cid = receipt.get("contribution_id")
        if cid not in by_cid or cid in skip:
            continue
        with isolation.contribution(cid, "ci_rerun_receipt"):
            key = by_cid[cid]
            c = contributions[key]
            op = snap.operations.get(receipt.get("operation_id"))
            if not op or op["kind"] != "ci_rerun" or op["status"] not in recovery.OPEN \
                    or c.get("active_operation_id") != op["operation_id"] \
                    or c["revision"] != receipt.get("revision") \
                    or receipt.get("repository") != op["target"]["repository"] \
                    or receipt.get("head_sha") != op["target"]["expected_head"]:
                continue
            entry = {"contribution": cid, "operation": op["operation_id"]}
            status = receipt.get("status")
            run_id = str(receipt.get("ci_rerun_run_id") or "")[:40]
            if status in ("stale", "refused"):
                if op["status"] not in ("planned", "applying"):
                    report.append(dict(entry, outcome="failure not folded"))
                    continue
                new_op = recovery.fold_failure_receipt(
                    op, external_ids={"ci_rerun_run_id": run_id}, now=now)
                new = recovery.release(c, new_op, now)
                report.append(dict(entry, outcome="failed", receipt=status))
            elif status == "rerun":
                try:
                    runs = [ci_rerun.read_run(api, op["target"]["repository"], r["run_id"])
                            for r in ci_rerun.marker_runs(op)]
                except collect.CollectError:
                    # A run that cannot be read (deleted in the fork) is not
                    # a collector failure for the contribution: the receipt
                    # stays unread, and the launch budget closes the operation.
                    runs = None
                if runs is None or ci_rerun.discover(op, runs) != "confirmed":
                    report.append(dict(entry, outcome="new attempt not read back yet"))
                    continue
                new_op = recovery.fold_receipt(
                    op, external_ids=ci_rerun.attempt_ids(
                        [{"run_id": r["id"], "run_attempt": r["run_attempt"]} for r in runs])
                    | {"ci_rerun_run_id": run_id},
                    detail=f"new attempts read back on {op['target']['expected_head']}", now=now)
                new = engine.record_beside(c, "ci_rerun_recorded", now=now,
                                           active_operation_id=None)
                out.staged.append(("events", lifecycle._event(
                    c, new, "ci_rerun_recorded", "observation", op["operation_id"], now, False)))
                report.append(dict(entry, outcome="confirmed",
                                   runs=[{"run_id": r["id"], "run_attempt": r["run_attempt"]}
                                         for r in runs]))
            else:
                report.append(dict(entry, outcome=f"not folded: {str(status)[:40]}"))
                continue
            contributions[key] = new
            snap.operations[op["operation_id"]] = new_op
            out.staged.extend([("operations", new_op), ("contributions", new)])
    return report


def fold_handoff_receipts(api: collect.Api, receipts: list[dict], contributions: dict[str, dict],
                          by_cid: dict[str, str], snap: Snapshot, out: TickOutput, *,
                          policy: dict, now: str, skip: frozenset[str]) -> list[dict]:
    """Fold the verified receipts of the handoff operations.

    A receipt is folded only for the open operation the record runs in the
    state of its kind. A confirmed receipt is read back independently
    (`riscv64` at the reviewed head, tree, and series, or the App-authored
    package comment); the operation then takes `begin_effect`,
    `record_response`, and `confirm_by_discovery` and fires the confirm row.
    A failed receipt takes `record_failure` and fires the retry row within the
    budget of the state, the exhaustion row beyond it, or, for a build of
    `riscv64` whose reviewed series does not rebuild or whose lease
    conflicts, `return_for_repair`, and for one that found an upstream PR open
    from `riscv64`, `require_branch_intervention`. A package attempt that
    found `riscv64` off the clean head is read back first: when the branch is
    read moved, deleted, or renamed (under the recorded fork id), it fires
    `return_for_repair`; when it is read at the clean head or cannot be read,
    the failure is folded as an infrastructure failure. A receipt of an
    attempt the workflow refused before any effect is not folded."""
    report = []
    for receipt in receipts:
        cid = receipt.get("contribution_id")
        if cid not in by_cid or cid in skip:
            continue
        with isolation.contribution(cid, "handoff_receipt"):
            key = by_cid[cid]
            c = contributions[key]
            op = snap.operations.get(receipt.get("operation_id"))
            if not op or handoff.receipt_problem(c, op, receipt):
                continue
            kind, result = op["kind"], receipt.get("result")
            run = {"handoff_run_id": str(receipt.get("run_id") or "")}
            entry = {"contribution": cid, "operation": op["operation_id"], "kind": kind}
            if result == "applied":
                checked = handoff.readback(api, c, op, receipt, snap.evidence)
                if isinstance(checked, str):
                    report.append(dict(entry, outcome="readback mismatch", detail=checked))
                    continue
                external_ids, values, detail = checked
                new_op = recovery.fold_receipt(op, external_ids=external_ids, detail=detail, now=now)
                details = {"attempt": int(handoff.reason_value(op, "attempt") or 0)}
                if kind == "upstream_branch":
                    replaced = receipt.get("replaced")
                    details.update(head=values["clean_head_sha"],
                                   written=bool(receipt.get("written")),
                                   replaced=replaced if isinstance(replaced, str)
                                   and len(replaced) == 40 else None)
                else:
                    values = {"handoff_request": {"command_id": op["operation_id"],
                                                  "head": c["clean_head_sha"],
                                                  "revision": c["revision"]}}
                    details.update(package_url=external_ids["comment_url"],
                                   head=c["clean_head_sha"])
                outcome = recovery.fire(c, new_op, policy=policy, now=now,
                                        facts={handoff.CONFIRM_FACTS[kind]}, values=values,
                                        counts={}, details=details)
                if outcome is None:
                    raise lifecycle.TransitionError(
                        f"{kind} receipt fires no row in {engine.state_key(c)}: none bound")
                fired = engine.applied(outcome, ("reconcile", "fold_handoff_receipts"))
            elif result == "failed":
                failure = str(receipt.get("failure") or "unknown")
                reason = str(receipt.get("reason") or "")
                details = None
                if (kind, failure) == ("submission_comment", handoff.MOVED):
                    observed = handoff.moved_readback(api, c)
                    if observed is None:
                        failure = handoff.INFRASTRUCTURE
                        reason = f"{UPSTREAM_REF} is not read back away from the clean head; " + reason
                    else:
                        details = {"head": observed, "clean_head": c["clean_head_sha"],
                                   "kept_evidence_id": c.get("last_ci_evidence_id")}
                new_op, fired = handoff.fail_attempt(
                    c, op, policy=policy, now=now, failure=failure,
                    reason=reason, external_ids=run, details=details)
            else:
                report.append(dict(entry, outcome=f"not folded: {str(result)[:40]}",
                                   detail=isolation.clean_text(receipt.get("reason") or "", 300)))
                continue
            contributions[key] = fired.record
            snap.operations[op["operation_id"]] = new_op
            out.staged.extend([("operations", new_op), ("contributions", fired.record),
                               ("events", fired.event)])
            report.append(dict(entry, outcome=new_op["status"], verb=fired.verb))
    return report


# The statuses of a provisioning receipt the reconciler folds. A `refused`
# receipt (the workflow's final check did not pass, nothing was attempted)
# and any other status are reported and not folded.
PROVISIONING_FOLDED = ("provisioned", "failed")


def _provision_run(receipt: dict) -> int:
    run = str(receipt.get("provision_run_id") or "")
    return int(run) if run.isdecimal() else -1


def fold_provisioning_receipts(api: collect.Api, receipts: list[dict], contributions: dict[str, dict],
                               by_cid: dict[str, str], snap: Snapshot, out: TickOutput, *,
                               policy: dict, now: str, skip: frozenset[str]) -> list[dict]:
    """Fold the verified receipts of the fork provisioning operations.

    A receipt is folded only for the open `fork_provision` operation the
    record names at the record's revision, and of several foldable receipts
    for one operation only the newest run's. `provisioned` is read back (the
    fork's identity and network, `riscv64-dev` at the expected head) and
    fires `confirm_provisioning`. `failed` is a definite failure reported by
    the trusted workflow: the operation takes `record_failure` and, from
    `provisioning.idle`, fires `require_provisioning_intervention` with
    `provisioning_operation_failed`; in the provisioning wait the state is
    kept. The closed failure code is stored on the operation; the receipt's
    free-text reason reaches only this report."""
    report = []
    newest: dict[str, dict] = {}
    for receipt in receipts:
        if receipt.get("status") in PROVISIONING_FOLDED:
            key = str(receipt.get("operation_id"))
            if key not in newest or _provision_run(receipt) > _provision_run(newest[key]):
                newest[key] = receipt
    for receipt in receipts:
        cid = receipt.get("contribution_id")
        if cid not in by_cid or cid in skip:
            continue
        key = by_cid[cid]
        with isolation.contribution(cid, "provisioning_receipt"):
            c = contributions[key]
            op = snap.operations.get(receipt.get("operation_id"))
            if not op or op["kind"] != "fork_provision" or op["status"] not in recovery.OPEN \
                    or c["phase"] != "provisioning" or c.get("active_operation_id") != op["operation_id"] \
                    or c["revision"] != receipt.get("revision") or not isinstance(receipt.get("status"), str):
                continue
            status = receipt["status"]
            if status not in PROVISIONING_FOLDED:
                entry = {"contribution": cid, "status": f"not folded: {status[:40]}"}
                if status == "refused":
                    entry["detail"] = isolation.clean_text(receipt.get("reason") or "", 300)
                report.append(entry)
                continue
            if newest.get(op["operation_id"]) is not receipt:
                report.append({"contribution": cid, "status": "superseded"})
                continue
            external_ids = {}
            if receipt.get("provision_run_id"):
                external_ids["provision_run_id"] = str(receipt["provision_run_id"])
            if receipt.get("provision_artifact_id"):
                external_ids["provision_artifact_id"] = int(receipt["provision_artifact_id"])
            if status == "failed":
                # The trusted workflow reports a definite failure: the
                # operation takes `record_failure` and, from
                # `provisioning.idle`, fires `require_provisioning_intervention`;
                # in the provisioning wait the state is kept.
                fired = None
                if op["status"] not in ("planned", "applying"):
                    # Only a launched operation fails by a receipt.
                    report.append({"contribution": cid, "status": "failure not folded"})
                    continue
                code = receipt.get("failure") if receipt.get("failure") in provisioning.FAILURES \
                    else "other"
                stage = receipt.get("stage") if receipt.get("stage") in provisioning.STAGES else None
                external_ids["provision_failure"] = code
                new_op = recovery.fold_failure_receipt(op, external_ids=external_ids, now=now)
                if recovery.fired_verbs(c, new_op):
                    facts, values = controller.RESOLUTION_FIRES[(new_op["kind"], new_op["status"])]
                    fired = engine.applied(recovery.fire(
                        c, new_op, policy=policy, now=now, facts=facts, values=values,
                        details={"receipt_status": "failed", "failure": code, "stage": stage}),
                        ("reconcile", "fold_provisioning_receipts"),
                        recovery.fired_group(c, new_op))
                    new = fired.record
                else:
                    new = recovery.release(c, new_op, now)
                schemas.validate("contribution", new)
                contributions[key] = new
                snap.operations[op["operation_id"]] = new_op
                out.staged.extend([("operations", new_op), ("contributions", new)])
                if fired is not None:
                    out.staged.append(("events", fired.event))
                report.append({"contribution": cid, "status": "failed", "failure": code,
                               "detail": isolation.clean_text(receipt.get("reason") or "", 300)})
                continue
            if receipt.get("fork") != op["target"]["repository"] \
                    or receipt.get("upstream_id") != c["upstream"]["repository_id"] \
                    or receipt.get("base_sha") != c["upstream"]["base_sha"] \
                    or receipt.get("actions_read_only") is not True:
                report.append({"contribution": cid, "status": "receipt mismatch"})
                continue
            repo = collect.repository(api, receipt["fork"])
            head = collect.branch_head(api, receipt["fork"], DEV_REF)
            expected = op["target"].get("expected_head") or op["target"]["base_sha"]
            if not head or head["head"] != receipt.get("head_sha") or head["head"] != expected \
                    or repo["id"] != receipt.get("fork_id") or repo.get("parent_id") != c["upstream"]["repository_id"]:
                report.append({"contribution": cid, "status": "fork identity/ref mismatch"})
                continue
            new_op = recovery.fold_receipt(
                op, external_ids=external_ids,
                detail="fork/source identity, branch and trusted setup receipt verified", now=now)
            fork = {"repository_id": repo["id"], "name": repo["full_name"], "head_ref": DEV_REF,
                    "head_sha": head["head"], "pr_number": None}
            if recovery.fired_verbs(c, new_op):
                # The receipt matches the operation and the fork and ref were
                # read back: the operation fires `confirm_provisioning`.
                fired = engine.applied(recovery.fire(
                    c, new_op, policy=policy, now=now,
                    facts={"receipt_matches_operation", "fork_and_ref_read_back"},
                    values={"fork": fork,
                            "upstream.repository_id": c["upstream"]["repository_id"],
                            "upstream.base_ref": c["upstream"]["base_ref"],
                            "upstream.base_sha": c["upstream"]["base_sha"]}),
                    ("reconcile", "fold_provisioning_receipts"), recovery.fired_group(c, new_op))
                new, event = fired.record, fired.event
            else:
                # The provisioning wait has no row for the operation: the
                # state is kept and the closed operation is released.
                new, event = recovery.release(c, new_op, now), None
            schemas.validate("contribution", new)
            contributions[key] = new
            snap.operations[op["operation_id"]] = new_op
            out.staged.extend([("operations", new_op), ("contributions", new)])
            if event is not None:
                out.staged.append(("events", event))
            report.append({"contribution": cid, "status": "provisioned"})
    return report


# The phases in which a record holds the fork it develops on.
_FORK_PHASES = ("development", "handoff", "upstream_review", "release_wait")


def pipeline_version(code_root: Path | None = None) -> str | None:
    """The version of the pipeline code that runs: the SHA-256 of the
    top-level git tree of ``HEAD`` without `.state`, so the state commit of
    every tick does not change it and a shallow checkout reads it as well.
    ``None`` when git cannot read it; then nothing is resumed for a code
    change."""
    root = code_root or Path(__file__).resolve().parent
    try:
        listing = subprocess.run(["git", "-C", str(root), "ls-tree", "HEAD"],
                                 capture_output=True, check=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line for line in listing.splitlines() if line.partition("\t")[2] != ".state"]
    if not lines:
        return None
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _resume_after_error(record: dict, *, policy: dict, now: str,
                        version: str | None) -> engine.Applied | None:
    """The controller's resume of its own stop after a processing error
    (``resume_pause`` taken by the tick): once the pipeline code changed
    since the pause (``paused_at_commit`` differs from ``version``, or the
    pause recorded none), while no operation is open and the stop is not
    stale. ``None`` when the row is not taken; a retry that fails again
    pauses at the new version."""
    if not engine.paused_for_error(record):
        return None
    changed = version is not None and record.get("paused_at_commit") != version
    match engine.dispatch(engine.try_apply_transition(
            record, "resume_pause", policy=policy, now=now,
            cause_id=f"pipeline:{(version or 'unknown')[:12]}",
            facts={"pipeline_changed_since_pause"} if changed else set(),
            details={"automatic": True, "paused_at_commit": record.get("paused_at_commit"),
                     "pipeline_version": version,
                     "paused_reason": str(record.get("paused_reason") or "")[:500]}),
            ("reconcile", "_resume_after_error")):
        case engine.Applied() as out:
            return out
        case engine.Skip(_refused):
            return None
        case engine.Bug(refused):
            raise engine.TransitionRefused(refused, ("reconcile", "_resume_after_error"))


# The row a loaded record that breaks an invariant of its state takes when
# the invariant names none, or none leaves its state.
DEFAULT_HEAL = "pause_on_processing_error"


def heal_verb(record: dict, name: str) -> str:
    """The row that heals the invariant ``name`` the record breaks when the
    controller plans nothing that establishes it: the last of the
    invariant's (``Invariant.heal``) when its state has that row, else the
    controller's stop (``DEFAULT_HEAL``)."""
    heal = lifecycle.INVARIANTS[name].heal
    state = engine.state_key(record)
    if heal and any(e.verb == heal[-1] for e in lifecycle.state_edges(state)):
        return heal[-1]
    return DEFAULT_HEAL


def _heal_invariant(record: dict, name: str, why: str, *, policy: dict, now: str,
                    version: str | None, operations: dict | None = None) -> engine.Applied | str:
    """Take the row that heals a loaded record that breaks the invariant
    ``name`` of its state, never an edit by hand: the invariant's row, or
    the controller's stop with the reason `invariant_violation: <state>
    requires <name>`. A record that waits for CI on a published head without
    its development PR takes no row here: the controller plans the PR of
    that head and launches it in this tick (`launch_development_pr`), and the
    verified receipt records it (`confirm_development_pr`); only when no
    such head exists does it wait for a human (`record_pr_mismatch`). Why
    not, when no row is taken: ``HEALING`` for the planned PR, a paused
    record is healed by the first tick after the resume, and a stopped or
    ended one is not stopped again."""
    state = engine.state_key(record)
    reason = f"invariant_violation: {state} requires {name}: {why}"[:2000]
    if name == "dev_pr" and not record.get("paused"):
        head = controller.published_head_without_pr(record, operations or {}, None)
        if head:
            return f"{HEALING}: the controller opens the development PR of {head} when it plans it"
    verb = heal_verb(record, name)
    if verb == DEFAULT_HEAL:
        outcome = engine.try_apply_transition(
            record, DEFAULT_HEAL, policy=policy, now=now, cause_id=f"invariant:{name}",
            facts={"processing_error"},
            values={"paused_reason": reason, "paused_at_commit": version},
            details={"invariant": name, "why": why[:500]})
    else:
        outcome = engine.try_apply_transition(
            record, verb, policy=policy, now=now, cause_id=f"invariant:{name}",
            facts=HEAL_FACTS[verb], details={"invariant": name, "mismatch": reason[:500]})
    match engine.dispatch(outcome, ("reconcile", "_heal_invariant"), verb):
        case engine.Applied() as out:
            return out
        case engine.Skip(refused):
            return f"{verb} is refused ({refused.kind}): {refused.detail}"
        case engine.Bug(refused):
            raise engine.TransitionRefused(refused, ("reconcile", "_heal_invariant"))


# The prefix of what `_heal_invariant` returns for a record that the
# controller heals by planning an operation, with no row taken in the load.
HEALING = "healing"

# What a heal row establishes about the record it heals: a record that names
# no development PR cannot match one on GitHub.
HEAL_FACTS: dict[str, set[str]] = {
    "record_pr_mismatch": {"development_pr_mismatch"},
}


def development_branch_problem(record: dict) -> str | None:
    """Why a record names a development branch other than `riscv64-dev`, or
    ``None``."""
    fork = record.get("fork")
    if record.get("phase") not in _FORK_PHASES or not isinstance(fork, dict):
        return None
    if fork.get("head_ref") != DEV_REF:
        return (f"the record names the development branch {str(fork.get('head_ref'))[:60]!r}, "
                f"not {DEV_REF!r}; no row of the table handles it")
    return None


def _tick(api: collect.Api, snap: Snapshot, *, policy: dict, now: datetime,
         publications: list[dict], pressure: capacity.Pressure,
         issues: list[dict] | None = None, comments: list[dict] | None = None,
         acceptances: dict[int, int] | None = None,
         observe: bool = True, dispatcher_runs: list[dict] | None = None,
         queue_works: dict[str, str] | None = None, tracking_issues: dict | None = None,
         queue_retries: dict[str, dict] | None = None,
         work_results: dict[str, dict] | None = None,
         upstream_snapshots: dict | None = None, provisions: list[dict] | None = None,
         development_prs: list[dict] | None = None, handoffs: list[dict] | None = None,
         ci_reruns: list[dict] | None = None,
         handoff_runs: dict[str, int] | None = None,
         development_pr_runs: dict[str, int] | None = None,
         provision_runs: dict[str, int] | None = None,
         review_comment_receipts: list[dict] | None = None,
         skip: frozenset[str] = frozenset(), pypi=None,
         queue_policy: dict | None = None, pipeline: str | None = None) -> TickOutput:
    """One attempt at the tick. ``skip`` names the contributions it does not
    process. Every step that works on one contribution runs in an
    ``isolation.contribution`` block; an exception that leaves such a block
    reaches ``tick`` attributed to that contribution. ``pypi`` reads PyPI
    for the confirmation of a decision close (``collect.pypi_project`` by
    default)."""
    out = TickOutput()
    iso = now.isoformat()
    contributions = dict(snap.contributions)
    by_cid = {c["contribution_id"]: k for k, c in contributions.items()}

    # Development happens on `riscv64-dev`. A record that names another
    # development branch is a shape no row of the table handles: it is an
    # unexpected processing error of that contribution, never acted on.
    for c in contributions.values():
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "record_shape"):
            problem = development_branch_problem(c)
            if problem:
                raise lifecycle.TransitionError(problem)

    # The controller resumes its own stop after a processing error once the
    # pipeline code changed, and a record that breaks an invariant of its
    # state (soft at load) takes the row that heals it; both before anything
    # else reads the record.
    resumed, violations, load_alerts = [], [], []
    for key, c in list(contributions.items()):
        cid = c["contribution_id"]
        if cid in skip:
            continue
        with isolation.contribution(cid, "resume_after_error"):
            out_resume = _resume_after_error(c, policy=policy, now=iso, version=pipeline)
            if out_resume is not None:
                c = contributions[key] = out_resume.record
                out.staged.extend([("contributions", c), ("events", out_resume.event)])
                resumed.append({"contribution": cid, "revision": c["revision"]})
        with isolation.contribution(cid, "invariants"):
            for name, why in engine.invariant_problems(c)[:1]:
                healed = _heal_invariant(c, name, why, policy=policy, now=iso, version=pipeline,
                                         operations=snap.operations)
                applied = isinstance(healed, engine.Applied)
                planned = not applied and healed.startswith(HEALING)
                entry = {"contribution": cid, "state": engine.state_key(c), "invariant": name,
                         "why": why, "heal": healed.verb if applied else
                         "launch_development_pr" if planned else None}
                violations.append(entry)
                # The alert is made once: not while the operation that heals
                # it is in flight, and not for a paused record that waits for CI
                # without a PR (the controller reports the wait as deferred,
                # and the heal happens after the resume).
                open_op = recovery.is_open((snap.operations or {}).get(c.get("active_operation_id")))
                quiet = (c.get("paused") and name == "dev_pr") or (planned and open_op)
                if applied or not quiet:
                    load_alerts.append({
                        "contribution": cid,
                        "alert": (f"invariant violation: {entry['state']} requires {name}: "
                                  f"{why}; "
                                  + (f"healed by {healed.verb}" if applied else
                                     healed if planned else
                                     f"not healed in this tick: {healed}"))})
                if applied:
                    c = contributions[key] = healed.record
                    out.staged.extend([("contributions", c), ("events", healed.event)])
    # Both lists are in the report only when the tick found something.
    if resumed:
        out.report["resumed_after_error"] = resumed
    if violations:
        out.report["invariant_violations"] = violations

    # Trusted same-fork development PR receipts are independently read back.
    dev_pr_report = []
    for receipt in development_prs or []:
        cid = receipt.get("contribution_id")
        if cid not in by_cid:
            continue
        if cid in skip:
            continue
        with isolation.contribution(cid, "development_pr_receipt"):
            key = by_cid[cid]
            c = contributions[key]
            op = snap.operations.get(receipt.get("operation_id"))
            if not op or op["kind"] not in ("dev_pr_create", "dev_pr_update") or op["status"] not in recovery.OPEN \
                    or c["phase"] != "development" or c.get("active_operation_id") != op["operation_id"] \
                    or c["revision"] != receipt.get("revision") or receipt.get("status") != "published":
                continue
            if receipt.get("repository") != op["target"]["repository"] \
                    or receipt.get("repository_id") != op["target"]["repository_id"] \
                    or receipt.get("head_sha") != op["target"]["expected_head"]:
                dev_pr_report.append({"contribution": cid, "status": "receipt mismatch"})
                continue
            try:
                pr = api.get(f"/repos/{receipt['repository']}/pulls/{int(receipt['pr_number'])}")
            except (ValueError, KeyError):
                dev_pr_report.append({"contribution": cid, "status": "readback unavailable"})
                continue
            # The PR carries this contribution's marker, of any revision or
            # head: a create adopts a PR that an earlier launch left.
            body_ok = dev_pr.marker_prefix(cid) in (pr.get("body") or "")
            if pr.get("id") != receipt.get("pr_id") or pr.get("state") != "open" \
                    or not body_ok \
                    or (pr.get("head") or {}).get("sha") != op["target"]["expected_head"] \
                    or (pr.get("head") or {}).get("ref") != DEV_REF \
                    or (pr.get("head") or {}).get("repo", {}).get("id") != op["target"]["repository_id"] \
                    or (pr.get("base") or {}).get("ref") != c["upstream"]["base_ref"]:
                dev_pr_report.append({"contribution": cid, "status": "PR readback mismatch"})
                continue
            new_op = recovery.fold_receipt(
                op, external_ids={"pr_id": int(pr["id"]), "pr_number": int(pr["number"]),
                                  "development_pr_run_id": str(receipt["development_pr_run_id"])},
                detail=f"development PR {pr['number']} verified", now=iso)
            snap.operations[op["operation_id"]] = new_op
            out.staged.append(("operations", new_op))
            if not recovery.fired_verbs(c, new_op):
                # A receipt that arrives in a state without a row for the
                # operation closes it and keeps the state; the PR is not
                # recorded, and the next create adopts it.
                new = recovery.release(c, new_op, iso)
                if new is not c:
                    contributions[key] = new
                    out.staged.append(("contributions", new))
                dev_pr_report.append({"contribution": cid,
                                      "status": "closed in a state without its row",
                                      "pr_number": pr["number"]})
                continue
            outcome = recovery.fire(c, new_op, policy=policy, now=iso,
                                    facts={"receipt_matches_operation", "development_pr_read_back"},
                                    values={"fork.pr_number": int(pr["number"])},
                                    details={"pr_number": int(pr["number"]), "kind": op["kind"]})
            if outcome is None:
                raise lifecycle.TransitionError("confirm_development_pr: no row is bound")
            fired = engine.applied(outcome, ("reconcile", "_tick"), recovery.fired_group(c, new_op))
            contributions[key] = fired.record
            out.staged.extend([("contributions", fired.record), ("events", fired.event)])
            dev_pr_report.append({"contribution": cid, "status": "confirmed", "pr_number": pr["number"]})
    out.report["development_prs"] = dev_pr_report

    # Receipts of the CI reruns of interrupted CI, read back independently.
    out.report["ci_reruns"] = fold_ci_rerun_receipts(
        api, ci_reruns or [], contributions, by_cid, snap, out, now=iso, skip=skip)

    # Verified receipts of the handoff operations, each read back
    # independently before its operation fires a row of the table.
    out.report["handoffs"] = fold_handoff_receipts(
        api, handoffs or [], contributions, by_cid, snap, out, policy=policy, now=iso, skip=skip)

    # Verified administrative provisioning receipts are separate from model
    # artifact Results. Re-read the actual fork/ref before advancing state.
    out.report["provisions"] = fold_provisioning_receipts(
        api, provisions or [], contributions, by_cid, snap, out, policy=policy, now=iso, skip=skip)

    # 1. Publication outcomes from finished workers. A development reset or an
    # infrastructure retry can leave multiple artifacts for the same immutable
    # contribution revision. Fold only the newest artifact; an older one no
    # longer matches anything once the newest is folded, so it takes
    # `record_late_effect` like every other Result that matches nothing.
    latest: dict[tuple[str, int], tuple[tuple[int, int], dict]] = {}
    superseded = []
    for pub in publications:
        task = pub.get("task") or {}
        cid, revision = task.get("contribution_id"), task.get("revision")
        if not isinstance(cid, str) or not isinstance(revision, int):
            continue
        def numeric(value):
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0
        rank = (numeric(pub.get("publication_artifact_id")), numeric(pub.get("worker_run_id")))
        key = (cid, revision)
        prior = latest.get(key)
        if prior is None or rank > prior[0]:
            if prior is not None:
                superseded.append(prior[1])
            latest[key] = (rank, pub)
        else:
            superseded.append(pub)
    publications = [item[1] for item in latest.values()] + [
        dict(pub, superseded=True) for pub in superseded]
    pubs_report = []
    for pub in publications:
        cid = (pub.get("task") or {}).get("contribution_id")
        if cid not in by_cid:
            pubs_report.append({"ignored": "unknown contribution"})
            continue
        if cid in skip:
            continue
        with isolation.contribution(cid, "publication"):
            key = by_cid[cid]
            rec = contributions[key]
            pid = pub.get("publication_artifact_id")
            if bool(pid) and pid in (rec.get("processed_publications") or []):
                # Already folded in by an earlier tick, or already recorded as a
                # late effect: the audit event is written once per artifact.
                continue
            if pub.get("superseded") and not pid:
                # Without an artifact id there is nothing to record it once by.
                pubs_report.append({"contribution": cid, "ignored": "superseded publication artifact"})
                continue
            before = rec
            if pid:
                rec = dict(rec, processed_publications=(
                    (rec.get("processed_publications") or []) + [pid])[-PROCESSED_PUBLICATIONS:])
                contributions[key] = rec
            unrequested = ("superseded publication artifact" if pub.get("superseded")
                           else result_mismatch(rec, pub))
            if unrequested:
                # The table's `record_late_effect`: a Result that does not match
                # the current generation, revision, and requested Work is recorded
                # for audit and never folded, in every state. A branch effect it
                # carried is seen by head observation.
                extra: dict[str, Any] = {}
                reported = late_report_evidence(rec, pub, now=iso, policy=policy,
                                                known=snap.evidence, details=extra)
                # A late `published` Result of a patch published anyway (its
                # record stopped, paused, or exhausted the publication before
                # this Result was folded): the patch is on the fork, so the
                # comment of the review is due all the same, in this write.
                # So does a branch operation the late Result confirms whose
                # commit a `publish_anyway` review pinned.
                due, dropped = patch_review.comment_due(
                    rec, patch_review.published_anyway(rec, pub, snap.evidence)
                    + patch_review.landed_anyway(
                        rec, patch_review.confirmed_heads(rec, [pub.get("operation") or {}]),
                        snap.evidence), now=iso)
                if due != (rec.get("review_comments") or []):
                    _note_due(extra, rec, due, dropped)
                    rec = engine.record_beside(rec, "review_comment_due", now=iso,
                                               review_comments=due)
                    contributions[key] = rec
                out.staged.extend(reported + [("contributions", rec), ("events", _late_effect(
                    rec, pub, unrequested, policy=policy, now=iso, extra=extra))])
                pubs_report.append({"contribution": cid, "ignored": unrequested})
                continue
            try:
                rec, staged = apply_publication(rec, pub, now=iso, policy=policy,
                                                worker_run_id=pub.get("worker_run_id"),
                                                evidence=snap.evidence, pypi=pypi,
                                                operations=snap.operations)
            except ResultDeferred as deferred:
                # Not folded: the artifact is read again in a later tick.
                contributions[key] = before
                out.staged.extend(deferred.staged)
                for _kind, r in deferred.staged:
                    snap.operations[r["operation_id"]] = r
                pubs_report.append({"contribution": cid, "status": pub.get("status"),
                                    "deferred": deferred.reason[:500]})
                continue
            contributions[key] = rec
            out.staged.extend(staged)
            for kind, r in staged:
                if kind == "operations":
                    snap.operations[r["operation_id"]] = r
            pubs_report.append({"contribution": cid, "status": pub.get("status"), "reason": pub.get("reason")})
    out.report["publications"] = pubs_report

    # 1b. Requested Work whose graph is terminal without a usable Result is
    # recovered by the table's rows in dispatch recovery; no deadline applies.
    # A completed graph is terminal without a usable Result once a
    # publication of this tick names it, or once its worker run ended
    # without one (``work_results``).
    # 1c. Requested tasks whose dispatch never reached the queue: relaunch the
    # dispatcher (bounded), then wait for a human.
    collected = frozenset(work.graph_id(key) for key in (
        (pub.get("task") or {}).get("dedupe_key") for pub in publications)
        if isinstance(key, str))
    relaunch, staged, rep = dispatch_recovery(contributions, runs=dispatcher_runs, queue_works=queue_works,
                                              now=now, policy=policy, skip=skip,
                                              queue_policy=queue_policy, evidence=snap.evidence,
                                              queue_retries=queue_retries,
                                              work_results=work_results, collected=collected)
    out.staged.extend(staged)
    out.report["dispatch_recovery"] = rep
    # 1d. A dispatch refused by the installed work-queue Policy: re-arm it
    # once the dispatcher has reinstalled the compiled policy, and launch the
    # dispatcher to reinstall it when the queue is quiescent.
    launch, staged, rep = queue_policy_recovery(contributions, queue_policy=queue_policy,
                                                runs=dispatcher_runs, now=now, policy=policy,
                                                skip=skip)
    out.staged.extend(staged)
    if queue_policy is not None:
        out.report["queue_policy"] = {"status": queue_policy, "recovery": rep}
    out.report["dispatch_relaunch"] = relaunch + launch

    # 2. Live branch and CI observation.
    if observe:
        contracts = {e["evidence_id"]: e for e in snap.evidence.values() if e.get("kind") == "target_contract"}
        obs = observe_ci.observe(api, contributions=contributions, operations=snap.operations,
                                 contracts=contracts, policy=policy, now=iso,
                                 evidence=snap.evidence, skip=skip, audited=snap.audited)
        for kind, r in obs.staged:
            if kind == "contributions":
                contributions[by_cid[r["contribution_id"]]] = r
        out.staged.extend(obs.staged)
        out.report["observation"] = obs.report
        # Recovery is evaluated before live CI. A record that no longer holds
        # its Work once observed (CI accepted the head, or the head moved) is
        # not launched again; the submission rewrite and review Work of the
        # handoff phase are relaunched like development Work.
        out.report["dispatch_relaunch"] = [
            item for item in out.report["dispatch_relaunch"]
            if "contribution_id" not in item
            or _holds_dispatched_work(contributions[by_cid[item["contribution_id"]]])]

    # 3. Commands, intake and work selection (controller). The failing CI job
    # of a head is decided from the evidence the record names, including the
    # evidence this tick recorded.
    evidence = dict(snap.evidence)
    evidence.update({r["evidence_id"]: r for kind, r in out.staged if kind == "evidence"})
    handoff_observations = {item["contribution"]: item["handoff"]
                            for item in out.report.get("observation") or []
                            if isinstance(item.get("handoff"), dict)}
    upstream_pr_observations = {item["contribution"]: item["upstream_pr"]
                                for item in out.report.get("observation") or []
                                if isinstance(item.get("upstream_pr"), dict)}
    observed_dev_heads = {item["contribution"]: item["observed_head"]
                          for item in out.report.get("observation") or []
                          if item.get("ref") == DEV_REF and isinstance(item.get("observed_head"), str)}
    # Work that returned a Result in this tick, or whose graph the queue
    # holds as terminal, is never allocated again under the same id.
    spent_graphs = frozenset(
        list(collected)
        + [graph for graph, state in (queue_works or {}).items()
           if state in ("completed", "cancelled")])
    res = controller.tick(contributions=contributions, operations=snap.operations, consumed=snap.consumed,
                          projects=snap.projects, policy=policy,
                          inputs=controller.Inputs(now=now, issues=issues or [], comments=comments or [],
                                                   pressure=pressure,
                                                   handoff_observations=handoff_observations,
                                                   upstream_pr_observations=upstream_pr_observations,
                                                   observed_dev_heads=observed_dev_heads,
                                                   tracking_issues=tracking_issues or {},
                                                   upstream_snapshots=upstream_snapshots or {},
                                                   acceptances=acceptances or {},
                                                   spent_graphs=spent_graphs,
                                                   handoff_runs=handoff_runs,
                                                   development_pr_runs=development_pr_runs,
                                                   provision_runs=provision_runs),
                          skip=skip, evidence=evidence)
    out.staged.extend(res.staged)
    # The steps below write beside the records as the controller left them,
    # never over what it staged.
    for kind, r in res.staged:
        if kind == "contributions" and r["contribution_id"] in by_cid:
            contributions[by_cid[r["contribution_id"]]] = r
    out.report["controller"] = res.report
    res.report["alerts"][:0] = load_alerts
    out.report["requested_effects"] = res.requested_effects
    out.report["provision_requests"] = res.report["provision_requests"]
    out.report["dev_pr_requests"] = res.report["dev_pr_requests"]
    out.report["handoff_requests"] = res.report["handoff_requests"]
    # 4. Publish-only retries due now (infrastructure failures; no model call).
    retries = []
    for key, c in list(contributions.items()):
        if c["contribution_id"] in skip:
            continue
        with isolation.contribution(c["contribution_id"], "publication_retry"):
            pp = c.get("pending_publication")
            if pp and controller.published_head_without_pr(c, snap.operations, None):
                # The push of the held patch landed and its development PR
                # is being opened: its Result is folded once the PR is
                # recorded, so nothing is retried or expired meanwhile.
                continue
            cid = c["contribution_id"]
            retrying = bool(pp) and not pp.get("exhausted") and not engine.stopped(c) \
                and engine.state_key(c) in WORK_RESULT_STATES
            if retrying and datetime.fromisoformat(pp["next_attempt_at"]) > now:
                # The retry is due, or the Result of the launched one is
                # waited for no longer, at that time; a held patch the
                # retry reads expires at its own time.
                wake.note(out.report, pp["next_attempt_at"],
                          "publish-only retry or its Result" if pp.get("launched_at")
                          else "publish-only retry", now=now, contribution=cid)
                expires = patch_review.held_patch_expiry_due_at(c) \
                    if engine.state_key(c) == lifecycle.PUBLISHING else None
                if expires is not None and expires > now:
                    wake.note(out.report, expires, "held patch expiry", now=now, contribution=cid)
            if retrying and datetime.fromisoformat(pp["next_attempt_at"]) <= now:
                # `schedule_publish_retry` held the Result and charged
                # `publish_recovery` when its publication failed; the retry
                # is launched once it is due, and the launch time is
                # recorded. The retry's own Result is folded by the table's
                # rows. A launched retry whose Result is not folded within
                # PUBLISH_RESULT_WAIT_MINUTES is a failed publication, like
                # a Result that reports one: `schedule_publish_retry` charges
                # `publish_recovery` again, or `exhaust_recovery`
                # waits for a human once it is spent.
                if engine.state_key(c) == lifecycle.PUBLISHING \
                        and patch_review.held_patch_expired(c, now):
                    # An artifact the publication reads expired or expires
                    # within a day: nothing is launched again, whether or
                    # not a retry was lost, the held patch is
                    # discarded and fresh development Work is due.
                    held = c.get("held_patch") or {}
                    fired = engine.applied(engine.try_apply_transition(
                        c, "expire_held_patch", policy=policy, now=now.isoformat(),
                        cause_id=f"expired-{held.get('worker_run_id')}",
                        facts={"held_patch_expired"},
                        details={"expires_at": held.get("expires_at"),
                                 "candidate": held.get("candidate")}), ("reconcile", "_tick"))
                    schemas.validate("contribution", fired.record)
                    contributions[key] = fired.record
                    out.staged.extend([("contributions", fired.record), ("events", fired.event)])
                    continue
                if pp.get("launched_at"):
                    fired = _fail_publication(
                        c, pp["worker_run_id"],
                        f"the publish-only retry launched at {pp['launched_at']} returned no "
                        f"Result within {PUBLISH_RESULT_WAIT_MINUTES} minutes",
                        now=now.isoformat(), policy=policy, cause=pp["worker_run_id"],
                        details={"result_status": "no_result", "launched_at": pp["launched_at"]})
                    schemas.validate("contribution", fired.record)
                    contributions[key] = fired.record
                    out.staged.extend([("contributions", fired.record), ("events", fired.event)])
                    continue
                attempt = pp["attempts"] + 1
                changed = engine.record_beside(c, "publish_retry_launched", now=now.isoformat(),
                                               pending_publication=dict(
                    pp, launched_at=now.isoformat(),
                    next_attempt_at=(now + timedelta(
                        minutes=PUBLISH_RESULT_WAIT_MINUTES)).isoformat()))
                wake.note(out.report, now + timedelta(minutes=PUBLISH_RESULT_WAIT_MINUTES),
                          "Result of the publish-only retry", now=now, contribution=cid)
                contributions[key] = changed
                out.staged.append(("contributions", changed))
                retries.append({"type": "retry_publication", "contribution_id": c["contribution_id"],
                                "worker_run_id": pp["worker_run_id"], "attempt": attempt})
    out.report["publication_retries"] = retries
    # 4b. The comment of a patch published after successive failed reviews:
    # its receipts are folded, and a due post is launched, bounded.
    out.report["review_comment_posts"] = review_comments(
        contributions, review_comment_receipts or [], out, policy=policy, now=now, skip=skip)
    # 5. The history of every requested Work that has none yet, normally
    # recorded in the tick that allocates it: the new human steering of the
    # contribution, then the history built from all its evidence. A failed
    # steering read defers only the history: the allocation is committed and
    # the dispatcher defers the Work until a later tick records its history;
    # a steering list that is gone for good is recorded as unreadable, and
    # one that still fails once the Work has waited
    # ``STEERING_FAILURE_GRACE`` is read without.
    if observe:
        out.report["work_history"] = work_histories(
            api, contributions, out, snap, policy=policy, now=iso,
            tracking_issues=tracking_issues or {}, skip=skip)
    out.staged = observe_ci.dedupe(out.staged)
    # 6. What ends every wait of this tick: the time of the earliest timed
    # wait, which the controller workflow wakes the controller at.
    wake.harvest(out.report, out.report.get("dispatch_recovery") or [], now=now,
                 why="dispatch recovery")
    # What could not be read decides nothing this tick, and the sleeper of a
    # later report replaces an earlier one: look again soon, so the earliest
    # wake is never lost.
    if (dispatcher_runs is None or queue_works is None) and any(
            c.get("active_work") for c in contributions.values()):
        wake.note(out.report, now + READ_RETRY, "dispatch recovery read nothing this tick",
                  now=now)
    if skip:
        wake.note(out.report, now + READ_RETRY, "contributions skipped this tick", now=now)
    wake.harvest(out.report, (out.report.get("queue_policy") or {}).get("recovery") or [], now=now,
                 why="queue policy")
    wake.finish(out.report, now)
    return out


def review_comments(contributions: dict[str, dict], receipts: list[dict], out: TickOutput, *,
                    policy: dict, now: datetime, skip: frozenset[str]) -> list[dict]:
    """Post every comment due on a development PR for a patch published
    after successive failed reviews (``review_comments``), durably.

    Each due comment has its own attempts. The receipt of a launched post
    (``review-comment.json`` of the ``review-comment.yml`` run, for the
    contribution, the review and its current attempt) is folded first: a
    comment posted, or found by its marker, ends it and only then is it
    removed; a post that failed, was skipped, or left no receipt within
    ``COMMENT_RECEIPT_WAIT_MINUTES`` is launched again later. Once
    ``COMMENT_ATTEMPTS`` are spent the comment is ``exhausted``: it stays
    recorded with the last reason, an alert in every tick says that the
    development PR lacks it, and it is launched again every
    ``COMMENT_EXHAUSTED_RETRY_HOURS``, so the alert ends once a human has
    restored what the post needs, up to ``COMMENT_LAST_ATTEMPT``, the last
    attempt ``review-comment.yml`` admits. A due post is launched while the
    policy permits writes, unless it is held: by the pause of a record that
    is not terminal (``patch_review.held_by_pause``), or while no
    development PR is recorded; a held post spends no attempt, and is
    alerted on in every tick once it has been held for
    ``COMMENT_HELD_ALERT_HOURS``, or at once for a terminal record. Its
    attempt is recorded before the launch, so a post is never launched twice
    for one attempt. Beyond ``MAX_DUE_COMMENTS`` the oldest exhausted
    comments are dropped, each with an alert, and so is every comment this
    tick dropped to fit the comments it made due within
    ``RECORDED_DUE_COMMENTS`` (``patch_review.comment_due``), named in its
    event, its observation or the report of the controller."""
    report: list[dict] = []
    out.report.setdefault("review_comment_requests", [])
    alerts = out.report["controller"]["alerts"]
    iso = now.isoformat()
    named = [(r.get("contribution_id"), (r.get("details") or {}).get("review_comments_dropped"))
             for kind, r in out.staged if kind == "events"] + [
        (item.get("contribution"), item.get("review_comments_dropped"))
        for item in (out.report.get("observation") or [])
        + ((out.report.get("controller") or {}).get("comments_due") or [])]
    for cid, dropped in named:
        for eid in dropped or []:
            alerts.append({"contribution": cid, "alert": (
                f"the comment of the review {eid} is no longer posted: more than "
                f"{patch_review.RECORDED_DUE_COMMENTS} comments were due on the development PR "
                "when another became due, and it was dropped to fit it; the patch was "
                "published although that review failed, and the PR does not say so")})
            report.append({"contribution": cid, "evidence_id": eid, "status": "dropped"})
    folded: dict[tuple[str, str, int], dict] = {}
    for receipt in receipts:
        attempt = receipt.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool):
            folded[(str(receipt.get("contribution_id")), str(receipt.get("evidence_id")),
                    attempt)] = receipt
    for key in list(contributions):
        c = contributions[key]
        cid = c["contribution_id"]
        if not c.get("review_comments") or cid in skip:
            continue
        with isolation.contribution(cid, "review_comment"):
            # 1. The receipts of launched posts.
            kept: list[dict] = []
            answered = False
            for due in c["review_comments"]:
                attempt = int(due["attempts"])
                entry: dict[str, Any] = {"contribution": cid, "evidence_id": due["evidence_id"],
                                         "attempt": attempt}
                if not due.get("launched_at"):
                    kept.append(due)
                    continue
                receipt = folded.get((cid, due["evidence_id"], attempt))
                waited = now - datetime.fromisoformat(due["launched_at"])
                if receipt is None and waited < timedelta(
                        minutes=patch_review.COMMENT_RECEIPT_WAIT_MINUTES):
                    kept.append(due)
                    report.append(entry | {"status": "launched"})
                    continue
                answered = True
                status = (receipt or {}).get("status")
                if status in patch_review.COMMENT_DONE:
                    report.append(entry | {"status": status,
                                           "comment_id": (receipt or {}).get("comment_id")})
                    continue
                reason = isolation.clean_text(
                    (receipt or {}).get("reason") or (
                        f"{status}" if receipt else
                        f"no receipt within {patch_review.COMMENT_RECEIPT_WAIT_MINUTES} minutes"),
                    500)
                spent = bool(due.get("exhausted")) \
                    or attempt >= patch_review.COMMENT_ATTEMPTS
                wait = (timedelta(hours=patch_review.COMMENT_EXHAUSTED_RETRY_HOURS) if spent
                        else timedelta(minutes=patch_review.COMMENT_RETRY_MINUTES * attempt))
                kept.append(dict(due, launched_at=None, last_reason=reason, exhausted=spent,
                                 next_attempt_at=(now + wait).isoformat()))
                entry["reason"] = reason
            # 2. The bound: the oldest exhausted comments beyond it are
            # dropped, each with an alert.
            while len(kept) > patch_review.MAX_DUE_COMMENTS:
                oldest = next((d for d in kept if d.get("exhausted")
                               and not d.get("launched_at")), None)
                if oldest is None:
                    break
                kept.remove(oldest)
                answered = True
                alerts.append({"contribution": cid, "alert": (
                    f"the comment of the review {oldest['evidence_id']} is no longer posted: "
                    f"more than {patch_review.MAX_DUE_COMMENTS} comments are due on the "
                    "development PR, and it is the oldest whose attempts are spent (last: "
                    f"{oldest.get('last_reason')}); the patch was published although that "
                    "review failed, and the PR does not say so")})
                report.append({"contribution": cid, "evidence_id": oldest["evidence_id"],
                               "attempt": int(oldest["attempts"]), "status": "dropped"})
            if answered:
                c = engine.record_beside(c, "review_comment_answered", now=iso,
                                         review_comments=kept)
                contributions[key] = c
                out.staged.append(("contributions", c))
            # 3. An alert for every exhausted comment, in every tick, and the
            # launch of every comment that is due now.
            launched: list[dict] = []
            requests: list[dict] = []
            terminal = engine.state_key(c) in lifecycle.TERMINAL_STATES
            for due in kept:
                attempt = int(due["attempts"])
                entry = {"contribution": cid, "evidence_id": due["evidence_id"],
                         "attempt": attempt}
                repository, number = patch_review.comment_target(c, due)
                last = attempt >= patch_review.COMMENT_LAST_ATTEMPT
                if due.get("exhausted"):
                    alerts.append({"contribution": cid, "alert": (
                        f"the comment of the review {due['evidence_id']} on the development PR "
                        f"{repository or '(none recorded)'}#{number or '?'} could not be posted "
                        f"in {attempt} attempts (last: {due.get('last_reason')}); the patch was "
                        "published although that review failed, and the PR does not say so. "
                        + ("Its attempts are spent and it is no longer posted; a human posts "
                           "it" if last else
                           "It is posted again every "
                           f"{patch_review.COMMENT_EXHAUSTED_RETRY_HOURS} hours until it is "
                           "posted"))})
                # A post that cannot be made yet is held without spending an
                # attempt: by the pause of a contribution that is not
                # terminal (its resume launches it), or until a development
                # PR is recorded (the first patch published anyway has none
                # until the development PR operation confirms one).
                held = ("the contribution is paused" if patch_review.held_by_pause(c)
                        else None if repository and number
                        else "no development PR is recorded")
                due_at = datetime.fromisoformat(due["next_attempt_at"])
                if held and not due.get("launched_at") and due_at <= now and (
                        terminal or now - due_at >= timedelta(
                            hours=patch_review.COMMENT_HELD_ALERT_HOURS)):
                    alerts.append({"contribution": cid, "alert": (
                        f"the comment of the review {due['evidence_id']} is due on the "
                        f"development PR since {due['next_attempt_at']} and is not posted: "
                        f"{held}"
                        + ("; the contribution is terminal, so nothing else will post it"
                           if terminal else "")
                        + ". The patch was published although that review failed, and the "
                        "PR does not say so")})
                if due.get("launched_at") or held or policy.get("read_only", True) \
                        or due_at > now or (last and due.get("exhausted")):
                    launched.append(due)
                    # What ends the wait: the receipt wait of a launched
                    # post, or the time its next attempt is due. A held
                    # post is woken by the resume or the PR that ends the
                    # hold.
                    if not policy.get("read_only", True) and not held:
                        if due.get("launched_at"):
                            wake.note(out.report, datetime.fromisoformat(due["launched_at"])
                                      + timedelta(minutes=patch_review.COMMENT_RECEIPT_WAIT_MINUTES),
                                      "receipt of the review comment post", now=now,
                                      contribution=cid)
                        elif due_at > now:
                            wake.note(out.report, due_at, "next attempt of the review comment",
                                      now=now, contribution=cid)
                    if not due.get("launched_at"):
                        report.append(entry | {"status": "exhausted" if due.get("exhausted")
                                               else "held" if held and due_at <= now
                                               else "waiting"} | (
                            {"reason": held} if held and due_at <= now else {}))
                    continue
                attempt += 1
                launched.append(dict(due, attempts=attempt, launched_at=iso,
                                     repository=repository, pr_number=number))
                wake.note(out.report, now + timedelta(
                    minutes=patch_review.COMMENT_RECEIPT_WAIT_MINUTES),
                    "receipt of the review comment post", now=now, contribution=cid)
                requests.append({
                    "type": "post_review_comment", "contribution_id": cid,
                    "contribution_file": contribution_file_id(cid),
                    "evidence_id": due["evidence_id"], "attempt": attempt})
                report.append(entry | {"status": "launch", "attempt": attempt})
            if requests:
                new = engine.record_beside(c, "review_comment_launched", now=iso,
                                           review_comments=launched)
                contributions[key] = new
                out.staged.append(("contributions", new))
                out.report["review_comment_requests"].extend(requests)
    return report


# The bounds of what the histories of one tick stage, in bytes of canonical
# JSON, so that the state commit always fits ``storage.max_commit_bytes``
# (2 MiB) with the rest of the tick: the new ``human_steering`` records of
# one contribution, and the steering and histories of the whole tick. The
# history of a Work that does not fit is built by a later tick; steering
# beyond the bound of one contribution is recorded by later ticks and
# counted as ``steering_deferred`` meanwhile.
STEERING_BYTES_PER_CONTRIBUTION = 256 * 1024
HISTORY_BYTES_PER_TICK = 1024 * 1024
# The bytes a ``work_history`` record takes at most (the document and the
# fields around it).
HISTORY_RECORD_BYTES = work_history.HISTORY_MAX_BYTES + 8 * 1024
# The bound of the ``human_steering`` records one contribution stages in one
# tick (the byte bounds above are reached first).
STEERING_RECORDS_PER_TICK = 64
# How long a Work waits for its history while a steering list fails with an
# error that is not final (a 403, a 5xx, a rate limit): after that, the list
# is recorded as unreadable and the Work proceeds without it.
STEERING_FAILURE_GRACE = timedelta(hours=1)
# How long dispatch recovery leaves requested Work alone while its history is
# not committed (the dispatcher defers it by design): the steering grace and
# a margin for the ticks that build the history.
HISTORY_WAIT = STEERING_FAILURE_GRACE + timedelta(hours=1)


def _waited(record: dict, now: str) -> timedelta:
    """How long the record's requested Work has waited since it was
    requested."""
    try:
        requested = datetime.fromisoformat(str(record["active_work"]["requested_at"]))
        return datetime.fromisoformat(now) - requested
    except (KeyError, TypeError, ValueError):
        return timedelta(0)


def work_histories(api: collect.Api, contributions: dict[str, dict], out: TickOutput,
                   snap: Snapshot, *, policy: dict, now: str, tracking_issues: dict,
                   skip: frozenset[str]) -> list[dict]:
    """Stage the ``work_history`` evidence of every requested Work that has
    none yet (``work_history.evidence_id`` of its Work id), with the
    ``human_steering`` evidence of the text collected for it first. The
    record as this tick leaves it is used, so Work allocated in this tick is
    covered. What is staged is bounded in bytes per contribution and per
    tick; a Work left out by the bound of the tick gets its history in a
    later tick. A steering list that fails with an error that is not final
    defers the history, never the allocation, which the tick commits; it is
    read without, with an alert, once the Work has waited
    ``STEERING_FAILURE_GRACE`` since its committed ``requested_at``, so one
    failing list never holds the Work. The tick that records the history of
    Work an earlier tick requested asks for a dispatcher launch
    (``dispatch_relaunch``), since the dispatcher deferred that Work."""
    latest = {c["contribution_id"]: c for c in contributions.values()}
    for kind, record in out.staged:
        if kind == "contributions":
            latest[record["contribution_id"]] = record
    evidence = dict(snap.evidence)
    evidence.update({r["evidence_id"]: r for kind, r in out.staged if kind == "evidence"})
    authority = policy["command_authority"]["actor_ids"]
    # The Work this tick submits is dispatched by the launch that follows
    # the commit; any other Work whose history this tick records was
    # deferred by an earlier dispatcher run.
    submitted = {item.get("dedupe_key") for item in out.report.get("requested_effects") or []
                 if item.get("type") == "submit_work"}
    report = []
    budget = HISTORY_BYTES_PER_TICK
    for cid in sorted(latest):
        if cid in skip:
            continue
        record = latest[cid]
        with isolation.contribution(cid, "work_history"):
            request = work.request_from_active(record)
            if request is None:
                continue
            eid = work_history.evidence_id(request["dedupe_key"])
            if eid in evidence:
                continue
            if budget < HISTORY_RECORD_BYTES:
                report.append({"contribution": cid, "work": request["dedupe_key"],
                               "deferred": "the histories of this tick reached their byte bound"})
                continue
            waited = _waited(record, now)
            degrade = waited >= STEERING_FAILURE_GRACE
            try:
                collected = collect.steering(
                    api, record, authority,
                    issue=tracking_issues.get(record["tracking_issue"]["number"]),
                    degrade=degrade)
            except collect.CollectError as error:
                # A list that fails with an error that is not final defers
                # only the history: the allocation is committed, so the
                # grace is measured from its durable ``requested_at``, and
                # the dispatcher defers the Work until a later tick builds
                # its history (without that list once the grace is over).
                report.append({
                    "contribution": cid, "work": request["dedupe_key"],
                    "deferred": "a steering list could not be read: "
                                f"{isolation.clean_text(error)}; the Work has waited "
                                f"{int(waited.total_seconds()) // 60} minutes, and once it "
                                f"has waited {STEERING_FAILURE_GRACE} its history is built "
                                "without the lists that still fail"})
                # The list is read again at the end of the grace, or soon
                # when it already is read without the lists that fail.
                moment = datetime.fromisoformat(now)
                wake.note(out.report, moment + (READ_RETRY if degrade else
                                                STEERING_FAILURE_GRACE - waited),
                          "steering list read again", now=moment, contribution=cid)
                continue
            # One record holds at most ``steering.MAX_ITEMS`` items; the
            # rest are recorded in further records of the same tick, within
            # the byte bounds, so the history holds the newest steering too.
            room = min(STEERING_BYTES_PER_CONTRIBUTION, budget - HISTORY_RECORD_BYTES)
            recorded, staged = [], []
            for _ in range(STEERING_RECORDS_PER_TICK):
                if room < steering.MAX_RECORD_ITEM_BYTES:
                    break
                new = steering.new_evidence(record, collected, evidence, policy=policy,
                                            now=now, max_bytes=room - 4096, newest=True)
                if new is None:
                    break
                size = len(canonical_json(new).encode())
                room -= size
                evidence[new["evidence_id"]] = new
                staged.append((("evidence", new), size))
                recorded.append(new["evidence_id"])
                if not new["data"]["deferred"]:
                    break
            deferred = steering.pending(record, collected, evidence)
            history = work_history.build(record, record["active_work"], evidence,
                                         policy=policy, now=now, deferred=deferred,
                                         unreadable=collected.get("unreadable") or [])
            evidence[eid] = history
            out.staged.extend(item for item, _size in staged)
            out.staged.append(("evidence", history))
            budget -= sum(size for _item, size in staged) + len(canonical_json(history).encode())
            data = history["data"]
            entry = {"contribution": cid, "work": request["dedupe_key"],
                     "evidence_id": eid, "steering": len(data["steering"]),
                     "timeline": len(data["timeline"]),
                     "condensed": data["condensed"],
                     "steering_evidence_ids": recorded,
                     "steering_unreadable": list(collected.get("unreadable") or [])}
            if deferred:
                entry["steering_deferred"] = deferred
            if request["dedupe_key"] not in submitted:
                # The Work was requested by an earlier tick and the
                # dispatcher deferred it while its history was missing:
                # the dispatcher is launched once this history is committed,
                # without waiting for dispatch recovery.
                entry["dispatch"] = "relaunch: the history was recorded after the request"
                launches = out.report.setdefault("dispatch_relaunch", [])
                if not launches:
                    launches.append({"type": "relaunch_dispatcher", "contribution_id": cid})
            if collected.get("failed"):
                entry["steering_failed"] = dict(collected["failed"])
                if not degrade:
                    moment = datetime.fromisoformat(now)
                    wake.note(out.report, moment + STEERING_FAILURE_GRACE - waited,
                              "steering list read without the lists that fail", now=moment,
                              contribution=cid)
                out.report["controller"]["alerts"].append({
                    "contribution": cid,
                    "alert": "steering lists failing for longer than "
                             f"{STEERING_FAILURE_GRACE}, read without them: "
                             + ", ".join(f"{k} (HTTP {v})" if isinstance(v, int)
                                         else f"{k} ({v})"
                                         for k, v in sorted(collected["failed"].items()))})
            report.append(entry)
    return report


def load_snapshot(root: Path, policy: dict, run: dict | None = None):
    """Load validated lifecycle state relative to an explicit repository root.
    ``run`` is the workflow run recorded on every event the store stages."""
    import state

    store = state.StateStore(root, policy, run=run)
    contributions = dict(store.load_all("contributions"))
    return store, Snapshot(
        contributions=contributions,
        operations={
            value["operation_id"]: value for value in store.load_all("operations").values()
        },
        consumed={
            value["comment_id"]: value for value in store.load_all("commands").values()
        },
        projects={value["project_id"]: value for value in store.load_all("projects").values()},
        evidence={
            value["evidence_id"]: value for value in store.load_all("evidence").values()
        },
        audited=audited_observations(store, contributions),
    )


# The states whose observation records each distinct observation once, and
# the verb that records it.
AUDITED_OBSERVATIONS = {"handoff.waiting_upstream_pr": "record_audit_observation",
                        "release_wait.idle": "record_release_incomplete"}


def audited_observations(store, contributions: dict[str, dict]) -> dict[str, set[str]]:
    """The observation keys already recorded, by contribution: from the
    `record_audit_observation` events of the contributions in the locked
    upstream-PR wait, and the `record_release_incomplete` events of those in
    `release_wait.idle`. No event is read when none is in such a state."""
    waiting = {record["contribution_id"]: AUDITED_OBSERVATIONS[engine.state_key(record)]
               for record in contributions.values()
               if engine.state_key(record) in AUDITED_OBSERVATIONS}
    audited: dict[str, set[str]] = {}
    if not waiting:
        return audited
    for event in store.load_all("events").values():
        key = (event.get("details") or {}).get("observation_key")
        if key and event.get("kind") == waiting.get(event.get("contribution_id")):
            audited.setdefault(event["contribution_id"], set()).add(key)
    return audited


def reconcile_snapshot(root: Path, policy: dict) -> Snapshot:
    return load_snapshot(root, policy)[1]


def _publication_outcomes(directory: str | None) -> list[dict]:
    publications = []
    for path in sorted(Path(directory).glob("*/outcome.json")) if directory else []:
        outcome = json.loads(path.read_text())
        task = path.parent / "task.json"
        outcome["task"] = json.loads(task.read_text()) if task.exists() else {}
        worker_run = path.parent / "worker-run-id"
        outcome["worker_run_id"] = (
            worker_run.read_text().strip() if worker_run.exists() else path.parent.name
        )
        artifact_id = path.parent / "artifact-id"
        outcome["publication_artifact_id"] = (
            int(artifact_id.read_text().strip()) if artifact_id.exists() else None
        )
        # The run that owns the artifact (the directory is named by it): the
        # worker run, or the publish-only retry that wrote it. A held patch
        # is pinned to it, with the digest of the series it carries and the
        # expiry GitHub reported for the artifact.
        outcome["publication_run_id"] = path.parent.name
        # The stage of a publish-only retry: `publish` when it read the record
        # in development.publishing (the controller keeps the file of a
        # retry only).
        stage = path.parent / "publication-stage"
        outcome["publication_stage"] = (
            "publish" if stage.is_file() and stage.read_text().strip() == "publish" else None)
        expires = path.parent / "artifact-expires-at"
        text = expires.read_text().strip() if expires.is_file() else ""
        outcome["publication_artifact_expires_at"] = (
            text if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})",
                                 text) else None)
        series = path.parent / "patches.mbox"
        outcome["publication_series_digest"] = (
            hashlib.sha256(series.read_bytes()).hexdigest()
            if series.is_file() and not series.is_symlink() else None
        )
        # The worker's report (untrusted): the bytes of the file, read as the
        # gate read them, checked again and stored only when a success row
        # folds the Result. A file that cannot be read is named, so that a
        # success fold of Work that carries a history fails loudly.
        outcome.pop("worker_report", None)
        try:
            report = worker_report.read(path.parent / "report.json")
        except (worker_report.ReportError, OSError) as error:
            outcome["worker_report_error"] = f"report.json cannot be read: {error}"
        else:
            if report is not None:
                outcome["worker_report"] = report
        for name, key in (
            ("plan.json", "plan"),
            ("proposal.json", "proposal"),
            ("result.json", "artifact"),
        ):
            candidate = path.parent / name
            if candidate.exists() and key not in outcome:
                try:
                    outcome[key] = json.loads(candidate.read_text())
                except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
                    # A deeply nested document is as unusable as a malformed one.
                    pass
        publications.append(outcome)
    return publications


# The run names `handoff.yml` and `dev-pr.yml` give a run:
# `Handoff <operation id>`, `Development PR <operation id>` and
# `Fork Provisioning <operation id>`.
HANDOFF_RUN_PREFIX = "Handoff "
DEVELOPMENT_PR_RUN_PREFIX = "Development PR "
PROVISION_RUN_PREFIX = "Fork Provisioning "


def _handoff_runs(path: str | None, prefix: str = HANDOFF_RUN_PREFIX) -> dict[str, int] | None:
    """The workflow runs that are not completed, by operation, from the run
    list the controller workflow wrote (``id``, ``status``, ``title``) for
    the workflow whose run names start with ``prefix``, or None when it was
    not read this tick. An entry of another shape is ignored; the title is
    only matched, never rendered."""
    title_pattern = re.compile(re.escape(prefix) + r"(op-[0-9a-f]{24})")
    if not path:
        return None
    try:
        entries = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    runs: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status, run_id, title = entry.get("status"), entry.get("id"), entry.get("title")
        if not isinstance(status, str) or status == "completed":
            continue
        if not isinstance(run_id, int) or isinstance(run_id, bool) or not isinstance(title, str):
            continue
        match = title_pattern.fullmatch(title)
        if match:
            runs[match.group(1)] = run_id
    return runs


def _review_comment_receipts(directory: str | None) -> list[dict]:
    """The receipts of the posts of review comments (``review-comment.json``
    of each collected ``review-comment.yml`` run); a malformed one is
    skipped."""
    receipts = []
    for path in sorted(Path(directory).glob("*/review-comment.json")) if directory else []:
        try:
            receipt = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            continue
        if isinstance(receipt, dict):
            receipts.append(receipt)
    return receipts


def _receipt_outcomes(directory: str | None, *, kind: str) -> list[dict]:
    receipts = []
    for path in sorted(Path(directory).glob("*/outcome.json")) if directory else []:
        receipt = json.loads(path.read_text())
        artifact_id = path.parent / "artifact-id"
        if kind == "development_pr":
            receipt["development_pr_artifact_id"] = (
                int(artifact_id.read_text()) if artifact_id.exists() else None
            )
        elif kind == "handoff":
            receipt["handoff_artifact_id"] = (
                int(artifact_id.read_text()) if artifact_id.exists() else None
            )
        elif kind == "ci_rerun":
            receipt["ci_rerun_artifact_id"] = (
                int(artifact_id.read_text()) if artifact_id.exists() else None
            )
        else:
            receipt["provision_run_id"] = path.parent.name
            receipt["provision_artifact_id"] = (
                int(artifact_id.read_text()) if artifact_id.exists() else None
            )
        receipts.append(receipt)
    return receipts


def cmd_reconcile(args: argparse.Namespace) -> int:
    import queue_result

    policy = load_policy()
    root = Path(args.root) if args.root else repository_root()
    now = datetime.now(timezone.utc)
    try:
        migrated = state.apply_pending_text_migration(
            root, policy, now=now.isoformat(), run=state.run_from_env())
    except state.StateError as error:
        print(f"legacy state text migration refused: {error}", file=sys.stderr)
        return 1
    if migrated:
        # Keep the reviewed legacy repair in its own state-only commit. In
        # particular, do not disturb active Work while its worker is running.
        import state_commit
        try:
            changed = state_commit.records_from_checkout(root, "HEAD")
        except state_commit.PublishError as error:
            print(f"legacy state text migration found unrelated mutation: {error}", file=sys.stderr)
            return 1
        if set(changed) != set(migrated):
            unexpected = sorted(set(changed) ^ set(migrated))
            print(
                f"legacy state text migration found unrelated state paths: {unexpected}",
                file=sys.stderr,
            )
            return 1
        report = state_text_migration_report(migrated)
        text = json.dumps(report, indent=2)
        if args.report:
            Path(args.report).write_text(text)
        else:
            print(text)
        return 0
    # A record the lifecycle table does not describe is migrated in its own
    # state-only commit, before anything reads it as ordinary state.
    try:
        migrated = state.apply_pending_record_migrations(
            root, policy, now=now.isoformat(), run=state.run_from_env())
    except state.StateError as error:
        print(f"record migration refused: {error}", file=sys.stderr)
        return 1
    if migrated:
        import state_commit
        try:
            changed = state_commit.records_from_checkout(root, "HEAD")
        except state_commit.PublishError as error:
            print(f"record migration found unrelated mutation: {error}", file=sys.stderr)
            return 1
        if set(changed) != set(migrated):
            unexpected = sorted(set(changed) ^ set(migrated))
            print(f"record migration found unrelated state paths: {unexpected}", file=sys.stderr)
            return 1
        text = json.dumps(migration_report("record_migration", migrated), indent=2)
        if args.report:
            Path(args.report).write_text(text)
        else:
            print(text)
        return 0
    store, snapshot = load_snapshot(root, policy, run=state.run_from_env())
    api = collect.Api()
    publications = _publication_outcomes(args.publications)
    development_prs = _receipt_outcomes(args.development_prs, kind="development_pr")
    ci_reruns = _receipt_outcomes(getattr(args, "ci_reruns", None), kind="ci_rerun")
    provisions = _receipt_outcomes(args.provisions, kind="provision")
    handoffs = _receipt_outcomes(getattr(args, "handoffs", None), kind="handoff")
    handoff_runs = None if args.offline else _handoff_runs(getattr(args, "handoff_runs", None))
    development_pr_runs = None if args.offline else _handoff_runs(
        getattr(args, "development_pr_runs", None), DEVELOPMENT_PR_RUN_PREFIX)
    provision_runs = None if args.offline else _handoff_runs(
        getattr(args, "provision_runs", None), PROVISION_RUN_PREFIX)
    comment_receipts = _review_comment_receipts(getattr(args, "review_comments", None))
    issues = [] if args.offline else collect.intake_issues(api)
    tracking = sorted(
        {record["tracking_issue"]["number"] for record in snapshot.contributions.values()}
        | {issue["number"] for issue in issues}
    )
    comments = [] if args.offline else collect.command_comments(api, tracking, args.since)
    source_snapshots = (
        {}
        if args.offline
        else collect.upstream_snapshots(
            api, snapshot.contributions.values(), issues, policy, comments,
            snapshot.consumed
        )
    )
    issue_snapshots = (
        {}
        if args.offline
        else collect.tracking_issues(api, snapshot.contributions.values(), issues)
    )
    acceptances = (
        {}
        if args.offline
        else collect.label_acceptances(
            api, issues, INTAKE_LABEL, policy["command_authority"]["actor_ids"]
        )
    )
    runs = queue = queue_state = retries = results = None
    if not args.offline and args.gh_aw:
        try:
            runs = collect.dispatcher_runs(api)
            projection = queue_result.queue_projection(args.gh_aw, collect.CONTROL_REPO)
            queue = queue_result.work_states(projection)
            skipped_retries: list[str] = []
            retries = queue_result.work_retries(projection, skipped_retries)
            for line in skipped_retries:
                print(line, file=sys.stderr)
            results = work_result_readings(api, snapshot.contributions, queue,
                                           queue_result.work_runs(projection),
                                           publication_collection(args.publications))
            queue_state = queue_policy_facts.status(
                projection or {}, queue_policy_facts.load_compiled(repository=collect.CONTROL_REPO))
        except (collect.CollectError, OSError, ValueError) as error:
            runs = queue = queue_state = retries = results = None
            print(f"dispatch recovery skipped this tick: {error}", file=sys.stderr)
    if args.offline:
        observed_pressure = capacity.Pressure(ok=False, reason="offline")
    else:
        try:
            usage_snapshot, fetched_at = collect.usage(policy["capacity"]["usage_url"])
            observed_pressure = capacity.pressure(
                policy, usage_snapshot, fetched_at=fetched_at, now=now
            )
        except Exception as error:
            observed_pressure = capacity.Pressure(
                ok=False, reason=f"usage unavailable: {error}"
            )
    try:
        output = tick(
            api,
            snapshot,
            policy=policy,
            now=now,
            run_id=os.environ.get("GITHUB_RUN_ID"),
            pipeline=pipeline_version(),
            publications=publications,
            pressure=observed_pressure,
            issues=issues,
            comments=comments,
            observe=not args.offline,
            dispatcher_runs=runs,
            queue_works=queue,
            queue_retries=retries,
            work_results=results,
            queue_policy=queue_state,
            acceptances=acceptances,
            tracking_issues=issue_snapshots,
            upstream_snapshots=source_snapshots,
            provisions=provisions,
            development_prs=development_prs,
            handoffs=handoffs,
            ci_reruns=ci_reruns,
            handoff_runs=handoff_runs,
            development_pr_runs=development_pr_runs,
            provision_runs=provision_runs,
            review_comment_receipts=comment_receipts,
        )
    except isolation.TickFailure as failure:
        # A pipeline failure: nothing is written, so nothing is committed.
        print(f"reconciliation stopped: {failure}", file=sys.stderr)
        for cause in failure.failures or (failure,):
            traceback.print_exception(cause, file=sys.stderr)
        return 1
    for failure in output.failures:
        # The traceback stays in the workflow log; state and the tracking
        # issue get the bounded description only.
        print(f"contribution not processed in this tick: {failure}", file=sys.stderr)
        traceback.print_exception(failure, file=sys.stderr)
    for kind, record in output.staged:
        store.stage(kind, record)
    output.report["written"] = store.write_staged()
    text = json.dumps(output.report, indent=2, default=str)
    if args.report:
        Path(args.report).write_text(text)
    else:
        print(text)
    # A processing error is charged to one contribution and the tick goes
    # on, so the run succeeds. The annotation keeps a pipeline bug visible
    # on the workflow run. It goes to stdout unless the report does.
    for line in processing_error_annotations(output.report):
        print(line, file=sys.stdout if args.report else sys.stderr)
    return 0


# Bound of one workflow annotation.
MAX_ANNOTATION = 600


def processing_error_annotations(report: dict) -> list[str]:
    """One GitHub Actions ``::error::`` line per processing error of a tick.

    The text is built from the bounded description in the report, cleaned
    again, and escaped as the workflow command format requires, so a message
    can neither span lines nor start another command."""
    lines = []
    for item in report.get("processing_errors") or []:
        text = isolation.clean_text(
            f"processing error for {item.get('contribution')} in {item.get('step')}: "
            f"{item.get('error_class')}: {item.get('message')}; {item.get('decision')}",
            MAX_ANNOTATION)
        text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        lines.append(f"::error::{text}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m reconcile")
    parser.add_argument("--root")
    parser.add_argument("--publications")
    parser.add_argument("--provisions")
    parser.add_argument("--development-prs")
    parser.add_argument("--ci-reruns")
    parser.add_argument("--review-comments")
    parser.add_argument("--handoffs")
    parser.add_argument("--handoff-runs")
    parser.add_argument("--development-pr-runs")
    parser.add_argument("--provision-runs")
    parser.add_argument("--report")
    parser.add_argument("--since")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--gh-aw", help="pinned gh-aw v1.2.2 binary used only to read queue state"
    )
    args = parser.parse_args(argv)
    return cmd_reconcile(args)


if __name__ == "__main__":
    raise SystemExit(main())
