#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Generate the public porting status page from durable contribution state."""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine  # noqa: E402
import lifecycle  # noqa: E402

CONTROL_REPO = "riseproject-dev/porting-agent"
GITHUB = "https://github.com"

# The name of each phase of the lifecycle table on the page.
PHASE_LABELS = {
    "queued": "Requested",
    "planning": "Planning",
    "provisioning": "Preparing fork",
    "development": "Development",
    "handoff": "Ready for upstream",
    "upstream_review": "Upstream review",
    "release_wait": "Waiting for release",
    "released": "Released",
    "parked": "Parked",
    "closed": "Closed",
}

# Why a contribution is closed (`terminal_reason` of `closed.idle`).
TERMINAL_REASON_LABELS = {
    "abandoned": "abandoned",
    "withdrawn": "request withdrawn",
    "no_work": "not applicable",
    "deferred": "deferred",
    "already_released": "already released",
}

# What a contribution waits for when no row the controller takes can apply
# yet, by the trigger of the rows that leave its state.
WAIT_LABELS = {
    lifecycle.Trigger.OBSERVATION: "observation",
    lifecycle.Trigger.RESULT: "the Result of requested Work",
    lifecycle.Trigger.OPERATION: "a remote operation",
}
# The rows a human command or a rule takes in many states say nothing about
# what a state waits for.
_RULE_VERBS = frozenset(lifecycle.RULES)


def _policy() -> dict:
    return json.loads((ROOT / "policy.json").read_text())


def _waiting_for(state: dict, key: str, ci_evidence: dict | None) -> str:
    """What a state of kind `work` or `auto_wait` waits for, from the table.
    In an `auto_wait` state: the first row the controller takes whose record
    guards hold, with the guards only the controller observes (a worker
    slot, capacity), or else the first guard of such a row that does not
    hold. Otherwise, and in a `work` state: the triggers of the rows that
    leave the state."""
    unmet = None
    if lifecycle.LIFECYCLE[key].kind == "auto_wait":
        ctx = engine.Context(state, key, _policy(), state.get("updated_at") or "",
                             ci_evidence=ci_evidence)
        for edge in lifecycle.state_edges(key):
            t = edge.transition
            if t.trigger != lifecycle.Trigger.TICK or edge.verb in _RULE_VERBS:
                continue
            facts, why = [], None
            for guard in t.guard:
                predicate = engine.PREDICATES.get(guard)
                if predicate is None:
                    why = guard
                elif predicate.kind == "fact":
                    facts.append(guard)
                else:
                    try:
                        why = guard if predicate.check(ctx) is not None else None
                    except (KeyError, TypeError, ValueError, AttributeError):
                        why = guard
                if why:
                    break
            if why is None:
                return f"next: {edge.verb}" + (f", once {'; '.join(facts)}" if facts else "")
            unmet = unmet or why
    triggers = [WAIT_LABELS[e.transition.trigger] for e in lifecycle.state_edges(key)
                if e.transition.trigger in WAIT_LABELS and e.verb not in _RULE_VERBS]
    if triggers:
        return "waiting for " + " or ".join(dict.fromkeys(triggers))
    return f"waiting until {unmet}" if unmet else ""


def status_label(state: dict, ci_evidence: dict | None = None) -> str:
    """The status of a contribution from the lifecycle table: its phase, the
    kind of its state, and what it waits for: the pending human action of a
    human wait, or the unmet guard of the next row the controller takes."""
    phase = PHASE_LABELS.get(state["phase"], state["phase"].replace("_", " "))
    key = f"{state['phase']}.{state['activity']}"
    spec = lifecycle.LIFECYCLE.get(key)
    if spec is None:
        detail = key
    elif spec.kind == "terminal":
        reason = state.get("terminal_reason")
        return f"{phase} ({TERMINAL_REASON_LABELS.get(reason, reason.replace('_', ' '))})" \
            if reason else phase
    elif spec.kind == "generation_end":
        detail = ""
    elif spec.kind in lifecycle.REMINDER_KINDS:
        pending = (state.get("pending_human_action") or {}).get("kind")
        detail = "waiting for a human" + (f": {pending.replace('_', ' ')}" if pending else "")
    else:
        detail = _waiting_for(state, key, ci_evidence)
    label = f"{phase} ({detail})" if detail else phase
    return f"Paused ({label})" if state.get("paused") else label


