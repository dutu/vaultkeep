# Vaultkeep Release Gate Checklist

Use this checklist from a clean checkout of the candidate tag. Tick each item only
after the command or manual check has completed on the stated host.

Record the candidate version and commit before testing:

- [ ] Candidate tag:
- [ ] Commit SHA:
- [ ] Tester:
- [ ] Date:

## Summary Gate Checklist

Do not publish a release until every gate below is checked:

- [ ] Source and version gate passed.
- [ ] Local quality gate passed.
- [ ] Debian 13 `trixie` amd64 release matrix gate passed.
- [ ] Debian 13 `trixie` arm64 release matrix gate passed.
- [ ] Installer first-install gate passed.
- [ ] Installer update gate passed.
- [ ] Installer normal-uninstall gate passed.
- [ ] Installer purge-uninstall gate passed.
- [ ] Restore drills passed on local filesystem destination.
- [ ] Restore drills passed on CIFS destination.
- [ ] Restore drills passed on NFS/NFS4 destination.
- [ ] Restore drills passed for `.tar.zst`.
- [ ] Restore drills passed for password-protected `.tar.7z`.
- [ ] Scheduling gate passed.
- [ ] Failure-injection gate passed.
- [ ] Security review gate passed.
- [ ] Packaging gate passed.
- [ ] Trigger integration/deployment gates below passed.
- [ ] No release-blocking defects remain open.

Acceptable result for every automated command: exit code `0`, no failed tests,
and no unexpected stderr. Skipped tests are acceptable only when the skip reason
matches the host capability being tested. On Debian release hosts, release-gate
tests must not be skipped.

---

## 1. Source and Version Gate

- [ ] Clean checkout is on the intended release tag.

  ```bash
  git status --short
  git rev-parse --short HEAD
  git describe --tags --exact-match
  ```

  Acceptable result:

  - `git status --short` prints nothing.
  - `git describe --tags --exact-match` prints the intended release tag.

- [ ] Project metadata version matches the intended tag.

  ```bash
  python3 - <<'PY'
  import tomllib
  from pathlib import Path

  data = tomllib.loads(Path("pyproject.toml").read_text())
  print(data["project"]["version"])
  PY
  ```

  Acceptable result:

  - The printed version is the intended release version.
  - The version is PEP 440-compatible.

---

## 2. Local Quality Gate

Run on a clean development host. Linux is preferred because the normal suite can
exercise real archive tools when available.

- [ ] Create and activate a fresh virtual environment.

  ```bash
  python3 -m venv .venv-release
  . .venv-release/bin/activate
  python -m pip install --upgrade pip
  ```

  Acceptable result:

  - The virtual environment activates.
  - `python --version` is a supported Python version for the gate host.

- [ ] Install locked development dependencies and the candidate package.

  ```bash
  python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
  python -m pip install --no-deps --no-build-isolation --editable .
  python -m pip download --require-hashes --only-binary=:all: -r requirements.lock -d /tmp/vaultkeep-wheelhouse
  ```

  Acceptable result:

  - Dependency installation succeeds with hash verification.
  - Editable install succeeds without installing undeclared runtime dependencies.

- [ ] Run formatting, linting, type checking, and tests.

  ```bash
  ruff format --check .
  ruff check .
  mypy
  pytest -vv
  python scripts/check_packages.py
  ```

  Acceptable result:

  - Ruff format check reports no files requiring formatting.
  - Ruff lint reports no violations.
  - Mypy reports success.
  - Pytest reports no failures or errors.

---

## 3. Debian Release Matrix Gate

Run this section for the unified Vaultkeep package on each supported Debian 13 architecture:

- [ ] Debian 13 `trixie` amd64
- [ ] Debian 13 `trixie` arm64

Each host must be disposable or snapshot-backed and run systemd as PID 1. Test the
same unified package on amd64 and arm64, including these deployment cases:

- [ ] Fresh unified installation, update, uninstall and purge.
- [ ] Schema-1 core deployment upgrades to the unified package.
- [ ] Schema-2 core-only deployment upgrades to the unified package.
- [ ] Schema-2 combined deployment upgrades to the unified package, retiring the old
      Trigger command and shared monitor while preserving user recovery evidence.

