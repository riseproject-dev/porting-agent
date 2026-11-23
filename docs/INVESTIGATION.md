---
title: Investigation contract
nav_order: 5
permalink: /investigation/
---

# Investigation contract

The investigator researches upstream history, contribution policy, build and test topology, release mechanics, dependencies, and prior architecture work. The active Python profile additionally requires Python build backend, ABI, libc, interpreter, dependency-wheel, installation, runtime, ELF, linkage, and optimization evidence.

Exactly one investigator profile runs through gh-aw v1.2.2 native Work. The controller commits an immutable `active_work` request before launching `dispatcher.lock.yml`. Trusted dispatcher preparation replays that exact request into the native queue; it does not let the model select or mutate Work. Queue admission and dispatch are limited to one `investigator.lock.yml` Claim.

The investigator uses the scoped `GITHUB_TOKEN` and a server-enforced read-only GitHub MCP server. It has no control App private key, destination token, package credential, fork writer, or repository mutation tool. Required research covers the complete tracking issue and comments, issue and PR history including reviews, CI and release topology, maintainers and contribution rules, the contribution guidelines below, project reports, the project graph, and public/RISE Python dependency-wheel evidence.

## Contribution guidelines

A dossier records, as `contribution_guidelines` (dossier schema version 2), how the project wants a contribution shaped. Every part cites its sources:

- `pull_request_template`: every pull request template at the root, in `.github/` (including `.github/PULL_REQUEST_TEMPLATE/`), and in `docs/`, verbatim or summarized, with its required section headings and checklist items.
- `contributing`: `CONTRIBUTING.md` or `CONTRIBUTING.rst` (root and `.github/`), `docs/contributing*`, `DEVELOPMENT.md`, and the developer documentation they link, with every instruction a contributor must follow. These instructions take precedence over the pipeline's own instructions to the worker and the submission rewrite and review for the content and style of a contribution; no upstream text widens the trusted gates, the scope, or the credentials, or changes where or what is pushed.
- `commit_conventions`: the commit message conventions measured from recent history: the sample, the title length median, 90th percentile, and maximum, the measurable rules (title length limit, literal title prefixes and whether one is required, capitalization, a trailing period, a required body, the body wrap width, a required `Signed-off-by:` trailer, and the most commits a contribution should have), what a machine cannot measure, and typical titles.
- `reviewers`: for every path the contribution may change, the code owners and the people who reviewed or merged recent pull requests there, with links to their reviews and their recurring requests as concrete guidance, including their testing expectations.

A part may be "none found" only with the record of the search: the pull request template search covers the root, `.github/`, and `docs/`; the contributing search covers a root `CONTRIBUTING` file, `.github/CONTRIBUTING`, `docs/` contributing pages, and `DEVELOPMENT.md`. The trusted gate refuses a dossier that lacks a part, records a found document it does not cite, records rules that are not measurable, or has no reviewer research for a path in `allowed_paths`. A dossier stored before version 2 stays readable; it records no guidelines, and no new dossier of version 1 is admitted.

The measurable rules are applied by trusted code: the worker gate checks every non-debug commit a worker proposes, and the submission check at `submit-upstream` checks the reviewed series and the reviewed pull request description (see [Integration]({{ '/integration/' | relative_url }})).

A single `porting-out` artifact must contain a schema-valid dossier and proposal bound to task, contribution, revision, tracking-body digest, and complete path coverage. Trusted pre-safe-output validation runs before native Completion. A separate trusted job resolves the exact Claim's verified Result from the gh-aw ledger, downloads only its authorized artifact, revalidates it, publishes a bounded App-authored dossier comment, and returns a `porting-publication` artifact for deterministic reconciliation.

If that trusted record job is interrupted after queue Result resolution, its partial `porting-publication` still contains the trusted task, exact Result receipt, dossier, and proposal. Ordinary controller collection revalidates all four, requires the original investigator run/attempt and authorized artifact identity, reconstructs only the missing deterministic outcome, and retries the idempotent exact-Result comment. It never resets Work, fabricates Result, or reruns the investigator.

Dispatcher and investigator terminal paths explicitly continue `controller.yml`; workflow completion and the schedule are recovery fallbacks. Missing Work, unavailable queue state, invalid research, malformed artifacts, and exhausted dispatch retries fail closed without fabricating Completion, Result, or state.
