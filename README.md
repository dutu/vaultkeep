# Vaultkeep

Vaultkeep is a backup application for Debian systems. It creates independent archive files from one or more files and directories, skips unchanged sources, applies calendar-based retention, and runs either manually or through managed systemd timers.

This file is the user guide. For configuration rules, data structures, security decisions, workflow details, and implementation requirements, see [architecture_and_design.md](architecture_and_design.md).

## Table of contents

- [Overview](#overview)
  - [What Vaultkeep does](#what-vaultkeep-does)
  - [How a backup works](#how-a-backup-works)
  - [Important boundaries](#important-boundaries)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [Install the application](#install-the-application)
  - [Update the application](#update-the-application)
  - [Uninstall the application](#uninstall-the-application)
- [Usage](#usage)
  - [Create a job configuration](#create-a-job-configuration)
  - [Minimal job configuration](#minimal-job-configuration)
  - [Destination name templates](#destination-name-templates)
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

Vaultkeep runs as root because jobs can read system files, access protected secrets, inspect mounts, and execute administrator-configured hooks.

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

`/path/to/vaultkeep` is only the administrator's source checkout. It is not an installed application directory and can be removed after installation. Vaultkeep copies the selected source into the active versioned release below `/opt/vaultkeep`. A later update can use the same refreshed checkout or another trusted checkout of the newer release.

The installer:

- stages the checkout and virtual environment as one versioned release;
- atomically activates that release through `/opt/vaultkeep/current`;
- creates `/usr/local/bin/vaultkeep`;
- creates configuration, secrets, state, and temporary directories;
- installs an inactive example job;
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

Installation does not start a backup. The example remains disabled at:

```text
/etc/vaultkeep/jobs/example.yaml.disabled
```

After installing Vaultkeep, continue with [Create a job configuration](#create-a-job-configuration).

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
- validates the executable, example configuration, and systemd units;
- atomically switches `/opt/vaultkeep/current` to the new release;
- reloads systemd;
- synchronizes existing timers;
- preserves user jobs and secrets;
- retains the complete preceding release for rollback.

`install.sh update` does not download source code; it installs the checkout from which it is executed. The same version and source digest produces an idempotent verification with no release switch. Reusing a version with different source content is rejected.

Failed staging does not replace the active release. A failure after activation restores the preceding release, templates, timer registry, generated timer files, and enabled states.

Confirm the installed version:

```bash
vaultkeep --version
```

Validate all jobs and inspect timer changes:

```bash
sudo vaultkeep timers validate
sudo vaultkeep timers sync --dry-run
```

The installer already performs timer synchronization. Run the following manually only when applying later configuration changes:

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

### Create a job configuration

Copy the disabled example and give it the intended job ID:

```bash
sudo cp \
  /etc/vaultkeep/jobs/example.yaml.disabled \
  /etc/vaultkeep/jobs/app.yaml
```

Edit it:

```bash
sudo nano /etc/vaultkeep/jobs/app.yaml
```

The filename stem and `job.id` must match. For this example:

```yaml
job:
  id: app
```

Create the destination before validation. For mounted destinations, mount the share through the operating system and configure `require_mount: true`.

To run the job manually, see [Run a job manually](#run-a-job-manually).

To run the job on a systemd schedule, see [Configure a job timer](#configure-a-job-timer).

### Minimal job configuration

```yaml
config_version: 1

job:
  id: app

sources:
  - path: /etc/myapp
  - path: /var/lib/myapp
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
  root: /mnt/backups/app
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
  require_mount: true

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
  interval: daily
  window: "01:00-05:00"
  persistent: true

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

`destination.name_template` controls the backup base name used for each new backup. Vaultkeep renders the template once after source change detection and derives the final artifact names from that rendered base name:

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

Template output must be a single safe path name below `destination.root`: it cannot be empty, contain `/`, contain `\`, contain `..`, contain null or control characters, or escape the destination root. Template conversions such as `{job!r}`, nested fields, unknown placeholders, and unsupported format specifiers are rejected.

Do not include `{backup_id}` in `destination.name_template`. Vaultkeep generates a lowercase 32-character backup ID and appends it only to the final directory name. Hook `command` values are not templates and do not expand `{job}`-style placeholders.

### CLI reference

`--config` is required for commands that operate on one job. It must appear before the command name.

```text
vaultkeep [-h|--help]
vaultkeep --version

vaultkeep --config <job.yaml> validate [--schema-only]
vaultkeep --config <job.yaml> run
vaultkeep --config <job.yaml> list
vaultkeep --config <job.yaml> verify
vaultkeep --config <job.yaml> prune [--dry-run]

vaultkeep --config <job.yaml> timer install
vaultkeep --config <job.yaml> timer update
vaultkeep --config <job.yaml> timer status
vaultkeep --config <job.yaml> timer next
vaultkeep --config <job.yaml> timer disable
vaultkeep --config <job.yaml> timer remove

vaultkeep timers list
vaultkeep timers validate
vaultkeep timers sync [--dry-run]
```

Every command also supports `-h` or `--help` in its own position, for example `vaultkeep timer --help`.

### Check the installed version

```bash
vaultkeep --version
```

The command prints only the installed version.

### Validate a job configuration

```bash
vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate --schema-only
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate
```

Use schema-only validation for quick YAML structure checks. Use complete validation before running a backup or installing a timer.

### Update a job configuration

After changing a job configuration:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate
```

For a schedule change:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer update
```

Changes to sources, exclusions, destination identity, archive format, encryption mode, password-file path, or metadata policy force a new backup on the next run.

Retention changes do not force a backup. Preview and apply the new policy explicitly:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml prune --dry-run
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml prune
```

### Run a job manually

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml run
```

Possible successful results:

- `created`: a new backup was finalized;
- `unchanged`: the current source and backup-relevant configuration match the last successful backup.

An unchanged run does not create an archive or apply retention.

### List job backups

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml list
```

`list` reports valid and malformed matching entries without reading complete archive contents or changing state.

### Verify job backups

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml verify
```

Verification checks:

- the backup manifest and filenames;
- the SHA-256 checksum;
- the complete Zstandard or 7-Zip stream;
- TAR member paths and structure.

Encrypted verification reads the configured password file.

### Prune job backups

Preview:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml prune --dry-run
```

Apply:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml prune
```

Retention is count-based and calendar-bucketed. Vaultkeep evaluates tiers from coarsest to finest:

```text
yearly → monthly → weekly → daily → hourly
```

Each finer tier is limited by the horizon established by the next enabled coarser tier. A tier value of `0` disables that tier, and at least one tier remains enabled.

Retention runs automatically only after a new backup is finalized. Time passing or an unchanged run does not delete backups. Vaultkeep never automatically deletes unrelated, temporary, or malformed destination entries.

### Configure a job timer

Vaultkeep uses one systemd timer instance per job:

```text
/etc/vaultkeep/jobs/app.yaml
→ vaultkeep@app.timer
→ vaultkeep@app.service
```

Enable scheduling in the job:

```yaml
schedule:
  enabled: true
  interval: daily
  window: "01:00-05:00"
  persistent: true
```

Install and start the timer:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer install
```

Update the timer after changing the job schedule:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer update
```

Inspect the timer:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer status
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer next
```

Stop the timer without removing its Vaultkeep-managed systemd schedule file:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer disable
```

Remove, or uninstall, the timer:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer remove
```

`timer remove` disables and stops the timer, removes the per-job systemd drop-in, reloads systemd, and unregisters the timer from Vaultkeep's timer ownership registry. It does not delete the job YAML file, local job state, or backup archives.

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

```bash
sudo vaultkeep timers list
sudo vaultkeep timers validate
sudo vaultkeep timers sync --dry-run
sudo vaultkeep timers sync
```

`timers sync` creates or updates timers for jobs with `schedule.enabled: true` and disables timers for jobs with `schedule.enabled: false`. Use `--dry-run` to preview the actions.

### Password-protected backups

Create a root-readable password file without placing the password in command history:

```bash
sudo nano /etc/vaultkeep/secrets/app.passphrase
```

The installer creates `/etc/vaultkeep/secrets` as a root-only directory. The file contains one UTF-8 passphrase line.

Change the archive settings:

```yaml
archive:
  format: tar.7z
  compression_level: 6

encryption:
  mode: password
  password_file: /etc/vaultkeep/secrets/app.passphrase
```

The archive filename retains the configured backup base name and changes its derived extension to `.tar.7z`.

Vaultkeep passes the password to `/usr/bin/7z` through a private input pipe. It does not place the password in command arguments, environment variables, manifests, or logs.

To preserve TAR filesystem semantics, Vaultkeep creates a private plaintext TAR below `/var/lib/vaultkeep/tmp`, encrypts it with AES-256 and encrypted headers, verifies the result, and removes the plaintext TAR before committing the backup. Secure physical erasure is not guaranteed on journaling filesystems, copy-on-write filesystems, or SSDs.

V1 uses one password for a job and destination namespace. Password rotation requires a new job ID, password file, and destination namespace. Retain the old password file while old encrypted backups are needed.

### Lifecycle hooks

Hooks run as root and are trusted administrator code. Each hook phase accepts one hook object. The `command` field is one command expressed as an argument vector: the first item is the executable path, and the remaining items are arguments passed to that executable. It is split across YAML lines for readability; it is not several commands and it is not a shell script.

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

Hook executables and their paths must be root-owned and not writable by group or other users. Shell strings, pipelines, inherited environments, secret arguments, and multiple commands per phase are not supported. Use a securely managed wrapper executable for multi-step actions.

### Restore an unencrypted backup

Restore into an empty staging directory:

```bash
sudo mkdir -p /restore/staging
sudo zstd --decompress --stdout \
  /mnt/backups/app/backup-app-20260723T090000Z-550e8400e29b41d4a716446655440000/backup-app-20260723T090000Z.tar.zst \
  | sudo tar --extract --file=- --directory=/restore/staging
```

Inspect the restored content before copying it to the final location.

### Restore an encrypted backup

Extract the inner TAR; 7-Zip prompts for the password:

```bash
sudo mkdir -p /restore/staging /restore/work
cd /restore/work
sudo 7z x /mnt/backups/app/backup-app-20260723T090000Z-550e8400e29b41d4a716446655440000/backup-app-20260723T090000Z.tar.7z
sudo tar --extract --file=app.tar --directory=/restore/staging
```

Vaultkeep does not provide a general restore command in v1. Standard `tar`, `zstd`, and `7z` tools remain sufficient.

### Logs and troubleshooting

Validate the job first:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate
```

Inspect timer and service status:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml timer status
sudo systemctl status vaultkeep@app.timer
sudo systemctl status vaultkeep@app.service
```

Read scheduled-run logs:

```bash
sudo journalctl -u vaultkeep@app.service
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

Local job state is stored below:

```text
/var/lib/vaultkeep/jobs
```

If a job's `state.json` is missing or unusable, Vaultkeep reconstructs it automatically from valid destination manifests. Do not edit destination manifests to recreate local state.

## Design and implementation reference

This guide intentionally omits internal module boundaries, typed models, hashing encodings, atomic-commit mechanics, manifest validation algorithms, systemd rendering details, security rationale, and implementation milestones.

The authoritative specification is [architecture_and_design.md](architecture_and_design.md).
