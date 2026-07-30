# Vaultkeep

Vaultkeep is a backup application for Debian systems. It creates independent archive files from one or more files and directories, skips unchanged sources, applies calendar-based retention, and runs either manually or through managed systemd timers.

This file is the shared user guide for both operating modes. For mode-specific setup and command examples, see the [root mode guide](docs/root-mode.md) or [user mode guide](docs/user-mode.md). For configuration rules, data structures, security decisions, workflow details, and implementation requirements, see [architecture_and_design.md](architecture_and_design.md).

## Table of contents

- [Overview](#overview)
  - [What Vaultkeep does](#what-vaultkeep-does)
  - [How a backup works](#how-a-backup-works)
  - [Operating modes](#operating-modes)
  - [Important boundaries](#important-boundaries)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [Install the application](#install-the-application)
  - [Update the application](#update-the-application)
  - [Uninstall the application](#uninstall-the-application)
- [Usage](#usage)
  - [Choose a mode-specific workflow](#choose-a-mode-specific-workflow)
  - [Minimal job configuration](#minimal-job-configuration)
  - [Destination name templates](#destination-name-templates)
  - [Runtime template parameters](#runtime-template-parameters)
  - [CLI reference](#cli-reference)
  - [Check the installed version](#check-the-installed-version)
  - [Validate a job configuration](#validate-a-job-configuration)
  - [Update a job configuration](#update-a-job-configuration)
  - [Run a job manually](#run-a-job-manually)
  - [List job backups](#list-job-backups)
  - [Verify job backups](#verify-job-backups)
  - [Prune job backups](#prune-job-backups)
  - [Configure a job timer](#configure-a-job-timer)
  - [Manage all job timers](#manage-all-job-timers)
  - [Password-protected backups](#password-protected-backups)
  - [Lifecycle hooks](#lifecycle-hooks)
  - [Restore an unencrypted backup](#restore-an-unencrypted-backup)
  - [Restore an encrypted backup](#restore-an-encrypted-backup)
  - [Logs and troubleshooting](#logs-and-troubleshooting)
- [Design and implementation reference](#design-and-implementation-reference)

## Overview

### What Vaultkeep does

Vaultkeep creates file-based backups and applies retention. You define backup jobs as YAML files. Each job can be run manually with the `vaultkeep` command or automatically through a managed systemd timer.

Vaultkeep has two operating modes:

- root mode, for system backups managed by an administrator;
- user mode, for per-user backups that do not require sudo after Vaultkeep and its Debian package dependencies have been installed by root.

A job specifies:

- one or more source files or directories;
- optional source exclusions;
- one local, CIFS-mounted, or NFS-mounted destination;
- an archive format;
- a retention policy;
- an optional systemd schedule;
- optional lifecycle hooks.

For each run, Vaultkeep validates the job, checks whether the selected sources changed, creates one independent backup directory when needed, verifies the archive, writes a manifest, and applies the configured retention policy. If nothing relevant changed, the run finishes as `unchanged` and does not create a new archive.

Backups are ordinary archive files:

- unencrypted `.tar.zst` archives;
- password-protected `.tar.7z` archives containing one inner TAR.

Each backup is self-contained and includes the archive, a SHA-256 checksum, and a JSON manifest. Retention can keep hourly, daily, weekly, monthly, and yearly backups. Local state is only a cache; Vaultkeep can reconstruct it from destination manifests if the local state file is missing or unusable.

Root-mode jobs run as root because they can read system files, access protected secrets, inspect mounts, and execute administrator-configured hooks. User-mode jobs run as the calling user and can only access files, destinations, secrets, and hooks available to that user.

### Operating modes

Root mode and user mode are independent workflows using the same centrally installed `vaultkeep` command.

Root mode:

- uses jobs below `/etc/vaultkeep/jobs`;
- uses local state below `/var/lib/vaultkeep`;
- uses system timers below `/etc/systemd/system`;
- runs scheduled jobs as root;
- requires `sudo` for installation, job management, timer management, and real backup operations.

User mode:

- uses jobs below `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/jobs`;
- uses local state below `${XDG_STATE_HOME:-~/.local/state}/vaultkeep`;
- uses temporary files below `${XDG_CACHE_HOME:-~/.cache}/vaultkeep`;
- uses locks below `$XDG_RUNTIME_DIR/vaultkeep/locks`, or `${XDG_CACHE_HOME:-~/.cache}/vaultkeep/locks` when `XDG_RUNTIME_DIR` is unavailable;
- uses systemd user timers below `${XDG_CONFIG_HOME:-~/.config}/systemd/user`;
- runs jobs as the calling user with `vaultkeep --user`;
- does not require sudo for job configuration, manual execution, or user timer management.

The two modes do not share job configuration, timer units, timer registries, state files, or locks. A machine can have root-mode jobs installed and scheduled while one or more users independently maintain user-mode jobs.

Root must still install Vaultkeep and the required Debian packages. User mode assumes the centrally installed `/usr/local/bin/vaultkeep` command and archive tools such as `tar`, `zstd`, and `7z` already exist.

Mode-specific guides:

- [Root mode guide](docs/root-mode.md)
- [User mode guide](docs/user-mode.md)

### How a backup works

For a normal run, Vaultkeep:

1. validates the job and destination;
2. executes `before_check` when configured;
3. discovers and hashes the selected sources;
4. reconstructs local state from destination manifests when required;
5. returns `unchanged` when the source and backup-relevant configuration have not changed;
6. otherwise creates and verifies a new archive in a temporary backup directory;
7. atomically renames the completed directory to its final name;
8. applies the configured retention policy;
9. records the final result in local state.

Each final backup directory contains:

```text
backup-app-20260723T090000Z-550e8400e29b41d4a716446655440000/
├── backup-app-20260723T090000Z.tar.zst
├── backup-app-20260723T090000Z.tar.zst.sha256
└── backup-app-20260723T090000Z.json
```

`destination.name_template` produces the shared base name. Vaultkeep appends the backup ID only to the directory name and derives the archive, checksum, and manifest names from the unchanged base name. The archive is directly readable with standard tools. Vaultkeep does not use a proprietary repository format or incremental archive chain.

### Important boundaries

- V1 supports Debian 12 `bookworm` and Debian 13 `trixie`.
- systemd 247 or newer is required.
- Each job and machine uses a unique destination namespace. Multiple machines do not manage retention in the same destination directory.
- Vaultkeep is not a filesystem snapshot system. Applications requiring point-in-time consistency provide a stable source through application dumps, lifecycle hooks, LVM, ZFS, or another snapshot mechanism.
- Local state is a reconstructable cache. Deleting `state.json` does not delete or invalidate destination backups.

## Installation

### Prerequisites

Installation requires:

- a supported Debian system;
- root access;
- systemd as the active system manager;
- a trusted local Vaultkeep source tree, normally a clone of the GitHub repository checked out at the intended release tag;
- network access to configured Debian package repositories and the Python package index;
- an existing backup destination or mounted share.

The installer installs:

- Python 3 and `python3-venv`;
- GNU TAR;
- Zstandard;
- Debian's maintained `7zip` package;
- `rsync`;
- `util-linux` and mount utilities.

Legacy `p7zip-full` is not used.

### Install the application

Clone the repository and select the release to install:

```bash
git clone https://github.com/dutu/vaultkeep.git /path/to/vaultkeep
cd /path/to/vaultkeep
git checkout <release-tag>
```

Then run the installer from that checkout:

```bash
sudo ./install.sh install --dry-run
sudo ./install.sh install
```

The first command previews dependencies and every planned filesystem and systemd change. The second command applies the installation.

`/path/to/vaultkeep` is only the administrator's source checkout. It is not an installed application directory and can be removed after installation. Vaultkeep copies the selected source into the active versioned release below `/opt/vaultkeep`. A later same-version reinstall can use the same refreshed checkout. A later version update can use the same checkout or another trusted checkout of the newer release.

The installer:

- stages the checkout and virtual environment as one versioned release;
- atomically activates that release through `/opt/vaultkeep/current`;
- creates `/usr/local/bin/vaultkeep`;
- creates configuration, secrets, state, and temporary directories;
- installs inactive `.example` job templates;
- installs and validates the shared systemd service and timer templates;
- validates existing jobs and synchronizes their timers;
- runs `vaultkeep --version`.

Application code, virtual environments, and installer ownership metadata are consolidated below:

```text
/opt/vaultkeep/
├── releases/
│   └── <version>/
│       ├── src/
│       ├── venv/
│       └── deployment.json
├── current -> releases/<version>
└── install-manifest.json
```

Debian keeps the remaining files in standard purpose-specific locations:

| Purpose | Location |
|---|---|
| Command | `/usr/local/bin/vaultkeep` |
| Jobs and secrets | `/etc/vaultkeep` |
| Local state and temporary files | `/var/lib/vaultkeep` |
| Shared systemd templates | `/etc/systemd/system/vaultkeep@.service` and `vaultkeep@.timer` |

The ownership manifest allows the installer to identify its files without scanning the system or guessing from filenames.

Installation does not start a backup. Example files are copied to the root-mode jobs directory with the `.example` extension, so they are not active jobs:

```text
/etc/vaultkeep/jobs/*.example
```

The installer copies every `examples/*.example` file from the release. Updates overwrite existing `.example` files so packaged examples stay current. User-created `.yaml` job files are not overwritten.

After installing Vaultkeep, continue with the workflow for the intended mode:

- [Root mode guide](docs/root-mode.md), for administrator-managed system backups.
- [User mode guide](docs/user-mode.md), for per-user backups using `vaultkeep --user`.

### Reinstall the same application version

During development, or when rebuilding the same release version from a refreshed checkout, run `install` again:

```bash
cd /path/to/vaultkeep
git pull
sudo ./install.sh install --dry-run
sudo ./install.sh install
```

If the installed version matches the checkout version but the source content changed, `install` stages the checkout as a complete replacement for the existing release directory, validates it, and replaces `/opt/vaultkeep/releases/<version>` in a rollback-protected transaction. Existing jobs, secrets, state, timer registry, and backup destinations are preserved.

If the installed version is different from the checkout version, `install` stops. Use `update` for a newer release version.

### Update the application

Fetch and select the newer release in the source checkout:

```bash
cd /path/to/vaultkeep
git fetch --tags --prune
git checkout <new-release-tag>
```

Preview the update from the refreshed checkout:

```bash
sudo ./install.sh update --dry-run
```

Apply it:

```bash
sudo ./install.sh update
```

The update mode:

- requires an existing installation and accepts a newer candidate release or an exact version-and-source match for idempotent verification;
- stages the source and virtual environment as a complete versioned release;
- validates the executable, example configurations, and systemd units;
- atomically switches `/opt/vaultkeep/current` to the new release;
- reloads systemd;
- preserves user jobs and secrets;
- retains the complete preceding release for rollback.

`install.sh update` does not download source code; it installs the checkout from which it is executed. The same version and source digest produces an idempotent verification with no release switch. The same version with different source content is not an update; use `install` to refresh the installed code or bump the project version before running `update`.

Failed staging does not replace the active release. A failure after activation restores the preceding release, templates, and timer registry.

Confirm the installed version:

```bash
vaultkeep --version
```

Validate root-mode jobs and inspect root-mode timer changes:

```bash
sudo vaultkeep timers validate
sudo vaultkeep timers sync --dry-run
```

The installer does not enable or start timers. Apply root-mode timer changes explicitly:

```bash
sudo vaultkeep timers sync
```

### Uninstall the application

Preview the complete uninstall plan:

```bash
sudo /opt/vaultkeep/current/src/install.sh uninstall --dry-run
```

Remove the application, executable link, managed timers, systemd templates, temporary data, and installed application tree:

```bash
sudo /opt/vaultkeep/current/src/install.sh uninstall
```

Normal uninstall preserves job configurations, secrets, and per-job local state for a later reinstall. It also preserves every backup destination.

To remove configuration, secrets, and local state as well:

```bash
sudo /opt/vaultkeep/current/src/install.sh uninstall --purge
```

`--purge` does not remove backup archives or hook executables. Installer-added Debian packages remain installed because other applications can use them.

## Usage

### Choose a mode-specific workflow

Job creation, command privileges, timer installation, secret paths, hook ownership, and state locations differ by mode. Use the mode-specific guide for the first working job:

- [Root mode guide](docs/root-mode.md), for jobs below `/etc/vaultkeep/jobs`, root-owned secrets, and system timers.
- [User mode guide](docs/user-mode.md), for jobs below `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/jobs`, user-owned secrets, and systemd user timers.

The sections below describe the shared job format, shared command surface, and backup behavior. Commands shown with `[--user]` mean:

- omit `--user` for root mode and run privileged operations with `sudo` as shown in the root mode guide;
- include `--user` for user mode and run as the user who owns the job.

### Minimal job configuration

```yaml
config_version: 1

job:
  id: app

sources:
  - path: /path/to/source
    archive_path_mode: prefix
    archive_prefix: source
  - path: /path/to/another-source
    archive_path_mode: prefix
    archive_prefix: another-source
    exclude:
      - cache/
      - "*.tmp"

exclude:
  - "**/.cache/**"

source_options:
  follow_symlinks: false
  cross_filesystems: false
  ignore_missing: false

destination:
  root: /path/to/backups/app
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"

archive:
  format: tar.zst
  compression_level: 6

encryption:
  mode: none

retention:
  hourly: 24
  daily: 7
  weekly: 8
  monthly: 12
  yearly: 3

schedule:
  enabled: false

hooks:
  before_check: null
  before_archive: null
  after_archive: null
  on_success: null
  on_failure: null
  on_unchanged: null

logging:
  level: info
  include_command_output: false
```

### Destination name templates

`destination.name_template` controls the backup base name used for each new backup. The backup base name is the rendered template value before Vaultkeep appends the backup ID. Vaultkeep renders the template once after source change detection and derives the final artifact names from that rendered base name:

```text
directory: <backup-base-name>-<backup_id>
archive:   <backup-base-name>.<archive-format>
checksum:  <backup-base-name>.<archive-format>.sha256
manifest:  <backup-base-name>.json
```

The supported placeholders are exactly:

| Placeholder | Meaning | Formatting |
|---|---|---|
| `{job}` | The configured `job.id`. | No format specifiers. |
| `{hostname}` | The machine hostname returned by the operating system for the run. | No format specifiers. |
| `{timestamp}` | The backup creation timestamp in the machine's local timezone. | Supports Python `strftime` datetime formatting. |
| `{timestamp_utc}` | The same backup creation timestamp converted to UTC. | Supports Python `strftime` datetime formatting. |
| `{source_hash}` | The 64-character lowercase SHA-256 source digest, without the `sha256:` prefix. | May be truncated with precision syntax, for example `{source_hash:.12}`. |
| `{format}` | The selected archive format, either `tar.zst` or `tar.7z`. | No format specifiers. |

At least one of `{timestamp}` or `{timestamp_utc}` is required. Use doubled braces to include a literal brace in the output, for example `{{` or `}}`.

Recommended timestamp form:

```yaml
destination:
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
```

Example with hostname and a shortened source hash:

```yaml
destination:
  name_template: "backup-{job}-{hostname}-{timestamp_utc:%Y%m%dT%H%M%SZ}-{source_hash:.12}"
```

Template output must be a single safe path name below `destination.root`: it cannot be empty, contain `/`, contain `\`, contain `..`, contain null or control characters, or escape the destination root. Template conversions such as `{job!r}`, nested fields, missing runtime parameters, and unsupported format specifiers are rejected.

Do not include `{backup_id}` in `destination.name_template`. Vaultkeep generates a lowercase 32-character backup ID and appends it only to the final directory name. Hook `command` values are not templates and do not expand `{job}`-style placeholders.

### Runtime template parameters

Shared job configurations can use explicit runtime parameters in `destination.root` and `destination.name_template`.

Example:

```yaml
destination:
  root: /path/to/backups/{repo}
  name_template: "backup-{job}-{repo}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
```

Run it with:

```text
vaultkeep [--user] --config <job.yaml> --param repo=nas-a run
```

Any placeholder in `destination.root` must be supplied with `--param`. Any placeholder in `destination.name_template` that is not one of the built-in fields above must also be supplied with `--param`. Extra unused parameters are rejected.

Runtime parameter names must match `[A-Za-z_][A-Za-z0-9_]*` and cannot use built-in names such as `job`, `timestamp_utc`, `source_hash`, or `backup_id`. Runtime parameter values are intended for non-secret selectors such as repository names; they may contain only letters, digits, `.`, `_`, `:`, `@`, `+`, and `-`.

Runtime parameter values are part of the effective backup identity. Different parameter values use different destination namespaces, configuration fingerprints, local state files, locks, and timer instances.

For destinations below a mounted filesystem, set `destination.mount_point` to the actual mount path:

```yaml
destination:
  root: /mnt/backups/app
  mount_point: /mnt/backups
```

When `mount_point` is present, Vaultkeep requires it to be currently mounted before using `destination.root`. The mount point can be `destination.root` itself or one of its parents. Omit `mount_point` for ordinary local destinations.

### CLI reference

`--config` is required for commands that operate on one job. It must appear before the command name.

```text
vaultkeep [-h|--help]
vaultkeep --version

vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] validate [--schema-only]
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] run
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] list
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] verify
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] prune [--dry-run]

vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] timer install
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] timer update
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] timer status
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] timer next
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] timer disable
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] timer remove

vaultkeep [--user] timers list
vaultkeep [--user] timers validate
vaultkeep [--user] timers sync [--dry-run]
```

Every command also supports `-h` or `--help` in its own position, for example `vaultkeep timer --help`.

Use `--user` for per-user job configuration, per-user state, and systemd user timers. Omit it for root mode. Root-mode operations that read protected sources, write root-owned state, manage system timers, or use root-owned secrets must be run with `sudo`.

### Check the installed version

```bash
vaultkeep --version
```

The command prints only the installed version.

### Validate a job configuration

```text
vaultkeep [--user] --config <job.yaml> validate --schema-only
vaultkeep [--user] --config <job.yaml> validate
```

Use schema-only validation for quick YAML structure checks. Use complete validation before running a backup or installing a timer.

### Update a job configuration

After changing a job configuration:

```text
vaultkeep [--user] --config <job.yaml> validate
```

For a schedule change:

```text
vaultkeep [--user] --config <job.yaml> timer update
```

Changes to sources, exclusions, destination identity, archive format, encryption mode, password-file path, or metadata policy force a new backup on the next run.

Retention changes do not force a backup. Preview and apply the new policy explicitly:

```text
vaultkeep [--user] --config <job.yaml> prune --dry-run
vaultkeep [--user] --config <job.yaml> prune
```

### Run a job manually

```text
vaultkeep [--user] --config <job.yaml> run
```

Possible successful results:

- `created`: a new backup was finalized;
- `unchanged`: the current source and backup-relevant configuration match the last successful backup.

An unchanged run does not create an archive or apply retention.

### List job backups

```text
vaultkeep [--user] --config <job.yaml> list
```

`list` reports valid and malformed matching entries without reading complete archive contents or changing state.

### Verify job backups

```text
vaultkeep [--user] --config <job.yaml> verify
```

Verification checks:

- the backup manifest and filenames;
- the SHA-256 checksum;
- the complete Zstandard or 7-Zip stream;
- TAR member paths and structure.

Encrypted verification reads the configured password file.

### Prune job backups

Preview:

```text
vaultkeep [--user] --config <job.yaml> prune --dry-run
```

Apply:

```text
vaultkeep [--user] --config <job.yaml> prune
```

Retention is count-based and calendar-bucketed. Vaultkeep evaluates tiers from coarsest to finest:

```text
yearly → monthly → weekly → daily → hourly
```

Each finer tier is limited by the horizon established by the next enabled coarser tier. A tier value of `0` disables that tier, and at least one tier remains enabled.

Retention runs automatically only after a new backup is finalized. Time passing or an unchanged run does not delete backups. Vaultkeep never automatically deletes unrelated, temporary, or malformed destination entries.

### Configure a job timer

Vaultkeep uses one systemd timer instance per job. Root mode manages system timers. User mode manages systemd user timers.

```text
<job.yaml>
-> vaultkeep@app.timer
-> vaultkeep@app.service
```

Parameterized jobs derive the timer instance from the job ID and sorted runtime parameters:

```text
<job.yaml> + --param repo=nas-a
-> vaultkeep@app--repo-nas-a--<hash>.timer
-> vaultkeep@app--repo-nas-a--<hash>.service
```

The generated service override stores the exact `vaultkeep --config ... --param ... run` command, so manual and scheduled runs use the same effective configuration.

Enable scheduling in the job:

```yaml
schedule:
  enabled: true
  interval: daily
  window: "01:00-05:00"
  persistent: true
```

For manual-only jobs, the required schedule block can be minimal:

```yaml
schedule:
  enabled: false
```

Install and start the timer:

```text
vaultkeep [--user] --config <job.yaml> timer install
```

For a parameterized job, pass the same parameters to every timer command:

```text
vaultkeep [--user] --config <job.yaml> --param repo=nas-a timer install
vaultkeep [--user] --config <job.yaml> --param repo=nas-a timer status
vaultkeep [--user] --config <job.yaml> --param repo=nas-a timer remove
```

Update the timer after changing the job schedule:

```text
vaultkeep [--user] --config <job.yaml> timer update
```

Inspect the timer:

```text
vaultkeep [--user] --config <job.yaml> timer status
vaultkeep [--user] --config <job.yaml> timer next
```

Stop the timer without removing its Vaultkeep-managed systemd schedule file:

```text
vaultkeep [--user] --config <job.yaml> timer disable
```

Remove, or uninstall, the timer:

```text
vaultkeep [--user] --config <job.yaml> timer remove
```

`timer remove` disables and stops the timer, removes the per-job systemd drop-in, reloads the appropriate systemd manager, and unregisters the timer from Vaultkeep's timer ownership registry. It does not delete the job YAML file, local job state, or backup archives.

Supported intervals:

- `hourly`;
- `daily`;
- `weekly`;
- `monthly`.

`window` spreads jobs deterministically across the configured period. The same job on the same machine normally receives the same execution offset, while different jobs and machines receive different offsets.

Use `at` instead of `window` for a fixed local time:

```yaml
schedule:
  enabled: true
  interval: daily
  at: "03:30"
  persistent: true
```

`at` and `window` are mutually exclusive. Persistent timers perform one catch-up activation after downtime.

Weekly schedules add a weekday:

```yaml
schedule:
  enabled: true
  interval: weekly
  day: sunday
  window: "01:00-06:00"
  persistent: true
```

Monthly schedules use a day from 1 through 28:

```yaml
schedule:
  enabled: true
  interval: monthly
  day: 1
  at: "03:30"
  persistent: true
```

Manual and scheduled runs execute the same backup workflow. A per-job lock prevents concurrent execution of the same job.

### Manage all job timers

Commands covering every job:

```text
vaultkeep [--user] timers list
vaultkeep [--user] timers validate
vaultkeep [--user] timers sync --dry-run
vaultkeep [--user] timers sync
```

`timers sync` creates or updates timers for jobs with `schedule.enabled: true` and disables timers for jobs with `schedule.enabled: false`. Use `--dry-run` to preview the actions.

### Password-protected backups

Create a password file without placing the password in command history. The path and ownership rules are mode-specific:

- root mode uses root-owned password files, normally below `/etc/vaultkeep/secrets`;
- user mode uses user-owned password files, normally below `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/secrets`.

The file contains one UTF-8 passphrase line.

Change the archive settings:

```yaml
archive:
  format: tar.7z
  compression_level: 6

encryption:
  mode: password
  password_file: /path/to/app.passphrase
```

The archive filename retains the configured backup base name and changes its derived extension to `.tar.7z`.

Vaultkeep passes the password to `/usr/bin/7z` through a private input pipe. It does not place the password in command arguments, environment variables, manifests, or logs.

To preserve TAR filesystem semantics, Vaultkeep creates a private plaintext TAR below the mode-specific temporary workspace, encrypts it with AES-256 and encrypted headers, verifies the result, and removes the plaintext TAR before committing the backup. Secure physical erasure is not guaranteed on journaling filesystems, copy-on-write filesystems, or SSDs.

V1 uses one password for a job and destination namespace. Password rotation requires a new job ID, password file, and destination namespace. Retain the old password file while old encrypted backups are needed.

### Lifecycle hooks

Hooks run under the job's execution identity: root in root mode, or the calling user in user mode. Hooks are trusted code for that mode. Each hook phase accepts one hook object. The `command` field is one command expressed as an argument vector: the first item is the executable path, and the remaining items are arguments passed to that executable. It is split across YAML lines for readability; it is not several commands and it is not a shell script.

```yaml
hooks:
  before_check:
    command:
      - /usr/local/sbin/prepare-app-backup
      - --job
      - app
    timeout_seconds: 300

  before_archive: null
  after_archive: null
  on_success: null
  on_failure: null
  on_unchanged: null
```

The supported hook fields are:

- `command`: required non-empty list of strings. `command[0]` must be an absolute executable path.
- `timeout_seconds`: optional integer from 1 through 3600. The default is 300 seconds. If the hook does not finish before the timeout, Vaultkeep terminates the hook process group and treats the hook as failed.

Available phases:

- `before_check`: prepare dumps or source material before discovery;
- `before_archive`: quiesce an application after changes are detected;
- `after_archive`: release quiescing or cleanup after source reads;
- `on_success`: notification after backup and retention succeed;
- `on_failure`: notification after a workflow failure;
- `on_unchanged`: notification after an unchanged run.

Hook failures stop the current workflow phase. If `before_check` fails, source discovery does not start. If `before_archive` fails, archive creation does not start; Vaultkeep still attempts `after_archive` so partially applied quiescing can be released, then runs `on_failure`. If archive creation fails after `before_archive` succeeds, Vaultkeep also attempts `after_archive` and then runs `on_failure`.

Hook executables and their paths must satisfy the ownership and writability rules for the selected mode. Shell strings, pipelines, inherited environments, secret arguments, and multiple commands per phase are not supported. Use a securely managed wrapper executable for multi-step actions.

### Restore an unencrypted backup

Restore into an empty staging directory:

```bash
mkdir -p /restore/staging
zstd --decompress --stdout \
  /path/to/backups/app/backup-app-20260723T090000Z-550e8400e29b41d4a716446655440000/backup-app-20260723T090000Z.tar.zst \
  | tar --extract --file=- --directory=/restore/staging
```

Use `sudo` when restoring root-owned backups or extracting to a privileged location. Inspect the restored content before copying it to the final location.

### Restore an encrypted backup

Extract the inner TAR; 7-Zip prompts for the password:

```bash
mkdir -p /restore/staging /restore/work
cd /restore/work
7z x /path/to/backups/app/backup-app-20260723T090000Z-550e8400e29b41d4a716446655440000/backup-app-20260723T090000Z.tar.7z
tar --extract --file=app.tar --directory=/restore/staging
```

Vaultkeep does not provide a general restore command in v1. Standard `tar`, `zstd`, and `7z` tools remain sufficient.

### Logs and troubleshooting

Validate the job first:

```text
vaultkeep [--user] --config <job.yaml> validate
```

Inspect timer and service status through Vaultkeep:

```text
vaultkeep [--user] --config <job.yaml> timer status
```

Inspect systemd directly when needed:

```bash
# root mode
systemctl status vaultkeep@app.timer
systemctl status vaultkeep@app.service
journalctl -u vaultkeep@app.service

# user mode
systemctl --user status vaultkeep@app.timer
systemctl --user status vaultkeep@app.service
journalctl --user -u vaultkeep@app.service
```

Common exit codes:

| Code | Meaning |
|---:|---|
| 0 | Success, including unchanged |
| 2 | Invalid command-line arguments |
| 3 | Invalid configuration |
| 4 | Source error |
| 5 | Destination or mount error |
| 7 | Archive creation failure |
| 8 | Verification or source-consistency failure |
| 9 | Retention failure |
| 10 | Job lock already held |
| 11 | Hook execution failure |
| 14 | Timer management failure |
| 15 | State or manifest failure |

Local job state is stored in the mode-specific state directory:

```text
root mode: /var/lib/vaultkeep/jobs
user mode: ${XDG_STATE_HOME:-~/.local/state}/vaultkeep/jobs
```

If a job's `state.json` is missing or unusable, Vaultkeep reconstructs it automatically from valid destination manifests. Do not edit destination manifests to recreate local state.

## Design and implementation reference

This guide intentionally omits internal module boundaries, typed models, hashing encodings, atomic-commit mechanics, manifest validation algorithms, systemd rendering details, security rationale, and implementation milestones.

The authoritative specification is [architecture_and_design.md](architecture_and_design.md).
