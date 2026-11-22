#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Apply the reviewed ASCII-only normalization to gh-aw v1.2.2 lock output."""

from __future__ import annotations

import argparse
from pathlib import Path

EM_DASH = "\N{EM DASH}"
REPLACEMENT = "--"


def normalize(path: Path) -> int:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path}: generated lock must be a regular file")
    text = path.read_text(encoding="utf-8")
    if "# gh-aw-manifest:" not in text:
        raise ValueError(f"{path}: not a gh-aw generated lock")
    count = text.count(EM_DASH)
    if count:
        path.write_text(text.replace(EM_DASH, REPLACEMENT), encoding="utf-8")
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("locks", nargs="+", type=Path)
    args = parser.parse_args(argv)
    total = 0
    for path in args.locks:
        count = normalize(path)
        total += count
        print(f"{path}: normalized {count} U+2014 occurrence(s)")
    print(f"normalized {total} compiler-owned U+2014 occurrence(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
