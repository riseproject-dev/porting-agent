---
title: Repository setup
parent: Operations
nav_order: 1
permalink: /operations/setup/
---

# Repository setup
{: .no_toc }

The reviewed repository settings are declared in `.github/repository-settings.json`. This page lists what the declaration requires and how it is audited.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## Declared settings and the audit

`scripts/audit_repository_settings.py` compares a normalized read-only settings snapshot with the declaration.

- Local tests use the deterministic snapshot in `tests/fixtures/repository-settings.json` (`--snapshot`).
- A live audit is a separate read-only operation: `--live riseproject-dev/porting-agent` reads, with `gh api`, the names of the repository's own secrets, the organization secrets visible to it, and its environment secrets. It fails with gh's error when a listing cannot be read in full.
- The audit fails on any `GH_AW_*` secret in the declaration or the snapshot, and on any such name in a live listing. The planner and worker agents run outside the sandbox, and gh-aw would hand such a token to their job. See the known gap [the planner and worker agents run on the runner]({{ '/state-machine/known-gaps/' | relative_url }}#the-planner-and-worker-agents-run-on-the-runner-outside-the-sandbox).

## Bootstrap resources

| Resource | Declaration |
|:---------|:------------|
| Label | `upstreaming-request`, also in `.github/labels.yml`. |
| Pages | Built through Actions (`workflow`), HTTPS enforced. |
| Default workflow permissions | `read`; workflows cannot approve pull request reviews. |
| Merges | Squash only. The head branch is deleted on merge. |
| Ruleset | `protect-main-history` on the default branch: no deletion, no non-fast-forward update, linear history required, no bypass actors. |
| Environment `state` | Deployments from `main`. Used by the controller's reconcile job. |
| Environment `publish` | Deployments from `main`, with a required reviewer. Declared as a bootstrap resource; no workflow currently uses it. |
| Environment `github-pages` | Deployments from `main`. Used by the website deploy job. |
| Organization secrets | `ANTHROPIC_API_KEY` and the private keys of the two Apps, selected for this repository. |
| Organization variables | The control App's id and client id, and the destination App's client id. |
| Repository secrets and variables | None. |
| App installations | Control, `riseproject-dev`, on all repositories. Runner, declared without a slug, on all repositories; no workflow mints a token from it. Destination, `riseproject-upstreaming`, on all repositories of that organization, with administration, actions, contents, pull requests, and workflows write, and repository creation. |

The two-App boundary is described in [Credentials]({{ '/security/credentials/' | relative_url }}).

## The canonical request

Issue [#1](https://github.com/riseproject-dev/porting-agent/issues/1) is the canonical `ciso8601` request. Its verified rendered-body SHA-256 is `9f58bffa65d7e7df0206ae1130ce6eb5e218181b966839f2e609c05e8baa7ebb`, and its frozen input and receipt are recorded in `tests/fixtures/ciso8601-request.json`.

Intake refuses that issue unless its id, title, and body are exactly the reviewed ones. Local validation performs no remote write.
