#!/bin/sh
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
# Check generated locks and rejection fixtures with the pinned gh-aw v1.2.2 CLI.
set -eu
aw=${1:?path to the gh-aw v1.2.2 binary}
"$aw" version | grep -q 'v1.2.2' || { echo "expected gh-aw v1.2.2" >&2; exit 1; }
root=$(git rev-parse --show-toplevel)
cd "$root"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

workflows=""
for workflow in worker investigator dispatcher; do
    source=.github/workflows/$workflow.md
    lock=.github/workflows/$workflow.lock.yml
    if [ -e "$source" ] || [ -e "$lock" ]; then
        if ! { [ -f "$source" ] && [ -f "$lock" ]; }; then
            echo "$workflow authored source and generated lock must move together" >&2
            exit 1
        fi
        cp "$lock" "$tmp/$workflow.lock.yml.committed"
        workflows="$workflows $workflow"
    fi
done
if [ -n "$workflows" ]; then
    # Deliberate word splitting supplies each reviewed workflow name as one argument.
    # shellcheck disable=SC2086
    scripts/compile_gh_aw.sh "$aw" $workflows >/dev/null
    for workflow in $workflows; do
        cmp -s "$tmp/$workflow.lock.yml.committed" ".github/workflows/$workflow.lock.yml" || {
            echo "$workflow.lock.yml is stale; recompile with gh-aw v1.2.2" >&2
            exit 1
        }
    done
fi

fixtures=$tmp/fixtures
mkdir -p "$fixtures/.github/workflows"
git -C "$fixtures" init -q
cp tests/gh-aw-fixtures/rejected-*.md "$fixtures/.github/workflows/"
cd "$fixtures"
if "$aw" compile rejected-custom-job --no-check-update --approve >out 2>&1; then
    echo "rejected-custom-job now compiles: revisit the trusted delivery contract" >&2
    exit 1
fi
grep -q 'requires a trusted per-Claim delivery adapter' out || { cat out; exit 1; }
"$aw" compile rejected-direct-push --no-check-update --approve >out 2>&1 || { cat out; exit 1; }
echo "gh-aw v1.2.2 checks passed"
