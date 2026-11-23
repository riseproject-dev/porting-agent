# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The trusted checks of the submission rewrite and the submission review.

At `/upstreaming submit-upstream` two uncredentialed Work tasks run one after
the other, each in a fresh context:

* ``submission_rewrite`` (``handoff.agent_running``) reshapes the non-debug
  commits of the approved `riscv64-dev` series after the project's commit
  conventions and writes the upstream pull request title and description
  after its pull request template and contributing instructions. Its Result
  (``proposal.json``, ``schemas/submission_rewrite.schema.json``, and
  ``patches.mbox``, ``git format-patch`` on the frozen base) is untrusted.
* ``submission_review`` (``handoff.reviewing``) reviews exactly that series
  and text, which the trusted pre-agent step downloads from the rewrite's
  publication artifact and checks against their digests, against the pull
  request template, the contributing instructions, the commit conventions,
  and the reviewers' expectations. Its Result
  (``schemas/submission_review.schema.json``) is pass or fail with reasons;
  it carries no series and can change nothing.

Trusted code decides, without a token, in a fresh repository
(``check_rewrite``), before the review:

* the rewritten series rebuilds on the frozen base, under the App identity,
  to a deterministic head; it and every one of its commits pass the
  publisher's validation of an upstream-facing rewrite (scope, no debug
  commit, no merge, no forbidden workflow content, no symlink, gitlink, or
  new executable mode); each commit touches only paths the approved series
  changes, and every reviewed tree holds only content of the base or of the
  approved series (``intermediate_problems``);
* its tree is exactly the tree of the approved series without its
  ``{debug}`` commits: the rewrite changes no code;
* every attribution trailer names the App's commit identity or repeats one
  of the approved commits;
* its commit messages follow the dossier's measurable commit rules, and the
  description has every required section of the pull request template.

A rewrite that fails a check, or reports that the series cannot be shaped
without a code change, returns to development with the reasons as feedback
(``reject_submission_rewrite``). The review (``check_review``) passes only
for exactly the rewritten series and text, with every expectation it lists
met; a failed review returns to development with its reasons
(``reject_submission_review``). A Result that is not usable (malformed, for
another Work, series, or text, or without its series) is a Work without a
usable Result: ``allocate_work_retry`` within its budget, never a verdict.
The trusted handoff workflow then builds `riscv64` from the same series,
checks it again, and pushes exactly the head this check computed.

    python -m submission check-rewrite --task T --rewrite R --mbox M --out O
    python -m submission check-review --task T --review R --out O
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import commit_rules
import fork_publish
import publisher
import schemas

REWRITE = "submission_rewrite"
REVIEW = "submission_review"
STATUS = REVIEW
PASS, FAIL = "pass", "fail"
MAX_TITLE = 256
# The description is posted in full in the package comment, which holds at
# most this much of it.
MAX_BODY = 12000
MAX_REASONS = 30
# The most commits a reviewed series may have, as for any upstream-facing
# series the publisher validates.
MAX_COMMITS = publisher.MAX_COMMITS
_SHA = re.compile(r"^[0-9a-f]{40}$")


class CheckError(RuntimeError):
    """The check could not run (a fetch failed): an infrastructure failure,
    not a verdict on the review."""


def _git(work: Path, *args: str, check: bool = True):
    return fork_publish._git(work, *args, check=check)


def _out(work: Path, *args: str) -> str:
    return _git(work, *args).stdout.decode().strip()


def text_digest(title: str, body: str) -> str:
    """The digest of the pull request text a review reviews."""
    return hashlib.sha256((str(title) + "\n\n" + str(body)).encode()).hexdigest()


def unusable(reason: str) -> dict:
    """The outcome of a Result that is not a usable rewrite or review: the
    Work ended without a usable Result."""
    return {"status": "rejected", "failure_class": "validation", "stage": "submission_check",
            "reason": str(reason)[:2000]}


