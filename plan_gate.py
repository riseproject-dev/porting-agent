# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Trusted research-completeness gate, before durable planning Completion.

Reads bounded JSON from the exact Claim staging directory, never executes
agent files. The native queue still authenticates the Claim and verifies upload.
This gate checks documented research coverage, not the truth of every assertion.
A plan decides develop, park, or close, for a project in any language and on
its own build, test, and release tooling; ``project`` records which. PyPI is
the one registry the reconciler can confirm a close from:
``close_confirmation`` is the pure check it applies to PyPI documents it read
itself before it takes a decision close, and a close of a project that is not
released on PyPI is refused here already.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

import commit_rules
import schemas
from common import sha256_text, validate_scope_paths

RESEARCH_CATEGORIES = (
    "tracking_issue", "upstream_issues", "upstream_pull_requests", "ci_history",
    "documentation", "website", "mailing_lists", "blogs", "maintainer_reviews", "release_repositories", "ecosystem_report",
)
OPTIONALLY_ABSENT = {"website", "mailing_lists", "blogs", "release_repositories", "ecosystem_report"}
# The schema version of a plan the gate admits: it records the contribution
# guidelines with their series requirements, and the project (its languages,
# build systems, release channels, and riscv64 toolchain). Stored plans of
# versions 1 to 3 stay readable and record no series requirement; no new one
# is admitted.
PLAN_VERSION = 4
# Where the planner must have looked, as (what, words every one of the
# searched locations of that kind names). Each kind must be searched; "none
# found" is admitted only with that evidence.
TEMPLATE_LOCATIONS = (
    ("the repository root", lambda s: s.startswith("pull_request_template")),
    ("`.github/`", lambda s: s.startswith(".github/") and "pull_request_template" in s),
    ("`docs/`", lambda s: s.startswith("docs/") and "pull_request_template" in s),
)
CONTRIBUTING_LOCATIONS = (
    ("a root CONTRIBUTING file", lambda s: s.startswith("contributing")),
    ("`.github/CONTRIBUTING`", lambda s: s.startswith(".github/contributing")),
    ("`docs/` contributing pages", lambda s: s.startswith("docs/") and "contributing" in s),
    ("DEVELOPMENT.md", lambda s: "development" in s),
)


# The `terminal_reason` of a decision close the reconciler confirmed from PyPI:
# a riscv64 wheel of the latest release is published, or every distribution
# publishes pure-Python wheels only.
CLOSE_REASONS = ("already_released", "no_work")
# Bounds of the PyPI values a close finding records.
_MAX_VERSION = 40
_MAX_FILENAME = 200
_MAX_NAME = 100


class PlanError(ValueError):
    pass


def _searched(section: dict, locations, what: str) -> None:
    searched = [str(s).strip().removeprefix("./").casefold() for s in section["searched"]]
    for name, matches in locations:
        if not any(matches(s) for s in searched):
            raise PlanError(f"{what}: the search does not cover {name}")


