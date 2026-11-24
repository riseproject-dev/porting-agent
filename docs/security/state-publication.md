---
title: State publication
parent: Security model
nav_order: 3
permalink: /security/state-publication/
---

# State publication
{: .no_toc }

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## `.state/` is authority

Committed lifecycle state under `.state/` on `main` is the authority for every contribution. Only schema-valid state may be published, in a plain fast-forward commit. Everything else (tracking issues, the status page) is a projection of it.

## The controller sequence

The controller's `reconcile` job, the only job that runs in the `state` environment, works in a fixed order.

1. **Observe.** The controller observes GitHub with the ordinary read token.
2. **Stage.** It deterministically stages local records.
3. **Mint.** Only if the policy's `read_only` flag is false, it mints a control-repository App token with `contents: write`.
4. **Commit.** It publishes one validated plain fast-forward commit that changes `.state/` only.
5. **Project.** It projects state onto tracking issues with a separate, issues-scoped control token.

## How a commit is built and checked

`state_commit.py` builds the commit with a private index, so the working tree is never touched and unrelated files cannot ride along. Its parent is the expected `main` tip, and its tree is that tip's tree plus the staged state files.

The commit is validated against the trusted parent by `state.validate_state_diff` and `python -m state check-diff`. Publishing is refused unless the reviewed policy has `read_only: false`.

## Moving `main`

State publication never force-pushes, deletes, merges, or uses a ruleset bypass. When an unrelated commit lands and moves the tip of `main`, the publisher:

1. fetches the new tip;
2. rechecks the stale revision preconditions of the affected Work;
3. rebuilds the commit on the new tip and validates the complete candidate tree;
4. tries again, at most 4 attempts in all (`state_commit.MAX_ATTEMPTS`).

It never repeats a remote effect to obtain a receipt.

## The ruleset

The `protect-main-history` ruleset on the default branch forbids force pushes and deletion and has no bypass actors. Its full declaration is in [Repository setup]({{ '/operations/setup/' | relative_url }}#bootstrap-resources).

## After the commit

Tracking-issue entries, failure replies, and reactions follow the commit and are repaired by a later tick when one fails. See [Recovery and retries]({{ '/operations/recovery/' | relative_url }}#repairing-tracking-issue-output).
