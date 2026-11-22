# Migration inventory

Milestone 1 freezes `riseproject-dev/python-wheels` commit `c21e458f67541af8b6e988eda7a20e30fbad201d` as the proven implementation reference. `migration-inventory.json` assigns every regular file under its `upstreaming/` directory to one target path and owning milestone, or records an explicit removal. `workflow-migration.json` separately records every control workflow rename and owner.

The inventory is a planning and completeness record, not permission to enable a later milestone early. In particular, this initial repository contains no runtime control modules, runtime workflows, generated agent locks, or migrated live lifecycle records.

Earlier implementation evidence: [the frozen `upstreaming/` tree](https://github.com/riseproject-dev/python-wheels/tree/c21e458f67541af8b6e988eda7a20e30fbad201d/upstreaming) and [its workflow directory](https://github.com/riseproject-dev/python-wheels/tree/c21e458f67541af8b6e988eda7a20e30fbad201d/.github/workflows) are historical sources only.