def rules_for(task: dict, review: dict) -> tuple[dict | None, list[str] | None, list[str]]:
    """The measurable rules and template sections the check applies, and
    notes about where they come from.

    The dossier's, from the trusted Work payload, when it records them. A
    dossier of schema version 1 records none; then the rules and sections the
    rewrite reports having found in the project are applied: they only add
    checks, so an untrusted value can refuse a submission but never admit
    one."""
    context = task.get("context") or {}
    notes = []
    rules, sections = context.get("commit_rules"), context.get("template_sections")
    if rules is None and review.get("commit_rules") is not None:
        rules = review["commit_rules"]
        notes.append("the commit rules the rewrite found (the dossier records none)")
    if sections is None and review.get("template_sections") is not None:
        sections = review["template_sections"]
        notes.append("the template sections the rewrite found (the dossier records none)")
    return (commit_rules.normalize(rules) if rules is not None else None,
            commit_rules.normalize_sections(sections) if sections is not None else None, notes)


def reconstruct(work: Path, *, fork: str, upstream: str, base: str, approved: str, mbox: bytes,
                scope_paths: list[str], read_token: str | None = None) -> dict:
    """Rebuild the reviewed series and the approved series without its debug
    commits in a fresh repository. Returns the reviewed head, its commits
    (message and tree), the tree of the approved series without its debug
    commits, the tree of the approved head, and the publisher's problems.
    Raises ``CheckError`` when a commit cannot be fetched and
    ``fork_publish.PublishRejected`` when the series does not apply."""
    import handoff

    if len(commit_rules.mbox_starts(mbox)) > MAX_COMMITS:
        raise fork_publish.PublishRejected(f"the series has more than {MAX_COMMITS} commits")
    try:
        head = fork_publish.rebuild(work, fork=fork, upstream=upstream, expected_head=approved,
                                    base_sha=base, mbox=mbox, operation="rewrite",
                                    read_token=read_token)
    except fork_publish.PublishInfraError as error:
        raise CheckError(str(error)) from None
    reviewed = _revs(work, base, head)
    if len(reviewed) > MAX_COMMITS:
        # Refused before any commit is validated, so a long series costs no
        # per-commit work.
        raise fork_publish.PublishRejected(
            f"the series has {len(reviewed)} commits, more than {MAX_COMMITS}")
    try:
        clean, _removed = handoff.rebuild_without_debug(work, base=base, head=approved)
    except handoff.HandoffInvalid as error:
        raise fork_publish.PublishRejected(f"the approved series: {error}") from None
    commits = [{"sha": sha, "message": _message(work, sha),
                "tree": _out(work, "rev-parse", f"{sha}^{{tree}}")}
               for sha in reviewed]
    approved_messages = [_message(work, sha) for sha in _revs(work, base, clean)]
    scope = publisher.Scope(allowed_globs=list(scope_paths), upstream_facing=True,
                            history="rewrite")
    report = publisher.validate_update(work, base=base, expected_head=approved, candidate=head,
                                       scope=scope)
    problems = list(report.problems) + intermediate_problems(
        work, base=base, approved=approved, clean=clean,
        reviewed=[c["sha"] for c in commits], scope=scope)
    return {"head": head, "commits": commits,
            "tree": _out(work, "rev-parse", f"{head}^{{tree}}"),
            "clean_tree": _out(work, "rev-parse", f"{clean}^{{tree}}"),
            "candidate_tree": _out(work, "rev-parse", f"{approved}^{{tree}}"),
            "approved_messages": approved_messages,
            "problems": problems}


def _revs(work: Path, base: str, head: str) -> list[str]:
    return [s for s in _out(work, "rev-list", "--reverse", f"{base}..{head}").split() if s]


def _message(work: Path, sha: str) -> str:
    return _git(work, "show", "-s", "--format=%B", sha).stdout.decode("utf-8", "replace") \
        .rstrip("\n")


def _changes(work: Path, old: str, new: str) -> list[tuple[str, str | None, str | None]]:
    """The paths ``new`` changes relative to ``old``, each with its mode and
    blob in ``new`` (``None`` for a deleted path)."""
    fields = _git(work, "diff-tree", "-r", "-z", "--no-renames", old, new).stdout \
        .decode("utf-8", "surrogateescape").split("\0")
    out = []
    for i in range(0, len(fields) - 1, 2):
        _old_mode, new_mode, _old_sha, new_sha, status = fields[i].lstrip(":").split()
        out.append((fields[i + 1], None, None) if status == "D" else
                   (fields[i + 1], new_mode, new_sha))
    return out


