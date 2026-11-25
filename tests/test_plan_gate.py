# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Missing research cannot become a completed planner task."""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import capacity
import collect
import commit_rules
import controller
import plan_gate
import schemas
import tasks
from common import sha256_text
from tests.helpers import NOW, SHA_A, contribution, policy

ISSUE_URL = "https://github.com/riseproject-dev/porting-agent/issues/12"
REPORT_URL = "https://github.com/riseproject-dev/sw-ecosystem/blob/main/project-reports/proj.md"


def task():
    return {"task": "planning", "contribution_id": "example--proj/12", "revision": 1, "expected_head": None,
            "distributions": ["proj"],
            "context": {"tracking_issue": {"id": 5000, "number": 12, "url": ISSUE_URL,
                                           "description": "Full original issue body, including user links.",
                                           "description_digest": sha256_text("Full original issue body, including user links.")}}}


def proposal():
    return {"schema_version": 1, "contribution_id": "example--proj/12", "expected_revision": 1,
            "expected_head": None, "ref": "riscv64", "operation": "none", "outcome": "planning",
            "progress": "new_causal_evidence", "reconstruction_base": None, "diagnosis": "Completed upstream research."}


REPO = "https://github.com/example/proj"


def guidelines(paths=(".github/workflows/*", "README.rst")):
    """The contribution guidelines of a schema version 2 plan."""
    template = f"{REPO}/blob/main/.github/PULL_REQUEST_TEMPLATE.md"
    contributing = f"{REPO}/blob/main/CONTRIBUTING.md"
    return {
        "pull_request_template": {
            "status": "found",
            "searched": ["PULL_REQUEST_TEMPLATE.md", ".github/PULL_REQUEST_TEMPLATE.md",
                         ".github/PULL_REQUEST_TEMPLATE/", "docs/pull_request_template.md"],
            "sources": [{"url": template, "finding": "The template asks for a summary and testing."}],
            "templates": [{"path": ".github/PULL_REQUEST_TEMPLATE.md", "url": template,
                           "content": "## Summary\n\n## Testing\n\n- [ ] Tests added",
                           "required_sections": ["Summary", "Testing"],
                           "checklist_items": ["Tests added"]}]},
        "contributing": {
            "status": "found",
            "searched": ["CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/contributing.rst",
                         "DEVELOPMENT.md"],
            "sources": [{"url": contributing, "finding": "Contributors add a changelog entry."}],
            "documents": [{"path": "CONTRIBUTING.md", "url": contributing,
                           "summary": "Run the tests and add a changelog entry for every change."}],
            "instructions": ["Add a CHANGELOG entry for every user-visible change."]},
        "commit_conventions": {
            "sources": [{"url": f"{REPO}/commits/main", "finding": "Two hundred recent commits read."}],
            "sample": {"commits": 200, "range": "main, the last 200 commits"},
            "title_lengths": {"median": 42, "p90": 64, "max": 88},
            "rules": {"title_max_length": 72, "title_prefixes": [], "title_prefix_required": False,
                      "title_capitalization": "upper", "title_trailing_period": "forbidden",
                      "body_required": False, "body_line_max_length": 72,
                      "signed_off_by_required": False, "max_commits": None},
            "observations": "Imperative titles, no scopes, issue references as Fixes #N in the body.",
            "examples": ["Add aarch64 wheels to the build matrix"]},
        "reviewers": {
            "status": "found",
            "searched": [".github/CODEOWNERS", "pulls?q=is:merged path:.github/workflows"],
            "sources": [{"url": f"{REPO}/pull/7", "finding": "The maintainer reviewed the wheel matrix."}],
            "paths": [{"path": path, "codeowners": [],
                       "reviewers": [{"login": "maintainer", "role": "Release maintainer",
                                      "review_urls": [f"{REPO}/pull/7"],
                                      "expectations": "Asks for one commit per architecture."}]}
                      for path in paths],
            "expectations": ["Keep the matrix sorted by architecture."]},
        "series_requirements": [],
    }


def project(riscv64="absent"):
    """The project a schema version 3 plan records: a Python project with
    a native extension, released on PyPI."""
    return {"languages": ["Python", "C"], "build_systems": ["setuptools", "cibuildwheel"],
            "build_and_test": "cibuildwheel in .github/workflows/wheels.yml builds and tests the wheels.",
            "release_channels": [{"kind": "pypi", "name": "proj", "url": "https://pypi.org/project/proj/",
                                  "riscv64": riscv64, "evidence": "The 2.4.1 files list on PyPI."}],
            "riscv64_toolchain": "GCC and CPython are packaged for riscv64 in Ubuntu 24.04."}


