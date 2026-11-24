---
title: Requests
parent: Pipeline
nav_order: 1
permalink: /pipeline/requests/
---

# Requests
{: .no_toc }

A request is an issue in the control repository, created from the "Upstream RISC-V support" form and labelled `upstreaming-request`. Intake parses it and freezes it before any later Work reads it.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## The request form

The form is language agnostic. Intake parses four stable headings:

| Heading | Required | Meaning |
|:--------|:---------|:--------|
| Upstream repository | Yes | The project's GitHub repository, for example `https://github.com/owner/project`. |
| Packages | No | The packages the project publishes, one per line, in any ecosystem (PyPI, crates.io, npm, Go modules, Maven, and so on). Kept as written and recorded in the contribution's `distributions` field. |
| Upstream target branch | No | The branch the work targets. Leave it blank to use the default branch. |
| Additional notes | No | Language, toolchain, build system, recipes, patches, dependencies, runner constraints, contribution rules, or contact. |

Package names are read from PyPI only to confirm a plan's decision to close the request, and only a PyPI project that names the upstream repository confirms it. See [Planning]({{ '/pipeline/planning/' | relative_url }}#the-plans-decision).

Two limits in `policy.json` apply:

- A request body larger than `intake.max_request_bytes` (16384) is refused.
- A request that lists more than `intake.max_distributions` (32) packages is refused.

## What intake freezes

Intake freezes the exact rendered body, its SHA-256, the parsed notes, and the optional target branch into durable contribution state. No later edit of the issue can affect Work that has already been planned from it. Later Work reads the frozen body, never the live issue.

## Earlier request forms

Compatibility is limited to earlier forms that were each an immutable contract:

- A form whose package field was named for PyPI distributions.
- The package request form that preceded it.
- A freeform issue titled `<name> riscv64 support` whose body links exactly one GitHub repository.

All of them name PyPI distributions. At least one is required, and each is normalized as PEP 503 says.

The headings of different forms are never mixed in one request.

## Target branch and refresh

A refresh from `parked` starts a new generation on the same target branch, read again from the frozen body. A request that named no branch is bound to the upstream default branch of the time the refresh starts. To name another branch for the new generation, post:

```text
/upstreaming refresh revision=N branch=NAME reason="..."
```

## When the upstream cannot be read

The controller reads the upstream repository and base branch on every tick while it needs them. A failed read is classified, and the contribution waits for a human only when waiting is the right answer.

| Cause | What the contribution does |
|:------|:---------------------------|
| The target branch does not exist or was renamed | Waits at once with `base_branch_unavailable`. It does not retry. |
| GitHub names the repository otherwise (renamed or transferred, same repository id) | Waits at once with `upstream_unavailable`. |
| Any other failed read: network or server errors, rate limits, a repository that cannot be read | Retried, counted at most once per slot. |

Failed reads are counted at most once per slot:

- A slot is `retries.source_unavailable_minutes` divided by `retries.source_unavailable_reads`, measured from the first failure. With the current 60 minutes and 3 reads, a slot is 20 minutes.
- Ticks that come early, late, or in bursts count once per slot.
- The retries continue until both thresholds are met: at least 60 minutes since the first failure, and 3 counted reads.
- The read that meets both is counted at once.

Then the contribution waits with `base_branch_unavailable`, or with `upstream_unavailable` when most of the counted reads could not read the repository itself.

How each wait ends:

- A wait that failed reads caused ends on its own once the upstream can be read again.
- The wait a renamed repository caused ends on its own once GitHub answers for the repository under its recorded name again.
- A missing or renamed base branch needs a human: resume once it can be read again, or park the contribution and then post `/upstreaming refresh revision=N branch=NAME reason="..."` with the new branch.

A base branch renamed while the upstream PR is open is followed without a human once the verified PR targets the new name. A rename before the PR is opened is followed once the PR targets the name GitHub redirects the base to. The [Transition Engine]({{ '/state-machine/engine/' | relative_url }}#the-upstream-base-branch) describes the rows in full.

## Withdrawing a request

Removing the intake label withdraws a request only while it is in `queued.idle`. A request that already waits for a human, for example in `planning.waiting_human` because its target branch is unavailable, is withdrawn by parking it and then abandoning it:

```text
/upstreaming park revision=N reason="..."
/upstreaming abandon revision=N reason="..."
```
