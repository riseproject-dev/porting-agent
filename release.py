# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Official release coverage against a frozen target contract (plan 11.2, 18.2).

This is the deterministic metadata half of release acceptance: which
official, non-yanked files cover which frozen matrix rows. It parses tags
with ``packaging`` (compressed tags, abi3, free-threaded ``t`` ABIs, libc
family/floor). Installing and running the wheels in clean target
environments is the separate probe step; ``released`` needs both.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable

from packaging.tags import Tag
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import Version

_MANY = re.compile(r"^manylinux_(\d+)_(\d+)_riscv64$")
_MUSL = re.compile(r"^musllinux_(\d+)_(\d+)_riscv64$")


@dataclass(frozen=True)
class Row:
    """One frozen target row, e.g. cp312/cp312/manylinux floor 2.39."""
    interpreter: str          # cp312, cp313, pp311 ... (free-threaded is cp313 + abi cp313t)
    abi: str                  # cp312, cp313t, abi3, none
    libc: str                 # manylinux | musllinux
    floor: tuple[int, int]    # maximum platform floor the payload may require

    @property
    def key(self) -> str:
        return f"{self.interpreter}-{self.abi}-{self.libc}_{self.floor[0]}_{self.floor[1]}"


@dataclass
class Coverage:
    version: str
    covered: dict[str, str] = field(default_factory=dict)     # row key -> filename
    missing: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing and not self.problems


def _platform(tag: Tag) -> tuple[str, tuple[int, int]] | None:
    m = _MANY.match(tag.platform)
    if m:
        return "manylinux", (int(m.group(1)), int(m.group(2)))
    m = _MUSL.match(tag.platform)
    if m:
        return "musllinux", (int(m.group(1)), int(m.group(2)))
    if tag.platform == "any":
        return "any", (0, 0)
    return None


def _abi3_ok(tag: Tag, row: Row) -> bool:
    """An abi3 wheel built for cp3X serves every CPython >= 3.X (non-free-threaded)."""
    if tag.abi != "abi3" or not tag.interpreter.startswith("cp") or row.abi.endswith("t"):
        return False
    if not row.interpreter.startswith("cp"):
        return False
    return int(tag.interpreter[3:]) <= int(row.interpreter[3:])


def row_satisfied(tags: Iterable[Tag], row: Row, *, allow_universal: bool) -> bool:
    for t in tags:
        plat = _platform(t)
        if plat is None:
            continue
        family, floor = plat
        if family == "any":
            if allow_universal and t.abi == "none":
                return True
            continue
        if family != row.libc or floor > row.floor:
            continue
        if t.interpreter == row.interpreter and t.abi == row.abi:
            return True
        if row.abi == "abi3" and _abi3_ok(t, row):
            return True
        if row.abi == row.interpreter and _abi3_ok(t, row):
            return True
    return False


def coverage(pypi_release_files: list[dict], version: str, rows: list[Row], *,
             allow_universal: bool = False) -> Coverage:
    """``pypi_release_files``: entries of the PyPI JSON API ``urls`` list for
    one version (filename, yanked, digests, upload_time_iso_8601).

    ``allow_universal`` must only be True for a genuinely architecture
    independent contract: a universal wheel never closes an acceleration gap.
    """
    cov = Coverage(version=version)
    want = Version(version)
    files = []
    for f in pypi_release_files:
        name = f.get("filename", "")
        if not name.endswith(".whl"):
            continue
        if f.get("yanked"):
            cov.problems.append(f"{name}: yanked")
            continue
        if not (f.get("digests") or {}).get("sha256"):
            cov.problems.append(f"{name}: no sha256 digest in official metadata")
            continue
        try:
            _n, v, _b, tags = parse_wheel_filename(name)
        except InvalidWheelFilename as e:
            cov.problems.append(f"{name}: {e}")
            continue
        if v != want:
            continue
        files.append((name, tags))
    for row in rows:
        hit = next((n for n, tags in files if row_satisfied(tags, row, allow_universal=allow_universal)), None)
        if hit:
            cov.covered[row.key] = hit
        else:
            cov.missing.append(row.key)
    # Yanked files of *other* rows are only a problem if they were the sole cover.
    cov.problems = [p for p in cov.problems if not p.endswith("yanked")] + [
        p for p in cov.problems if p.endswith("yanked") and cov.missing]
    return cov


def select_release(releases: dict[str, list[dict]], *, minimum: str | None) -> list[str]:
    """Candidate official versions (non-yanked, non-prerelease) newer than or
    equal to ``minimum`` (the first version expected to contain the merge),
    oldest first. Provenance (merge ancestry or maintainer confirmation) is
    checked separately: a version number is never assumed to contain a fix."""
    out = []
    for v, files in releases.items():
        try:
            ver = Version(v)
        except Exception:
            continue
        if ver.is_prerelease or ver.is_devrelease:
            continue
        if files and all(f.get("yanked") for f in files):
            continue
        if minimum and ver < Version(minimum):
            continue
        out.append(v)
    return sorted(out, key=Version)


