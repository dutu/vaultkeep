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

The wheelhouse makes executed installer and installed-contract tests offline. These
tests skip with a preparation hint when compatible wheels for any locked dependency
are missing. Tests that do not install packages still run without a wheelhouse.
Set `VAULTKEEP_TEST_WHEELHOUSE` to another prepared directory if necessary. Production
installation does not use this test setting. Prepare the complete wheelhouse before
CI or release validation so the installation checks run. Install GNU tar, zstd,
Debian 7zip and rsync for relevant tests. Build tooling is development-only except the locked
Hatchling dependencies used for local production package installation.

## Checks

```sh
ruff format --check .
ruff check .
mypy
pytest --no-cov -q
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

Notification unit tests cover independent category settings, lock-gated execution
start, lifecycle phase throttling, per-file job streams, failure diagnostics, and
bounded final delivery. Subprocess tests exercise manual, rendered Schedule, and
Trigger worker initiation, plus handled SIGTERM and unavailable SIGKILL completion
evidence. Trigger tests verify receipt recovery and orchestration deduplication
without ordinary backup result alerts or backup-progress polling.

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
stale, and contradictory history and verify cleanup-pending versus unresolved
suspects, notification cleanup diagnostics, verbose details, and JSON without mutation.
Safety regressions cover owner-correct home and process working-directory resolution,
post-commit corruption outcomes, required metadata versions, backward clock changes,
circular and conflicting references, interrupted marker updates, and committed
replacements across archive formats and encryption changes.
Real archive tests use disposable files. Each test in `test_archive_tools.py` has a
60-second deadline, including encrypted creation, verification, and restore; a
timeout fails the test and lets the archive runners clean up their subprocesses.
The production password-prompt wait has a separate 30-second deadline, without
limiting archive processing time. Regression tests cover missing and unexpected
prompts and subprocess cleanup for commands and pipelines.
Manual verification tests cover both formats, missing sources, invalid archives
with matching checksums, wrong passwords,
unsafe TAR paths, and preservation of backup and state files.

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

The commands above include the Debian interactive `.tar.7z` creation/restore tests;
they skip on other distributions or when archive tools are unavailable. Earlier
local runs excluded these tests after stalls, as recorded in the historical
[local validation record](../plans/trigger-validation.md#local-validation-record).
Run the complete `pytest -vv` suite with the prepared wheelhouse and all required
tools on a Debian release host before release. Skipped tests do not count as passing
release gates.

Automated checks use disposable files, fake managers, simulated process providers,
and installed CLI/worker contracts. They do not certify real app identity/writer
coverage, systemd user lifecycle/lingering, network restore behavior or arm64
execution. Run the [release gates](../../RELEASE.md) and
[deployment checks](../plans/trigger-validation.md) on prepared hosts. Keep app-specific
examples disabled until the installed process model has been reviewed.

## Backup safety regression coverage

Schema/layout-2 tests use disposable sources, destinations and local state. The
safety suite covers offline boundaries and release hooks, source refresh, physical
history locks across aliases and per-file/direct-child access, suspect markers and compatible
references, protected dependencies, unsupported/malformed barriers, cleanup warnings,
and failure after commitment. Metadata-only inventory/status and dry-run checks do
not read archives or write classification. Installed workers exercise the same
engine for both backup modes. Installer fixtures copy only shipped templates and
exclude private local examples. Filesystem and live application release gates remain
separate from these local tests.

Network regressions emulate mount metadata while using real file locks, separate
process contention, real archive tools, both backup modes, and encrypted/unencrypted
archives. They cover unsafe mount options and lock anchors, permissions/server-lock
failures, the NFS rename fallback and its lock requirement, existing-target safety,
verification, suspect replacement, and pruning. They do not establish live server
or cross-machine semantics.

To test actual shares, mount disposable CIFS and NFS test shares with server locking
enabled, and grant the test account permission to create `.vaultkeep.lock` in each
temporary history directory. Then run:

```sh
VAULTKEEP_TEST_CIFS_ROOT=/mnt/vaultkeep-test-cifs \
VAULTKEEP_TEST_NFS_ROOT=/mnt/vaultkeep-test-nfs \
pytest --no-cov -q tests/integration/test_network_destinations.py -k live_network
```

These opt-in tests create and remove private test directories, including their
history lock files after all writers have stopped. They check real
commit/verify/retention and separate process contention. Release validation must additionally check contention from two
writing machines, server recovery, and the deployed mount/permission configuration.
