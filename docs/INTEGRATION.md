# Integration contract

The porting agent separates trusted authorization and remote effects from model judgment. Lifecycle state, native Work queue state, tracking issues, and remote operation receipts have distinct ownership. Every later remote effect must bind the exact repository identity, contribution revision, Git head, and verified readback.

Milestone 1 enables only `.github/workflows/validate.yml`. It has read-only repository permission, does not select a deployment environment, and does not receive an App key, destination token, package credential, or state publisher credential.

The reviewed workflow migration and ownership table is in `workflow-migration.json`. A runtime workflow may be added only by its owning milestone and an agentic Markdown workflow must be committed with exactly one generated `.lock.yml` file.
