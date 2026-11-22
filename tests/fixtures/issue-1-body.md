### Upstream repository

https://github.com/closeio/ciso8601

### PyPI distributions

ciso8601 (import: ciso8601)

### Upstream target branch

master

### Additional notes

Migration canary based on https://github.com/riseproject-dev/python-wheels/issues/631.
The downstream riscv64 wheel build was added in python-wheels#625.
The earlier investigation found that PyPI ciso8601 2.4.1 had no riscv64 wheel,
upstream issue closeio/ciso8601#181 requested support, and the project is a pure
C extension using setuptools and cibuildwheel with no third-party native library.
The earlier proposal selected configuration_integration and these bounded paths:
.github/workflows/build-wheels.yml, pyproject.toml, CHANGELOG.md, and tox.ini.
The expected topology is an ubuntu-24.04-riscv row with CIBW_ARCHS=riscv64,
following the project's existing native aarch64 row.
The previous implementation reached handoff/waiting_human at revision 9 and
reported clean head 9339ec54ae594458b0b9c6039bb55e9b2b13eff7.
Treat all previous dossier, CI, fork, PR, and head data as migration evidence.
Do not treat it as current authorization or verified state without the migration
and readback gates in this plan.
