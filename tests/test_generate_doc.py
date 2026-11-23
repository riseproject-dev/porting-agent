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
        self.assertIn("Development (waiting for observation)", rendered)
        self.assertEqual(rendered.count("<tr class=\"event-log-row\">"), 2)

    def test_paused_state_is_primary_status_with_the_label_of_its_state(self):
        self.assertEqual(generate_doc.status_label({"phase": "parked", "activity": "idle",
                                                    "paused": True}),
                         "Paused (Parked)")
        self.assertEqual(generate_doc.status_label({"phase": "development", "activity": "waiting_ci",
                                                    "paused": True}),
                         "Paused (Development (waiting for observation))")

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
    def render(self, rec=None, events=EVENTS):
        rec = rec or record()
        with tempfile.TemporaryDirectory() as directory:
            state = write_state(Path(directory), rec, events, evidence=[CI], operations=[OPERATION])
            return generate_doc.render([rec], state)

    def log(self, page):
        return page[page.index('<details class="event-log">'):page.index("</details>")]

    def test_each_row_has_a_closed_disclosure_with_a_chevron(self):
        page = self.render()
        self.assertEqual(page.count('<details class="event-log">'), 1)
        self.assertNotIn("<details open", page)
        self.assertIn('<summary><svg class="event-log__chevron"', page)
        self.assertIn("(8 events)", page)
        self.assertRegex(page, r'<tr class="event-log-row"><td colspan="6">')
        # The project row is unchanged: one <tr> before the log row.
        body = page[page.index("<tbody>"):page.index("</tbody>")]
        self.assertEqual(body.count("<tr"), 2)

    def test_events_are_listed_newest_first_with_time_kind_states_and_revision(self):
        log = self.log(self.render())
        sequences = [int(n) for n in re.findall(r'event-log__seq">#(\d+)<', log)]
        self.assertEqual(sequences, list(range(7, -1, -1)))
        self.assertIn('<time datetime="2026-11-22T01:02:03+00:00">2026-11-22 01:02:03</time>', log)
        self.assertIn('<code class="event-log__kind">record_ci_failure</code>', log)
        self.assertIn("<code>development.waiting_ci</code> &rarr; <code>development.fix_needed</code>",
                      log)
        self.assertIn("<code>provisioning.idle</code> &rarr; <code>development.idle</code> "
                      '<span class="event-log__revision">r0 &rarr; r1</span>', log)
        self.assertIn('<span class="event-log__flag">paused</span>', log)

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
        for text in ("<script>", "alert", "@everyone", "@someone", "{%", "{{", "<img", "<b>build",
                     "free text", "raw"):
            with self.subTest(text=text):
                self.assertNotIn(text, page)
        hostile = record(upstream={"full_name": "a/b<i>{{x}}"})
        page = self.render(hostile, [event(0, "<b>{{kind}}</b>", "observation", "x\"><y")])
        self.assertNotIn("<b>", page)
        self.assertNotIn("{{", page)
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
        self.assertIn('<code class="event-log__kind">return_for_feedback</code>', log)
        self.assertIn("<code>development.waiting_ci</code> &rarr; "
                      "<code>development.fix_needed</code>", log)
        self.assertIn(f'href="https://github.com/riseproject-dev/porting-agent/blob/main/'
                      f'.state/evidence/{CI["evidence_id"]}.json"', log)
        self.assertNotIn("review-", log)

    def test_rendering_is_deterministic(self):
        first = self.render()
        self.assertEqual(first, self.render())
        self.assertEqual(first, self.render(events=list(reversed(EVENTS))))

    def test_a_contribution_without_events_says_so(self):
        page = self.render(events=[])
        self.assertIn("(0 events)", page)
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
            self.assertIn(b"(8 events)", first)
            self.assertNotIn("\u2014".encode(), first)


if __name__ == "__main__":
    unittest.main()
