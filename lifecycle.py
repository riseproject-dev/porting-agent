# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Contribution lifecycle: phases, ordinary transitions and command handling.

The lifecycle table at the end of this module (``LIFECYCLE``, ``RULES``,
``FLAGS``, ``BUDGETS`` and ``OPERATIONS``) is the definition of the contribution
state machine. Documentation and tests are derived from it.

Everything here is pure: functions take a record and return a new record plus
the semantic event to persist. They never perform remote effects. The trusted
state publisher persists the outcome; remote writers call ``publication_check``
immediately before an effect.

Revision rule: ``revision`` increments only when requested work changes (head
or base, approved scope/contract, or a human decision affecting work). Polls,
heartbeats and routine CI observations never increment it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NamedTuple

import schemas
from commands import URGENT, Command
from common import DEV_REF, validate_scope_paths

PHASES = (
    "queued", "investigation", "provisioning", "development", "handoff",
    "upstream_review", "release_wait", "released", "parked",
    "no_work", "deferred", "abandoned", "already_released",
)
TERMINAL = frozenset({"released", "no_work", "deferred", "abandoned", "already_released"})
ACTIVITIES = ("idle", "agent_running", "waiting_ci", "fix_needed",
              "waiting_capacity", "waiting_human")

# Ordinary (non-command) phase exits. "parked" and "abandoned" are reachable
# from every non-terminal phase through commands only.
ORDINARY = {
    "queued": {"investigation", "deferred"},
    "investigation": {"provisioning", "no_work", "deferred", "already_released"},
    "provisioning": {"development", "deferred"},
    "development": {"handoff"},
    "handoff": {"upstream_review", "development"},
    "upstream_review": {"release_wait"},
    "release_wait": {"released"},
    "released": set(),
    "parked": set(),
}

# Phases in which automated contribution writes to the fork may happen.
WRITE_PHASES = frozenset({"provisioning", "development", "handoff", "upstream_review"})


class TransitionError(ValueError):
    pass


@dataclass
class Outcome:
    record: dict[str, Any]
    accepted: bool
    reason: str | None
    event: dict[str, Any] | None


def _event(before: dict, after: dict, kind: str, cause_type: str, cause_id: str | None,
           now: str, bumped: bool) -> dict:
    keys = ("phase", "activity", "paused", "revision", "generation",
            "blocked_reason", "parked_reason")
    return {
        "schema_version": 1,
        "contribution_id": after["contribution_id"],
        "sequence": 0,  # assigned by the state store
        "at": now,
        "kind": kind,
        "from": {k: before.get(k) for k in keys if before.get(k) != after.get(k)},
        "to": {k: after.get(k) for k in keys if before.get(k) != after.get(k)},
        "cause": {"type": cause_type, "id": cause_id},
        "revision_bumped": bumped,
    }


def _bump(rec: dict) -> None:
    rec["revision"] += 1


def transition(record: dict, to_phase: str, *, cause_type: str, cause_id: str | None,
               now: str, reason: str | None = None) -> Outcome:
    """Apply an ordinary (observation/result-driven) phase transition."""
    if to_phase not in PHASES:
        raise TransitionError(f"unknown phase {to_phase!r}")
    frm = record["phase"]
    if to_phase not in ORDINARY.get(frm, set()):
        raise TransitionError(f"{frm} -> {to_phase} is not an ordinary transition")
    if record.get("paused") and to_phase not in ("release_wait", "released"):
        # Pause stops new work. Merge/release are observations, not work.
        raise TransitionError("contribution is paused")
    rec = copy.deepcopy(record)
    rec["phase"] = to_phase
    rec["activity"] = "idle"
    rec["updated_at"] = now
    bumped = False
    if to_phase in TERMINAL:
        rec["terminal_reason"] = reason
    if to_phase == "release_wait":
        # Merge ends fork development immediately.
        rec["active_operation_id"] = None
    if frm == "handoff" and to_phase == "development":
        # New contribution changes before submission invalidate approval.
        rec["handoff_approval"] = None
        rec["handoff_request"] = None
        bumped = True
        _bump(rec)
    schemas.validate("contribution", rec)
    return Outcome(rec, True, None, _event(record, rec, f"phase:{to_phase}", cause_type,
                                           cause_id, now, bumped))


def observe_head(record: dict, new_head: str, *, now: str, cause_id: str | None = None) -> Outcome:
    """Record an observed fork head. A changed head is changed requested work:
    bump the revision and invalidate head-bound readiness and approval."""
    fork = record.get("fork") or {}
    if fork.get("head_sha") == new_head:
        return Outcome(record, True, "unchanged", None)
    rec = copy.deepcopy(record)
    rec["fork"]["head_sha"] = new_head
    rec["last_ci_evidence_id"] = None
    if rec.get("upstream_pr") is None:
        rec["handoff_approval"] = None
    rec["updated_at"] = now
    _bump(rec)
    schemas.validate("contribution", rec)
    return Outcome(rec, True, None, _event(record, rec, "head_changed", "observation",
                                           cause_id, now, True))


def observe_status(record: dict, *, activity: str | None = None, next_poll_at: str | None = None,
                   now: str) -> Outcome:
    """Routine observation; never bumps revision, never invalidates work."""
    rec = copy.deepcopy(record)
    if activity is not None:
        if activity not in ACTIVITIES:
            raise TransitionError(f"unknown activity {activity!r}")
        rec["activity"] = activity
    if next_poll_at is not None:
        rec["next_poll_at"] = next_poll_at
    semantic = rec.get("activity") != record.get("activity")
    if semantic:
        rec["updated_at"] = now
    schemas.validate("contribution", rec)
    ev = _event(record, rec, "activity", "observation", None, now, False) if semantic else None
    return Outcome(rec, True, None, ev)


# --------------------------------------------------------------------------
# Commands


def _reject(record: dict, reason: str) -> Outcome:
    return Outcome(record, False, reason, None)


def _restart_generation(record: dict, now: str) -> dict:
    """Reset mutable lifecycle state while retaining identity and replay fences."""
    rec = copy.deepcopy(record)
    rec.update({
        "generation": int(record.get("generation", 0)) + 1, "refresh": None,
        "phase": "queued", "activity": "idle", "paused": False,
        "blocked_reason": None, "parked_reason": None, "terminal_reason": None,
        "previous_phase": None,
        "owners": {"primary": None, "backup": None, "submitter": None,
                   "handoff_reviewer": None},
        "admission_class": None,
        "upstream": {"repository_id": None, "full_name": record["upstream"]["full_name"],
                     "base_ref": None, "base_sha": None},
        "fork": None, "upstream_pr": None, "target_contract_id": None,
        "prerequisite_ids": [], "active_operation_id": None,
        "queue_work_id": None, "queue_claim_handle": None, "queue_result_id": None,
        "proposal_artifact": None, "publication_operation_id": None,
        "pending_publication": None,
        # Keep processed_publications as a replay fence against old run artifacts.
        "processed_review_ids": [], "processed_review_comment_ids": [],
        "processed_pr_conversation_comment_ids": [],
        "pending_feedback_evidence_ids": [], "active_work": None,
        "dossier_evidence_id": None, "scope_coverage_complete": False,
        "scope_paths": None, "debug_commits": [], "clean_head_sha": None,
        "handoff_request": None, "handoff_approval": None,
        "last_ci_evidence_id": None, "merge": None, "retirement": None,
        "release_acceptance": None, "pending_human_action": None,
        "next_poll_at": None, "updated_at": now,
    })
    budget = record["budget"]
    rec["budget"] = {"policy_id": budget["policy_id"], "attempts_used": 0,
                     "no_progress_streak": 0, "clusters": {},
                     "spent_estimate_usd_cents": 0}
    _bump(rec)
    return rec


def park(rec: dict, reason: str) -> dict:
    """End the current generation in place (plan 3.3.4 failure case 2).

    The completed generation is retained for audit and late-effect
    reconciliation; only exact-revision ``resume`` restarts it. Callers own
    revision bumps and the event."""
    rec["previous_phase"] = rec["phase"]
    rec["phase"] = "parked"
    rec["parked_reason"] = reason[:2000]
    rec["activity"] = "idle"
    return rec


def apply_command(record: dict, cmd: Command, *, command_id: str, actor: dict, now: str,
                  verified_pr: dict | None = None, provisioning: dict | None = None,
                  backup_user: dict | None = None) -> Outcome:
    """Apply an already authenticated, not-yet-consumed command.

    Actor authority, replay and digest checks happen in ``intake.accept_command``
    before this is called. ``verified_pr`` is the upstream PR resource as read
    from the GitHub API for ``register-upstream-pr``; the URL alone is never
    sufficient.
    """
    if cmd.verb == "resolve-operation":
        raise TransitionError("resolve-operation applies to operations, see recovery.py")
    if cmd.contribution is not None and cmd.contribution != record["contribution_id"]:
        return _reject(record, "command names another contribution than this issue's")
    rev = cmd.revision
    cur = record["revision"]
    phase = record["phase"]
    if rev is None:
        # Only stop commands may omit it (enforced by the grammar).
        rev = cur
    if rev > cur:
        return _reject(record, "revision is not a known revision")
    # Stopping is always safe, so a stale pause/park/block/abandon is honoured.
    # Everything that (re)activates or approves work must name the current revision.
    if cmd.verb not in ("pause", "block", "park", "abandon") and rev != cur:
        return _reject(record, f"stale revision {rev}, current is {cur}")
    if phase in TERMINAL and cmd.verb not in ("authorize-followup", "restart"):
        return _reject(record, f"contribution is terminal ({phase})")

    rec = copy.deepcopy(record)
    rec["updated_at"] = now
    bumped = False
    v = cmd.verb
    a = cmd.args

    if v == "pause":
        if rec["paused"]:
            return _reject(record, "already paused")
        rec["paused"] = True
    elif v == "block":
        rec["blocked_reason"] = a.get("reason") or "blocked"
        rec["activity"] = "waiting_human"
    elif v == "resume":
        if phase == "parked":
            # Plan 3.3.4 failure case 2 and 3.5: resume from parked needs
            # quiescence and starts a clean generation at queued:idle, so the
            # next generation cannot bypass a complete verified dossier.
            if record.get("active_work"):
                return _reject(record, "cannot resume from parked while Work is active")
            if record.get("active_operation_id") or record.get("pending_publication"):
                return _reject(record, "cannot resume from parked while a writer operation is active")
            rec = _restart_generation(record, now)
            bumped = True
        elif rec["paused"] or rec.get("blocked_reason"):
            rec["paused"] = False
            rec["blocked_reason"] = None
            if rec["activity"] == "waiting_human" and rec.get("pending_human_action") is None:
                rec["activity"] = "idle"
        else:
            return _reject(record, "nothing to resume")
        if not bumped:
            _bump(rec)
            bumped = True
    elif v == "park":
        if phase == "parked":
            return _reject(record, "already parked")
        park(rec, a["reason"])
    elif v == "abandon":
        rec["phase"] = "abandoned"
        rec["terminal_reason"] = a["reason"]
        rec["activity"] = "idle"
    elif v == "restart":
        if phase != "abandoned":
            return _reject(record, f"restart needs phase abandoned, not {phase}")
        if record.get("active_work"):
            return _reject(record, "cannot restart while Work is active")
        if record.get("active_operation_id") or record.get("pending_publication"):
            return _reject(record, "cannot restart while a writer operation is active")
        rec = _restart_generation(record, now)
        bumped = True
    elif v == "refresh":
        if phase not in ("development", "handoff"):
            return _reject(record, f"refresh needs an existing development PR, not phase {phase}")
        fork = record.get("fork") or {}
        if not fork.get("pr_number") or not fork.get("head_sha"):
            return _reject(record, "refresh needs a verified development PR and head")
        if record.get("active_work"):
            return _reject(record, "cannot refresh while Work is active")
        if record.get("active_operation_id") or record.get("pending_publication"):
            return _reject(record, "cannot refresh while a writer operation is active")
        rec["refresh"] = {"command_id": command_id, "reason": a["reason"],
                          "from_phase": phase, "head_sha": fork["head_sha"],
                          "pr_number": fork["pr_number"]}
        rec["phase"] = "queued"
        rec["activity"] = "idle"
        rec["dossier_evidence_id"] = None
        rec["scope_coverage_complete"] = False
        rec["scope_paths"] = None
        rec["proposal_artifact"] = None
        rec["last_ci_evidence_id"] = None
        rec["clean_head_sha"] = None
        rec["handoff_request"] = None
        rec["handoff_approval"] = None
        rec["pending_human_action"] = None
        rec["publication_operation_id"] = None
        _bump(rec)
        bumped = True
    elif v == "request-handoff":
        if phase != "development":
            return _reject(record, f"request-handoff needs phase development, not {phase}")
        if rec["paused"]:
            return _reject(record, "contribution is paused")
        if (rec.get("fork") or {}).get("head_sha") != a["head"]:
            return _reject(record, "head does not match the current fork head")
        if not rec.get("last_ci_evidence_id"):
            return _reject(record, "no executed development CI evidence for this head")
        rec["phase"] = "handoff"
        rec["activity"] = "idle"
        rec["handoff_request"] = {"command_id": command_id, "head": a["head"], "revision": cur}
    elif v == "approve-handoff":
        if phase != "handoff":
            return _reject(record, f"approve-handoff needs phase handoff, not {phase}")
        clean = rec.get("clean_head_sha")
        if not clean:
            return _reject(record, "debug cleanup has not produced a clean head yet")
        if a["head"] != clean or (rec.get("fork") or {}).get("head_sha") != clean:
            return _reject(record, "approval head is not the current clean head")
        if rec.get("debug_commits") and any(d["sha"] == clean for d in rec["debug_commits"]):
            return _reject(record, "clean head is a debug commit")
        rec["handoff_approval"] = {
            "command_id": command_id, "actor": actor, "head": clean, "revision": cur,
            "evidence_ids": [x for x in [rec.get("last_ci_evidence_id")] if x],
        }
        rec["activity"] = "waiting_human"
        rec["pending_human_action"] = {"kind": "open_upstream_pr", "requested_at": now,
                                       "reminded_at": None, "escalated_at": None}
    elif v == "register-upstream-pr":
        if phase != "handoff":
            return _reject(record, f"register-upstream-pr needs phase handoff, not {phase}")
        appr = rec.get("handoff_approval")
        if not appr:
            return _reject(record, "handoff has not been approved")
        if verified_pr is None:
            return _reject(record, "upstream PR resource not verified")
        if verified_pr.get("html_url") != a["url"]:
            return _reject(record, "verified PR does not match the URL")
        if verified_pr.get("base_repository") != rec["upstream"]["full_name"]:
            return _reject(record, "PR is not against the upstream repository")
        if rec["upstream"].get("base_ref") and verified_pr.get("base_ref") != rec["upstream"]["base_ref"]:
            return _reject(record, "PR base ref differs from the approved base")
        if verified_pr.get("head_sha") != appr["head"]:
            return _reject(record, "PR head is not the approved clean head")
        if verified_pr.get("state") != "open":
            return _reject(record, "PR is not open")
        head_repo = verified_pr.get("head_repository")
        rec["upstream_pr"] = {
            "id": verified_pr["id"], "number": verified_pr["number"], "url": a["url"],
            "head_repository_id": verified_pr["head_repository_id"],
            "head_ref": verified_pr["head_ref"], "head_sha": verified_pr["head_sha"],
            "base_ref": verified_pr["base_ref"], "registered_by": actor,
        }
        writable = (rec.get("fork") and head_repo == rec["fork"]["name"]
                    and verified_pr["head_ref"] == DEV_REF)
        if not writable:
            # The human used another branch: prepare patches for them, never
            # silently create another automated branch.
            rec["blocked_reason"] = ("upstream PR head is not "
                                     f"{(rec.get('fork') or {}).get('name')}:{DEV_REF}; "
                                     "automated revisions disabled, patches go to the submitter")
        rec["phase"] = "upstream_review"
        rec["activity"] = "waiting_ci"
        rec["pending_human_action"] = None
    elif v == "clarify":
        if rec["activity"] != "waiting_human" and not rec.get("blocked_reason"):
            return _reject(record, "no pending question")
        rec["activity"] = "idle"
        rec["pending_human_action"] = None
        _bump(rec)
        bumped = True
    elif v == "revise-scope":
        if rec.get("upstream_pr") is None:
            rec["handoff_approval"] = None
        rec["target_contract_id"] = a["proposal"]
        rec["activity"] = "idle"
        _bump(rec)
        bumped = True
    elif v == "force-develop":
        if phase != "parked":
            return _reject(record, f"force-develop needs phase parked, not {phase}")
        paths = [g for g in a["paths"].split(",") if g]
        try:
            validate_scope_paths(paths)
        except ValueError as exc:
            return _reject(record, str(exc))
        rec["phase"] = "provisioning"
        rec["activity"] = "idle"
        rec["parked_reason"] = None
        rec["admission_class"] = a["class"]
        rec["scope_paths"] = paths
        rec["pending_human_action"] = None
        _bump(rec)
        bumped = True
    elif v == "approve-scope":
        if phase != "investigation":
            return _reject(record, f"approve-scope needs phase investigation, not {phase}")
        if rec.get("active_work"):
            return _reject(record, "the investigation is still running")
        if not rec.get("dossier_evidence_id"):
            return _reject(record, "no investigation dossier recorded yet")
        if backup_user is None:
            return _reject(record, f"backup owner {a['backup']!r} is not a known GitHub user")
        if backup_user["id"] == actor["id"]:
            return _reject(record, "the backup owner must be someone else")
        rec["owners"] = dict(rec["owners"], primary=dict(actor), backup=dict(backup_user))
        rec["admission_class"] = a["class"]
        rec["scope_paths"] = [g for g in (a.get("paths") or "").split(",") if g] or None
        rec["phase"] = "provisioning"
        rec["activity"] = "waiting_human"
        rec["pending_human_action"] = {"kind": "provision_fork", "requested_at": now,
                                       "reminded_at": None, "escalated_at": None}
        _bump(rec)
        bumped = True
    elif v == "provision":
        if phase != "provisioning":
            return _reject(record, f"provision needs phase provisioning, not {phase}")
        if not provisioning or provisioning.get("error"):
            return _reject(record, (provisioning or {}).get("error") or "fork not verified")
        if provisioning.get("workflow_verified") is not True:
            return _reject(record, "fork provisioning must complete through the trusted upstreaming workflow; a bare existing fork is not sufficient")
        rec["fork"] = {"repository_id": provisioning["fork_id"], "name": provisioning["fork"],
                       "head_ref": DEV_REF, "head_sha": provisioning["head_sha"], "pr_number": None}
        rec["upstream"] = dict(rec["upstream"], repository_id=provisioning["upstream_id"],
                               base_ref=provisioning["base_ref"], base_sha=provisioning["base_sha"])
        rec["phase"] = "development"
        rec["activity"] = "idle"
        rec["pending_human_action"] = None
        rec["last_ci_evidence_id"] = None
        _bump(rec)
        bumped = True
    elif v == "authorize-followup":
        if phase not in ("release_wait", "released"):
            return _reject(record, "follow-ups need a merged contribution")
        # The completed record is left intact; intake.new_followup creates
        # the new identity. Here we only record the event.
    else:  # pragma: no cover - grammar and this table must agree
        raise TransitionError(f"unhandled verb {v}")

    # Stopping a contribution or moving it to a new revision ends any
    # requested task: a late worker outcome is then ignored as stale, and
    # dispatch recovery must not launch it again.
    if rec.get("active_work") and (rec["paused"] or rec["phase"] in TERMINAL | {"parked"}
                                   or rec["revision"] != record["revision"] or rec.get("blocked_reason")):
        rec["active_work"] = None
        if rec["activity"] == "agent_running":
            rec["activity"] = "idle"

    if rec != record or v == "authorize-followup":
        schemas.validate("contribution", rec)
    kind = f"command:{v}"
    return Outcome(rec, True, None, _event(record, rec, kind, "command", command_id, now, bumped))


