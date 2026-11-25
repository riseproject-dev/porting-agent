# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The review loop of a worker run.

The worker implements a patch, reviews it in its own run with the review loop
of ``pi-subagents`` (at most 3 rounds of fresh reviewers) and records the loop
in its report. Trusted code publishes the patch only when a round ran and no
P0 finding is open (``review_loop.check``, run by the worker gate), posts the
summary of the loop on the pushed commit once, and folds the published Result
from ``development.agent_running``, once the development PR of the pushed head
is recorded."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import capacity
import dev_pr
import engine
import fork_publish
import lifecycle as lc
import reconcile
import review_loop
import worker_gate
from tests import helpers
from tests.helpers import NOW, SHA_A, contribution, policy
from tests.test_worker_report import CID, HISTORY, report

POLICY = policy(read_only=False)
CANDIDATE = "c" * 40
CHANGED = [".github/workflows/wheels.yml"]


def finding(severity="P1", status="fixed", round=1, closed=2, **over):
    value = {"severity": severity, "status": status, "round": round, "closed_in_round": closed,
             "title": "The riscv64 job skips the tests", "detail": "The job lists no test step.",
             "resolution": "A test step was added."}
    value.update(over)
    return value


SERIES = "0" * 64


def loop(*findings, rounds=2, stopped="clean", digest=SERIES, **over):
    """A review loop whose every round reviewed the series with ``digest``."""
    value = {"rounds": rounds, "round_digests": [digest] * rounds, "stopped_because": stopped,
             "findings": list(findings),
             "summary": "Two rounds of fresh reviewers."}
    value.update(over)
    return value


class TestCheck(unittest.TestCase):
    def check(self, value, series=SERIES, **kw):
        return review_loop.check(report(review_loop=value), series_digest=series, **kw)

    def refused(self, value, pattern, **kw):
        with self.assertRaisesRegex(review_loop.LoopError, pattern):
            self.check(value, **kw)

    def test_a_delivered_series_needs_a_loop_with_a_round(self):
        self.assertEqual(self.check(loop())["rounds"], 2)
        with self.assertRaisesRegex(review_loop.LoopError, "records no review loop"):
            review_loop.check({"summary": "x"}, series_digest=SERIES)
        self.refused(loop(rounds=0, stopped="deadline"), "no review round ran")
        self.refused(loop(stopped="decision_needed"), "stopped for a decision")
        # A task that delivers no series needs none.
        self.assertIsNone(review_loop.check({}, series_digest=None, delivers_series=False))

    def test_a_p0_finding_that_is_open_or_deferred_blocks_publication(self):
        self.refused(loop(finding("P0", "open", 2, None)), "P0 finding open")
        self.refused(loop(finding("P0", "deferred", 1, None)), "P0 finding open")
        # Findings of lower severity never block, whatever their status.
        self.assertEqual(len(self.check(loop(finding("P1", "open", 2, None),
                                             finding("P2", "deferred", 1, None)))["findings"]), 2)

    def test_only_a_later_round_closes_a_p0_finding(self):
        self.assertEqual(len(self.check(loop(finding("P0", "fixed", 1, 2)))["findings"]), 1)
        self.assertEqual(len(self.check(loop(finding("P0", "withdrawn", 1, 2)))["findings"]), 1)
        self.refused(loop(finding("P0", "fixed", 1, 1)), "not by a later round")
        self.refused(loop(finding("P0", "withdrawn", 2, 2)), "not by a later round")

    def test_the_loop_must_not_contradict_itself(self):
        self.refused(loop(finding("P1", "open", 1, 2)), "open and closed in round 2")
        self.refused(loop(finding("P1", "fixed", 1, None)), "fixed in no round")
        self.refused(loop(finding("P1", "fixed", 1, 3)), "after the last round")
        self.refused(loop(finding("P1", "open", 3, None)), "after the last round")

    def test_the_last_round_must_have_reviewed_the_delivered_series(self):
        other = "1" * 64
        self.refused(loop(digest=other), "did not review the delivered series")
        # An earlier round may have reviewed an earlier series.
        value = loop(rounds=2)
        value["round_digests"] = [other, SERIES]
        self.assertEqual(self.check(value)["rounds"], 2)
        value["round_digests"] = [SERIES, other]
        self.refused(value, "did not review the delivered series")
        value["round_digests"] = [SERIES]
        self.refused(value, "names 1 reviewed series for 2 rounds")
        self.refused(loop(), "did not review the delivered series", series=None)

    def test_a_rewrite_text_must_be_the_text_the_last_round_read(self):
        text = "2" * 64
        value = loop(text_digest=text)
        self.assertEqual(review_loop.check(report(review_loop=value), series_digest=SERIES,
                                           text_digest=text)["rounds"], 2)
        for bad in (loop(text_digest="3" * 64), loop()):
            with self.assertRaisesRegex(review_loop.LoopError,
                                        "did not review the delivered title and description"):
                review_loop.check(report(review_loop=bad), series_digest=SERIES,
                                  text_digest=text)
        # A patch has no text: nothing is compared.
        self.assertEqual(self.check(loop())["rounds"], 2)

    def test_the_schema_bounds_the_loop(self):
        import schemas
        for bad in (loop(rounds=4), loop(stopped="because"), loop(digest="x"), loop(text_digest="x"), loop(finding(severity="blocker")),
                    loop(*[finding("P2", "deferred", 1, None)] * 11),
                    loop(finding(title="x" * 151)), loop(summary="x" * 1001)):
            with self.assertRaises(schemas.SchemaError):
                schemas.validate("worker_report", report(review_loop=bad))
        schemas.validate("worker_report", report(review_loop=loop(finding())))