def intermediate_problems(work: Path, *, base: str, approved: str, clean: str,
                          reviewed: list[str], scope: publisher.Scope) -> list[str]:
    """Why a commit of the reviewed series, not only its head, may not be
    published upstream. Each commit passes the publisher's validation of an
    upstream-facing rewrite against the base (scope, forbidden workflow
    content, symlinks, gitlinks, modes); each path a commit touches is a path
    the approved series without its debug commits changes; and every file of
    every reviewed tree is the base's or one the approved series without its
    debug commits has in one of its trees (the same path, mode, and blob). A
    series that adds and later reverts anything else is refused, even when
    its head has the approved tree."""
    allowed_paths = {path for path, _mode, _blob in _changes(work, base, clean)}
    approved_entries: set[tuple[str, str | None, str | None]] = set()
    for sha in _revs(work, base, clean):
        approved_entries.update(_changes(work, base, sha))
    problems = []
    for sha in reviewed[:-1]:
        report = publisher.validate_update(work, base=base, expected_head=approved,
                                           candidate=sha, scope=scope)
        problems += [f"commit {sha[:12]}: {p}" for p in report.problems]
    for sha in reviewed:
        parent = _out(work, "rev-parse", f"{sha}^")
        touched = sorted({path for path, _m, _b in _changes(work, parent, sha)} - allowed_paths)
        if touched:
            problems.append(f"commit {sha[:12]} touches {', '.join(touched[:5])}, which the "
                            "approved series does not change")
        foreign = sorted(path for path, mode, blob in _changes(work, base, sha)
                         if path in allowed_paths and (path, mode, blob) not in approved_entries)
        if foreign:
            problems.append(f"commit {sha[:12]} has content in {', '.join(foreign[:5])} that no "
                            "commit of the approved series has")
    return problems


def _identity_problem(task: dict, result: dict) -> str | None:
    for result_key, task_key in (("contribution_id", "contribution_id"),
                                 ("expected_revision", "revision"),
                                 ("expected_head", "expected_head")):
        if result.get(result_key) != task.get(task_key):
            return f"the Result's {result_key} does not answer the Work"
    return None


def _exact(task: dict, kind: str) -> bool:
    return task.get("task") == kind and bool(_SHA.match(str(task.get("expected_head") or ""))) \
        and bool(_SHA.match(str(task.get("base_sha") or "")))


def check_rewrite(task: dict, rewrite: dict, mbox: bytes | None, *,
                  workdir: Path | None = None, read_token: str | None = None) -> dict:
    """The trusted outcome of one submission rewrite Result."""
    out: dict[str, Any] = {"status": REWRITE, "stage": "submission_check",
                           "approved_head": task.get("expected_head"),
                           "base_sha": task.get("base_sha")}

    def fail(reasons: list[str], **extra) -> dict:
        return dict(out, verdict=FAIL, reasons=[str(r)[:2000] for r in reasons][:MAX_REASONS],
                    **extra)

    if not _exact(task, REWRITE):
        return unusable("the Work is not a submission rewrite of an exact head")
    try:
        schemas.validate("submission_rewrite", rewrite)
    except schemas.SchemaError as error:
        return unusable(f"the rewrite is malformed: {error}")
    problem = _identity_problem(task, rewrite)
    if problem:
        return unusable(problem)
    if rewrite["verdict"] == FAIL:
        return fail([f"rewrite: {r}" for r in rewrite["reasons"]])
    title, body = rewrite["title"], rewrite["body"]
    if "\n" in title or "\r" in title or not title.strip() or len(title) > MAX_TITLE:
        return unusable("the title is not one line of at most 256 characters")
    if len(body) > MAX_BODY:
        return unusable(f"the description has {len(body)} characters, more than {MAX_BODY}")
    digest = hashlib.sha256(mbox or b"").hexdigest()
    if not mbox or rewrite["series_digest"] != digest:
        return unusable("the rewritten series is missing or does not match its digest")
    try:
        rules, sections, notes = rules_for(task, rewrite)
    except commit_rules.RuleError as error:
        return fail([f"check: invalid rules: {error}"])
    tmp = tempfile.TemporaryDirectory(prefix="upstreaming-review-") if workdir is None else None
    work = Path(tmp.name) / "repo" if tmp else workdir
    try:
        try:
            built = reconstruct(work, fork=task["fork"], upstream=task["upstream"],
                                base=task["base_sha"], approved=task["expected_head"], mbox=mbox,
                                scope_paths=task.get("allowed_paths") or [],
                                read_token=read_token)
        except fork_publish.PublishRejected as error:
            return fail([f"check: the rewritten series does not rebuild: {error}"])
    finally:
        if tmp:
            tmp.cleanup()
    messages = [c["message"] for c in built["commits"]]
    reasons = [f"check: {p}" for p in built["problems"]]
    if built["tree"] != built["clean_tree"]:
        reasons.append("check: the rewritten series changes the tree of the approved series "
                       "without its debug commits; the rewrite may not change code")
    reasons += [f"check: {p}" for p in commit_rules.series_problems(messages, rules)]
    reasons += [f"check: {p}" for p in commit_rules.attribution_problems(
        messages, built["approved_messages"])]
    reasons += [f"check: {p}" for p in commit_rules.body_problems(body, sections)]
    result = {"title": title, "body": body, "head": built["head"], "series_digest": digest,
              "text_digest": text_digest(title, body),
              "commits": [{"message": c["message"], "tree": c["tree"]} for c in built["commits"]],
              "clean_tree": built["clean_tree"], "candidate_tree": built["candidate_tree"],
              "rules_from": notes}
    if reasons:
        return fail(reasons, **result)
    return dict(out, verdict=PASS, reasons=[], **result)