def command_priority(cmd: Command) -> int:
    """Urgent stop commands sort first, then other controls."""
    return 0 if cmd.verb in URGENT else 1


# --------------------------------------------------------------------------
# Publication guard


def publication_check(record: dict, *, expected_revision: int, expected_head: str | None,
                      observed_head: str | None, ref: str, repository: str) -> str | None:
    """Re-read immediately before a fork write. Returns None when the write
    may proceed, otherwise the reason it must not. An effect that already
    passed this check may still finish after a later pause (best-effort
    pause, plan section 2); it is reconciled as a normal effect."""
    if record["phase"] not in WRITE_PHASES:
        return f"phase {record['phase']} does not permit fork writes"
    if record.get("paused"):
        return "paused"
    if record.get("blocked_reason"):
        return "blocked"
    if ref != DEV_REF:
        return f"ref {ref!r} is not {DEV_REF!r}"
    fork = record.get("fork") or {}
    if repository != fork.get("name"):
        return "repository is not the recorded contribution fork"
    if record["revision"] != expected_revision:
        return "work revision changed"
    if observed_head != expected_head:
        return "fork head changed since the proposal was made"
    if record.get("active_operation_id"):
        return "another writer operation is active on this fork"
    return None


def new_contribution(*, contribution_id: str, project_id: str, issue: dict, upstream: str,
                     distributions: list[str], base_ref: str | None, policy_id: str,
                     now: str, follows: str | None = None) -> dict:
    rec = {
        "schema_version": 1,
        "contribution_id": contribution_id,
        "project_id": project_id,
        "revision": 0,
        "generation": 0,
        "refresh": None,
        "phase": "queued",
        "activity": "idle",
        "paused": False,
        "blocked_reason": None,
        "parked_reason": None,
        "terminal_reason": None,
        "previous_phase": None,
        "tracking_issue": dict(issue),
        "owners": {"primary": None, "backup": None, "submitter": None, "handoff_reviewer": None},
        "admission_class": None,
        "distributions": distributions,
        "upstream": {"repository_id": None, "full_name": upstream, "base_ref": base_ref,
                     "base_sha": None},
        "fork": None,
        "upstream_pr": None,
        "target_contract_id": None,
        "prerequisite_ids": [],
        "active_operation_id": None,
        "queue_work_id": None,
        "queue_claim_handle": None,
        "queue_result_id": None,
        "proposal_artifact": None,
        "publication_operation_id": None,
        "pending_publication": None,
        "processed_publications": [],
        "processed_review_ids": [],
        "processed_review_comment_ids": [],
        "processed_pr_conversation_comment_ids": [],
        "active_work": None,
        "dossier_evidence_id": None,
        "debug_commits": [],
        "clean_head_sha": None,
        "handoff_request": None,
        "handoff_approval": None,
        "last_ci_evidence_id": None,
        "merge": None,
        "retirement": None,
        "release_acceptance": None,
        "budget": {"policy_id": policy_id, "attempts_used": 0, "no_progress_streak": 0,
                   "clusters": {}, "spent_estimate_usd_cents": 0},
        "pending_human_action": None,
        "follows": follows,
        "next_poll_at": None,
        "created_at": now,
        "updated_at": now,
    }
    schemas.validate("contribution", rec)
    return rec


def proposal_admission(record: dict) -> str | None:
    """Gate uncredentialed patch proposal Work before any fork exists."""
    if not record.get("admission_class"):
        return "admission class not approved"
    if not record.get("dossier_evidence_id"):
        return "no verified dossier"
    if not record.get("scope_paths") or record.get("scope_coverage_complete") is not True:
        return "verified patch scope is incomplete"
    if record["upstream"].get("base_sha") is None:
        return "no immutable upstream base SHA in the dossier"
    return None


def write_admission(record: dict) -> str | None:
    """Gate before any destination write-capable work is submitted."""
    if not record.get("admission_class"):
        return "admission class not approved"
    if not record.get("fork") or not record["fork"].get("repository_id"):
        return "fork not provisioned/verified"
    if record["upstream"].get("base_sha") is None:
        return "no immutable upstream base SHA in the dossier"
    return None


# --------------------------------------------------------------------------
# Lifecycle table
#
# This is the definition of the contribution state machine. ``LIFECYCLE``
# lists every state a contribution can be in, keyed ``phase.activity``, with
# its properties and, in order, the transitions that leave it. Each
# transition is keyed by its verb and the state it leads to, and records its
# trigger, guard, effects on the record and the retry budgets it charges.
# ``RULES`` are transitions that apply in many states; each is written once
# and expanded into ``LIFECYCLE`` when this module is imported, so the table
# holds every row. ``FLAGS`` are the orthogonal overrides (``paused``) that
# do not change phase or activity. ``BUDGETS`` names every retry budget: what
# one charge is, where the count lives, the ``policy.json`` key that sets its
# limit, and the exhaustion verbs that leave it. ``OPERATIONS`` is the table
# of one remote operation, held to the same checks.
#
# ``DIAGRAM_SECTIONS`` is presentation metadata used to render the table as
# diagrams; it selects and orders table entries but adds no transitions.
# ``check_table`` (``python -m lifecycle check``) verifies that the tables
# are consistent.


class Trigger(StrEnum):
    """What causes a transition to be taken."""

    COMMAND = "command"
    OBSERVATION = "observation"
    RESULT = "result"
    OPERATION = "operation"
    TICK = "tick"


TRIGGER_DESCRIPTIONS = {
    Trigger.COMMAND: "An authorized `/upstreaming` command. Commands that activate or approve work "
                     "name the exact current revision; stop commands may name a stale one.",
    Trigger.OBSERVATION: "A read-only observation by the collector: fork branch, CI runs, pull "
                         "requests, review comments, or releases.",
    Trigger.RESULT: "A verified native-queue Result folded by reconciliation. A Result is folded "
                    "only when it matches the current generation, revision, and requested Work; "
                    "any other Result takes `record_late_effect`. Each Result records its token "
                    "usage in the events.",
    Trigger.OPERATION: "A trusted remote operation receipt and its exact readback.",
    Trigger.TICK: "A controller decision without new input: Work allocation, a budget charge "
                  "or exhaustion, or a deadline.",
}

KIND_DESCRIPTIONS = {
    "auto_wait": "Waits for an automatic trigger; the controller acts without a human. A guard "
                 "that does not hold yet (a worker slot, capacity) keeps the contribution here; "
                 "there is no separate capacity wait.",
    "work": "Requested Work or a trusted operation is in progress.",
    "human_wait": "A recorded `pending_human_action` names what a human must do; "
                  "`/upstreaming resume` continues.",
    "gate": "Waits for a human command that starts the next step.",
    "locked": "Write-fenced wait: only the listed exits leave it, and no Work, patch, "
              "or branch write happens while here.",
    "generation_end": "The current generation has ended; the record is kept for audit and "
                      "late-effect reconciliation until a clean restart.",
    "terminal": "No transition leaves this state.",
}

# The identity of requested Work. A Result names all three; a Result that
# differs in any of them is never folded and never published.
WORK_IDENTITY = ("generation", "revision", "Work id")


@dataclass(frozen=True)
class Effects:
    """Record effects of a transition.

    ``revision`` and ``generation`` are ``keep`` or ``bump``; a generation
    bump resets every mutable lifecycle field and keeps identity and replay
    fences. ``work`` is what happens to the requested Work (``active_work``):
    ``keep``, ``clear``, ``allocate`` (new Work for this state) or
    ``reallocate`` (a fresh Work graph for the same task). ``sets`` and
    ``clears`` name record fields; ``field=value`` names the value set.
    ``restarts`` names the budgets whose counter the transition may start
    again: it starts exactly the one named in ``pending_human_action.counter``,
    and none when no counter is named.
    """

    revision: str = "keep"
    generation: str = "keep"
    work: str = "keep"
    sets: tuple[str, ...] = ()
    clears: tuple[str, ...] = ()
    restarts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.revision not in ("keep", "bump") or self.generation not in ("keep", "bump"):
            raise ValueError("revision and generation are keep or bump")
        if self.work not in ("keep", "clear", "allocate", "reallocate"):
            raise ValueError(f"unknown work effect {self.work!r}")


@dataclass(frozen=True)
class Transition:
    """One transition, stored under the ``(verb, target)`` key of its state.

    ``command`` is the `/upstreaming` command that triggers it. ``guard``
    lists every condition that must hold; an unconditional transition states
    ``(ALWAYS,)``. ``charges`` names the ``BUDGETS`` entries one taking of
    this transition charges.
    """

    trigger: Trigger
    command: str | None = None
    guard: tuple[str, ...] = ()
    effects: Effects = field(default_factory=Effects)
    charges: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class State:
    """One state and the transitions that leave it.

    ``launches``: the controller may allocate Work or launch remote effects
    from this state (subject to the flags). ``writes``: automated writers may
    write the contribution fork while the contribution is in this state.
    ``transitions`` maps ``(verb, target)`` to the transition, in order;
    ``target`` is a state key, or ``SELF`` for a transition that keeps the
    state. A verb leads from a state to one target only, so the machine is
    deterministic; several verbs may lead to the same target.
    """

    kind: str
    launches: bool
    writes: bool
    description: str
    transitions: dict[tuple[str, str], Transition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KIND_DESCRIPTIONS:
            raise ValueError(f"unknown state kind {self.kind!r}")


@dataclass(frozen=True)
class Rule:
    """A transition that applies in many states, written once.

    The rule's verb leads to ``target`` (a state key, or ``SELF``) from every
    state whose kind is in ``kinds`` (every kind when empty) and whose key
    matches a pattern in ``states`` (every state when empty), except the
    states matching ``exclude`` that do not also match ``include``.
    ``applies`` says the same in words. ``expand_rules`` adds one row per
    matching state to ``LIFECYCLE``.
    """

    target: str
    transition: Transition
    applies: str
    kinds: tuple[str, ...] = ()
    states: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    include: tuple[str, ...] = ()


@dataclass(frozen=True)
class Flag:
    """An orthogonal override stored in ``field``.

    ``set_by`` and ``cleared_by`` map a verb to its transition; they apply
    from every non-terminal state and keep phase and activity. The one
    exception is ``set_moves``: setting the flag clears the requested Work,
    so a state that holds requested Work is left for the state in which that
    Work is due again. A setter that is not a command records its reason. ``mask`` says what each trigger may still do while
    the flag is set.
    """

    field: str
    description: str
    set_by: dict[str, Transition]
    cleared_by: dict[str, Transition]
    set_moves: dict[str, str]
    mask: dict[Trigger, str]


@dataclass(frozen=True)
class Budget:
    """A retry budget.

    The limit is the value at ``policy_key`` in ``policy.json`` plus
    ``offset`` (``offset=1`` for budgets whose first try is free). ``unit``
    is what one charge is, ``counter`` where the count lives. When the count
    reaches the limit, one of ``exhaustion_verbs`` is taken instead of the
    charging verb, from the charging state or the state the charging verb
    leads to; its guard names the budget. ``infrastructure`` marks a budget
    spent on infrastructure failures: its exhaustion always leads to a human
    wait of the same phase and never ends the generation. Every exhaustion
    that leads to a human wait names its counter in
    ``pending_human_action.counter``, and the resume from that wait starts
    exactly that counter again.
    """

    policy_key: str
    offset: int
    unit: str
    counter: str
    exhaustion_verbs: tuple[str, ...]
    infrastructure: bool = False


@dataclass(frozen=True)
class OperationState:
    """One state of a remote operation and the transitions that leave it.

    ``open``: the effect may still happen or is not yet known, so the
    operation fences its contribution (no park, refresh, or second writer).
    A state that is not open is an end state.
    """

    open: bool
    description: str
    transitions: dict[tuple[str, str], Transition] = field(default_factory=dict)


SELF = "@self"
ALWAYS = "always"  # the guard of an unconditional transition
_C, _O, _R, _P, _K = (Trigger.COMMAND, Trigger.OBSERVATION, Trigger.RESULT,
                 Trigger.OPERATION, Trigger.TICK)
_E = Effects

PARK = "/upstreaming park"
RESUME = "/upstreaming resume"
REFRESH = "/upstreaming refresh"
RESOLVE = "/upstreaming resolve-operation"

INITIAL_STATE = "queued.idle"
INITIAL_VERB = "accept_request"

# Guard conditions that the consistency check reads.
EXACT_REVISION = "exact revision"
ANY_REVISION = "any revision, current or stale"
NOT_PAUSED = "not paused"
QUIESCENT = "quiescent: no open operation and no pending publication"
NO_WORK = "no requested Work"
# A stop is stale when it lasted longer than ``refresh.stale_after_days``,
# counted from the older of ``paused_at`` and
# ``pending_human_action.requested_at``. ``resume_stale`` requires it; every
# other resume from the same states requires the complement, so exactly one
# of them applies.
STALE = "stopped for longer than `refresh.stale_after_days` days"
NOT_STALE = "stopped for at most `refresh.stale_after_days` days"
FLAG_NOT_STALE = ("stopped for at most `refresh.stale_after_days` days, or the phase is not "
                  "provisioning or development")
STALE_PHASES = ("provisioning", "development")
STALE_KEY = "refresh.stale_after_days"
REMINDER_KEY = "human_sla_business_days.reminder"
REMINDER_KINDS = ("gate", "human_wait", "locked")
INFRASTRUCTURE_KEY = "retries.infrastructure_per_head_failure"
EXACT_OPERATION = "the command names this operation"

# Guards on the recorded ``pending_human_action``, as (negated, kinds). The
# resumes of a human wait that are guarded by it are mutually exclusive, and
# together they cover every pending action with which the wait is entered.
PENDING_GUARDS: dict[str, tuple[bool, tuple[str, ...]]] = {}
# The field in which an exhaustion that leads to a human wait names the
# counter that the resume from that wait starts again.
PENDING_COUNTER = "pending_human_action.counter"


def _one_of(kinds: tuple[str, ...]) -> str:
    if len(kinds) < 3:
        return " or ".join(kinds)
    return ", ".join(kinds[:-1]) + f", or {kinds[-1]}"


def pending_is(*kinds: str) -> str:
    """The guard that the pending human action is one of ``kinds``."""
    guard = f"pending_human_action is {_one_of(kinds)}"
    PENDING_GUARDS[guard] = (False, kinds)
    return guard


def pending_is_not(*kinds: str) -> str:
    """The guard that the pending human action is none of ``kinds``."""
    guard = f"pending_human_action is not {_one_of(kinds)}"
    PENDING_GUARDS[guard] = (True, kinds)
    return guard


_REFRESH_EFFECTS = _E(
    revision="bump", sets=("refresh",),
    clears=("dossier_evidence_id", "scope_coverage_complete", "scope_paths", "proposal_artifact",
            "last_ci_evidence_id", "clean_head_sha", "handoff_request", "handoff_approval",
            "pending_human_action", "publication_operation_id"))
_FLAG_FIELDS = ("paused", "paused_at", "paused_reason", "paused_by")

# Transitions shared by several states. The key under each state names the
# verb and the target.
_REFRESH = Transition(
    _C, REFRESH,
    (EXACT_REVISION, QUIESCENT, NO_WORK, "development PR and head recorded"),
    _REFRESH_EFFECTS,
    description="Runs a complete investigation again while keeping the fork and development PR. "
                "Accepted while paused.")
_RETRY_DISPATCH = Transition(
    _K, None,
    ("a dispatcher run ended with the requested Work absent or unclaimed", "dispatch_recovery remains"),
    charges=("dispatch_recovery",),
    description="Launches the dispatcher again for the same committed Work request.")
_EXHAUST_DISPATCH_RECOVERY = Transition(
    _K, None, ("dispatch_recovery exhausted",),
    _E(work="clear", sets=("pending_human_action=dispatch_failed",
                           f"{PENDING_COUNTER}=dispatch_recovery")))
# Guards that decide which transition applies when Work is due. EXCLUSIVE_GUARDS
# lists the combinations that cannot hold together.
WORK_TOTAL_REMAINS = "work_total remains"
WORK_TOTAL_EXHAUSTED = "work_total exhausted"
PATCH_DUE = "no CI evidence for the head and no feedback recorded"
REPAIR_DUE = "CI evidence for the head or feedback recorded"
NO_PROGRESS_REMAINS = "no_progress remains"
REPAIR_BUDGETS_REMAIN = "repair_attempts_cluster and no_progress remain"
REPAIR_BUDGET_EXHAUSTED = ("no_progress is exhausted, or repair_attempts_cluster is exhausted "
                           "and CI evidence for the head or feedback is recorded")
REFRESH_UNUSED = "the automatic refresh is unused in this generation"
REFRESH_USED = "the automatic refresh is used in this generation"
REVISION_BUDGETS_REMAIN = "repair_attempts_cluster, no_progress, and work_total remain"
REVISION_BUDGET_EXHAUSTED = "repair_attempts_cluster, no_progress, or work_total is exhausted"
EXCLUSIVE_GUARDS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    ((WORK_TOTAL_REMAINS,), (WORK_TOTAL_EXHAUSTED,)),
    ((PATCH_DUE,), (REPAIR_DUE,)),
    ((PATCH_DUE, NO_PROGRESS_REMAINS), (REPAIR_BUDGET_EXHAUSTED,)),
    ((REPAIR_BUDGETS_REMAIN,), (REPAIR_BUDGET_EXHAUSTED,)),
    ((REFRESH_UNUSED,), (REFRESH_USED,)),
    ((REVISION_BUDGETS_REMAIN,), (REVISION_BUDGET_EXHAUSTED,)),
)

