---
title: Submission and Handoff
parent: Pipeline
nav_order: 7
permalink: /pipeline/submission/
---

# Submission and Handoff
{: .no_toc }

When CI passes on the development head, a human decides whether to submit upstream. Trusted workflows then prepare a clean branch and a submission package; the human opens the upstream pull request under their own identity. No workflow holds a credential that can write an upstream repository. See [Credentials]({{ '/security/credentials/' | relative_url }}).

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## Requesting submission

At the submission gate the tracking issue names the candidate head on which CI passed and shows the command to post:

```text
/upstreaming submit-upstream revision=N head=<full head SHA>
```

See [Requesting upstream submission]({{ '/state-machine/engine/' | relative_url }}#requesting-upstream-submission) for the exact rules.

## The rewrite

An uncredentialed rewrite Work reshapes the non-debug commits of the approved `riscv64-dev` series after the plan's commit conventions. It also writes the pull request title and description after the project's template and contributing instructions.

## Trusted checks without a token

Trusted code checks the rewrite without a token:

- every commit of the series, and that the final tree is the same;
- the attribution trailers;
- the measurable commit rules;
- the template sections;
- the number of commits;
- the review loop of the worker's report, with no P0 finding open.

## The review of the rewrite

Before it returns, the rewrite Work reviews its own series and text with the review loop (fresh reviewers, at most 3 rounds), against:

- the pull request template;
- the contributing instructions;
- the commit conventions;
- the reviewers' expectations.

A P0 finding that is still open blocks the submission.

## Building `riscv64`

`handoff.yml` creates or replaces `riscv64` from the reviewed series under an exact-head lease. It never does so while a pull request is open from `riscv64`. `riscv64-dev` and the development PR are not touched, and the development PR is never closed.

## CI on the clean head

When the rebuilt head has another tree than the candidate head, the same trusted job dispatches the project's required CI workflow on `riscv64`, if it accepts `workflow_dispatch`. It uses a separate `actions: write` token for the one fork, minted only for that attempt. Interrupted jobs are rerun.

The wait asks a human when that CI cannot run or does not complete within a bounded wait.

## The submission package

The package is posted as one comment on the tracking issue, with a control token that holds only `issues: write`. It contains a prefilled pull request link, the reviewed title and description, and the evidence behind them. See [The submission package]({{ '/state-machine/engine/' | relative_url }}#the-submission-package) for its exact contents and the protections against stray mentions.

The authorized human opens the upstream pull request from `riscv64` under their own identity, at exactly that head. The controller verifies the pull request by observation. No command registers it.

## Retraction

`/upstreaming retract-pr revision=N head=<clean head>` withdraws the package and returns the contribution to development. It leaves `riscv64` as it is: the next submission replaces it.
