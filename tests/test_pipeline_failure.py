# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A run that failed before its agent ran, for a step of the pipeline, is not
a failure of the Work (worker run 45397386038 of luhenry--d2vm/3 failed at
"Start MCP Gateway")."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import collect
import pipeline_failure
import queue_result
import reconcile

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "live-d2vm-gateway"
WORKFLOWS = ROOT / ".github" / "workflows"


def live():
    data = json.loads((FIXTURE / "worker-run-45397386038.json").read_text())
    return data["run"], data["jobs"]


def fail_step(jobs, job, step, conclusion="failure"):
    """``jobs`` with ``step`` of ``job`` concluded ``conclusion``, and every
    step after it skipped, as GitHub reports a failure."""
    jobs = copy.deepcopy(jobs)
    target = next(j for j in jobs if j["name"] == job)
    hit = False
    for s in target["steps"]:
        if hit and s["conclusion"] == "success" and s["number"] < 100 and \
                s["name"].startswith(("Mount", "Clean", "Audit", "Execute")):
            s["conclusion"] = "skipped"
        if s["name"] == step:
            s["conclusion"] = conclusion
            hit = True
    target["conclusion"] = "failure"
    return jobs


class TestClassify(unittest.TestCase):
    def test_the_gateway_failure_of_the_live_run_is_the_pipeline(self):
        run, jobs = live()
        self.assertEqual(pipeline_failure.classify(run, jobs),
                         {"pipeline": True, "job": "agent", "step": "Start MCP Gateway",
                          "head_sha": "db37ee679561919520f8d28ab28a849c192420d6",
                          "ended_at": "2026-11-24T12:54:51Z"})

    def test_a_failed_activation_is_the_pipeline(self):
        run, jobs = live()
        jobs = copy.deepcopy(jobs)
        for j in jobs:
            if j["name"] == "activation":
                j["conclusion"] = "failure"
                j["steps"] = [{"number": 1, "name": "Set up job", "status": "completed",
                               "conclusion": "success"},
                              {"number": 2, "name": "Validate the activation", "status": "completed",
                               "conclusion": "failure"}]
            elif j["name"] == "agent":
                j["conclusion"] = "skipped"
                j["steps"] = []
        self.assertEqual(pipeline_failure.classify(run, jobs)["job"], "activation")

    def test_a_failed_contribution_step_is_the_works(self):
        run, jobs = live()
        # The fork checkout failed, so the gateway was never started.
        failed = copy.deepcopy(jobs)
        agent = next(j for j in failed if j["name"] == "agent")
        for s in agent["steps"]:
            if s["name"] == "Start MCP Gateway":
                s["conclusion"] = "skipped"
            if s["name"] == "Check out the contribution fork at the expected head (read-only)":
                s["conclusion"] = "failure"
        self.assertIsNone(pipeline_failure.classify(run, failed))

    def test_a_failed_agent_step_is_the_works(self):
        run, jobs = live()
        failed = copy.deepcopy(jobs)
        agent = next(j for j in failed if j["name"] == "agent")
        for s in agent["steps"]:
            if s["name"] == "Start MCP Gateway":
                s["conclusion"] = "success"
            if s["name"] == "Execute Pi CLI":
                s["conclusion"] = "failure"
        self.assertIsNone(pipeline_failure.classify(run, failed))

    def test_a_cancelled_or_timed_out_run_is_not_a_failure(self):
        run, jobs = live()
        for conclusion in ("cancelled", "timed_out", "success", None):
            with self.subTest(conclusion=conclusion):
                self.assertIsNone(pipeline_failure.classify(dict(run, conclusion=conclusion), jobs))
        self.assertIsNone(pipeline_failure.classify(dict(run, status="in_progress"), jobs))

    def test_a_lost_runner_is_not_a_failure_of_a_step(self):
        run, jobs = live()
        lost = copy.deepcopy(jobs)
        agent = next(j for j in lost if j["name"] == "agent")
        for s in agent["steps"]:
            if s["conclusion"] == "failure":
                s["conclusion"] = "cancelled"
        self.assertIsNone(pipeline_failure.classify(run, lost))
        agent["steps"] = []
        self.assertIsNone(pipeline_failure.classify(run, lost))

    def test_a_step_before_the_agent_that_is_not_listed_is_the_pipelines(self):
        run, jobs = live()
        self.assertEqual(pipeline_failure.classify(
            run, fail_step(jobs, "agent", "Install pinned Pi and create credential-free unsandboxed "
                                          "engine wrapper"))["step"].split()[0], "Install")

    def test_each_workflow_has_its_own_contribution_steps(self):
        run, jobs = live()
        for workflow, step in (
                ("planner.lock.yml", "Check out the upstream project (read-only)"),
                ("reviewer.lock.yml", "Fetch and verify the held patch"),
                ("dispatcher.lock.yml", "Replay the controller-requested task from committed "
                                        "state (deterministic, no model)"),
                ("worker.lock.yml", "Check out the contribution fork at the expected head "
                                    "(read-only)")):
            failed = copy.deepcopy(jobs)
            agent = next(j for j in failed if j["name"] == "agent")
            agent["steps"] = [{"number": 1, "name": step, "status": "completed",
                               "conclusion": "failure"},
                              {"number": 2, "name": "Execute Pi CLI", "status": "completed",
                               "conclusion": "skipped"}]
            with self.subTest(workflow=workflow):
                found = pipeline_failure.classify(
                    dict(run, path=f".github/workflows/{workflow}"), failed)
                # Only the dispatcher's custom steps are the pipeline's.
                self.assertEqual(found is not None, workflow == "dispatcher.lock.yml")
        # Another workflow's contribution step fails the Work, whichever
        # workflow it is read as: an unknown workflow keeps all of them.
        failed = copy.deepcopy(jobs)
        agent = next(j for j in failed if j["name"] == "agent")
        agent["steps"] = [{"number": 1, "name": "Check out the upstream project (read-only)",
                           "status": "completed", "conclusion": "failure"}]
        self.assertIsNone(pipeline_failure.classify(dict(run, path=".github/workflows/x.yml"),
                                                    failed))