_ALLOCATE_WORK_RETRY = Transition(
    _K, None,
    ("the Work graph is terminal without a usable Result", "work_recovery and work_total remain"),
    _E(revision="bump", work="reallocate"), ("work_recovery", "work_total"),
    description="A terminal Work graph is never submitted again; a fresh graph is allocated.")
_EXHAUST_WORK_RECOVERY = Transition(
    _K, None,
    ("the Work graph is terminal without a usable Result", "work_recovery or work_total exhausted"),
    _E(work="clear", sets=("pending_human_action=work_failed", PENDING_COUNTER)),
    description="`pending_human_action.counter` names `work_total` when that budget is "
                "exhausted, and `work_recovery` otherwise.")
_SCHEDULE_PUBLISH_RETRY = Transition(
    _R, None,
    ("publication failed with an infrastructure error", "publish_recovery remains"),
    _E(work="clear", sets=("pending_publication",)), ("publish_recovery",),
    "The same verified Result is published again; no new Work is requested.")
_EXHAUST_PUBLISH_RECOVERY = Transition(
    _R, None,
    ("publish_recovery exhausted",),
    _E(sets=("pending_human_action=publication_infrastructure_failure",
            f"{PENDING_COUNTER}=publish_recovery", "pending_publication.exhausted")))
_RECORD_EXPLANATION = Transition(
    _O, None, ("authorized explanation-only inquiry",),
    _E(sets=("processed_review_ids", "processed_review_comment_ids",
            "processed_pr_conversation_comment_ids")),
    description="Answered on the development PR or recorded without requesting a change; "
                "revision and CI evidence are kept.")
_INTERRUPTED = "a required CI job on the exact head was cancelled or lost its runner"
_RERUN_INTERRUPTED_CI = Transition(
    _O, None, (_INTERRUPTED, "interrupted_reruns remains"),
    charges=("interrupted_reruns",),
    description="The collector reruns the interrupted jobs on the exact head. It reruns for no "
                "other conclusion.")
_EXHAUST_INTERRUPTED_RERUNS = Transition(
    _O, None, (_INTERRUPTED, "interrupted_reruns exhausted"),
    _E(sets=("pending_human_action=ci_interrupted", f"{PENDING_COUNTER}=interrupted_reruns")))
_RESUME_CI = _E(revision="bump", clears=("pending_human_action",), restarts=("interrupted_reruns",))
_RESUME_CI_NOTE = ("Reruns the interrupted jobs on the exact head and starts `interrupted_reruns` "
                   "again for the head.")
_RETURN_FOR_REPAIR = _E(revision="bump", clears=("clean_head_sha", "handoff_request", "pending_human_action"))
_RESUME_EFFECTS = _E(revision="bump", clears=("pending_human_action",))
_RESTART_NOTE = ("When an exhausted budget caused the wait, the resume starts again the one "
                 "counter named in `pending_human_action.counter`, and no other. The event "
                 "records the actor, the counter, and its count at that point.")


def _resume_restarting(*budgets: str) -> Effects:
    """The effects of the resume of a human wait that one exhausted budget caused."""
    return _E(revision="bump", clears=("pending_human_action",), restarts=budgets)
_ADMISSION = ("verified dossier decision develop, or verified dossier decision park and a "
              "recorded admission_override")


LIFECYCLE: dict[str, State] = {
    "queued.idle": State(
        "auto_wait", True, False,
        "An accepted request is recorded; investigation has not started.",
        transitions={
            ("begin_investigation", "investigation.idle"): Transition(_K, None, (NOT_PAUSED,)),
            ("withdraw_request", "closed.idle"): Transition(
                _O, None,
                ("intake label removed", "no Work requested", "no dossier", "no fork"),
                _E(sets=("terminal_reason=withdrawn",))),
        }),
    "investigation.idle": State(
        "auto_wait", True, False,
        "Investigation is due; the controller allocates investigation Work when a worker slot is free.",
        transitions={
            ("dispatch_investigation", "investigation.agent_running"): Transition(
                _K, None,
                ("worker slot free", WORK_TOTAL_REMAINS, NOT_PAUSED, "no open operation"),
                _E(work="allocate"), ("work_total",)),
            ("exhaust_work_total", "investigation.waiting_human"): Transition(
                _K, None, (WORK_TOTAL_EXHAUSTED,),
                _E(sets=("pending_human_action=work_budget_exhausted",
                        f"{PENDING_COUNTER}=work_total"))),
        }),
    "investigation.agent_running": State(
        "work", False, False,
        "Investigation Work is requested or running and will return a dossier.",
        transitions={
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "investigation.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "investigation.waiting_human"): _EXHAUST_WORK_RECOVERY,
            ("reject_dossier", "investigation.idle"): Transition(
                _R, None,
                ("the Result has no dossier, the dossier is invalid, or its decision is not "
                 "develop, park, or close",
                 "investigation_attempts remains"),
                _E(revision="bump", work="clear"), ("investigation_attempts",),
                "Trusted validation rejects the Result; a new investigation is due."),
            ("exhaust_investigation_recovery", "investigation.waiting_human"): Transition(
                _R, None,
                ("the Result has no usable dossier", "investigation_attempts exhausted"),
                _E(work="clear", sets=("pending_human_action=investigation_failed",
                                       f"{PENDING_COUNTER}=investigation_attempts"))),
            ("admit_development", "provisioning.idle"): Transition(
                _R, None,
                (_ADMISSION,
                 "no refresh recorded, or the recorded fork or development PR no longer matches"),
                _E(revision="bump", work="clear",
                  sets=("dossier_evidence_id", "admission_class", "scope_paths"),
                  clears=("refresh", "admission_override"))),
            ("readmit_development", "development.fix_needed"): Transition(
                _R, None,
                (_ADMISSION,
                 "refresh recorded", "the recorded fork and development PR still match"),
                _E(revision="bump", work="clear",
                  sets=("dossier_evidence_id", "admission_class", "scope_paths"),
                  clears=("refresh", "admission_override")),
                description="A refreshed investigation returns to the existing fork for repair."),
            ("park_investigation", "parked.idle"): Transition(
                _R, None,
                ("verified dossier decision park", "no admission_override recorded"),
                _E(work="clear", sets=("parked_reason", "previous_phase"))),
            ("close_investigation", "closed.idle"): Transition(
                _R, None,
                ("verified dossier decision close",),
                _E(work="clear", sets=("terminal_reason",)),
                description="A recorded `admission_override` is not applied against a decision close; the "
                "reply states so."),
        }),
    "investigation.waiting_human": State(
        "human_wait", False, False,
        "Investigation recovery or a budget is exhausted; a human corrects the cause and resumes.",
        transitions={
            ("resume_investigation", "investigation.idle"): Transition(
                _C, RESUME,
                (EXACT_REVISION, QUIESCENT),
                _resume_restarting("work_total", "dispatch_recovery", "work_recovery",
                                   "investigation_attempts"),
                description=_RESTART_NOTE),
        }),
    "provisioning.idle": State(
        "work", True, True,
        "The fork provisioning operation is planned or running through the trusted workflow.",
        transitions={
            ("start_operation", SELF): Transition(
                _K, None,
                ("fork_provision operation planned", "capacity available", "operation_launch remains",
                 NOT_PAUSED),
                charges=("operation_launch",),
                description="Launches the planned operation; an uncertain launch is never repeated "
                            "without discovery."),
            ("require_provisioning_intervention", "provisioning.waiting_human"): Transition(
                _P, None,
                ("a prerequisite cannot be satisfied, an uncertain operation has no discovery, "
                 "or operation_launch is exhausted",),
                _E(sets=("pending_human_action", PENDING_COUNTER)),
                description="`pending_human_action.counter` names `operation_launch` when that "
                            "budget is exhausted, and no counter otherwise."),
            ("confirm_provisioning", "development.fix_needed"): Transition(
                _P, None,
                ("the receipt matches the operation", "exact fork and ref readback"),
                _E(sets=("fork", "upstream.repository_id", "upstream.base_ref",
                        "upstream.base_sha"))),
        }),
    "provisioning.waiting_human": State(
        "human_wait", False, False,
        "Provisioning cannot continue safely; a human corrects the prerequisite and resumes, "
        "or parks.",
        transitions={
            ("resume_provisioning", "provisioning.idle"): Transition(
                _C, RESUME,
                (EXACT_REVISION, "the prerequisite is corrected", NOT_STALE),
                _resume_restarting("operation_launch"), description=_RESTART_NOTE),
        }),
    "development.agent_running": State(
        "work", False, True,
        "Patch Work is requested or running, or its verified Result is being published.",
        transitions={
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "development.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "development.waiting_human"): _EXHAUST_WORK_RECOVERY,
            ("record_publish_success", "development.waiting_ci"): Transition(
                _R, None,
                ("the Result is published, or uncertain and later confirmed",),
                _E(work="clear", sets=("proposal_artifact", "publication_operation_id"))),
            ("reject_patch_proposal", "development.fix_needed"): Transition(
                _R, None,
                ("the proposal is rejected for the current revision, the worker reports it is "
                 "blocked, or the worker reports a failure not caused by the patch and "
                 "flaky_reruns is exhausted",
                 "no_progress remains"),
                _E(revision="bump", work="clear", clears=("clean_head_sha",)), ("no_progress",)),
            ("replan_development", "development.fix_needed"): Transition(
                _R, None,
                ("the proposal no longer applies to the fork head",),
                _E(revision="bump", work="clear")),
            ("request_ci_rerun", "development.waiting_ci"): Transition(
                _R, None,
                ("the worker reports a failure not caused by the patch", "flaky_reruns remains"),
                _E(work="clear", clears=("last_ci_evidence_id",)), ("flaky_reruns",),
                "The repair worker classifies the failure; the failed jobs are rerun on the exact "
                "head, and the head and revision are kept."),
            ("schedule_publish_retry", SELF): _SCHEDULE_PUBLISH_RETRY,
            ("exhaust_publish_recovery", "development.waiting_human"): _EXHAUST_PUBLISH_RECOVERY,
        }),
    "development.waiting_ci": State(
        "auto_wait", False, True,
        "A patch is published to the development PR; CI and review evaluate the exact head. "
        "Passing CI that does not yet satisfy `nominate_for_handoff` keeps the contribution here.",
        transitions={
            ("record_ci_failure", "development.fix_needed"): Transition(
                _O, None,
                ("an executed blocking failure on the exact head",),
                _E(sets=("last_ci_evidence_id",))),
            ("rerun_interrupted_ci", SELF): _RERUN_INTERRUPTED_CI,
            ("exhaust_interrupted_reruns", "development.waiting_human"): _EXHAUST_INTERRUPTED_RERUNS,
            ("record_actionable_review", "development.fix_needed"): Transition(
                _O, None,
                ("authorized actionable review feedback",),
                _E(revision="bump", sets=("pending_feedback_evidence_ids",))),
            ("record_explanation", SELF): _RECORD_EXPLANATION,
            ("record_head_change", SELF): Transition(
                _O, None,
                ("the observed fork head differs from the recorded head",),
                _E(revision="bump", sets=("fork.head_sha",), clears=("last_ci_evidence_id",))),
            ("record_pr_mismatch", "development.waiting_human"): Transition(
                _O, None,
                ("the observed development PR does not match the recorded fork, ref, or PR",),
                _E(sets=("pending_human_action=development_pr_mismatch",))),
            ("nominate_for_handoff", "handoff.awaiting_submission"): Transition(
                _O, None,
                ("every required CI job executed and passed on the exact head",
                 "no pending actionable feedback", "no requested Work"),
                _E(sets=("last_ci_evidence_id", "handoff_request",
                        "pending_human_action=submit_upstream"))),
            ("refresh_investigation", "queued.idle"): _REFRESH,
        }),
    "development.fix_needed": State(
        "auto_wait", True, True,
        "Development Work is due: the fork is ready for a first patch, or a CI failure, "
        "actionable review, rejected proposal, refreshed dossier, or a resume needs a bounded "
        "repair.",
        transitions={
            ("request_patch", "development.agent_running"): Transition(
                _K, None,
                (PATCH_DUE, "proposal or write admission", "worker slot free",
                 "capacity available", NO_PROGRESS_REMAINS, WORK_TOTAL_REMAINS, NOT_PAUSED),
                _E(work="allocate"), ("work_total",)),
            ("request_repair", "development.agent_running"): Transition(
                _K, None,
                (REPAIR_DUE, "write admission", "worker slot free", "capacity available",
                 REPAIR_BUDGETS_REMAIN, WORK_TOTAL_REMAINS, NOT_PAUSED),
                _E(work="allocate"), ("repair_attempts_cluster", "work_total")),
            ("refresh_on_exhaustion", "queued.idle"): Transition(
                _K, None,
                (REPAIR_BUDGET_EXHAUSTED, WORK_TOTAL_REMAINS, REFRESH_UNUSED, NOT_PAUSED),
                _E(revision="bump", sets=("refresh", "automatic_refresh_used"),
                  clears=(*_REFRESH_EFFECTS.clears, "budget.clusters", "budget.no_progress_streak")),
                description="One automatic refresh per generation: a complete investigation runs "
                            "again on the existing fork. The per-job and no-progress counters "
                            "start again; `work_total` does not."),
            ("exhaust_repair_attempts", "parked.idle"): Transition(
                _K, None,
                (REPAIR_BUDGET_EXHAUSTED, WORK_TOTAL_REMAINS, REFRESH_USED),
                _E(work="clear", sets=("parked_reason", "previous_phase")),
                description="Parks for `repair_attempts_cluster` and `no_progress` only, after "
                            "`refresh_on_exhaustion` is used. Exhaustion of `work_total` takes "
                            "`exhaust_work_total` first."),
            ("exhaust_work_total", "development.waiting_human"): Transition(
                _K, None, (WORK_TOTAL_EXHAUSTED,),
                _E(sets=("pending_human_action=work_budget_exhausted",
                        f"{PENDING_COUNTER}=work_total")),
                description="The reply states the number of Work allocations spent in this "
                            "generation and names both `/upstreaming resume`, which starts "
                            "`work_total` again, and `/upstreaming park`."),
            ("refresh_investigation", "queued.idle"): _REFRESH,
        }),
    "development.waiting_human": State(
        "human_wait", False, False,
        "Development recovery or `work_total` is exhausted, the development PR does not match, "
        "or CI on the head was interrupted repeatedly; a human corrects the cause and resumes.",
        transitions={
            ("resume_ci", "development.waiting_ci"): Transition(
                _C, RESUME,
                (EXACT_REVISION, pending_is("ci_interrupted"), QUIESCENT, NOT_STALE),
                _RESUME_CI, description=_RESUME_CI_NOTE),
            ("resume_development", "development.fix_needed"): Transition(
                _C, RESUME,
                (EXACT_REVISION, pending_is_not("ci_interrupted"), QUIESCENT, NOT_STALE),
                _E(revision="bump", clears=("pending_human_action", "pending_publication"),
                  restarts=("dispatch_recovery", "work_recovery", "work_total", "publish_recovery")),
                description="Fresh Work is requested; a Result held for publication is not "
                            "reused. " + _RESTART_NOTE),
            ("refresh_investigation", "queued.idle"): _REFRESH,
        }),
    "handoff.awaiting_submission": State(
        "gate", False, False,
        "Required CI passed on the exact head; a human may request upstream submission.",
        transitions={
            ("submit_upstream", "handoff.cleaning_debug"): Transition(
                _C, "/upstreaming submit-upstream",
                (EXACT_REVISION, "exact candidate head", NOT_PAUSED),
                _E(sets=("handoff_approval", "active_operation_id"), clears=("pending_human_action",)),
                description="The only submission command; it authorizes the whole deterministic "
                            "handoff preparation."),
            ("return_for_repair", "development.fix_needed"): Transition(
                _O, None,
                ("an executed blocking failure or actionable review on the candidate head",),
                _RETURN_FOR_REPAIR),
            ("record_head_change", "development.waiting_ci"): Transition(
                _O, None,
                ("the observed fork head differs from the candidate head",),
                _E(revision="bump", sets=("fork.head_sha",),
                  clears=("last_ci_evidence_id", "handoff_request", "pending_human_action"))),
            ("refresh_investigation", "queued.idle"): _REFRESH,
        }),
    "handoff.cleaning_debug": State(
        "work", False, True,
        "Debug commits are removed from the admitted series and the clean head is published under lease.",
        transitions={
            ("retry_debug_cleanup", SELF): Transition(
                _P, None,
                ("the lease write or its readback failed with an infrastructure error",
                 "debug_cleanup remains"),
                charges=("debug_cleanup",)),
            ("confirm_clean_head", "handoff.waiting_ci"): Transition(
                _P, None,
                ("the lease write and ref readback match",),
                _E(sets=("clean_head_sha", "fork.head_sha"))),
            ("return_for_repair", "development.fix_needed"): Transition(
                _P, None,
                ("the cleanup is invalid, or the exact-head lease conflicts",),
                _RETURN_FOR_REPAIR),
            ("exhaust_debug_cleanup_retries", "handoff.waiting_human"): Transition(
                _P, None,
                ("debug_cleanup exhausted",),
                _E(sets=("pending_human_action=debug_cleanup_failed",
                        f"{PENDING_COUNTER}=debug_cleanup"))),
        }),
    "handoff.waiting_ci": State(
        "auto_wait", False, False,
        "Required CI runs on the clean head.",
        transitions={
            ("accept_clean_head_ci", "handoff.closing_internal_pr"): Transition(
                _O, None,
                ("every required CI job executed and passed on the clean head",),
                _E(sets=("last_ci_evidence_id",))),
            ("rerun_interrupted_ci", SELF): _RERUN_INTERRUPTED_CI,
            ("exhaust_interrupted_reruns", "handoff.waiting_human"): _EXHAUST_INTERRUPTED_RERUNS,
            ("return_for_repair", "development.fix_needed"): Transition(
                _O, None,
                ("a blocking CI failure on the clean head, or the observed fork head differs "
                 "from the clean head",),
                _RETURN_FOR_REPAIR),
        }),
    "handoff.closing_internal_pr": State(
        "work", False, True,
        "The development PR is closed and the closure is read back.",
        transitions={
            ("retry_internal_pr_closure", SELF): Transition(
                _P, None,
                ("the close or its readback failed", "internal_pr_closure remains"),
                charges=("internal_pr_closure",)),
            ("confirm_internal_pr_closed", "handoff.preparing_submission"): Transition(
                _P, None,
                ("the readback shows the exact development PR closed",)),
            ("exhaust_internal_pr_retries", "handoff.waiting_human"): Transition(
                _P, None,
                ("internal_pr_closure exhausted",),
                _E(sets=("pending_human_action=internal_pr_closure_failed",
                        f"{PENDING_COUNTER}=internal_pr_closure"))),
        }),
    "handoff.preparing_submission": State(
        "work", False, False,
        "The submission package (title, body, prefilled compare link) is posted to the tracking issue.",
        transitions={
            ("retry_submission_comment", SELF): Transition(
                _P, None,
                ("posting the package failed", "submission_comment remains"),
                charges=("submission_comment",)),
            ("confirm_submission_package", "handoff.waiting_upstream_pr"): Transition(
                _P, None,
                ("the package comment is read back",),
                _E(sets=("pending_human_action=open_upstream_pr", "handoff_request"))),
            ("exhaust_comment_retries", "handoff.waiting_human"): Transition(
                _P, None,
                ("submission_comment exhausted",),
                _E(sets=("pending_human_action=submission_comment_failed",
                        f"{PENDING_COUNTER}=submission_comment"))),
        }),
    "handoff.waiting_human": State(
        "human_wait", False, False,
        "A handoff step failed repeatedly: publishing the clean head, CI on the clean head, "
        "closing the development PR, or posting the submission package. A human corrects it and "
        "resumes; the resume returns to the step named by `pending_human_action` and starts "
        "again the counter named in `pending_human_action.counter`.",
        transitions={
            ("resume_debug_cleanup", "handoff.cleaning_debug"): Transition(
                _C, RESUME, (EXACT_REVISION, pending_is("debug_cleanup_failed")),
                _resume_restarting("debug_cleanup")),
            ("resume_ci", "handoff.waiting_ci"): Transition(
                _C, RESUME, (EXACT_REVISION, pending_is("ci_interrupted")), _RESUME_CI,
                description=_RESUME_CI_NOTE),
            ("resume_internal_pr_closure", "handoff.closing_internal_pr"): Transition(
                _C, RESUME, (EXACT_REVISION, pending_is("internal_pr_closure_failed")),
                _resume_restarting("internal_pr_closure")),
            ("resume_submission_preparation", "handoff.preparing_submission"): Transition(
                _C, RESUME, (EXACT_REVISION, pending_is("submission_comment_failed")),
                _resume_restarting("submission_comment")),
        }),
    "handoff.waiting_upstream_pr": State(
        "locked", False, False,
        "Waiting for a human to open the upstream PR from the package. Exactly three exits leave "
        "it: `verify_upstream_pr`, `retract_submission`, and `refresh_investigation`.",
        transitions={
            ("record_audit_observation", SELF): Transition(
                _O, None,
                ("an observation that does not satisfy verify_upstream_pr",),
                description="Recorded for audit only; it never changes phase or activity."),
            ("verify_upstream_pr", "upstream_review.waiting_ci"): Transition(
                _O, None,
                ("an open PR against the upstream repository and the frozen base",
                 "its head is the controlled fork ref at the exact clean head",
                 "a compatible human author"),
                _E(sets=("upstream_pr",), clears=("pending_human_action",))),
            ("retract_submission", "development.fix_needed"): Transition(
                _C, "/upstreaming retract-pr",
                (EXACT_REVISION, "exact clean head", QUIESCENT),
                _E(revision="bump", clears=("pending_human_action", "handoff_request", "handoff_approval")),
                description="Stops reminders and unlocks branch work."),
            ("refresh_investigation", "queued.idle"): Transition(
                _C, REFRESH,
                (EXACT_REVISION, "exact clean head", QUIESCENT),
                _REFRESH_EFFECTS,
                description="Returns to a complete investigation while keeping the fork. "
                            "Accepted while paused."),
        }),
    "upstream_review.waiting_ci": State(
        "auto_wait", False, False,
        "The upstream PR is open; upstream CI, review, and merge are observed. Upstream CI is "
        "never rerun by the pipeline.",
        transitions={
            ("record_upstream_ci_failure", "upstream_review.fix_needed"): Transition(
                _O, None,
                ("an executed blocking failure on the upstream PR head",
                 "not yet recorded for this head and CI run"),
                _E(sets=("last_ci_evidence_id",)),
                description="Fires once per head and CI run, so a failure that was already recorded does not "
                "request a revision again after a resume."),
            ("record_actionable_review", "upstream_review.fix_needed"): Transition(
                _O, None,
                ("authorized actionable review feedback",),
                _E(revision="bump", sets=("pending_feedback_evidence_ids",))),
            ("record_explanation", SELF): Transition(
                _O, None, ("authorized explanation-only inquiry",),
                _E(sets=("processed_review_ids", "processed_review_comment_ids",
                        "processed_pr_conversation_comment_ids")),
                description="Recorded and surfaced on the tracking issue; nothing is posted on the "
                            "upstream PR. Revision and CI evidence are kept."),
            ("record_head_change", "upstream_review.waiting_human"): Transition(
                _O, None,
                ("the upstream PR head changed and the pipeline did not publish the change",),
                _E(revision="bump",
                  sets=("upstream_pr.head_sha", "pending_human_action=upstream_head_changed"),
                  clears=("last_ci_evidence_id",))),
            ("record_pr_closed_unmerged", "upstream_review.waiting_human"): Transition(
                _O, None,
                ("the upstream PR is closed without merge",),
                _E(sets=("pending_human_action=pr_closed_unmerged",))),
            ("record_merge", "release_wait.idle"): Transition(
                _O, None,
                ("the upstream PR is merged",),
                _E(sets=("merge",), clears=("active_operation_id",))),
        }),
    "upstream_review.fix_needed": State(
        "auto_wait", True, True,
        "Upstream CI or actionable review needs a revision of the upstream PR head.",
        transitions={
            ("request_upstream_revision", "upstream_review.agent_running"): Transition(
                _K, None,
                ("the upstream PR head is the controlled fork ref", "worker slot free",
                 REVISION_BUDGETS_REMAIN, NOT_PAUSED),
                _E(work="allocate"), ("repair_attempts_cluster", "work_total")),
            ("exhaust_repair_attempts", "upstream_review.waiting_human"): Transition(
                _K, None,
                (REVISION_BUDGET_EXHAUSTED,),
                _E(sets=("pending_human_action=repair_budget_exhausted", PENDING_COUNTER)),
                description="`pending_human_action.counter` names the counter that is exhausted."),
        }),
    "upstream_review.agent_running": State(
        "work", False, True,
        "Revision Work for the upstream PR head is requested or running, or its verified Result "
        "is being published.",
        transitions={
            ("record_revision_publish_success", "upstream_review.waiting_ci"): Transition(
                _R, None,
                ("the revision is published to the upstream PR head",),
                _E(work="clear", sets=("proposal_artifact", "publication_operation_id"))),
            ("reject_patch_proposal", "upstream_review.fix_needed"): Transition(
                _R, None,
                ("the proposal is rejected for the current revision, or the worker reports it is "
                 "blocked",
                 "no_progress remains"),
                _E(revision="bump", work="clear"), ("no_progress",)),
            ("require_reviewer_answer", "upstream_review.waiting_human"): Transition(
                _R, None,
                ("the worker reports that no change is needed",),
                _E(work="clear", sets=("pending_human_action=answer_reviewer",)),
                description="A human answers the reviewer; the pipeline posts nothing on the "
                            "upstream PR."),
            ("schedule_publish_retry", SELF): _SCHEDULE_PUBLISH_RETRY,
            ("exhaust_publish_recovery", "upstream_review.waiting_human"): _EXHAUST_PUBLISH_RECOVERY,
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "upstream_review.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "upstream_review.waiting_human"): _EXHAUST_WORK_RECOVERY,
        }),
    "upstream_review.waiting_human": State(
        "human_wait", False, False,
        "The upstream PR was closed unmerged, its head changed, a reviewer needs an answer, or "
        "recovery or a budget is exhausted; a human triages.",
        transitions={
            ("resume_upstream_review", "upstream_review.waiting_ci"): Transition(
                _C, RESUME,
                (EXACT_REVISION,
                 pending_is("pr_closed_unmerged", "answer_reviewer", "upstream_head_changed"),
                 "for pr_closed_unmerged, the upstream PR is observed open again"),
                _E(revision="bump", clears=("pending_human_action", "pending_feedback_evidence_ids")),
                description="No revision Work is requested. The pending feedback is cleared when "
                            "the pending action is `answer_reviewer`, because a human answered "
                            "it; the other two pending actions hold none."),
            ("resume_upstream_revision", "upstream_review.fix_needed"): Transition(
                _C, RESUME,
                (EXACT_REVISION,
                 pending_is("dispatch_failed", "work_failed", "publication_infrastructure_failure",
                            "repair_budget_exhausted")),
                _E(revision="bump", clears=("pending_human_action", "pending_publication"),
                  restarts=("repair_attempts_cluster", "no_progress", "work_total",
                            "dispatch_recovery", "work_recovery", "publish_recovery")),
                description="Only when revision Work failed: dispatch, Work, publication, or a "
                            "budget. " + _RESTART_NOTE),
        }),
    "release_wait.idle": State(
        "auto_wait", False, False,
        "The upstream PR is merged; official release coverage is observed.",
        transitions={
            ("record_release_incomplete", SELF): Transition(
                _O, None,
                ("release evidence does not yet satisfy the acceptance contract",)),
            ("require_release_intervention", "release_wait.waiting_human"): Transition(
                _O, None,
                ("release evidence needs operator action",), _E(sets=("pending_human_action",))),
            ("verify_release", "released.idle"): Transition(
                _O, None,
                ("verified release coverage satisfies the acceptance contract",),
                _E(sets=("release_acceptance",))),
        }),
    "release_wait.waiting_human": State(
        "human_wait", False, False,
        "Release evidence needs operator action.",
        transitions={
            ("resume_release_observation", "release_wait.idle"): Transition(
                _C, RESUME, (EXACT_REVISION,), _RESUME_EFFECTS),
        }),
    "released.idle": State(
        "terminal", False, False,
        "Verified official release coverage satisfies the acceptance contract."),
    "parked.idle": State(
        "generation_end", False, False,
        "The generation ended without success; a clean restart is possible.",
        transitions={
            ("restart_generation", "queued.idle"): Transition(
                _C, RESUME,
                (EXACT_REVISION, QUIESCENT, "no open upstream PR"),
                _E(revision="bump", generation="bump", work="clear", sets=("admission_override",)),
                description="Starts a clean generation that runs the complete investigation; "
                            "identity, events, evidence, and replay fences are kept. An optional "
                            "class, paths, and reason are stored as `admission_override`."),
            ("abandon", "closed.idle"): Transition(
                _C, "/upstreaming abandon",
                ("a reason is given", ANY_REVISION),
                _E(sets=("terminal_reason=abandoned",))),
        }),
    "closed.idle": State(
        "terminal", False, False,
        "No further work: withdrawn, not applicable, already satisfied, or abandoned. The reason "
        "is `terminal_reason`."),
}

