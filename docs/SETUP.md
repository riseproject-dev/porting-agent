# Repository setup

The reviewed repository settings are declared in `.github/repository-settings.json`. `scripts/audit_repository_settings.py` compares a normalized read-only settings snapshot with that declaration. Local tests use the deterministic snapshot in `tests/fixtures/repository-settings.json`; a live audit is a separate read-only operation after the first commit reaches GitHub.

Required bootstrap resources include the `upstreaming-request` label, Pages through Actions, read-only default workflow permissions, squash-only merges, automatic head-branch deletion, the `protect-main-history` ruleset, branch-scoped `state` and reviewed `publish` environments, selected organization secrets and variables, and the two-App installation boundary.

The canonical `ciso8601` request must be created only after the issue form is present on remote `main`. Its frozen expected input and rendered-body digest are recorded in `tests/fixtures/ciso8601-request.json`. Local bootstrap validation does not create the issue or perform any other remote write.
