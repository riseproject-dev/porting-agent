# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT

import json
import re
import tempfile
import unittest
from pathlib import Path

import lifecycle
from scripts import generate_doc

FORK = "riseproject-upstreaming/bravo"
HEAD = "b29a8f49374004d650cf312e9c31848953e5ae60"


def record(**over):
    rec = {
        "contribution_id": "example--bravo/1",
        "tracking_issue": {"number": 42},
        "phase": "handoff",
        "activity": "awaiting_submission",
        "revision": 6,
        "fork": {"name": FORK, "pr_number": 2, "head_sha": HEAD},
        "upstream_pr": None,
        "upstream": {"full_name": "example/bravo"},
        "updated_at": "2026-11-20T15:00:00+00:00",
    }
    rec.update(over)
    return rec


def event(sequence, kind, cause_type, cause_id, **over):
    out = {"schema_version": 1, "contribution_id": "example--bravo/1", "sequence": sequence,
           "at": f"2026-11-2{sequence % 10}T01:02:03.456+00:00", "kind": kind, "from": {}, "to": {},
           "cause": {"type": cause_type, "id": cause_id}, "revision_bumped": False}
    out.update(over)
    return out


def git(root: Path, *args: str) -> str:
    import subprocess
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout


def write_state(root: Path, rec: dict, events: list[dict], evidence=(), operations=()):
    state = root / ".state"
    for kind, items, key in (("evidence", evidence, "evidence_id"),
                             ("operations", operations, "operation_id")):
        (state / kind).mkdir(parents=True, exist_ok=True)
        for item in items:
            (state / kind / f"{item[key]}.json").write_text(json.dumps(item))
    (state / "contributions").mkdir(parents=True, exist_ok=True)
    cid = rec["contribution_id"].replace("/", "--")
    (state / "contributions" / f"{cid}.json").write_text(json.dumps(rec))
    (state / "events" / cid).mkdir(parents=True, exist_ok=True)
    for item in events:
        (state / "events" / cid / f"{item['sequence']:08d}.json").write_text(json.dumps(item))
    return state


CI = {"evidence_id": "ci-9-555-1", "kind": "ci", "summary": "free text {{ site }}",
      "ci": {"repository": FORK, "run_id": 555, "head_sha": HEAD,
             "jobs": [{"job_id": 7001, "name": "<b>build</b>", "executed": True,
                       "conclusion": "failure"},
                      {"job_id": 7002, "name": "ok", "executed": True, "conclusion": "success"}]}}
OPERATION = {"operation_id": "op-0123456789abcdef01234567", "kind": "dev_pr_create",
             "target": {"repository": FORK}, "proposed": {"commit": HEAD},
             "external_ids": {"development_pr_run_id": "4500", "pr_number": 1}}
EVENTS = [
    event(0, "intake:accepted", "observation", "issue-5000", to={"phase": "queued"}),
    event(1, "development_pr_created", "observation", OPERATION["operation_id"],
          **{"from": {"phase": "provisioning"}, "to": {"phase": "development", "revision": 1},
             "revision_bumped": True}),
    event(2, "record_ci_failure", "observation", CI["evidence_id"],
          details={"verb": "record_ci_failure", "from_state": "development.waiting_ci",
                   "to_state": "development.fix_needed"}),
    event(3, "request_patch", "result", "44982902579",
          details={"verb": "request_patch", "from_state": "development.fix_needed",
                   "to_state": "development.agent_running", "note": "<script>alert(1)</script>"}),
    event(4, "record_head_change", "observation", "branch:b29a8f493740"),
    event(5, "verify_upstream_pr", "observation", "upstream-pr-77",
          details={"verb": "verify_upstream_pr", "url": "https://github.com/example/bravo/pull/9",
                   "pr_number": 9, "head": HEAD, "author": "@someone"}),
    event(6, "pause_on_processing_error", "tick", "4600", to={"paused": True},
          details={"run_id": "4600", "message": "@everyone {% raw %} <img src=x>"}),
    event(7, "submit_upstream", "command", "comment-321"),
]


