# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Frozen copies of the live ``luhenry--d2vm/3`` record and evidence from the
incident of the over-budget Work payload (``tests/fixtures/live-d2vm-payload``).

``record-900e2b9.json`` is the record when its repair Work was requested
(`luhenry--d2vm/3@g1.r10:development_repair`, which no queue accepted),
``record-df439c0.json`` the record once the dispatch recovery waited for a
human (`dispatch_failed`); the evidence is the plan it was repaired under,
the relayed review, the history of the Work and the steering, as committed;
``events/`` holds events 37 to 41."""

from __future__ import annotations

import copy
import json
from pathlib import Path

DIRECTORY = Path(__file__).parent / "fixtures" / "live-d2vm-payload"
CID = "luhenry--d2vm/3"
WORK = "luhenry--d2vm/3@g1.r10:development_repair"
PLAN = "plan-luhenry--d2vm--3-r9"
REVIEW = "review-1469259163-1-4680121101"
HISTORY = "history-fc92db593b9e5ae1e40d1c270060207d"


def record(name: str = "900e2b9") -> dict:
    return json.loads((DIRECTORY / f"record-{name}.json").read_text())


def evidence() -> dict[str, dict]:
    found = {}
    for path in sorted((DIRECTORY / "evidence").glob("*.json")):
        item = json.loads(path.read_text())
        found[item["evidence_id"]] = item
    return found


def events() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((DIRECTORY / "events").glob("*.json"))]


def fresh() -> tuple[dict, dict]:
    return copy.deepcopy(record()), copy.deepcopy(evidence())
