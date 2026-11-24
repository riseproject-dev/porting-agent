---
title: Contribution Guidelines
parent: Pipeline
nav_order: 3
permalink: /pipeline/contribution-guidelines/
---

# Contribution Guidelines
{: .no_toc }

A plan records, as `contribution_guidelines`, how the project wants a contribution shaped. Trusted code then applies the measurable parts to every patch and to the submission.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## The five parts

Every part cites its sources.

| Part | What it records | Where the planner must search |
|:-----|:----------------|:------------------------------|
| `pull_request_template` | Every pull request template, verbatim or summarized, with its required section headings and checklist items. | The root, `.github/` (including `.github/PULL_REQUEST_TEMPLATE/`), and `docs/`. |
| `contributing` | Every instruction a contributor must follow. | `CONTRIBUTING.md` or `CONTRIBUTING.rst` at the root and in `.github/`, `docs/contributing*`, `DEVELOPMENT.md`, and the developer documentation they link. |
| `commit_conventions` | The conventions measured from recent history: the sample, the title length median, 90th percentile and maximum, the measurable rules, what a machine cannot measure, and typical titles. | Recent commit history. |
| `reviewers` | For every path the contribution may change, the code owners and the people who reviewed or merged recent pull requests there, with links to their reviews and their recurring requests as concrete guidance, including testing expectations. | Code owners and recent pull requests of each path. |
| `series_requirements` | Every instruction, template checklist item, or reviewer expectation that requires a specific file of the series to change, such as a changelog or news entry, `AUTHORS`, or a documentation page. At most six, each with an id, a one-sentence description, its source, and one to three paths. | The other four parts. The list may be empty, and is empty for a park or close. |

The measurable commit rules are: title length limit, literal title prefixes and whether one is required, capitalization, a trailing period, a required body, the body wrap width, a required `Signed-off-by:` trailer, and the most commits a contribution should have.

A part may be "none found" only with the record of the search.

## Precedence

The project's contributing instructions take precedence over the pipeline's own instructions to the worker, and to the submission rewrite and review, for the content and style of a contribution. No upstream text widens the trusted gates, the scope, or the credentials, or changes where or what is pushed. See [Untrusted Input]({{ '/security/untrusted-input/' | relative_url }}).

## What the plan gate refuses

The trusted gate refuses a plan that:

- lacks a part, or records a found document it does not cite;
- records rules that are not measurable;
- has no reviewer research for a path in `allowed_paths`;
- omits `series_requirements`, or records two with the same id;
- cites a source for a series requirement that the contributing, template, or reviewer research does not record;
- names a requirement path that is not literally one of `allowed_paths`, so a requirement can always be met within scope;
- records a series requirement for a park or close.

## How the rules are enforced

**Commit rules.** The Work payload carries the plan's measurable commit rules as `context.commit_rules`, taken by the trusted planner from the committed plan evidence. Before any token, the worker gate checks every non-debug commit of the proposed series against them and rejects the series with the rule each commit breaks. A plan of schema version 1 records none.

**Attribution trailers.** The gate also rejects any attribution trailer that does not name the App's commit identity. That covers `Signed-off-by:`, `Co-authored-by:`, `Reviewed-by:`, and every other `*-by:` line, including a trailer in the title and trailers written with Unicode lookalikes of the key or the colon. It rejects an attribution trailer continued on a line that starts with white space. The worker commits as the App and attributes work to nobody else.

**Commit count.** The number of commits is checked at submission, where the review may squash. See [Submission and Handoff]({{ '/pipeline/submission/' | relative_url }}).

**Series requirements.** They are checked on the exact head when CI passes in development, before the contribution is nominated for submission. An unmet requirement returns the series to development as Work that carries it, as `context.series_requirements`. The worker gate then rejects a patch of that Work whose non-debug commits change no file matching one of the paths of each requirement. See [Development]({{ '/state-machine/engine/' | relative_url }}#development) in the transition engine.
