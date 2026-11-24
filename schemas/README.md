# Schemas

These JSON schemas are the migrated contracts for policy, lifecycle state, evidence, plans, proposals, operations, commands, projects, and events. Root modules resolve them relative to the repository root, independent of the process working directory.

`worker_report.schema.json` is the report every patch, repair, revision, submission rewrite, and submission review worker writes as `report.json` beside its Result (untrusted, at most 32 KiB). Evidence records carry three further kinds: `worker_report` (the cleaned report of a successful worker, bound to its Work), `human_steering` (the tracking issue and pull request text of accounts in the command authority and the reviews of upstream maintainers, with author, time, and link), and `work_history` (the bounded history one Work carries, which its payload names as `context.history`). The shapes of their `data` are enforced by `worker_report.py`, `steering.py`, and `work_history.py`.

Evidence of kind `resolution` carries a `resolution` object, required for that kind: the authorized `/upstreaming resolve-operation` command the controller recorded (operation, disposition, command and comment ids, issue, body digest, actor, and the human's reason as bounded data) with its snapshot of the operation and the contribution at that moment.