def validate_guidelines(plan: dict) -> None:
    """The contribution guidelines a new plan must record: the pull
    request templates, the contributing instructions, the measured commit
    conventions, and the reviewers of the paths the contribution changes.
    Each may be "none found" only with the record of the search."""
    if plan.get("schema_version") != PLAN_VERSION or "contribution_guidelines" not in plan:
        raise PlanError(f"a new plan must be schema version {PLAN_VERSION} and record "
                           "the contribution guidelines and the project")
    found = plan["contribution_guidelines"]
    template = found["pull_request_template"]
    _searched(template, TEMPLATE_LOCATIONS, "pull_request_template")
    if (template["status"] == "found") != bool(template["templates"]):
        raise PlanError("pull_request_template: a found template is recorded, and only then")
    sources = {s["url"] for s in template["sources"]}
    for item in template["templates"]:
        if item["url"] not in sources:
            raise PlanError(f"pull_request_template: {item['path']} is not cited in sources")
    contributing = found["contributing"]
    _searched(contributing, CONTRIBUTING_LOCATIONS, "contributing")
    if (contributing["status"] == "found") != bool(contributing["documents"]):
        raise PlanError("contributing: a found document is recorded, and only then")
    if contributing["status"] == "found" and not contributing["instructions"]:
        raise PlanError("contributing: a found document needs its instructions")
    sources = {s["url"] for s in contributing["sources"]}
    for item in contributing["documents"]:
        if item["url"] not in sources:
            raise PlanError(f"contributing: {item['path']} is not cited in sources")
    conventions = found["commit_conventions"]
    try:
        commit_rules.normalize(conventions["rules"])
        commit_rules.sections_from_plan(plan)
    except commit_rules.RuleError as exc:
        raise PlanError(f"commit_conventions: {exc}") from None
    lengths = conventions["title_lengths"]
    if not lengths["median"] <= lengths["p90"] <= lengths["max"]:
        raise PlanError("commit_conventions: the title lengths are not ordered")
    reviewers = found["reviewers"]
    named = [r for entry in reviewers["paths"] for r in (*entry["codeowners"], *entry["reviewers"])]
    if (reviewers["status"] == "found") != bool(named):
        raise PlanError("reviewers: a reviewer or code owner is recorded, and only when found")
    if reviewers["status"] == "found" and not reviewers["expectations"]:
        raise PlanError("reviewers: found reviewers need their expectations")
    decision = plan["proposal"]
    if decision["decision"] == "develop":
        covered = {entry["path"] for entry in reviewers["paths"]}
        missing = [p for p in decision["allowed_paths"] if p not in covered]
        if missing:
            raise PlanError(f"reviewers: no reviewer research for the paths {missing[:8]}")
    validate_series_requirements(plan)


def validate_series_requirements(plan: dict) -> None:
    """The series requirements a new plan records: the measurable items of
    the contributing instructions, the template checklist, and the reviewer
    expectations, each a path the non-debug commits of the series must
    change. The list is required (it may be empty), the ids are unique, each
    cites a source the guidelines record, and each path is literally one of
    the approved ``allowed_paths``, so a requirement can always be met in
    scope. A park or close records none."""
    found = plan["contribution_guidelines"]
    if "series_requirements" not in found:
        raise PlanError("series_requirements: a new plan records them, possibly none")
    try:
        requirements = commit_rules.series_requirements_from_plan(plan)
    except commit_rules.RuleError as exc:
        raise PlanError(f"series_requirements: {exc}") from None
    decision = plan["proposal"]
    if decision["decision"] != "develop":
        if requirements:
            raise PlanError("series_requirements: a park or close records none")
        return
    cited = {s["url"] for key in ("contributing", "pull_request_template", "reviewers")
             for s in found[key]["sources"]}
    allowed = set(decision["allowed_paths"])
    for raw, item in zip(found["series_requirements"], requirements):
        if raw["source_url"] not in cited:
            raise PlanError(f"series_requirements: {item['id']} cites a source the contributing, "
                            "template, or reviewer research does not record")
        outside = [p for p in item["paths"] if p not in allowed]
        if outside:
            raise PlanError(f"series_requirements: {item['id']} names paths that are not "
                            f"allowed_paths: {outside}")


def validate_project(plan: dict, task: dict) -> None:
    """The project a new plan records, and the close it may decide: a close
    is confirmed from PyPI only, so it needs a PyPI release channel, no
    release channel that lacks riscv64, and recorded distributions to read.
    Any other project that already supports riscv64 is parked with the
    evidence."""
    project = plan.get("project")
    if not isinstance(project, dict):
        raise PlanError("a new plan must record the project")
    if plan["proposal"]["decision"] != "close":
        return
    channels = project["release_channels"]
    if not any(c["kind"] == "pypi" for c in channels) \
            or any(c["riscv64"] == "absent" for c in channels) \
            or not [n for n in (task.get("distributions") or []) if isinstance(n, str)]:
        raise PlanError("a decision close is confirmed from PyPI only: use park for a project "
                        "that is not released on PyPI, that lacks riscv64 on a release "
                        "channel, or whose request names no distribution")


