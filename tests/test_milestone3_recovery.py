# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Milestone 3 deterministic recovery, trigger isolation, and security gates."""

import copy
import re
import unittest
from datetime import datetime
from pathlib import Path

import yaml

import capacity
import collect
import controller
import triggers
from common import CONTROL_REPO
from scripts import generate_doc
from tests.helpers import NOW, contribution, policy

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"


class TestDeterministicController(unittest.TestCase):
    def test_identical_fixture_inputs_produce_identical_state_effects_and_report(self):
        record = contribution(
            phase="investigation",
            activity="fix_needed",
            fork=None,
            active_work=None,
        )
        inputs = controller.Inputs(
            now=datetime.fromisoformat(NOW),
            pressure=capacity.Pressure(ok=True),
        )
        arguments = {
            "contributions": {"record": record},
            "operations": {},
            "consumed": {},
            "projects": {},
            "policy": policy(read_only=False),
            "inputs": inputs,
        }
        first = controller.tick(**copy.deepcopy(arguments))
        second = controller.tick(**copy.deepcopy(arguments))
        self.assertEqual(first.staged, second.staged)
        self.assertEqual(first.requested_effects, second.requested_effects)
        self.assertEqual(first.report, second.report)


class TestCanonicalTrackingRepository(unittest.TestCase):
    def test_intake_and_tracking_urls_are_control_repository_only(self):
        class Api:
            def __init__(self):
                self.paths = []

            def paginate(self, path, **_kwargs):
                self.paths.append(path)
                return []

        api = Api()
        self.assertEqual(collect.intake_issues(api), [])
        self.assertEqual(
            api.paths,
            [
                "/repos/riseproject-dev/porting-agent/issues?labels="
                "upstreaming-request&state=open&per_page=50"
            ],
        )
        context = collect.tracking_issue_context(
            {"id": 1, "number": 1, "body": "request"},
            {"id": 1, "number": 1},
        )
        self.assertEqual(
            context["url"], "https://github.com/riseproject-dev/porting-agent/issues/1"
        )


class TestTriggerIsolation(unittest.TestCase):
    def workflows_for(self, paths):
        return [
            finding.workflow
            for finding in triggers.audit(WORKFLOWS, paths)
            if finding.triggered
        ]

    def test_state_only_commit_wakes_only_website(self):
        self.assertEqual(
            self.workflows_for([".state/commands/comment-1.json"]),
            ["website.yml"],
        )

    def test_code_and_workflow_deployment_wake_controller_not_status_recursion(self):
        code = self.workflows_for(["controller.py"])
        self.assertIn("controller.yml", code)
        self.assertIn("validate.yml", code)
        self.assertNotIn("website.yml", code)
        workflow = self.workflows_for([".github/workflows/controller.yml"])
        self.assertIn("controller.yml", workflow)
        self.assertIn("validate.yml", workflow)
        self.assertNotIn("website.yml", workflow)