class TestWorkflowContract(unittest.TestCase):
    def frontmatter(self, name):
        return yaml.safe_load((WORKFLOWS / name).read_text().split("---", 2)[1])

    def test_resolving_the_task_is_a_step_of_the_pipeline_not_of_the_contribution(self):
        """The task file, the plan, the history and the report are what the
        controller committed, read at a pinned commit and digest: a step that
        cannot read them is a failure of the pipeline, so it is not a
        contribution step."""
        run, jobs = live()
        for name in ("worker", "planner", "reviewer"):
            steps = self.frontmatter(f"{name}.md")["pre-agent-steps"]
            [resolve] = [s for s in steps if "task_file.py resolve" in s.get("run", "")]
            self.assertTrue(resolve["name"].startswith("Resolve the task and fetch the pinned"))
            self.assertNotIn(resolve["name"],
                             pipeline_failure.CONTRIBUTION_STEPS[f"{name}.lock.yml"])
            for step in steps:
                if "held_patch.py" in step.get("run", "") or "git checkout" in step.get("run", ""):
                    self.assertIn(step["name"],
                                  pipeline_failure.CONTRIBUTION_STEPS[f"{name}.lock.yml"])
            failed = copy.deepcopy(jobs)
            agent = next(j for j in failed if j["name"] == "agent")
            agent["steps"] = [{"number": 1, "name": resolve["name"], "status": "completed",
                               "conclusion": "failure"},
                              {"number": 2, "name": "Execute Pi CLI", "status": "completed",
                               "conclusion": "skipped"}]
            found = pipeline_failure.classify(
                dict(run, path=f".github/workflows/{name}.lock.yml"), failed)
            self.assertEqual(found["step"], resolve["name"])

    def test_the_agent_step_runs_once_in_each_lock(self):
        for name in pipeline_failure.CONTRIBUTION_STEPS:
            text = (WORKFLOWS / name).read_text()
            self.assertEqual(len(re.findall(rf"^\s+- name: {re.escape(pipeline_failure.AGENT_STEP)}$",
                                            text, re.M)), 1, name)

    def test_every_contribution_step_is_a_step_of_its_workflow(self):
        for name, steps in pipeline_failure.CONTRIBUTION_STEPS.items():
            lock = yaml.safe_load((WORKFLOWS / name).read_text())
            names = {s.get("name") for job in lock["jobs"].values() for s in job.get("steps", [])}
            self.assertLessEqual(steps, names, name)

    # The pre-agent and custom steps of the sources that do not depend on the
    # contribution: a failure in one of them is the pipeline's.
    PIPELINE_STEPS = frozenset({
        "Register pinned riscv64 QEMU binfmt", "Install pinned gh-act when available",
        # It pulls the one fixed image of the pipeline, whatever the contribution.
        "Resolve anonymous reproduction image",
        # They read what the controller committed, at a pinned commit and digest.
        "Resolve the task and fetch the pinned plan and history",
        "Resolve the task and fetch the pinned history",
        "Resolve the task and fetch the pinned plan, history and report",
        "Split Pi provider authentication from untrusted tool environment",
        "Install pinned Pi and create credential-free unsandboxed engine wrapper",
        "Suspend workflow commands around untrusted agent output",
        "Resume workflow commands after untrusted agent output",
        "Install ripgrep and fd for the agent's grep and find tools",
        "Make the home directories the sandbox mounts read-only to the agent",
        "Make the code that the steps after the agent run unwritable by the agent",
        "Seal the workspace's git checkout against the agent",
        "Mint a queue token for the Policy reinstall", "Write the compiled queue policy of this run",
        "Reinstall the queue Policy when the compiled policy drops an installed profile",
        "Replay the controller-requested task from committed state (deterministic, no model)",
        "Copy the trusted intent check out of the agent's reach",
        "Stage the plan as trusted queue intents",
        "Keep only the intents the trusted stage wrote",
    })

    def test_every_pre_agent_and_custom_step_is_a_contribution_step_or_the_pipelines(self):
        # A step added to a source must be classified here, and a step that
        # reads the contribution listed in `CONTRIBUTION_STEPS`.
        for name in ("worker", "planner", "reviewer", "dispatcher"):
            data = self.frontmatter(f"{name}.md")
            listed = pipeline_failure.CONTRIBUTION_STEPS[f"{name}.lock.yml"]
            for step in (s for key in ("pre-agent-steps", "steps", "post-steps")
                         for s in data.get(key, [])):
                title = step.get("name")
                if title is None:
                    continue     # an action step without a name (setup-uv)
                self.assertTrue(title in listed or title in self.PIPELINE_STEPS,
                                f"{name}.md: step {title!r} is in neither "
                                "CONTRIBUTION_STEPS nor PIPELINE_STEPS")
                self.assertFalse(title in listed and title in self.PIPELINE_STEPS, title)