For every upgrade case, verify retained-release paths, data preservation and rollback
of the old links, templates and metadata. Core-only and combined layouts are legacy
inputs, not current installation selections.

Provide dedicated disposable user accounts with running user managers and one
writable CIFS mount and one writable NFS mount for release validation.

For each host, record:

- [ ] Host:
- [ ] Debian codename:
- [ ] Architecture:
- [ ] CIFS mount:
- [ ] NFS mount:

### 3.1 Host Preparation

- [ ] Confirm host identity.

  ```bash
  cat /etc/os-release
  uname -m
  systemctl --version
  test -d /run/systemd/system
  ```

  Acceptable result:

  - `ID=debian`.
  - `VERSION_CODENAME` is `trixie`; Python is >=3.13.
  - Architecture is the intended matrix entry.
  - `systemd` version is `247` or newer.
  - `/run/systemd/system` exists.

- [ ] Install required Debian packages.

  ```bash
  sudo apt-get update
  sudo apt-get install --yes python3 python3-venv tar zstd 7zip rsync util-linux mount
  ```

  Acceptable result:

  - Apt exits successfully.
  - `python3`, `tar`, `zstd`, `7z`, `rsync`, `findmnt`, `systemctl`, and
    `systemd-analyze` are present in `PATH`.

- [ ] Confirm CIFS and NFS mounts.

  ```bash
  export VAULTKEEP_RELEASE_CIFS_MOUNT=/mnt/vaultkeep-release-cifs
  export VAULTKEEP_RELEASE_NFS_MOUNT=/mnt/vaultkeep-release-nfs

  findmnt --target "$VAULTKEEP_RELEASE_CIFS_MOUNT" --output TARGET,FSTYPE,OPTIONS
  findmnt --target "$VAULTKEEP_RELEASE_NFS_MOUNT" --output TARGET,FSTYPE,OPTIONS

  sudo sh -c 'echo cifs-write-test > "$1/.vaultkeep-release-write-test" && rm "$1/.vaultkeep-release-write-test"' sh "$VAULTKEEP_RELEASE_CIFS_MOUNT"
  sudo sh -c 'echo nfs-write-test > "$1/.vaultkeep-release-write-test" && rm "$1/.vaultkeep-release-write-test"' sh "$VAULTKEEP_RELEASE_NFS_MOUNT"
  ```

  Acceptable result:

  - CIFS mount reports `FSTYPE` as `cifs`.
  - NFS mount reports `FSTYPE` as `nfs` or `nfs4`.
  - Both write tests succeed and leave no test file behind.

### 3.2 Release-Gate Tests

- [ ] Run opt-in release gates as root.

  ```bash
  sudo env \
    VAULTKEEP_RELEASE_GATE=1 \
    VAULTKEEP_RELEASE_CIFS_MOUNT="$VAULTKEEP_RELEASE_CIFS_MOUNT" \
    VAULTKEEP_RELEASE_NFS_MOUNT="$VAULTKEEP_RELEASE_NFS_MOUNT" \
    python3 -m pytest -vv -m release_gate
  ```

  Acceptable result:

  - Pytest exits `0`.
  - Release-gate tests are collected and run.
  - No release-gate test is skipped.
  - The host check confirms Debian, root, systemd, and required tools.
  - The mount check confirms CIFS and NFS filesystem types.
  - Installer dry-runs complete or report the explicitly accepted clean-host
    update precondition.

- [ ] Run the complete test suite on the same host.

  ```bash
  python3 -m pytest -vv
  ```

  Acceptable result:

  - Pytest exits `0`.
  - The real GNU TAR/Zstandard archive tests run.
  - On Debian, the interactive encrypted `.tar.7z` tests run.
  - Restore-drill integration tests pass.

---

## 4. Installer Gate

Run on a disposable Debian release host. This gate mutates `/opt/vaultkeep`,
`/usr/local/bin/vaultkeep`, `/etc/vaultkeep`, `/var/lib/vaultkeep`, and
`/etc/systemd/system`.

### 4.1 First Install

- [ ] Preview first install.

  ```bash
  sudo ./install.sh install --dry-run
  ```

  Acceptable result:

  - Command exits `0`.
  - Output is a plan only.
  - No Vaultkeep files are created or changed.