def validate(plan: dict, proposal: dict, task: dict) -> None:
    schemas.validate("plan", plan)
    schemas.validate("proposal", proposal)
    for obj in (plan, proposal):
        if obj["contribution_id"] != task["contribution_id"] or obj["expected_revision"] != task["revision"]:
            raise PlanError("plan/proposal does not answer this immutable task")
    if proposal["outcome"] != "planning" or proposal["operation"] != "none":
        raise PlanError("planner must deliver a planning run, not a patch")
    if proposal.get("expected_head") != task.get("expected_head"):
        raise PlanError("proposal head does not match task")
    issue = (task.get("context") or {}).get("tracking_issue") or {}
    if not issue.get("url") or not isinstance(issue.get("description"), str):
        raise PlanError("task has no frozen tracking-issue description and URL")
    digest = sha256_text(issue["description"])
    if digest != issue.get("description_digest") or plan["tracking_issue_description_digest"] != digest:
        raise PlanError("tracking issue description digest does not match immutable task")
    for category in RESEARCH_CATEGORIES:
        entry = plan["research"][category]
        if entry["status"] == "not_applicable" and category not in OPTIONALLY_ABSENT:
            raise PlanError(f"{category}: research cannot be skipped")
    report = plan["ecosystem_report"]
    research = plan["research"]["ecosystem_report"]
    if report["status"] == "available":
        if research["status"] != "searched" or report["url"] not in {s["url"] for s in research["sources"]}:
            raise PlanError("available ecosystem report must be researched and cited")
    elif research["status"] != "not_applicable":
        raise PlanError("absent ecosystem report needs documented discovery and justification")
    if issue["url"] not in {s["url"] for s in plan["research"]["tracking_issue"]["sources"]}:
        raise PlanError("tracking-issue research must cite the original issue URL")
    decision = plan["proposal"]
    coverage = decision["path_coverage"]
    areas = [entry["area"] for entry in coverage]
    required_areas = {"build", "test", "release", "packaging", "documentation", "native_sources"}
    if set(areas) != required_areas or len(areas) != len(set(areas)):
        raise PlanError("path_coverage must assess each required area exactly once")
    covered_paths = [path for entry in coverage for path in entry["paths"]]
    if covered_paths:
        try:
            validate_scope_paths(covered_paths)
        except ValueError as exc:
            raise PlanError(f"path_coverage: {exc}") from None
    if decision["decision"] == "develop":
        try:
            validate_scope_paths(decision["allowed_paths"])
        except ValueError as exc:
            raise PlanError(str(exc)) from None
        missing = [path for path in covered_paths if not any(
            fnmatch.fnmatchcase(path, allowed) for allowed in decision["allowed_paths"])]
        if missing:
            raise PlanError(f"allowed_paths omit researched riscv64 paths: {sorted(set(missing))}")
    validate_guidelines(plan)
    validate_project(plan, task)


def scope_problem(decision: dict, *, admission_class: str | None,
                  allowed_paths: list[str] | None, coverage: bool = True) -> str | None:
    """Why the patch scope admitted for a plan is incomplete, or ``None``.

    The scope is complete when it has an admission class, names 1..32 safe
    scope paths, and, with ``coverage``, matches every path the plan's
    ``path_coverage`` lists as possibly needing a riscv64 change. The
    reconciler asserts the fact "the admitted scope names an admission
    class and 1 to 32 safe `scope_paths`, and a plan's own scope matches
    every path of its `path_coverage`" (``lifecycle.PATCH_SCOPE_COMPLETE``)
    of ``admit_development`` and ``readmit_development`` from this: with
    ``coverage`` for a plan's own scope, without it for an admission
    override, whose coverage is recorded but does not refuse it."""
    if not isinstance(admission_class, str) or not admission_class:
        return "no admission class"
    if not isinstance(allowed_paths, list) or not all(isinstance(p, str) for p in allowed_paths):
        return "no scope paths"
    try:
        validate_scope_paths(allowed_paths)
    except ValueError as exc:
        return str(exc)
    if not coverage:
        return None
    areas = decision.get("path_coverage") if isinstance(decision, dict) else None
    if not isinstance(areas, list) or not all(
            isinstance(entry, dict) and isinstance(entry.get("paths"), list) for entry in areas):
        return "no path coverage"
    covered = [path for entry in areas for path in entry["paths"]]
    missing = sorted({path for path in covered if not isinstance(path, str) or not any(
        fnmatch.fnmatchcase(path, allowed) for allowed in allowed_paths)}, key=str)
    if missing:
        return f"the scope omits researched riscv64 paths: {missing[:8]}"
    return None


