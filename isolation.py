# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Isolation of one contribution's processing within a controller tick.

Every step of a tick that works on one contribution runs inside
``contribution(contribution_id, step)``. An exception that leaves the block
is attributed to that contribution and classified:

* ``CollectorFailure``: the collector could not read GitHub for it
  (``COLLECTOR_ERRORS``). The contribution is skipped for the tick, with no
  state change and no pause.
* ``ProcessingError``: any other exception. It is unexpected: the expected
  rejections (a refused transition of a command, a rejected command, an
  intake error) are handled where they occur and never leave the block.
  Everything staged for the contribution is discarded and the controller
  pauses it with ``pause_on_processing_error``.

An exception outside any block is not attributed to a contribution and stops
the whole tick. ``reconcile.tick`` owns the policy; this module only
attributes, classifies, and renders the error text.
"""

from __future__ import annotations

import contextlib
import http.client
import re
from typing import Any

import collect
import state

# What a failed GitHub read raises. ``collect.Api`` converts every transport
# and decode failure of a read into ``CollectError``; the HTTP layer's and the
# transport's own errors are kept as a safety net (``OSError`` covers
# ``URLError``, TLS errors, socket timeouts, and reset connections). A
# ``KeyError``, ``TypeError``, or ``ValueError`` on a response that was read
# is a processing error, not a collector failure.
COLLECTOR_ERRORS = (collect.CollectError, http.client.HTTPException, OSError)

# Bounds of the error text that is recorded in state and posted on the
# tracking issue.
MAX_MESSAGE = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class ContributionFailure(Exception):
    """An exception attributed to the processing of one contribution."""

    def __init__(self, contribution_id: str, step: str, error: BaseException):
        super().__init__(f"{contribution_id}: {step}: {type(error).__name__}: {error}")
        self.contribution_id = contribution_id
        self.step = step
        self.error = error


class CollectorFailure(ContributionFailure):
    """The collector could not read GitHub for one contribution."""


class ProcessingError(ContributionFailure):
    """An unexpected exception while one contribution was processed."""


class TickFailure(RuntimeError):
    """A pipeline failure: the whole tick stops and nothing is committed.
    ``failures`` are the contribution failures that led to it, if any."""

    def __init__(self, message: str, failures: tuple[ContributionFailure, ...] = ()):
        super().__init__(message)
        self.failures = failures


@contextlib.contextmanager
def contribution(contribution_id: str, step: str):
    """Attribute every exception of the block to ``contribution_id``."""
    try:
        yield
    except ContributionFailure:
        raise
    except COLLECTOR_ERRORS as error:
        raise CollectorFailure(contribution_id, step, error) from error
    except Exception as error:
        raise ProcessingError(contribution_id, step, error) from error


def clean_text(text: Any, limit: int = MAX_MESSAGE) -> str:
    """Untrusted text as one bounded line that is safe to store and to post.

    Exception messages can quote API, human, or model text. The repository
    text normalization of the state store is applied, everything that matches
    a credential pattern is removed, control characters and runs of
    whitespace become one space, and the result is cut to ``limit``.
    """
    value = state.normalize_state_text(str(text))
    for pattern in state.SECRET_PATTERNS:
        value = pattern.sub("[redacted]", value)
    value = " ".join(_CONTROL.sub(" ", value).split())
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def describe(failure: ContributionFailure, run_id: str | None) -> dict[str, Any]:
    """The bounded description of a failure: no traceback, no secret."""
    return {
        "contribution": failure.contribution_id,
        "step": failure.step,
        "error_class": clean_text(type(failure.error).__name__, 80),
        "message": clean_text(failure.error),
        "run_id": clean_text(run_id, 40) if run_id else None,
    }


def pause_reason(description: dict[str, Any]) -> str:
    """``paused_reason`` of ``pause_on_processing_error``: the error class,
    the message, and the run id."""
    run = description["run_id"] or "unknown"
    return (f"processing_error: {description['error_class']}: {description['message']} "
            f"(step {description['step']}, run {run})")
