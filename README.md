# RISE porting agent

[`riseproject-dev/porting-agent`](https://github.com/riseproject-dev/porting-agent) is the trusted control plane for AI-assisted RISC-V porting work. The active profile helps upstream Python projects build, test, and publish Linux `riscv64` wheels through their own release processes.

The repository is being migrated in reviewed milestones. Milestone 1 establishes the repository contract, migration inventory, pinned tooling, request form, and read-only validation. Runtime controllers, agent workflows, and lifecycle state are intentionally not enabled yet.

## Repository contract

- Root Python modules will own trusted control behavior; there is no package wrapper or `src/` tree.
- `.state/` will become lifecycle authority when state handling is introduced in Milestone 3.
- Authored gh-aw workflows and generated lock workflows move together.
- Pull-request validation is read-only and receives no App private key, destination token, package credential, or state-publisher environment.
- The control repository is `riseproject-dev/porting-agent`; destination forks remain in `riseproject-upstreaming`.

## Local validation

```sh
uv run python -m unittest discover -s tests -t . -v
uv run python scripts/dependencies/sync.py
uv run ruff check .
sh scripts/gh-aw-compat/check.sh
scripts/check_gh_aw.sh /path/to/pinned-gh-aw
```

The final command checks authored/generated workflow parity once agentic workflows are introduced. During Milestone 1 it still checks the pinned compiler and rejected-route fixtures.

## Documentation

- [Migration inventory](docs/MIGRATION.md)
- [Integration contract](docs/INTEGRATION.md)
- [Investigation contract](docs/INVESTIGATION.md)
- [Repository setup](docs/SETUP.md)

Earlier implementation evidence: [`riseproject-dev/python-wheels/upstreaming`](https://github.com/riseproject-dev/python-wheels/tree/c21e458f67541af8b6e988eda7a20e30fbad201d/upstreaming) is the immutable migration reference. New control and tracking activity belongs only in this repository.

## License

The control implementation is available under the [MIT License](LICENSE). The gh-aw compatibility fixture retains its upstream licensing in `scripts/gh-aw-compat/LICENSE`.
