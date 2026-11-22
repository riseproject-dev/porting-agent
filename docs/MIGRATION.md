# Migration inventory

Milestone 1 freezes `riseproject-dev/python-wheels` commit `c21e458f67541af8b6e988eda7a20e30fbad201d` as the proven implementation reference. `migration-inventory.json` assigns every regular file under its `upstreaming/` directory to one target path and owning milestone, or records an explicit removal. `workflow-migration.json` separately records every control workflow rename and owner.

The inventory is a planning and completeness record, not permission to enable a later milestone early. In particular, this initial repository contains no runtime control modules, runtime workflows, generated agent locks, or migrated live lifecycle records.

Earlier implementation evidence: [the frozen `upstreaming/` tree](https://github.com/riseproject-dev/python-wheels/tree/c21e458f67541af8b6e988eda7a20e30fbad201d/upstreaming) and [its workflow directory](https://github.com/riseproject-dev/python-wheels/tree/c21e458f67541af8b6e988eda7a20e30fbad201d/.github/workflows) are historical sources only.

## Milestone 2 update

The control-plane Python package is flattened into repository-root modules, schemas are rooted at `schemas/`, operational utilities are rooted at `scripts/`, and proven local/read-only tests are rooted at `tests/`. Aggregate commands moved to their owning modules; there is no replacement aggregate entry point.

Workflow-dependent tests remain represented without enabling runtime workflows early. `test-deferrals.json` binds every deferred case to the milestone and workflow prerequisite that must restore it. All other proven tests run cumulatively in Milestone 2.

## Milestone 3 update

The control controller and Pages status workflows are now owned by `controller.yml` and `website.yml`. Lifecycle authority is `.state/`; publication is a bounded plain fast-forward under `protect-main-history`, with no bypass or force path. Tracking issue ledgers, deterministic failure replies, and terminal reactions are recoverable from durable events and command receipts. The six Milestone 3 deferrals are restored. Agent and destination workflow contracts remain deferred to their Milestone 4-6 owners.

## Milestone 4 update

`dispatcher.md` and `investigator.md` now own native Work admission and authenticated read-only research. Their generated locks are compiler-owned outputs of pinned gh-aw v1.2.2. The controller can automatically select the queued canonical ciso8601 contribution, commit its immutable task, launch the dispatcher, fold only an exact verified investigation Result, and recover through explicit continuations plus schedule/workflow completion fallbacks. The four Milestone 4 workflow contract deferrals are restored; worker and destination writer contracts remain assigned to Milestones 5 and 6.

### Milestone 4 live recovery corrections

Flattened artifact extraction now resolves control state through `common.repository_root()` rather than package-depth arithmetic. Trusted controller collection can recover an investigation record job interrupted after exact queue Result resolution by revalidating the partial trusted publication; it does not reset or resubmit Work. Pages deployment now waits for exact current-run artifact readback and retries the official deploy action once after bounded rediscovery.

## Milestone 5 update

`image.yml` and `image/` now own the exact-base, linux/riscv64 reproduction image and anonymous exact-digest verification. `worker.md` joins the native queue with its normalized generated lock, pinned QEMU setup, pinned `gh-act`, stop-command isolation, immutable dossier readback, selected build/test reproduction, and bounded proposal evidence. The trusted worker record job performs structural scope/debug/reproduction validation but has no destination token or publication path. All four Milestone 5 deferrals are restored; destination provisioning, branch publication, development PR, and exact-head CI remain Milestone 6.
