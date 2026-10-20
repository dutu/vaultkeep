# Development and testing

Use Python 3.13+ on Linux. Debian 13 amd64/arm64 is the release matrix. Core's strict
mypy boundary stays in place; the imported Trigger implementation keeps its existing
typing boundary via the `vaultkeep.trigger.*` override.

## Set up

From the repository root:

```sh
python3 -m venv /tmp/vaultkeep-dev
. /tmp/vaultkeep-dev/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
python -m pip download --require-hashes --only-binary=:all: \
    -r requirements.lock -d /tmp/vaultkeep-wheelhouse
```

The wheelhouse makes executed installer and installed-contract tests offline. Set
`VAULTKEEP_TEST_WHEELHOUSE` to another prepared directory if necessary. Production
installation does not use this test setting. Install GNU tar, zstd, Debian 7zip and
rsync for relevant tests. Build tooling is development-only except the locked
Hatchling dependencies used for local production package installation.

## Checks

```sh
ruff format --check .
ruff check .
mypy
pytest -vv
python scripts/check_packages.py
git diff --check
```

Root pytest runs backup, Trigger, installer, and installed-worker contract tests in
one process. Trigger fixtures live under `tests/trigger/` with explicit package
imports, so test modules do not collide.

Installer tests execute `install.sh` with temporary prefix/config/state/bin/systemd
roots, controlled service-manager responses, real venvs, hash-locked wheel installs
and entry-point relocation. Ownership tools are faked so tests need no root. They
install/enable no live units and touch no home data. Installed-worker tests use wheel
installs, disposable source/destination/runtime/state paths and fake absent wallet
processes, exercising a real core invocation from Trigger workers. No production
wallets, jobs or archives are used. Real archive tests use disposable files.

`check_packages.py` builds the unified wheel and source archive, independently
rebuilds the extracted archive, checks the single entry point and bundled Trigger
modules, installs into an isolated environment, validates all four job examples and
previews installation without touching services.

## Dependency locks

Regenerate using Python 3.13, preserving existing pins unless explicitly updating:

```sh
pip-compile --generate-hashes --allow-unsafe --no-emit-index-url --no-emit-trusted-host \
    --strip-extras --output-file requirements.lock requirements.in
pip-compile --generate-hashes --allow-unsafe --no-emit-index-url --no-emit-trusted-host \
    --strip-extras --output-file requirements-dev.lock requirements-dev.in
```

Both backup and Trigger runtime/build dependencies use `requirements.lock`. Verify
hash-locked binary downloads on both supported architectures; cross-platform download
checks availability and hashes, not execution on arm64.


## Validation limits

Automated checks use disposable files, fake managers, simulated process providers,
and installed CLI/worker contracts. They do not certify real app identity/writer
coverage, systemd user lifecycle/lingering, network restore behavior or arm64
execution. Run the [release gates](../../RELEASE.md) and
[deployment checks](../plans/trigger-validation.md) on prepared hosts. Keep app-specific
examples disabled until the installed process model has been reviewed.