def go_project():
    """The project of a Go plan: a Go module released with goreleaser."""
    return {"languages": ["Go"], "build_systems": ["go", "goreleaser"],
            "build_and_test": "go build ./... and go test ./... in .github/workflows/ci.yml.",
            "release_channels": [
                {"kind": "go_module", "name": "github.com/example/proj",
                 "url": "https://pkg.go.dev/github.com/example/proj", "riscv64": "not_applicable",
                 "evidence": "A Go module is built from source by its users."},
                {"kind": "github_release", "name": "proj", "url": f"{REPO}/releases/tag/v1.2.0",
                 "riscv64": "absent", "evidence": "The release lists linux amd64 and arm64 archives."}],
            "riscv64_toolchain": "Go supports GOARCH=riscv64 on linux since Go 1.14."}


def plan():
    entry = {"status": "searched", "queries": ["riscv64 all states"],
             "coverage": {"complete": True, "scope": "All matching open and closed discussions reviewed, including all result pages."},
             "sources": [{"url": ISSUE_URL, "finding": "Read the description and all discussion comments."}],
             "findings": "The linked discussions establish architecture requirements and prior reviewer decisions."}
    d = {"schema_version": 4, "contribution_id": "example--proj/12", "expected_revision": 1,
         "tracking_issue_description_digest": task()["context"]["tracking_issue"]["description_digest"],
         "summary": "Research covers project history, dependencies, documentation and maintainer review decisions.",
         "research": {category: copy.deepcopy(entry) for category in plan_gate.RESEARCH_CATEGORIES},
         "history": [], "maintainers": [{"name": "reviewer", "role": "Release maintainer and architecture reviewer", "sources": [ISSUE_URL]}],
         "dependency_graph": {"tool": "project_graph_query", "success": True, "canonical_project": "proj",
                              "query": {"cypher": "MATCH (p) RETURN p"},
                              "result": {"projects": ["proj"]},
                              "assessment": "Compared the canonical graph project with build and runtime dependencies."},
         "ecosystem_report": {"status": "available", "url": REPORT_URL, "revision": "abc123",
                              "riscv64_status": "The report documents native support gaps and the current upstream CI status.",
                              "used_findings": {
                                  "support_status": "Used and verified the report's riscv64 support-status findings.",
                                  "dependencies": "Used the report's graph findings and checked them against the build configuration.",
                                  "prior_work_and_blockers": "Followed and reviewed the report's linked prior contributions and architecture blockers."},
                              "corroboration_and_conflicts": [ISSUE_URL]},
         "proposal": {"decision": "develop", "rationale": "Add bounded wheel CI configuration following the verified architecture precedent.",
                      "admission_class": "configuration_integration", "allowed_paths": [".github/workflows/*", "README.rst"],
                      "path_coverage": [
                          {"area": "build", "paths": [".github/workflows/wheels.yml"], "assessment": "The wheel workflow owns architecture build rows."},
                          {"area": "test", "paths": [".github/workflows/test.yml"], "assessment": "The test matrix mirrors supported build architectures."},
                          {"area": "release", "paths": [".github/workflows/release.yml"], "assessment": "The release workflow consumes validated wheel artifacts."},
                          {"area": "packaging", "paths": [], "assessment": "Packaging metadata requires no architecture-specific change."},
                          {"area": "documentation", "paths": ["README.rst"], "assessment": "The support table documents shipped architectures."},
                          {"area": "native_sources", "paths": [], "assessment": "No architecture-specific native source dispatch exists."}
                      ]}}
    d["research"]["ecosystem_report"]["sources"][0]["url"] = REPORT_URL
    d["contribution_guidelines"] = guidelines()
    d["project"] = project()
    return d