SELF_REVIEW = ("The subagent tool failed to launch reviewer children (settings error), so the "
               "controller reviewed the series itself")
SETTINGS_ERROR = ("Builtin override 'reviewer' in '/tmp/gh-aw/pi-agent-dir/settings.json' has "
                  "invalid 'tools'; expected an array of strings, \"inherit\", or false.")
STAMP = "2026-11-25T09:38:58.1551821Z "


def event(kind, call, **fields):
    return STAMP + json.dumps(dict(type=kind, toolCallId=call, toolName="subagent", **fields),
                              separators=(",", ":"))


def failed_launch(call="toolu_01Rp6qwZAFpZgUB2EABMh9Vf"):
    """The calls of run 45577808226 (agent log lines 7278-7279), the task text cut."""
    return [event("tool_execution_start", call, args={"tasks": [
                {"agent": "reviewer", "cwd": "./fork", "task": "Review angle: CORRECTNESS"},
                {"agent": "reviewer", "cwd": "./fork", "task": "Review angle: TESTS"}]}),
            event("tool_execution_end", call,
                  result={"content": [{"type": "text", "text": SETTINGS_ERROR}], "details": {}},
                  isError=True, durationMs=3)]


def good_launch(call="toolu_ok", agents=("reviewer", "reviewer", "reviewer")):
    return [event("tool_execution_start", call,
                  args={"tasks": [{"agent": a, "task": "x"} for a in agents]}),
            event("tool_execution_end", call, result={"content": [
                {"type": "text", "text": "Verdict: OK with notes. P2: rename a helper."}],
                "details": {}}, isError=False, durationMs=91000)]