# Transitions that apply in many states, keyed by verb.
RULES: dict[str, Rule] = {
    "park_contribution": Rule(
        "parked.idle",
        Transition(
            _C, PARK,
            ("a reason is given", ANY_REVISION, "no open operation"),
            _E(work="clear", sets=("parked_reason", "previous_phase")),
            description="Ends the current generation; evidence is kept for audit. Accepted "
                        "while paused."),
        "every non-terminal state except `parked.idle`, `handoff.waiting_upstream_pr`, "
        "`release_wait.*`, and `upstream_review.*` other than `upstream_review.waiting_human`",
        kinds=("auto_wait", "work", "human_wait", "gate", "locked"),
        exclude=("handoff.waiting_upstream_pr", "release_wait.*", "upstream_review.*"),
        include=("upstream_review.waiting_human",)),
    "resume_stale": Rule(
        "queued.idle",
        Transition(
            _C, RESUME,
            (EXACT_REVISION, "paused, or waiting for a human", STALE, QUIESCENT, NO_WORK),
            _E(revision="bump", sets=_REFRESH_EFFECTS.sets,
              clears=(*_REFRESH_EFFECTS.clears, *_FLAG_FIELDS),
              restarts=("operation_launch", "interrupted_reruns", "dispatch_recovery",
                        "work_recovery", "work_total", "publish_recovery")),
            description="After a long stop the dossier is refreshed: a complete investigation "
                        "runs again while the fork and development PR are kept. The stop is "
                        "counted from the older of `paused_at` and "
                        "`pending_human_action.requested_at`. While an operation is open no "
                        "resume applies, and the rejection names the open operation. From a "
                        "human wait that an exhausted budget caused, it starts again the "
                        "counter named in `pending_human_action.counter`, like every resume."),
        "every `provisioning.*` and `development.*` state",
        states=tuple(f"{phase}.*" for phase in STALE_PHASES)),
    "record_reminder": Rule(
        SELF,
        Transition(
            _K, None,
            ("at least `human_sla_business_days.reminder` business days since the request or the "
             "last reminder", NOT_PAUSED),
            _E(sets=("pending_human_action.reminded_at",)),
            description="Posts an append-only reminder of the pending human step. In "
                        "`handoff.waiting_upstream_pr` the reminder text carries the package "
                        "link and the exact head, and states when an observed PR does not match."),
        "every state of kind `gate`, `human_wait`, or `locked`",
        kinds=REMINDER_KINDS),
    "record_late_effect": Rule(
        SELF,
        Transition(
            _R, None,
            ("the Result does not match the current generation, revision, and requested Work",),
            description="Recorded in the events for audit. The Result is never folded, the "
                        "publisher refuses it, and the state never changes."),
        "every state"),
}


def rule_states(verb: str) -> tuple[str, ...]:
    """The states a rule applies in, in table order."""
    from fnmatch import fnmatchcase

    rule = RULES[verb]

    def matches(key: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatchcase(key, p) for p in patterns)

    return tuple(
        key for key, state in LIFECYCLE.items()
        if (not rule.kinds or state.kind in rule.kinds)
        and (not rule.states or matches(key, rule.states))
        and (not matches(key, rule.exclude) or matches(key, rule.include)))


def expand_rules() -> None:
    """Add one row per matching state for every rule, after the state's own rows."""
    for verb, rule in RULES.items():
        for key in rule_states(verb):
            transitions = LIFECYCLE[key].transitions
            if any(v == verb for v, _to in transitions):
                raise ValueError(f"{key} already has a transition {verb}")
            transitions[(verb, SELF if rule.target == key else rule.target)] = rule.transition


expand_rules()


FLAGS: dict[str, Flag] = {
    "paused": Flag(
        "paused",
        "A stop of new Work and writer admission, with its reason and who set it (a human or "
        "the controller) in `paused_reason` and `paused_by`. `pause` accepts an optional reason; "
        "a pause set by the controller always records one. Setting it clears the requested "
        "Work and records `paused_at`; phase, activity, evidence, and pending feedback are kept. "
        "The one exception: `pause` in an `agent_running` state leads to the state in which the "
        "Work is due again, `investigation.idle`, `development.fix_needed`, or "
        "`upstream_review.fix_needed`. "
        "In `handoff.awaiting_submission` and `handoff.waiting_upstream_pr`, `resume_pause` "
        "keeps the dossier, candidate head, clean head, `handoff_request`, and the posted "
        "package: `submit_upstream`, `verify_upstream_pr`, and the package bind to the exact "
        "head, so the revision bump does not invalidate the submission.",
        {
            "pause": Transition(
                _C, "/upstreaming pause",
                (NOT_PAUSED, "not terminal", ANY_REVISION),
                _E(work="clear", sets=(*_FLAG_FIELDS[:3], "paused_by=human")),
                description="A reason is optional; when given it is recorded in `paused_reason`."),
            "pause_on_identity_change": Transition(
                _O, None,
                (NOT_PAUSED, "not terminal",
                 "the observed upstream repository identity differs from the recorded one"),
                _E(work="clear", sets=("paused", "paused_at",
                                       "paused_reason=upstream_identity_changed",
                                       "paused_by=controller"))),
        },
        {
            "resume_pause": Transition(
                _C, RESUME, ("paused", EXACT_REVISION, FLAG_NOT_STALE),
                _E(revision="bump", clears=_FLAG_FIELDS),
                description="A Result that landed during the pause no longer matches and takes "
                            "`record_late_effect`. In a human wait the resume clears the flag "
                            "only: the reply states that the contribution still waits for a "
                            "human and names the pending action, and a second resume leaves "
                            "the wait."),
        },
        {
            "investigation.agent_running": "investigation.idle",
            "development.agent_running": "development.fix_needed",
            "upstream_review.agent_running": "upstream_review.fix_needed",
        },
        {
            _C: "`resume`, `park`, `refresh`, `retract-pr`, `abandon`, and `resolve-operation` "
                "apply; other commands are rejected. `resume` clears the flag first: it takes "
                "`resume_pause` or `resume_stale`. In a human wait a second `resume` then leaves "
                "the wait.",
            _O: "Folded normally, including CI on the exact head.",
            _R: "The requested Work was cleared, so a Result takes `record_late_effect`; nothing "
                "is published.",
            _P: "Folded normally. The publisher's final check reads `paused`; an effect that "
                "passed it before the pause may finish.",
            _K: "No launch, no Work allocation, no budget charge; reminders are suppressed.",
        },
    ),
}

