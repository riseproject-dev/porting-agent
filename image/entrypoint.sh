#!/bin/sh
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
# Credential-free command entrypoint: no runner registration, no tokens.
for v in GITHUB_TOKEN GH_TOKEN ACTIONS_RUNTIME_TOKEN ACTIONS_ID_TOKEN_REQUEST_TOKEN \
         RUNNER_TOKEN JIT_CONFIG ANTHROPIC_API_KEY RISEPROJECT_APP_PRIVATE_KEY \
         RISEPROJECT_UPSTREAMING_APP_PRIVATE_KEY UPSTREAMING_FORK_TOKEN AWS_ACCESS_KEY_ID \
         AWS_SECRET_ACCESS_KEY SSH_AUTH_SOCK; do
    unset "$v"
done
exec "$@"