def escape_html(value: str) -> str:
    """Escape a value for HTML text or a quoted attribute. Braces are
    escaped too, so no value can start a Liquid tag when Jekyll renders the
    page."""
    return html.escape(str(value), quote=True).replace("{", "&#123;").replace("}", "&#125;")


def escape_cell(value: str) -> str:
    """Escape generated values for one table cell."""
    return escape_html(str(value).replace("\n", " "))


_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_RUN = re.compile(r"[1-9][0-9]{0,19}")
_URL = re.compile(r"https://github\.com/[A-Za-z0-9._~/#?=&%:+-]{1,500}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def html_link(label: str, url: str | None) -> str:
    """An HTML link to a GitHub URL the pipeline built or recorded, or ``-``."""
    if not url or not _URL.fullmatch(url):
        return "-"
    return f'<a href="{escape_html(url)}">{escape_cell(label)}</a>'


def repo_url(repo, *path) -> str | None:
    repo = str(repo or "")
    return "/".join((GITHUB, repo, *map(str, path))) if _REPO.fullmatch(repo) else None


def run_url(run_id, repo: str = CONTROL_REPO) -> str | None:
    run = str(run_id or "")
    return repo_url(repo, "actions/runs", run) if _RUN.fullmatch(run) else None


def commit_url(repo, sha) -> str | None:
    sha = str(sha or "")
    return repo_url(repo, "commit", sha) if _SHA.fullmatch(sha) else None


def state_file_url(kind: str, item_id: str) -> str:
    return f"{GITHUB}/{CONTROL_REPO}/blob/main/.state/{kind}/{item_id}.json"


# --------------------------------------------------------------------------
# Event log


_BOT = re.compile(r"[A-Za-z0-9._-]+\[bot\]")
_STATE_SUBJECT = re.compile(r"porting-agent: .* \(run ([1-9][0-9]{0,19})\)")


def state_commits(root: Path | None) -> dict[str, tuple[str, int | None]]:
    """For each event file under the state directory ``root``, as a path
    relative to it, the commit that added it and the controller run named by
    the subject `porting-agent: ... (run N)` of that commit, taken only when
    a bot authored it. Anything the history does not give (no git, a missing
    or shallow history, an unexpected output) yields fewer entries or none,
    never an error."""
    if root is None or not Path(root).is_dir():
        return {}
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "--relative", "--diff-filter=A", "--name-only",
             "--no-renames", "--format=%x00%H%x09%an%x09%s", "--", "events"],
            capture_output=True, text=True, timeout=300, check=False).stdout
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return {}
    found: dict[str, tuple[str, int | None]] = {}
    for block in out.split("\0")[1:]:
        header, _, names = block.partition("\n")
        parts = header.split("\t", 2)
        if len(parts) != 3 or not _SHA.fullmatch(parts[0]):
            continue
        sha, author, subject = parts
        match = _STATE_SUBJECT.fullmatch(subject) if _BOT.fullmatch(author) else None
        run = int(match[1]) if match else None
        for name in names.split("\n"):
            if re.fullmatch(r"events/[A-Za-z0-9._-]+/[0-9]{8}\.json", name):
                found.setdefault(name, (sha, run))
    return found


class StateData:
    """The committed records an event log resolves its references against."""

    def __init__(self, root: Path | None):
        self.root = root
        self._commits: dict[str, tuple[str, int | None]] | None = None

    def commit(self, event: dict) -> tuple[str, int | None] | None:
        """The state commit that added ``event`` to the repository and the
        controller run its subject names, read once from the git history of
        the state directory. ``None`` when the history does not say."""
        if self._commits is None:
            self._commits = state_commits(self.root)
        cid = str(event.get("contribution_id") or "").replace("/", "--")
        sequence = event.get("sequence")
        return self._commits.get(f"events/{cid}/{sequence:08d}.json") \
            if isinstance(sequence, int) and sequence >= 0 else None

    def _read(self, kind: str, item_id: str) -> dict | None:
        if self.root is None or not _ID.fullmatch(str(item_id or "")):
            return None
        path = self.root / kind / f"{item_id}.json"
        if not path.is_file() or path.is_symlink():
            return None
        try:
            data = json.loads(path.read_text())
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def evidence(self, item_id):
        return self._read("evidence", item_id)

    def operation(self, item_id):
        return self._read("operations", item_id)

    def events(self, contribution_id: str) -> list[dict]:
        if self.root is None:
            return []
        directory = self.root / "events" / contribution_id.replace("/", "--")
        if not _ID.fullmatch(directory.name) or not directory.is_dir():
            return []
        events = []
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or not re.fullmatch(r"[0-9]{8}\.json", path.name):
                continue
            try:
                event = json.loads(path.read_text())
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("sequence"), int):
                events.append(event)
        return sorted(events, key=lambda event: event["sequence"])


