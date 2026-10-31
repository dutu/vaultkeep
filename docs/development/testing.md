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

External notifier helper tests use fake executables without network access. They check CLI
errors, failure warnings, process-group termination, system-log failures, and a
failed delivery inside a successful backup hook. Package checks exercise the helper
through the wheel-installed CLI.

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

Maintain the following regression requirements alongside the
[architecture contract](../design/architecture_and_design.md#13-manifest-and-state).
The main focused suites are
[backup safety](../../tests/unit/test_backup_safety_redesign.py) and
[safety regressions](../../tests/unit/test_review_safety_regressions.py).

| Area | Required cases |
| --- | --- |
| Policy and snapshots | Both consistency modes with Trigger enabled/disabled; required shared identity; unsupported schema and incomplete fields; policy/identity fingerprint changes; frozen dispatch policy versus child setup; one effective configuration per execution. |
| Source and offline boundaries | Refreshed pre-archive snapshot, source changes, stream/structure/checksum failures, no engine process checks for `files`, every offline boundary per archive, restart in `after_archive` with no later source reads, release exactly once on failure, primary error preserved with release errors. |
| Metadata and classification | Explicit manifest/replacement versions, unsafe/oversized/unreadable markers, multiple suspects, original diagnostics preserved through durable updates and relocation, unsupported matching entries block deletion, operational/credential failures do not imply corruption. |
| Replacement compatibility | Content changes within equal coverage; independent classes for coverage/policy/application changes; format, compression, retention, password-path, and matcher differences; independently verified encryption changes; incompatible classes preserved without endless creation. |
| Replacement relationships | Missing, forged, cross-history, self/circular/conflicting IDs; later missing/corrupt replacements; reassignment; backward clock changes; fresh discovery after marker updates; interrupted/partial writes authorize no unsafe deletion. |
| Cleanup and partial failure | Complete per-history planning, last recovery point and replacement dependencies protected, failed suspect deletion followed by `UNCHANGED`, explicit/later-new-archive retry only, successful per-file cleanup despite another child's failure, deleted/excluded histories preserved, malformed-entry boundaries fail closed. |
| Failure results | Required state or hook failure after commit preserves archives; new committed corruption fails execution even during cleanup or marker-write failure; ordinary cleanup warnings preserve `created` and exact partial results; no cleanup after failed/suspect/unchanged creation. |
| Inspection and reporting | Metadata-only list/status/dry-run versus deletion-time SHA-256, no preview writes or repairs, all inventory decisions including disabled/unknown, inline suspect reason without unverified health claims, unavailable destination still permits local status, cleanup pending separate from recorded error, JSON and notifications preserve cleanup diagnostics. |
| Initiators | Identical engine behavior through Manual/Schedule/Trigger, worker idempotency and dispatch uncertainty, frozen identity checks, no replay or archive classification from monitoring gaps, independent notification switches, original result-marker interpretation. |

These are continuing regression requirements, not a claim that every release or
live-deployment scenario has passed. A recurring installer timeout must be investigated
and recorded by affected repair variant; a successful rerun does not erase an
intermittent failure.

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

## Application identity regression coverage

The shared identity/preparation contract is specified in
[application identity](../design/architecture_and_design.md#213-shared-application-identity).
Focused coverage lives in [application tests](../../tests/unit/test_application.py)
and [preparation tests](../../tests/unit/test_application_preparation.py), with
Schedule, Trigger, installer/maintenance, hashing, validation, and installed-worker
suites covering the consumers. Preserve these checks when changing that boundary:

| Area | Required cases |
| --- | --- |
| Detached identity | Raw fields unchanged, frozen runtime fields, nested matcher and serialized-container independence, equivalent repeated/raw-reconstructed resolution, explicit runtime serialization, stable canonical fingerprint and policy payloads including separate UID. |
| Scope and accounts | Execution UID despite misleading environment/YAML location, implicit/explicit root equivalence, root selecting another owner, non-root system rejection before state/locks/services even without an application, missing accounts and lookup failures, user override rejection before every account lookup with field and absolute YAML path. |
| Preparation | Optional/required/invalid identity outcomes, one resolution per ordinary loader/validator/adapter chain and combined/per-file operation, directory checks reuse resolved identity, schema-only performs no account/runtime checks, partial recovery/status remains useful. |
| Runtime revalidation | Same-looking processes under different UIDs, UID filtering before metadata access, PID reuse, ambiguous/unreadable evidence, account removal/remapping, ownership and symlink changes, authoritative Trigger parent/child policy comparison. |
| Inspection and deletion | `list`/`verify` survive missing sources, application executables/processes/accounts, and account-service failures without fabricated identity; existing credentials/member/destination/lock/suspect protections remain; inspection never enables unauthorized replacement or prune. |
| Consumers and transactions | Schedule install/update/bulk checks include disabled and parameter-deferred templates; installer configuration failures precede mutation; identity and later failures/signals preserve units, registry, rollback, and restoration; retained replacement identity remains username-based. |
| Dependencies | Import identity/preparation in different orders; inspect deferred imports too. Shared application code imports no validation, Backup workflows, or Trigger adapters, and offline enforcement/Trigger use the same detector. |

Use disposable paths and mocked accounts/processes/managers for failure cases. Run
the normal [checks](#checks), including package validation and the prepared installer
repair variants, after relevant implementation changes. Record failures, skips, and
environment limitations separately from passing results.