class TestPlanCompleteness(unittest.TestCase):
    def test_complete_plan_accepted(self):
        plan_gate.validate(plan(), proposal(), task())

    def test_agent_preflight_cli_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, value in (("plan", plan()), ("proposal", proposal()), ("task", task())):
                (root / f"{name}.json").write_text(json.dumps(value))
            command = [sys.executable, "-m", "plan_gate",
                       "--plan", str(root / "plan.json"), "--proposal", str(root / "proposal.json"),
                       "--task", str(root / "task.json")]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            invalid = plan(); invalid["history"] = [[ISSUE_URL, "open"]]
            (root / "plan.json").write_text(json.dumps(invalid))
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)

    def test_development_decision_requires_bounded_scope_and_park_is_empty(self):
        for path in ("*", "**", "../setup.py", "/tmp/setup.py", ".git/config", "src/**"):
            d = plan(); d["proposal"]["allowed_paths"] = [path]
            with self.assertRaises((plan_gate.PlanError, schemas.SchemaError), msg=path):
                plan_gate.validate(d, proposal(), task())
        d = plan()
        d["proposal"].update(decision="park", admission_class=None, allowed_paths=[],
                             rationale="No concrete patch is actionable until a documented external prerequisite is resolved.")
        plan_gate.validate(d, proposal(), task())
        d["proposal"]["allowed_paths"] = ["pyproject.toml"]
        with self.assertRaises(schemas.SchemaError):
            plan_gate.validate(d, proposal(), task())

    def test_a_close_decision_has_no_class_and_no_paths(self):
        d = plan()
        d["project"] = project(riscv64="published")
        d["proposal"].update(decision="close", admission_class=None, allowed_paths=[],
                             rationale="The latest release 2.4.1 publishes a manylinux riscv64 wheel on PyPI.")
        plan_gate.validate(d, proposal(), task())
        for change in ({"admission_class": "configuration_integration"},
                       {"allowed_paths": ["pyproject.toml"]}):
            with self.subTest(change=change), self.assertRaises(schemas.SchemaError):
                plan_gate.validate(dict(d, proposal=dict(d["proposal"], **change)),
                                      proposal(), task())

    def test_allowed_paths_cover_every_researched_riscv64_path(self):
        d = plan()
        d["proposal"]["allowed_paths"] = [".github/workflows/wheels.yml", "README.rst"]
        with self.assertRaisesRegex(plan_gate.PlanError, "omit researched"):
            plan_gate.validate(d, proposal(), task())
        d = plan()
        d["proposal"]["path_coverage"][1]["area"] = "build"
        with self.assertRaisesRegex(plan_gate.PlanError, "each required area"):
            plan_gate.validate(d, proposal(), task())

    def test_the_contribution_guidelines_are_required(self):
        d = plan()
        del d["contribution_guidelines"]
        with self.assertRaises(schemas.SchemaError):
            plan_gate.validate(d, proposal(), task())
        d["schema_version"] = 1
        with self.assertRaisesRegex(plan_gate.PlanError, "schema version 4"):
            plan_gate.validate(d, proposal(), task())
        for section in ("pull_request_template", "contributing", "commit_conventions", "reviewers"):
            d = plan()
            del d["contribution_guidelines"][section]
            with self.assertRaises(schemas.SchemaError, msg=section):
                plan_gate.validate(d, proposal(), task())

    def test_none_found_needs_the_record_of_the_search(self):
        d = plan()
        g = d["contribution_guidelines"]
        g["pull_request_template"].update(status="none_found", templates=[])
        g["contributing"].update(status="none_found", documents=[], instructions=[])
        plan_gate.validate(d, proposal(), task())
        for section, missing in (("pull_request_template", "docs/pull_request_template.md"),
                                 ("contributing", "DEVELOPMENT.md")):
            bad = copy.deepcopy(d)
            bad["contribution_guidelines"][section]["searched"].remove(missing)
            with self.assertRaisesRegex(plan_gate.PlanError, "does not cover", msg=section):
                plan_gate.validate(bad, proposal(), task())
        bad = copy.deepcopy(d)
        bad["contribution_guidelines"]["pull_request_template"]["status"] = "found"
        with self.assertRaisesRegex(plan_gate.PlanError, "found template"):
            plan_gate.validate(bad, proposal(), task())
        d = plan()
        for entry in d["contribution_guidelines"]["reviewers"]["paths"]:
            entry["reviewers"] = []
        with self.assertRaisesRegex(plan_gate.PlanError, "only when found"):
            plan_gate.validate(d, proposal(), task())
        d["contribution_guidelines"]["reviewers"]["status"] = "none_found"
        plan_gate.validate(d, proposal(), task())

    def test_found_documents_are_cited_and_rules_are_measurable(self):
        d = plan()
        d["contribution_guidelines"]["pull_request_template"]["templates"][0]["url"] = REPO + "/x"
        with self.assertRaisesRegex(plan_gate.PlanError, "not cited"):
            plan_gate.validate(d, proposal(), task())
        d = plan()
        d["contribution_guidelines"]["commit_conventions"]["rules"].update(
            title_prefix_required=True, title_prefixes=[])
        with self.assertRaisesRegex(plan_gate.PlanError, "commit_conventions"):
            plan_gate.validate(d, proposal(), task())
        d = plan()
        d["contribution_guidelines"]["commit_conventions"]["title_lengths"]["median"] = 99
        with self.assertRaisesRegex(plan_gate.PlanError, "not ordered"):
            plan_gate.validate(d, proposal(), task())
        d = plan()
        d["contribution_guidelines"]["commit_conventions"]["rules"]["title_max_length"] = 5
        with self.assertRaises(schemas.SchemaError):
            plan_gate.validate(d, proposal(), task())

    def test_every_changed_path_has_reviewer_research(self):
        d = plan()
        d["contribution_guidelines"]["reviewers"]["paths"].pop()
        with self.assertRaisesRegex(plan_gate.PlanError, "README.rst"):
            plan_gate.validate(d, proposal(), task())

    def test_every_category_is_required(self):
        for category in plan_gate.RESEARCH_CATEGORIES:
            d = plan()
            del d["research"][category]
            with self.assertRaises(schemas.SchemaError, msg=category):
                plan_gate.validate(d, proposal(), task())

    def test_inaccessible_partial_or_unsupported_research_rejected(self):
        for update in ({"status": "unavailable"}, {"sources": []}, {"queries": []},
                       {"coverage": {"complete": False, "scope": "Only the first search page was read."}}):
            d = plan()
            d["research"]["upstream_pull_requests"].update(update)
            with self.assertRaises(schemas.SchemaError):
                plan_gate.validate(d, proposal(), task())
        d = plan()
        d["research"]["upstream_issues"].update(status="not_applicable", justification="Did not attempt an upstream issue search for this project.")
        with self.assertRaises(plan_gate.PlanError):
            plan_gate.validate(d, proposal(), task())

    def test_missing_graph_or_unused_report_rejected(self):
        d = plan()
        del d["dependency_graph"]
        with self.assertRaises(schemas.SchemaError):
            plan_gate.validate(d, proposal(), task())
        d = plan()
        d["research"]["ecosystem_report"]["sources"][0]["url"] = ISSUE_URL
        with self.assertRaises(plan_gate.PlanError):
            plan_gate.validate(d, proposal(), task())
        d = plan()
        d["ecosystem_report"] = {"status": "absent"}
        with self.assertRaises(plan_gate.PlanError):
            plan_gate.validate(d, proposal(), task())
        d["research"]["ecosystem_report"].update(status="not_applicable", justification="Searched report index, canonical name, repository and synonyms; no report exists.")
        plan_gate.validate(d, proposal(), task())

    def test_stale_issue_and_task_identity_rejected(self):
        for field, value in (("expected_revision", 0), ("contribution_id", "other/12"),
                             ("tracking_issue_description_digest", "0" * 64)):
            d = plan()
            d[field] = value
            with self.assertRaises(plan_gate.PlanError):
                plan_gate.validate(d, proposal(), task())
        d = plan()
        d["research"]["tracking_issue"]["sources"][0]["url"] = REPORT_URL
        with self.assertRaises(plan_gate.PlanError):
            plan_gate.validate(d, proposal(), task())

    def test_exact_claim_staging_and_cancellation(self):
        assignment = {"version": 3, "dispatch_id": "d1", "claims": [{"claim_id": "c1", "work_id": "w1", "handle": "h1", "work": task()}]}
        identity = {"dispatch_id": "d1", "claim_id": "c1", "work_id": "w1", "claim_handle": "h1"}
        ns = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(ns, "7a521a54e115fee86af74f06e7e1636613643950ecc9f57a6e82039311a49d94")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = root / "claims" / ns / "porting-out"
            stage.mkdir(parents=True)
            (stage / "plan.json").write_text(json.dumps(plan()))
            (stage / "proposal.json").write_text(json.dumps(proposal()))
            finish = root / "work-queue.finish.jsonl"
            finish.write_text(json.dumps({"version": 3, "kind": "finish", "claim_handle": "h1", "parameters": {"outcome": "completed"}}))
            self.assertIn("eligible", plan_gate.check_stage(assignment, root, root))
            (stage / "plan.json").unlink()
            with self.assertRaises(plan_gate.PlanError):
                plan_gate.check_stage(assignment, root, root)
            finish.write_text(json.dumps({"version": 3, "kind": "finish", "claim_handle": "h1", "parameters": {"outcome": "cancelled"}}))
            self.assertIn("cancelled", plan_gate.check_stage(assignment, root, root))
            finish.write_text(finish.read_text() + "\n" + json.dumps({"version": 3, "kind": "finish", "claim_handle": "h1", "parameters": {"outcome": "completed"}}))
            with self.assertRaises(plan_gate.PlanError):
                plan_gate.check_stage(assignment, root, root)

    def test_symlinked_inputs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "link").symlink_to(root, target_is_directory=True)
            with self.assertRaises(plan_gate.PlanError):
                plan_gate._read(root, Path("link/anything.json"))