class TestUnavailable(unittest.TestCase):
    """A loop with no reviewer behind it is no review, judged by structured
    evidence: the report's own field and the tool events of the agent's log,
    never the worker's prose."""

    def check(self, value, log=None, **over):
        launches = None if log is None else review_loop.reviewer_launches("\n".join(log))
        return review_loop.check(report(review_loop=value, **over), series_digest=SERIES,
                                 launches=launches)

    def test_unavailable_reviewers_are_not_a_review(self):
        value = loop(rounds=0, stopped="reviewers_unavailable", summary="The subagent tool was missing.")
        value["round_digests"] = []
        with self.assertRaisesRegex(review_loop.ReviewersUnavailable, "reviewers were unavailable"):
            self.check(value)
        with self.assertRaises(review_loop.ReviewersUnavailable):
            self.check(loop(stopped="reviewers_unavailable"))

    def test_the_live_log_of_run_45577808226_bears_out_no_review(self):
        """Two `subagent` calls that returned the settings error, and a report
        that claimed one round: the Result is unreviewed, found in the log."""
        log = ["Extension error (/tmp/gh-aw/pi-agent-dir/npm/node_modules/pi-subagents/bundle/index.js): "
               + SETTINGS_ERROR] + failed_launch() + failed_launch("toolu_01PdkFDfrDyzPTbxmaKttwy4")
        self.assertEqual(review_loop.reviewer_launches("\n".join(log)), 0)
        with self.assertRaises(review_loop.ReviewersUnavailable) as raised:
            self.check(loop(rounds=1, summary=SELF_REVIEW, stopped="optional_only"), log)
        self.assertEqual(raised.exception.stage, "agent_log")
        self.assertIn("0 successful launches", str(raised.exception))

    def test_successful_launches_bear_out_the_rounds(self):
        self.assertEqual(self.check(loop(rounds=2), failed_launch() + good_launch("a")
                                    + good_launch("b", ("reviewer",)))["rounds"], 2)
        self.assertEqual(review_loop.reviewer_launches("\n".join(good_launch())), 3)
        # A single `agent` call counts too, a worker child does not.
        single = [event("tool_execution_start", "s", args={"agent": "reviewer", "task": "x"}),
                  event("tool_execution_end", "s", result={"content": []}, isError=False)]
        self.assertEqual(review_loop.reviewer_launches("\n".join(single)), 1)
        self.assertEqual(review_loop.reviewer_launches("\n".join(good_launch(agents=("worker",)))), 0)
        # Fewer launches than rounds is unreviewed; one launch per round is enough.
        with self.assertRaises(review_loop.ReviewersUnavailable):
            self.check(loop(rounds=3), good_launch("a", ("reviewer",)) + good_launch("b", ("reviewer",)))
        self.assertEqual(self.check(loop(rounds=3), good_launch("a", ("reviewer",)) * 0
                                    + good_launch("b") + good_launch("c", ("reviewer",))
                                    + good_launch("d", ("reviewer",)))["rounds"], 3)

    def test_an_error_text_without_the_flag_is_no_launch(self):
        log = [event("tool_execution_start", "t", args={"agent": "reviewer"}),
               event("tool_execution_end", "t", result={"content": [
                   {"type": "text", "text": "Error: unknown agent 'reviewer'"}]})]
        self.assertEqual(review_loop.reviewer_launches("\n".join(log)), 0)

    def test_an_absent_log_decides_nothing(self):
        self.assertEqual(self.check(loop(rounds=2))["rounds"], 2)
        self.assertIsNone(review_loop.log_launches(Path("/nonexistent/agent-stdio.log")))
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "agent-stdio.log"
            log.write_text("\n".join(good_launch()) + "\nnot json {\n{\"type\":\"tool_execution_end\"\n")
            self.assertEqual(review_loop.log_launches(log), 3)

    def test_prose_is_never_read(self):
        """Sentences that look like a self-review are only words: with the
        launches in the log, a legitimately reviewed patch is accepted."""
        log = good_launch()
        for prose in ("two reviewer subagents flagged a missing Signed-off-by",
                      "findings were reviewed by the controller", "Zbb is unavailable on old cores",
                      "The test itself reviewed nothing", SELF_REVIEW,
                      "Self-review of the error paths: none needed"):
            with self.subTest(prose=prose):
                self.assertEqual(self.check(loop(finding(detail=prose), summary=prose), log,
                                            summary=prose, open_items=[prose])["rounds"], 2)

    def test_it_is_a_loop_error_for_a_caller_that_only_knows_a_refusal(self):
        self.assertTrue(issubclass(review_loop.ReviewersUnavailable, review_loop.LoopError))
        self.assertFalse(issubclass(review_loop.OpenP0, review_loop.ReviewersUnavailable))