BUDGETS: dict[str, Budget] = {
    "work_total": Budget(
        "retries.max_attempts_per_contribution", 0,
        "one Work allocation of any kind",
        "`budget.attempts_used`, counted per generation",
        ("exhaust_work_total", "exhaust_work_recovery", "exhaust_repair_attempts")),
    "work_recovery": Budget(
        "retries.work_recovery", 1,
        "one fresh Work graph after terminal Work without a usable Result",
        "`active_work.attempt`", ("exhaust_work_recovery",), infrastructure=True),
    "investigation_attempts": Budget(
        "retries.investigation_attempts", 0,
        "one investigation Result without a usable dossier",
        "`budget.investigation_attempts`", ("exhaust_investigation_recovery",)),
    "repair_attempts_cluster": Budget(
        "retries.patch_attempts_per_cluster", 0,
        "one CI-driven repair Work allocation for one failing CI job",
        "`budget.clusters[<job>]`, started again by `refresh_on_exhaustion`",
        ("refresh_on_exhaustion", "exhaust_repair_attempts")),
    "no_progress": Budget(
        "retries.no_progress_review_after", 0,
        "one successive rejected or blocked outcome without progress",
        "`budget.no_progress_streak`, started again by a published patch and by "
        "`refresh_on_exhaustion`",
        ("refresh_on_exhaustion", "exhaust_repair_attempts")),
    "flaky_reruns": Budget(
        "retries.flaky_reruns", 0,
        "one rerun of the failed CI jobs on the exact head, requested by the repair worker",
        "reruns recorded for the head", ("reject_patch_proposal",)),
    "interrupted_reruns": Budget(
        INFRASTRUCTURE_KEY, 0,
        "one rerun by the collector of CI jobs that were cancelled or lost their runner",
        "reruns recorded for the head", ("exhaust_interrupted_reruns",), infrastructure=True),
    "dispatch_recovery": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one dispatcher run that ended with the requested Work absent or unclaimed",
        "dispatcher runs since `active_work.requested_at`", ("exhaust_dispatch_recovery",),
        infrastructure=True),
    "publish_recovery": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one publish-only retry of the same verified Result",
        "`pending_publication.attempts`", ("exhaust_publish_recovery",), infrastructure=True),
    "operation_launch": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one launch of a planned fork provisioning operation", "the operation's launch attempts",
        ("require_provisioning_intervention",), infrastructure=True),
    "debug_cleanup": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one lease write and readback of the clean head", "the handoff operation's attempts",
        ("exhaust_debug_cleanup_retries",), infrastructure=True),
    "internal_pr_closure": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one close-and-readback attempt of the development PR", "the handoff operation's attempts",
        ("exhaust_internal_pr_retries",), infrastructure=True),
    "submission_comment": Budget(
        INFRASTRUCTURE_KEY, 1,
        "one attempt to post the submission package", "the handoff operation's attempts",
        ("exhaust_comment_retries",), infrastructure=True),
}

# The table of one remote operation. An operation is an intent recorded
# before a remote effect and a receipt confirmed by discovering the actual
# resource. A lifecycle transition with the ``operation`` trigger is taken
# when its operation reaches ``confirmed`` or ``failed``.
OPERATION_INITIAL_STATE = "planned"
OPERATION_INITIAL_VERB = "record_intent"

_RESOLVE_GUARD = (EXACT_OPERATION, "a resolution evidence record states the disposition")
_RESOLVE = {
    ("resolve_confirmed", "confirmed"): Transition(
        _C, RESOLVE, (*_RESOLVE_GUARD, "disposition confirmed"), _E(sets=("resolution",))),
    ("resolve_failed", "failed"): Transition(
        _C, RESOLVE, (*_RESOLVE_GUARD, "disposition failed"), _E(sets=("resolution",))),
    ("resolve_not_launched", "not_launched"): Transition(
        _C, RESOLVE, (*_RESOLVE_GUARD, "disposition not-launched"), _E(sets=("resolution",))),
}

OPERATIONS: dict[str, OperationState] = {
    "planned": OperationState(
        True, "The intent is recorded; the remote effect has not been attempted.",
        transitions={
            ("begin_effect", "applying"): Transition(
                _K, None,
                ("capacity available", "the contribution is not paused",
                 "the final check before the effect passes")),
            **_RESOLVE,
        }),
    "applying": OperationState(
        True, "The remote call is in flight.",
        transitions={
            ("record_response", "uncertain"): Transition(
                _P, None,
                ("the remote call succeeded, or its response was lost or ambiguous",),
                _E(sets=("external_ids",)),
                description="Even a successful response is provisional until discovery confirms "
                            "the resource."),
            ("record_failure", "failed"): Transition(
                _P, None, ("the remote call returned a definite failure",),
                _E(sets=("external_ids",))),
            **_RESOLVE,
        }),
    "uncertain": OperationState(
        True, "The effect may or may not exist; it is discovered, never repeated.",
        transitions={
            ("confirm_by_discovery", "confirmed"): Transition(
                _P, None, ("discovery finds the exact resource the operation proposed",),
                _E(sets=("discovery",))),
            ("record_discovery", SELF): Transition(
                _P, None, ("discovery finds the resource absent or an unrelated one",),
                _E(sets=("discovery",)),
                description="Absence never proves that the effect did not launch, and elapsed "
                            "time never closes an operation."),
            **_RESOLVE,
        }),
    "confirmed": OperationState(False, "The effect exists and was read back."),
    "failed": OperationState(False, "The effect definitely did not succeed."),
    "not_launched": OperationState(
        False, "A human established with evidence that the effect was never launched."),
}

TERMINAL_STATES = frozenset(k for k, s in LIFECYCLE.items() if s.kind == "terminal")


class Edge(NamedTuple):
    """One table transition with its source and resolved target state."""

    source: str
    verb: str
    target: str
    transition: Transition


def state_phase(state: str) -> str:
    return state.split(".", 1)[0]


def resolve_target(state: str, to: str) -> str:
    return state if to == SELF else to


def state_edges(state: str, table: dict | None = None):
    """Yield an ``Edge`` for every transition leaving ``state``, in table order."""
    for (verb, to), t in (table or LIFECYCLE)[state].transitions.items():
        yield Edge(state, verb, resolve_target(state, to), t)


def table_edges(table: dict | None = None):
    """Yield an ``Edge`` for every table transition, in table order."""
    for state in table or LIFECYCLE:
        yield from state_edges(state, table)


def find_edge(state: str, verb: str, table: dict | None = None) -> Edge:
    """The transition ``verb`` from ``state``; the verb must lead to one target."""
    edges = [e for e in state_edges(state, table) if e.verb == verb]
    if len(edges) != 1:
        raise KeyError(f"{state} has {len(edges)} transitions {verb}")
    return edges[0]


def is_rule_row(edge: Edge) -> bool:
    """Whether a table row is the expansion of a rule."""
    rule = RULES.get(edge.verb)
    return rule is not None and edge.transition is rule.transition


def _names_exhaustion(t: Transition, budget: str) -> bool:
    """Whether a guard of ``t`` states that ``budget`` is exhausted. A guard
    that requires the budget to remain does not."""
    return any(budget in g and "exhausted" in g for g in t.guard)


def exhaustion_edges(budget: str):
    """The transitions taken when ``budget`` is exhausted: one of its
    exhaustion verbs with a guard that names the budget."""
    verbs = BUDGETS[budget].exhaustion_verbs
    for e in table_edges():
        if e.verb in verbs and _names_exhaustion(e.transition, budget):
            yield e


def resolve_node(node: str, verb: str) -> tuple[Transition, str]:
    """The transition ``verb`` from a diagram node and the node it leads to.

    ``node`` may carry a flag (``state+flag``). Setting a flag keeps the
    state, or follows ``set_moves``; clearing it keeps the state. A table
    transition keeps the flag unless it clears the flag's field or starts a
    new generation."""
    state, _, flag = node.partition("+")
    if flag and verb in FLAGS[flag].cleared_by:
        return FLAGS[flag].cleared_by[verb], state
    for name, f in FLAGS.items():
        if verb in f.set_by:
            return f.set_by[verb], f"{f.set_moves.get(state, state)}+{name}"
    edge = find_edge(state, verb)
    effects = edge.transition.effects
    keeps = flag and FLAGS[flag].field not in effects.clears and effects.generation == "keep"
    return edge.transition, f"{edge.target}+{flag}" if keeps else edge.target


def policy_value(policy: dict, key: str) -> Any:
    value: Any = policy
    for part in key.split("."):
        value = value[part]
    return value


def budget_limit(name: str, policy: dict) -> int:
    budget = BUDGETS[name]
    return int(policy_value(policy, budget.policy_key)) + budget.offset


# --------------------------------------------------------------------------
# Diagram presentation metadata. Each diagram selects table states (``nodes``,
# in drawing order; phases are grouped in order of first appearance) and
# table transitions (``edges`` as ``(from_node, verb)``, in drawing order). A
# node written ``state+flag`` is ``state`` with that flag set. A diagram with
# ``rule`` draws that rule once, from a single node that stands for every
# state the rule applies in; rule rows are not repeated on every state.


@dataclass(frozen=True)
class Diagram:
    title: str | None
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    start: str | None = None
    start_verb: str | None = None
    ends: tuple[str, ...] = ()
    rule: str | None = None
    text: str = ""


@dataclass(frozen=True)
class DiagramSection:
    title: str
    text: str
    diagrams: tuple[Diagram, ...]


_SIMPLIFIED_NODES = (
    "queued.idle",
    "investigation.idle", "investigation.agent_running", "investigation.waiting_human",
    "provisioning.idle", "provisioning.waiting_human",
    "development.fix_needed", "development.agent_running", "development.waiting_ci",
    "development.waiting_human",
    "handoff.awaiting_submission", "handoff.cleaning_debug", "handoff.waiting_ci",
    "handoff.closing_internal_pr", "handoff.preparing_submission", "handoff.waiting_upstream_pr",
    "upstream_review.waiting_ci", "upstream_review.fix_needed", "upstream_review.agent_running",
    "upstream_review.waiting_human",
    "release_wait.idle", "release_wait.waiting_human",
    "released.idle", "parked.idle", "closed.idle",
)