- [ ] Apply first install.

  ```bash
  sudo ./install.sh install
  ```

  Acceptable result:

  - Command exits `0`.
  - `/opt/vaultkeep/current` points to the installed release.
  - `/usr/local/bin/vaultkeep` points to the active release executable.
  - `/etc/systemd/system/vaultkeep@.service` and
    `/etc/systemd/system/vaultkeep@.timer` exist.
  - `/etc/vaultkeep/jobs/vaultkeep-schedule.yaml.example` exists and remains inactive.

- [ ] Verify installed command and timers.

  ```bash
  vaultkeep --version
  sudo vaultkeep schedules validate
  systemd-analyze verify /etc/systemd/system/vaultkeep@.service /etc/systemd/system/vaultkeep@.timer
  ```

  Acceptable result:

  - `vaultkeep --version` prints exactly the candidate version.
  - Timer validation exits `0`.
  - `systemd-analyze verify` exits `0`.

### 4.2 Update

Review [automatic maintenance](README.md#pause-and-resume-for-maintenance).
Keep existing timers/monitors active to verify automatic pause and resume, with no
backup work running; also verify safe refusal while a disposable backup is active.
Use an already installed previous Vaultkeep release as the starting point, then
run this candidate checkout as the update source.

- [ ] Preview update.

  ```bash
  sudo ./install.sh update --dry-run
  ```

  Acceptable result:

  - Command exits `0`.
  - Output is a plan only.
  - Existing active release remains unchanged.

- [ ] Apply update.

  ```bash
  sudo ./install.sh update
  vaultkeep --version
  sudo vaultkeep schedules validate
  ```

  Acceptable result:

  - Command exits `0`.
  - `/opt/vaultkeep/current` points to the candidate release.
  - `vaultkeep --version` prints exactly the candidate version.
  - The previous release is retained for rollback.
  - Timer validation exits `0`.

### 4.3 Uninstall and Purge

First stop/disable the relevant root and per-user schedules and drain work as described
in [uninstallation](README.md#uninstall-the-application). Inspect offline-account
enablement as well. Do not let the installer terminate backups to make checks pass.

- [ ] Preview normal uninstall.

  ```bash
  sudo /opt/vaultkeep/current/src/install.sh uninstall --dry-run
  ```

  Acceptable result:

  - Command exits `0`.
  - Output is a plan only.
  - Configuration, secrets, local job state, and backup destinations remain untouched.

- [ ] Apply normal uninstall.

  ```bash
  sudo /opt/vaultkeep/current/src/install.sh uninstall
  ```

  Acceptable result:

  - Command exits `0`.
  - Vaultkeep application tree, executable link, managed systemd templates, timer
    registry, and temporary files are removed.
  - `/etc/vaultkeep` is preserved.
  - `/var/lib/vaultkeep/jobs` is preserved.
  - Backup destinations are not touched.

- [ ] Reinstall, then apply purge uninstall.

  ```bash
  sudo ./install.sh install
  sudo /opt/vaultkeep/current/src/install.sh uninstall --purge
  ```

  Acceptable result:

  - Command exits `0`.
  - Vaultkeep application files are removed.
  - `/etc/vaultkeep` and `/var/lib/vaultkeep` are removed only if they are
    installer-managed paths.
  - Backup destinations are not touched.

---

## 5. Public Workflow Restore Drills

Run for every destination type and archive format:

Destination types:

- [ ] Local filesystem
- [ ] CIFS mount
- [ ] NFS/NFS4 mount

Archive formats:

- [ ] `.tar.zst`
- [ ] password-protected `.tar.7z`

For each combination, create a fixture containing:

- regular files;
- nested directories;
- spaces in filenames;
- unusual UTF-8 filenames;
- symlinks where the filesystem supports them;
- at least one excluded file or directory.

- [ ] Create a job config for the destination and archive format.

  Required config properties:

  - `job.id` matches the config filename stem.
  - `destination.root` points to the tested destination.
  - `destination.marker_file` is present and exists in the destination.
  - `destination.mount_point` is set to the actual CIFS or NFS mount path.
  - `archive.format` is the format being tested.
  - `encryption.mode` is `password` only for `.tar.7z`.
  - `encryption.password_file` points to a root-owned mode-`0600` file for
    `.tar.7z`.

- [ ] Validate the job.

  ```bash
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml validate
  ```

  Acceptable result:

  - Command exits `0`.
  - Output is `validate: valid`.

- [ ] Run the backup.

  ```bash
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml run
  ```

  Acceptable result:

  - Command exits `0`.
  - Output is `run: created`.
  - Exactly one final backup directory appears in the destination.
  - The final backup directory contains archive, checksum sidecar, and manifest.
  - No `.partial-vaultkeep-*` directory remains.

- [ ] Verify and list through the public CLI.

  ```bash
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml verify
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml list
  ```

  Acceptable result:

  - Both commands exit `0`.
  - `verify` reports `verify: verified`.
  - `list` reports at least one backup.

- [ ] Restore with standard Debian tools, not Vaultkeep internals.

  For `.tar.zst`:

  ```bash
  mkdir -p /tmp/vaultkeep-restore
  zstd -q -d -c /path/to/archive.tar.zst | tar --extract --file=- --directory=/tmp/vaultkeep-restore
  ```

  For `.tar.7z`:

  ```bash
  mkdir -p /tmp/vaultkeep-restore
  7z x -so -bd -p /path/to/archive.tar.7z | tar --extract --file=- --directory=/tmp/vaultkeep-restore
  ```

  Acceptable result:

  - Restore command exits `0`.
  - Restored paths match the source paths included by the config.
  - Excluded paths are absent.
  - File contents match.
  - Symlink targets match where symlinks are supported.
  - Permissions, mtimes, and numeric ownership match where the filesystem and
    restore privileges permit.

---

## 6. Scheduling Gate

Run after the installer gate on a systemd Debian host.

- [ ] Install or update a timer for a disabled test job changed to
  `schedule.enabled: true`.

  ```bash
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml schedule install
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml status
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml schedule next
  sudo vaultkeep schedules list
  sudo vaultkeep schedules validate
  sudo vaultkeep schedules sync --dry-run
  ```

  Acceptable result:

  - Install exits `0`.
  - Status shows the corresponding `vaultkeep@<job-id>.timer`.
  - `schedule next` prints a next elapse value.
  - `schedules list` includes the job.
  - `schedules validate` exits `0`.
  - Dry-run sync prints planned actions and makes no changes.

- [ ] Disable and remove the timer.

  ```bash
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml schedule disable
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml schedule remove
  ```

  Acceptable result:

  - Both commands exit `0`.
  - The timer is disabled.
  - The owned drop-in directory for the job is removed.

---

## 7. Failure-Injection Gate

Run only on disposable hosts or disposable destinations.

- [ ] Interrupted archive creation leaves no finalized backup.

  Test method:

  - Start a backup against a large fixture.
  - Kill the `vaultkeep` process before archive creation completes.
  - Inspect the destination.

  Acceptable result:

  - No final backup directory appears for the interrupted run.
  - Any partial directory is either absent or clearly named `.partial-vaultkeep-*`.
  - The previous successful backup remains intact.

- [ ] Interrupted encrypted archive creation removes private plaintext TAR.

  Test method:

  - Run a password-protected `.tar.7z` backup.
  - Interrupt during encrypted archive creation.
  - Inspect `/var/lib/vaultkeep/tmp`.

  Acceptable result:

  - No private inner `.tar` remains in `/var/lib/vaultkeep/tmp`.
  - No password appears in command output, process arguments, environment, or logs.

- [ ] Finalization never overwrites an existing backup directory.

  Test method:

  - Create a destination directory with the exact name Vaultkeep would finalize to,
    or reproduce the unit-level collision case.
  - Run the backup or finalization path.

  Acceptable result:

  - Command fails safely.
  - Existing directory contents are preserved byte-for-byte.
  - Staging content is not committed over existing data.

- [ ] Hook failure and timeout produce documented error behavior.

  Test method:

  - Configure a hook that exits non-zero.
  - Configure a hook that sleeps past `timeout_seconds`.
  - Run the public workflow.

  Acceptable result:

  - Hook failures map to exit code `11`.
  - Timeout terminates the hook process group.
  - Local state records the failed run when prior state exists.
  - Hook output is bounded and respects `logging.include_command_output`.

- [ ] Installer failure before activation leaves active release unchanged.

  Test method:

  - On a host with an existing installation, inject a failure before the current
    symlink switch, for example by making the staged release path impossible to
    create.

  Acceptable result:

  - Installer exits non-zero.
  - `/opt/vaultkeep/current` still points to the original release.
  - `/usr/local/bin/vaultkeep` still points to the original executable.
  - Existing systemd unit files and timer registry remain usable.

- [ ] Installer failure after activation rolls back.

  Test method:

  - On a snapshot-backed host, inject a failure after activation.
  - Inspect release symlinks, executable link, unit files, and timer registry.

  Acceptable result:

  - Installer exits non-zero.
  - Previous release is restored as active.
  - Unit files, timer registry, and executable link match the previous working
    installation.

---

## 8. Security Review Gate

Review the candidate against these invariants:

- [ ] Passwords never appear in argv.
- [ ] Passwords never appear in environment variables.
- [ ] Passwords never appear in manifests, checksums, hook contexts, logs, or
  test diagnostics.
- [ ] Encrypted `.tar.7z` archives use encrypted headers.
- [ ] Private plaintext TAR files live only under `/var/lib/vaultkeep/tmp`.
- [ ] Private plaintext TAR workspaces are root-owned and mode `0700`.
- [ ] Job configs are root-owned and not writable by group or other users.
- [ ] Secret files are root-owned and mode `0600`.
- [ ] Installer-owned paths are removed only through the ownership manifest.
- [ ] Normal uninstall preserves `/etc/vaultkeep` and `/var/lib/vaultkeep/jobs`.
- [ ] Purge never deletes backup destinations.

Acceptable result:

- Every invariant is confirmed by code review, tests, or direct host inspection.
- Any exception is fixed before release.

---

## 9. Packaging Gate

- [ ] Build release artifacts from the candidate checkout.

  ```bash
  python -m hatchling build
  ```

  Acceptable result:

  - Build exits `0`.
  - `dist/` contains one source distribution and one wheel for the candidate
    version.

- [ ] Inspect source distribution contents.

  ```bash
  python - <<'PY'
  import tarfile
  from pathlib import Path

  sdists = sorted(Path("dist").glob("vaultkeep-*.tar.gz"))
  if len(sdists) != 1:
      raise SystemExit(f"expected exactly one sdist, found {len(sdists)}")
  with tarfile.open(sdists[0]) as archive:
      names = set(archive.getnames())
  required = [
      "install.sh",
      "requirements.lock",
      "systemd/vaultkeep@.service",
      "systemd/vaultkeep@.timer",
      "systemd/vaultkeep-trigger-exec@.service",
      "src/vaultkeep/trigger/cli.py",
      "examples/vaultkeep-schedule.yaml.example",
      "examples/electrum-trigger.yaml.example",
      "examples/bisq-trigger.yaml.example",
      "examples/wasabi-trigger.yaml.example",
      "README.md",
      "RELEASE.md",
      "docs/design/architecture_and_design.md",
  ]
  root = sdists[0].name.removesuffix(".tar.gz")
  for item in required:
      candidate = f"{root}/{item}"
      if candidate not in names:
          raise SystemExit(f"missing from sdist: {item}")
  print(sdists[0])
  PY
  ```

  Acceptable result:

  - Script exits `0`.
  - All required operational files are present in the source distribution.

- [ ] Verify wheel console script.

  ```bash
  tmp="$(mktemp -d)"
  python -m venv "$tmp/venv"
  "$tmp/venv/bin/python" -m pip install --require-hashes --only-binary=:all: -r requirements.lock
  "$tmp/venv/bin/python" -m pip install --no-deps --no-index --find-links dist vaultkeep
  "$tmp/venv/bin/vaultkeep" --version
  rm -rf "$tmp"
  ```

  Acceptable result:

  - Wheel installs from local `dist/`.
  - `vaultkeep --version` prints exactly the candidate version.

---

## 10. Release Decision

- [ ] Local quality gate passed.
- [ ] Unified installation and schema-1/schema-2 upgrade cases passed on both Debian 13 architectures.
- [ ] Installer install/update/uninstall/purge passed.
- [ ] Restore drills passed for local, CIFS, and NFS destinations.
- [ ] Restore drills passed for `.tar.zst` and `.tar.7z`.
- [ ] Scheduling gate passed.
- [ ] Failure-injection gate passed.
- [ ] Security review gate passed.
- [ ] Packaging gate passed.
- [ ] Trigger integration/deployment gates below passed.
- [ ] No release-blocking defects remain open.

Release decision:

- [ ] Candidate approved for publication.
- [ ] Rejected; fixes required before retest.

## Trigger integration and transition gates

Use disposable users, fake wallet processes, and temporary backup jobs. Record the
candidate/host/interpreter/commands/results for each test; production wallet
activation is not part of this checklist.

- [ ] The unified package contains Trigger modules and psutil, exposing only the vaultkeep entry point.
- [ ] Fresh installation installs the unified command and inactive user worker template;
      works without logged-in users, keeps new monitoring opt-in, and never enables
      schedules or lingering. Stopped user managers remain stopped.
- [ ] The unified wheel and source archive pass `python scripts/check_packages.py`;
      the archive supports installation without a neighboring checkout.
- [ ] Debian 12 and an incompatible existing interpreter are rejected before apt/staging.
      A clean Debian 13 host missing Python can bootstrap Debian Python, then checks >=3.13.
- [ ] Schema-1 core, schema-2 core-only, and schema-2 combined deployments upgrade to
      the unified package on both architectures, preserving data and retaining the old
      deployment. Upgrade Debian 12 before this transition.
- [ ] Same-version reinstall, later-version upgrade and version/source guards pass.
- [ ] Per-job monitor service, lock, recovery and worker namespaces stay isolated.
- [ ] YAML validation rejects simultaneously enabled scheduling and shutdown triggering.
- [ ] Legacy recovery evidence blocks new monitors until reviewed and archived.
- [ ] Damaged packages/dependencies/entry points rebuild safely; missing owned artifacts
      reconcile; modified units, masks, overrides and unrelated links are preserved/refused.
- [ ] Root and multiple/lingering user managers are inventoried twice; paused schedules,
      active/transitioning backups/workers, queued jobs, held foreground locks, unreachable
      managers and a newly appearing manager are handled conservatively.
- [ ] Enabled-active, enabled-inactive, disabled-active and disabled-inactive schedules
      retain exact enablement/runtime links and persistent timer timestamps after automatic
      pause, success, refusal, INT/TERM/HUP and rollback. Only originally active launchers
      receive stop/start calls; backup services and workers never receive stop calls.
- [ ] Injection at dependency/venv/package install, relocated entry points, package/example/
      unit validation, each artifact/manifest write, activation and system/user reload restores
      old software and all managed artifacts. Restoration failures retain useful snapshots
      and report each failed path/account. Test fresh installation, legacy upgrades, reinstall and repair cases.
- [ ] Existing root/user jobs refresh with original paths/params, including disabled
      monitors and offline accounts in standard locations. Local overrides and changed
      identities cause refusal. Generated-unit rollback preserves file modes/timestamps.
- [ ] Only previously active schedules resume automatically after validation. Persistent
      calendar catch-up works normally; the installer directly replays no completed attempt.
- [ ] Shared maintenance locking blocks new normal CLI work while installation runs;
      legacy/custom launchers remain controlled during the first upgrade. Post-commit
      resume failures retain the deployment and recovery journal and attempt remaining units.
- [ ] Real user units accept an explicit job-specific monitor --config path, retain exact worker
      semantics, preserve independent worker lifetime and handle queued/activating states.
- [ ] Standalone migration preserves IDs and schema-1 instance/request/launch/receipt/block
      evidence; corrupt/unknown records still block, completed attempts never replay, and
      pending requests recheck absence. Effective units resolve to shared executables.
- [ ] Installed core/Trigger validation, created/unchanged markers, params, exit 10,
      shared manual/timer/worker lock contention, errors/signals and version warnings pass.
- [ ] Both selections uninstall normally and with purge after upgrade/addition; refuse
      active/queued work and enabled schedules; preserve every user's Trigger evidence,
      core user data, wallets, archives and unrelated units. Inspect offline enablement too.
- [ ] Run the remaining [Trigger deployment checks](docs/plans/trigger-validation.md),
      including process identity/writer review, systemctl rejection classification,
      notifications and logout/shutdown limits. Keep unverified example instances disabled.

Current local execution and remaining limits are recorded in
[development/testing](docs/development/testing.md#validation-limits). Automated
fake-manager success does not satisfy the live Debian release matrix.
