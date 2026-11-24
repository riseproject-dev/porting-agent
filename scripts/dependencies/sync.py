#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Check or synchronize Dependabot inventory with authored workflow pins.

Milestone 1 has no agentic workflows. The dependency inventory and pinned gh-aw
installer are still checked. As each authored workflow arrives, this utility
requires its source pin to agree with the reviewed inventory.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ("planner", "worker", "dispatcher")
EXTENSIONS = ("pi-web-access", "pi-subagents")


def synchronize(root: Path, write: bool = False) -> list[str]:
    inventory = root / "scripts" / "dependencies"
    dependencies = json.loads((inventory / "package.json").read_text(encoding="utf-8"))[
        "dependencies"
    ]
    versions = {
        name: dependencies[name]
        for name in ("@earendil-works/pi-coding-agent", *EXTENSIONS)
    }
    for name, dependency_version in versions.items():
        if not re.fullmatch(r"\d+\.\d+\.\d+", dependency_version):
            raise ValueError(f"{name}: require an exact stable release, got {dependency_version!r}")

    compiler = re.search(
        r"^require github\.com/github/gh-aw (v\d+\.\d+\.\d+)$",
        (inventory / "go.mod").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if compiler is None:
        raise ValueError("gh-aw inventory must specify one exact stable release")

    issues: list[str] = []
    compiler_version = compiler.group(1)
    installer = (root / "scripts" / "install_gh_aw.sh").read_text(encoding="utf-8")
    if f"version={compiler_version}\n" not in installer:
        issues.append("gh-aw update requires reviewed installer hashes and compatibility patch")

    result_reader = root / "queue_result.py"
    result_reader_matches = not result_reader.exists() or (
        f'GH_AW_VERSION = "{compiler_version}"'
        in result_reader.read_text(encoding="utf-8")
    )
    if not result_reader_matches:
        issues.append("gh-aw update requires reviewed Result reader behavior")

    for name in WORKFLOWS:
        path = root / ".github" / "workflows" / f"{name}.md"
        lock = path.with_name(f"{name}.lock.yml")
        if path.exists() != lock.exists():
            issues.append(f"{name}: authored workflow and generated lock must move together")
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        target, count = re.subn(
            r'(engine:\n  id: pi\n  version: )["\']?[^\n"\']+["\']?',
            lambda match: match[1]
            + json.dumps(versions["@earendil-works/pi-coding-agent"]),
            source,
        )
        if count != 1:
            raise ValueError(f"{path}: expected exactly one explicit Pi version")
        if name == "planner":
            for extension in EXTENSIONS:
                target, count = re.subn(
                    r"npm:" + re.escape(extension) + r"@[^\s]+",
                    "npm:" + extension + "@" + versions[extension],
                    target,
                )
                if count != 1:
                    raise ValueError(f"{path}: expected one {extension} pin")
        if target != source:
            if write:
                path.write_text(target, encoding="utf-8")
            else:
                issues.append(
                    f"{path.relative_to(root)} pins do not match dependency inventory; "
                    "run sync.py --write and recompile"
                )
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    issues = synchronize(args.root, args.write)
    for issue in issues:
        print(issue)
    return bool(issues)


if __name__ == "__main__":
    raise SystemExit(main())