DIAGRAM_SECTIONS: tuple[DiagramSection, ...] = (
    DiagramSection(
        "Simplified state diagram",
        "This is the expected path when investigation finds useful work, provisioning succeeds, "
        "development CI becomes green, upstream accepts the contribution, and an official release "
        "publishes the required artifacts.",
        (
            Diagram(None, _SIMPLIFIED_NODES, (
                ("queued.idle", "begin_investigation"),
                ("queued.idle", "withdraw_request"),
                ("investigation.idle", "dispatch_investigation"),
                ("investigation.agent_running", "reject_dossier"),
                ("investigation.agent_running", "exhaust_investigation_recovery"),
                ("investigation.waiting_human", "resume_investigation"),
                ("investigation.agent_running", "admit_development"),
                ("investigation.agent_running", "park_investigation"),
                ("investigation.agent_running", "close_investigation"),
                ("provisioning.idle", "require_provisioning_intervention"),
                ("provisioning.waiting_human", "resume_provisioning"),
                ("provisioning.idle", "confirm_provisioning"),
                ("development.fix_needed", "request_patch"),
                ("development.fix_needed", "request_repair"),
                ("development.agent_running", "record_publish_success"),
                ("development.agent_running", "reject_patch_proposal"),
                ("development.agent_running", "request_ci_rerun"),
                ("development.waiting_ci", "record_ci_failure"),
                ("development.waiting_ci", "record_actionable_review"),
                ("development.waiting_ci", "record_explanation"),
                ("development.waiting_human", "resume_development"),
                ("development.waiting_ci", "nominate_for_handoff"),
                ("handoff.awaiting_submission", "submit_upstream"),
                ("handoff.cleaning_debug", "confirm_clean_head"),
                ("handoff.waiting_ci", "accept_clean_head_ci"),
                ("handoff.closing_internal_pr", "confirm_internal_pr_closed"),
                ("handoff.preparing_submission", "confirm_submission_package"),
                ("handoff.waiting_upstream_pr", "verify_upstream_pr"),
                ("handoff.cleaning_debug", "return_for_repair"),
                ("handoff.waiting_ci", "return_for_repair"),
                ("handoff.waiting_upstream_pr", "retract_submission"),
                ("upstream_review.waiting_ci", "record_upstream_ci_failure"),
                ("upstream_review.waiting_ci", "record_actionable_review"),
                ("upstream_review.waiting_ci", "record_explanation"),
                ("upstream_review.fix_needed", "request_upstream_revision"),
                ("upstream_review.agent_running", "record_revision_publish_success"),
                ("upstream_review.waiting_ci", "record_pr_closed_unmerged"),
                ("upstream_review.waiting_human", "resume_upstream_review"),
                ("upstream_review.waiting_human", "resume_upstream_revision"),
                ("upstream_review.waiting_ci", "record_merge"),
                ("release_wait.idle", "require_release_intervention"),
                ("release_wait.waiting_human", "resume_release_observation"),
                ("release_wait.idle", "verify_release"),
                ("parked.idle", "restart_generation"),
                ("parked.idle", "abandon"),
            ), start=INITIAL_STATE, start_verb=INITIAL_VERB, ends=("released.idle", "closed.idle"),
                text="On both the development PR and the upstream PR, a CI failure keeps the "
                     "contribution in its phase while a bounded repair is prepared and tested, and "
                     "an actionable review comment is included in the next repair. An "
                     "explanation-only comment is answered on the development PR; on the upstream "
                     "PR it is recorded and surfaced on the tracking issue, and nothing is posted "
                     "upstream. Transitions that apply in many states (`park_contribution`, "
                     "`resume_stale`, `record_reminder`, `record_late_effect`) are drawn once, "
                     "under the rules below."),
        ),
    ),
    DiagramSection(
        "Investigation and provisioning failure cases",
        "Investigation and provisioning fail in different ways, and each case has its own recovery "
        "path. An infrastructure failure that exhausts its retries waits for a human in the same "
        "phase; it never ends the generation.",
        (
            Diagram(
                "Failure case 1: investigation finds no development path",
                ("investigation.idle", "investigation.agent_running", "provisioning.idle",
                 "parked.idle", "queued.idle", "closed.idle"),
                (("investigation.idle", "dispatch_investigation"),
                 ("investigation.agent_running", "admit_development"),
                 ("investigation.agent_running", "park_investigation"),
                 ("investigation.agent_running", "close_investigation"),
                 ("parked.idle", "restart_generation"),
                 ("parked.idle", "abandon")),
                start="investigation.idle", ends=("closed.idle",),
                text="The verified dossier decides between development, park, and close. "
                     "`closed` is the single terminal phase for work that is not needed; the reason "
                     "is `terminal_reason`. A restart may record an `admission_override`; the new "
                     "generation still runs the complete investigation."),
            Diagram(
                "Failure case 2: the Result has no usable dossier",
                ("investigation.idle", "investigation.agent_running", "investigation.waiting_human",
                 "provisioning.idle"),
                (("investigation.idle", "dispatch_investigation"),
                 ("investigation.agent_running", "reject_dossier"),
                 ("investigation.agent_running", "exhaust_investigation_recovery"),
                 ("investigation.waiting_human", "resume_investigation"),
                 ("investigation.agent_running", "admit_development")),
                start="investigation.idle",
                text="`reject_dossier` retries automatically while `investigation_attempts` remains; "
                     "a Result without a dossier counts like an invalid one. An incomplete artifact "
                     "is never adopted by hand."),
            Diagram(
                "Failure case 3: the investigator or dispatcher has an infrastructure failure",
                ("investigation.idle", "investigation.agent_running", "investigation.waiting_human",
                 "parked.idle"),
                (("investigation.idle", "dispatch_investigation"),
                 ("investigation.agent_running", "retry_dispatch"),
                 ("investigation.agent_running", "allocate_work_retry"),
                 ("investigation.agent_running", "exhaust_dispatch_recovery"),
                 ("investigation.agent_running", "exhaust_work_recovery"),
                 ("investigation.idle", "exhaust_work_total"),
                 ("investigation.waiting_human", "resume_investigation"),
                 ("investigation.waiting_human", "park_contribution")),
                start="investigation.idle",
                text="Dispatch and terminal-Work recovery are automatic within `dispatch_recovery` "
                     "and `work_recovery`. A terminal Work graph is never submitted again under the "
                     "same identity. Every Work allocation also charges `work_total`."),
            Diagram(
                "Failure case 4: fork provisioning fails or its outcome is uncertain",
                ("provisioning.idle", "provisioning.waiting_human", "development.fix_needed",
                 "parked.idle"),
                (("provisioning.idle", "start_operation"),
                 ("provisioning.idle", "require_provisioning_intervention"),
                 ("provisioning.waiting_human", "resume_provisioning"),
                 ("provisioning.idle", "confirm_provisioning"),
                 ("provisioning.waiting_human", "park_contribution")),
                start="provisioning.idle",
                text="`confirm_provisioning` follows fork creation, configuration, receipt "
                     "validation, and exact readback. `start_operation` relaunches within "
                     "`operation_launch`. A lost receipt is not proof that an operation did not "
                     "launch; an uncertain effect is resolved by discovery or "
                     "`/upstreaming resolve-operation`, never repeated blindly. A human corrects "
                     "the prerequisite and resumes, or parks."),
        ),
    ),
    DiagramSection(
        "Development and repair cases",
        "Development is one bounded loop around the same development PR. A worker proposes a patch, "
        "trusted code validates and publishes it, and CI plus authorized review evaluate the exact "
        "head. Only `nominate_for_handoff` leaves development forward.",
        (
            Diagram(
                "Success case: patch, publish, CI, and handoff",
                ("development.fix_needed", "development.agent_running", "development.waiting_ci",
                 "handoff.awaiting_submission"),
                (("development.fix_needed", "request_patch"),
                 ("development.agent_running", "record_publish_success"),
                 ("development.waiting_ci", "nominate_for_handoff")),
                start="development.fix_needed",
                text="`development.fix_needed` is the one state in which development Work is due. "
                     "`request_patch` applies while the head has no CI evidence and no feedback is "
                     "recorded; `request_repair` applies otherwise. Every applicable build and "
                     "test job must execute and pass on the exact head."),
            Diagram(
                "Failure case 1: patch proposal, dispatch, or Work failure",
                ("development.fix_needed", "development.agent_running",
                 "development.waiting_human"),
                (("development.fix_needed", "request_patch"),
                 ("development.agent_running", "reject_patch_proposal"),
                 ("development.agent_running", "replan_development"),
                 ("development.fix_needed", "request_repair"),
                 ("development.agent_running", "retry_dispatch"),
                 ("development.agent_running", "allocate_work_retry"),
                 ("development.agent_running", "exhaust_dispatch_recovery"),
                 ("development.agent_running", "exhaust_work_recovery"),
                 ("development.waiting_human", "resume_development")),
                start="development.fix_needed",
                text="Dispatch recovery and bounded repair are automatic. Dispatch and Work "
                     "exhaustion wait for a human in development."),
            Diagram(
                "Failure case 2: trusted publication failure",
                ("development.agent_running", "development.fix_needed", "development.waiting_human",
                 "development.waiting_ci"),
                (("development.agent_running", "record_publish_success"),
                 ("development.agent_running", "schedule_publish_retry"),
                 ("development.agent_running", "exhaust_publish_recovery"),
                 ("development.waiting_human", "resume_development"),
                 ("development.fix_needed", "request_repair")),
                start="development.agent_running",
                text="An infrastructure failure publishes the same verified Result again within "
                     "`publish_recovery`. After exhaustion and a resume, fresh Work is requested; "
                     "the held Result is not reused."),
            Diagram(
                "Failure case 3: internal PR CI or review failure",
                ("development.waiting_ci", "development.fix_needed", "development.agent_running",
                 "development.waiting_human", "handoff.awaiting_submission"),
                (("development.waiting_ci", "record_ci_failure"),
                 ("development.waiting_ci", "record_actionable_review"),
                 ("development.waiting_ci", "record_explanation"),
                 ("development.fix_needed", "request_repair"),
                 ("development.agent_running", "record_publish_success"),
                 ("development.agent_running", "request_ci_rerun"),
                 ("development.waiting_ci", "nominate_for_handoff"),
                 ("development.waiting_ci", "rerun_interrupted_ci"),
                 ("development.waiting_ci", "exhaust_interrupted_reruns"),
                 ("development.waiting_human", "resume_ci"),
                 ("development.waiting_ci", "record_head_change"),
                 ("development.waiting_ci", "record_pr_mismatch")),
                start="development.waiting_ci",
                text="Build and test failures and actionable comments request repair automatically. "
                     "The repair worker classifies a failure: one not caused by the patch takes "
                     "`request_ci_rerun` within `flaky_reruns`. The collector reruns on its own only "
                     "jobs that were cancelled or lost their runner, within `interrupted_reruns` "
                     "per head; after that a human resumes with `resume_ci`, which reruns the "
                     "jobs. Explanation-only comments do not bump the revision, invalidate CI, or "
                     "request repair."),
            Diagram(
                "Failure case 4: a repair budget is exhausted",
                ("development.fix_needed", "development.agent_running", "queued.idle",
                 "investigation.idle", "investigation.agent_running", "parked.idle"),
                (("development.fix_needed", "request_repair"),
                 ("development.fix_needed", "refresh_on_exhaustion"),
                 ("queued.idle", "begin_investigation"),
                 ("investigation.idle", "dispatch_investigation"),
                 ("investigation.agent_running", "readmit_development"),
                 ("development.fix_needed", "exhaust_repair_attempts")),
                start="development.fix_needed",
                text="Exhaustion is decided at `development.fix_needed`. The first time "
                     "`repair_attempts_cluster` or `no_progress` is exhausted in a generation, "
                     "`refresh_on_exhaustion` runs a complete investigation again on the existing "
                     "fork. After that, `exhaust_repair_attempts` parks. Both apply only while "
                     "`work_total` remains."),
            Diagram(
                "Failure case 5: the Work budget is exhausted",
                ("development.fix_needed", "development.agent_running",
                 "development.waiting_human", "parked.idle"),
                (("development.fix_needed", "request_repair"),
                 ("development.fix_needed", "exhaust_work_total"),
                 ("development.waiting_human", "resume_development"),
                 ("development.waiting_human", "park_contribution")),
                start="development.fix_needed",
                text="When `work_total` is exhausted at `development.fix_needed`, "
                     "`exhaust_work_total` waits for a human and names `work_total` in "
                     "`pending_human_action.counter`, whatever the repair budgets hold. The reply states the "
                     "number of Work allocations spent in this generation and names both "
                     "`/upstreaming resume` and `/upstreaming park`. One resume starts "
                     "`work_total` again and returns to `development.fix_needed`; the repair "
                     "budgets are not started again."),
            Diagram(
                "Failure case 6: development must be re-investigated",
                ("development.waiting_ci", "development.fix_needed",
                 "development.waiting_human", "queued.idle", "investigation.agent_running"),
                (("development.waiting_ci", "refresh_investigation"),
                 ("development.fix_needed", "refresh_investigation"),
                 ("development.waiting_human", "refresh_investigation"),
                 ("investigation.agent_running", "readmit_development")),
                text="`refresh` runs a complete investigation without deleting the fork or "
                     "development PR. A refreshed dossier that decides to develop returns to "
                     "`development.fix_needed` on the existing fork."),
        ),
    ),
    DiagramSection(
        "Pause, waiting-human, and park cases",
        "Pause keeps the lifecycle phase and activity. `waiting_human` is entered automatically "
        "when a specific failure or external prerequisite needs operator action. Park means the "
        "approach is not working or a human said stop; it ends the current generation, and resume "
        "from parked performs a clean reset. A Result is reused only while the pipeline ran "
        "continuously: after a pause, a human wait, or a park, the next step is fresh Work.",
        (
            Diagram(
                "Success case 1: temporary pause and resume",
                ("development.waiting_ci", "development.waiting_ci+paused"),
                (("development.waiting_ci", "pause"),
                 ("development.waiting_ci", "pause_on_identity_change"),
                 ("development.waiting_ci+paused", "resume_pause")),
                start="development.waiting_ci",
                text="There is one flag, `paused`, with its reason and who set it. A human sets it "
                     "with `pause`, with an optional reason; the controller sets it when the "
                     "upstream identity changes and always records the reason. `resume_pause` "
                     "bumps the revision. An effect that passed its final "
                     "publication check before the pause may finish and is reconciled normally."),
            Diagram(
                "Success case 2: pause while Work is requested",
                ("development.agent_running", "development.fix_needed+paused",
                 "development.fix_needed"),
                (("development.agent_running", "pause"),
                 ("development.fix_needed+paused", "resume_pause")),
                start="development.agent_running",
                text="Pause clears the requested Work, so the state in which that Work is due "
                     "again is entered. A Result that lands during the pause takes "
                     "`record_late_effect`; after the resume, fresh Work is requested."),
            Diagram(
                "Success case 3: resume after a long stop refreshes the dossier",
                ("development.waiting_ci", "development.waiting_ci+paused",
                 "development.waiting_human", "development.fix_needed", "queued.idle"),
                (("development.waiting_ci", "pause"),
                 ("development.waiting_ci+paused", "resume_pause"),
                 ("development.waiting_ci+paused", "resume_stale"),
                 ("development.waiting_human", "resume_development"),
                 ("development.waiting_human", "resume_stale")),
                start="development.waiting_ci",
                text="In provisioning and development, a pause or human wait longer than "
                     "`refresh.stale_after_days` days resumes through `resume_stale`, which runs "
                     "the investigation again. The normal resume verbs carry the complementary "
                     "guard, so exactly one applies. Handoff, upstream review, and release waits "
                     "are never refreshed automatically."),
            Diagram(
                "Failure case 1: waiting for operator action or an external prerequisite",
                ("development.agent_running", "development.waiting_human", "development.fix_needed",
                 "parked.idle"),
                (("development.agent_running", "exhaust_publish_recovery"),
                 ("development.waiting_human", "resume_development"),
                 ("development.waiting_human", "park_contribution")),
                start="development.agent_running",
                text="The controller records the concrete reason in `pending_human_action`. Correct "
                     "the stated problem first, then resume with the exact revision. A "
                     "contribution that is also paused needs two resumes: the first clears the "
                     "flag and its reply names the pending action, the second leaves the wait. "
                     "When an exhausted budget caused the wait, the resume starts again the one "
                     "counter named in `pending_human_action.counter`."),
            Diagram(
                "Failure case 2: park, then restart as a clean generation or abandon",
                ("development.fix_needed", "parked.idle", "queued.idle", "closed.idle"),
                (("development.fix_needed", "park_contribution"),
                 ("parked.idle", "record_late_effect"),
                 ("parked.idle", "restart_generation"),
                 ("parked.idle", "abandon")),
                start="development.fix_needed", ends=("closed.idle",),
                text="`restart_generation` is accepted only when no operation is open, no "
                     "publication is pending, and no upstream PR is open. It bumps the generation, "
                     "clears mutable lifecycle state, and starts at `queued.idle`. Requested Work "
                     "carries its generation, so a Result of the ended generation is never folded "
                     "or published. Nothing here closes PRs, deletes branches, or deletes "
                     "repositories."),
        ),
    ),
    DiagramSection(
        "Handoff, upstream review, and release cases",
        "The submission request is bound to one exact revision and candidate head. Trusted "
        "preparation derives and verifies the clean head; the Apps prepare the submission package "
        "but do not open the upstream PR.",
        (
            Diagram(
                "Success case 1: one command publishes a prefilled PR package and a human submits it",
                ("development.waiting_ci", "handoff.awaiting_submission", "handoff.cleaning_debug",
                 "handoff.waiting_ci", "handoff.closing_internal_pr", "handoff.preparing_submission",
                 "handoff.waiting_upstream_pr", "upstream_review.waiting_ci"),
                (("development.waiting_ci", "nominate_for_handoff"),
                 ("handoff.awaiting_submission", "record_reminder"),
                 ("handoff.awaiting_submission", "submit_upstream"),
                 ("handoff.cleaning_debug", "confirm_clean_head"),
                 ("handoff.waiting_ci", "accept_clean_head_ci"),
                 ("handoff.closing_internal_pr", "confirm_internal_pr_closed"),
                 ("handoff.preparing_submission", "confirm_submission_package"),
                 ("handoff.waiting_upstream_pr", "record_reminder"),
                 ("handoff.waiting_upstream_pr", "verify_upstream_pr")),
                text="`submit-upstream` is the only submission command. The human follows the "
                     "prefilled link and opens the PR under their own identity; observation "
                     "verifies it before `upstream_review`. Both human steps are reminded."),
            Diagram(
                "Failure case 1: pre-submission changes versus the locked upstream-PR wait",
                ("handoff.awaiting_submission", "handoff.cleaning_debug", "handoff.waiting_ci",
                 "handoff.waiting_upstream_pr", "development.fix_needed", "development.waiting_ci",
                 "queued.idle"),
                (("handoff.awaiting_submission", "submit_upstream"),
                 ("handoff.cleaning_debug", "confirm_clean_head"),
                 ("handoff.waiting_ci", "rerun_interrupted_ci"),
                 ("handoff.awaiting_submission", "return_for_repair"),
                 ("handoff.awaiting_submission", "record_head_change"),
                 ("handoff.cleaning_debug", "return_for_repair"),
                 ("handoff.waiting_ci", "return_for_repair"),
                 ("handoff.awaiting_submission", "refresh_investigation"),
                 ("handoff.waiting_upstream_pr", "record_audit_observation"),
                 ("handoff.waiting_upstream_pr", "retract_submission"),
                 ("handoff.waiting_upstream_pr", "refresh_investigation")),
                start="handoff.awaiting_submission",
                text="Before the package is posted, a changed head, cleanup validation, an "
                     "exact-head lease conflict, or blocking CI on the clean head returns to "
                     "development. `handoff.waiting_upstream_pr` is locked: only "
                     "`verify_upstream_pr`, `retract_submission`, and `refresh_investigation` "
                     "leave it."),
            Diagram(
                "Failure case 2: a handoff operation fails repeatedly",
                ("handoff.cleaning_debug", "handoff.waiting_ci", "handoff.closing_internal_pr",
                 "handoff.preparing_submission", "handoff.waiting_human",
                 "handoff.waiting_upstream_pr"),
                (("handoff.cleaning_debug", "retry_debug_cleanup"),
                 ("handoff.cleaning_debug", "exhaust_debug_cleanup_retries"),
                 ("handoff.waiting_human", "resume_debug_cleanup"),
                 ("handoff.cleaning_debug", "confirm_clean_head"),
                 ("handoff.waiting_ci", "exhaust_interrupted_reruns"),
                 ("handoff.waiting_human", "resume_ci"),
                 ("handoff.waiting_ci", "accept_clean_head_ci"),
                 ("handoff.closing_internal_pr", "retry_internal_pr_closure"),
                 ("handoff.closing_internal_pr", "exhaust_internal_pr_retries"),
                 ("handoff.waiting_human", "resume_internal_pr_closure"),
                 ("handoff.closing_internal_pr", "confirm_internal_pr_closed"),
                 ("handoff.preparing_submission", "retry_submission_comment"),
                 ("handoff.preparing_submission", "exhaust_comment_retries"),
                 ("handoff.waiting_human", "resume_submission_preparation"),
                 ("handoff.preparing_submission", "confirm_submission_package")),
                start="handoff.cleaning_debug",
                text="The lease write, the reruns of interrupted CI, the closure, and the package "
                     "each retry within their budget and then wait for a human in the one "
                     "`handoff.waiting_human`. `pending_human_action` names the step that "
                     "failed, and exactly one of the four resume verbs applies: it returns to "
                     "that step. Recovery does not need another `submit-upstream`."),
            Diagram(
                "Success case 2: upstream review, merge, and release",
                ("upstream_review.waiting_ci", "release_wait.idle", "released.idle"),
                (("upstream_review.waiting_ci", "record_merge"),
                 ("release_wait.idle", "verify_release")),
                start="upstream_review.waiting_ci", ends=("released.idle",),
                text="Merge enters `release_wait`; it is not terminal success. Only verified "
                     "release coverage enters `released`."),
            Diagram(
                "Failure case 3: upstream CI or actionable review needs a revision",
                ("upstream_review.waiting_ci", "upstream_review.fix_needed",
                 "upstream_review.agent_running", "upstream_review.waiting_human"),
                (("upstream_review.waiting_ci", "record_upstream_ci_failure"),
                 ("upstream_review.waiting_ci", "record_actionable_review"),
                 ("upstream_review.waiting_ci", "record_explanation"),
                 ("upstream_review.fix_needed", "request_upstream_revision"),
                 ("upstream_review.agent_running", "record_revision_publish_success"),
                 ("upstream_review.agent_running", "reject_patch_proposal"),
                 ("upstream_review.agent_running", "require_reviewer_answer"),
                 ("upstream_review.fix_needed", "exhaust_repair_attempts"),
                 ("upstream_review.waiting_human", "resume_upstream_review"),
                 ("upstream_review.waiting_human", "resume_upstream_revision")),
                start="upstream_review.waiting_ci",
                text="Automated upstream revisions require that the upstream PR head is the "
                     "controlled fork ref. A rejected proposal is repaired within `no_progress`. "
                     "When the worker reports that no change is needed, a human answers the "
                     "reviewer; `resume_upstream_review` then clears the pending feedback and "
                     "returns to `upstream_review.waiting_ci` without requesting Work. After a "
                     "budget is exhausted, `resume_upstream_revision` starts again the one "
                     "counter named in `pending_human_action.counter`. A CI failure is recorded "
                     "once per head and CI run. Upstream CI is never rerun by the pipeline."),
            Diagram(
                "Failure case 4: revision Work or its publication fails",
                ("upstream_review.agent_running", "upstream_review.waiting_human",
                 "upstream_review.fix_needed"),
                (("upstream_review.agent_running", "retry_dispatch"),
                 ("upstream_review.agent_running", "allocate_work_retry"),
                 ("upstream_review.agent_running", "schedule_publish_retry"),
                 ("upstream_review.agent_running", "exhaust_dispatch_recovery"),
                 ("upstream_review.agent_running", "exhaust_work_recovery"),
                 ("upstream_review.agent_running", "exhaust_publish_recovery"),
                 ("upstream_review.waiting_human", "resume_upstream_revision")),
                start="upstream_review.agent_running",
                text="Every exhaustion in upstream review waits for a human in "
                     "`upstream_review.waiting_human`; nothing parks from upstream review "
                     "automatically."),
            Diagram(
                "Failure case 5: the upstream PR is closed or its head changes",
                ("upstream_review.waiting_ci", "upstream_review.waiting_human", "parked.idle"),
                (("upstream_review.waiting_ci", "record_pr_closed_unmerged"),
                 ("upstream_review.waiting_ci", "record_head_change"),
                 ("upstream_review.waiting_human", "resume_upstream_review"),
                 ("upstream_review.waiting_human", "park_contribution")),
                start="upstream_review.waiting_ci",
                text="The pipeline does not reopen the PR or create a replacement. "
                     "`resume_upstream_review` returns to `upstream_review.waiting_ci` after a "
                     "head change, and after a closed PR once it is observed open again. "
                     "`resume_upstream_revision` applies only when revision Work failed. Human "
                     "triage either resumes or parks."),
            Diagram(
                "Failure case 6: release is missing, incomplete, or waiting for operator action",
                ("release_wait.idle", "release_wait.waiting_human", "released.idle"),
                (("release_wait.idle", "record_release_incomplete"),
                 ("release_wait.idle", "require_release_intervention"),
                 ("release_wait.waiting_human", "resume_release_observation"),
                 ("release_wait.idle", "verify_release")),
                start="release_wait.idle",
                text="Release observation waits without invoking a worker until the evidence "
                     "satisfies the frozen acceptance contract."),
        ),
    ),
    DiagramSection(
        "Rules that apply in many states",
        "Four transitions apply in many states. Each is written once as a rule and expanded into "
        "the table; each is drawn once here, from a single node that stands for every state the "
        "rule applies in.",
        (
            Diagram(
                "Rule: park by command", ("parked.idle",), (), rule="park_contribution",
                text="Nothing parks from `upstream_review` automatically, and "
                     "`handoff.waiting_upstream_pr` and `release_wait` cannot be parked."),
            Diagram(
                "Rule: resume after a long stop", ("queued.idle",), (), rule="resume_stale"),
            Diagram(
                "Rule: reminders of a pending human step", (), (), rule="record_reminder",
                text="The rule covers the `gate`, `human_wait`, and `locked` kinds. In "
                     "`handoff.waiting_upstream_pr` the reminder text carries the package link."),
            Diagram(
                "Rule: late Results", (), (), rule="record_late_effect"),
        ),
    ),
)

OPERATION_SECTION = (
    "Remote operations",
    "Every remote effect is an operation with its own small table, held to the same checks. The "
    "intent is recorded before the effect, and the receipt is confirmed by discovering the actual "
    "resource. While an operation is open (`planned`, `applying`, `uncertain`) it fences its "
    "contribution: no park, no refresh, and no second writer. `/upstreaming resolve-operation` "
    "closes an open operation with a resolution evidence record.",
)

# Short node ids used when drawing states (``phase_activity`` otherwise).
DIAGRAM_PHASE_IDS = {"upstream_review": "upstream"}


# --------------------------------------------------------------------------
# Consistency check of the tables, run in CI as ``python -m lifecycle check``.

# The revision rule. A transition bumps the revision when it is a resume,
# when it starts a generation or a fresh Work graph, when it changes a field
# that defines the requested or approved work, or when a Result is rejected
# and the same kind of Work is due again. No other transition bumps it.
# Setting a field in ``REVISION_SET_FIELDS`` or clearing one in
# ``REVISION_CLEAR_FIELDS`` changes the requested or approved work; so does
# an observed head (``REVISION_OBSERVED_FIELDS`` set by an observation).
REVISION_SET_FIELDS = ("scope_paths", "pending_feedback_evidence_ids", "refresh")
REVISION_CLEAR_FIELDS = ("scope_paths", "dossier_evidence_id", "handoff_request",
                         "handoff_approval", "clean_head_sha")
