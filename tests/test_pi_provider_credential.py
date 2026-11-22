# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Runtime contract for provider authentication separated from model tools."""

import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts import pi_provider_credential

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/pi_provider.v1.2.2.cjs.b64"
WORKER = ROOT / ".github/workflows/worker.md"
LOCK = ROOT / ".github/workflows/worker.lock.yml"


class TestPiProviderCredentialPatch(unittest.TestCase):
    def patched_provider(self, directory: Path) -> Path:
        actions = directory / "actions"
        actions.mkdir()
        provider = actions / "pi_provider.cjs"
        provider.write_bytes(base64.b64decode(FIXTURE.read_bytes()))
        self.assertEqual(pi_provider_credential.apply(actions), "patched")
        self.assertEqual(pi_provider_credential.apply(actions), "already patched")
        return provider

    def test_provider_captures_key_then_deletes_tool_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = self.patched_provider(Path(temporary))
            harness = Path(temporary) / "contract.cjs"
            harness.write_text(
                """
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[2], "utf8");
const childProcess = { env: { ANTHROPIC_API_KEY: "provider-secret" }, stderr: { write() {} } };
const moduleBox = { exports: {} };
const sandbox = {
  module: moduleBox,
  exports: moduleBox.exports,
  process: childProcess,
  require(name) {
    if (name === "fs") return require("fs");
    if (name === "./awf_reflect.cjs") return {
      fetchAWFReflect: async () => ({}),
      AWF_API_PROXY_REFLECT_URL: "", AWF_REFLECT_OUTPUT_PATH: "",
      AWF_REFLECT_TIMEOUT_MS: 1, AWF_MODELS_URL_TIMEOUT_MS: 1,
    };
    if (name === "./safeoutputs_cli.cjs") return { emitInfrastructureIncomplete() {} };
    if (name === "./error_helpers.cjs") return { getErrorMessage: String };
    if (name === "./pi_provider_error.cjs") return { getProviderErrorDetails: () => ({}) };
    throw new Error("unexpected require " + name);
  },
  URL, Date, console, setTimeout, clearTimeout,
};
vm.runInNewContext(code, sandbox, { filename: "pi_provider.cjs" });
const registered = [];
moduleBox.exports.registerConfiguredProviders(
  { registerProvider(name, config) { registered.push({ name, apiKey: config.apiKey }); } },
  () => {}
);
console.log(JSON.stringify({ registered, environmentHasKey: "ANTHROPIC_API_KEY" in childProcess.env }));
"""
            )
            result = subprocess.run(
                ["node", str(harness), str(provider)],
                check=True,
                capture_output=True,
                text=True,
            )
        contract = json.loads(result.stdout)
        anthropic = [item for item in contract["registered"] if item["name"] == "anthropic"]
        self.assertEqual(anthropic, [{"name": "anthropic", "apiKey": "provider-secret"}])
        self.assertFalse(contract["environmentHasKey"])

    def test_unknown_runtime_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            actions = Path(temporary)
            provider = actions / "pi_provider.cjs"
            provider.write_text("unknown")
            with self.assertRaises(ValueError):
                pi_provider_credential.apply(actions)
            provider.unlink()
            provider.symlink_to(FIXTURE)
            with self.assertRaises(ValueError):
                pi_provider_credential.apply(actions)


class TestGeneratedWorkerCredentialBoundary(unittest.TestCase):
    def test_provider_auth_is_present_but_repo_credentials_are_scrubbed(self):
        source_text = WORKER.read_text()
        source = yaml.safe_load(source_text.split("---", 2)[1])
        steps = source["pre-agent-steps"]
        split = next(
            step for step in steps
            if step.get("name") == "Split Pi provider authentication from untrusted tool environment"
        )
        wrapper = next(
            step for step in steps
            if "credential-free unsandboxed engine wrapper" in step.get("name", "")
        )["run"]
        self.assertIn("pi_provider_credential.py", split["run"])
        self.assertNotIn("unset ANTHROPIC_API_KEY", wrapper)
        for credential in (
            "GITHUB_TOKEN", "GH_AW_GITHUB_TOKEN", "RISEPROJECT_APP_PRIVATE_KEY",
            "RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY", "UPSTREAMING_FORK_TOKEN",
            "ACTIONS_RUNTIME_TOKEN", "AWS_ACCESS_KEY_ID", "SSH_AUTH_SOCK",
        ):
            self.assertIn(credential, wrapper)
        gh_act_setup = steps[1]["run"]
        self.assertIn("unset GITHUB_TOKEN GH_TOKEN ANTHROPIC_API_KEY", gh_act_setup)
        self.assertNotRegex(gh_act_setup, r"docker (?:run|pull).*\s-e\s")

        lock = yaml.safe_load(LOCK.read_text())
        execute = next(
            step
            for step in lock["jobs"]["agent"]["steps"]
            if step.get("name") == "Execute Pi CLI"
        )
        self.assertEqual(execute["env"]["ANTHROPIC_API_KEY"], "${{ secrets.ANTHROPIC_API_KEY }}")
        self.assertEqual(execute["env"]["GH_AW_PI_COMMAND"], "/tmp/gh-aw/agent/pi-scrubbed")
        self.assertIn("/tmp/gh-aw/agent/pi-scrubbed", execute["run"])
        self.assertNotIn("awf --config", execute["run"])


if __name__ == "__main__":
    unittest.main()
