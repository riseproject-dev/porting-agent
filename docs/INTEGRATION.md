# Integration contract

The porting agent separates trusted authorization and remote effects from model judgment. Lifecycle state, native Work queue state, tracking issues, and remote operation receipts have distinct ownership. Every later remote effect must bind the exact repository identity, contribution revision, Git head, and verified readback.

Intake parses the stable `Upstream repository`, `PyPI distributions`, `Upstream target branch`, and `Additional notes` headings. It freezes the exact rendered body, SHA-256, parsed notes, and optional target branch into durable contribution state before any issue edit can affect later Work. Compatibility is limited to the immutable earlier package-request headings and the proven single-repository freeform request shape; current and legacy headings cannot be mixed.

Pull-request validation remains read-only. Milestone 3 adds `controller.yml` and `website.yml`. The controller observes with the ordinary read token, deterministically stages local records, and only then mints a control-repository App token for one validated plain fast-forward `.state/` commit. Issue projection uses a separate issues-scoped control token after state publication. The Pages workflow has only `contents: read`, `pages: write`, and `id-token: write`. Neither workflow receives a model credential or destination App credential.

The reviewed workflow migration and ownership table is in `workflow-migration.json`. A runtime workflow may be added only by its owning milestone and an agentic Markdown workflow must be committed with exactly one generated `.lock.yml` file.

State publication never force-pushes, deletes, merges, or uses a ruleset bypass. On a moving `main` tip it fetches, rechecks stale revision preconditions, rebuilds on the new tip, validates the complete candidate tree, and retries with a fixed bound. Human-facing ledger entries, failure replies, and terminal reactions scan durable state and hidden markers so a failure after the state commit is repaired by a later tick without duplicate comments.