REVISION_OBSERVED_FIELDS = ("fork.head_sha", "upstream_pr.head_sha")
_REVISION_GUARDS = (EXACT_REVISION, ANY_REVISION)

# The exits of the locked upstream-PR wait.
LOCKED_EXITS = {"handoff.waiting_upstream_pr": ("verify_upstream_pr", "retract_submission",
                                                "refresh_investigation")}
# Phases from which only a command may end the generation.
NO_AUTOMATIC_PARK = ("upstream_review",)


def _field_name(entry: str) -> str:
    return entry.split("=", 1)[0]


def _reachable(start: str, step) -> set[str]:
    seen, todo = {start}, [start]
    while todo:
        for nxt in step(todo.pop()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def must_bump(edge: Edge) -> bool:
    """Whether the revision rule requires ``edge`` to bump the revision."""
    t, e = edge.transition, edge.transition.effects
    sets = {_field_name(f) for f in e.sets}
    clears = {_field_name(f) for f in e.clears}
    if t.command == RESUME or e.generation == "bump" or e.work == "reallocate":
        return True
    if sets & set(REVISION_SET_FIELDS) or clears & set(REVISION_CLEAR_FIELDS):
        return True
    if t.trigger == Trigger.OBSERVATION and sets & set(REVISION_OBSERVED_FIELDS):
        return True
    target = LIFECYCLE.get(edge.target)
    return bool(t.trigger == Trigger.RESULT and e.work == "clear" and edge.target != edge.source
                and target is not None and target.kind == "auto_wait" and target.launches)


def _check_transition(where: str, t: Transition, problems: list[str], *,
                      command_prefix: str = "/upstreaming ",
                      command_guards: tuple[str, ...] = _REVISION_GUARDS) -> None:
    if not isinstance(t.trigger, Trigger):
        problems.append(f"{where}: trigger {t.trigger!r} is not a Trigger")
    if not t.guard:
        problems.append(f"{where}: empty guard; write ({ALWAYS!r},) for an unconditional transition")
    elif ALWAYS in t.guard and len(t.guard) > 1:
        problems.append(f"{where}: {ALWAYS!r} combined with other conditions")
    if any(not g.strip() for g in t.guard):
        problems.append(f"{where}: blank guard condition")
    if t.trigger == Trigger.COMMAND:
        if not (t.command or "").startswith(command_prefix):
            problems.append(f"{where}: command trigger without an `/upstreaming` command")
        if not any(g in command_guards for g in t.guard):
            problems.append(f"{where}: command guard names none of "
                            + ", ".join(repr(g) for g in command_guards))
    elif t.command:
        problems.append(f"{where}: command {t.command!r} on a {t.trigger} trigger")
    e = t.effects
    if e.generation == "bump" and (e.revision != "bump" or e.work != "clear"):
        problems.append(f"{where}: a generation bump must bump the revision and clear Work")
    if e.work == "allocate" and NOT_PAUSED not in t.guard:
        problems.append(f"{where}: allocates Work without the guard {NOT_PAUSED!r}")
    guard_text = "; ".join(t.guard)
    for name in t.charges:
        if name not in BUDGETS:
            problems.append(f"{where}: charges unknown budget {name!r}")
        elif name not in guard_text:
            problems.append(f"{where}: charges {name!r} but its guard does not mention it")


def _check_graph(name: str, table: dict, initial: str, is_end, problems: list[str]) -> bool:
    """Structure of one table: targets, end states, reachability, and one
    target per verb. Returns False when targets are missing."""
    label = f"{name} " if name else ""
    if initial not in table:
        problems.append(f"{label}initial state {initial!r} is not a table state")
        return False
    ends = {k for k in table if is_end(k)}
    if not ends:
        problems.append(f"{label}no state is marked as an end state")
    ok = True
    for key, state in table.items():
        targets: dict[str, list[str]] = {}
        for verb, to in state.transitions:
            if not verb:
                problems.append(f"{label}{key}: transition with an empty verb")
            if to != SELF and to not in table:
                problems.append(f"{label}{key} --{verb}--> {to}: target is not a table state")
                ok = False
            targets.setdefault(verb, []).append(resolve_target(key, to))
        for verb, to in targets.items():
            if len(to) > 1:
                problems.append(f"{label}{key} --{verb}-->: the verb leads to {len(to)} targets "
                                f"({', '.join(to)}); a verb from a state must lead to one target")
        leaving = [v for v, to in state.transitions if resolve_target(key, to) != key]
        if key in ends and leaving:
            problems.append(f"{label}{key}: end state has outgoing transitions")
        if key not in ends and not leaving:
            problems.append(f"{label}{key}: not an end state but no transition leaves it")
    if not ok:
        return False
    successors: dict[str, set[str]] = {k: set() for k in table}
    predecessors: dict[str, set[str]] = {k: set() for k in table}
    for e in table_edges(table):
        successors[e.source].add(e.target)
        predecessors[e.target].add(e.source)
    reached = _reachable(initial, successors.__getitem__)
    for key in table:
        if key not in reached:
            kind = "end state" if key in ends else "state"
            problems.append(f"{label}{key}: {kind} is not reachable from {initial}")
    can_end: set[str] = set()
    for end in ends:
        can_end |= _reachable(end, predecessors.__getitem__)
    for key in table:
        if key not in can_end:
            problems.append(f"{label}{key}: no end state is reachable from this state")
    return True


def _check_states(problems: list[str]) -> None:
    for key, state in LIFECYCLE.items():
        phase, _, activity = key.partition(".")
        if not phase or not activity or "." in activity:
            problems.append(f"{key}: state key is not phase.activity")
        if state.kind == "terminal" and (state.launches or state.writes):
            problems.append(f"{key}: end state launches or writes")
        if state.kind in ("locked", "human_wait", "generation_end") and (state.launches or state.writes):
            problems.append(f"{key}: {state.kind} state launches or writes")
        if state.kind in ("human_wait", "generation_end") and not any(
                t.command == RESUME for t in state.transitions.values()):
            problems.append(f"{key}: {state.kind} state has no `{RESUME}` exit")
        if state.kind == "gate" and not any(
                t.trigger == Trigger.COMMAND for t in state.transitions.values()):
            problems.append(f"{key}: gate state has no command exit")
        for e in state_edges(key):
            where = f"{key} --{e.verb}--> {e.target}"
            _check_transition(where, e.transition, problems)
            effects = e.transition.effects
            if effects.work == "allocate" and not state.launches:
                problems.append(f"{where}: allocates Work from a state that does not launch")
            if effects.work in ("allocate", "reallocate") and "work_total" not in e.transition.charges:
                problems.append(f"{where}: allocates Work without charging 'work_total'")
            if must_bump(e) and effects.revision != "bump":
                problems.append(f"{where}: the revision rule requires a revision bump")
            if not must_bump(e) and effects.revision == "bump":
                problems.append(f"{where}: bumps the revision although the revision rule "
                                "does not require it")
            target = LIFECYCLE[e.target]
            entering = e.target != key
            sets = {_field_name(f).split(".", 1)[0] for f in effects.sets}
            if entering and target.kind == "human_wait" and state.kind != "human_wait" \
                    and "pending_human_action" not in sets:
                problems.append(f"{where}: enters a human wait without setting pending_human_action")
            if entering and target.kind == "generation_end":
                if not {"parked_reason", "previous_phase"} <= sets:
                    problems.append(f"{where}: ends the generation without parked_reason and "
                                    "previous_phase")
                if state_phase(key) in NO_AUTOMATIC_PARK and e.transition.trigger != Trigger.COMMAND:
                    problems.append(f"{where}: ends the generation from {state_phase(key)} "
                                    "without a command")
    for key, verbs in LOCKED_EXITS.items():
        state = LIFECYCLE.get(key)
        if state is None or state.kind != "locked":
            problems.append(f"{key}: LOCKED_EXITS names a state that is not locked")
            continue
        exits = [e.verb for e in state_edges(key) if e.target != key]
        if sorted(exits) != sorted(verbs):
            problems.append(f"{key}: exits {sorted(exits)} differ from the {len(verbs)} "
                            f"allowed exits {sorted(verbs)}")
        guards = {e.transition.guard for e in state_edges(key)
                  if e.target != key and e.transition.trigger == Trigger.COMMAND}
        if len(guards) > 1:
            problems.append(f"{key}: the command exits of a locked state must share one guard")
    for key, state in LIFECYCLE.items():
        if state.kind == "locked" and key not in LOCKED_EXITS:
            problems.append(f"{key}: locked state without LOCKED_EXITS")


def _check_rules(problems: list[str]) -> None:
    for verb, rule in RULES.items():
        where = f"rule {verb}"
        if rule.target != SELF and rule.target not in LIFECYCLE:
            problems.append(f"{where}: target {rule.target!r} is not a table state")
            continue
        _check_transition(where, rule.transition, problems)
        if not rule.applies.strip():
            problems.append(f"{where}: no description of where it applies")
        expected = set(rule_states(verb))
        if not expected:
            problems.append(f"{where}: applies in no state")
        rows = {e.source for e in table_edges() if is_rule_row(e)
                and e.target == resolve_target(e.source, rule.target) and e.verb == verb}
        for key in sorted(expected - rows):
            problems.append(f"{where}: applies in {key} but the table has no such row")
        for key in sorted(rows - expected):
            problems.append(f"{where}: the table has a row in {key} where the rule does not apply")
    # Reminders: every state that waits for a human command is reminded.
    reminder = RULES.get("record_reminder")
    if reminder is None or set(reminder.kinds) != set(REMINDER_KINDS) or reminder.states \
            or reminder.exclude or reminder.target != SELF:
        problems.append("rule record_reminder: must apply in every gate, human_wait, and locked "
                        "state and keep the state")
    for key, state in LIFECYCLE.items():
        if state.kind in REMINDER_KINDS and not any(
                v == "record_reminder" for v, _to in state.transitions):
            problems.append(f"{key}: {state.kind} state without record_reminder")
    for e in table_edges():
        if e.verb != "record_reminder":
            continue
        if REMINDER_KEY not in "; ".join(e.transition.guard):
            problems.append(f"{e.source} --record_reminder-->: guard does not read {REMINDER_KEY}")
        if not is_rule_row(e):
            problems.append(f"{e.source} --record_reminder-->: a reminder row beside the rule")
    # Late Results: recorded in every state, never changing it.
    late = RULES.get("record_late_effect")
    if late is None or late.target != SELF or late.kinds or late.states or late.exclude \
            or late.transition.effects != Effects():
        problems.append("rule record_late_effect: must apply in every state, keep the state, "
                        "and have no effects")
    # Park: one command rule; nothing else is a command into the generation end.
    park = RULES.get("park_contribution")
    if park is None or park.transition.command != PARK or "no open operation" not in park.transition.guard:
        problems.append(f"rule park_contribution: must be the `{PARK}` command guarded by "
                        "'no open operation'")


def _check_stale_resume(policy: dict, problems: list[str]) -> None:
    """``resume_stale`` and the other resumes of the same states carry
    complementary guards, so exactly one applies."""
    rule = RULES.get("resume_stale")
    if rule is None:
        problems.append("rule resume_stale: missing")
        return
    stale_states = set(rule_states("resume_stale"))
    expected = {k for k in LIFECYCLE if state_phase(k) in STALE_PHASES}
    if stale_states != expected:
        problems.append(f"rule resume_stale: applies in {sorted(stale_states)}, not in every "
                        f"state of {', '.join(STALE_PHASES)}")
    t = rule.transition
    if t.command != RESUME or STALE not in t.guard or NOT_STALE in t.guard:
        problems.append(f"rule resume_stale: must be `{RESUME}` guarded by {STALE!r}")
    refresh = {e.transition.guard for e in table_edges() if e.verb == "refresh_investigation"}
    for guard in (QUIESCENT, NO_WORK):
        if guard not in t.guard:
            problems.append(f"rule resume_stale: without the guard {guard!r}")
        if not any(guard in g for g in refresh):
            problems.append(f"refresh_investigation: no row with the guard {guard!r}")
    if not set(_REFRESH_EFFECTS.sets) <= set(t.effects.sets) \
            or not set(_REFRESH_EFFECTS.clears) <= set(t.effects.clears):
        problems.append("rule resume_stale: does not have the effects of refresh_investigation")
    for e in table_edges():
        if e.transition.command != RESUME or e.verb == "resume_stale":
            continue
        where = f"{e.source} --{e.verb}--> {e.target}"
        if STALE in e.transition.guard:
            problems.append(f"{where}: only resume_stale may be guarded by {STALE!r}")
        declared = NOT_STALE in e.transition.guard
        if e.source in stale_states and not declared:
            problems.append(f"{where}: a resume beside resume_stale without the guard {NOT_STALE!r}")
        if e.source not in stale_states and declared:
            problems.append(f"{where}: guard {NOT_STALE!r} in a state without resume_stale")
    for name, f in FLAGS.items():
        for verb, clear in f.cleared_by.items():
            if clear.command == RESUME and FLAG_NOT_STALE not in clear.guard:
                problems.append(f"flag {name} {verb}: a resume without the guard {FLAG_NOT_STALE!r}")
    for key in (STALE_KEY, REMINDER_KEY):
        try:
            if int(policy_value(policy, key)) <= 0:
                problems.append(f"policy.json: {key} is not positive")
        except (KeyError, TypeError, ValueError):
            problems.append(f"policy.json has no integer {key}")


def _check_flags(problems: list[str]) -> None:
    allocated = {e.target for e in table_edges() if e.transition.effects.work == "allocate"}
    table_verbs = {e.verb for e in table_edges()}
    for name, f in FLAGS.items():
        if not f.set_by or not f.cleared_by:
            problems.append(f"flag {name}: no set or no clear transition")
        for verb, t in (*f.set_by.items(), *f.cleared_by.items()):
            where = f"flag {name} {verb}"
            if verb in table_verbs:
                problems.append(f"{where}: is also a table verb")
            _check_transition(where, t, problems)
        for verb, t in f.set_by.items():
            where = f"flag {name} {verb}"
            if f.field not in t.effects.sets:
                problems.append(f"{where}: does not set {f.field}")
            if NOT_PAUSED not in t.guard and name == "paused":
                problems.append(f"{where}: sets the flag without the guard {NOT_PAUSED!r}")
            if t.effects.work != "clear":
                problems.append(f"{where}: setting the flag must clear the requested Work")
            if t.effects.revision == "bump":
                problems.append(f"{where}: a stop must not bump the revision")
            reason = [s for s in t.effects.sets if _field_name(s) == f"{f.field}_reason"]
            if not reason:
                problems.append(f"{where}: does not set {f.field}_reason")
            elif t.trigger != Trigger.COMMAND and "=" not in reason[0]:
                problems.append(f"{where}: a stop set by the controller must record its reason")
        for verb, t in f.cleared_by.items():
            where = f"flag {name} {verb}"
            if t.trigger != Trigger.COMMAND:
                problems.append(f"{where}: is not command-triggered")
            if f.field not in t.effects.clears:
                problems.append(f"{where}: does not clear {f.field}")
            if t.command == RESUME and t.effects.revision != "bump":
                problems.append(f"{where}: a resume must bump the revision")
            extra = [c for c in t.effects.clears if not c.startswith(f.field)]
            if extra or t.effects.sets:
                problems.append(f"{where}: changes more than the flag: {extra or t.effects.sets}")
        if set(f.set_moves) != allocated:
            problems.append(f"flag {name}: set_moves {sorted(f.set_moves)} differ from the states "
                            f"that hold requested Work {sorted(allocated)}")
        for source, target in f.set_moves.items():
            state = LIFECYCLE.get(target)
            if state is None or state_phase(source) != state_phase(target) or not state.launches \
                    or state.kind != "auto_wait":
                problems.append(f"flag {name}: {source} moves to {target}, which is not a state "
                                "of the same phase in which Work is due")
        if set(f.mask) != set(Trigger):
            problems.append(f"flag {name}: mask does not cover every trigger")


def pending_kinds(state: str) -> set[str]:
    """The pending human actions with which ``state`` is entered; ``""`` for
    an entry that does not name its action."""
    kinds = set()
    for e in table_edges():
        if e.target == state and e.source != state:
            for entry in e.transition.effects.sets:
                name, _, value = entry.partition("=")
                if name == "pending_human_action":
                    kinds.add(value)
    return kinds


def _admits(t: Transition, kind: str) -> bool:
    """Whether the pending-action guards of ``t`` allow the action ``kind``."""
    for guard in t.guard:
        if guard in PENDING_GUARDS:
            negated, kinds = PENDING_GUARDS[guard]
            if (kind in kinds) == negated:
                return False
    return True


def _check_human_waits(problems: list[str]) -> None:
    """The resumes of one human wait are mutually exclusive: when a wait has
    several, each is guarded by the pending action, no action satisfies two
    of them, and every action with which the wait is entered satisfies one."""
    for e in table_edges():
        if any(g in PENDING_GUARDS for g in e.transition.guard) and (
                LIFECYCLE[e.source].kind != "human_wait" or e.transition.command != RESUME):
            problems.append(f"{e.source} --{e.verb}--> {e.target}: a pending-action guard on a "
                            "transition that is not the resume of a human wait")
    for key, state in LIFECYCLE.items():
        if state.kind != "human_wait":
            continue
        resumes = [e for e in state_edges(key) if e.transition.command == RESUME and not is_rule_row(e)]
        entering = pending_kinds(key)
        guarded: dict[str, tuple[bool, tuple[str, ...]]] = {}
        for e in resumes:
            guards = [PENDING_GUARDS[g] for g in e.transition.guard if g in PENDING_GUARDS]
            if len(guards) > 1 or (len(resumes) > 1 and not guards):
                problems.append(f"{key} --{e.verb}--> {e.target}: one of {len(resumes)} resumes of "
                                "the wait, so exactly one pending-action guard is required")
            elif guards:
                guarded[e.verb] = guards[0]
        if not guarded:
            continue
        positive = [k for negated, kinds in guarded.values() if not negated for k in kinds]
        for kind in sorted(set(positive)):
            if positive.count(kind) > 1:
                problems.append(f"{key}: several resumes apply when pending_human_action is {kind}")
        named = {k for _negated, kinds in guarded.values() for k in kinds}
        for kind in sorted(named - entering):
            problems.append(f"{key}: a resume is guarded by pending_human_action {kind}, with "
                            "which the wait is never entered")
        negative = [kinds for negated, kinds in guarded.values() if negated]
        if len(negative) > 1 or (negative and set(negative[0]) != set(positive)):
            problems.append(f"{key}: the resume for the other pending actions must exclude "
                            "exactly the actions the other resumes name")
        if "" in entering:
            problems.append(f"{key}: entered without naming the pending action although its "
                            "resumes are guarded by it")
        for kind in sorted(entering - {""}):
            if sum(1 for e in resumes if _admits(e.transition, kind)) != 1:
                problems.append(f"{key}: not exactly one resume applies when pending_human_action "
                                f"is {kind}")


def excluded(a: Transition, b: Transition) -> bool:
    """Whether the guards of ``a`` and ``b`` cannot hold together, by
    ``EXCLUSIVE_GUARDS``."""
    return any((set(x) <= set(a.guard) and set(y) <= set(b.guard))
               or (set(y) <= set(a.guard) and set(x) <= set(b.guard))
               for x, y in EXCLUSIVE_GUARDS)


def due_work_edges(state: str) -> list[Edge]:
    """The controller transitions of a state in which Work is due: the rows
    of the table, without the expansions of rules."""
    return [e for e in state_edges(state)
            if e.transition.trigger == Trigger.TICK and not is_rule_row(e)]


def _check_due_work(problems: list[str]) -> None:
    """In a state in which the controller allocates Work, at most one of its
    controller transitions applies: every two of them carry guards that
    ``EXCLUSIVE_GUARDS`` lists as exclusive. Exhaustion of ``work_total``
    is one transition, guarded by nothing else, so that it applies whatever
    the other budgets hold."""
    guards = {g for e in table_edges() for g in e.transition.guard}
    for x, y in EXCLUSIVE_GUARDS:
        for g in (*x, *y):
            if g not in guards:
                problems.append(f"exclusive guards: {g!r} is not the guard of any transition")
    for key, state in LIFECYCLE.items():
        edges = due_work_edges(key)
        if not any(e.transition.effects.work == "allocate" for e in edges):
            continue
        for i, a in enumerate(edges):
            for b in edges[i + 1:]:
                if not excluded(a.transition, b.transition):
                    problems.append(f"{key}: {a.verb} and {b.verb} can apply together; their "
                                    "guards are not exclusive")
        for e in edges:
            if WORK_TOTAL_EXHAUSTED in e.transition.guard and (
                    e.verb != "exhaust_work_total" or e.transition.guard != (WORK_TOTAL_EXHAUSTED,)
                    or LIFECYCLE[e.target].kind != "human_wait"):
                problems.append(f"{key} --{e.verb}--> {e.target}: exhaustion of work_total where "
                                "Work is due is exhaust_work_total, guarded by nothing else, and "
                                "leads to a human wait")


def exhausted_budgets(edge: Edge) -> list[str]:
    """The budgets whose exhaustion ``edge`` is."""
    return [n for n, b in BUDGETS.items()
            if edge.verb in b.exhaustion_verbs and _names_exhaustion(edge.transition, n)]


def counters_named(edge: Edge) -> set[str]:
    """The counters an exhaustion may name in ``PENDING_COUNTER``: the one it
    states, or any budget it exhausts when it states none."""
    named = [s for s in edge.transition.effects.sets if _field_name(s) == PENDING_COUNTER]
    if not named:
        return set()
    value = named[0].partition("=")[2]
    return {value} if value else set(exhausted_budgets(edge))


def _check_counter_restarts(problems: list[str]) -> None:
    """A resume after a budget was exhausted starts that counter again, with
    no exception: every exhaustion that leads to a human wait names its
    counter in ``PENDING_COUNTER``, and every resume from that wait restarts
    exactly the counters that can be named when it applies. No other
    transition names or restarts a counter."""
    entries: dict[str, list[tuple[set[str], list[str]]]] = {}
    for e in table_edges():
        where = f"{e.source} --{e.verb}--> {e.target}"
        budgets = exhausted_budgets(e)
        named = [s for s in e.transition.effects.sets if _field_name(s) == PENDING_COUNTER]
        counted = bool(budgets) and e.target != e.source \
            and LIFECYCLE[e.target].kind == "human_wait"
        if not counted:
            if named:
                problems.append(f"{where}: sets {PENDING_COUNTER} but is not an exhaustion that "
                                "leads to a human wait")
            continue
        if len(named) != 1:
            problems.append(f"{where}: exhaustion that leads to a human wait without "
                            f"{PENDING_COUNTER}")
            continue
        value = named[0].partition("=")[2]
        if value and value not in budgets:
            problems.append(f"{where}: {PENDING_COUNTER} names {value!r}, which this transition "
                            "does not exhaust")
            continue
        if not value and PENDING_COUNTER not in e.transition.description:
            problems.append(f"{where}: {PENDING_COUNTER} names no counter, so the description "
                            "must state which one it names")
        kinds = [s.partition("=")[2] for s in e.transition.effects.sets
                 if s.startswith("pending_human_action=")]
        entries.setdefault(e.target, []).append((counters_named(e), kinds))
    rule_expected: dict[str, set[str]] = {}
    for e in table_edges():
        t = e.transition
        restarts = set(t.effects.restarts)
        where = f"{e.source} --{e.verb}--> {e.target}"
        resume = t.command == RESUME and LIFECYCLE[e.source].kind == "human_wait"
        if e.verb in RULES:
            if resume:
                rule_expected.setdefault(e.verb, set()).update(
                    n for names, _kinds in entries.get(e.source, ()) for n in names)
            continue
        if not resume:
            if restarts:
                problems.append(f"{where}: only the resume of a human wait starts a counter again")
            continue
        expected = {n for names, kinds in entries.get(e.source, ())
                    if all(_admits(t, k) for k in kinds) for n in names}
        for name in sorted(expected - restarts):
            problems.append(f"{where}: does not start {name!r} again, although an exhaustion of "
                            f"it leads to {e.source} and this resume applies")
        for name in sorted(restarts - expected):
            problems.append(f"{where}: starts {name!r} again, but no exhaustion of it names "
                            f"its counter on the way into {e.source}")
    for verb, rule in RULES.items():
        restarts = set(rule.transition.effects.restarts)
        expected = rule_expected.get(verb, set())
        if restarts and rule.transition.command != RESUME:
            problems.append(f"rule {verb}: only a resume starts a counter again")
        for name in sorted(expected - restarts):
            problems.append(f"rule {verb}: does not start {name!r} again, although it resumes a "
                            "human wait that an exhaustion of it leads to")
        for name in sorted(restarts - expected):
            problems.append(f"rule {verb}: starts {name!r} again, but no exhaustion of it names "
                            "its counter on the way into a human wait the rule resumes")
    for f in FLAGS.values():
        for verb, t in (*f.set_by.items(), *f.cleared_by.items()):
            if t.effects.restarts:
                problems.append(f"flag {f.field} {verb}: a flag transition starts a counter again")


def _check_budgets(policy: dict, problems: list[str]) -> None:
    edges = list(table_edges())
    charged = {n for e in edges for n in e.transition.charges}
    for name, b in BUDGETS.items():
        try:
            limit = budget_limit(name, policy)
        except (KeyError, TypeError, ValueError):
            problems.append(f"budget {name}: policy.json has no integer {b.policy_key}")
            continue
        if limit <= 0:
            problems.append(f"budget {name}: limit {limit} is not positive")
        if name not in charged:
            problems.append(f"budget {name}: no transition charges it")
        for verb in b.exhaustion_verbs:
            if not any(e.verb == verb for e in exhaustion_edges(name)):
                problems.append(f"budget {name}: no transition {verb!r} has a guard that names it")
        if b.policy_key == INFRASTRUCTURE_KEY and not b.infrastructure:
            problems.append(f"budget {name}: uses {INFRASTRUCTURE_KEY} but is not marked "
                            "infrastructure")
        for x in exhaustion_edges(name):
            where = f"{x.source} --{x.verb}--> {x.target}"
            target = LIFECYCLE[x.target]
            same_phase_wait = (target.kind == "human_wait"
                               and state_phase(x.target) == state_phase(x.source))
            if b.infrastructure and not same_phase_wait:
                problems.append(f"{where}: exhaustion of infrastructure budget {name!r} must lead "
                                "to a human wait of the same phase")
            if state_phase(x.source) in NO_AUTOMATIC_PARK and not same_phase_wait:
                problems.append(f"{where}: exhaustion of {name!r} in {state_phase(x.source)} must "
                                "lead to its human wait")
            if name in x.transition.charges:
                problems.append(f"{where}: exhaustion charges {name!r}")
    shared = sorted(n for n, b in BUDGETS.items() if b.policy_key == INFRASTRUCTURE_KEY)
    if len(shared) < 2:
        problems.append(f"budgets: {INFRASTRUCTURE_KEY} is shared by {shared}")
    for e in edges:
        for name in e.transition.charges:
            if name not in BUDGETS:
                continue
            if not [x for x in exhaustion_edges(name) if x.source in (e.source, e.target)]:
                problems.append(f"{e.source} --{e.verb}--> {e.target}: charges {name!r} but no "
                                f"exhaustion transition whose guard names {name!r} leaves "
                                f"{e.source} or {e.target}")


def _check_operations(problems: list[str]) -> None:
    import recovery

    name = "operation"
    if not _check_graph(name, OPERATIONS, OPERATION_INITIAL_STATE,
                        lambda k: not OPERATIONS[k].open, problems):
        return
    if set(OPERATIONS) != set(recovery.STATUSES):
        problems.append(f"operation table states differ from {sorted(recovery.STATUSES)}")
    if {k for k, s in OPERATIONS.items() if s.open} != set(recovery.OPEN):
        problems.append(f"open operation states differ from {sorted(recovery.OPEN)}")
    lifecycle_verbs = {e.verb for e in table_edges()} | {
        v for f in FLAGS.values() for v in (*f.set_by, *f.cleared_by)}
    for e in table_edges(OPERATIONS):
        where = f"operation {e.source} --{e.verb}--> {e.target}"
        _check_transition(where, e.transition, problems, command_guards=(EXACT_OPERATION,))
        effects = e.transition.effects
        if effects.revision != "keep" or effects.generation != "keep" or effects.work != "keep":
            problems.append(f"{where}: an operation transition changes the contribution")
        if e.transition.charges:
            problems.append(f"{where}: an operation transition charges a budget")
        if e.transition.trigger == Trigger.COMMAND and e.transition.command != RESOLVE:
            problems.append(f"{where}: operation commands are `{RESOLVE}`")
        if e.verb in lifecycle_verbs:
            problems.append(f"{where}: the verb is also a lifecycle verb")
    for key, state in OPERATIONS.items():
        resolves = {e.target for e in state_edges(key, OPERATIONS) if e.transition.command == RESOLVE}
        ends = {k for k, s in OPERATIONS.items() if not s.open}
        if state.open and resolves != ends:
            problems.append(f"operation {key}: `{RESOLVE}` leads to {sorted(resolves)}, "
                            f"not to every end state")
    if OPERATION_INITIAL_VERB in {e.verb for e in table_edges(OPERATIONS)}:
        problems.append(f"operation initial verb {OPERATION_INITIAL_VERB!r} is also a transition")


def _check_diagrams(problems: list[str]) -> None:
    from scripts.state_machine_decisions import (
        DECISIONS,
        FURTHER_CHANGES,
        OPEN_ITEMS,
        SETTLED_OPEN_ITEMS,
    )

    def drawn(title: str, source: str, verb: str) -> tuple[str, str, str] | None:
        try:
            _t, target = resolve_node(source, verb)
        except KeyError as exc:
            problems.append(f"{title}: {source} {verb}: {exc.args[0]}")
            return None
        return source.partition("+")[0], verb, target.partition("+")[0]

    def check_node(title: str, node: str, extra: set[str] = frozenset()) -> None:
        state, _, flag = node.partition("+")
        if state not in LIFECYCLE and node not in extra:
            problems.append(f"{title}: node {node!r} is not a table state")
        if flag and flag not in FLAGS:
            problems.append(f"{title}: unknown flag in {node!r}")

    drawn_states: set[str] = set()
    drawn_edges: set[tuple[str, str, str]] = set()
    drawn_verbs: set[str] = set()
    drawn_rules: list[str] = []
    for section in DIAGRAM_SECTIONS:
        for d in section.diagrams:
            title = f"diagram {d.title or section.title!r}"
            for node in d.nodes:
                check_node(title, node)
            for node in (n for n in (d.start, *d.ends) if n):
                if node not in d.nodes:
                    problems.append(f"{title}: start or end node {node!r} is not drawn")
            drawn_states.update(n.partition("+")[0] for n in d.nodes)
            if d.rule is not None:
                drawn_rules.append(d.rule)
                rule = RULES.get(d.rule)
                if rule is None:
                    problems.append(f"{title}: unknown rule {d.rule!r}")
                elif d.edges or d.nodes != (() if rule.target == SELF else (rule.target,)):
                    problems.append(f"{title}: a rule diagram draws only the rule and its target")
                continue
            if len(set(d.edges)) != len(d.edges):
                problems.append(f"{title}: an edge is drawn twice")
            for source, verb in d.edges:
                if source not in d.nodes:
                    problems.append(f"{title}: edge source {source!r} is not drawn")
                    continue
                edge = drawn(title, source, verb)
                if edge:
                    drawn_edges.add(edge)
                    drawn_verbs.add(verb)
                    _t, target = resolve_node(source, verb)
                    if target not in d.nodes:
                        problems.append(f"{title}: {source} {verb} leads to {target}, not drawn")
    for key in LIFECYCLE:
        if key not in drawn_states:
            problems.append(f"{key}: state appears in no diagram")
    for e in table_edges():
        if not is_rule_row(e) and (e.source, e.verb, e.target) not in drawn_edges:
            problems.append(f"{e.source} --{e.verb}--> {e.target}: transition appears in no diagram")
    for verb in RULES:
        if drawn_rules.count(verb) != 1:
            problems.append(f"rule {verb}: drawn in {drawn_rules.count(verb)} rule diagrams, not one")
    for name, f in FLAGS.items():
        for verb in (*f.set_by, *f.cleared_by):
            if verb not in drawn_verbs:
                problems.append(f"flag {name} {verb}: appears in no diagram")
        if f.set_moves and not any((source, verb, target) in drawn_edges
                                   for source, target in f.set_moves.items() for verb in f.set_by):
            problems.append(f"flag {name}: no move out of a state with requested Work is drawn")

    for d in (*DECISIONS, *FURTHER_CHANGES, *SETTLED_OPEN_ITEMS, *OPEN_ITEMS):
        title = f"decision {d.title!r}"
        if d.operations:
            if d.nodes or d.edges:
                problems.append(f"{title}: an operation slice draws the operation table only")
            continue
        candidate_nodes = {n for c in d.candidates for n in (c.source, c.target)}
        for node in d.nodes:
            check_node(title, node, candidate_nodes)
        if len(set(d.edges)) != len(d.edges):
            problems.append(f"{title}: an edge is drawn twice")
        for source, verb in d.edges:
            if source not in d.nodes:
                problems.append(f"{title}: edge source {source!r} is not drawn")
            elif source.partition("+")[0] in LIFECYCLE and drawn(title, source, verb):
                _t, target = resolve_node(source, verb)
                if target not in d.nodes:
                    problems.append(f"{title}: {source} {verb} leads to {target}, not drawn")
        for edge in d.highlighted:
            if edge not in d.edges:
                problems.append(f"{title}: highlighted edge {edge} is not drawn")
        letters = {letter for letter, _ in d.options}
        settled = not any(d is o for o in OPEN_ITEMS)
        if settled and (d.options or d.candidates):
            problems.append(f"{title}: a settled decision lists options")
        if not settled and len(d.options) < 2:
            problems.append(f"{title}: an open item needs at least two options")
        for c in d.candidates:
            if c.option not in letters:
                problems.append(f"{title}: candidate names unknown option {c.option!r}")
            for node in (c.source, c.target):
                if node not in d.nodes:
                    problems.append(f"{title}: candidate node {node!r} is not drawn")


def check_table(policy: dict | None = None) -> list[str]:
    """Every consistency problem of the lifecycle tables; empty when coherent."""
    import json
    from pathlib import Path

    if policy is None:
        policy = json.loads((Path(__file__).resolve().parent / "policy.json").read_text())
    problems: list[str] = []
    if not _check_graph("", LIFECYCLE, INITIAL_STATE,
                        lambda k: LIFECYCLE[k].kind == "terminal", problems):
        return problems
    _check_states(problems)
    _check_rules(problems)
    _check_stale_resume(policy, problems)
    _check_flags(problems)
    _check_budgets(policy, problems)
    _check_due_work(problems)
    _check_human_waits(problems)
    _check_counter_restarts(problems)
    _check_operations(problems)

    # Constants used elsewhere in this module.
    writes = {state_phase(k) for k, s in LIFECYCLE.items() if s.writes}
    if writes != set(WRITE_PHASES):
        problems.append(f"fork-writing phases {sorted(writes)} differ from WRITE_PHASES")
    phases = {state_phase(k) for k in LIFECYCLE}
    if INITIAL_VERB in {e.verb for e in table_edges()}:
        problems.append(f"initial verb {INITIAL_VERB!r} is also a table transition")
    for phase in (*DIAGRAM_PHASE_IDS, *STALE_PHASES, *NO_AUTOMATIC_PARK):
        if phase not in phases:
            problems.append(f"unknown phase {phase!r} in a table constant")
    _check_diagrams(problems)
    return problems


def table_summary() -> str:
    ends = sum(1 for s in LIFECYCLE.values() if s.kind == "terminal")
    transitions = sum(len(s.transitions) for s in LIFECYCLE.values())
    rule_rows = sum(1 for e in table_edges() if is_rule_row(e))
    verbs = len({e.verb for e in table_edges()})
    operation_rows = sum(len(s.transitions) for s in OPERATIONS.values())
    return (f"{len(LIFECYCLE)} states ({ends} end states), {transitions} transitions "
            f"({rule_rows} expanded from {len(RULES)} rules) with {verbs} verbs, "
            f"{len(FLAGS)} flag{'' if len(FLAGS) == 1 else 's'}, {len(BUDGETS)} budgets; operation table: "
            f"{len(OPERATIONS)} states, {operation_rows} transitions")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m lifecycle",
                                     description="Lifecycle table tools.")
    parser.add_argument("command", choices=["check"],
                        help="check: verify the consistency of the lifecycle tables")
    parser.parse_args(argv)
    problems = check_table()
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    print(f"lifecycle table: {table_summary()}; "
          + (f"{len(problems)} problems" if problems else "consistent"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