class TestImmutableTrackingIssue(unittest.TestCase):
    def test_authorized_human_relay_enables_natural_review_scope_extension(self):
        rec = contribution(activity="agent_running")
        rec["active_work"] = {"task": "development_repair", "reason_ids": ["review-1"],
                              "requested_at": NOW, "revision": rec["revision"],
                              "dedupe_key": "repair", "deadline_at": NOW, "generation": 0, "attempt": 1, "dispatched_run_id": None, "feedback_evidence_ids": []}
        request = tasks.request_from_active(rec)
        node = tasks.node(request, rec, control_repo_id="7", allowed_paths=["build.yml", "test.yml"],
                         context={"reason_evidence": [{"evidence_id": "review-1",
                                                       "kind": "human_relay",
                                                       "summary": "Add the test workflow."}]})
        self.assertTrue(node["payload"]["review_scope_extension"])
        self.assertEqual(node["payload"]["allowed_paths"], ["build.yml", "test.yml"])
        ordinary = tasks.node(request, rec, control_repo_id="7", allowed_paths=["build.yml"],
                             context={"reason_evidence": [{"evidence_id": "ci-1", "kind": "ci"}]})
        self.assertFalse(ordinary["payload"]["review_scope_extension"])
        self.assertTrue(ordinary["payload"]["legacy_scope_extension"])
        rec["scope_coverage_complete"] = True
        complete = tasks.node(request, rec, control_repo_id="7", allowed_paths=["build.yml"],
                             context={"reason_evidence": [{"evidence_id": "ci-1", "kind": "ci"}]})
        self.assertFalse(complete["payload"]["legacy_scope_extension"])

    def test_refresh_planning_freezes_reason_and_existing_pr(self):
        rec = contribution(phase="planning", activity="agent_running")
        rec["refresh"] = {"command_id": "comment-9", "reason": "audit dependencies",
                          "from_phase": "development", "head_sha": SHA_A, "pr_number": 1}
        rec["active_work"] = {"task": "planning", "reason_ids": [],
                              "requested_at": NOW, "revision": rec["revision"],
                              "dedupe_key": "refresh", "deadline_at": NOW, "generation": 0, "attempt": 1, "dispatched_run_id": None, "feedback_evidence_ids": []}
        node = tasks.node(tasks.request_from_active(rec), rec, control_repo_id="7", allowed_paths=[])
        self.assertEqual(node["payload"]["context"]["refresh"]["reason"], "audit dependencies")
        self.assertEqual(node["payload"]["context"]["existing_fork"]["pr_number"], 1)
        self.assertEqual(node["payload"]["expected_head"], SHA_A)

    def test_full_description_url_and_links_frozen_into_task(self):
        body = "### Additional context\n\nhttps://github.com/example/proj/pull/31488\n" + "user context\n" * 500
        rec = contribution(phase="planning", activity="idle")
        # A record created before the description was frozen at intake.
        rec["tracking_issue"] = {"id": 5000, "number": 12}
        issue = {"id": 5000, "number": 12, "body": body}
        result = controller.tick(contributions={"c": rec}, operations={}, consumed={}, projects={}, policy=policy(read_only=False),
                                 inputs=controller.Inputs(now=datetime.fromisoformat(NOW), pressure=capacity.Pressure(ok=True),
                                                          tracking_issues={12: issue}))
        claimed = [r for k, r in result.staged if k == "contributions"][-1]
        node = tasks.node(result.requested_effects[0], claimed, control_repo_id="7", allowed_paths=[".github/workflows/*"])
        frozen = node["payload"]["context"]["tracking_issue"]
        self.assertEqual(frozen["description"], body)
        self.assertEqual(frozen["url"], ISSUE_URL)
        issue["body"] = "subsequent edit"
        self.assertEqual(node["payload"]["context"]["tracking_issue"]["description"], body)

    def test_issue_identity_and_oversized_description_rejected_not_truncated(self):
        expected = {"id": 5000, "number": 12}
        with self.assertRaises(collect.CollectError):
            collect.tracking_issue_context({"id": 5001, "number": 12, "body": "x"}, expected)
        with self.assertRaises(collect.CollectError):
            collect.tracking_issue_context({"id": 5000, "number": 12, "body": "x" * 16385}, expected)


