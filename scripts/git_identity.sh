#!/bin/sh
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
# Configure the exact GitHub App bot identity for trusted state commits.
set -eu

if [ -n "${APP_SLUG:-}" ]; then
    name="${APP_SLUG}[bot]"
    id=$(gh api "/users/${APP_SLUG}%5Bbot%5D" --jq '.id')
else
    name="github-actions[bot]"
    id=41898282
fi

git config user.name "$name"
git config user.email "${id}+${name}@users.noreply.github.com"
