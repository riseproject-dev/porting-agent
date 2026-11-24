---
title: Repository setup
nav_order: 3
permalink: /setup/
---

# Repository setup

The reviewed repository settings are declared in `.github/repository-settings.json`. `scripts/audit_repository_settings.py` compares a normalized read-only settings snapshot with that declaration. It fails on any `GH_AW_*` secret in the declaration or the snapshot, since the planner and worker agents run outside the sandbox and gh-aw would hand such a token to their job. Local tests use the deterministic snapshot in `tests/fixtures/repository-settings.json`; a live audit is a separate read-only operation after the first commit reaches GitHub, and `--live riseproject-dev/porting-agent` reads the names of the repository's own secrets, the organization secrets visible to it and its environment secrets with `gh api` and fails on any `GH_AW_*` name, or with gh's error when a listing cannot be read in full.

Required bootstrap resources include the `upstreaming-request` label, Pages through Actions, read-only default workflow permissions, squash-only merges, automatic head-branch deletion, the `protect-main-history` ruleset, branch-scoped `state` and reviewed `publish` environments, selected organization secrets and variables, and the two-App installation boundary.

The canonical [`ciso8601` request](https://github.com/riseproject-dev/porting-agent/issues/1) was created after the issue form reached remote `main`. Its verified rendered-body SHA-256 is `9f58bffa65d7e7df0206ae1130ce6eb5e218181b966839f2e609c05e8baa7ebb`; the frozen input and receipt are recorded in `tests/fixtures/ciso8601-request.json`. Local validation performs no remote write.
