# RISE porting agent

[`riseproject-dev/porting-agent`](https://github.com/riseproject-dev/porting-agent) is the trusted control plane for AI-assisted RISC-V porting work. It helps upstream projects of any language build, test, and release for Linux `riscv64` through their own build, test, and release processes; Python wheels are one case among others.

## Repository contract

- Root Python modules own trusted control behavior; there is no package wrapper or `src/` tree.
- `.state/` is lifecycle authority. Only schema-valid state files may be published in a plain fast-forward commit on `main`.
- Authored gh-aw workflows and generated lock workflows move together.
- Pull-request validation is read-only and receives no App private key, destination token, package credential, or state-publisher environment.
- The controller mints control App tokens only, never a destination credential: after deterministic reconciliation for state and issue projection, plus one issues-scoped token that adds an acknowledgement reaction to an `/upstreaming` comment, and one `contents: read`, `actions: write` token that dispatches workflows.
- The planner receives only scoped read access, returns one verified artifact Result, and cannot push or mutate upstream resources.
- The worker model receives no destination credential. Its operator-approved unsandboxed process reaches host Docker/QEMU through a custom engine boundary that preserves only captured Anthropic provider authentication while stripping and verifying repository, App, destination, Actions, cloud, and SSH credentials before tools run. A separate trusted job reconstructs and publishes only a verified proposal using a token narrowed to one fork and an exact-head lease.
- The worker reviews its own patch with fresh reviewers it launches as foreground children of its process (a review loop on the `subagent` tool of `pi-subagents`, at most 3 rounds, with read, grep, find and ls only), before trusted code publishes it. Trusted code checks the shape of the loop it reports and publishes nothing while a P0 finding is open. The dispatcher is the one locked-down agent: no shell, no file writes, no web access and no extension.
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

The final command checks authored/generated workflow parity.

Local and read-only entry points are owned by their root modules:

```sh
python -m state validate
python -m state check-diff BASE CANDIDATE
python -m triggers audit
python -m commands parse FILE
python -m capacity observe
python -m controller observe --offline
python -m tasks plan --out PLAN.json --control-repo-id ID
python -m reconcile --offline
```

## Documentation

The documentation is published at <https://riseproject-dev.github.io/porting-agent/>.

- [Pipeline](https://riseproject-dev.github.io/porting-agent/pipeline/): requests, planning, development with its review loop, and submission
- [Operations](https://riseproject-dev.github.io/porting-agent/operations/): repository setup, workflows, recovery, and the website
- [Security Model](https://riseproject-dev.github.io/porting-agent/security/): credentials, agent isolation, state publication, and untrusted input
- [State Machine](https://riseproject-dev.github.io/porting-agent/state-machine/), its [Lifecycle Table](https://riseproject-dev.github.io/porting-agent/state-machine/table/), the [Transition Engine](https://riseproject-dev.github.io/porting-agent/state-machine/engine/), and the [Known Gaps](https://riseproject-dev.github.io/porting-agent/state-machine/known-gaps/)

## License

The control implementation is available under the [MIT License](LICENSE). The gh-aw compatibility fixture retains its upstream licensing in `scripts/gh-aw-compat/LICENSE`.