class TestGenerateUpstreamingDoc(unittest.TestCase):
    def test_render_links_and_orders_tracking_rows(self):
        common = {
            "tracking_issue": {"number": 42},
            "phase": "development",
            "activity": "waiting_ci",
            "fork": {
                "name": "riseproject-upstreaming/bravo",
                "pr_number": 3,
            },
            "upstream_pr": {
                "number": 7,
                "url": "https://github.com/example/bravo/pull/7",
            },
            "updated_at": "2026-11-20T15:00:00+00:00",
        }
        bravo = {**common, "upstream": {"full_name": "example/bravo"}}
        alpha = {
            **common,
            "upstream": {"full_name": "example/Alpha"},
            "tracking_issue": {"number": 41},
            "fork": None,
            "upstream_pr": None,
        }

        rendered = generate_doc.render([bravo, alpha])

        self.assertLess(rendered.index("example/Alpha"), rendered.index("example/bravo"))
        self.assertIn("https://github.com/riseproject-dev/porting-agent/issues/42", rendered)
        self.assertIn("https://github.com/riseproject-upstreaming/bravo/pull/3", rendered)
        self.assertIn("https://github.com/example/bravo/pull/7", rendered)
        self.assertIn("Development (waiting for observation or a remote operation)", rendered)
        self.assertEqual(rendered.count("<tr class=\"event-log-row\">"), 2)

    def test_paused_state_is_primary_status_with_the_label_of_its_state(self):
        self.assertEqual(generate_doc.status_label({"phase": "parked", "activity": "idle",
                                                    "paused": True}),
                         "Paused (Parked)")
        self.assertEqual(generate_doc.status_label({"phase": "development", "activity": "waiting_ci",
                                                    "paused": True}),
                         "Paused (Development (waiting for observation or a remote operation))")

    def test_submission_gate_names_the_human_step(self):
        self.assertEqual(
            generate_doc.status_label({"phase": "handoff", "activity": "awaiting_submission",
                                       "pending_human_action": {"kind": "submit_upstream"}}),
            "Ready for upstream (waiting for a human: submit upstream)")

    def test_every_state_of_the_table_has_a_label_from_its_kind(self):
        from tests.helpers import contribution
        self.assertEqual(set(generate_doc.PHASE_LABELS), set(lifecycle.PHASES))
        for key, spec in lifecycle.LIFECYCLE.items():
            phase, activity = key.split(".")
            label = generate_doc.status_label(contribution(phase=phase, activity=activity))
            with self.subTest(state=key):
                self.assertTrue(label.startswith(generate_doc.PHASE_LABELS[phase]))
                if spec.kind in lifecycle.REMINDER_KINDS:
                    self.assertIn("waiting for a human", label)
                elif spec.kind in ("work", "auto_wait"):
                    self.assertRegex(label, r"\((next: [a-z_]+.*|waiting (for|until) .+)\)$")
                self.assertNotIn("capacity wait", label)

    def test_work_that_is_due_names_the_guard_it_waits_on(self):
        from tests.helpers import contribution
        label = generate_doc.status_label(contribution(activity="fix_needed"))
        self.assertEqual(label, "Development (next: request_patch, once worker slot free; "
                                "capacity available)")
        exhausted = contribution(activity="fix_needed")
        exhausted["budget"]["attempts_used"] = 99
        self.assertEqual(generate_doc.status_label(exhausted),
                         "Development (next: exhaust_work_total)")

    def test_closed_names_its_terminal_reason(self):
        for reason, label in (("abandoned", "Closed (abandoned)"),
                              ("withdrawn", "Closed (request withdrawn)")):
            with self.subTest(reason=reason):
                self.assertEqual(generate_doc.status_label(
                    {"phase": "closed", "activity": "idle", "terminal_reason": reason,
                     "paused": True}), label)
        self.assertEqual(generate_doc.status_label({"phase": "closed", "activity": "idle"}),
                         "Closed")


