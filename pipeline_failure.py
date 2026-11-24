# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A run of an agent workflow that failed before its agent ran, for a step
of the pipeline.

The worker, planner and dispatcher runs prepare their agent in
steps that do not depend on the contribution (the gateway, the runner, the
tools) and in a few that do (the checkout of the contribution's fork). A run
that fails in the first kind before ``AGENT_STEP`` is a failure of the
pipeline: it says nothing about the Work, so the controller does not charge
the Work for it (``lifecycle.PIPELINE_FAILURE``).

``classify`` is pure: it reads the run and its jobs as the REST API returns
them. ``tree_version`` is the version of the pipeline code, the one
``reconcile.pipeline_version`` reads from a checkout and
``collect.pipeline_version_at`` from the API."""

from __future__ import annotations

import hashlib
from typing import Iterable

# The step that runs the agent, once in each of the four lock files.
AGENT_STEP = "Execute Pi CLI"
AGENT_JOB = "agent"
ACTIVATION_JOB = "activation"

# The pre-agent steps, by workflow file, that depend on the contribution: a
# failure in one of them is the contribution's. Every other step before the
# agent is the pipeline's (the setup of gh-aw, the gateway, the tools, and the
# step that resolves the task file and fetches the plan, the history and the
# report the controller committed, at their pinned commit and digest: a
# missing or mismatching one is no fault of the contribution). The
# worker's "Resolve anonymous reproduction image" pulls one fixed image of the
# pipeline (`worker.md`): it is the pipeline's.
CONTRIBUTION_STEPS: dict[str, frozenset[str]] = {
    "worker.lock.yml": frozenset({
        "Check out the contribution fork at the expected head (read-only)",
    }),
    "planner.lock.yml": frozenset({
        "Check out the upstream project (read-only)",
    }),
    "dispatcher.lock.yml": frozenset(),
}


def tree_version(lines: Iterable[str]) -> str | None:
    """The version of the pipeline code: the SHA-256 of the lines of the
    top-level git tree (``<mode> <type> <sha>\\t<path>``, as ``git ls-tree``
    prints them) without `.state`, so the state commit of every tick does not
    change it. ``None`` for an empty tree."""
    kept = [line for line in lines if line.partition("\t")[2] != ".state"]
    if not kept:
        return None
    return hashlib.sha256("\n".join(kept).encode()).hexdigest()


def classify(run: dict, jobs: list[dict]) -> dict | None:
    """``{"pipeline": True, "job", "step", "head_sha", "ended_at"}`` when ``run`` failed
    before its agent ran for a step of the pipeline, else ``None``:

    * the run concluded ``failure`` and its ``activation`` job failed in a
      step, or
    * its ``agent`` job failed in a step, ``AGENT_STEP`` did not run
      (skipped or absent), and the first failed step is not a contribution
      step of the workflow.

    ``None`` for a run that was cancelled or timed out, for a job that
    failed with no failed step (the runner was lost), for a failed agent
    step, and for a failed contribution step."""
    if not isinstance(run, dict) or run.get("status") != "completed" \
            or run.get("conclusion") != "failure":
        return None
    path = str(run.get("path") or "").rsplit("/", 1)[-1].partition("@")[0]
    contribution = CONTRIBUTION_STEPS.get(path)
    if contribution is None:
        # An unknown workflow: every known contribution step counts, so
        # nothing is called a pipeline failure that might be the Work's.
        contribution = frozenset().union(*CONTRIBUTION_STEPS.values())
    by_name = {j.get("name"): j for j in jobs if isinstance(j, dict)}
    for name in (ACTIVATION_JOB, AGENT_JOB):
        job = by_name.get(name)
        if not isinstance(job, dict) or job.get("conclusion") != "failure":
            continue
        steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
        failed = next((s for s in steps if s.get("conclusion") == "failure"), None)
        if failed is None:
            return None      # a lost runner: no step failed
        if name == AGENT_JOB:
            agent = next((s for s in steps if s.get("name") == AGENT_STEP), None)
            if agent is not None and agent.get("conclusion") not in ("skipped", None):
                return None  # the agent ran (or was cancelled)
            if failed.get("name") in contribution:
                return None
        return {"pipeline": True, "job": name, "step": str(failed.get("name")),
                "head_sha": run.get("head_sha"), "ended_at": run.get("updated_at")}
    return None
