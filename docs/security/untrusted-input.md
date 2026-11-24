---
title: Untrusted Input
parent: Security Model
nav_order: 4
permalink: /security/untrusted-input/
---

# Untrusted Input

Text written by people, upstream projects, and models is data. It can inform a decision but never widens a path, an operation, or a credential.

| Input | How it is treated |
|:------|:------------------|
| Request issue body | Frozen at intake with its SHA-256, and size-limited by `intake.max_request_bytes`. Later edits of the issue change nothing. See [Requests]({{ '/pipeline/requests/' | relative_url }}). |
| The canonical request, issue #1 | Refused unless its id, title, and body are exactly the reviewed ones. See [Repository Setup]({{ '/operations/setup/' | relative_url }}). |
| Upstream contribution text | Takes precedence for the content and style of a contribution, but never widens gates, scope, or credentials, or changes where or what is pushed. See [Contribution Guidelines]({{ '/pipeline/contribution-guidelines/' | relative_url }}). |
| History and steering | Redacted, bounded, never rendered on the tracking issue or the status page, and unable to widen any path, operation, or credential. See [Reports and History]({{ '/pipeline/history/' | relative_url }}). |
| Worker reports | Bounded, checked against a schema, and stored as evidence only by a success row. |
| Review findings | Bounded and redacted. Comments on the development PR are posted as inert text. See [Patch Review]({{ '/pipeline/patch-review/' | relative_url }}). |
| Text in the submission package | Placed in code spans or fences, stripped of credential patterns, and made unable to mention anyone. See [Submission and Handoff]({{ '/pipeline/submission/' | relative_url }}). |
| The status page | Shows no free text of an event, evidence, or comment, and escapes every value. |
| Pull-request previews | Rendered as data, in Jekyll safe mode without a token. See [Website]({{ '/operations/website/' | relative_url }}#preview-safety). |
