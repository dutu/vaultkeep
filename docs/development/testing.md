# Development and testing

Use Python 3.13+ on Linux. Debian 13 amd64/arm64 is the release matrix. Core's strict
mypy boundary stays at `src/`; Trigger retains its separate pytest configuration.

## Set up

From the repository root:

```sh
python3 -m venv /tmp/vaultkeep-dev
. /tmp/vaultkeep-dev/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e . -e components/trigger
python -m pip download --require-hashes --only-binary=:all: \
    -r requirements-trigger.lock -d /tmp/vaultkeep-wheelhouse
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
(cd components/trigger && pytest -vv)
python scripts/check_packages.py
git diff --check
```

Root pytest runs core and cross-package tests. It does **not** run Trigger's suite.
Run Trigger in a separate process from its project directory; its `tests.conftest`
and `tests.test_monitor` imports must not collide with core's tests.

Installer tests execute `install.sh` with temporary prefix/config/state/bin/systemd
roots, controlled service-manager responses, real venvs, hash-locked wheel installs
and entry-point relocation. Ownership tools are faked so tests need no root. They
install/enable no live units and touch no home data. Cross-package tests use wheel
installs, disposable source/destination/runtime/state paths and fake absent wallet
processes, exercising a real core invocation from Trigger workers. No production
wallets, jobs or archives are used. Real archive tests use disposable files.

`check_packages.py` builds both wheels/sdists, independently rebuilds extracted
sdists, checks package/entry-point/dependency isolation, installs each wheel in an
isolated venv, and previews both installer selections from the root sdist. The
optional subproject needs no neighboring checkout to build.

## Dependency locks

Regenerate using Python 3.13, preserving existing pins unless explicitly updating:

```sh
pip-compile --generate-hashes --allow-unsafe --no-emit-index-url --no-emit-trusted-host \
    --strip-extras --output-file requirements.lock requirements.in
pip-compile --generate-hashes --allow-unsafe --no-emit-index-url --no-emit-trusted-host \
    --strip-extras --output-file requirements-trigger.lock requirements-trigger.in
pip-compile --generate-hashes --allow-unsafe --no-emit-index-url --no-emit-trusted-host \
    --strip-extras --output-file requirements-dev.lock requirements-dev.in
```

The combined input constrains every core dependency to `requirements.lock`. Verify
hash-locked binary downloads on both supported architectures; cross-platform download
checks availability and hashes, not execution on arm64.

## Validation record

The integration was checked locally on Debian 13 amd64, Python 3.13.5. The record
applies to this working-tree implementation; it is not a published-release approval.

- Locked binary dependencies, including psutil 7.2.2, downloaded with hash verification
  for amd64 and arm64 Python 3.13. Core dependency pins are unchanged.
- Both wheel/sdist builds, extracted independent builds, isolated wheel installs and
  root-sdist installer previews passed.
- Core/support/installed-contract/local-zstd checks: 281 passed; two encrypted tests
  deselected after the timeouts below. Separate Trigger suite: 217 passed. Executed
  installer suite: 50 passed, with seven affected cases rechecked after final host/
  staging validation changes.
- Installer transaction tests cover both selections, legacy upgrade, same-version
  addition, selection retention, exact-match verification/repair, metadata/ownership
  refusal, dry-run immutability, signals, rollback and uninstall data preservation.
- The installed cross-package worker check covers schema-only/full validation, runtime
  parameters, exact created/unchanged markers, exit-10 lock contention, failed core
  execution, and no replay of a completed attempt.
- Real local `.tar.zst` create/verification and restore checks passed. The existing
  interactive `.tar.7z` creation check did not finish within a 25-second diagnostic
  timeout in the sandbox and a 60-second retry outside it; encrypted restore remains
  unverified here. No archive runtime changes were made for this integration.

Static Trigger unit verification passed using private temporary runtime paths with no
user bus. Read-only live probes verified named-instance properties and absent-template
handling; bare template names are not valid `systemctl show` targets.

No real service installation, user-manager lifecycle exercise, wallet identity review,
CIFS/NFS restore drill or arm64 execution was performed. These remain required
[release gates](../../RELEASE.md) and [Trigger deployment checks](../plans/trigger-validation.md).
A passing fake-manager suite does not close them. Existing Trigger limitations
remain documented in its [design](../design/trigger.md) and operator references.