class TestGate(unittest.TestCase):
    """The worker gate accepts a patch only with a usable review loop."""

    def validate(self, value, *, reviewed_series=True, agent_log=None):
        """The gate on a patch whose report holds ``value``; its rounds
        reviewed exactly the patch unless ``reviewed_series`` is false."""
        import hashlib

        from tests import test_milestone5_worker as m5
        series = m5.mbox()
        if value is not None and reviewed_series:
            value = dict(value, round_digests=[hashlib.sha256(series).hexdigest()] * value["rounds"])
        own = dict(m5.task(), context={"history": {"sha256": HISTORY}})
        with tempfile.TemporaryDirectory() as directory:
            mbox = Path(directory) / "patches.mbox"
            mbox.write_bytes(series)
            path = Path(directory) / "report.json"
            body = report(contribution_id=own["contribution_id"], task=own["task"])
            body.pop("review_loop")
            if value is not None:
                body["review_loop"] = value
            path.write_text(json.dumps(body))
            return worker_gate.validate(own, m5.proposal(series), mbox, path, agent_log)

    def test_a_patch_with_its_loop_is_accepted(self):
        self.assertEqual(self.validate(loop(finding("P0", "fixed", 1, 2)))["status"], "accepted")

    def test_a_patch_without_a_usable_loop_is_rejected(self):
        for value, why in ((None, "records no review loop"),
                           (loop(rounds=0, stopped="deadline"), "no review round ran"),
                           (loop(finding("P0", "open", 2, None)), "P0 finding open")):
            with self.subTest(why=why), self.assertRaisesRegex(
                    worker_gate.WorkerGateError, f"review loop of the patch: .*{why}"):
                self.validate(value)

    def test_a_task_without_a_history_needs_the_same_report(self):
        """Every task the controller queues carries its history (`task_build`
        refuses to build a task without it); only a Work queued inline before
        task files existed has none. Its patch is accepted with a valid
        report whose `history_digest` is null, like any other, and a missing
        report is the same rejection as for a task with a history."""
        import hashlib

        from tests import test_milestone5_worker as m5
        series = m5.mbox()
        own = m5.task()
        self.assertIsNone((own.get("context") or {}).get("history"))
        digest = hashlib.sha256(series).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            mbox = Path(directory) / "patches.mbox"
            mbox.write_bytes(series)
            path = Path(directory) / "report.json"
            body = report(contribution_id=own["contribution_id"], task=own["task"],
                          expected_revision=own.get("revision"), history_digest=None)
            body["review_loop"] = loop(finding("P0", "fixed", 1, 2), digest=digest)
            path.write_text(json.dumps(body))
            self.assertEqual(worker_gate.validate(own, m5.proposal(series), mbox, path)["status"],
                             "accepted")
            path.unlink()
            with self.assertRaisesRegex(worker_gate.ReviewLoopRejected, "records no review loop"):
                worker_gate.validate(own, m5.proposal(series), mbox, path)

    def test_a_patch_nobody_reviewed_is_unreviewed_not_rejected(self):
        """No reviewer ran: a failure of the pipeline, never a charged rejection."""
        with self.assertRaisesRegex(worker_gate.ReviewersUnavailable, "review loop of the patch: "):
            self.validate(loop(rounds=0, stopped="reviewers_unavailable"))
        self.assertFalse(issubclass(worker_gate.ReviewersUnavailable,
                                    worker_gate.ReviewLoopRejected))

    def test_a_patch_whose_log_shows_no_reviewer_launch_is_unreviewed(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "agent-stdio.log"
            log.write_text("\n".join(failed_launch()))
            with self.assertRaises(worker_gate.ReviewersUnavailable) as raised:
                self.validate(loop(rounds=1, summary=SELF_REVIEW), agent_log=log)
            self.assertEqual(raised.exception.stage, "agent_log")
            log.write_text("\n".join(good_launch()))
            self.assertEqual(self.validate(loop(rounds=1), agent_log=log)["status"], "accepted")
            # No log: the report alone decides, as before.
            self.assertEqual(self.validate(loop(rounds=1), agent_log=log.with_name("none"))["status"],
                             "accepted")

    def test_the_gate_writes_unreviewed_for_the_publish_job(self):
        from tests import test_milestone5_worker as m5
        series = m5.mbox()
        own = dict(m5.task(), context={"history": {"sha256": HISTORY}})
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / "patches.mbox").write_bytes(series)
            body = report(contribution_id=own["contribution_id"], task=own["task"])
            body["review_loop"] = loop(rounds=0, stopped="reviewers_unavailable")
            (d / "report.json").write_text(json.dumps(body))
            (d / "task.json").write_text(json.dumps(own))
            (d / "proposal.json").write_text(json.dumps(m5.proposal(series)))
            code = worker_gate.main(["--task", str(d / "task.json"), "--proposal",
                                     str(d / "proposal.json"), "--mbox", str(d / "patches.mbox"),
                                     "--report", str(d / "report.json"), "--out", str(d / "o.json")])
            out = json.loads((d / "o.json").read_text())
        self.assertEqual((code, out["status"], out["stage"]), (1, "unreviewed", "review_loop"))
        self.assertNotIn("failure_class", out)
        # The same report with a log of failed launches: found in the log.
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / "patches.mbox").write_bytes(series)
            import hashlib
            body["review_loop"] = loop(rounds=1, digest=hashlib.sha256(series).hexdigest())
            (d / "report.json").write_text(json.dumps(body))
            (d / "task.json").write_text(json.dumps(own))
            (d / "proposal.json").write_text(json.dumps(m5.proposal(series)))
            (d / "agent-stdio.log").write_text("\n".join(failed_launch()))
            code = worker_gate.main(["--task", str(d / "task.json"), "--proposal",
                                     str(d / "proposal.json"), "--mbox", str(d / "patches.mbox"),
                                     "--report", str(d / "report.json"), "--agent-log",
                                     str(d / "agent-stdio.log"), "--out", str(d / "o.json")])
            out = json.loads((d / "o.json").read_text())
        self.assertEqual((code, out["status"], out["stage"]), (1, "unreviewed", "agent_log"))
        self.assertNotIn("failure_class", out)

    def test_a_patch_the_last_round_did_not_review_is_rejected(self):
        with self.assertRaisesRegex(worker_gate.ReviewLoopRejected,
                                    "did not review the delivered series"):
            self.validate(loop(), reviewed_series=False)

    def test_a_result_that_delivers_no_patch_needs_no_loop(self):
        from tests import test_milestone5_worker as m5
        candidate = m5.TestWorkerGate._no_change(None, m5.proposal(m5.mbox()))
        own = dict(m5.task(), context={"history": {"sha256": HISTORY}})
        body = report(contribution_id=own["contribution_id"], task=own["task"])
        body.pop("review_loop")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(body))
            self.assertEqual(worker_gate.validate(own, candidate, Path(directory) / "patches.mbox",
                                                  path)["status"], "accepted")


