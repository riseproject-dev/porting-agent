# RISE porting agent

[`riseproject-dev/porting-agent`](https://github.com/riseproject-dev/porting-agent) is the trusted control plane for AI-assisted RISC-V porting work. The active profile helps upstream Python projects build, test, and publish Linux `riscv64` wheels through their own release processes.

The repository is being migrated in reviewed milestones. Milestones 1 through 5 established deterministic planning and uncredentialed riscv64 proposal development. Milestone 6 enables trusted, exact-resource destination effects: fork provisioning, exact-head publication, same-fork development PRs, and exact-head CI observation.

## Repository contract

- Root Python modules own trusted control behavior; there is no package wrapper or `src/` tree.
- `.state/` is lifecycle authority. Only schema-valid state files may be published in a plain fast-forward commit on `main`.
- Authored gh-aw workflows and generated lock workflows move together.
- Pull-request validation is read-only and receives no App private key, destination token, package credential, or state-publisher environment.
- The controller uses the control App only after deterministic reconciliation; destination credentials are absent.
- The planner receives only scoped read access, returns one verified artifact Result, and cannot push or mutate upstream resources.
- The worker model receives no destination credential. Its operator-approved unsandboxed process reaches host Docker/QEMU through a custom engine boundary that preserves only captured Anthropic provider authentication while stripping and verifying repository, App, destination, Actions, cloud, and SSH credentials before tools run. A separate trusted job reconstructs and publishes only a verified proposal using a token narrowed to one fork and an exact-head lease.
- Public status is generated at <https://riseproject-dev.github.io/porting-agent/status/> from committed state. The documentation site at <https://riseproject-dev.github.io/porting-agent/> renders `docs/`.
- The control repository is `riseproject-dev/porting-agent`; destination forks remain in `riseproject-upstreaming`.

## Local validation

```sh
uv run python -m unittest discover -s tests -t . -v
uv run python scripts/dependencies/sync.py
uv run python -m state validate
uv run ruff check .
sh scripts/gh-aw-compat/check.sh
scripts/check_gh_aw.sh /path/to/pinned-gh-aw
```

The final command checks authored/generated workflow parity once agentic workflows are introduced. Until then it checks the pinned compiler and rejected-route fixtures.

Local and read-only entry points are owned by their root modules:

```sh
python -m state validate
python -m state check-diff BASE CANDIDATE
python -m triggers audit
python -m commands parse FILE
python -m capacity observe
python -m controller observe --offline
python -m work plan --out PLAN.json --control-repo-id ID
python -m reconcile --offline
```

## Documentation

- [Migration inventory](docs/MIGRATION.md)
- [Integration contract](docs/INTEGRATION.md)
- [Planning contract](docs/PLANNING.md)
- [Repository setup](docs/SETUP.md)
- [State machine](docs/state-machine/index.md), its [lifecycle table](docs/state-machine/table.md), and the [transition engine](docs/state-machine/engine.md)

Earlier implementation evidence: [`riseproject-dev/python-wheels/upstreaming`](https://github.com/riseproject-dev/python-wheels/tree/c21e458f67541af8b6e988eda7a20e30fbad201d/upstreaming) is the immutable migration reference. New control and tracking activity belongs only in this repository.

## License

The control implementation is available under the [MIT License](LICENSE). The gh-aw compatibility fixture retains its upstream licensing in `scripts/gh-aw-compat/LICENSE`.