def pypi(version, *files, source=REPO):
    urls = [{"filename": f, "yanked": False} if isinstance(f, str)
            else {"filename": f[0], "yanked": f[1]} for f in files]
    return {"info": {"version": version, "project_urls": {"Source": source}}, "urls": urls}


class TestCloseConfirmation(unittest.TestCase):
    RISCV = "proj-2.4.1-cp312-cp312-manylinux_2_39_riscv64.whl"

    def confirm(self, documents, names=("proj",), upstream="example/proj"):
        return plan_gate.close_confirmation(list(names), documents, upstream=upstream)

    def test_a_riscv64_wheel_confirms_already_released(self):
        for wheel in (self.RISCV, "proj-2.4.1-cp312-cp312-musllinux_1_2_riscv64.whl",
                      "proj-2.4.1-cp312-cp312-linux_riscv64.whl"):
            with self.subTest(wheel=wheel):
                reason, problem, finding = self.confirm(
                    {"proj": pypi("2.4.1", "proj-2.4.1.tar.gz", wheel)})
                self.assertEqual((reason, problem), ("already_released", None))
                self.assertEqual(finding, {"proj": {"version": "2.4.1", "served_by": "riscv64",
                                                    "wheel": wheel}})

    def test_pure_python_wheels_only_confirm_no_work(self):
        reason, problem, finding = self.confirm(
            {"proj": pypi("2.4.1", "proj-2.4.1-py3-none-any.whl", "proj-2.4.1.tar.gz")})
        self.assertEqual((reason, problem), ("no_work", None))
        self.assertEqual(finding["proj"]["served_by"], "pure")

    def test_one_riscv64_and_one_pure_distribution_is_already_released(self):
        reason, problem, _finding = self.confirm(
            {"proj": pypi("2.4.1", self.RISCV),
             "proj-data": pypi("1.0", "proj_data-1.0-py3-none-any.whl")},
            names=("proj", "proj-data"))
        self.assertEqual((reason, problem), ("already_released", None))

    def test_what_does_not_confirm_a_close(self):
        cases = {
            "platform wheels without riscv64": pypi(
                "2.4.1", "proj-2.4.1-cp312-cp312-manylinux_2_17_x86_64.whl",
                "proj-2.4.1-cp312-cp312-manylinux_2_17_aarch64.whl"),
            "sdist only": pypi("2.4.1", "proj-2.4.1.tar.gz"),
            "a yanked riscv64 wheel only": pypi("2.4.1", (self.RISCV, True)),
            "a riscv64 wheel of an older release only": pypi(
                "2.4.1", "proj-2.4.0-cp312-cp312-manylinux_2_39_riscv64.whl"),
            "no project": None,
            "a malformed document": {"info": "x", "urls": {}},
            "no valid version": {"info": {"version": "not a version"}, "urls": []},
            "a list": [],
            "a malformed wheel name only": pypi("2.4.1", "proj-2.4.1-x.whl", "-.whl"),
        }
        for name, document in cases.items():
            with self.subTest(case=name):
                reason, problem, _finding = self.confirm({"proj": document})
                self.assertIsNone(reason)
                self.assertTrue(problem)
        reason, problem, finding = self.confirm({}, names=())
        self.assertEqual((reason, problem, finding), (None, "no distribution is recorded", {}))

    def test_a_malformed_wheel_name_is_ignored(self):
        reason, problem, finding = self.confirm(
            {"proj": pypi("2.4.1", "proj-2.4.1-x.whl", "proj-2.4.1-py3-none-any.whl")})
        self.assertEqual((reason, problem), ("no_work", None))
        self.assertEqual(finding["proj"]["wheel"], "proj-2.4.1-py3-none-any.whl")

    def test_the_pypi_project_must_name_the_upstream_repository(self):
        wheel = "proj-2.4.1-py3-none-any.whl"
        for source in ("https://github.com/example/proj", "https://github.com/Example/Proj.git",
                       "https://www.github.com/example/proj/tree/main", "http://github.com/example/proj/"):
            with self.subTest(source=source):
                reason, problem, _ = self.confirm({"proj": pypi("2.4.1", wheel, source=source)})
                self.assertEqual((reason, problem), ("no_work", None))
        document = pypi("2.4.1", wheel)
        document["info"] = {"version": "2.4.1", "home_page": "https://github.com/example/proj"}
        self.assertEqual(self.confirm({"proj": document})[:2], ("no_work", None))
        document["info"] = {"version": "2.4.1", "download_url": "https://github.com/example/proj/releases"}
        self.assertEqual(self.confirm({"proj": document})[:2], ("no_work", None))
        for source in ("https://github.com/other/proj", "https://github.com/example/proj2",
                       "https://gitlab.com/example/proj", "https://github.com/example",
                       "https://github.example.com/example/proj", "not a url", None, 7):
            with self.subTest(source=source):
                reason, problem, _ = self.confirm({"proj": pypi("2.4.1", wheel, source=source)})
                self.assertIsNone(reason)
                self.assertEqual(problem, "the PyPI project proj does not name the upstream repository")
        # A same-named PyPI project of another repository confirms nothing.
        self.assertIsNone(self.confirm({"proj": pypi("2.4.1", wheel)}, upstream="someone/proj")[0])
        for upstream in (None, "", "example", "a/b/c"):
            with self.subTest(upstream=upstream):
                self.assertIsNone(self.confirm({"proj": pypi("2.4.1", wheel)}, upstream=upstream)[0])

    def test_malformed_metadata_never_raises(self):
        wheel = "proj-2.4.1-py3-none-any.whl"
        for info_extra in ({"project_urls": "x"}, {"project_urls": None}, {"project_urls": {"a": 1}},
                           {"project_urls": {"a": "https://[bad"}}, {"home_page": ["x"]},
                           {"project_urls": {str(n): "x" for n in range(500)}}):
            with self.subTest(info=info_extra):
                document = pypi("2.4.1", wheel)
                document["info"] = {"version": "2.4.1", **info_extra}
                reason, problem, _ = self.confirm({"proj": document})
                self.assertIsNone(reason)
                self.assertTrue(problem)


