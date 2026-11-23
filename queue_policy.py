# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The compiled work-queue policy and the installed one.

The dispatcher's generated lock carries the policy gh-aw v1.2.2 proposes to
the queue on every dispatch (``GH_AW_WORK_QUEUE_POLICY``). gh-aw installs new
worker profiles on demand, but refuses every dispatch once the compiled
policy drops an installed profile (``work_queue_policy_proposal_mismatch``),
for example after a worker workflow is renamed. The dispatcher then
reinstalls the compiled policy as a new Policy epoch once the queue is
quiescent (``scripts/work_queue_policy.cjs``).

This module gives both sides the facts:

* ``proposal`` writes the compiled policy of a lock, with the run's commit
  and repository, for the dispatcher's reinstall step;
* ``status`` tells the controller, from the validating replay of the ledger,
  whether the installed policy is preserved by the compiled one, whether the
  queue is quiescent, and when the installed Policy epoch was published.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from common import repository_root

DISPATCHER_LOCK = Path(".github/workflows/dispatcher.lock.yml")
POLICY_ENV = "GH_AW_WORK_QUEUE_POLICY"
# The only expressions the compiler writes into the policy.
EXPRESSIONS = ("${{ github.sha }}", "${{ github.repository }}")


class PolicyConfigError(ValueError):
    """The lock carries no single, complete compiled queue policy."""


def compiled_text(lock_text: str) -> str:
    """The raw ``GH_AW_WORK_QUEUE_POLICY`` of a generated lock. Every step
    that carries it must carry the same value."""
    lock = yaml.safe_load(lock_text)
    values = set()
    for job in (lock.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            env = step.get("env") or {}
            if any(key.startswith(f"{POLICY_ENV}_") and key != f"{POLICY_ENV}_PARTS"
                   for key in env) or f"{POLICY_ENV}_PARTS" in env:
                raise PolicyConfigError("chunked compiled queue policies are not supported")
            if POLICY_ENV in env:
                values.add(env[POLICY_ENV])
    if len(values) != 1:
        raise PolicyConfigError(f"expected one compiled queue policy, found {len(values)}")
    return values.pop()


def compiled_policy(lock_text: str, *, sha: str, repository: str) -> dict:
    """The policy the dispatcher's run proposes, at commit ``sha`` of
    ``repository``."""
    text = compiled_text(lock_text)
    text = text.replace(EXPRESSIONS[0], sha).replace(EXPRESSIONS[1], repository)
    if "${{" in text:
        raise PolicyConfigError("the compiled queue policy names an unsupported expression")
    policy = json.loads(text)
    if not isinstance(policy, dict) or not isinstance(policy.get("pools"), dict):
        raise PolicyConfigError("the compiled queue policy has no pools")
    return policy


def dropped_profiles(installed: dict, compiled: dict) -> list[str]:
    """``pool/profile`` of every installed worker profile the compiled policy
    no longer declares, and ``pool`` of every installed pool it drops. gh-aw
    refuses every dispatch while one exists. Profiles the compiled policy
    adds are installed on demand and are not listed."""
    out = []
    pools = compiled.get("pools") or {}
    for name, pool in sorted((installed.get("pools") or {}).items()):
        if name not in pools:
            out.append(name)
            continue
        out.extend(f"{name}/{profile}" for profile in sorted(pool.get("profiles") or {})
                   if profile not in (pools[name].get("profiles") or {}))
    return out


def quiescent(projection: dict) -> bool:
    """gh-aw v1.2.2's quiescence: every Work completed or cancelled with no
    pending barrier, and every dispatch released. Only then may a Policy be
    replaced."""
    works = (projection.get("works") or {}).values()
    dispatches = (projection.get("dispatches") or {}).values()
    return (all(w.get("state") in ("completed", "cancelled") and w.get("barrier") != "pending"
                for w in works)
            and all(d.get("released") for d in dispatches))


def installed_at(projection: dict) -> str | None:
    """When the installed Policy epoch was published (ISO 8601), from the
    request that carries its Policy operation."""
    epoch = projection.get("policy_epoch")
    times = [commit.get("at") for commit in (projection.get("requests") or {}).values()
             if commit.get("policy_epoch") == epoch
             and any(op.get("kind") == "Policy" and op.get("epoch") == epoch
                     for op in commit.get("operations") or [])]
    times = [t for t in times if isinstance(t, int)]
    if not times:
        return None
    return datetime.fromtimestamp(min(times) / 1000, tz=timezone.utc).isoformat()


def status(projection: dict, compiled: dict) -> dict:
    """The controller's facts about the queue policy: ``dropped`` (installed
    profiles the compiled policy drops), ``preserved`` (none), ``quiescent``,
    ``epoch`` and ``installed_at``. A queue with no policy yet is preserved:
    the first submission installs the compiled policy."""
    installed = projection.get("policy")
    dropped = dropped_profiles(installed, compiled) if installed else []
    return {"preserved": not dropped, "dropped": dropped, "quiescent": quiescent(projection),
            "epoch": projection.get("policy_epoch"),
            "installed_at": installed_at(projection) if installed else None}


def load_compiled(root: Path | None = None, *, sha: str = "0" * 40,
                  repository: str = "") -> dict:
    """The compiled policy of the dispatcher lock in the checkout."""
    root = root or repository_root()
    return compiled_policy((root / DISPATCHER_LOCK).read_text(encoding="utf-8"), sha=sha,
                           repository=repository)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m queue_policy")
    sub = parser.add_subparsers(dest="command", required=True)
    proposal = sub.add_parser("proposal", help="write the compiled queue policy of a lock")
    proposal.add_argument("--lock", type=Path, default=DISPATCHER_LOCK)
    proposal.add_argument("--sha", required=True)
    proposal.add_argument("--repository", required=True)
    proposal.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    policy = compiled_policy(args.lock.read_text(encoding="utf-8"), sha=args.sha,
                             repository=args.repository)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(policy, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
