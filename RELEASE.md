# Vaultkeep Release Checklist

This checklist is the v1 release gate. Run it from a clean checkout of the
candidate tag.

## Local Quality Gate

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation --editable .
ruff format --check .
ruff check .
mypy
pytest -vv
```

The normal test suite includes real `tar`/`zstd` archive integration on Linux.
On Debian release hosts it also includes the interactive `.tar.7z` integration
and restore drills.

## Debian Release Gate

Run the release gate on each supported Debian release and architecture:

- Debian 12 `bookworm` amd64
- Debian 12 `bookworm` arm64
- Debian 13 `trixie` amd64
- Debian 13 `trixie` arm64

The host must run systemd as the active system manager and provide one CIFS
mount and one NFS mount dedicated to release validation.

```bash
sudo apt-get update
sudo apt-get install --yes python3 python3-venv tar zstd 7zip rsync util-linux mount

sudo env \
  VAULTKEEP_RELEASE_GATE=1 \
  VAULTKEEP_RELEASE_CIFS_MOUNT=/mnt/vaultkeep-release-cifs \
  VAULTKEEP_RELEASE_NFS_MOUNT=/mnt/vaultkeep-release-nfs \
  python3 -m pytest -vv -m release_gate
```

Then run the complete suite on the same host:

```bash
python3 -m pytest -vv
```

## Installer Gate

Preview and apply a first install:

```bash
sudo ./install.sh install --dry-run
sudo ./install.sh install
vaultkeep --version
sudo vaultkeep timers validate
```

Preview and apply an update from a newer checkout:

```bash
sudo ./install.sh update --dry-run
sudo ./install.sh update
vaultkeep --version
sudo vaultkeep timers validate
```

Preview and apply uninstall modes:

```bash
sudo /opt/vaultkeep/current/src/install.sh uninstall --dry-run
sudo /opt/vaultkeep/current/src/install.sh uninstall
sudo ./install.sh install
sudo /opt/vaultkeep/current/src/install.sh uninstall --purge
```

## Restore Drills

For each supported archive format and destination type:

- create a fixture containing files, directories, spaces, unusual names, and
  symlinks where the filesystem supports them;
- create a backup through the public `vaultkeep run` workflow;
- restore with standard Debian tools, not Vaultkeep internals;
- compare paths, file content, symlink targets, permissions, mtimes, and numeric
  ownership where permitted.

The automated archive restore drills cover `.tar.zst` and `.tar.7z` for local
Linux filesystems. CIFS and NFS restore drills are release-host gates.

## Failure Injection

Exercise at least these cases on a disposable host:

- interrupted archive creation leaves no finalized backup;
- interrupted encrypted archive creation removes the private plaintext TAR;
- finalization never overwrites an existing backup directory;
- hook failure and timeout produce the documented exit code and state result;
- installer failure before activation leaves the active release unchanged;
- installer failure after activation restores the previous release, unit files,
  timer registry, and executable link.

## Security Review

Review the candidate against these v1 invariants:

- passwords never appear in argv, environment variables, manifests, checksums,
  hook contexts, logs, or test diagnostics;
- encrypted `.tar.7z` archives use encrypted headers;
- private plaintext TAR files live only under `/var/lib/vaultkeep/tmp` in a
  root-owned mode-0700 workspace;
- job configs are root-owned and not writable by group or other users;
- secret files are root-owned mode 0600;
- installer-owned paths are removed only through the ownership manifest;
- normal uninstall preserves `/etc/vaultkeep` and `/var/lib/vaultkeep/jobs`;
- purge never deletes backup destinations.

## Packaging

Build artifacts from the checked-out release:

```bash
python -m hatchling build
```

The produced source distribution must include `install.sh`, `requirements.lock`,
`systemd/`, `examples/`, `README.md`, and `architecture_and_design.md`. The wheel
must install the `vaultkeep` console script.