class TestClaimRuns(unittest.TestCase):
    def test_the_runs_of_every_claim_of_the_work_newest_first(self):
        p = {"works": {"w1": {"work_id": "w1", "graph_id": "g1"},
                       "w2": {"work_id": "w2", "graph_id": "g2"}},
             "claims": {"c1": {"work_id": "w1", "dispatch_id": "d1"},
                        "c2": {"work_id": "w1", "dispatch_id": "d2"},
                        "c3": {"work_id": "w1", "dispatch_id": "d3"},
                        "c4": {"work_id": "w2", "dispatch_id": "d4"},
                        "c5": {"work_id": "w2", "dispatch_id": "missing"}},
             "dispatches": {"d1": {"run": {"run_id": "9"}}, "d2": {"run": {"run_id": 100}},
                            "d3": {"run": {"run_id": "bad id"}}, "d4": {"run": None}}}
        self.assertEqual(queue_result.work_claim_runs(p), {"g1": ["100", "9"]})
        self.assertEqual(queue_result.work_claim_runs(None), {})


class TestVersion(unittest.TestCase):
    def test_the_version_of_a_checkout_is_the_version_of_its_api_tree(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            env = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "a.py").write_text("a")
            (repo / "scripts").mkdir()
            (repo / "scripts" / "b.sh").write_text("b")
            (repo / ".state").mkdir()
            (repo / ".state" / "x.json").write_text("{}")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), *env, "commit", "-qm", "one"], check=True)
            first = reconcile.pipeline_version(repo)
            sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                 capture_output=True, text=True).stdout.strip()
            listing = subprocess.run(["git", "-C", str(repo), "ls-tree", "HEAD"], check=True,
                                     capture_output=True, text=True).stdout.splitlines()
            entries = []
            for line in listing:
                meta, _, path = line.partition("\t")
                mode, kind, object_id = meta.split()
                entries.append({"mode": mode, "type": kind, "sha": object_id, "path": path})
            api = mock.Mock()
            api.get.return_value = {"tree": entries, "truncated": False}
            collect._TREE_VERSIONS.clear()
            self.assertEqual(collect.pipeline_version_at(api, sha), first)
            collect.pipeline_version_at(api, sha)
            self.assertEqual(api.get.call_count, 1)        # cached per commit
            # A state commit does not change the version.
            (repo / ".state" / "y.json").write_text("{}")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), *env, "commit", "-qm", "two"], check=True)
            self.assertEqual(reconcile.pipeline_version(repo), first)
            (repo / "a.py").write_text("changed")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), *env, "commit", "-qm", "three"], check=True)
            self.assertNotEqual(reconcile.pipeline_version(repo), first)
        self.assertIsNone(pipeline_failure.tree_version([]))
        self.assertIsNone(pipeline_failure.tree_version(["040000 tree abc\t.state"]))

    def test_the_version_of_a_real_main_commit_is_read_alike_from_git_and_the_api(self):
        data = json.loads((FIXTURE / "main-tree.json").read_text())
        api = mock.Mock()
        api.get.return_value = data["api"]
        collect._TREE_VERSIONS.clear()
        from_api = collect.pipeline_version_at(api, data["commit"])
        listing = subprocess.CompletedProcess([], 0, stdout=data["ls_tree"])
        with mock.patch("subprocess.run", return_value=listing):
            from_git = reconcile.pipeline_version(Path("."))
        self.assertIsNotNone(from_git)
        self.assertEqual(from_api, from_git)
        # `.state` is no part of it.
        self.assertTrue(any(e["path"] == ".state" for e in data["api"]["tree"]))
        without = dict(data["api"], tree=[e for e in data["api"]["tree"] if e["path"] != ".state"])
        api.get.return_value = without
        collect._TREE_VERSIONS.clear()
        self.assertEqual(collect.pipeline_version_at(api, data["commit"]), from_api)

    def test_a_truncated_or_malformed_tree_is_no_version(self):
        api = mock.Mock()
        collect._TREE_VERSIONS.clear()
        for body in ({"tree": [], "truncated": True}, {"tree": "x"}, None):
            api.get.return_value = body
            with self.assertRaises(collect.CollectError):
                collect.pipeline_version_at(api, "a" * 40)
        with self.assertRaises(collect.CollectError):
            collect.pipeline_version_at(api, "not a commit")