class TestEventLog(unittest.TestCase):
    def render(self, rec=None, events=EVENTS, root=None):
        rec = rec or record()
        with tempfile.TemporaryDirectory() as directory:
            state = write_state(Path(directory), rec, events, evidence=[CI], operations=[OPERATION])
            return generate_doc.render([rec], state)

    def log(self, page):
        return page[page.index('<details class="event-log">'):page.index("</details>")]

    def rows(self, log):
        return re.findall(r'<tr class="event-table__event[^"]*".*?</tr>', log)

    def test_each_row_has_a_closed_disclosure_with_a_chevron(self):
        page = self.render()
        self.assertEqual(page.count('<details class="event-log">'), 1)
        self.assertNotIn("<details open", page)
        self.assertIn('<summary><svg class="event-log__chevron"', page)
        self.assertIn("(8 events, 5 state changes)", page)
        self.assertRegex(page, r'<tr class="event-log-row"><td colspan="6">')

    def test_the_log_is_a_table_with_accessible_headers_and_labels(self):
        log = self.log(self.render())
        self.assertIn("<caption class=\"sr-only\">", log)
        for header in generate_doc.EVENT_HEADERS:
            self.assertIn(f'<th scope="col">{header}</th>', log)
        for row in self.rows(log):
            self.assertEqual(row.count("<td "), 6)
            self.assertEqual(len(re.findall(r'<td data-label="[^"]+">', row)), 6)
        self.assertIn("State changes only", log)
        self.assertIn('class="event-log__path"', log)

    def test_events_are_listed_newest_first_with_time_states_and_revision(self):
        log = self.log(self.render())
        sequences = [int(n) for n in re.findall(r'event-table__seq">#(\d+)<', log)]
        self.assertEqual(sequences, list(range(7, -1, -1)))
        self.assertIn('<time datetime="2026-11-22T01:02:03+00:00">2026-11-22 01:02:03</time>', log)
        fail = next(row for row in self.rows(log) if "record_ci_failure" in row)
        self.assertIn('class="state state--auto_wait"', fail)
        self.assertIn('href="../state-machine/table/#state-development-waiting_ci"', fail)
        self.assertIn('href="../state-machine/table/#state-development-fix_needed"', fail)
        self.assertIn("&rarr; <a href=\"../state-machine/table/#state-development-waiting_ci\">"
                      "<code>record_ci_failure</code></a> &rarr;", fail)
        bump = next(row for row in self.rows(log) if "development_pr_created" in row)
        self.assertIn("<strong>r0 &rarr; r1</strong>", bump)
        self.assertIn('<span class="event-log__flag">paused</span>', log)

    def test_every_state_of_the_table_has_a_badge_of_its_kind_and_an_anchor(self):
        table = (Path(generate_doc.ROOT) / "docs/state-machine/table.md").read_text()
        for key, spec in lifecycle.LIFECYCLE.items():
            badge = generate_doc.state_badge(key)
            with self.subTest(state=key):
                self.assertIn(f"state--{spec.kind}", badge)
                self.assertIn("(", badge.split('class="sr-only">')[1])
                anchor = generate_doc.state_anchor(key)
                self.assertIn(f'href="../state-machine/table/#{anchor}"', badge)
                self.assertIn(f"{{: #{anchor} }}", table)
        unknown = generate_doc.state_badge("nowhere.idle")
        self.assertIn("state--unknown", unknown)
        self.assertNotIn("href=", unknown)
        self.assertIn("state--derived", generate_doc.state_badge("queued.idle", derived=True))

    def test_a_historic_name_is_shown_under_its_current_name_and_recorded_as(self):
        events = [event(0, "park_investigation", "result", "44982902579",
                        details={"verb": "park_investigation",
                                 "from_state": "investigation.agent_running",
                                 "to_state": "parked.idle"})]
        log = self.log(self.render(events=events))
        self.assertIn("<code>park_planning</code>", log)
        self.assertIn("planning.agent_running", log)
        self.assertIn("#state-planning-agent_running", log)
        self.assertIn("(recorded as <code>park_investigation</code>, "
                      "<code>investigation.agent_running</code>)", log)
        self.assertNotIn("<code>park_investigation</code></a>", log)

    def test_summary_is_the_first_sentence_of_the_row_description(self):
        edge = next(e for e in lifecycle.table_edges()
                    if e.transition.description and e.target != e.source
                    and e.verb not in lifecycle.RULES and "`" in e.transition.description)
        first = generate_doc._first_sentence(edge.transition.description)
        events = [event(0, edge.verb, "result", "1",
                        details={"verb": edge.verb, "from_state": edge.source,
                                 "to_state": edge.target})]
        log = self.log(self.render(events=events))
        self.assertIn(f'<div class="transition__summary">{generate_doc.escape_cell(first)}</div>', log)
        self.assertNotIn("`", log)

    def test_an_empty_description_falls_back_to_the_destination_state(self):
        edge = next(e for e in lifecycle.table_edges()
                    if not e.transition.description and e.target in lifecycle.LIFECYCLE
                    and e.target != e.source and e.verb not in lifecycle.RULES)
        text = generate_doc.event_summary(edge.verb, edge.source, edge.target, edge.verb)
        self.assertEqual(text, generate_doc._first_sentence(
            lifecycle.LIFECYCLE[edge.target].description))
        self.assertEqual(generate_doc.event_summary("nothing", None, None, "nothing"), "")

    def test_minor_and_bookkeeping_rows_are_muted_and_self_transitions_say_so(self):
        events = [
            event(0, "retry_dispatch", "tick", "dispatcher-5",
                  details={"verb": "retry_dispatch", "from_state": "planning.agent_running",
                           "to_state": "planning.agent_running"}),
            event(1, "record_fields_migrated", "migration", "strict-record-schema",
                  details={"from_state": "planning.agent_running",
                           "to_state": "planning.agent_running"}),
            event(2, "park_planning", "result", "9",
                  details={"verb": "park_planning", "from_state": "planning.agent_running",
                           "to_state": "parked.idle"}),
        ]
        log = self.log(self.render(events=events))
        rows = {int(re.search(r"#(\d+)<", row)[1]): row for row in self.rows(log)}
        self.assertIn("event-table__event--minor", rows[0])
        self.assertIn("&#8635;", rows[0])
        self.assertIn("stays in this state", rows[0])
        self.assertIn("event-table__event--bookkeeping", rows[1])
        self.assertIn("bookkeeping</span>", rows[1])
        self.assertNotIn("--minor", rows[2])
        self.assertNotIn("--bookkeeping", rows[2])
        self.assertIn("(3 events, 1 state change)", log)

    def test_identical_events_of_one_run_are_folded_listing_every_sequence(self):
        late = dict(details={"verb": "record_late_effect", "from_state": "handoff.awaiting_submission",
                             "to_state": "handoff.awaiting_submission"})
        same = [event(n, "record_late_effect", "result", "4400", at="2026-11-23T13:28:18+00:00",
                      run={"id": 77, "attempt": 1}, **late) for n in (0, 1, 2)]
        log = self.log(self.render(events=same))
        self.assertEqual(len(self.rows(log)), 1)
        self.assertIn("&times;3", log)
        self.assertIn("#2, #1, #0", log)
        # Another run, another cause or another state is not folded.
        other_run = [dict(e, run={"id": 78, "attempt": 1}) if e["sequence"] == 1 else e
                     for e in same]
        self.assertEqual(len(self.rows(self.log(self.render(events=other_run)))), 3)
        other_cause = [dict(e, cause={"type": "result", "id": "4401"}) if e["sequence"] == 1 else e
                       for e in same]
        self.assertEqual(len(self.rows(self.log(self.render(events=other_cause)))), 3)
        # Without a recorded run the exact time of the tick tells events apart.
        no_run = [{k: v for k, v in e.items() if k != "run"} for e in same]
        self.assertEqual(len(self.rows(self.log(self.render(events=no_run)))), 1)
        later = [dict(e, at="2026-11-23T13:30:00+00:00") if e["sequence"] == 2 else e
                 for e in no_run]
        self.assertEqual(len(self.rows(self.log(self.render(events=later)))), 2)

    def test_rows_of_one_run_are_grouped(self):
        events = [event(n, kind, "tick", "dispatcher-1", run={"id": 5, "attempt": 1})
                  for n, kind in enumerate(("a_kind", "b_kind"))]
        log = self.log(self.render(events=events))
        rows = self.rows(log)
        self.assertNotIn("--same-run", rows[0])
        self.assertIn("event-table__event--same-run", rows[1])

    def test_cause_labels_name_what_the_cause_is(self):
        log = self.log(self.render())
        self.assertIn(">worker run 44982902579<", log)
        self.assertIn(">comment 321<", log)
        self.assertIn(">controller run 4600<", log)
        self.assertIn('<span class="cause__type">worker Result</span>', log)
        dispatcher = self.log(self.render(events=[
            event(0, "retry_dispatch", "tick", "dispatcher-4500")]))
        self.assertIn(">dispatcher run 4500<", dispatcher)

    def test_the_controller_run_comes_from_the_event_then_its_details(self):
        control = "https://github.com/riseproject-dev/porting-agent/actions/runs"
        recorded = event(0, "a_kind", "tick", None, run={"id": 900, "attempt": 3},
                         details={"run_id": "901"})
        details = event(1, "b_kind", "tick", None, details={"run_id": "902"})
        none = event(2, "c_kind", "tick", None)
        log = self.log(self.render(events=[recorded, details, none]))
        rows = {int(re.search(r"#(\d+)<", row)[1]): row for row in self.rows(log)}
        self.assertIn(f'<a href="{control}/900/attempts/3">run 900 (attempt 3)</a>', rows[0])
        self.assertIn(f'<a href="{control}/902">run 902</a>', rows[1])
        self.assertIn('data-label="Controller run">-</td>', rows[2])

    def test_the_controller_run_of_an_older_event_comes_from_its_state_commit(self):
        rec = record()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = write_state(root, rec, [], evidence=[CI], operations=[OPERATION])
            events_dir = state / "events" / "example--bravo--1"

            def commit(subject, author, sequence):
                (events_dir / f"{sequence:08d}.json").write_text(json.dumps(
                    event(sequence, "a_kind", "tick", None)))
                git(root, "add", "-A")
                git(root, "-c", f"user.name={author}", "-c", "user.email=a@b.c",
                    "commit", "-q", "-m", subject)
                return git(root, "rev-parse", "HEAD").strip()

            git(root, "init", "-q")
            ok = commit("porting-agent: reconcile state (run 123)", "porting[bot]", 0)
            commit("porting-agent: reconcile state (run 124)", "Someone", 1)
            commit("another subject (run 125)", "porting[bot]", 2)
            commit("porting-agent: confirm canary (run 126)", "porting[bot]", 3)
            commits = generate_doc.state_commits(state)
            self.assertEqual(commits["events/example--bravo--1/00000000.json"], (ok, 123))
            self.assertEqual(commits["events/example--bravo--1/00000001.json"][1], None)
            self.assertEqual(commits["events/example--bravo--1/00000002.json"][1], None)
            self.assertEqual(commits["events/example--bravo--1/00000003.json"][1], 126)
            log = self.log(generate_doc.render([rec], state))
            rows = {int(re.search(r"#(\d+)<", row)[1]): row for row in self.rows(log)}
            self.assertIn("actions/runs/123\">run 123</a>", rows[0])
            self.assertIn(f"commit/{ok}\">state commit {ok[:7]}</a>", rows[0])
            self.assertIn('data-label="Controller run">', rows[1])
            self.assertNotIn("run 124", rows[1])
            self.assertIn("state commit", rows[1])
            # An event that records its own run keeps it.
            (events_dir / "00000000.json").write_text(json.dumps(
                event(0, "a_kind", "tick", None, run={"id": 5, "attempt": 1})))
            log = self.log(generate_doc.render([rec], state))
            self.assertIn("actions/runs/5/attempts/1", log)

    def test_missing_or_shallow_history_never_fails_the_page(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(generate_doc.state_commits(Path(directory) / "missing"), {})
            self.assertEqual(generate_doc.state_commits(Path(directory)), {})
            self.assertEqual(generate_doc.state_commits(None), {})
        log = self.log(self.render())
        self.assertIn('data-label="Controller run">-</td>', log)

    def test_an_event_that_ends_a_paused_contribution_is_not_shown_as_a_resume(self):
        rec = record(phase="closed", activity="idle", terminal_reason="abandoned")
        events = [
            event(0, "pause", "command", "c-1", to={"paused": True}),
            event(1, "abandon", "command", "c-2",
                  **{"from": {"phase": "parked", "paused": True},
                     "to": {"phase": "closed", "paused": False}}),
            event(2, "resume_pause", "command", "c-3",
                  **{"from": {"paused": True}, "to": {"paused": False}}),
        ]
        log = self.log(self.render(rec, events))
        self.assertEqual(log.count('<span class="event-log__flag">resumed</span>'), 1)
        abandon = next(row for row in self.rows(log) if "<code>abandon</code>" in row)
        self.assertNotIn("event-log__flag", abandon)

    def test_states_of_older_events_are_marked_derived(self):
        log = self.log(self.render())
        old = next(row for row in self.rows(log) if "development_pr_created" in row)
        self.assertIn("state--derived", old)
        self.assertIn(">derived</span>", old)
        named = next(row for row in self.rows(log) if "record_ci_failure" in row)
        self.assertNotIn("derived", named)

    def test_links_resolve_every_reference_from_committed_data(self):
        log = self.log(self.render())
        control = "https://github.com/riseproject-dev/porting-agent"
        for url in (
                f"{control}/issues/42",
                f"{control}/blob/main/.state/operations/{OPERATION['operation_id']}.json",
                f"{control}/actions/runs/4500",
                f"https://github.com/{FORK}/pull/1",
                f"https://github.com/{FORK}/commit/{HEAD}",
                f"{control}/blob/main/.state/evidence/ci-9-555-1.json",
                f"https://github.com/{FORK}/actions/runs/555",
                f"https://github.com/{FORK}/actions/runs/555/job/7001",
                f"{control}/actions/runs/44982902579",
                "https://github.com/example/bravo/pull/9",
                f"{control}/actions/runs/4600",
                f"{control}/issues/42#issuecomment-321"):
            with self.subTest(url=url):
                self.assertIn(f'href="{url}"', log)
        # A passing job is not linked as a failure.
        self.assertNotIn("/job/7002", log)

    def test_no_free_text_and_everything_is_escaped(self):
        page = self.render()
        page = page[page.index('<table class="status-table">'):]
        for text in ("<script>", "alert", "@everyone", "@someone", "{%", "{{", "<img", "<b>build",
                     "free text", "raw"):
            with self.subTest(text=text):
                self.assertNotIn(text, page)
        hostile = record(upstream={"full_name": "a/b<i>{{x}}"})
        page = self.render(hostile, [event(0, "<b>{{kind}}</b>", "observation", "x\"><y")])
        self.assertNotIn("<b>", page)
        self.assertNotIn("{{", page[page.index('<table class="status-table">'):])
        self.assertIn("&lt;b&gt;&#123;&#123;kind&#125;&#125;&lt;/b&gt;", page)
        self.assertNotIn('href="https://github.com/a/b', page)

    def test_unresolved_references_are_not_linked(self):
        events = [event(0, "record_ci_failure", "observation", "ci-missing-1"),
                  event(1, "verify_upstream_pr", "observation", "upstream-pr-1",
                        details={"verb": "verify_upstream_pr", "url": "javascript:alert(1)"})]
        log = self.log(self.render(events=events))
        self.assertNotIn("ci-missing-1.json", log)
        self.assertNotIn("javascript", log)

    def test_returning_for_feedback_lists_its_states_and_evidence_only(self):
        events = [event(0, "return_for_feedback", "observation", CI["evidence_id"],
                        details={"verb": "return_for_feedback",
                                 "from_state": "development.waiting_ci",
                                 "to_state": "development.fix_needed",
                                 "evidence_id": CI["evidence_id"], "head": HEAD,
                                 "feedback": ["review-<b>1</b>"]})]
        log = self.log(self.render(events=events))
        self.assertIn("<code>return_for_feedback</code>", log)
        self.assertIn("#state-development-waiting_ci", log)
        self.assertIn("#state-development-fix_needed", log)
        self.assertIn(f'href="https://github.com/riseproject-dev/porting-agent/blob/main/'
                      f'.state/evidence/{CI["evidence_id"]}.json"', log)
        self.assertNotIn("review-", log)

    def test_rendering_is_deterministic(self):
        first = self.render()
        self.assertEqual(first, self.render())
        self.assertEqual(first, self.render(events=list(reversed(EVENTS))))

    def test_a_contribution_without_events_says_so(self):
        page = self.render(events=[])
        self.assertIn("(0 events, 0 state changes)", page)
        self.assertIn("No events are recorded.", page)

    def test_main_reads_the_state_directory_beside_the_records(self):
        rec = record()
        with tempfile.TemporaryDirectory() as directory:
            state = write_state(Path(directory), rec, EVENTS, evidence=[CI], operations=[OPERATION])
            out = Path(directory) / "out" / "index.md"
            import sys
            from unittest import mock
            argv = ["generate_doc", "--state-dir", str(state / "contributions"), "--output", str(out)]
            with mock.patch.object(sys, "argv", argv):
                generate_doc.main()
            first = out.read_bytes()
            with mock.patch.object(sys, "argv", argv):
                generate_doc.main()
            self.assertEqual(out.read_bytes(), first)
            self.assertIn(b"(8 events, 5 state changes)", first)
            self.assertNotIn("\u2014".encode(), first)


if __name__ == "__main__":
    unittest.main()
