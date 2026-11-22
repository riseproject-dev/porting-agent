#!/bin/sh
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
# Compile with pinned gh-aw v1.2.2, then apply the reviewed text-only lock normalization.
set -eu
aw=${1:?path to gh-aw v1.2.2}
shift
[ "$#" -gt 0 ] || { echo "at least one workflow is required" >&2; exit 1; }
"$aw" version | grep -q 'v1.2.2' || { echo "expected gh-aw v1.2.2" >&2; exit 1; }
locks=""
for workflow in "$@"; do
    case "$workflow" in
        *[!A-Za-z0-9_.-]*|'') echo "unsafe workflow name: $workflow" >&2; exit 1 ;;
    esac
    locks="$locks .github/workflows/$workflow.lock.yml"
done
"$aw" compile "$@" --no-check-update --approve
# Deliberate word splitting supplies each validated generated lock path.
# shellcheck disable=SC2086
python3 scripts/normalize_gh_aw_locks.py $locks
