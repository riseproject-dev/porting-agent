# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Lifecycle issue ledger entries are concise, append-only and App-authored."""
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lifecycle
import status_ledger
from tests.helpers import NOW, SHA_A, contribution


class FakeApi:
    def __init__(self):
        self.calls = []
        self.comment = None

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("?per_page=100"):
            return []
        if method == "POST":
            self.comment = {"id": 7, "body": body["body"], "html_url": "https://github.com/x",
                            "user": {"login": "riseproject-dev[bot]"}}
        return self.comment


class TestStatusLedger(unittest.TestCase):
    def records(self, root: Path):
        old = contribution(phase="queued", activity="idle", revision=0)
        new = dict(old, phase="parked", activity="idle", revision=1, parked_reason="No patch", updated_at=NOW)
        event = lifecycle._event(old, new, "dossier_parked", "observation", "dossier-1", NOW, True)
        event["sequence"] = 3
        cpath = root / ".state/contributions/example--proj--12.json"
        epath = root / ".state/events/example--proj--12/00000003.json"
        cpath.parent.mkdir(parents=True); epath.parent.mkdir(parents=True)
        cpath.write_text(json.dumps(new)); epath.write_text(json.dumps(event))
        return new, event, str(epath.relative_to(root))

    def test_render_has_short_summary_and_folded_prefilled_commands(self):
        record = contribution(phase="parked", activity="idle", revision=4, parked_reason="No patch")
        event = lifecycle._event(record, record, "dossier_parked", "observation", "d", NOW, False)
        event["sequence"] = 2
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertTrue(body.startswith("**Upstreaming r4 · parked/idle**"))
        self.assertIn("<details>", body)
        self.assertIn("<summary>Possible next commands</summary>", body)
        self.assertIn("/upstreaming resume revision=4 class=configuration_integration", body)
        self.assertNotIn("force-develop", body)
        self.assertIn("<!-- upstreaming-ledger:", body)

    def test_gate_review_rows_render_what_they_did(self):
        import engine
        from tests.helpers import awaiting_submission, policy
        gate = awaiting_submission()
        explained = engine.apply_transition(
            gate, "record_explanation", policy=policy(), now=NOW, cause_id="inquiry-1",
            facts={"explanation_inquiry"},
            values={"processed_review_ids": [], "processed_review_comment_ids": [],
                    "processed_pr_conversation_comment_ids": [88]},
            details={"comments": [{"url": "https://github.com/o/r/pull/1#issuecomment-88",
                                   "body": "Why `riscv64` only? <!-- x --> @someone [l](u)"}]})
        body = status_ledger.render(dict(explained.event, sequence=24), explained.record,
                                    "https://github.com/run")
        self.assertTrue(body.startswith("**Upstreaming r0 · handoff/awaiting_submission**"))
        self.assertIn("`record_explanation` by observation: authorized explanation-only inquiry.",
                      body)
        self.assertIn("The contribution stays in `handoff.awaiting_submission`", body)
        # The comment is quoted as a code span: nothing in it renders.
        self.assertIn("> `Why 'riscv64' only?  x  @someone [l](u)`", body)
        self.assertIn("([comment](https://github.com/o/r/pull/1#issuecomment-88))", body)
        self.assertEqual(body.count("<!--"), 1)   # only the ledger marker
        self.assertIn("/upstreaming submit-upstream revision=0", body)
        reviewed = engine.apply_transition(
            gate, "record_actionable_review", policy=policy(), now=NOW, cause_id="review-1",
            facts={"actionable_review", "candidate_head_not_failed"},
            values={"pending_feedback_evidence_ids": ["review-1"]})
        body = status_ledger.render(dict(reviewed.event, sequence=25), reviewed.record,
                                    "https://github.com/run")
        self.assertTrue(body.startswith("**Upstreaming r1 · development/fix_needed**"))
        self.assertIn("`record_actionable_review` by observation: authorized actionable review "
                      "feedback", body)
        self.assertIn("Now `development.fix_needed`: "
                      + lifecycle.LIFECYCLE["development.fix_needed"].description, body)
        self.assertNotIn(chr(0x2014), body)

    def test_development_pr_ledger_offers_dossier_refresh(self):
        record = contribution(phase="development", activity="fix_needed", revision=7)
        event = lifecycle._event(record, record, "activity", "observation", None, NOW, False)
        event["sequence"] = 8
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn('/upstreaming refresh revision=7 reason="..."', body)

    def test_closed_ledger_offers_no_command(self):
        import engine
        from tests.helpers import policy
        parked = contribution(phase="parked", activity="idle", revision=8, parked_reason="x",
                              previous_phase="development")
        out = engine.apply_transition(parked, "abandon", policy=policy(), now=NOW, cause_id="c",
                                      command=__import__("commands").parse(
                                          '/upstreaming abandon revision=8 reason="done"'))
        self.assertTrue(out.accepted, out.reason)
        body = status_ledger.render(dict(out.event, sequence=9), out.record,
                                    "https://github.com/run")
        self.assertIn("r8 · closed/idle", body)
        self.assertIn("`abandon` by the command `/upstreaming abandon`", body)
        self.assertIn("Now `closed.idle`: " + lifecycle.LIFECYCLE["closed.idle"].description, body)
        self.assertNotIn("Possible next commands", body)
        self.assertNotIn("/upstreaming abandon revision", body)

    def test_ledger_never_offers_a_retired_command(self):
        """In every admitted state, paused or not, the offered commands are
        commands of the grammar, never retired, and each parses."""
        import commands
        import engine
        states = sorted(set(lifecycle.LIFECYCLE))
        for state, paused in itertools.product(states, (False, True)):
            phase, activity = state.split(".")
            record = contribution(phase=phase, activity=activity, revision=3,
                                  clean_head_sha=SHA_A, paused=paused,
                                  handoff_request={"command_id": "x", "head": SHA_A,
                                                   "revision": 3})
            with self.subTest(state=state, paused=paused):
                for line in status_ledger._command_lines(record):
                    verb = line.split()[1]
                    self.assertNotIn(verb, commands.RETIRED)
                    self.assertIn(verb, commands.GRAMMAR)
                    parsed = commands.parse(line.replace("...", "x"))
                    if verb not in ("pause", "park", "resume") or state in lifecycle.LIFECYCLE:
                        # Every other offer is a command the table takes here.
                        self.assertTrue(engine.command_verbs(record, parsed), line)
                if state == "closed.idle" and not paused:
                    self.assertEqual(status_ledger._command_lines(record), [])

    def test_an_admission_override_is_shown_as_data(self):
        parked = contribution(phase="parked", activity="idle", revision=8, parked_reason="x")
        record = dict(parked, phase="queued", revision=9, admission_override={
            "class": "optimized_backend", "paths": ["setup.py"], "reason": "r"})
        event = lifecycle._event(parked, record, "restart_generation", "command", "c", NOW, True)
        event["sequence"] = 9
        event["details"] = {"verb": "restart_generation", "from_state": "parked.idle",
                            "to_state": "queued.idle",
                            "admission_override": record["admission_override"]}
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn("`restart_generation` by the command `/upstreaming resume`", body)
        self.assertIn("Admission override: class `optimized_backend`, paths `setup.py`.", body)
        closed = contribution(phase="closed", activity="idle", terminal_reason="no_work")
        before = dict(closed, phase="investigation", activity="agent_running")
        event = lifecycle._event(before, closed, "close_investigation", "result", "1", NOW, False)
        event["sequence"] = 11
        event["details"] = {"verb": "close_investigation", "from_state": "investigation.agent_running",
                            "to_state": "closed.idle", "admission_override_not_applied": True}
        body = status_ledger.render(event, closed, "https://github.com/run")
        self.assertIn("`close_investigation` by the Result of requested Work: verified dossier "
                      "decision close; PyPI serves riscv64", body)
        self.assertIn("The recorded admission override was not applied", body)

    def test_a_close_shows_the_pypi_finding_quoted(self):
        closed = contribution(phase="closed", activity="idle", terminal_reason="already_released")
        before = dict(closed, phase="investigation", activity="agent_running")
        event = lifecycle._event(before, closed, "close_investigation", "result", "1", NOW, False)
        event["sequence"] = 11
        wheel = "proj-2.4.1-cp312-cp312-manylinux_2_39_riscv64.whl"
        event["details"] = {"verb": "close_investigation", "from_state": "investigation.agent_running",
                            "to_state": "closed.idle", "rationale": "`ignore` <!-- x -->",
                            "pypi": {"proj": {"version": "2.4.1", "served_by": "riscv64",
                                              "wheel": wheel},
                                     "proj-data": {"version": "1.0", "served_by": "pure",
                                                   "wheel": "proj_data-1.0-py3-none-any.whl"}}}
        body = status_ledger.render(event, closed, "https://github.com/run")
        self.assertIn(f"PyPI: `proj 2.4.1` (riscv64 wheel `{wheel}`); "
                      "`proj-data 1.0` (pure-Python wheels only).", body)
        # The dossier's rationale is not rendered as trusted text.
        self.assertNotIn("ignore", body)
        # A hostile value in the finding is quoted and bounded.
        event["details"]["pypi"] = {"proj": {"version": "`x` <!--", "served_by": "riscv64",
                                             "wheel": "w" * 500 + "`\n# heading"}}
        body = status_ledger.render(event, closed, "https://github.com/run")
        line = body[body.index("PyPI: "):].split("\n")[0]
        self.assertNotIn("<!--", line)
        self.assertEqual(line.count("`"), 4)
        self.assertNotIn("w" * 201, line)


    def test_publish_appends_one_app_authored_comment_for_written_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); _record, _event, path = self.records(root); api = FakeApi()
            with patch("status_ledger.Api", return_value=api):
                receipts = status_ledger.publish(root, {"written": [path]}, "token", "https://github.com/run")
            self.assertEqual(receipts[0]["status"], "created")
            self.assertEqual([call[0] for call in api.calls], ["GET", "POST", "GET"])
            self.assertIn("Upstreaming r1", api.comment["body"])

    def test_next_tick_backfills_event_after_post_commit_projection_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.records(root)
            api = FakeApi()
            with patch("status_ledger.Api", return_value=api):
                receipts = status_ledger.publish(
                    root, {"written": []}, "token", "https://github.com/run-2"
                )
        self.assertEqual(receipts[0]["status"], "created")
        self.assertIn("run-2", api.comment["body"])