# --------------------------------------------------------------------------
# Release observation in `release_wait.idle`
#
# The acceptance contract is the frozen target contract the record names
# (`target_contract_id`): its distributions and the matrix rows their
# official release must cover. The observation reads each distribution's
# official PyPI metadata and decides one row of the table:
#
# * `require_release_intervention` when no contract with rows and
#   distributions is recorded, or PyPI has no such distribution (operator
#   action);
# * `verify_release` when, for every distribution, the oldest official
#   release whose tag contains the merge commit covers every row;
# * `record_release_incomplete` otherwise.

# The pending actions of `require_release_intervention`.
CONTRACT_MISSING = "release_contract_missing"
DISTRIBUTION_MISSING = "release_distribution_missing"


@dataclass(frozen=True)
class Contract:
    distributions: tuple[str, ...]
    rows: tuple[Row, ...]
    allow_universal: bool


def acceptance_contract(contract: dict | None) -> Contract | None:
    """The release acceptance contract of a target contract evidence record,
    or ``None`` when it names no distribution or no valid matrix row."""
    data = (contract or {}).get("data") or {}
    names = data.get("distributions") or ([data["distribution"]] if data.get("distribution") else [])
    rows = []
    for raw in data.get("release_rows") or []:
        try:
            floor = tuple(int(x) for x in raw["floor"])
            if len(floor) != 2 or raw["libc"] not in ("manylinux", "musllinux"):
                return None
            rows.append(Row(str(raw["interpreter"]), str(raw["abi"]), raw["libc"],
                            (floor[0], floor[1])))
        except (KeyError, TypeError, ValueError):
            return None
    if not names or not rows or not all(isinstance(n, str) and n for n in names):
        return None
    return Contract(tuple(names), tuple(rows), bool(data.get("allow_universal")))


@dataclass
class Decision:
    verb: str
    facts: frozenset
    values: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    # The observation's summary, recorded once by `record_release_incomplete`.
    key: str | None = None


def _uploaded_after(files: list[dict], merged_at: str) -> bool:
    """Whether every file of a release was uploaded at or after the merge: a
    release uploaded before it cannot contain it."""
    times = [f.get("upload_time_iso_8601") for f in files]
    return bool(times) and all(isinstance(t, str) and t.replace("Z", "+00:00") >= merged_at
                               for t in times)


def decide(contract: Contract | None, *, merge: dict, pypi: dict[str, dict | None],
           contains) -> Decision:
    """The row of `release_wait.idle` for the observed release evidence.

    ``pypi`` maps each distribution of the contract to its PyPI JSON
    document, or ``None`` when PyPI has no such distribution. ``contains(version)``
    says whether the upstream release tag of ``version`` contains the merge
    commit (``None`` when no such tag exists)."""
    if contract is None:
        return Decision("require_release_intervention", frozenset({"release_needs_operator"}),
                        {"pending_human_action": CONTRACT_MISSING},
                        {"why": "no release acceptance contract (distributions and matrix rows) "
                                "is recorded for this contribution"})
    missing = [name for name in contract.distributions if pypi.get(name) is None]
    if missing:
        return Decision("require_release_intervention", frozenset({"release_needs_operator"}),
                        {"pending_human_action": DISTRIBUTION_MISSING},
                        {"why": f"PyPI has no distribution {', '.join(missing)}"})
    merged_at = merge["merged_at"].replace("Z", "+00:00")
    found: dict[str, dict] = {}
    incomplete: dict[str, str] = {}
    for name in contract.distributions:
        releases = (pypi[name] or {}).get("releases") or {}
        candidates = [v for v in select_release(releases, minimum=None)
                      if _uploaded_after(releases.get(v) or [], merged_at)]
        version = next((v for v in candidates if contains(v)), None)
        if version is None:
            incomplete[name] = ("no official release after the merge whose tag contains the "
                                "merge commit")
            continue
        cov = coverage(releases[version], version, list(contract.rows),
                       allow_universal=contract.allow_universal)
        found[name] = {"version": version, "covered": cov.covered, "missing": cov.missing,
                       "problems": cov.problems[:16]}
        if not cov.complete:
            incomplete[name] = (f"{version} does not cover {', '.join(cov.missing) or 'every row'}"
                                + (f" ({'; '.join(cov.problems[:4])})" if cov.problems else ""))
    if not incomplete:
        return Decision("verify_release", frozenset({"release_verified"}),
                        details={"releases": {n: found[n]["version"] for n in found},
                                 "coverage": found})
    summary = "; ".join(f"{name}: {why}" for name, why in sorted(incomplete.items()))
    key = hashlib.sha256(summary.encode()).hexdigest()[:16]
    return Decision("record_release_incomplete", frozenset({"release_incomplete"}),
                    details={"incomplete": summary[:2000], "observation_key": key}, key=key)
