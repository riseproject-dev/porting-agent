#!/bin/sh
# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
#
# Launch a reviewed reproduction script in the linux/riscv64 repro image.
#
# ONLY on an approved, dedicated, disposable VM (plan 13.2): the Docker
# socket gives the workload host-level control. Refuses to run unless
# PORTING_DISPOSABLE_HOST=1 is set by that host's provisioning, and the
# image is pinned by digest. Use the digest of a published
# ghcr.io/riseproject-dev/porting-agent/ubuntu-24.04-riscv build (rebuilt
# weekly from the runner image's :prod, see .github/workflows/image.yml).
#
#   PORTING_DISPOSABLE_HOST=1 \
#   image/run.sh IMAGE@sha256:... ./approved-repro.sh
#
# The checkout, output and cache paths are mounted at the same absolute
# path inside the outer container so cibuildwheel's inner manylinux/
# musllinux containers, which the host daemon resolves, see the same files.
set -eu
image=${1:?image@sha256 digest}
script=${2:?reviewed reproduction script}
case "$image" in *@*) ;; *) echo "image must be pinned by digest" >&2; exit 2 ;; esac
digest=${image##*@}
printf '%s\n' "$digest" | grep -Eq '^sha256:[0-9a-f]{64}$' || {
    echo "image must use one exact sha256 digest" >&2; exit 2; }
if [ "${PORTING_DISPOSABLE_HOST:-}" != 1 ]; then
    echo "refusing: not an approved disposable host (see plan section 13.2)" >&2
    exit 2
fi
if env | grep -qE '^(GITHUB_TOKEN|GH_TOKEN|SSH_AUTH_SOCK|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|ANTHROPIC_API_KEY|RISEPROJECT_[A-Z_]*|UPSTREAMING_FORK_TOKEN|GH_AW_[A-Z_]*TOKEN)='; then
    echo "refusing: a credential variable is set in the launching environment" >&2
    exit 2
fi
[ -f /proc/sys/fs/binfmt_misc/qemu-riscv64 ] || {
    echo "qemu-riscv64 binfmt handler missing; provision it on the host first" >&2; exit 2; }
work=$(pwd)
out=${REPRO_OUT:-$work/.repro-out}
mkdir -p "$out"
{
    echo "source_sha=$(git -C "$work" rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "image=$image"
    echo "qemu=$(sed -n 's/^interpreter //p' /proc/sys/fs/binfmt_misc/qemu-riscv64)"
    echo "script=$script sha256=$(sha256sum "$script" | cut -d' ' -f1)"
    echo "started=$(date -u +%FT%TZ)"
} > "$out/provenance.txt"
set +e
timeout "${REPRO_TIMEOUT_SECONDS:-7200}" docker run --rm --init --platform linux/riscv64 \
  --memory "${REPRO_MEMORY:-8g}" --cpus "${REPRO_CPUS:-4}" --pids-limit "${REPRO_PIDS:-2048}" \
  --mount "type=bind,src=$work,dst=$work" \
  --mount "type=bind,src=$out,dst=$out" \
  --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  --workdir "$work" -e REPRO_OUT="$out" \
  "$image" bash -lc "uname -m; python3 --version; docker version --format '{{.Server.Version}}'; bash $script"
rc=$?
echo "exit=$rc finished=$(date -u +%FT%TZ) evidence=emulated-local" >> "$out/provenance.txt"
exit $rc