class FakeApi:
    def __init__(self, comments=()):
        self.comments, self.posted = list(comments), []

    def call(self, method, path, data=None, **kw):
        if method == "GET":
            return list(self.comments)
        self.posted.append((method, path, data, kw))
        return {"id": 1}


class TestComment(unittest.TestCase):
    def test_the_comment_names_the_loop_in_inert_text(self):
        value = loop(finding("P0", "fixed", 1, 2, title="Hello @octocat", detail="cc @mona\n```",
                             resolution="Fixed by @hubot"))
        body = review_loop.comment_body(value, CANDIDATE)
        self.assertIn(CANDIDATE, body)
        self.assertIn("2 rounds of fresh reviewers", body)
        self.assertIn("**P0**, round 1, fixed in round 2", body)
        self.assertTrue(body.endswith(review_loop.COMMENT_MARKER.format(CANDIDATE)))
        # No at sign starts a mention, and the fence of a text cannot be closed by it.
        self.assertNotRegex(body, r"@(?!⁠)[A-Za-z]")
        self.assertLess(len(body), 20000)

    def test_the_post_is_a_single_attempt_on_the_wire(self):
        """A lost answer or a 5xx must not post the comment twice: the real
        client makes the POST once."""
        import io
        import urllib.error
        from unittest import mock

        import review_response

        class Page:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"[]"

        def urlopen(request, timeout=None):
            if request.get_method() == "GET":
                return Page()
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, io.BytesIO(b""))

        with mock.patch("urllib.request.urlopen", side_effect=urlopen) as opened, \
                mock.patch("time.sleep"):
            with self.assertRaises(review_response.ResponseError):
                review_loop.comment(review_response.Api("t"), "riseproject-upstreaming/proj",
                                    CANDIDATE, "body")
        methods = [c.args[0].get_method() for c in opened.call_args_list]
        self.assertEqual(methods, ["GET", "POST"])

    def test_it_is_posted_once(self):
        api = FakeApi()
        fork = "riseproject-upstreaming/proj"
        self.assertEqual(review_loop.comment(api, fork, CANDIDATE, "body"), "posted")
        self.assertEqual(api.posted, [("POST", f"/repos/{fork}/commits/{CANDIDATE}/comments",
                                       {"body": "body"}, {"attempts": 1})])
        again = FakeApi([{"user": {"login": review_loop.COMMENT_AUTHOR},
                          "body": "x " + review_loop.COMMENT_MARKER.format(CANDIDATE)}])
        self.assertEqual(review_loop.comment(again, fork, CANDIDATE, "body"), "found")
        self.assertEqual(again.posted, [])
        # The marker in the comment of anyone else is not the App's.
        other = FakeApi([{"user": {"login": "someone"},
                          "body": review_loop.COMMENT_MARKER.format(CANDIDATE)}])
        self.assertEqual(review_loop.comment(other, fork, CANDIDATE, "body"), "posted")

    def test_a_failure_is_only_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "task.json"
            path = Path(directory) / "report.json"
            task.write_text(json.dumps({"task": "development_patch", "contribution_id": CID,
                                        "revision": 3, "context": {"history": {"sha256": HISTORY}}}))
            path.write_text("{")
            args = ["comment", "--task", str(task), "--report", str(path),
                    "--fork", "riseproject-upstreaming/proj", "--head", CANDIDATE]
            import os
            from unittest import mock
            with mock.patch.dict(os.environ, {"UPSTREAMING_FORK_TOKEN": "token"}):
                self.assertEqual(review_loop.main(args), 1)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("UPSTREAMING_FORK_TOKEN", None)
                self.assertEqual(review_loop.main(args), 1)


def running(task="development_patch", revision=3, **over):
    key = f"example--proj/12@g0.r{revision}:{task}"
    return contribution(activity="agent_running", revision=revision, active_task={
        "dedupe_key": key, "task": task, "generation": 0, "revision": revision, "attempt": 1,
        "requested_at": NOW, "deadline_at": NOW, "dispatched_run_id": "4000",
        "reason_ids": ["plan-1"], "feedback_evidence_ids": []}, plan_evidence_id="plan-1", **over)


