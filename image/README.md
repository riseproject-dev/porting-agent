# RISC-V reproduction image

`image/` builds `ghcr.io/riseproject-dev/porting-agent/ubuntu-24.04-riscv`. The trusted image workflow resolves the native runner base to an exact digest, builds only `linux/riscv64`, records the base digest in OCI labels, and publishes `latest` plus a base-digest-derived tag only outside pull requests.

After publishing, the workflow logs out of GHCR, removes Docker authentication, pulls the exact output digest anonymously, verifies RepoDigest identity, and requires `uname -m` to report `riscv64` under QEMU. The image removes runner registration payload and credentials and unsets GitHub/runtime tokens at entry.

`run.sh` is restricted to a reviewed disposable host, requires an exact SHA-256 image reference and QEMU binfmt, rejects credential-bearing environments, and applies wall-time, memory, CPU, and PID bounds. The worker resolves the same public image anonymously and passes its exact digest to Docker and pinned `gh-act` runner mappings without production secrets.