class PostedApi(FakeApi):
    """An issue that already carries ``comments``."""

    def __init__(self, comments):
        super().__init__()
        self.posted = comments

    def request(self, method, path, body=None):
        if path.endswith("?per_page=100"):
            self.calls.append((method, path, body))
            return list(self.posted)
        return super().request(method, path, body)


def table_event(verb, source, target, *, sequence=1, bumped=False, to=None):
    return {"schema_version": 1, "contribution_id": "example--proj/12", "sequence": sequence,
            "at": NOW, "kind": verb, "from": {}, "to": dict(to or {}),
            "cause": {"type": "tick", "id": None}, "revision_bumped": bumped,
            "details": {"verb": verb, "from_state": source, "to_state": target}}


class TestOnlyHumanStepsReachTheIssue(unittest.TestCase):
    """The tracking issue receives the events that need a human; the
    lifecycle table decides which."""

    def notifies(self, event):
        (source, target), = status_ledger.event_states([event])
        return status_ledger.notifies(event, source, target)

    def test_every_table_edge_posts_by_the_kind_of_its_destination(self):
        human = set(lifecycle.REMINDER_KINDS)
        for edge in lifecycle.table_edges():
            with self.subTest(edge=edge[:3]):
                kind = lifecycle.LIFECYCLE[edge.target].kind
                bumped = edge.transition.effects.revision == "bump"
                event = table_event(edge.verb, edge.source, edge.target, bumped=bumped)
                entered = edge.source != edge.target
                expected = (edge.verb in status_ledger.REMINDER_VERBS
                            or (kind in human and (entered or bumped))
                            or (kind in ("generation_end", "terminal") and entered))
                self.assertEqual(self.notifies(event), expected)

    def test_reminders_are_the_rule_of_the_human_kinds(self):
        self.assertEqual(status_ledger.REMINDER_VERBS, {"record_reminder"})
        self.assertEqual(set(lifecycle.rule_states("record_reminder")),
                         {k for k, s in lifecycle.LIFECYCLE.items()
                          if s.kind in status_ledger.HUMAN_KINDS})

    def test_every_pause_setter_posts(self):
        for verb in lifecycle.FLAGS["paused"].set_by:
            with self.subTest(verb=verb):
                event = table_event(verb, "development.waiting_ci", "development.waiting_ci",
                                    to={"paused": True})
                self.assertTrue(self.notifies(event))

    def test_routine_events_stay_off_the_issue(self):
        for verb, source, target in (
                ("record_late_effect", "handoff.awaiting_submission", "handoff.awaiting_submission"),
                ("record_explanation", "handoff.awaiting_submission", "handoff.awaiting_submission"),
                ("request_patch", "development.fix_needed", "development.agent_running"),
                ("record_publish_success", "development.agent_running", "development.waiting_ci"),
                ("request_ci_rerun", "development.agent_running", "development.waiting_ci")):
            with self.subTest(verb=verb):
                self.assertFalse(self.notifies(table_event(verb, source, target)))

    def test_closure_park_and_gate_post(self):
        for verb, source, target in (
                ("abandon", "parked.idle", "closed.idle"),
                ("park_contribution", "development.waiting_ci", "parked.idle"),
                ("nominate_for_handoff", "development.waiting_ci", "handoff.awaiting_submission"),
                ("exhaust_work_total", "development.fix_needed", "development.waiting_human")):
            with self.subTest(verb=verb):
                self.assertTrue(self.notifies(table_event(verb, source, target)))

    def test_resume_of_a_paused_human_wait_posts_its_new_revision(self):
        event = table_event("resume_pause", "development.waiting_human",
                            "development.waiting_human", bumped=True, to={"paused": False})
        self.assertTrue(self.notifies(event))

    def test_older_events_are_placed_by_folding_their_changed_fields(self):
        events = [
            {"sequence": 0, "kind": "intake:accepted", "from": {}, "to": {"phase": "queued"}},
            {"sequence": 1, "kind": "fork_provisioned", "from": {"phase": "provisioning"},
             "to": {"phase": "development"}},
            {"sequence": 2, "kind": "activity", "from": {"activity": "waiting_capacity"},
             "to": {"activity": "waiting_human"}},
            {"sequence": 3, "kind": "dossier_parked", "from": {}, "to": {"phase": "parked"}},
        ]
        states = status_ledger.event_states(events)
        self.assertEqual(states[2], ("development.waiting_capacity", "development.waiting_human"))
        self.assertEqual([status_ledger.notifies(e, *s) for e, s in zip(events, states)],
                         [False, False, True, True])

    def write(self, root, record, events):
        cid = record["contribution_id"].replace("/", "--")
        (root / ".state/contributions").mkdir(parents=True, exist_ok=True)
        (root / ".state/contributions" / f"{cid}.json").write_text(json.dumps(record))
        directory = root / ".state/events" / cid
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for event in events:
            path = directory / f"{event['sequence']:08d}.json"
            path.write_text(json.dumps(event))
            paths.append(str(path.relative_to(root)))
        return paths

    def test_publish_reports_status_page_only_events_without_any_request(self):
        record = contribution(phase="development", activity="agent_running", revision=3)
        event = table_event("request_patch", "development.fix_needed", "development.agent_running")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.write(root, record, [event])
            api = FakeApi()
            with patch("status_ledger.Api", return_value=api):
                for _tick in range(2):
                    receipts = status_ledger.publish(root, {"written": paths}, "token", "run")
                    self.assertEqual(receipts, [{"event": paths[0], "status": "status_page_only"}])
        self.assertEqual(api.calls, [])

    def test_an_already_posted_event_is_not_posted_again_and_a_reminder_is(self):
        import engine
        from tests.helpers import awaiting_submission, policy
        record = awaiting_submission(revision=6)
        gate = table_event("nominate_for_handoff", "development.waiting_ci",
                           "handoff.awaiting_submission", sequence=0, bumped=True)
        late = table_event("record_late_effect", "handoff.awaiting_submission",
                           "handoff.awaiting_submission", sequence=1)
        reminder = engine.apply_transition(record, "record_reminder", policy=policy(),
                                           now="2026-11-30T12:00:00+00:00", cause_id="reminder-6")
        self.assertTrue(reminder.accepted)
        reminder_event = dict(reminder.event, sequence=2)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.write(root, reminder.record, [gate, late, reminder_event])
            posted = {"id": 1, "user": {"login": "riseproject-dev[bot]"},
                      "body": "x <!-- upstreaming-ledger:example--proj/12:0 -->"}
            api = PostedApi([posted])
            with patch("status_ledger.Api", return_value=api):
                receipts = status_ledger.publish(root, {"written": paths}, "token", "run")
        self.assertEqual([r["status"] for r in receipts], ["existing", "status_page_only", "created"])
        self.assertEqual([c[0] for c in api.calls], ["GET", "POST", "GET"])
        self.assertIn("<!-- upstreaming-ledger:example--proj/12:2 -->", api.comment["body"])
        self.assertIn("`record_reminder` by the controller", api.comment["body"])