def check_review(task: dict, review: dict) -> dict:
    """The trusted outcome of one submission review Result: a review of
    exactly the rewritten series and text the Work names
    (``context.rewrite``)."""
    out: dict[str, Any] = {"status": REVIEW, "stage": "review_check",
                           "approved_head": task.get("expected_head"),
                           "base_sha": task.get("base_sha")}
    rewrite = (task.get("context") or {}).get("rewrite") or {}
    if not _exact(task, REVIEW) or not re.fullmatch(r"[0-9a-f]{64}", str(rewrite.get("series_digest"))) \
            or not re.fullmatch(r"[0-9a-f]{64}", str(rewrite.get("text_digest"))) \
            or not rewrite.get("evidence_id"):
        return unusable("the Work is not a submission review of a rewritten series")
    try:
        schemas.validate("submission_review", review)
    except schemas.SchemaError as error:
        return unusable(f"the review is malformed: {error}")
    problem = _identity_problem(task, review)
    if problem:
        return unusable(problem)
    if review["series_digest"] != rewrite["series_digest"] \
            or review["text_digest"] != rewrite["text_digest"]:
        return unusable("the review names another series or text than the rewrite it was given")
    out.update(rewrite_evidence_id=str(rewrite["evidence_id"]),
               series_digest=review["series_digest"], text_digest=review["text_digest"],
               expectations=[{k: e[k] for k in ("expectation", "met", "source", "note") if k in e}
                             for e in review["expectations"]])
    if review["verdict"] == FAIL:
        return dict(out, verdict=FAIL,
                    reasons=[f"review: {r}"[:2000] for r in review["reasons"]][:MAX_REASONS])
    unmet = [e["expectation"] for e in review["expectations"] if not e["met"]]
    if unmet:
        return dict(out, verdict=FAIL, reasons=[f"review: expectation not met: {e}"[:2000]
                                                for e in unmet][:MAX_REASONS])
    return dict(out, verdict=PASS, reasons=[])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m submission")
    parser.add_argument("command", choices=["check-rewrite", "check-review"])
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True,
                        help="the rewrite or review proposal.json")
    parser.add_argument("--mbox", type=Path, help="the rewritten series (check-rewrite)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    task = json.loads(args.task.read_text())
    try:
        result_value = json.loads(args.result.read_text())
        if args.command == "check-review":
            result = check_review(task, result_value)
        else:
            mbox = args.mbox.read_bytes() if args.mbox and args.mbox.is_file() \
                and not args.mbox.is_symlink() else None
            result = check_rewrite(task, result_value, mbox)
    except (OSError, ValueError) as error:
        result = unusable(f"the Result cannot be read: {str(error)[:300]}")
    except CheckError as error:
        result = {"status": "infra_error", "failure_class": "infrastructure",
                  "stage": "submission_check", "reason": str(error)[:2000]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