class TestRunRead(unittest.TestCase):
    def test_the_live_run_is_read_from_its_run_and_its_jobs(self):
        run, jobs = live()
        api = mock.Mock()
        api.get.return_value = run
        api.paginate.return_value = jobs
        found = collect.agent_run_failure(api, "45397386038")
        self.assertEqual(found["step"], "Start MCP Gateway")
        self.assertIn("/runs/45397386038/attempts/1/jobs", api.paginate.call_args.args[0])

    def test_a_run_that_did_not_fail_or_is_gone_is_not_a_failure(self):
        run, _ = live()
        api = mock.Mock()
        api.get.return_value = dict(run, conclusion="cancelled")
        self.assertIsNone(collect.agent_run_failure(api, "1"))
        api.paginate.assert_not_called()
        api.get.side_effect = collect.CollectError("gone", status=404)
        self.assertIsNone(collect.agent_run_failure(api, "1"))
        api.get.side_effect = collect.CollectError("down", status=502)
        with self.assertRaises(collect.CollectError):
            collect.agent_run_failure(api, "1")
        with self.assertRaises(collect.CollectError):
            collect.agent_run_failure(api, "12; rm")

    def test_the_readings_of_a_tick(self):
        record = json.loads((FIXTURE / "contribution.json").read_text())
        import work
        graph = work.graph_id(record["active_work"]["dedupe_key"])

        def failure(run_id):
            return {"pipeline": True, "job": "agent", "step": "Start MCP Gateway",
                    "head_sha": ("d" if run_id != "45400000000" else "e") * 40,
                    "ended_at": f"2026-11-24T1{run_id[-1]}:00:00Z"}

        versions = {"d" * 40: "v1", "e" * 40: "v0"}
        dispatcher_runs = [
            {"id": 7, "status": "completed", "conclusion": "failure",
             "created_at": "2026-11-24T12:00:00Z"},
            {"id": 6, "status": "completed", "conclusion": "failure",
             "created_at": "2026-11-24T11:00:00Z"},
            {"id": 5, "status": "completed", "conclusion": "success",
             "created_at": "2026-11-24T10:00:00Z"}]
        with mock.patch.object(collect, "agent_run_failure",
                               side_effect=lambda _api, run_id: failure(run_id)) as read, \
                mock.patch.object(collect, "pipeline_version_at",
                                  side_effect=lambda _api, sha: versions[sha]):
            got = reconcile.pipeline_failure_readings(
                None, {"c": record}, {graph: "cancelled"}, {},
                {graph: ["45413173475", "45413173472", "45400000000", "45399999999"]},
                dispatcher_runs)
        # The newest Claim, and how many of the newest ran that code in a row.
        self.assertEqual(got[graph], {"run_id": "45413173475", "pipeline": True, "job": "agent",
                                      "step": "Start MCP Gateway", "version": "v1", "count": 2,
                                      "ended_at": "2026-11-24T15:00:00Z"})
        self.assertEqual((got["7"]["count"], got["7"]["run_id"]), (2, "7"))
        self.assertNotIn("6", got)
        self.assertEqual([c.args[1] for c in read.call_args_list],
                         ["45413173475", "45413173472", "45400000000", "7", "6"])
        # A graph the queue holds in another state, or without a retry, is
        # not read; one without a Claim run is no failure; a failed read
        # leaves no reading.
        with mock.patch.object(collect, "agent_run_failure",
                               side_effect=collect.CollectError("boom")):
            self.assertEqual(reconcile.pipeline_failure_readings(
                None, {"c": record}, {graph: "cancelled"}, {}, {graph: ["5"]}, []), {})
        self.assertEqual(reconcile.pipeline_failure_readings(
            None, {"c": record}, {graph: "cancelled"}, {}, {}, []),
            {graph: {"run_id": None, "pipeline": False}})
        for state in ("claimed", "available"):
            self.assertEqual(reconcile.pipeline_failure_readings(
                None, {"c": record}, {graph: state}, {}, {graph: ["5"]}, []), {})
        with mock.patch.object(collect, "agent_run_failure", return_value=None):
            self.assertEqual(reconcile.pipeline_failure_readings(
                None, {"c": record}, {graph: "available"}, {graph: {"attempts": 1}},
                {graph: ["5"]}, []), {graph: {"run_id": "5", "pipeline": False}})


if __name__ == "__main__":
    unittest.main()
