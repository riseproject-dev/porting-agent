# RISC-V reproduction image

`image/` builds `ghcr.io/riseproject-dev/porting-agent/ubuntu-24.04-riscv`. The trusted image workflow resolves the native runner base to an exact digest, builds only `linux/riscv64`, records the base digest in OCI labels, and publishes `latest` plus a base-digest-derived tag only outside pull requests.

After publishing, the workflow logs out of GHCR, removes Docker authentication, and pulls and inspects the exact output digest anonymously with explicit `--platform linux/riscv64`. This is required because the image intentionally has no host-amd64 manifest. RepoDigest identity must match and `uname -m` must report `riscv64` under QEMU. The image removes runner registration payload and credentials and unsets GitHub/runtime tokens at entry.

`run.sh` is restricted to a reviewed disposable host, requires an exact SHA-256 image reference and QEMU binfmt, rejects credential-bearing environments, and applies wall-time, memory, CPU, and PID bounds. The worker resolves the same public image anonymously and passes its exact digest to Docker and pinned `gh-act` runner mappings without production secrets.