def event_states(events: list[dict]) -> list[tuple]:
    """The source and destination state and revision of each event, in
    sequence order, as ``(source, target, old_revision, revision,
    source_derived, target_derived)``. An event of the lifecycle table names
    its states in `details`; an older event records only the fields it
    changed, which are folded over the events before it, and a state placed
    that way is *derived*."""
    phase, activity, revision = None, "idle", 0
    out = []
    for event in events:
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        before = event.get("from") if isinstance(event.get("from"), dict) else {}
        to = event.get("to") if isinstance(event.get("to"), dict) else {}
        source = details.get("from_state")
        source_derived = not source
        if not source and (before.get("phase") or phase):
            source = f"{before.get('phase') or phase}.{before.get('activity') or activity}"
        target = details.get("to_state")
        target_derived = not target
        if not target and (to.get("phase") or phase):
            target = f"{to.get('phase') or phase}.{to.get('activity') or activity}"
        if target and "." in str(target):
            phase, activity = str(target).split(".", 1)
        old_revision = before.get("revision", revision)
        if isinstance(to.get("revision"), int):
            revision = to["revision"]
        elif event.get("revision_bumped") is True and isinstance(revision, int):
            revision += 1
        elif isinstance(details.get("revision"), int):
            # An older event that names the revision it is bound to.
            revision = details["revision"]
        out.append((source, target, old_revision if isinstance(old_revision, int) else None,
                    revision, source_derived and source is not None,
                    target_derived and target is not None))
    return out


def _strings(value, depth: int = 0):
    """Every string in a bounded walk of ``value``, in order."""
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from _strings(value[key], depth + 1)
    elif isinstance(value, list):
        for item in value[:50]:
            yield from _strings(item, depth + 1)


def _evidence_links(evidence_id: str, evidence: dict) -> list[tuple[str, str | None]]:
    links = [(f"evidence {evidence_id}", state_file_url("evidence", evidence_id))]
    ci = evidence.get("ci") if isinstance(evidence.get("ci"), dict) else None
    if ci:
        repo = ci.get("repository")
        links.append((f"CI run {ci.get('run_id')}", run_url(ci.get("run_id"), repo)))
        links.append((f"commit {str(ci.get('head_sha') or '')[:12]}", commit_url(repo, ci.get("head_sha"))))
        for job in ci.get("jobs") or []:
            if isinstance(job, dict) and job.get("executed") \
                    and job.get("conclusion") not in ("success", "skipped", "neutral", None) \
                    and _RUN.fullmatch(str(job.get("job_id") or "")) and run_url(ci.get("run_id"), repo):
                links.append((f"failed job {job['job_id']}",
                              f"{run_url(ci.get('run_id'), repo)}/job/{job['job_id']}"))
    return links


def _operation_links(operation_id: str, op: dict, record: dict) -> list[tuple[str, str | None]]:
    links = [(f"operation {operation_id}", state_file_url("operations", operation_id))]
    target = op.get("target") if isinstance(op.get("target"), dict) else {}
    external = op.get("external_ids") if isinstance(op.get("external_ids"), dict) else {}
    proposed = op.get("proposed") if isinstance(op.get("proposed"), dict) else {}
    for key in sorted(external):
        if key.endswith("_run_id"):
            links.append((f"workflow run {external[key]}", run_url(external[key])))
    repo = target.get("repository")
    if str(op.get("kind") or "").startswith("dev_pr") and external.get("pr_number"):
        links.append((f"development PR #{external['pr_number']}",
                      repo_url(repo, "pull", external["pr_number"])
                      if _RUN.fullmatch(str(external["pr_number"])) else None))
    if proposed.get("commit"):
        links.append((f"commit {str(proposed['commit'])[:12]}", commit_url(repo, proposed["commit"])))
    return links