def _served(name: str, document) -> tuple[str | None, str | None, dict]:
    """How PyPI serves riscv64 for the latest release of ``name``: as
    (``"riscv64"`` or ``"pure"`` or ``None``, the problem when it is not
    served, the finding). Only values that parsed as a version or a wheel
    file name are recorded."""
    shown = str(name)[:_MAX_NAME]
    finding = {"version": None, "served_by": None, "wheel": None}
    info = document.get("info") if isinstance(document, dict) else None
    files = document.get("urls") if isinstance(document, dict) else None
    if document is None:
        return None, f"PyPI has no distribution {shown}", finding
    if not isinstance(info, dict) or not isinstance(info.get("version"), str) \
            or not isinstance(files, list):
        return None, f"the PyPI document of {shown} names no latest release", finding
    try:
        latest = Version(info["version"])
    except InvalidVersion:
        return None, f"the PyPI document of {shown} names no valid latest version", finding
    version = str(latest)[:_MAX_VERSION]
    finding["version"] = version
    wheels: list[tuple[str, frozenset]] = []
    for entry in files:
        if not isinstance(entry, dict) or entry.get("yanked") is True:
            continue
        filename = entry.get("filename")
        if not isinstance(filename, str) or not filename.endswith(".whl"):
            continue
        try:
            _name, wheel_version, _build, tags = parse_wheel_filename(filename)
        except (InvalidWheelFilename, InvalidVersion):
            continue
        if wheel_version != latest:
            continue
        wheels.append((filename[:_MAX_FILENAME], tags))
    if not wheels:
        return None, f"{shown} {version} publishes no wheel", finding
    for filename, tags in wheels:
        if any(tag.platform.endswith("riscv64") for tag in tags):
            finding.update(served_by="riscv64", wheel=filename)
            return "riscv64", None, finding
    if all(tag.platform == "any" for _filename, tags in wheels for tag in tags):
        finding.update(served_by="pure", wheel=wheels[0][0])
        return "pure", None, finding
    return None, f"{shown} {version} has no riscv64 wheel and is not pure Python", finding


_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})


def _repository(url) -> tuple[str, str] | None:
    """The GitHub repository a URL names, as casefolded (owner, name)."""
    if not isinstance(url, str) or len(url) > 2000:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "") not in _GITHUB_HOSTS:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return None
    return segments[0].casefold(), segments[1].casefold().removesuffix(".git")


def _names_repository(info, upstream) -> bool:
    """Whether PyPI project metadata names the GitHub repository ``upstream``
    (``owner/name``) in its project URLs, home page, or download URL. Pure,
    never raises."""
    if not isinstance(info, dict) or not isinstance(upstream, str) or upstream.count("/") != 1:
        return False
    owner, _, name = upstream.casefold().partition("/")
    wanted = (owner, name.removesuffix(".git"))
    urls = info.get("project_urls")
    candidates = list(urls.values()) if isinstance(urls, dict) else []
    candidates += [info.get("home_page"), info.get("download_url")]
    return any(_repository(url) == wanted for url in candidates[:200])


