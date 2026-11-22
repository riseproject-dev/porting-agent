#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""Pin and apply the worker-only Pi provider credential-isolation patch."""

from __future__ import annotations

import hashlib
from pathlib import Path

SOURCE_SHA256 = "de15d3fdbe40757b1daf1bd0694bb7b6b01a00f39d1d3b18f2cd7115d306bc61"
PATCHED_SHA256 = "2f9bc177ad92e6d34991aab71fe1a24087e931a88ffaec139889e333a4e04f55"
OLD = b'''  if (process.env.ANTHROPIC_API_KEY) {
    registerProviderAliases(
      pi,
      ["anthropic"],
      {
        apiKey: process.env.ANTHROPIC_API_KEY,'''
NEW = b'''  if (process.env.ANTHROPIC_API_KEY) {
    // Capture provider authentication, then remove it from the process
    // environment before Pi can launch any untrusted tool subprocess.
    const anthropicApiKey = process.env.ANTHROPIC_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
    registerProviderAliases(
      pi,
      ["anthropic"],
      {
        apiKey: anthropicApiKey,'''


def apply(actions: Path) -> str:
    actions = actions.resolve(strict=True)
    provider = actions / "pi_provider.cjs"
    if provider.is_symlink() or not provider.is_file():
        raise ValueError("Pi provider runtime must be a regular file")
    data = provider.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest == PATCHED_SHA256:
        return "already patched"
    if digest != SOURCE_SHA256:
        raise ValueError(f"unexpected Pi provider SHA-256: {digest}")
    if data.count(OLD) != 1:
        raise ValueError("Pi provider credential block is not unique")
    patched = data.replace(OLD, NEW)
    if hashlib.sha256(patched).hexdigest() != PATCHED_SHA256:
        raise ValueError("patched Pi provider SHA-256 does not match")
    temporary = provider.with_suffix(".cjs.tmp")
    temporary.write_bytes(patched)
    temporary.replace(provider)
    return "patched"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("actions", type=Path)
    args = parser.parse_args()
    print(apply(args.actions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
