# Development and testing

Use Python 3.13+ on Linux. Debian 13 amd64/arm64 is the release matrix. Strict mypy
covers core and the Trigger integration, including its configuration adapter, CLI,
parser, paths, environment, preflight and service management. The explicit
module list in `pyproject.toml` exempts only imported legacy internals that still need
gradual typing.

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
pytest --no-cov -q -k 'not test_real_header_encrypted_tar_7z_creation_and_verification and not test_real_header_encrypted_tar_7z_restore_drill'
python scripts/check_packages.py
git diff --check
```

Monitor-unit path tests use `systemd-analyze --user verify` and inspect its parsed
arguments, following [systemd quoting rules](https://www.freedesktop.org/software/systemd/man/devel/systemd.syntax.html).
They skip when the tool is absent or a sandbox blocks its `SO_PASSCRED` socket
operation. Run them outside that sandbox to validate the actual systemd parser;
verification does not start services.

The desktop-notification integration test creates disposable D-Bus servers and a
notification daemon, sends real `Notify` calls, and replaces the simulated desktop
session without restarting the notifier. It requires `dbus-daemon`, `busctl`, and
optional system Python `dbus`/`gi` bindings (`python3-dbus` and `python3-gi` on Debian).
It skips when those tools are unavailable or the sandbox blocks Unix sockets. It
does not contact a real desktop or change systemd services or manager environments.

Root pytest runs backup, unified status, Trigger, Schedule, installer, and installed-worker contract tests in
one process. Trigger fixtures live under `tests/trigger/` with explicit package
imports, so test modules do not collide.

Installer tests execute `install.sh` with temporary prefix/config/state/bin/systemd
roots, controlled service-manager responses, real venvs, hash-locked wheel installs
and entry-point relocation. Ownership tools are faked so tests need no root. They
install/enable no live units and touch no home data. Installed-worker tests use wheel
installs, disposable source/destination/runtime/state paths and fake absent wallet
processes, exercising a real core invocation from Trigger workers. No production
wallets, jobs or archives are used. The installed contract checks matching created,
unchanged, finalization-failure, and cleanup-failure results through manual commands,
rendered timer commands, and Trigger workers in both backup modes, including results
read back through the installed `status --json` command with matching mode, parameters,
and state environment. Process tests check SIGTERM completion evidence and SIGKILL
uncertainty. Status tests separate lock activity from missing, corrupt, inaccessible,
stale, and contradictory history and verify compact historical safety warnings,
verbose details, and JSON without mutation.
Real archive tests use disposable files.

`check_packages.py` builds the unified wheel and source archive, independently
rebuilds the extracted archive, checks the single entry point and bundled Trigger
modules, installs into an isolated environment, validates all four job examples and
previews installation without touching services.

## Dependency locks

Regenerate using Python 3.13, preserving existing pins unless explicitly updating:

```sh
pip-compile --generate-hashes --allow-unsafe --no-emit-index-url --no-emit-trusted-host --no-emit-find-links \
    --strip-extras --output-file requirements.lock requirements.in
pip-compile --generate-hashes --allow-unsafe --no-emit-index-url --no-emit-trusted-host --no-emit-find-links \
    --strip-extras --output-file requirements-dev.lock requirements-dev.in
```

For offline regeneration, prepare the wheelhouse above and set `PIP_NO_INDEX=1`
and `PIP_FIND_LINKS=/absolute/path/to/wheelhouse` for the same commands. The
`--no-emit-find-links` option keeps that local artifact source out of generated locks.
Do not change existing pins or hashes merely to change the generation environment.

Both backup and Trigger runtime/build dependencies use `requirements.lock`. Verify
hash-locked binary downloads on both supported architectures; cross-platform download
checks availability and hashes, not execution on arm64.


## Validation limits

The non-interactive command above excludes two Debian interactive `.tar.7z` creation/
restore tests that stall in this environment. An unfiltered `pytest` run therefore
has not completed here. Their exclusion is a validation limitation, not a passing
encrypted-backup release gate. Run the complete `pytest -vv` suite, including those
tests, on a host where the interactive 7zip checks can finish before release. See the
[local validation record](../plans/trigger-validation.md#local-validation-record).

Automated checks use disposable files, fake managers, simulated process providers,
and installed CLI/worker contracts. They do not certify real app identity/writer
coverage, systemd user lifecycle/lingering, network restore behavior or arm64
execution. Run the [release gates](../../RELEASE.md) and
[deployment checks](../plans/trigger-validation.md) on prepared hosts. Keep app-specific
examples disabled until the installed process model has been reviewed.