class TestWordingIsTheTable(unittest.TestCase):
    """Every entry of a table event is generated from the row it took and
    the state it led to; no verb has hand-written wording."""

    def test_every_row_renders_its_verb_guards_description_and_state(self):
        import engine
        for edge in lifecycle.table_edges():
            t = edge.transition
            phase, activity = edge.target.split(".")
            record = contribution(phase=phase, activity=activity, revision=2)
            event = table_event(edge.verb, edge.source, edge.target)
            with self.subTest(edge=edge[:3]):
                text = status_ledger.table_message(event, record)
                self.assertTrue(text.startswith(f"`{edge.verb}` "))
                for guard in t.guard:
                    if guard not in status_ledger.GENERIC_GUARDS:
                        self.assertIn(guard, text)
                if t.description:
                    self.assertIn(t.description.rstrip("."), text)
                self.assertIn(lifecycle.LIFECYCLE[edge.target].description.rstrip("."), text)
                for entry in t.effects.sets:
                    name, literal, value = entry.partition("=")
                    if name == "pending_human_action" and literal:
                        self.assertIn(f"Pending human action: `{value}`", text)
                if t.charges:
                    self.assertIn(", ".join(f"`{c}`" for c in t.charges), text)
                self.assertNotIn(chr(0x2014), text)
                self.assertTrue(engine.resolve(edge.source, edge.verb))

    def test_every_flag_verb_renders_from_the_table(self):
        for flag in lifecycle.FLAGS.values():
            for verb, t in (*flag.set_by.items(), *flag.cleared_by.items()):
                with self.subTest(verb=verb):
                    event = table_event(verb, "development.waiting_ci", "development.waiting_ci")
                    text = status_ledger.table_message(event, contribution())
                    self.assertTrue(text.startswith(f"`{verb}` "))
                    if t.description:
                        self.assertIn(t.description.rstrip("."), text)

    def test_only_events_outside_the_table_have_fixed_wording(self):
        import engine
        self.assertFalse(set(status_ledger.RECORDED_KINDS) & engine.table_verbs())
        self.assertFalse(hasattr(status_ledger, "MESSAGES"))

    def test_an_unknown_kind_is_named_quoted(self):
        record = contribution()
        event = table_event("some_kind`<b>", "x", "y")
        body = status_ledger.render(event, record, "https://github.com/run")
        self.assertIn("Recorded `some_kind'<b>`.", body)
