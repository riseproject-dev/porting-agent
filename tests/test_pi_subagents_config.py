# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The Pi settings the worker's lock compiles are accepted by the pinned
``pi-subagents``.

The worker's ``engine.config`` reaches Pi as ``GH_AW_PI_CONFIG`` and lands in
``/tmp/gh-aw/pi-agent-dir/settings.json``. pi-subagents validates every
``subagents.agentOverrides`` entry when it loads, and an invalid one fails the
whole extension: no ``subagent`` tool exists in the run (run 45577808226: the
comma-separated ``"tools"`` string of an agent file was refused, and every
reviewer launch returned the settings error).

``validate_override`` mirrors, rule for rule, ``parseBuiltinOverrideEntry`` of
pi-subagents 0.89.0 (``src/agents/agents.js`` of the package, which is
``bundle/chunk-GHJ6BH5U.js`` lines 4899 to 5075 in the published bundle:
``parseOverrideStringArrayOrFalse``, ``parseToolsOverride``,
``validateOptionalMachine``, ``parseBuiltinOverrideEntry``, with the field list
``BUILTIN_OVERRIDE_FIELDS`` at line 4314 and ``THINKING_LEVELS`` at line 803).
The package is not vendored and the tests run offline, so its code is not run;
a bump of the pin in ``scripts/dependencies/package.json`` means rereading
those functions."""

import json
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
PINNED = "pi-subagents@0.89.0"

FIELDS = {
    "description", "advertise", "machine", "output", "outputMode", "defaultReads", "model",
    "defaultProvider", "fast", "thinking", "systemPromptMode", "inheritProjectContext",
    "inheritGlobalContext", "inheritSkills", "defaultContext", "acceptanceRole", "disabled",
    "systemPrompt", "skills", "tools", "excludeTools", "allowNestedSubagents", "allowedAgents",
    "extensions", "subagentOnlyExtensions", "mutationTools", "toolBudget",
}
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_ARRAY_FIELDS = ("defaultReads", "skills", "excludeTools", "allowedAgents", "extensions",
                 "subagentOnlyExtensions", "mutationTools")


class Invalid(ValueError):
    pass


def _string_array_or_false(value, field):
    if value is None or value is False:
        return
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise Invalid(f"invalid {field!r}; expected an array of strings or false")


def _is_bool(value):
    return isinstance(value, bool)


def _non_empty_string(value):
    return isinstance(value, str) and bool(value.strip())


def validate_override(name: str, value) -> None:
    """Raise ``Invalid`` where ``parseBuiltinOverrideEntry`` throws."""
    if not isinstance(value, dict):
        raise Invalid(f"override {name!r} must be an object")
    for removed in ("fallbackModels", "launcher"):
        if removed in value:
            raise Invalid(f"override {name!r} sets {removed!r}, which is not supported")
    for key in value:
        if key not in FIELDS:
            raise Invalid(f"override {name!r} has unknown field {key!r}")
    checks = {
        "description": _non_empty_string,
        "advertise": _is_bool,
        "output": lambda v: _non_empty_string(v) or v is False,
        "outputMode": lambda v: v in ("inline", "file-only"),
        "model": lambda v: isinstance(v, str) or v is False,
        "fast": _is_bool,
        "thinking": lambda v: v in THINKING_LEVELS or v is False,
        "systemPromptMode": lambda v: v in ("append", "replace"),
        "inheritProjectContext": _is_bool,
        "inheritGlobalContext": _is_bool,
        "inheritSkills": _is_bool,
        "defaultContext": lambda v: v in ("fresh", "fork") or v is False,
        "acceptanceRole": lambda v: v in ("read-only", "writer") or v is False,
        "disabled": _is_bool,
        "toolBudget": lambda v: v is False or isinstance(v, dict),
        "systemPrompt": lambda v: isinstance(v, str),
        "allowNestedSubagents": _is_bool,
        "defaultProvider": lambda v: v is False or _non_empty_string(v),
        "machine": lambda v: v is False or (
            isinstance(v, str) and bool(v.strip()) and len(v.strip()) <= 128
            and not re.search(r"[\x00-\x1f\x7f]", v.strip())),
    }
    for key, ok in checks.items():
        if key in value and not ok(value[key]):
            raise Invalid(f"override {name!r} has invalid {key!r}")
    for key in _ARRAY_FIELDS:
        _string_array_or_false(value.get(key), key)
    tools = value.get("tools")
    if not (tools is None or tools is False or (isinstance(tools, str) and tools.strip() == "inherit")):
        if not isinstance(tools, list):
            raise Invalid(f"override {name!r} has invalid 'tools'; expected an array of "
                          'strings, "inherit", or false')
        _string_array_or_false(tools, "tools")


def validate_settings(settings: dict) -> None:
    """The ``subagents`` part of a Pi settings file, as ``readSubagentSettings``
    reads the keys the worker sets."""
    subagents = settings.get("subagents")
    if not isinstance(subagents, dict):
        return
    extensions = subagents.get("defaultSubagentOnlyExtensions")
    if extensions is not None:
        _string_array_or_false(extensions, "defaultSubagentOnlyExtensions")
    overrides = subagents.get("agentOverrides")
    if isinstance(overrides, dict):
        for name, value in overrides.items():
            validate_override(name, value)


def lock_pi_config(name: str = "worker") -> dict:
    """The ``GH_AW_PI_CONFIG`` of the compiled lock: one value, in the agent step."""
    lock = yaml.safe_load((WORKFLOWS / f"{name}.lock.yml").read_text())
    found = {step["env"]["GH_AW_PI_CONFIG"]
             for job in lock["jobs"].values() for step in job.get("steps", [])
             if isinstance(step.get("env"), dict) and "GH_AW_PI_CONFIG" in step["env"]}
    assert len(found) == 1, found
    return json.loads(found.pop())


class TestMirror(unittest.TestCase):
    def test_the_string_form_of_run_45577808226_is_refused(self):
        with self.assertRaisesRegex(Invalid, "invalid 'tools'"):
            validate_override("reviewer", {"tools": "read, grep, find, ls"})

    def test_what_the_package_accepts(self):
        for value in ({"tools": ["read", "grep"]}, {"tools": []}, {"tools": False},
                      {"tools": "inherit"}, {"tools": " inherit "}, {"thinking": "high"},
                      {"model": False, "skills": False}):
            with self.subTest(value=value):
                validate_override("reviewer", value)

    def test_what_the_package_refuses(self):
        for value in ({"tools": "read"}, {"tools": ["read", 1]}, {"tools": True},
                      {"tool": ["read"]}, {"thinking": "extreme"}, {"disabled": "yes"},
                      {"skills": "a, b"}, {"launcher": "x"}, ["read"]):
            with self.subTest(value=value), self.assertRaises(Invalid):
                validate_override("reviewer", value)


class TestWorkerConfig(unittest.TestCase):
    def test_the_package_the_config_is_written_for_is_the_pinned_one(self):
        pinned = json.loads((ROOT / "scripts/dependencies/package.json").read_text())
        self.assertEqual("pi-subagents@" + pinned["dependencies"]["pi-subagents"], PINNED)
        install = (WORKFLOWS / "worker.md").read_text()
        self.assertIn("npm:" + PINNED, install)

    def test_the_compiled_lock_config_passes_the_validator_of_the_pinned_package(self):
        settings = lock_pi_config()["settings"]
        validate_settings(settings)
        overrides = settings["subagents"]["agentOverrides"]
        self.assertEqual(set(overrides), {"reviewer", "worker"})
        for name, override in overrides.items():
            self.assertIsInstance(override["tools"], list, name)
            self.assertTrue(override["tools"], name)

    def test_the_lock_config_is_the_source_config(self):
        source = yaml.safe_load((WORKFLOWS / "worker.md").read_text().split("---", 2)[1])
        self.assertEqual(lock_pi_config()["settings"], json.loads(source["engine"]["config"])["settings"])

    def test_the_planner_config_passes_too(self):
        validate_settings(lock_pi_config("planner")["settings"])


if __name__ == "__main__":
    unittest.main()