def tick(rec, pubs=(), *, operations=None, queue_works=None):
    snap = reconcile.Snapshot(contributions={"c": rec}, operations=dict(operations or {}),
                              consumed={}, projects={}, evidence={})
    return reconcile.tick(None, snap, policy=POLICY, now=datetime.fromisoformat(NOW),
                          publications=list(pubs), pressure=capacity.Pressure(ok=True),
                          observe=False, dispatcher_runs=[] if queue_works else None,
                          queue_works=queue_works)


def final(out):
    return [r for k, r in out.staged if k == "contributions"][-1]


def kinds(out):
    return [e["kind"] for k, e in out.staged if k == "events"]


def published(rec, *, run="4242", artifact=903):
    """The Result of the worker run that pushed its patch."""
    work_ = rec["active_task"]
    task = {"task": work_["task"], "contribution_id": rec["contribution_id"],
            "revision": work_["revision"], "generation": 0, "dedupe_key": work_["dedupe_key"],
            "expected_head": SHA_A, "fork": rec["fork"]["name"], "ref": "riscv64-dev",
            "context": {"requested_at": NOW}}
    op = {"operation_id": "op-publish-1", "contribution_id": rec["contribution_id"],
          "kind": "branch_update", "status": "confirmed", "revision": work_["revision"],
          "created_at": NOW, "target": {"ref": "riscv64-dev", "expected_head": SHA_A},
          "proposed": {"commit": CANDIDATE, "changed_files": CHANGED}}
    return {"status": "published", "candidate": CANDIDATE, "operation": op, "task": task,
            "worker_run_id": run, "publication_artifact_id": artifact,
            "artifact": {"work_id": "w", "claim_id": "c", "artifact_id": artifact, "name": "n",
                         "digest": "sha256:" + "0" * 64, "run_id": run}, "attempt": 1}


class TestFirstPatch(unittest.TestCase):
    """The first published patch has no development PR: the controller opens it
    from `development.agent_running` once the push is confirmed, and the Result
    is folded once the PR is recorded."""

    def first(self):
        rec = running()
        rec["fork"] = dict(rec["fork"], pr_number=None)
        rec["upstream"] = dict(rec["upstream"], base_sha=rec["fork"]["head_sha"])
        return rec

    def test_the_result_waits_for_the_pr_the_controller_opens_in_the_same_tick(self):
        rec = self.first()
        result = published(rec)
        out = tick(rec, [result])
        pending = final(out)
        self.assertEqual(engine.state_key(pending), "development.agent_running")
        self.assertIn("development PR", out.report["publications"][0]["deferred"])
        self.assertEqual(kinds(out), ["launch_development_pr"])
        op = next(o for k, o in out.staged if k == "operations" and o["kind"] == "dev_pr_create")
        self.assertEqual(op["target"]["expected_head"], CANDIDATE)
        # Once the PR is recorded the Result is folded.
        with_pr = dict(pending, fork=dict(pending["fork"], pr_number=7), active_operation_id=None)
        done = final(tick(with_pr, [result]))
        self.assertEqual(engine.state_key(done), "development.waiting_ci")
        self.assertIsNone(done["pending_publication"])
        self.assertEqual(done["proposal_artifact"]["artifact_id"], 903)

    def test_a_deferred_result_is_not_task_without_a_usable_result(self):
        """The queue says the graph completed and the artifact is collected,
        but the Result is only deferred: dispatch recovery must not allocate
        a retry, bump the revision or charge `task_recovery`."""
        import tasks

        rec = self.first()
        graph = tasks.graph_id(rec["active_task"]["dedupe_key"])
        out = tick(rec, [published(rec)], queue_works={graph: "completed"})
        after = final(out)
        self.assertNotIn("allocate_task_retry", kinds(out))
        self.assertEqual(kinds(out), ["launch_development_pr"])
        self.assertEqual((after["revision"], after["active_task"]["dedupe_key"]),
                         (rec["revision"], rec["active_task"]["dedupe_key"]))
        self.assertEqual(after["budget"], rec["budget"])

    def test_the_intent_names_the_commit_the_worker_pushed_only(self):
        rec = self.first()
        op = published(rec)["operation"]
        self.assertEqual(dev_pr.pushed_head(rec, [op]), CANDIDATE)
        # Another revision, an open operation, another lease, or the recorded head is no push.
        for bad in (dict(op, revision=2), dict(op, status="applying"),
                    dict(op, target={"ref": "riscv64-dev", "expected_head": "f" * 40}),
                    dict(op, proposed={"commit": rec["fork"]["head_sha"]})):
            self.assertIsNone(dev_pr.pushed_head(rec, [bad]))
        planned = {"kind": "dev_pr_create", "revision": rec["revision"],
                   "target": {"repository": rec["fork"]["name"],
                              "repository_id": rec["fork"]["repository_id"], "ref": "riscv64-dev",
                              "base_sha": rec["upstream"]["base_sha"],
                              "expected_head": CANDIDATE}}
        self.assertIsNone(dev_pr.intent_problem(rec, planned, [op]))
        self.assertEqual(dev_pr.intent_problem(rec, planned, []),
                         "the head is not the commit the worker pushed")
        self.assertEqual(dev_pr.intent_problem(rec, dict(
            planned, target=dict(planned["target"], expected_head="e" * 40)), [op]),
            "the head is not the commit the worker pushed")


