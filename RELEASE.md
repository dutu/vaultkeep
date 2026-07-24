# Vaultkeep v1 Release Gate Checklist

Use this checklist from a clean checkout of the candidate tag. Tick each item only
after the command or manual check has completed on the stated host.

Record the candidate version and commit before testing:

- [ ] Candidate tag:
- [ ] Commit SHA:
- [ ] Tester:
- [ ] Date:

## Summary Gate Checklist

Do not publish v1 until every gate below is checked:

- [ ] Source and version gate passed.
- [ ] Local quality gate passed.
- [ ] Debian 12 `bookworm` amd64 release matrix gate passed.
- [ ] Debian 12 `bookworm` arm64 release matrix gate passed.
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
  ```

  Acceptable result:

  - Ruff format check reports no files requiring formatting.
  - Ruff lint reports no violations.
  - Mypy reports success.
  - Pytest reports no failures or errors.

---

## 3. Debian Release Matrix Gate

Run this section on each supported Debian release and architecture:

- [ ] Debian 12 `bookworm` amd64
- [ ] Debian 12 `bookworm` arm64
- [ ] Debian 13 `trixie` amd64
- [ ] Debian 13 `trixie` arm64

Each host must be disposable or snapshot-backed. It must run systemd as PID 1
and provide one dedicated writable CIFS mount and one dedicated writable NFS
mount for Vaultkeep release validation.

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
  - `VERSION_CODENAME` is `bookworm` or `trixie`.
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
  - `/etc/vaultkeep/jobs/example.yaml.disabled` exists and remains inactive.

- [ ] Verify installed command and timers.

  ```bash
  vaultkeep --version
  sudo vaultkeep timers validate
  systemd-analyze verify /etc/systemd/system/vaultkeep@.service /etc/systemd/system/vaultkeep@.timer
  ```

  Acceptable result:

  - `vaultkeep --version` prints exactly the candidate version.
  - Timer validation exits `0`.
  - `systemd-analyze verify` exits `0`.

### 4.2 Update

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
  sudo vaultkeep timers validate
  ```

  Acceptable result:

  - Command exits `0`.
  - `/opt/vaultkeep/current` points to the candidate release.
  - `vaultkeep --version` prints exactly the candidate version.
  - The previous release is retained for rollback.
  - Timer validation exits `0`.

### 4.3 Uninstall and Purge

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
  - `destination.require_mount` is `true` for CIFS and NFS.
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
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml timer install
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml timer status
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml timer next
  sudo vaultkeep timers list
  sudo vaultkeep timers validate
  sudo vaultkeep timers sync --dry-run
  ```

  Acceptable result:

  - Install exits `0`.
  - Status shows the corresponding `vaultkeep@<job-id>.timer`.
  - `timer next` prints a next elapse value.
  - `timers list` includes the job.
  - `timers validate` exits `0`.
  - Dry-run sync prints planned actions and makes no changes.

- [ ] Disable and remove the timer.

  ```bash
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml timer disable
  sudo vaultkeep --config /etc/vaultkeep/jobs/<job-id>.yaml timer remove
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
      "examples/vaultkeep-job.yaml.disabled",
      "README.md",
      "RELEASE.md",
      "architecture_and_design.md",
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
- [ ] All four Debian release matrix entries passed.
- [ ] Installer install/update/uninstall/purge passed.
- [ ] Restore drills passed for local, CIFS, and NFS destinations.
- [ ] Restore drills passed for `.tar.zst` and `.tar.7z`.
- [ ] Scheduling gate passed.
- [ ] Failure-injection gate passed.
- [ ] Security review gate passed.
- [ ] Packaging gate passed.
- [ ] No release-blocking defects remain open.

Release decision:

- [ ] Approved for v1 publication.
- [ ] Rejected; fixes required before retest.
