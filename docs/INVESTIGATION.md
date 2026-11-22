# Investigation contract

The investigator researches upstream history, contribution policy, build and test topology, release mechanics, dependencies, and prior architecture work. The active Python profile additionally requires Python build backend, ABI, libc, interpreter, dependency-wheel, installation, runtime, ELF, linkage, and optimization evidence.

Milestone 4 enables exactly one investigator profile through gh-aw v1.2.2 native Work. The controller commits an immutable `active_work` request before launching `dispatcher.lock.yml`. Trusted dispatcher preparation replays that exact request into the native queue; it does not let the model select or mutate Work. Queue admission and dispatch are limited to one `investigator.lock.yml` Claim.

The investigator uses the scoped `GITHUB_TOKEN` and a server-enforced read-only GitHub MCP server. It has no control App private key, destination token, package credential, fork writer, or repository mutation tool. Required research covers the complete tracking issue and comments, issue and PR history including reviews, CI and release topology, maintainers and contribution rules, project reports, the project graph, and public/RISE Python dependency-wheel evidence.

A single `porting-out` artifact must contain a schema-valid dossier and proposal bound to task, contribution, revision, tracking-body digest, and complete path coverage. Trusted pre-safe-output validation runs before native Completion. A separate trusted job resolves the exact Claim's verified Result from the gh-aw ledger, downloads only its authorized artifact, revalidates it, publishes a bounded App-authored dossier comment, and returns a `porting-publication` artifact for deterministic reconciliation.

Dispatcher and investigator terminal paths explicitly continue `controller.yml`; workflow completion and the schedule are recovery fallbacks. Missing Work, unavailable queue state, invalid research, malformed artifacts, and exhausted dispatch retries fail closed without fabricating Completion, Result, or state.