class TestPublishJobGate(unittest.TestCase):
    """The worker's publish job turns an unreviewed Result into the free path."""

    def test_the_publish_job_reads_the_agent_log_and_maps_unreviewed(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        for name in (".github/workflows/worker.md", ".github/workflows/worker.lock.yml"):
            text = (root / name).read_text()
            self.assertIn("name: Download the agent log", text, name)
            self.assertIn("../agent-artifact/agent-stdio.log", text, name)
            self.assertNotIn("find ../agent-artifact", text, name)
            self.assertIn("--agent-log", text, name)
            self.assertIn("::warning::", text, name)
            self.assertIn('{status:"unreviewed", failure_class:null, reason:$reason, '
                          'stage:$stage}', text, name)
        lock = yaml.safe_load((root / ".github/workflows/worker.lock.yml").read_text())
        steps = lock["jobs"]["publish"]["steps"]
        names = [s.get("name") for s in steps]
        download = steps[names.index("Download the agent log")]
        self.assertEqual(download["with"], {"name": "agent", "path": "agent-artifact"})
        self.assertLess(names.index("Download the agent log"),
                        names.index("Resolve exact Result and gate the uncredentialed proposal"))
        # The log is the one the gh-aw steps parse and upload with the agent artifact.
        agent = lock["jobs"]["agent"]["steps"]
        upload = next(s for s in agent if s.get("name") == "Upload agent artifacts")
        self.assertEqual(upload["with"]["name"], "agent")
        self.assertIn("/tmp/gh-aw/agent-stdio.log", upload["with"]["path"])

    def test_the_prompt_makes_the_worker_report_unavailable_reviewers_not_review_itself(self):
        prompt = (Path(__file__).resolve().parents[1] / ".github/workflows/worker.md").read_text()
        section = prompt[prompt.index("## The review loop"):prompt.index("## The worker report")]
        flat = " ".join(section.split())
        for needle in ("you never stand in for it", "you do not review the series yourself",
                       "`rounds: 0`", '`stopped_because: "reviewers_unavailable"`',
                       "deliver the committed series as usual"):
            self.assertIn(needle, flat)


class TestPublishRetryGate(unittest.TestCase):
    """The publish-only retry reaches the push without the worker gate: the
    preflight both paths run refuses a patch without a usable review loop
    (the checks of the proposal itself are in ``test_fork_publish.py``)."""

    def test_both_publishing_jobs_pass_the_report_to_the_preflight(self):
        root = Path(__file__).resolve().parents[1]
        for name in (".github/workflows/publish-retry.yml", ".github/workflows/worker.md"):
            text = (root / name).read_text()
            start = text.index("python -m fork_publish preflight")
            self.assertIn("--report ../in/report.json", text[start:start + 400], name)


class TestOldWork(unittest.TestCase):
    def test_a_result_of_the_retired_review_is_task_without_a_usable_result_nothing_charged(self):
        rec = running()
        result = dict(published(rec), status="verified")
        result.pop("operation")
        out = tick(rec, [result])
        self.assertEqual(kinds(out), ["reallocate_after_pipeline_failure"])
        new = final(out)
        self.assertEqual(engine.state_key(new), "development.agent_running")
        self.assertEqual(new["budget"]["attempts_used"], rec["budget"]["attempts_used"])
        self.assertEqual(new["active_task"]["attempt"], 1)
        self.assertNotEqual(new["active_task"]["dedupe_key"], None)

    def test_a_held_result_of_the_earlier_contract_is_relaunched_nothing_charged(self):
        """The publish-only retry of a Result held before the deploy finds a
        report with no review loop: `unreviewed`, a free relaunch like
        `verified`, and the held Result goes with it."""
        held = {"worker_run_id": "4242", "attempts": 1, "last_reason": "infra",
                "next_attempt_at": NOW}
        # The task was cleared when the Result was held.
        work_ = running()
        rec = dict(work_, pending_publication=held, active_task=None)
        result = dict(published(work_), status="unreviewed")
        result.pop("operation")
        out = tick(rec, [result])
        self.assertEqual(kinds(out), ["reallocate_after_pipeline_failure"])
        new = final(out)
        self.assertEqual(engine.state_key(new), "development.agent_running")
        self.assertIsNone(new["pending_publication"])
        self.assertEqual(new["budget"], rec["budget"])
        self.assertEqual(new["active_task"]["attempt"], 1)
        self.assertEqual(new["revision"], rec["revision"] + 1)

    def test_a_patch_with_no_review_is_relaunched_nothing_charged_a_bounded_number_of_times(self):
        """`unreviewed` (the reviewers could not run) takes the free path, and
        the free relaunches are counted per version of the task, so a task
        whose reviewers stay unavailable is charged after the third."""
        rec = running()
        result = dict(published(rec), status="unreviewed")
        result.pop("operation")
        for used in range(3):
            with self.subTest(used=used):
                out = tick(rec, [result])
                self.assertEqual(kinds(out), ["reallocate_after_pipeline_failure"])
                new = final(out)
                self.assertEqual(new["budget"], rec["budget"])
                self.assertEqual(new["active_task"]["review_free"], used + 1)
                self.assertNotIn("pipeline_free", new["active_task"])
                rec = dict(new)
                result = dict(published(rec, run=str(4243 + used), artifact=904 + used),
                              status="unreviewed")
                result.pop("operation")
        # The fourth is no longer free: a task without a usable Result, charged.
        out = tick(rec, [result])
        self.assertEqual(kinds(out), ["allocate_task_retry"])
        self.assertNotIn("review_free", final(out)["active_task"])

    def test_the_two_kinds_of_free_relaunch_never_reset_each_other(self):
        """A failure before the agent (`pipeline_free`) between the failures of
        the review (`review_free`) does not give the review a fresh count."""
        rec = running()
        used = 0
        for round_ in range(6):
            result = dict(published(rec, run=str(5000 + round_), artifact=7000 + round_),
                          status="unreviewed")
            result.pop("operation")
            out = tick(rec, [result])
            if kinds(out) != ["reallocate_after_pipeline_failure"]:
                break
            used += 1
            rec = dict(final(out))
            # The version gate relaunches the new graph for a failure before its agent.
            rec["active_task"] = dict(rec["active_task"],
                                      pipeline_free={"version": f"v{round_}", "count": 1})
        self.assertEqual(used, 3)
        self.assertEqual(kinds(out), ["allocate_task_retry"])

    def test_a_held_result_keeps_the_review_count_of_its_task(self):
        """The task is cleared when its Result is held for a publish-only retry,
        and the rebuilt task must not start the free relaunches again."""
        work_ = running()
        held = {"worker_run_id": "4242", "attempts": 1, "last_reason": "infra",
                "next_attempt_at": NOW, "review_free": 3}
        rec = dict(work_, pending_publication=held, active_task=None)
        result = dict(published(work_), status="unreviewed")
        result.pop("operation")
        self.assertEqual(kinds(tick(rec, [result])), ["allocate_task_retry"])
        rec["pending_publication"] = dict(held, review_free=2)
        out = tick(rec, [result])
        self.assertEqual(kinds(out), ["reallocate_after_pipeline_failure"])
        self.assertEqual(final(out)["active_task"]["review_free"], 3)

    def test_the_gate_of_a_failure_before_the_agent_keeps_the_review_count(self):
        import controller
        aw = dict(running()["active_task"], review_free=2)
        new, _event, _why = controller.terminal_task(
            dict(running(), active_task=aw), aw, "cancelled", now=datetime.fromisoformat(NOW),
            policy=POLICY, details={"pipeline_failure": {"version": "abc", "free_count": 1}})
        self.assertEqual((new["active_task"]["review_free"], new["active_task"]["pipeline_free"]),
                         (2, {"version": "abc", "count": 1}))

    def test_task_that_carries_the_review_contract_publishes_like_any_other(self):
        rec = running()
        task = {"task": "development_patch", "review": "required", "contribution_id": CID,
                "revision": 3, "expected_head": SHA_A, "fork": "riseproject-upstreaming/proj"}
        proposal = {"outcome": "patch", "operation": "append", "expected_head": SHA_A,
                    "ref": "riscv64-dev", "contribution_id": rec["contribution_id"],
                    "expected_revision": 3, "fork_repository_id": rec["fork"]["repository_id"]}
        result = fork_publish.preflight(
            record=dict(rec, contribution_id=CID), task=task, proposal=copy.deepcopy(proposal),
            report_path=helpers.accepted_loop(self),
            fork_lookup=lambda name: {"id": rec["fork"]["repository_id"], "full_name": name,
                                      "archived": False})
        self.assertNotEqual(result["status"], "verify")
        self.assertFalse(hasattr(fork_publish, "verify"))
        self.assertNotIn("development.publishing", lc.LIFECYCLE)


if __name__ == "__main__":
    unittest.main()