def close_confirmation(distributions: list[str], documents: dict[str, dict | None], *,
                       upstream: str | None) -> tuple[str | None, str | None, dict]:
    """Whether PyPI confirms a decision close, from the PyPI documents of the
    record's ``distributions`` (``None`` for a project PyPI does not have)
    and the record's ``upstream`` repository (``owner/name``).

    Returns ``(terminal_reason, problem, finding)``; exactly one of the
    first two is set. The close is confirmed when each PyPI project names
    the upstream repository in its metadata (the distributions of a request
    may name packages of any registry, and a PyPI project of the same name
    from another repository confirms nothing), and the latest release of
    every distribution publishes a non-yanked riscv64 wheel, or pure-Python
    (platform ``any``) wheels only. ``terminal_reason`` is
    ``already_released`` when at least one distribution has a riscv64 wheel,
    otherwise ``no_work``. ``finding`` maps each distribution to the version,
    how it is served, and one matching wheel. Pure: no I/O, never raises."""
    names = [n for n in (distributions or []) if isinstance(n, str)]
    if not names:
        return None, "no distribution is recorded", {}
    finding: dict[str, dict] = {}
    served = []
    for name in names:
        document = (documents or {}).get(name)
        how, problem, entry = _served(name, document)
        finding[name[:_MAX_NAME]] = entry
        if problem:
            return None, problem, finding
        if not _names_repository(document.get("info"), upstream):
            return None, (f"the PyPI project {str(name)[:_MAX_NAME]} does not name the "
                          "upstream repository"), finding
        served.append(how)
    return ("already_released" if "riscv64" in served else "no_work"), None, finding


def _read(root: Path, relative: Path, limit: int = 256 * 1024) -> str:
    if root.is_symlink() or relative.is_absolute() or ".." in relative.parts:
        raise PlanError("invalid or symlinked staging path")
    path = root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise PlanError("symlink in planning staging")
    if not path.is_file() or path.stat().st_size > limit:
        raise PlanError(f"missing or oversized research input: {relative}")
    data = path.read_bytes()
    if len(data) > limit:
        raise PlanError("research input grew beyond its bound")
    return data.decode("utf-8")


def check_stage(assignment: dict, staging: Path, finish_root: Path) -> str:
    claims = assignment.get("claims") or []
    if assignment.get("version") != 3 or len(claims) != 1:
        raise PlanError("expected a singleton version-3 planner assignment")
    member = claims[0]
    task = member["work"]
    if task.get("task") != "planning":
        raise PlanError("research gate only accepts planner tasks")
    # A cancellation needs no plan, but cannot smuggle a completed intent.
    # Native processing downstream still authenticates and applies these intents.
    intents = [json.loads(line) for line in _read(finish_root, Path("work-queue.finish.jsonl"), 1024 * 1024).splitlines()
               if line.strip()]
    if intents and all(i.get("version") == 3 and i.get("kind") == "finish"
                       and i.get("claim_handle", member["handle"]) == member["handle"]
                       and (i.get("parameters") or {}).get("outcome") == "cancelled" for i in intents):
        return "cancelled: no completed planning admitted"
    identity = {"dispatch_id": assignment["dispatch_id"], "claim_id": member["claim_id"],
                "work_id": member["work_id"], "claim_handle": member["handle"]}
    # gh-aw's native codec is compact, recursively key-sorted JSON. The broader
    # pipeline canonical_json format is intentionally pretty-printed and is not
    # the Claim artifact namespace codec.
    native = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    namespace = hashlib.sha256(native.encode()).hexdigest()
    base = Path("claims") / namespace / "porting-out"
    plan = json.loads(_read(staging, base / "plan.json"))
    proposal = json.loads(_read(staging, base / "proposal.json"))
    validate(plan, proposal, task)
    return "research complete: eligible for native Completion and verified artifact delivery"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--finish-root", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--task", type=Path)
    parser.add_argument("--assignment-env", default="ASSIGNMENT")
    args = parser.parse_args()
    files = (args.plan, args.proposal, args.task)
    if any(files):
        if not all(files) or args.staging or args.finish_root:
            parser.error("--plan, --proposal and --task must be supplied together")
        validate(*(json.loads(path.read_text()) for path in files))
        print("plan, proposal and task satisfy the research contract")
    else:
        if not args.staging or not args.finish_root:
            parser.error("--staging and --finish-root are required for trusted staging validation")
        raw = os.environ[args.assignment_env]
        if len(raw.encode()) > 49152:
            raise PlanError("assignment exceeds native bound")
        print(check_stage(json.loads(raw), args.staging, args.finish_root))
