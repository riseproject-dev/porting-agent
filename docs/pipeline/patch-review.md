---
title: Patch review
parent: Pipeline
nav_order: 5
permalink: /pipeline/patch-review/
---

# Patch review
{: .no_toc }

Every development patch is reviewed before it is published. The reviewer is a separate agent on its own model, with no destination credential and no ability to write anything but its review.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
1. TOC
{:toc}
</details>

## Which patches are reviewed

The Work payload of a development patch or repair carries the review contract (`review: required`). Its trusted publish job rebuilds and validates the series exactly as for a push, without a token (`fork_publish verify`), and publishes nothing.

## The held patch

The reconciler holds the verified patch as `held_patch`, pinned to:

- the worker run, and the `porting-publication` artifact that carries the verified series with the run that owns it (the worker run, or a publish-only retry that verified the patch again);
- the Work;
- the SHA-256 of the series;
- the commit and tree it rebuilds to.

The worker's report is stored with that commit.

## The review Work

The review is its own Work, `development_review`, run by the `reviewer` profile of `reviewer.md`. It has its own model, a 45 minute limit, the read-only GitHub MCP server, and no reproduction image.

Its trusted pre-agent steps run before the model:

1. Download the series from the artifact with the job's read token (`scripts/held_patch.py`).
2. Check the run that owns the artifact and the series digest.
3. Rebuild the series on the head it was verified on, in `./fork`, and check the tree.
4. Fetch the worker's report, the plan, and the history at their pinned commit, by SHA-256.

The model then reads and diffs exactly that patch.

## How the review is checked

The review is a Result under `schemas/patch_review.schema.json`. The publish job checks it without a token (`patch_review check`):

- it answers the exact Work and pin;
- findings are only on changed files;
- every text is bounded and redacted;
- the verdict is derived from the findings: a blocker or major finding fails the review, whatever the model declares.

## Publishing the reviewed patch

The publish-only workflow, `publish-retry.yml`, publishes the reviewed patch.

- A fork token is minted only once the contribution is in `development.publishing`.
- The push happens only when the series has the held digest and rebuilds to exactly the reviewed commit.
- A held patch whose artifacts expire within a day is discarded instead (`held_patch.expires_at`).

## Failed reviews

| Review | What happens |
|:-------|:-------------|
| First failure | The patch returns for repair, with the review as feedback (see [Reviews in later Work](#reviews-in-later-work)). |
| Second failure in succession (`retries.patch_review_rejections` is 1) | The held patch is published anyway, and a comment on the development PR states the latest review and why it failed. |

## The review comment

After a publish that followed a failed review, trusted code posts one comment on the development PR. Nothing about reviews is posted on the tracking issue.

- The reconciler adds the comment to the comments due on the record (`review_comments`), which are kept across later publications and generations. It launches `review-comment.yml` once the publication is folded.
- The workflow checks without a token that the comment is still due, and posts it with a token for the one fork that carries only `pull-requests: write`. A hidden marker finds the App's comment on a retry.
- A failed or lost post is launched again, up to 5 attempts. After that an alert in every tick says the PR lacks the comment, and the launch repeats daily.

See [The review of a development patch]({{ '/state-machine/engine/' | relative_url }}#the-review-of-a-development-patch) for the rows.

## Reviews in later Work

Every review, passed or failed, is stored as `patch_review` evidence and is part of the history of every later Work and review.

A failed review that returns the patch for repair is also the pending feedback of the next development Work. That Work's payload names the rejected series (`context.previous_patch`). Its trusted pre-agent step downloads the series and checks it by digest, so the worker amends it. An expired artifact is skipped.