class TestTrustedPublicationWorkflow(unittest.TestCase):
    def setUp(self):
        self.controller_text = (WORKFLOWS / "controller.yml").read_text()
        self.controller = yaml.safe_load(self.controller_text)

    def test_state_writer_is_control_repo_only_and_plain_fast_forward(self):
        reconcile = self.controller["jobs"]["reconcile"]
        self.assertEqual(reconcile["environment"], "state")
        steps = reconcile["steps"]
        token = next(step for step in steps if step.get("id") == "state-token")
        self.assertEqual(token["with"]["repositories"], "porting-agent")
        self.assertEqual(token["with"]["permission-contents"], "write")
        self.assertNotIn("permission-actions", token["with"])
        publish = next(
            step
            for step in steps
            if step.get("name") == "Publish one validated state-only commit on main"
        )
        self.assertIn("python -m state_commit", publish["run"])
        state_source = (ROOT / "state_commit.py").read_text()
        self.assertNotIn("--force", state_source)
        self.assertNotIn("force-with-lease", state_source)
        self.assertIn('f"{commit}:refs/heads/{branch}"', state_source)

    def test_control_jobs_have_no_model_or_destination_credential(self):
        control_workflows = "\n".join(
            (WORKFLOWS / name).read_text()
            for name in ("controller.yml", "validate.yml", "website.yml")
        )
        self.assertNotIn(
            "${{ secrets.RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY }}", control_workflows
        )
        self.assertNotIn(
            "${{ vars.RISEPROJECT_UPSTREAMING_APP_CLIENT_ID }}", control_workflows
        )
        self.assertNotIn("ANTHROPIC_API_KEY", control_workflows)
        self.assertEqual(CONTROL_REPO, "riseproject-dev/porting-agent")

    def test_human_projections_follow_state_publication_and_are_retryable(self):
        steps = self.controller["jobs"]["reconcile"]["steps"]
        names = [step.get("name") for step in steps]
        publication = names.index("Publish one validated state-only commit on main")
        for projection in (
            "Mark command receipts complete",
            "Append lifecycle entries to tracking issues",
            "Explain rejected commands on their tracking issues",
        ):
            index = names.index(projection)
            self.assertGreater(index, publication)
            self.assertTrue(steps[index]["continue-on-error"])


class TestStatusSite(unittest.TestCase):
    def test_empty_tree_is_stable_and_project_page_is_rooted(self):
        rendered = generate_doc.render([])
        self.assertIn("# Porting status", rendered)
        self.assertIn("permalink: /status/", rendered)
        self.assertIn("title: Status", rendered)
        self.assertIn("nav_order: 2", rendered)
        self.assertIn("| Project | Tracking issue |", rendered)
        self.assertNotIn("None", rendered)

    def test_generated_cells_escape_untrusted_markdown(self):
        state = contribution(updated_at=NOW)
        state["upstream"]["full_name"] = "example/proj|escape"
        rendered = generate_doc.render([state])
        self.assertIn("proj\\|escape", rendered)
        self.assertNotIn("proj|escape", rendered)

    def test_pages_uses_actions_deployment_without_contents_write(self):
        website = yaml.safe_load((WORKFLOWS / "website.yml").read_text())
        self.assertEqual(
            website["permissions"],
            {
                "contents": "read",
                "pages": "write",
                "id-token": "write",
                "pull-requests": "read",
            },
        )
        self.assertEqual(website["name"], "Website")
        self.assertEqual(website["concurrency"]["group"], "github-pages")
        self.assertNotIn("status site", (WORKFLOWS / "website.yml").read_text().lower())
        deploy = website["jobs"]["deploy"]
        self.assertEqual(deploy["environment"]["name"], "github-pages")
        steps = deploy["steps"]
        uses = [step.get("uses", "") for step in steps]
        by_id = {step["id"]: step for step in steps if "id" in step}
        deploys = [s for s in steps if s.get("uses", "").startswith("actions/deploy-pages@")]
        self.assertEqual(
            [s["id"] for s in deploys],
            ["deployment", "deployment-2", "deployment-3", "deployment-4"],
        )
        self.assertTrue(all(s["continue-on-error"] for s in deploys))
        self.assertNotIn("if", deploys[0])
        # Each deployment attempt is preceded by a readback gate through the
        # deploy-pages lookup, with a fixed increasing backoff.
        gates = ["pages-artifact", "pages-artifact-2", "pages-artifact-3", "pages-artifact-4"]
        waits = []
        for index, (gate_id, attempt) in enumerate(zip(gates, deploys)):
            gate = by_id[gate_id]
            self.assertLess(steps.index(gate), steps.index(attempt))
            self.assertTrue(gate["uses"].startswith("actions/github-script@"))
            self.assertNotIn("continue-on-error", gate)
            source = gate["with"]["script"]
            self.assertIn("'scripts/pages_artifact.py', 'await'", source)
            self.assertNotIn("${{", source)
            self.assertEqual(
                gate["env"]["ARTIFACT_ID"], "${{ steps.pages-upload.outputs.artifact_id }}"
            )
            waits.append(int(gate["env"]["WAIT_SECONDS"]))
            if index:
                condition = f"steps.{deploys[index - 1]['id']}.outcome == 'failure'"
                self.assertEqual(gate["if"], condition)
                self.assertEqual(attempt["if"], condition)
        self.assertEqual(waits, [0, 15, 45, 90])
        self.assertEqual(by_id["pages-artifact"]["env"]["ATTEMPTS"], "6")
        final = steps[-1]
        self.assertEqual(final["if"], "steps.deployment-4.outcome == 'failure'")
        self.assertIn("exit 1", final["run"])
        self.assertIn("::error::", final["run"])
        for attempt in deploys:
            output = f"steps.{attempt['id']}.outputs.page_url"
            self.assertIn(output, deploy["environment"]["url"])
            self.assertIn(output, deploy["outputs"]["page-url"])
        self.assertNotIn("deployment-retry", (WORKFLOWS / "website.yml").read_text())
        self.assertFalse(any("peaceiris" in item for item in uses))