class TestProjectContract(unittest.TestCase):
    def test_stored_plans_of_earlier_versions_stay_schema_valid(self):
        v2 = plan()
        v2["schema_version"] = 2
        del v2["project"]
        schemas.validate("plan", v2)
        v1 = copy.deepcopy(v2)
        v1["schema_version"] = 1
        del v1["contribution_guidelines"]
        schemas.validate("plan", v1)
        # ... but no new plan of version 2 is admitted.
        with self.assertRaisesRegex(plan_gate.PlanError, "schema version 4"):
            plan_gate.validate(v2, proposal(), task())
        v3 = plan()
        del v3["project"]
        with self.assertRaises(schemas.SchemaError):
            plan_gate.validate(v3, proposal(), task())

    def test_the_project_is_bounded(self):
        for change in ({"languages": []}, {"languages": ["x" * 41]}, {"build_systems": []},
                       {"build_and_test": "x" * 4001}, {"riscv64_toolchain": ""},
                       {"release_channels": [{"kind": "pip", "name": "p", "url": REPO,
                                              "riscv64": "absent", "evidence": "e"}]},
                       {"release_channels": [{"kind": "pypi", "name": "p", "url": "file:///x",
                                              "riscv64": "absent", "evidence": "e"}]},
                       {"release_channels": [{"kind": "pypi", "name": "p", "url": REPO,
                                              "riscv64": "maybe", "evidence": "e"}]},
                       {"release_channels": [{"kind": "pypi", "name": "p", "url": REPO,
                                              "riscv64": "absent", "evidence": "e", "x": 1}]},
                       {"unknown": "x"}):
            with self.subTest(change=change):
                d = plan()
                d["project"].update(change)
                with self.assertRaises(schemas.SchemaError):
                    plan_gate.validate(d, proposal(), task())

    def test_a_close_is_refused_unless_pypi_can_confirm_it(self):
        def close(project_, task_=None):
            d = plan()
            d["project"] = project_
            d["proposal"].update(decision="close", admission_class=None, allowed_paths=[],
                                 rationale="The latest release already serves riscv64 for every user.")
            plan_gate.validate(d, proposal(), task_ or task())
        close(project(riscv64="published"))
        without_pypi = go_project()
        without_pypi["release_channels"][1]["riscv64"] = "published"
        absent = project(riscv64="published")
        absent["release_channels"].append(
            {"kind": "container_image", "name": "ghcr.io/example/proj", "url": REPO,
             "riscv64": "absent", "evidence": "The image manifest has no linux/riscv64."})
        no_names = dict(task(), distributions=[])
        for name, args in (("no pypi channel", (without_pypi,)), ("an absent channel", (absent,)),
                           ("no distributions", (project(riscv64="published"), no_names)),
                           ("no channel at all", (dict(project(), release_channels=[]),))):
            with self.subTest(case=name), self.assertRaisesRegex(
                    plan_gate.PlanError, "confirmed from PyPI only"):
                close(*args)

    def test_a_go_project_is_planned_and_developed(self):
        d = plan()
        d["project"] = go_project()
        d["proposal"].update(
            decision="develop", admission_class="architecture_correctness",
            allowed_paths=[".goreleaser.yaml", "internal/arch/*"],
            rationale="Add riscv64 to the goreleaser targets and the architecture switch of the loader.",
            path_coverage=[
                {"area": "build", "paths": [".goreleaser.yaml"], "assessment": "goreleaser lists the goarch values."},
                {"area": "test", "paths": [], "assessment": "go test runs on the host architecture only."},
                {"area": "release", "paths": [".goreleaser.yaml"], "assessment": "The same file publishes the archives."},
                {"area": "packaging", "paths": [], "assessment": "go.mod has no architecture-specific content."},
                {"area": "documentation", "paths": [], "assessment": "The README names no architectures."},
                {"area": "native_sources", "paths": ["internal/arch/riscv64.go"],
                 "assessment": "The loader switches on runtime.GOARCH and has no riscv64 case."}])
        d["contribution_guidelines"] = guidelines(paths=(".goreleaser.yaml", "internal/arch/*"))
        plan_gate.validate(d, proposal(), dict(task(), distributions=[]))


