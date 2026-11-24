# Control dependency updates

Dependabot will monitor GitHub Actions, exact Pi and planner extension versions in `package.json`, stable gh-aw releases in `go.mod`, and the Python ranges in `pyproject.toml`. The Go module is release inventory, not an application.

A manifest-only change is an update proposal, not a ready runtime upgrade. `sync.py` checks exact stable versions, the pinned gh-aw installer, authored workflow pins, and source/lock pairing.

For a reviewed Pi or extension update after agentic workflows exist:

```sh
uv run python scripts/dependencies/sync.py --write
scripts/compile_gh_aw.sh /path/to/reviewed-gh-aw worker planner dispatcher
uv run python -m unittest discover -s tests -t .
```

Do not hand-edit generated locks. A gh-aw upgrade must separately review installer hashes, `scripts/check_gh_aw.sh`, Result reader behavior, the `scripts/gh-aw-compat/` patch, detector behavior, generated locks, and native queue Work contracts. The v1.2.2 compatibility patch remains pinned through rollout.
