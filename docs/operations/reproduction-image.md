---
title: Reproduction Image
parent: Operations
nav_order: 3
permalink: /operations/reproduction-image/
---

# Reproduction Image
{: .no_toc }

`image.yml` and `image/` own the exact-base, `linux/riscv64` reproduction image the worker builds and tests in. The workflow also verifies the published image anonymously by its exact digest.

## Why `riscv64` only

The image is intentionally `riscv64`-only.

- Every Docker pull and local image inspection passes `--platform linux/riscv64`.
- Base-index inspection selects exactly one Linux `riscv64` manifest before extracting its digest.
- Dockerfiles rely on that exact manifest plus Buildx `platforms: linux/riscv64`, so they carry no constant `FROM --platform` flag.

## Verification after publishing

After publishing, the workflow:

1. logs out of GHCR and removes Docker authentication;
2. pulls and inspects the exact output digest anonymously;
3. checks that the RepoDigest identity matches;
4. checks that `uname -m` reports `riscv64` under QEMU.

The anonymous check is required because the image has no host-amd64 manifest.

For the image's contents and the restricted `run.sh` runner, see the [image README](https://github.com/riseproject-dev/porting-agent/blob/main/image/README.md). The worker resolves the same public image and passes its exact digest to Docker; see [Development]({{ '/pipeline/development/' | relative_url }}#reproduction-evidence).