class TestDocsSite(unittest.TestCase):
    def test_just_the_docs_theme_search_and_mermaid(self):
        config = yaml.safe_load((ROOT / "docs/_config.yml").read_text())
        self.assertRegex(config["remote_theme"], r"^just-the-docs/just-the-docs@v\d+\.\d+\.\d+$")
        self.assertNotIn("theme", config)
        self.assertIn("jekyll-remote-theme", config["plugins"])
        self.assertTrue(config["search_enabled"])
        self.assertRegex(config["mermaid"]["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(config["baseurl"], "/porting-agent")
        inventories = {path.name for path in (ROOT / "docs").glob("*.json")}
        self.assertTrue(inventories)
        self.assertLessEqual(inventories, set(config["exclude"]))

    def test_every_docs_page_has_navigation_front_matter(self):
        orders = []
        for path in sorted((ROOT / "docs").rglob("*.md")):
            text = path.read_text()
            self.assertTrue(text.startswith("---\n"), path)
            front = yaml.safe_load(text.split("---\n", 2)[1])
            self.assertIsInstance(front.get("title"), str, path)
            if front.get("parent") is None:
                self.assertIsInstance(front.get("nav_order"), int, path)
                orders.append(front["nav_order"])
        self.assertEqual(len(orders), len(set(orders)), orders)


    def test_rendered_pages_do_not_mention_implementation_milestones(self):
        rendered = [path.read_text() for path in sorted((ROOT / "docs").rglob("*.md"))]
        rendered.append(generate_doc.render([]))
        rendered.append((ROOT / "scripts/generate_doc.py").read_text())
        for text in rendered:
            self.assertIsNone(re.search(r"(?i)\bmilestones?\b|\bM[0-9]+\b", text))


class TestDocsPreviews(unittest.TestCase):
    def setUp(self):
        self.text = (WORKFLOWS / "website.yml").read_text()
        self.website = yaml.safe_load(self.text)
        self.jobs = self.website["jobs"]

    def steps(self):
        return [step for job in self.jobs.values() for step in job.get("steps", [])]

    def test_pull_request_target_is_limited_to_docs_changes(self):
        on = self.website[True]
        self.assertEqual(
            on["pull_request_target"],
            {"types": ["opened", "synchronize", "reopened", "closed"], "paths": ["docs/**"]},
        )
        self.assertNotIn("pull_request", on)
        self.assertIn("workflow_dispatch", on)

    def test_pull_request_code_is_never_checked_out_or_executed(self):
        checkouts = [s for s in self.steps() if s.get("uses", "").startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(checkouts[0]["with"]["ref"], "${{ github.sha }}")
        self.assertFalse(checkouts[0]["with"]["persist-credentials"])
        self.assertNotIn("github.event.pull_request.head.sha", self.text)
        self.assertNotIn("github.event.pull_request.head.ref", self.text)
        self.assertNotIn("git checkout", self.text)
        self.assertNotIn("git switch", self.text)
        fetch = next(s for s in self.steps() if s.get("name", "").startswith("Fetch docs"))
        self.assertIn('"+refs/pull/$number/head:$ref"', fetch["run"])
        self.assertIn('git archive --format=tar "$ref" docs |', fetch["run"])
        self.assertIn('find "$dest" -type l -delete', fetch["run"])
        # Every interpreter call runs a script from the main checkout (or inline
        # trusted code); preview trees are only ever arguments or Jekyll input.
        runs = "\n".join(step.get("run", "") for step in self.steps())
        interpreters = re.findall(r"uv run --locked python (\S+)", runs)
        self.assertTrue(interpreters)
        for target in interpreters:
            self.assertRegex(target, r"^(scripts/[a-z_]+\.py|-)$")
        scripts = "\n".join(
            step["with"]["script"]
            for step in self.steps()
            if step.get("uses", "").startswith("actions/github-script@")
        )
        self.assertEqual(
            set(re.findall(r"'python', '([^']+)'", scripts)), {"scripts/pages_artifact.py"}
        )
        build = next(s for s in self.steps() if s.get("id") == "previews")
        self.assertIn('"$JEKYLL_BUILD_PAGES_IMAGE"', build["run"])
        self.assertIn("-e INPUT_TOKEN= ", build["run"])
        image = self.website["env"]["JEKYLL_BUILD_PAGES_IMAGE"]
        self.assertRegex(image, r"^ghcr\.io/actions/jekyll-build-pages:v1\.0\.13@sha256:[0-9a-f]{64}$")
        self.assertIn(
            "uses: actions/jekyll-build-pages@44a6e6beabd48582f863aeeb6cb2151cc1716697 # v1.0.13",
            self.text,
        )

    def test_previews_only_for_same_repository_pull_requests(self):
        same_repo = "github.event.pull_request.head.repo.full_name == github.repository"
        self.assertIn(same_repo, " ".join(self.jobs["deploy"]["if"].split()))
        self.assertIn(same_repo, " ".join(self.jobs["preview-comment"]["if"].split()))
        self.assertIn("github.ref == 'refs/heads/main'", self.jobs["deploy"]["if"])
        fetch = next(s for s in self.steps() if s.get("name", "").startswith("Fetch docs"))
        self.assertIn('select(.head.repo.full_name == \\"$GITHUB_REPOSITORY\\")', fetch["run"])
        self.assertIn("pulls?state=open", fetch["run"])

    def test_preview_path_and_comment(self):
        build = next(s for s in self.steps() if s.get("id") == "previews")
        self.assertIn('INPUT_DESTINATION="_site/pr/$1"', build["run"])
        self.assertIn('"/porting-agent/pr/$1"', build["run"])
        self.assertIn('--output "$PREVIEW_ROOT/$1/docs/status/index.md"', build["run"])
        status = next(
            s for s in self.steps() if s.get("name") == "Generate status from committed lifecycle state"
        )
        self.assertIn("--output docs/status/index.md", status["run"])
        comment = self.jobs["preview-comment"]
        self.assertEqual(comment["permissions"], {"pull-requests": "write"})
        self.assertEqual(comment["needs"], "deploy")
        script = comment["steps"][0]
        self.assertTrue(script["uses"].startswith("actions/github-script@"))
        source = script["with"]["script"]
        self.assertIn("pr/${number}/", source)
        self.assertIn("<!-- porting-agent-docs-preview -->", source)
        self.assertIn("updateComment", source)
        self.assertNotIn("${{", source)


if __name__ == "__main__":
    unittest.main()