def event_links(event: dict, record: dict,
                data: StateData) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Every reference of ``event`` that committed data resolves, as
    ``(label, url)`` pairs in a fixed order and without duplicates: the links
    of its cause, then every other link. The controller run of the event is
    shown in its own column and is not listed again."""
    fork = record.get("fork") or {}
    fork_repo = fork.get("name")
    issue = (record.get("tracking_issue") or {}).get("number")
    upstream_pr = record.get("upstream_pr") or {}
    cause = event.get("cause") if isinstance(event.get("cause"), dict) else {}
    cause_id = str(cause.get("id") or "")
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    links: list[tuple[str, str | None]] = []

    def head(sha, what="commit"):
        full = str(fork.get("head_sha") or "")
        sha = full if sha and full.startswith(str(sha)) else sha
        links.append((f"{what} {str(sha or '')[:12]}", commit_url(fork_repo, sha)))

    # The cause.
    if _RUN.fullmatch(cause_id):
        what = {"result": "worker run", "tick": "controller run"}.get(cause.get("type"),
                                                                       "workflow run")
        links.append((f"{what} {cause_id}", run_url(cause_id)))
    elif m := re.fullmatch(r"dispatcher-([0-9]+)", cause_id):
        links.append((f"dispatcher run {m[1]}", run_url(m[1])))
    elif m := re.fullmatch(r"(?:branch:|clean-head-ci-)([0-9a-f]{7,40})", cause_id):
        head(m[1])
    elif m := re.fullmatch(r"comment-([0-9]+)", cause_id):
        if _RUN.fullmatch(str(issue or "")):
            links.append((f"comment {m[1]}",
                          f"{GITHUB}/{CONTROL_REPO}/issues/{issue}#issuecomment-{m[1]}"))
    elif re.fullmatch(r"issue-[0-9]+", cause_id) and issue:
        links.append((f"tracking issue #{issue}", repo_url(CONTROL_REPO, "issues", issue)))
    split = len(links)
    # Evidence and operations the cause or the details name.
    for value in [cause_id, *_strings(details)]:
        evidence = data.evidence(value)
        if evidence is not None:
            links.extend(_evidence_links(value, evidence))
            continue
        op = data.operation(value)
        if op is not None:
            links.extend(_operation_links(value, op, record))
    # Heads, pull requests, and runs the details name.
    for key in ("head", "clean_head", "replaced"):
        if details.get(key):
            head(details[key], "clean head" if key == "clean_head" else "commit")
    bound = details.get("bound_to") if isinstance(details.get("bound_to"), dict) else {}
    if bound.get("head"):
        head(bound["head"])
    if m := re.fullmatch(r"pr-([1-9][0-9]*)", cause_id):
        links.append((f"development PR #{m[1]}", repo_url(fork_repo, "pull", m[1])))
    if re.fullmatch(r"upstream-pr-[0-9]+", cause_id) or details.get("verb") == "verify_upstream_pr":
        url = details.get("url") or upstream_pr.get("url")
        number = details.get("pr_number") or upstream_pr.get("number")
        links.append((f"upstream PR #{number}", url if _URL.fullmatch(str(url or "")) else None))
    if details.get("package_url"):
        url = str(details["package_url"])
        links.append(("submission package", url if _URL.fullmatch(url) else None))
    for item in details.get("comments") or []:
        url = str((item or {}).get("url") or "") if isinstance(item, dict) else ""
        if _URL.fullmatch(url):
            links.append(("comment", url))
    seen, parts = set(), ([], [])
    for n, (label, url) in enumerate(links):
        if url and _URL.fullmatch(url) and url not in seen:
            seen.add(url)
            parts[n >= split].append((label, url))
    return parts


CHEVRON = ('<svg class="event-log__chevron" viewBox="0 0 16 16" width="16" height="16" '
           'aria-hidden="true" focusable="false"><path d="M 3.5 6 L 8 10.5 L 12.5 6" fill="none" '
           'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
           'stroke-linejoin="round"/></svg>')

# Where a state or a verb is described: the generated lifecycle table. The
# path is relative so that it holds on the site and on a pull-request preview.
TABLE_PAGE = "../state-machine/table/"

# What each state kind means, in words a reader of the badge can use.
KIND_LABELS = {
    "work": "work in progress",
    "auto_wait": "waits for an automatic trigger",
    "human_wait": "waits for a human",
    "gate": "waits for a human command",
    "locked": "locked, waiting for a fixed exit",
    "generation_end": "generation ended",
    "terminal": "ended",
}
UNKNOWN_KIND = "unknown"
UNKNOWN_LABEL = "a state the lifecycle table does not have"

# Who or what the cause of an event is.
CAUSE_LABELS = {
    "command": "command",
    "observation": "observation",
    "result": "worker Result",
    "operation": "operation",
    "tick": "controller",
    "migration": "migration",
    "policy": "policy",
}

# Events that only keep the records current.
BOOKKEEPING_KINDS = frozenset({"record_fields_migrated", "record_names_migrated",
                               "state_text_normalized"})
STATE_KEYS = ("phase", "activity", "paused", "generation", "parked_reason")


def _time(value) -> tuple[str, str]:
    try:
        at = datetime.fromisoformat(str(value)).astimezone(timezone.utc)
    except ValueError:
        return "", "-"
    return at.isoformat(timespec="seconds"), at.strftime("%Y-%m-%d %H:%M:%S")


def _anchor_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value))


def state_anchor(key: str) -> str:
    return "state-" + _anchor_id(key.replace(".", "-"))


def _current_state(key):
    """A state under its current name; ``None`` stays ``None``."""
    if not isinstance(key, str):
        return None
    now = lifecycle.current_name(key)
    if now == key and "." in key:
        phase, activity = key.split(".", 1)
        now = f"{lifecycle.current_name(phase)}.{activity}"
    return now


def _first_sentence(text: str, limit: int = 240) -> str:
    """The first sentence of a table description as plain text."""
    text = " ".join(str(text or "").replace("`", "").split())
    if m := re.search(r"[.!?](?=\s+[A-Z])", text):
        text = text[:m.end()]
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return text


def _flag_transition(verb: str):
    for name, flag in lifecycle.FLAGS.items():
        for table in (flag.set_by, flag.cleared_by):
            if verb in table:
                return name, table[verb]
    return None


def find_verb(verb: str, source: str | None):
    """``(transition, anchor)`` of a current verb of the lifecycle table: the
    row it takes from ``source`` when it has one, else the first row of the
    table that has it. A rule and a flag verb are found in their section."""
    if verb in lifecycle.RULES:
        return lifecycle.RULES[verb].transition, "rule-" + _anchor_id(verb)
    if flagged := _flag_transition(verb):
        return flagged[1], "flag-" + _anchor_id(flagged[0])
    if source in lifecycle.LIFECYCLE:
        for edge in lifecycle.state_edges(source):
            if edge.verb == verb:
                return edge.transition, state_anchor(source)
    for edge in lifecycle.table_edges():
        if edge.verb == verb:
            return edge.transition, state_anchor(edge.source)
    return None, None


def event_summary(verb: str, source: str | None, target: str | None, kind: str) -> str:
    """What the event did, in one plain sentence: the first sentence of the
    description of its row, else the description of the state it leads to,
    else what the pipeline recorded for an event it no longer writes."""
    transition, _ = find_verb(verb, source)
    if transition is not None:
        text = _first_sentence(transition.description)
        if not text and target in lifecycle.LIFECYCLE:
            text = _first_sentence(lifecycle.LIFECYCLE[target].description)
        return text
    import status_ledger

    return _first_sentence(status_ledger.RECORDED_KINDS.get(kind, ""))


def event_run(event: dict) -> tuple[int, int | None] | None:
    """The workflow run that recorded an event: its `run`, else the
    controller run a `tick` event names in its details. Events written before
    the run was recorded have neither."""
    def positive(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 \
            else None

    run = event.get("run")
    if isinstance(run, dict) and positive(run.get("id")):
        return run["id"], positive(run.get("attempt"))
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    if _RUN.fullmatch(str(details.get("run_id") or "")):
        return int(details["run_id"]), None
    return None


def _run_cell(run, commit: str | None = None) -> str:
    out = "-"
    if run is not None:
        run_id, attempt = run
        url = run_url(run_id)
        if url and attempt:
            url += f"/attempts/{attempt}"
        label = f"run {run_id}" + (f" (attempt {attempt})" if attempt and attempt > 1 else "")
        out = html_link(label, url)
    if commit:
        link = html_link(f"state commit {commit[:7]}", commit_url(CONTROL_REPO, commit))
        if link != "-":
            out = link if out == "-" else f"{out}<div>{link}</div>"
    return out


def state_badge(key: str | None, *, recorded_as: str | None = None, derived: bool = False,
                link: bool = True) -> str:
    """A state as a badge: its color is its kind, its text names the kind
    for readers who do not see the color, and it links to the state in the
    lifecycle table."""
    if not key:
        return "-"
    spec = lifecycle.LIFECYCLE.get(key)
    kind = spec.kind if spec else UNKNOWN_KIND
    classes = ["state", f"state--{kind}"]
    if spec and kind == "terminal":
        classes.append(f"state--{'released' if key.startswith('released.') else 'closed'}")
    if derived:
        classes.append("state--derived")
    label = KIND_LABELS.get(kind, UNKNOWN_LABEL) if spec else UNKNOWN_LABEL
    notes = [label] + (["derived from earlier events"] if derived else [])
    title = (_first_sentence(spec.description) if spec else UNKNOWN_LABEL)
    hidden = escape_cell("; ".join(notes))
    inner = f'{escape_cell(key)}<span class="sr-only"> ({hidden})</span>'
    attrs = f'class="{" ".join(classes)}" title="{escape_cell(title)}"'
    if spec and link:
        return f'<a {attrs} href="{TABLE_PAGE}#{state_anchor(key)}">{inner}</a>'
    return f"<span {attrs}>{inner}</span>"


def legend() -> str:
    """One line that names every badge color."""
    items = []
    for kind, label in KIND_LABELS.items():
        items.append(f'<span class="state state--{kind}">{escape_cell(kind)}</span> '
                     f'{escape_cell(label)}')
    items.append(f'<span class="state state--{UNKNOWN_KIND}">other</span> {UNKNOWN_LABEL}')
    return ('<p class="state-legend">State badges, colored by the kind of the state '
            "(the kind is also written in the badge for screen readers): "
            + "; ".join(items) + ".</p>")


class Row:
    """One row of an event log: an event, or the identical events of one run
    that were folded into it."""

    def __init__(self, event, states, record, data):
        self.events = [event]
        source, target, old_revision, revision, source_derived, target_derived = states
        self.raw_source, self.raw_target = source, target
        source, target = _current_state(source), _current_state(target)
        self.source, self.target = source, target
        self.old_revision, self.revision = old_revision, revision
        self.source_derived, self.target_derived = source_derived, target_derived
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        self.kind = str(event.get("kind") or "")
        self.recorded_verb = str(details.get("verb") or self.kind)
        self.verb = lifecycle.current_name(self.recorded_verb)
        cause = event.get("cause") if isinstance(event.get("cause"), dict) else {}
        self.cause_type = str(cause.get("type") or "")
        self.cause_id = cause.get("id")
        self.cause_links, self.links = event_links(event, record, data)
        git = data.commit(event)
        self.commit = git[0] if git else None
        self.run = event_run(event) or ((git[1], None) if git and git[1] else None)
        to = event.get("to") if isinstance(event.get("to"), dict) else {}
        self.paused = to.get("paused")
        self.terminal = to.get("phase") in lifecycle.TERMINAL
        changed = (source != target or old_revision != revision
                   or any(key in to for key in STATE_KEYS))
        self.bookkeeping = (self.cause_type == "migration" or self.kind in BOOKKEEPING_KINDS
                            or self.kind.startswith("canary:"))
        self.minor = not changed and not self.bookkeeping
        self.state_change = changed and not self.bookkeeping
        self.known_verb = find_verb(self.verb, source)[0] is not None
        self.at = event.get("at")

    def fold_key(self):
        """Events fold when one controller run recorded identical ones: the
        same verb, states, cause, revision and links. A run that is not
        recorded is told by the exact time of the tick."""
        when = self.run if self.run else ("at", self.at)  # tick time when no run is known
        return (when, self.recorded_verb, self.source, self.target, self.old_revision,
                self.revision, self.cause_type, self.cause_id, self.paused,
                tuple(self.cause_links), tuple(self.links))


def _revision_cell(row: Row) -> str:
    if row.revision is None:
        return "-"
    if row.old_revision is not None and row.old_revision != row.revision:
        return f"<strong>r{row.old_revision} &rarr; r{row.revision}</strong>"
    return f"r{row.revision}"


def _transition_cell(row: Row) -> str:
    verb_text = f"<code>{escape_cell(row.verb)}</code>"
    anchor = find_verb(row.verb, row.source)[1]
    if anchor:
        verb_text = f'<a href="{TABLE_PAGE}#{anchor}">{verb_text}</a>'
    if row.count > 1:
        verb_text += f' <span class="transition__count">&times;{row.count}</span>'
    derived = row.source_derived or row.target_derived
    if row.source and row.source == row.target:
        body = (f'{state_badge(row.source, derived=row.source_derived)} '
                f'<span class="transition__verb">&#8635; {verb_text}'
                '<span class="sr-only"> (stays in this state)</span></span>')
    else:
        body = (f'{state_badge(row.source, derived=row.source_derived)} '
                f'<span class="transition__verb">&rarr; {verb_text} &rarr;</span> '
                f'{state_badge(row.target, derived=row.target_derived)}')
    chips = ""
    if row.paused is True:
        chips = ' <span class="event-log__flag">paused</span>'
    elif row.paused is False and not row.terminal:
        chips = ' <span class="event-log__flag">resumed</span>'
    tags = ""
    if row.bookkeeping:
        tags += ' <span class="event-table__tag">bookkeeping</span>'
    elif not row.known_verb:
        tags += (' <span class="event-table__tag" title="Recorded under a name '
                 'the lifecycle table does not have.">earlier event</span>')
    if derived:
        tags += (' <span class="event-table__tag" title="The states of this event are '
                 'derived from the events before it.">derived</span>')
    summary = event_summary(row.verb, row.source, row.target, row.kind)
    recorded = []
    if row.recorded_verb != row.verb:
        recorded.append(row.recorded_verb)
    for original, now in ((row.raw_source, row.source), (row.raw_target, row.target)):
        if original and original != now:
            recorded.append(original)
    out = body + chips + tags
    if summary:
        out += f'<div class="transition__summary">{escape_cell(summary)}</div>'
    if recorded:
        out += ('<div class="transition__recorded">(recorded as '
                + ", ".join(f"<code>{escape_cell(name)}</code>" for name in recorded) + ")</div>")
    return out


def _sequences(row: Row) -> str:
    return ", ".join(f"#{int(e['sequence'])}" for e in reversed(row.events))


def _cause_cell(row: Row) -> str:
    out = f'<span class="cause__type">{escape_cell(CAUSE_LABELS.get(row.cause_type, row.cause_type or "-"))}</span>'
    for label, url in row.cause_links:
        out += f"<div>{html_link(label, url)}</div>"
    return out


def render_row(row: Row, cid: str, same_run: bool) -> str:
    stamp, shown = _time(row.events[-1].get("at"))
    classes = ["event-table__event"]
    if row.minor:
        classes.append("event-table__event--minor")
    if row.bookkeeping:
        classes.append("event-table__event--bookkeeping")
    if same_run:
        classes.append("event-table__event--same-run")
    time_html = f'<time datetime="{escape_html(stamp)}">{escape_cell(shown)}</time>' if stamp else "-"
    links = "".join(f"<div>{html_link(label, url)}</div>" for label, url in row.links) or "-"
    return (
        f'<tr class="{" ".join(classes)}" id="ev-{escape_html(_anchor_id(cid))}-'
        f'{int(row.events[0]["sequence"])}">'
        f'<td data-label="When (UTC)">{time_html} <span class="event-table__seq">'
        f'{escape_cell(_sequences(row))}</span></td>'
        f'<td data-label="Transition">{_transition_cell(row)}</td>'
        f'<td data-label="Cause">{_cause_cell(row)}</td>'
        f'<td data-label="Rev">{_revision_cell(row)}</td>'
        f'<td data-label="Controller run">{_run_cell(row.run, row.commit)}</td>'
        f'<td data-label="Links">{links}</td></tr>')


def fold_rows(events: list[dict], record: dict, data: StateData) -> list[Row]:
    """The rows of an event log in sequence order. Adjacent identical events
    of one run share a row, which lists every sequence number."""
    rows: list[Row] = []
    for event, states in zip(events, event_states(events)):
        row = Row(event, states, record, data)
        row.count = 1
        if rows and rows[-1].fold_key() == row.fold_key() and not row.state_change:
            rows[-1].events.append(event)
            rows[-1].count += 1
        else:
            rows.append(row)
    return rows


def phase_path(rows: list[Row]) -> str:
    """The phases the contribution went through, oldest first."""
    phases: list[str] = []
    for row in rows:
        for state in (row.source, row.target) if not phases else (row.target,):
            if state:
                phase = lifecycle.current_name(str(state).split(".", 1)[0])
                if not phases or phases[-1] != phase:
                    phases.append(phase)
    return " &rarr; ".join(escape_cell(PHASE_LABELS.get(phase, phase.replace("_", " ")))
                           for phase in phases)


def render_event_log(record: dict, data: StateData) -> str:
    """The collapsed event log of one contribution, newest event first."""
    cid = str(record.get("contribution_id") or "")
    events = data.events(cid)
    rows = fold_rows(events, record, data)
    count = len(events)
    changes = sum(len(row.events) for row in rows if row.state_change)
    noun = "event" if count == 1 else "events"
    change_noun = "state change" if changes == 1 else "state changes"
    summary = (f'<summary>{CHEVRON}<span>Event log</span> '
               f'<span class="event-log__count">({count} {noun}, {changes} {change_noun})'
               '</span></summary>')
    if not events:
        return f'<details class="event-log">{summary}<p>No events are recorded.</p></details>'
    toggle = f"state-changes-{_anchor_id(cid)}"
    shown = list(reversed(rows))
    body = []
    for n, row in enumerate(shown):
        same_run = bool(row.run) and n > 0 and shown[n - 1].run == row.run
        body.append(render_row(row, cid, same_run))
    head = "".join(f'<th scope="col">{escape_cell(h)}</th>' for h in EVENT_HEADERS)
    return (
        f'<details class="event-log">{summary}'
        f'<p class="event-log__path">{phase_path(rows)}</p>'
        f'<p class="event-log__filter"><input type="checkbox" class="event-log__toggle" '
        f'id="{escape_html(toggle)}"> <label for="{escape_html(toggle)}">State changes only</label></p>'
        '<table class="event-table">'
        f'<caption class="sr-only">Events of {escape_cell(cid)}, newest first</caption>'
        f"<thead><tr>{head}</tr></thead><tbody>" + "".join(body) + "</tbody></table></details>")


EVENT_HEADERS = ("When (UTC)", "Transition", "Cause", "Rev", "Controller run", "Links")


HEADERS = ("Project", "Tracking issue", "Status", "Development PR", "Upstream PR", "Updated (UTC)")


def render(states: list[dict], state_root: Path | None = None) -> str:
    """The status page. ``state_root`` is the committed `.state` directory
    from which each contribution's event log is read."""
    data = StateData(state_root)
    states = sorted(states, key=lambda state: (state["upstream"]["full_name"].casefold(),
                                               str(state.get("contribution_id") or "")))
    lines = [
        "---",
        "title: Status",
        "nav_order: 2",
        "permalink: /status/",
        "---",
        "",
        "# Porting status",
        "",
        "This table tracks work to contribute RISC-V support back to upstream projects. "
        "It is generated from the pipeline's durable state whenever the website is built. "
        "Open the event log under a project to see every recorded event, newest first: when it "
        "happened, the transition from one state to another (each state links to its entry in "
        "the [lifecycle table]({{ '/state-machine/table/' | relative_url }})), what caused it, "
        "the revision, the controller run that recorded it, and links to the workflow runs, "
        "commits, pull requests, CI runs, and evidence it refers to. Events that only keep "
        "the records current, and events that change nothing, are shown muted.",
        "",
        legend(),
        "",
        '<table class="status-table">',
        "<thead><tr>" + "".join(f'<th scope="col">{escape_cell(h)}</th>' for h in HEADERS) + "</tr></thead>",
    ]
    for state in states:
        full_name = state["upstream"]["full_name"]
        issue = state["tracking_issue"]["number"]
        fork = state.get("fork") or {}
        internal_url = None
        if fork.get("name") and fork.get("pr_number"):
            internal_url = repo_url(fork["name"], "pull", fork["pr_number"])
        upstream_pr = state.get("upstream_pr") or {}
        updated = datetime.fromisoformat(state["updated_at"]).strftime("%Y-%m-%d %H:%M")
        cells = [
            html_link(full_name, repo_url(full_name)) if repo_url(full_name)
            else escape_cell(full_name),
            html_link(f"#{issue}", repo_url(CONTROL_REPO, "issues", issue)),
            escape_cell(status_label(state, data.evidence(state.get("last_ci_evidence_id")))),
            html_link(f"#{fork.get('pr_number')}", internal_url) if internal_url else "-",
            html_link(f"#{upstream_pr.get('number')}", upstream_pr.get("url")) if upstream_pr else "-",
            escape_cell(updated),
        ]
        lines.append("<tbody>")
        lines.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
        lines.append(f'<tr class="event-log-row"><td colspan="{len(HEADERS)}">'
                     + render_event_log(state, data) + "</td></tr>")
        lines.append("</tbody>")
    lines.extend(
        [
            "</table>",
            "",
            "Status changes are recorded by the automated porting pipeline. The tracking issue "
            "receives an entry when a step needs a human (a gate, a human wait, a pause, a park, "
            "and their reminders) and when a contribution ends; every other event is shown only "
            "in the event log above.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=Path(".state/contributions"))
    parser.add_argument("--output", type=Path, default=Path("docs/status/index.md"))
    args = parser.parse_args()
    states = [json.loads(path.read_text()) for path in sorted(args.state_dir.glob("*.json"))]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(states, args.state_dir.parent))


if __name__ == "__main__":
    main()
