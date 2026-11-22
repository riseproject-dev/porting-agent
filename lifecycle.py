# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Contribution lifecycle: phases, ordinary transitions and command handling.

The lifecycle table at the end of this module (``LIFECYCLE``, ``FLAGS`` and
``BUDGETS``) is the definition of the contribution state machine. Documentation and tests are derived from it.

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
# ``FLAGS`` are the orthogonal overrides (``paused``, ``blocked``) that do not
# change phase or activity. ``BUDGETS`` names every retry budget: what one
# charge is, where the count lives, the ``policy.json`` key that sets its
# limit, and the exhaustion verb that leaves it.
#
# ``DIAGRAM_SECTIONS`` is presentation metadata used to render the table as
# diagrams; it selects and orders table entries but adds no transitions.
# ``check_table`` (``python -m lifecycle check``) verifies that the table is
# consistent.


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
    Trigger.RESULT: "A verified native-queue Result folded by reconciliation.",
    Trigger.OPERATION: "A trusted remote operation receipt and its exact readback.",
    Trigger.TICK: "A controller decision without new input: Work allocation, a budget charge "
                  "or exhaustion, or a deadline.",
}

KIND_DESCRIPTIONS = {
    "auto_wait": "Waits for an automatic trigger; the controller acts without a human.",
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


@dataclass(frozen=True)
class Effects:
    """Record effects of a transition.

    ``revision`` and ``generation`` are ``keep`` or ``bump``; a generation
    bump resets every mutable lifecycle field and keeps identity and replay
    fences. ``work`` is what happens to the requested Work (``active_work``):
    ``keep``, ``clear``, ``allocate`` (new Work for this state) or
    ``reallocate`` (a fresh Work graph for the same task). ``sets`` and
    ``clears`` name record fields; ``field=value`` names the value set.
    """

    revision: str = "keep"
    generation: str = "keep"
    work: str = "keep"
    sets: tuple[str, ...] = ()
    clears: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.revision not in ("keep", "bump") or self.generation not in ("keep", "bump"):
            raise ValueError("revision and generation are keep or bump")
        if self.work not in ("keep", "clear", "allocate", "reallocate"):
            raise ValueError(f"unknown work effect {self.work!r}")


@dataclass(frozen=True)
class Transition:
    """One transition, stored under the ``(verb, target)`` key of its state.

    ``command`` is the `/upstreaming` command that triggers it, or, for a
    Result that applies a decision, the command with the same effect.
    ``guard`` lists every condition that must hold; an unconditional
    transition states ``(ALWAYS,)``. ``charges`` names the ``BUDGETS``
    entries one taking of this transition charges.
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
class Flag:
    """An orthogonal override stored in ``field``.

    ``set_by`` (verb ``set_verb``) and ``cleared_by`` (verb ``clear_verb``)
    apply from every non-terminal state and keep phase and activity.
    ``mask`` says what each trigger may still do while the flag is set.
    """

    field: str
    description: str
    set_verb: str
    set_by: Transition
    clear_verb: str
    cleared_by: Transition
    mask: dict[Trigger, str]


@dataclass(frozen=True)
class Budget:
    """A retry budget.

    The limit is the value at ``policy_key`` in ``policy.json`` plus
    ``offset`` (``offset=1`` for budgets whose first try is free). ``unit``
    is what one charge is, ``counter`` where the count lives. When the count
    reaches the limit, ``exhaustion_verb`` is taken instead of the charging
    verb, from the charging state or the state the charging verb leads to;
    its guard names the budget.
    """

    policy_key: str
    offset: int
    unit: str
    counter: str
    exhaustion_verb: str
    description: str = ""


SELF = "@self"
ALWAYS = "always"  # the guard of an unconditional transition
_C, _O, _R, _P, _K = (Trigger.COMMAND, Trigger.OBSERVATION, Trigger.RESULT,
                 Trigger.OPERATION, Trigger.TICK)
_E = Effects

PARK = "/upstreaming park"
RESUME = "/upstreaming resume"
REFRESH = "/upstreaming refresh"

INITIAL_STATE = "queued.idle"
INITIAL_VERB = "accept_request"


# Transitions shared by several states. The key under each state names the
# verb and the target.
_PARK_CONTRIBUTION = Transition(
    _C, PARK,
    ("a reason is given", "any revision, current or stale"),
    _E(work="clear", sets=("parked_reason", "previous_phase")),
    description="Ends the current generation; evidence is kept for audit.")
_REFRESH = Transition(
    _C, REFRESH,
    ("exact revision", "quiescent: no Work, operation, or pending publication",
     "development PR and head recorded"),
    _E(revision="bump", sets=("refresh",),
      clears=("dossier_evidence_id", "scope_coverage_complete", "scope_paths", "proposal_artifact",
              "last_ci_evidence_id", "clean_head_sha", "handoff_request", "handoff_approval",
              "pending_human_action", "publication_operation_id")),
    description="Runs a complete investigation again while keeping the fork and development PR.")
_RETRY_DISPATCH = Transition(
    _K, None,
    ("a dispatcher run ended with the requested Work absent or unclaimed", "dispatch_recovery remains"),
    charges=("dispatch_recovery",),
    description="Launches the dispatcher again for the same committed Work request.")
_EXHAUST_DISPATCH_RECOVERY = Transition(
    _K, None, ("dispatch_recovery exhausted",),
    _E(work="clear", sets=("pending_human_action=dispatch_failed",)))
_ALLOCATE_WORK_RETRY = Transition(
    _K, None,
    ("the Work graph is terminal without a usable Result", "work_recovery remains"),
    _E(revision="bump", work="reallocate"), ("work_recovery",),
    description="A terminal Work graph is never submitted again; a fresh graph is allocated.")
_EXHAUST_WORK_RECOVERY = Transition(
    _K, None, ("work_recovery exhausted",),
    _E(work="clear", sets=("parked_reason", "previous_phase")))
_RECORD_EXPLANATION = Transition(
    _O, None, ("authorized explanation-only inquiry",),
    _E(sets=("processed_review_ids", "processed_review_comment_ids",
            "processed_pr_conversation_comment_ids")),
    description="Answered or recorded without requesting a change; revision and CI evidence are kept.")
_RETURN_FOR_REPAIR = _E(revision="bump", clears=("clean_head_sha", "handoff_request", "pending_human_action"))
_EXHAUST_REPAIR_ATTEMPTS = Transition(
    _K, None,
    ("repair_attempts_cluster, repair_attempts_total, or no_progress is exhausted at allocation",),
    _E(work="clear", sets=("parked_reason", "previous_phase")))


LIFECYCLE: dict[str, State] = {
    "queued.idle": State(
        "auto_wait", True, False,
        "An accepted request is recorded; investigation has not started.",
        transitions={
            ("begin_investigation", "investigation.idle"): Transition(
                _K, None, ("not paused", "not blocked")),
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
                ("worker slot free", "not paused", "not blocked", "no open operation"),
                _E(work="allocate")),
            ("park_contribution", "parked.idle"): _PARK_CONTRIBUTION,
        }),
    "investigation.agent_running": State(
        "work", False, False,
        "Investigation Work is requested or running and will return a dossier.",
        transitions={
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "investigation.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "parked.idle"): _EXHAUST_WORK_RECOVERY,
            ("reject_dossier", "investigation.idle"): Transition(
                _R, None,
                ("the dossier is invalid, or its decision is not develop, park, or close",
                 "investigation_attempts remains"),
                _E(revision="bump", work="clear"), ("investigation_attempts",),
                "Trusted validation rejects the dossier; a new investigation is due."),
            ("exhaust_investigation_recovery", "investigation.waiting_human"): Transition(
                _R, None,
                ("a Result without a dossier, or investigation_attempts exhausted",),
                _E(work="clear", sets=("pending_human_action",))),
            ("admit_development", "provisioning.idle"): Transition(
                _R, None,
                ("verified dossier decision develop", "no refresh recorded"),
                _E(revision="bump", work="clear",
                  sets=("dossier_evidence_id", "admission_class", "scope_paths"))),
            ("readmit_development", "development.fix_needed"): Transition(
                _R, None,
                ("verified dossier decision develop", "refresh recorded",
                 "the recorded fork and development PR still match"),
                _E(revision="bump", work="clear",
                  sets=("dossier_evidence_id", "admission_class", "scope_paths"),
                  clears=("refresh",)),
                description="A refreshed investigation returns to the existing fork for repair."),
            ("park_investigation", "parked.idle"): Transition(
                _R, PARK, ("verified dossier decision park",),
                _E(revision="bump", work="clear", sets=("parked_reason", "previous_phase"))),
            ("close_investigation", "closed.idle"): Transition(
                _R, None, ("verified dossier decision close",),
                _E(work="clear", sets=("terminal_reason",))),
        }),
    "investigation.waiting_human": State(
        "human_wait", False, False,
        "Investigation recovery is exhausted; a human corrects the cause and resumes.",
        transitions={
            ("resume_investigation", "investigation.idle"): Transition(
                _C, RESUME,
                ("exact revision", "quiescent"),
                _E(revision="bump", clears=("pending_human_action",))),
            ("park_contribution", "parked.idle"): _PARK_CONTRIBUTION,
        }),
    "provisioning.idle": State(
        "work", True, True,
        "The fork provisioning operation is planned or running through the trusted workflow.",
        transitions={
            ("start_operation", SELF): Transition(
                _K, None,
                ("fork_provision operation planned", "capacity available", "operation_launch remains",
                 "not paused", "not blocked"),
                charges=("operation_launch",),
                description="Launches the planned operation; an uncertain launch is never repeated "
                            "without discovery."),
            ("require_provisioning_intervention", "provisioning.waiting_human"): Transition(
                _P, None,
                ("a prerequisite cannot be satisfied, an uncertain operation has no discovery, "
                 "or operation_launch is exhausted",),
                _E(sets=("pending_human_action",))),
            ("confirm_provisioning", "development.idle"): Transition(
                _P, None,
                ("the receipt matches the operation", "exact fork and ref readback"),
                _E(revision="bump", sets=("fork", "upstream.repository_id", "upstream.base_ref",
                                         "upstream.base_sha"))),
            ("park_contribution", "parked.idle"): _PARK_CONTRIBUTION,
            ("close_investigation", "closed.idle"): Transition(
                _P, None,
                ("the provisioning readback shows that no contribution is needed",),
                _E(sets=("terminal_reason",))),
        }),
    "provisioning.waiting_human": State(
        "human_wait", False, False,
        "Provisioning cannot continue safely; a human corrects the prerequisite and resumes.",
        transitions={
            ("resume_provisioning", "provisioning.idle"): Transition(
                _C, RESUME,
                ("exact revision", "the prerequisite is corrected", "provisioning_attempts remains"),
                _E(revision="bump", clears=("pending_human_action",)), ("provisioning_attempts",)),
            ("exhaust_provisioning_attempts", "parked.idle"): Transition(
                _K, None,
                ("provisioning_attempts exhausted",),
                _E(sets=("parked_reason", "previous_phase"), clears=("pending_human_action",))),
            ("park_contribution", "parked.idle"): _PARK_CONTRIBUTION,
        }),
    "development.idle": State(
        "auto_wait", True, True,
        "The fork is ready and no development Work is requested yet.",
        transitions={
            ("request_patch", "development.agent_running"): Transition(
                _K, None,
                ("no CI evidence for the head yet", "proposal or write admission", "worker slot free",
                 "capacity available", "not paused", "not blocked"),
                _E(work="allocate")),
            ("refresh_investigation", "queued.idle"): _REFRESH,
            ("park_contribution", "parked.idle"): _PARK_CONTRIBUTION,
        }),
    "development.agent_running": State(
        "work", False, True,
        "Patch Work is requested or running, or its verified Result is being published.",
        transitions={
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "development.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "parked.idle"): _EXHAUST_WORK_RECOVERY,
            ("record_publish_success", "development.waiting_ci"): Transition(
                _R, None,
                ("the Result is published, or uncertain and later confirmed",),
                _E(work="clear", sets=("proposal_artifact", "publication_operation_id"))),
            ("reject_patch_proposal", "development.fix_needed"): Transition(
                _R, None,
                ("the proposal is rejected for the current revision, or the worker reports it is blocked",
                 "no_progress remains"),
                _E(revision="bump", work="clear", clears=("clean_head_sha",)), ("no_progress",)),
            ("replan_development", "development.idle"): Transition(
                _R, None,
                ("the proposal is rejected as stale",), _E(work="clear")),
            ("record_no_change", "development.idle"): Transition(
                _R, None,
                ("the worker reports that no change is needed",), _E(work="clear")),
            ("schedule_publish_retry", SELF): Transition(
                _R, None,
                ("publication failed with an infrastructure error", "publish_recovery remains"),
                _E(work="clear", sets=("pending_publication",)), ("publish_recovery",),
                "The same verified Result is published again; no new Work is requested."),
            ("exhaust_publish_recovery", "development.waiting_human"): Transition(
                _R, None,
                ("publish_recovery exhausted",),
                _E(sets=("pending_human_action=publication_infrastructure_failure",
                        "pending_publication.exhausted"))),
        }),
    "development.waiting_ci": State(
        "auto_wait", False, True,
        "A patch is published to the development PR; CI and review evaluate the exact head.",
        transitions={
            ("record_ci_failure", "development.fix_needed"): Transition(
                _O, None,
                ("an executed blocking failure on the exact head",
                 "the failure is not flaky, or flaky_reruns is exhausted"),
                _E(sets=("last_ci_evidence_id",))),
            ("rerun_failed_ci", SELF): Transition(
                _O, None,
                ("the failure is classified flaky", "flaky_reruns remains"),
                charges=("flaky_reruns",),
                description="Reruns the failed jobs on the exact head."),
            ("record_actionable_review", "development.fix_needed"): Transition(
                _O, None,
                ("authorized actionable review feedback",),
                _E(revision="bump", work="clear", sets=("pending_feedback_evidence_ids",))),
            ("record_explanation", SELF): _RECORD_EXPLANATION,
            ("record_head_change", SELF): Transition(
                _O, None,
                ("the observed fork head differs from the recorded head",),
                _E(revision="bump", sets=("fork.head_sha",),
                  clears=("last_ci_evidence_id", "handoff_approval"))),
            ("nominate_for_handoff", "handoff.awaiting_submission"): Transition(
                _O, None,
                ("every required CI job executed and passed on the exact head",
                 "no pending actionable feedback", "no requested Work"),
                _E(work="clear", sets=("last_ci_evidence_id", "handoff_request",
                                      "pending_human_action=submit_upstream"))),
        }),
    "development.fix_needed": State(
        "auto_wait", True, True,
        "A CI failure, actionable review, or rejected proposal needs a bounded repair.",
        transitions={
            ("request_repair", "development.agent_running"): Transition(
                _K, None,
                ("write admission", "worker slot free", "capacity available",
                 "repair_attempts_cluster, repair_attempts_total, and no_progress remain",
                 "not paused", "not blocked"),
                _E(work="allocate"), ("repair_attempts_cluster", "repair_attempts_total")),
            ("exhaust_repair_attempts", "parked.idle"): _EXHAUST_REPAIR_ATTEMPTS,
            ("park_contribution", "parked.idle"): _PARK_CONTRIBUTION,
        }),
    "development.waiting_human": State(
        "human_wait", False, False,
        "Development recovery is exhausted; a human corrects the cause and resumes.",
        transitions={
            ("resume_development", "development.fix_needed"): Transition(
                _C, RESUME,
                ("exact revision", "quiescent"),
                _E(revision="bump", clears=("pending_human_action", "pending_publication"))),
            ("park_contribution", "parked.idle"): _PARK_CONTRIBUTION,
        }),
    "handoff.awaiting_submission": State(
        "gate", False, False,
        "Required CI passed on the exact head; a human may request upstream submission.",
        transitions={
            ("submit_upstream", "handoff.cleaning_debug"): Transition(
                _C, "/upstreaming submit-upstream",
                ("exact revision", "exact candidate head", "not paused", "not blocked"),
                _E(sets=("handoff_approval", "active_operation_id"), clears=("pending_human_action",)),
                description="The only submission command; it authorizes the whole deterministic "
                            "handoff preparation."),
            ("return_for_repair", "development.fix_needed"): Transition(
                _O, None,
                ("an executed blocking failure or actionable review on the candidate head",),
                _RETURN_FOR_REPAIR),
            ("refresh_investigation", "queued.idle"): _REFRESH,
            ("park_contribution", "parked.idle"): _PARK_CONTRIBUTION,
        }),
    "handoff.cleaning_debug": State(
        "work", False, True,
        "Debug commits are removed from the admitted series and the clean head is published under lease.",
        transitions={
            ("confirm_clean_head", "handoff.waiting_ci"): Transition(
                _P, None,
                ("the lease write and ref readback match",),
                _E(sets=("clean_head_sha", "fork.head_sha"))),
            ("return_for_repair", "development.fix_needed"): Transition(
                _P, None,
                ("the cleanup is invalid, or the exact-head lease conflicts",),
                _RETURN_FOR_REPAIR),
        }),
    "handoff.waiting_ci": State(
        "auto_wait", False, False,
        "Required CI runs on the clean head.",
        transitions={
            ("accept_clean_head_ci", "handoff.closing_internal_pr"): Transition(
                _O, None,
                ("every required CI job executed and passed on the clean head",),
                _E(sets=("last_ci_evidence_id",))),
            ("return_for_repair", "development.fix_needed"): Transition(
                _O, None,
                ("a blocking CI failure on the clean head",), _RETURN_FOR_REPAIR),
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
            ("exhaust_internal_pr_retries", "handoff.waiting_internal_pr"): Transition(
                _P, None,
                ("internal_pr_closure exhausted",), _E(sets=("pending_human_action",))),
        }),
    "handoff.waiting_internal_pr": State(
        "human_wait", False, False,
        "Closing the development PR failed repeatedly; a human corrects it and resumes.",
        transitions={
            ("resume_internal_pr_closure", "handoff.closing_internal_pr"): Transition(
                _C, RESUME,
                ("exact revision",), _E(clears=("pending_human_action",))),
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
            ("exhaust_comment_retries", "handoff.waiting_comment"): Transition(
                _P, None,
                ("submission_comment exhausted",), _E(sets=("pending_human_action",))),
        }),
    "handoff.waiting_comment": State(
        "human_wait", False, False,
        "Posting the submission package failed repeatedly; a human corrects it and resumes.",
        transitions={
            ("resume_submission_preparation", "handoff.preparing_submission"): Transition(
                _C, RESUME,
                ("exact revision",), _E(clears=("pending_human_action",))),
        }),
    "handoff.waiting_upstream_pr": State(
        "locked", False, False,
        "Waiting for a human to open the upstream PR from the package. Exactly two exits leave it.",
        transitions={
            ("record_reminder", SELF): Transition(
                _K, None,
                ("at least 7 days since the package or the last reminder", "not paused"),
                _E(sets=("pending_human_action.reminded_at",)),
                description="Posts an append-only reminder with the package link and exact head."),
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
                ("exact revision", "exact clean head", "quiescent"),
                _E(revision="bump", clears=("pending_human_action", "handoff_request", "handoff_approval")),
                description="The only non-success exit; it stops reminders and unlocks branch work."),
        }),
    "upstream_review.waiting_ci": State(
        "auto_wait", False, False,
        "The upstream PR is open; upstream CI, review, and merge are observed.",
        transitions={
            ("record_upstream_ci_failure", "upstream_review.fix_needed"): Transition(
                _O, None,
                ("an executed blocking failure on the upstream PR head",),
                _E(sets=("last_ci_evidence_id",))),
            ("record_actionable_review", "upstream_review.fix_needed"): Transition(
                _O, None,
                ("authorized actionable review feedback",),
                _E(revision="bump", sets=("pending_feedback_evidence_ids",))),
            ("record_explanation", SELF): _RECORD_EXPLANATION,
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
                 "repair_attempts_cluster, repair_attempts_total, and no_progress remain",
                 "not paused", "not blocked"),
                _E(work="allocate"), ("repair_attempts_cluster", "repair_attempts_total")),
            ("exhaust_repair_attempts", "parked.idle"): _EXHAUST_REPAIR_ATTEMPTS,
        }),
    "upstream_review.agent_running": State(
        "work", False, True,
        "Revision Work for the upstream PR head is requested or running.",
        transitions={
            ("record_revision_publish_success", "upstream_review.waiting_ci"): Transition(
                _R, None,
                ("the revision is published to the upstream PR head",),
                _E(work="clear", sets=("proposal_artifact", "publication_operation_id"))),
            ("retry_dispatch", SELF): _RETRY_DISPATCH,
            ("exhaust_dispatch_recovery", "upstream_review.waiting_human"): _EXHAUST_DISPATCH_RECOVERY,
            ("allocate_work_retry", SELF): _ALLOCATE_WORK_RETRY,
            ("exhaust_work_recovery", "parked.idle"): _EXHAUST_WORK_RECOVERY,
        }),
    "upstream_review.waiting_human": State(
        "human_wait", False, False,
        "The upstream PR was closed unmerged or recovery is exhausted; a human triages.",
        transitions={
            ("resume_upstream_review", "upstream_review.waiting_ci"): Transition(
                _C, RESUME,
                ("exact revision",), _E(revision="bump", clears=("pending_human_action",))),
            ("park_contribution", "parked.idle"): _PARK_CONTRIBUTION,
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
                _C, RESUME,
                ("exact revision",), _E(clears=("pending_human_action",))),
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
                ("exact revision", "quiescent: no Work, operation, or pending publication"),
                _E(revision="bump", generation="bump", work="clear"),
                description="Starts a clean generation; identity, events, evidence, and replay "
                            "fences are kept."),
            ("record_late_effect", SELF): Transition(
                _R, None,
                ("a late Result of the ended generation is published or uncertain",),
                _E(sets=("proposal_artifact", "publication_operation_id")),
                description="Reconciles a late effect for audit without changing state."),
        }),
    "closed.idle": State(
        "terminal", False, False,
        "No further work: withdrawn, not applicable, or already satisfied. The reason is "
        "`terminal_reason`."),
}

FLAGS: dict[str, Flag] = {
    "paused": Flag(
        "paused",
        "A temporary stop of new Work and writer admission. Phase, activity, evidence, and "
        "pending feedback are kept.",
        "pause", Transition(_C, "/upstreaming pause",
                            ("not paused", "not terminal", "any revision, current or stale"),
                            _E(sets=("paused",))),
        "resume_pause", Transition(_C, RESUME, ("paused", "exact revision"),
                                   _E(clears=("paused",))),
        {
            _C: "`resume`, `park`, and `retract-pr` apply; other commands are rejected.",
            _O: "Folded normally.",
            _R: "Folded normally.",
            _P: "Folded normally; an effect that passed its final check before the pause may finish.",
            _K: "No launch, no Work allocation, no budget charge; reminders are suppressed.",
        },
    ),
    "blocked": Flag(
        "blocked_reason",
        "A stop recorded by a human or by the controller when the upstream identity changes. "
        "It fences launches like `paused` and records why.",
        "block", Transition(_C, "/upstreaming block", ("any revision, current or stale",),
                            _E(sets=("blocked_reason",))),
        "resume_block", Transition(_C, RESUME, ("blocked", "exact revision"),
                                   _E(revision="bump", clears=("blocked_reason",))),
        {
            _C: "`resume`, `park`, and `retract-pr` apply; other commands are rejected.",
            _O: "Folded normally.",
            _R: "Folded normally.",
            _P: "Folded normally.",
            _K: "No launch, no Work allocation, no budget charge.",
        },
    ),
}

BUDGETS: dict[str, Budget] = {
    "dispatch_recovery": Budget(
        "retries.infrastructure_per_head_failure", 1,
        "one dispatcher run that ended with the requested Work absent or unclaimed",
        "dispatcher runs since `active_work.requested_at`", "exhaust_dispatch_recovery"),
    "work_recovery": Budget(
        "retries.infrastructure_per_head_failure", 1,
        "one fresh Work graph after terminal Work without a usable Result",
        "`active_work.attempt`", "exhaust_work_recovery"),
    "publish_recovery": Budget(
        "retries.infrastructure_per_head_failure", 1,
        "one publish-only retry of the same verified Result",
        "`pending_publication.attempts`", "exhaust_publish_recovery"),
    "investigation_attempts": Budget(
        "retries.max_attempts_per_contribution", 0,
        "one rejected dossier", "`budget.attempts_used`", "exhaust_investigation_recovery"),
    "repair_attempts_cluster": Budget(
        "retries.patch_attempts_per_cluster", 0,
        "one CI-driven repair Work allocation for one failing CI job",
        "`budget.clusters[<job>]`", "exhaust_repair_attempts"),
    "repair_attempts_total": Budget(
        "retries.max_attempts_per_contribution", 0,
        "one CI-driven repair Work allocation", "`budget.attempts_used`", "exhaust_repair_attempts"),
    "no_progress": Budget(
        "retries.no_progress_review_after", 0,
        "one successive rejected or blocked outcome without progress",
        "`budget.no_progress_streak` (reset by a published patch)", "exhaust_repair_attempts"),
    "flaky_reruns": Budget(
        "retries.flaky_reruns", 0,
        "one rerun of the failed CI jobs on the exact head", "reruns recorded for the head",
        "record_ci_failure"),
    "operation_launch": Budget(
        "retries.infrastructure_per_head_failure", 1,
        "one launch of a planned fork provisioning operation", "the operation's launch attempts",
        "require_provisioning_intervention"),
    "provisioning_attempts": Budget(
        "retries.infrastructure_per_head_failure", 0,
        "one resume of a provisioning human wait", "resumes recorded for the provisioning phase",
        "exhaust_provisioning_attempts"),
    "internal_pr_closure": Budget(
        "retries.infrastructure_per_head_failure", 1,
        "one close-and-readback attempt of the development PR", "the handoff operation's attempts",
        "exhaust_internal_pr_retries"),
    "submission_comment": Budget(
        "retries.infrastructure_per_head_failure", 1,
        "one attempt to post the submission package", "the handoff operation's attempts",
        "exhaust_comment_retries"),
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


def state_edges(state: str):
    """Yield an ``Edge`` for every transition leaving ``state``, in table order."""
    for (verb, to), t in LIFECYCLE[state].transitions.items():
        yield Edge(state, verb, resolve_target(state, to), t)


def table_edges():
    """Yield an ``Edge`` for every table transition, in table order."""
    for state in LIFECYCLE:
        yield from state_edges(state)


def find_edge(state: str, verb: str) -> Edge:
    """The transition ``verb`` from ``state``; the verb must lead to one target."""
    edges = [e for e in state_edges(state) if e.verb == verb]
    if len(edges) != 1:
        raise KeyError(f"{state} has {len(edges)} transitions {verb}")
    return edges[0]


def exhaustion_edges(budget: str):
    """The transitions taken when ``budget`` is exhausted: its exhaustion verb
    with a guard that names the budget."""
    verb = BUDGETS[budget].exhaustion_verb
    for e in table_edges():
        if e.verb == verb and budget in "; ".join(e.transition.guard):
            yield e


def resolve_node(node: str, verb: str) -> tuple[Transition, str]:
    """The transition ``verb`` from a diagram node and the node it leads to.

    ``node`` may carry a flag (``state+flag``); flag transitions keep the
    state and toggle the flag."""
    state, _, flag = node.partition("+")
    if flag and verb == FLAGS[flag].clear_verb:
        return FLAGS[flag].cleared_by, state
    for name, f in FLAGS.items():
        if verb == f.set_verb:
            return f.set_by, f"{node}+{name}"
    edge = find_edge(state, verb)
    return edge.transition, f"{edge.target}+{flag}" if flag and edge.target == state else edge.target


def budget_limit(name: str, policy: dict) -> int:
    budget = BUDGETS[name]
    value: Any = policy
    for part in budget.policy_key.split("."):
        value = value[part]
    return int(value) + budget.offset


# --------------------------------------------------------------------------
# Diagram presentation metadata. Each diagram selects table states (``nodes``,
# in drawing order; phases are grouped in order of first appearance) and
# table transitions (``edges`` as ``(from_state, verb)``, in drawing order). A flag
# diagram draws ``nodes[0]`` with and without the flag.


@dataclass(frozen=True)
class Diagram:
    title: str | None
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    start: str | None = None
    start_verb: str | None = None
    ends: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
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
    "development.idle", "development.agent_running", "development.waiting_ci",
    "development.fix_needed", "development.waiting_human",
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
                ("development.idle", "request_patch"),
                ("development.fix_needed", "request_repair"),
                ("development.agent_running", "record_publish_success"),
                ("development.agent_running", "reject_patch_proposal"),
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
                ("upstream_review.waiting_ci", "record_merge"),
                ("release_wait.idle", "require_release_intervention"),
                ("release_wait.waiting_human", "resume_release_observation"),
                ("release_wait.idle", "verify_release"),
                ("parked.idle", "restart_generation"),
            ), start=INITIAL_STATE, start_verb=INITIAL_VERB, ends=("released.idle", "closed.idle"),
                text="On both the development PR and the upstream PR, a CI failure keeps the "
                     "contribution in its phase while a bounded repair is prepared and tested; an "
                     "actionable review comment is answered and included in the next repair; an "
                     "explanation-only comment is answered or recorded without requesting a change."),
        ),
    ),
    DiagramSection(
        "Investigation and provisioning failure cases",
        "Investigation and provisioning fail in different ways, and each case has its own recovery "
        "path.",
        (
            Diagram(
                "Failure case 1: investigation finds no development path",
                ("investigation.idle", "investigation.agent_running", "provisioning.idle",
                 "parked.idle", "queued.idle", "closed.idle"),
                (("investigation.idle", "dispatch_investigation"),
                 ("investigation.agent_running", "admit_development"),
                 ("investigation.agent_running", "park_investigation"),
                 ("investigation.agent_running", "close_investigation"),
                 ("parked.idle", "restart_generation")),
                start="investigation.idle", ends=("closed.idle",),
                text="The verified dossier decides between development, park, and close. "
                     "`closed` is the single terminal phase for work that is not needed; the reason "
                     "is `terminal_reason`."),
            Diagram(
                "Failure case 2: the dossier is incomplete or invalid",
                ("investigation.idle", "investigation.agent_running", "provisioning.idle",
                 "parked.idle", "queued.idle"),
                (("investigation.idle", "dispatch_investigation"),
                 ("investigation.agent_running", "reject_dossier"),
                 ("investigation.agent_running", "admit_development"),
                 ("investigation.agent_running", "park_investigation"),
                 ("parked.idle", "restart_generation")),
                start="investigation.idle",
                text="`reject_dossier` retries automatically while `investigation_attempts` remains. "
                     "An incomplete artifact is never adopted by hand."),
            Diagram(
                "Failure case 3: the investigator or dispatcher has an infrastructure failure",
                ("investigation.idle", "investigation.agent_running", "investigation.waiting_human",
                 "provisioning.idle", "parked.idle", "queued.idle", "closed.idle"),
                (("investigation.idle", "dispatch_investigation"),
                 ("investigation.agent_running", "reject_dossier"),
                 ("investigation.agent_running", "exhaust_investigation_recovery"),
                 ("investigation.waiting_human", "resume_investigation"),
                 ("investigation.agent_running", "admit_development"),
                 ("investigation.agent_running", "park_investigation"),
                 ("investigation.agent_running", "close_investigation"),
                 ("parked.idle", "restart_generation"),
                 ("investigation.agent_running", "retry_dispatch"),
                 ("investigation.agent_running", "allocate_work_retry"),
                 ("investigation.agent_running", "exhaust_dispatch_recovery"),
                 ("investigation.agent_running", "exhaust_work_recovery"),
                 ("investigation.waiting_human", "park_contribution"),
                 ("investigation.idle", "park_contribution")),
                start="investigation.idle", ends=("closed.idle",),
                text="Dispatch and terminal-Work recovery are automatic within `dispatch_recovery` "
                     "and `work_recovery`. A terminal Work graph is never submitted again under the "
                     "same identity."),
            Diagram(
                "Failure case 4: fork provisioning has a transient failure",
                ("provisioning.idle", "provisioning.waiting_human", "development.idle", "parked.idle"),
                (("provisioning.idle", "start_operation"),
                 ("provisioning.idle", "require_provisioning_intervention"),
                 ("provisioning.waiting_human", "resume_provisioning"),
                 ("provisioning.idle", "confirm_provisioning"),
                 ("provisioning.idle", "park_contribution")),
                start="provisioning.idle",
                text="`confirm_provisioning` follows fork creation, configuration, receipt "
                     "validation, and exact readback. `start_operation` relaunches within "
                     "`operation_launch`."),
            Diagram(
                "Failure case 5: provisioning outcome is uncertain",
                ("provisioning.idle", "provisioning.waiting_human", "development.idle"),
                (("provisioning.idle", "require_provisioning_intervention"),
                 ("provisioning.waiting_human", "resume_provisioning"),
                 ("provisioning.idle", "confirm_provisioning")),
                start="provisioning.idle",
                text="A lost receipt is not proof that an operation did not launch; an uncertain "
                     "effect is resolved by discovery or `/upstreaming resolve-operation`, never "
                     "repeated blindly."),
            Diagram(
                "Failure case 6: provisioning cannot safely satisfy a prerequisite",
                ("provisioning.idle", "provisioning.waiting_human", "development.idle", "parked.idle",
                 "closed.idle"),
                (("provisioning.idle", "confirm_provisioning"),
                 ("provisioning.idle", "require_provisioning_intervention"),
                 ("provisioning.waiting_human", "resume_provisioning"),
                 ("provisioning.waiting_human", "exhaust_provisioning_attempts"),
                 ("provisioning.waiting_human", "park_contribution"),
                 ("provisioning.idle", "park_contribution"),
                 ("provisioning.idle", "close_investigation")),
                start="provisioning.idle", ends=("closed.idle",),
                text="Correct the external permission or identity problem first, then resume. "
                     "`exhaust_provisioning_attempts` parks after `provisioning_attempts`."),
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
                ("development.idle", "development.agent_running", "development.waiting_ci",
                 "handoff.awaiting_submission"),
                (("development.idle", "request_patch"),
                 ("development.agent_running", "record_publish_success"),
                 ("development.waiting_ci", "nominate_for_handoff")),
                start="development.idle",
                text="Every applicable build and test job must execute and pass on the exact head."),
            Diagram(
                "Failure case 1: patch proposal or dispatch failure",
                ("development.idle", "development.agent_running", "development.fix_needed",
                 "development.waiting_human", "parked.idle", "queued.idle"),
                (("development.idle", "request_patch"),
                 ("development.agent_running", "reject_patch_proposal"),
                 ("development.fix_needed", "request_repair"),
                 ("development.agent_running", "exhaust_publish_recovery"),
                 ("development.waiting_human", "resume_development"),
                 ("development.fix_needed", "park_contribution"),
                 ("parked.idle", "restart_generation"),
                 ("development.agent_running", "retry_dispatch"),
                 ("development.agent_running", "allocate_work_retry"),
                 ("development.agent_running", "exhaust_dispatch_recovery"),
                 ("development.agent_running", "exhaust_work_recovery"),
                 ("development.fix_needed", "exhaust_repair_attempts")),
                start="development.idle",
                text="Dispatch recovery and bounded repair are automatic. Repair-budget exhaustion "
                     "parks; `restart_generation` starts a clean generation that cannot bypass "
                     "dossier validation."),
            Diagram(
                "Failure case 2: trusted publication failure",
                ("development.agent_running", "development.fix_needed", "development.waiting_human",
                 "development.waiting_ci", "development.idle"),
                (("development.agent_running", "record_publish_success"),
                 ("development.agent_running", "reject_patch_proposal"),
                 ("development.agent_running", "exhaust_publish_recovery"),
                 ("development.fix_needed", "request_repair"),
                 ("development.waiting_human", "resume_development"),
                 ("development.agent_running", "schedule_publish_retry"),
                 ("development.agent_running", "replan_development"),
                 ("development.agent_running", "record_no_change")),
                start="development.agent_running",
                text="A validation failure needs new bounded repair Work. An infrastructure failure "
                     "publishes the same verified Result again within `publish_recovery`."),
            Diagram(
                "Failure case 3: internal PR CI or review failure",
                ("development.waiting_ci", "development.fix_needed", "development.agent_running",
                 "handoff.awaiting_submission"),
                (("development.waiting_ci", "record_ci_failure"),
                 ("development.waiting_ci", "record_actionable_review"),
                 ("development.waiting_ci", "record_explanation"),
                 ("development.fix_needed", "request_repair"),
                 ("development.agent_running", "record_publish_success"),
                 ("development.waiting_ci", "nominate_for_handoff"),
                 ("development.waiting_ci", "rerun_failed_ci"),
                 ("development.waiting_ci", "record_head_change")),
                start="development.waiting_ci",
                text="Build and test failures and actionable comments request repair automatically. "
                     "Explanation-only comments do not bump the revision, invalidate CI, or request "
                     "repair."),
            Diagram(
                "Failure case 4: development must be re-investigated or stopped",
                ("development.idle", "queued.idle", "investigation.idle", "investigation.agent_running",
                 "development.fix_needed", "parked.idle"),
                (("development.idle", "refresh_investigation"),
                 ("queued.idle", "begin_investigation"),
                 ("investigation.idle", "dispatch_investigation"),
                 ("investigation.agent_running", "readmit_development"),
                 ("development.idle", "park_contribution"),
                 ("parked.idle", "restart_generation")),
                start="development.idle",
                text="`refresh` runs a complete investigation without deleting the fork or "
                     "development PR. `park` ends the current generation."),
        ),
    ),
    DiagramSection(
        "Pause, waiting-human, and park cases",
        "Pause and block keep the lifecycle phase and activity. `waiting_human` is entered "
        "automatically when a specific failure or external prerequisite needs operator action. "
        "Park ends the current generation; resume from parked performs a clean reset.",
        (
            Diagram(
                "Success case 1: temporary pause and successful resume",
                ("development.waiting_ci",), (), start="development.waiting_ci", flags=("paused",),
                text="Pause stops new Work and writer admission. An effect that passed its final "
                     "publication check before the pause may finish and is reconciled normally."),
            Diagram(
                "Success case 2: block and resume",
                ("development.waiting_ci",), (), start="development.waiting_ci", flags=("blocked",),
                text="Block records a reason and fences launches like pause. Resuming a block names "
                     "the exact revision and bumps it."),
            Diagram(
                "Failure case 1: waiting for operator action or an external prerequisite",
                ("development.agent_running", "development.waiting_human", "development.fix_needed",
                 "parked.idle"),
                (("development.agent_running", "exhaust_publish_recovery"),
                 ("development.waiting_human", "resume_development"),
                 ("development.waiting_human", "park_contribution")),
                start="development.agent_running",
                text="The controller records the concrete reason in `pending_human_action`. Correct "
                     "the stated problem first, then resume with the exact revision."),
            Diagram(
                "Failure case 2: park and later resume as a clean generation",
                ("development.idle", "parked.idle", "queued.idle"),
                (("development.idle", "park_contribution"),
                 ("parked.idle", "record_late_effect"),
                 ("parked.idle", "restart_generation")),
                start="development.idle",
                text="`restart_generation` is accepted only when the contribution is quiescent. It "
                     "bumps the generation, clears mutable lifecycle state, and starts at "
                     "`queued.idle`. It does not close PRs, delete branches, or delete repositories."),
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
                 ("handoff.awaiting_submission", "submit_upstream"),
                 ("handoff.cleaning_debug", "confirm_clean_head"),
                 ("handoff.waiting_ci", "accept_clean_head_ci"),
                 ("handoff.closing_internal_pr", "confirm_internal_pr_closed"),
                 ("handoff.preparing_submission", "confirm_submission_package"),
                 ("handoff.waiting_upstream_pr", "record_reminder"),
                 ("handoff.waiting_upstream_pr", "verify_upstream_pr")),
                text="`submit-upstream` is the only submission command. The human follows the "
                     "prefilled link and opens the PR under their own identity; observation "
                     "verifies it before `upstream_review`."),
            Diagram(
                "Failure case 1: pre-submission changes versus the locked upstream-PR wait",
                ("handoff.awaiting_submission", "handoff.cleaning_debug", "handoff.waiting_ci",
                 "handoff.closing_internal_pr", "handoff.preparing_submission",
                 "handoff.waiting_upstream_pr", "development.fix_needed", "queued.idle", "parked.idle"),
                (("handoff.awaiting_submission", "submit_upstream"),
                 ("handoff.cleaning_debug", "confirm_clean_head"),
                 ("handoff.waiting_ci", "accept_clean_head_ci"),
                 ("handoff.closing_internal_pr", "confirm_internal_pr_closed"),
                 ("handoff.preparing_submission", "confirm_submission_package"),
                 ("handoff.waiting_upstream_pr", "record_audit_observation"),
                 ("handoff.cleaning_debug", "return_for_repair"),
                 ("handoff.waiting_ci", "return_for_repair"),
                 ("handoff.waiting_upstream_pr", "retract_submission"),
                 ("handoff.awaiting_submission", "return_for_repair"),
                 ("handoff.awaiting_submission", "refresh_investigation"),
                 ("handoff.awaiting_submission", "park_contribution")),
                start="handoff.awaiting_submission",
                text="Before the package is posted, cleanup validation, an exact-head lease "
                     "conflict, or blocking CI on the clean head returns to development. "
                     "`handoff.waiting_upstream_pr` is locked: only `verify_upstream_pr` and "
                     "`retract_submission` leave it."),
            Diagram(
                "Failure case 2: submission-package publication or PR verification fails",
                ("handoff.closing_internal_pr", "handoff.waiting_internal_pr",
                 "handoff.preparing_submission", "handoff.waiting_comment",
                 "handoff.waiting_upstream_pr", "upstream_review.waiting_ci"),
                (("handoff.closing_internal_pr", "confirm_internal_pr_closed"),
                 ("handoff.closing_internal_pr", "exhaust_internal_pr_retries"),
                 ("handoff.waiting_internal_pr", "resume_internal_pr_closure"),
                 ("handoff.preparing_submission", "confirm_submission_package"),
                 ("handoff.preparing_submission", "exhaust_comment_retries"),
                 ("handoff.waiting_comment", "resume_submission_preparation"),
                 ("handoff.waiting_upstream_pr", "record_reminder"),
                 ("handoff.waiting_upstream_pr", "verify_upstream_pr"),
                 ("handoff.closing_internal_pr", "retry_internal_pr_closure"),
                 ("handoff.preparing_submission", "retry_submission_comment")),
                start="handoff.closing_internal_pr",
                text="Closure and package recovery happen before the locked wait and do not need "
                     "another `submit-upstream`."),
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
                 "upstream_review.agent_running", "upstream_review.waiting_human",
                 "release_wait.idle", "parked.idle"),
                (("upstream_review.waiting_ci", "record_upstream_ci_failure"),
                 ("upstream_review.waiting_ci", "record_actionable_review"),
                 ("upstream_review.waiting_ci", "record_explanation"),
                 ("upstream_review.fix_needed", "request_upstream_revision"),
                 ("upstream_review.agent_running", "record_revision_publish_success"),
                 ("upstream_review.waiting_ci", "record_merge"),
                 ("upstream_review.agent_running", "retry_dispatch"),
                 ("upstream_review.agent_running", "allocate_work_retry"),
                 ("upstream_review.agent_running", "exhaust_dispatch_recovery"),
                 ("upstream_review.agent_running", "exhaust_work_recovery"),
                 ("upstream_review.fix_needed", "exhaust_repair_attempts")),
                start="upstream_review.waiting_ci",
                text="Automated upstream revisions require that the registered PR head is the "
                     "controlled fork ref. Otherwise patches go to the submitter."),
            Diagram(
                "Failure case 4: upstream PR is closed or maintainer intent is unclear",
                ("upstream_review.waiting_ci", "upstream_review.waiting_human", "parked.idle"),
                (("upstream_review.waiting_ci", "record_pr_closed_unmerged"),
                 ("upstream_review.waiting_human", "resume_upstream_review"),
                 ("upstream_review.waiting_human", "park_contribution")),
                start="upstream_review.waiting_ci",
                text="The pipeline does not reopen the PR or create a replacement. Human triage "
                     "either resumes or parks."),
            Diagram(
                "Failure case 5: release is missing, incomplete, or waiting for operator action",
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
)

# Short node ids used when drawing states (``phase_activity`` otherwise).
DIAGRAM_PHASE_IDS = {"upstream_review": "upstream"}


# --------------------------------------------------------------------------
# Consistency check of the table, run in CI as ``python -m lifecycle check``.

# Record fields whose change is a change of the requested work, so a
# transition that sets or clears one bumps the revision.
REVISION_FIELDS = ("scope_paths", "pending_feedback_evidence_ids", "handoff_approval")
_REVISION_SET_EXEMPT = ("handoff_approval",)  # granting approval keeps the revision
_REVISION_GUARDS = ("exact revision", "any revision, current or stale")


def _field_name(entry: str) -> str:
    return entry.split("=", 1)[0].split(".", 1)[0]


def _reachable(start: str, step) -> set[str]:
    seen, todo = {start}, [start]
    while todo:
        for nxt in step(todo.pop()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def _check_transition(where: str, source: str | None, t: Transition, problems: list[str]) -> None:
    if not isinstance(t.trigger, Trigger):
        problems.append(f"{where}: trigger {t.trigger!r} is not a Trigger")
    if not t.guard:
        problems.append(f"{where}: empty guard; write ({ALWAYS!r},) for an unconditional transition")
    elif ALWAYS in t.guard and len(t.guard) > 1:
        problems.append(f"{where}: {ALWAYS!r} combined with other conditions")
    if any(not g.strip() for g in t.guard):
        problems.append(f"{where}: blank guard condition")
    if t.trigger == Trigger.COMMAND:
        if not (t.command or "").startswith("/upstreaming "):
            problems.append(f"{where}: command trigger without an `/upstreaming` command")
        if not any(g in _REVISION_GUARDS for g in t.guard):
            problems.append(f"{where}: command guard names neither {_REVISION_GUARDS[0]!r} "
                            f"nor {_REVISION_GUARDS[1]!r}")
    elif t.command and t.trigger != Trigger.RESULT:
        problems.append(f"{where}: command {t.command!r} on a {t.trigger} trigger")
    e = t.effects
    if e.generation == "bump" and (e.revision != "bump" or e.work != "clear"):
        problems.append(f"{where}: a generation bump must bump the revision and clear Work")
    if e.work == "reallocate" and e.revision != "bump":
        problems.append(f"{where}: reallocating Work must bump the revision")
    changed = {_field_name(f) for f in e.sets if _field_name(f) not in _REVISION_SET_EXEMPT}
    changed |= {_field_name(f) for f in e.clears}
    if changed & set(REVISION_FIELDS) and e.revision != "bump":
        problems.append(f"{where}: changes {sorted(changed & set(REVISION_FIELDS))} "
                        "without a revision bump")
    if e.work == "allocate":
        if source is not None and not LIFECYCLE[source].launches:
            problems.append(f"{where}: allocates Work from a state that does not launch")
        for g in ("not paused", "not blocked"):
            if g not in t.guard:
                problems.append(f"{where}: allocates Work without the guard {g!r}")
    guard_text = "; ".join(t.guard)
    for name in t.charges:
        if name not in BUDGETS:
            problems.append(f"{where}: charges unknown budget {name!r}")
        elif name not in guard_text:
            problems.append(f"{where}: charges {name!r} but its guard does not mention it")


def check_table(policy: dict | None = None) -> list[str]:
    """Every consistency problem of the lifecycle table; empty when coherent."""
    import json
    from pathlib import Path

    from scripts.state_machine_decisions import DECISIONS

    if policy is None:
        policy = json.loads((Path(__file__).resolve().parent / "policy.json").read_text())
    problems: list[str] = []
    ends = {k for k, s in LIFECYCLE.items() if s.kind == "terminal"}
    edges = list(table_edges())

    # States, targets, and end states.
    if INITIAL_STATE not in LIFECYCLE:
        problems.append(f"initial state {INITIAL_STATE!r} is not a table state")
        return problems
    if not ends:
        problems.append("no state is marked as an end state (kind 'terminal')")
    for key, state in LIFECYCLE.items():
        phase, _, activity = key.partition(".")
        if not phase or not activity or "." in activity:
            problems.append(f"{key}: state key is not phase.activity")
        for verb, to in state.transitions:
            if not verb:
                problems.append(f"{key}: transition with an empty verb")
            if to != SELF and to not in LIFECYCLE:
                problems.append(f"{key} --{verb}--> {to}: target is not a table state")
        if state.kind == "terminal":
            if state.transitions:
                problems.append(f"{key}: end state has outgoing transitions")
            if state.launches or state.writes:
                problems.append(f"{key}: end state launches or writes")
        elif not [v for v, to in state.transitions if resolve_target(key, to) != key]:
            problems.append(f"{key}: not an end state but no transition leaves it")
        if state.kind in ("locked", "human_wait", "generation_end") and (state.launches or state.writes):
            problems.append(f"{key}: {state.kind} state launches or writes")
        if state.kind in ("human_wait", "generation_end") and not any(
                t.command == RESUME for t in state.transitions.values()):
            problems.append(f"{key}: {state.kind} state has no `{RESUME}` exit")
        if state.kind == "gate" and not any(
                t.trigger == Trigger.COMMAND for t in state.transitions.values()):
            problems.append(f"{key}: gate state has no command exit")
    if any(p.endswith("is not a table state") for p in problems):
        return problems

    successors: dict[str, set[str]] = {k: set() for k in LIFECYCLE}
    predecessors: dict[str, set[str]] = {k: set() for k in LIFECYCLE}
    for e in edges:
        successors[e.source].add(e.target)
        predecessors[e.target].add(e.source)
    reached = _reachable(INITIAL_STATE, successors.__getitem__)
    for key in LIFECYCLE:
        if key not in reached:
            kind = "end state" if key in ends else "state"
            problems.append(f"{key}: {kind} is not reachable from {INITIAL_STATE}")
    can_end: set[str] = set()
    for end in ends:
        can_end |= _reachable(end, predecessors.__getitem__)
    for key in LIFECYCLE:
        if key not in can_end:
            problems.append(f"{key}: no end state is reachable from this state")

    # Transitions. A verb leads from a state to one target only, so the
    # machine is deterministic; several verbs may share a target.
    for key, state in LIFECYCLE.items():
        targets: dict[str, list[str]] = {}
        for e in state_edges(key):
            where = f"{key} --{e.verb}--> {e.target}"
            targets.setdefault(e.verb, []).append(e.target)
            _check_transition(where, key, e.transition, problems)
            target = LIFECYCLE[e.target]
            entering = e.target != key
            sets = {_field_name(f) for f in e.transition.effects.sets}
            if entering and target.kind == "human_wait" and state.kind != "human_wait" \
                    and "pending_human_action" not in sets:
                problems.append(f"{where}: enters a human wait without setting pending_human_action")
            if entering and target.kind == "generation_end" \
                    and not {"parked_reason", "previous_phase"} <= sets:
                problems.append(f"{where}: ends the generation without parked_reason and previous_phase")
        for verb, to in targets.items():
            if len(to) > 1:
                problems.append(f"{key} --{verb}-->: the verb leads to {len(to)} targets "
                                f"({', '.join(to)}); a verb from a state must lead to one target")

    # Flags.
    for name, f in FLAGS.items():
        for verb, t, what in ((f.set_verb, f.set_by, "set"), (f.clear_verb, f.cleared_by, "clear")):
            if not verb or t is None:
                problems.append(f"flag {name}: no {what} transition")
                continue
            if t.trigger != Trigger.COMMAND:
                problems.append(f"flag {name}: {verb} is not command-triggered")
            _check_transition(f"flag {name} {verb}", None, t, problems)
        if f.field not in f.set_by.effects.sets or f.field not in f.cleared_by.effects.clears:
            problems.append(f"flag {name}: set and clear transitions do not set and clear {f.field}")
        if set(f.mask) != set(Trigger):
            problems.append(f"flag {name}: mask does not cover every trigger")

    # Budgets.
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
        if not any(e.verb == b.exhaustion_verb for e in edges):
            problems.append(f"budget {name}: exhaustion verb {b.exhaustion_verb!r} is not in the table")
    for e in edges:
        for name in e.transition.charges:
            if name not in BUDGETS:
                continue
            verb = BUDGETS[name].exhaustion_verb
            exits = [x for x in exhaustion_edges(name) if x.source in (e.source, e.target)]
            if not exits:
                problems.append(f"{e.source} --{e.verb}--> {e.target}: charges {name!r} but no "
                                f"{verb!r} whose guard names {name!r} leaves {e.source} or "
                                f"{e.target}")
            for x in exits:
                if name in x.transition.charges:
                    problems.append(f"{x.source} --{verb}--> {x.target}: exhaustion charges {name!r}")

    # Constants used elsewhere in this module.
    writes = {state_phase(k) for k, s in LIFECYCLE.items() if s.writes}
    if writes != set(WRITE_PHASES):
        problems.append(f"fork-writing phases {sorted(writes)} differ from WRITE_PHASES")
    phases = {state_phase(k) for k in LIFECYCLE}
    if INITIAL_VERB in {e.verb for e in edges}:
        problems.append(f"initial verb {INITIAL_VERB!r} is also a table transition")
    for phase in DIAGRAM_PHASE_IDS:
        if phase not in phases:
            problems.append(f"DIAGRAM_PHASE_IDS names unknown phase {phase!r}")

    # Diagrams and decision slices.
    def drawn(title: str, source: str, verb: str) -> tuple[str, str, str] | None:
        try:
            _t, target = resolve_node(source, verb)
        except KeyError as exc:
            problems.append(f"{title}: {source} {verb}: {exc.args[0]}")
            return None
        return source.partition("+")[0], verb, target.partition("+")[0]

    drawn_states: set[str] = set()
    drawn_edges: set[tuple[str, str, str]] = set()
    drawn_flags: set[str] = set()
    for section in DIAGRAM_SECTIONS:
        for d in section.diagrams:
            title = f"diagram {d.title or section.title!r}"
            for node in (*d.nodes, *(n for n in (d.start, *d.ends) if n)):
                if node not in LIFECYCLE:
                    problems.append(f"{title}: node {node!r} is not a table state")
                if node not in d.nodes:
                    problems.append(f"{title}: start or end node {node!r} is not drawn")
            for flag in d.flags:
                if flag not in FLAGS:
                    problems.append(f"{title}: unknown flag {flag!r}")
            drawn_states.update(d.nodes)
            drawn_flags.update(d.flags)
            if len(set(d.edges)) != len(d.edges):
                problems.append(f"{title}: an edge is drawn twice")
            for source, verb in d.edges:
                edge = drawn(title, source, verb)
                if edge:
                    drawn_edges.add(edge)
                    if edge[2] not in d.nodes:
                        problems.append(f"{title}: {source} {verb} leads to {edge[2]}, not drawn")
    for key in LIFECYCLE:
        if key not in drawn_states:
            problems.append(f"{key}: state appears in no diagram")
    for e in edges:
        if (e.source, e.verb, e.target) not in drawn_edges:
            problems.append(f"{e.source} --{e.verb}--> {e.target}: transition appears in no diagram")
    for flag in FLAGS:
        if flag not in drawn_flags:
            problems.append(f"flag {flag}: appears in no diagram")
    for d in DECISIONS:
        title = f"decision {d.title!r}"
        candidate_nodes = {n for c in d.candidates for n in (c.source, c.target)}
        for node in d.nodes:
            state, _, flag = node.partition("+")
            if state not in LIFECYCLE and node not in candidate_nodes:
                problems.append(f"{title}: node {node!r} is neither a table state nor a candidate")
            if flag and flag not in FLAGS:
                problems.append(f"{title}: unknown flag in {node!r}")
        if len(set(d.edges)) != len(d.edges):
            problems.append(f"{title}: an edge is drawn twice")
        for source, verb in d.edges:
            if source not in d.nodes:
                problems.append(f"{title}: edge source {source!r} is not drawn")
            elif source.partition("+")[0] in LIFECYCLE:
                edge = drawn(title, source, verb)
                if edge:
                    _t, target = resolve_node(source, verb)
                    if target not in d.nodes:
                        problems.append(f"{title}: {source} {verb} leads to {target}, not drawn")
        for edge in d.contested:
            if edge not in d.edges:
                problems.append(f"{title}: contested edge {edge} is not drawn")
        letters = {letter for letter, _ in d.options}
        for c in d.candidates:
            if c.option not in letters:
                problems.append(f"{title}: candidate names unknown option {c.option!r}")
            for node in (c.source, c.target):
                if node not in d.nodes:
                    problems.append(f"{title}: candidate node {node!r} is not drawn")
    return problems


def table_summary() -> str:
    ends = sum(1 for s in LIFECYCLE.values() if s.kind == "terminal")
    transitions = sum(len(s.transitions) for s in LIFECYCLE.values())
    return (f"{len(LIFECYCLE)} states ({ends} end states), {transitions} transitions, "
            f"{len(FLAGS)} flags, {len(BUDGETS)} budgets")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m lifecycle",
                                     description="Lifecycle table tools.")
    parser.add_argument("command", choices=["check"],
                        help="check: verify the consistency of the lifecycle table")
    parser.parse_args(argv)
    problems = check_table()
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    print(f"lifecycle table: {table_summary()}; "
          + (f"{len(problems)} problems" if problems else "consistent"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