class TestStoredPlansStayValid(unittest.TestCase):
    """Every committed plan, of every schema version, stays valid under the
    current plan contract: no stored plan needs a migration."""

    def test_every_committed_plan_validates(self):
        root = Path(__file__).resolve().parent.parent
        paths = [*sorted((root / ".state/evidence").glob("*.json")),
                 *sorted((root / "tests/fixtures/plan-rename/evidence").glob("*.json")),
                 root / "tests/fixtures/live-handoff-r6/plan.json",
                 root / "tests/fixtures/canary-m5-plan.json"]
        seen = 0
        for path in paths:
            record = json.loads(path.read_text())
            if record.get("kind") not in ("plan", "dossier"):
                continue
            with self.subTest(path=path.name):
                schemas.validate("plan", record["data"])
                seen += 1
        self.assertGreaterEqual(seen, 4)


def changelog_requirement(**change):
    """A series requirement of a version 4 plan: the series changes the
    changelog, as the contributing instructions ask."""
    return {"id": "changelog", "description": "Add a CHANGELOG entry for every user-visible change.",
            "source_url": f"{REPO}/blob/main/CONTRIBUTING.md", "paths": ["CHANGELOG.md"], **change}


class TestSeriesRequirements(unittest.TestCase):
    def plan(self, *requirements):
        d = plan()
        d["proposal"]["allowed_paths"].append("CHANGELOG.md")
        d["contribution_guidelines"]["reviewers"]["paths"].append(
            copy.deepcopy(d["contribution_guidelines"]["reviewers"]["paths"][0]) | {"path": "CHANGELOG.md"})
        d["contribution_guidelines"]["series_requirements"] = list(requirements)
        return d

    def test_a_requirement_in_scope_that_cites_the_guidelines_is_admitted(self):
        plan_gate.validate(self.plan(changelog_requirement()), proposal(), task())
        plan_gate.validate(self.plan(), proposal(), task())
        self.assertEqual(commit_rules.series_requirements_from_plan(self.plan(changelog_requirement())),
                         [{"id": "changelog", "paths": ["CHANGELOG.md"],
                           "description": "Add a CHANGELOG entry for every user-visible change."}])

    def test_the_requirements_are_recorded_unique_cited_and_in_scope(self):
        d = self.plan()
        del d["contribution_guidelines"]["series_requirements"]
        with self.assertRaisesRegex(plan_gate.PlanError, "possibly none"):
            plan_gate.validate(d, proposal(), task())
        with self.assertRaisesRegex(plan_gate.PlanError, "unique id"):
            plan_gate.validate(self.plan(changelog_requirement(), changelog_requirement()),
                               proposal(), task())
        with self.assertRaisesRegex(plan_gate.PlanError, "cites a source"):
            plan_gate.validate(self.plan(changelog_requirement(source_url=f"{REPO}/wiki")),
                               proposal(), task())
        with self.assertRaisesRegex(plan_gate.PlanError, "not allowed_paths"):
            plan_gate.validate(self.plan(changelog_requirement(paths=["NEWS.rst"])),
                               proposal(), task())
        for change in ({"id": "Changelog"}, {"paths": []}, {"paths": ["a", "b", "c", "d"]},
                       {"description": "short"}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(schemas.SchemaError):
                plan_gate.validate(self.plan(changelog_requirement(**change)), proposal(), task())
        with self.assertRaises(schemas.SchemaError):
            plan_gate.validate(self.plan(*(changelog_requirement(id=f"r{i}") for i in range(7))),
                               proposal(), task())

    def test_a_park_or_close_records_none(self):
        d = self.plan(changelog_requirement())
        d["proposal"].update(decision="park", admission_class=None, allowed_paths=[],
                             rationale="No concrete patch is actionable until a documented external prerequisite is resolved.")
        with self.assertRaisesRegex(plan_gate.PlanError, "park or close records none"):
            plan_gate.validate(d, proposal(), task())
        d["contribution_guidelines"]["series_requirements"] = []
        plan_gate.validate(d, proposal(), task())

    def test_plans_of_earlier_versions_record_none(self):
        for version in (1, 2, 3):
            d = self.plan(changelog_requirement())
            d["schema_version"] = version
            self.assertEqual(commit_rules.series_requirements_from_plan(d), [], version)
        self.assertEqual(commit_rules.series_requirements_from_plan(None), [])
        d = self.plan()
        del d["contribution_guidelines"]["series_requirements"]
        self.assertEqual(commit_rules.series_requirements_from_plan(d), [])
        for bad in ("x", [1], [changelog_requirement(paths="CHANGELOG.md")],
                    [changelog_requirement(description="a\x00b" * 5)]):
            d = self.plan()
            d["contribution_guidelines"]["series_requirements"] = bad
            with self.subTest(bad=bad), self.assertRaises(commit_rules.RuleError):
                commit_rules.series_requirements_from_plan(d)

    def test_a_requirement_is_met_by_a_matching_changed_path(self):
        reqs = [{"id": "changelog", "paths": ["CHANGELOG.md"], "description": "x" * 10},
                {"id": "news", "paths": ["changes/*.rst"], "description": "y" * 10}]
        met, unmet = commit_rules.changed_paths_by_requirement(
            reqs, ["setup.py", "changes/12.feature.rst"])
        self.assertEqual(met, ["news"])
        self.assertEqual([r["id"] for r in unmet], ["changelog"])
