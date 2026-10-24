# Vaultkeep

Vaultkeep is a backup application for Debian 13 systems. It creates independent archive files from one or more files and directories, skips unchanged sources, applies calendar-based retention, and runs manually, through per-job systemd timers, or after a supported app shuts down.

This is the operational user guide for manual, shutdown-triggered, and scheduled backups.
Root and user setup details are also available in the [root mode guide](docs/root-mode.md)
and [user mode guide](docs/user-mode.md).

## Table of contents

- [Overview](#overview)
  - [What Vaultkeep does](#what-vaultkeep-does)
  - [How a backup works](#how-a-backup-works)
  - [Operating modes](#operating-modes)
  - [Important boundaries](#important-boundaries)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [Install the application](#install-the-application)
- [Usage](#usage)
  - [Choose a mode-specific workflow](#choose-a-mode-specific-workflow)
  - [Minimal job configuration](#minimal-job-configuration)
  - [Back up directory files separately](#back-up-directory-files-separately)
  - [Destination name templates](#destination-name-templates)
  - [Runtime template parameters](#runtime-template-parameters)
  - [Destination safety checks](#destination-safety-checks)
  - [Desktop notifications](#desktop-notifications)
  - [CLI reference](#cli-reference)
  - [Terminal output](#terminal-output)
  - [Check job status](#check-job-status)
  - [Check the installed version](#check-the-installed-version)
  - [Validate a job configuration](#validate-a-job-configuration)
  - [Start backups in the chosen mode](#start-backups-in-the-chosen-mode)
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
- [Optional Trigger](#optional-trigger)
- [Maintenance and removal](#maintenance-and-removal)
  - [Update the application](#update-the-application)
  - [Uninstall the application](#uninstall-the-application)
- [Design and implementation reference](#design-and-implementation-reference)

## Overview

### What Vaultkeep does

Vaultkeep creates file-based backups and applies retention. You define backup jobs as
YAML files. Each job can run manually, on a calendar schedule, or after a supported
application shuts down.

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
- an optional application shutdown trigger;
- optional lifecycle hooks.

For each run, Vaultkeep validates the job, checks whether the selected sources changed, creates an independent backup directory when needed, verifies the archive, writes a manifest, and applies the configured retention policy. By default, all selected sources share one archive. The optional [per-file mode](#back-up-directory-files-separately) creates a separate archive and history for each file in a directory. If nothing relevant changed, the run finishes as `unchanged` and does not create a new archive.

Backups are ordinary archive files:

- unencrypted `.tar.zst` archives;
- password-protected `.tar.7z` archives containing one inner TAR.

Each backup is self-contained and includes the archive, a SHA-256 checksum, and a JSON manifest. Retention can keep hourly, daily, weekly, monthly, and yearly backups. Local state is only a cache; Vaultkeep can reconstruct it from destination manifests if the local state file is missing or unusable.

Root-mode jobs run as root because they can read system files, access protected secrets, inspect mounts, and execute administrator-configured hooks. User-mode jobs run as the calling user and can only access files, destinations, secrets, and hooks available to that user.

### Operating modes

Choose how each job starts its backups:

| Operation | Configuration | Start or manage it with |
| --- | --- | --- |
| Manual | `schedule.enabled: false`, `trigger.enabled: false` | `vaultkeep --config JOB.yaml run` |
| Trigger | `trigger.enabled: true`, `schedule.enabled: false` | `vaultkeep --user --config JOB.yaml trigger <action>` |
| Schedule | `schedule.enabled: true`, `trigger.enabled: false` | `vaultkeep --config JOB.yaml schedule <action>` |

Trigger and Schedule are mutually exclusive for one job. Manual `run` remains
available for every job, including jobs with automation enabled. The same per-job
lock applies to all three ways of starting a backup.

Execution scope is a separate choice: use `--user` to run as the job's owning user;
omit it and use `sudo` for root/system jobs. Manual and Schedule support both scopes.
Trigger supports only a non-root application owner using `--user`.

Root and user scope use the same centrally installed `vaultkeep` command:

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

The two modes do not share job configuration, timer units, timer registries, state files, or locks. A machine can run root-mode scheduled jobs alongside one or more users’ shutdown-triggered jobs. Install Vaultkeep once as root, then configure each job in its owning mode.

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
backup-app-20260723T090000Z/
├── backup-app-20260723T090000Z.tar.zst
├── backup-app-20260723T090000Z.tar.zst.sha256
└── backup-app-20260723T090000Z.json
```

`destination.name_template` produces the shared base name. Vaultkeep uses this name for the directory and derives the archive, checksum, and manifest names from it. If the directory name is already occupied, Vaultkeep appends `-2`, `-3`, and so on only to the directory name. The archive is directly readable with standard tools. Vaultkeep does not use a proprietary repository format or incremental archive chain.

### Important boundaries

- Vaultkeep targets Debian 13 `trixie`, Python 3.13+, on amd64 and arm64.
- Upgrade Debian 12 to Debian 13 before updating an existing 0.1.0 installation.
  On Debian 12, keep using 0.1.0 and its installed uninstall script; the current
  installer is unsupported there, including with a manually installed Python.
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

- Debian Python 3.13 and `python3-venv`;
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

The installer shows its plan, live stages, and final result. Add `--no-color` to
install, update, or uninstall commands to disable colors; see
[terminal output](#terminal-output) for details.

The first command previews dependencies and deployment changes. The second applies
installation. Vaultkeep is one package with built-in scheduling and optional
shutdown triggering, including its `psutil` process-inspection dependency.
A fresh installation starts no backups or monitors, enables no jobs, and configures
no lingering. Replacing an existing installation automatically pauses, refreshes,
and resumes its existing launchers; see [maintenance](#pause-and-resume-for-maintenance).

Verify the installed command:

```bash
vaultkeep --version
```

`/path/to/vaultkeep` is only the administrator's source checkout. It is not an installed application directory and can be removed after installation. Vaultkeep copies the selected source into the active versioned release below `/opt/vaultkeep`. A later same-version reinstall can use the same refreshed checkout. A later version update can use the same checkout or another trusted checkout of the newer release.

The installer:

- stages the checkout and virtual environment as one versioned release;
- atomically activates that release through `/opt/vaultkeep/current`;
- creates `/usr/local/bin/vaultkeep`;
- creates configuration, secrets, state, and temporary directories;
- installs inactive `.example` job templates;
- installs and validates the shared systemd service and timer templates;
- leaves existing jobs and schedules for explicit operator validation and resumption;
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

By default, Debian keeps the remaining files in standard purpose-specific locations:

| Purpose | Location |
|---|---|
| Command | `/usr/local/bin/vaultkeep` |
| Jobs and secrets | `/etc/vaultkeep` |
| Local state and temporary files | `/var/lib/vaultkeep` |
| Core system templates | `/etc/systemd/system/vaultkeep@.service` and `vaultkeep@.timer` |
| Trigger worker template | `/etc/systemd/user/vaultkeep-trigger-exec@.service` |

The ownership manifest allows the installer to identify its files without scanning the system or guessing from filenames.

To use a custom command or root configuration directory, pass absolute, normalized paths to the installer:

```sh
sudo env VAULTKEEP_BIN_LINK=/usr/local/bin/vaultkeep-custom \
  VAULTKEEP_CONFIG_ROOT=/etc/vaultkeep-custom ./install.sh install
```

`VAULTKEEP_INSTALL_PREFIX` and `VAULTKEEP_VAR_ROOT` similarly override the application directory and root state/temporary directory. Repeat the same overrides for `update`, reinstallation, and `uninstall`; changing them does not migrate an existing installation. Generated services and installed commands retain the chosen layout without requiring these variables in user sessions or systemd environments. Use the chosen command and configuration paths in the procedures below.

Systemd does not allow quotes or backslashes in executable paths; the installer rejects those command paths. Keep shared unit directories at their defaults unless systemd is separately configured to search the chosen locations.

Installation does not start a backup. Example files are copied to the root-mode jobs directory with the `.example` extension, so they are not active jobs:

```text
/etc/vaultkeep/jobs/*.example
```

The installer copies every `examples/*.example` file from the release. Updates overwrite existing `.example` files so packaged examples stay current. User-created `.yaml` job files are not overwritten.

After installing Vaultkeep, continue with the workflow for the intended mode:

- [Root mode guide](docs/root-mode.md), for administrator-managed system backups.
- [User mode guide](docs/user-mode.md), for per-user backups using `vaultkeep --user`.

## Usage

### Choose a mode-specific workflow

Choose the account that will own and run the job. User jobs normally live under
`${XDG_CONFIG_HOME:-~/.config}/vaultkeep/jobs`; root jobs live under `/etc/vaultkeep/jobs`.
The filename stem must match `job.id` in the YAML.

For a user job, copy the installed manual/schedule example without overwriting an
existing job:

```bash
jobs_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs"
install -d -m 700 "$jobs_dir"
job_config="$jobs_dir/app.yaml"
if [ ! -e "$job_config" ]; then
    install -m 600 /opt/vaultkeep/current/src/examples/vaultkeep-schedule.yaml.example "$job_config"
fi
nano "$job_config"
```

For a root job, copy and edit the root-owned template:

```bash
sudo cp --no-clobber /etc/vaultkeep/jobs/vaultkeep-schedule.yaml.example /etc/vaultkeep/jobs/app.yaml
sudo nano /etc/vaultkeep/jobs/app.yaml
```

Set `job.id: app`, replace the source and destination paths, and configure any
secrets needed for encryption. The destination directory must already exist; mount
network destinations before validation. Start with both automation settings disabled
unless you are configuring a Schedule or Trigger job using the procedures below.
For Trigger, use an [application-specific example](#configure-and-validate-trigger)
with the correct process matcher and data directory.

Detailed scope-specific instructions are available in:

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

### Back up directory files separately

To back up Electrum wallets independently, set these fields in a complete job:

```yaml
backup_mode: per_file

sources:
  - path: /home/alice/.electrum/wallets
    archive_path_mode: prefix
    archive_prefix: wallets
    exclude:
      - "*.lock"
      - "*.tmp"

destination:
  root: /mnt/backups/electrum
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
```

1. Set the source and destination to your actual paths. The destination root must
   already exist. Configure the optional [destination safety checks](#destination-safety-checks)
   as needed. Keep the job's archive, encryption, and retention settings.
2. Validate and run the job as its owning user:

   ```sh
   vaultkeep --user --config ~/.config/vaultkeep/jobs/electrum-main.yaml validate
   vaultkeep --user --config ~/.config/vaultkeep/jobs/electrum-main.yaml run
   ```

3. Check the per-file results on stderr, then verify the destination:

   ```sh
   vaultkeep --user --config ~/.config/vaultkeep/jobs/electrum-main.yaml list
   vaultkeep --user --config ~/.config/vaultkeep/jobs/electrum-main.yaml verify
   ```

`per_file` requires exactly one source directory and
`source_options.follow_symlinks: false`. It selects immediate regular files,
applies global and per-source exclusions, and skips subdirectories and symlinks.
New files are included automatically. Unchanged files create no archive; changes
to permissions, ownership, or backup-relevant configuration can also require a
backup. An empty selection finishes as `unchanged` without creating an archive.

Each file has its own destination folder, local state, and retention history. A
folder is named `file-<readable-name>-<sha256-of-original-filename>` so names remain
distinct on case-insensitive shares. Inside it, backups use the usual timestamped
directories and three artifacts. Each archive contains just `wallets/<filename>`
with the configuration above. Existing `tar.zst` and `tar.7z` formats apply; ZIP is
not supported.

Deleting or excluding a source file leaves its history intact. Renaming a file
starts a new history. `list`, `verify`, and `prune` include histories whose sources
are gone; retention is calculated separately for each file. Automatic retention
runs only for files that receive a new backup. A failure for one file does not
stop the remaining files; the command reports each result and exits with code 7
if any file failed. Successful files are checked normally on the next run.

Lifecycle hooks run separately for each selected file, including `before_check`
after the directory's file list has been selected. An empty selection runs no
hooks. Use a timer or the existing shutdown trigger for the complete job; see
[Electrum setup](docs/trigger/apps/electrum.md#separate-wallet-backups).
Omit `backup_mode`, or set it to `combined`, for the default shared archive.

When migrating an existing combined job, use a separate destination root for
per-file backups. Existing combined backups and the wrapper's ZIP archives are
not imported; retain them separately. The first run creates a new baseline for
every selected wallet.

### Destination name templates

`destination.name_template` controls the backup base name used for each new backup. The backup base name is the rendered template value. Vaultkeep renders the template once after source change detection and derives the final artifact names from that rendered base name:

```text
directory: <backup-base-name>
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

If a path with the rendered directory name already exists, Vaultkeep uses the first available numbered name: `<backup-base-name>-2`, `<backup-base-name>-3`, and so on. The archive, checksum, and manifest filenames keep the original base name. Existing files, directories, and symbolic links are never overwritten. If a selected path becomes occupied before finalization, the run fails safely; a later run selects an available name. Existing backups with backup-ID directory suffixes remain supported by listing, verification, state reconstruction, and retention.

Do not include `{backup_id}` in `destination.name_template`. Vaultkeep keeps its randomly generated backup ID in the manifest and local state. Hook `command` values are not templates and do not expand `{job}`-style placeholders.

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

### Destination safety checks

`destination.root` must be an existing directory. Two optional safety checks can
be configured independently: use either, both, or neither.

- `marker_file` confirms that a designated marker exists as a regular file below
  `destination.root`. Use a relative path without `..` components. Vaultkeep does
  not check its contents or create it automatically. A missing or inaccessible
  marker blocks destination writes.
- `expected_fstype` verifies the actual filesystem backing `destination.root` as
  seen by Vaultkeep, including inside a container. Use an exact kernel filesystem
  type such as `cifs`, `nfs`, `nfs4`, or `ext4`.

For a CIFS destination, especially one mounted on an LXC host and bind-mounted into
the container, both checks can be useful:

```yaml
destination:
  root: /mnt/backups/app
  name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
  expected_fstype: cifs
  marker_file: .vaultkeep-destination
```

If an LXC bind mount exposes a host local directory instead of the CIFS share,
the filesystem type differs and Vaultkeep stops before writing to or
managing backups on that destination. A type check does not identify a particular
server or share.

1. Inspect the destination from the environment that runs Vaultkeep
   (inside LXC when applicable):

   ```sh
   findmnt --uniq --target /mnt/backups/app --output TARGET,SOURCE,FSTYPE
   ```

   Confirm the expected server/share and `FSTYPE` of `cifs`. A local filesystem
   such as `ext4` or `zfs` indicates that the intended share is unavailable at that path.

2. After confirming the real share, create the configured marker once:

   ```sh
   touch /mnt/backups/app/.vaultkeep-destination
   ```

   Keep this marker absent from the underlying host local directory. Vaultkeep
   checks that the marker is a file below `destination.root`; it does not check
   its contents or create it automatically.

3. Run full validation with your job configuration:

   ```sh
   vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate
   ```

Filesystem type checks use `findmnt` from the required `util-linux` package and
recognize bind mounts in the running process's mount namespace. Failed, unavailable,
or ambiguous filesystem inspection and filesystem type mismatches stop the operation
with destination error exit code 5. Schema-only validation skips these runtime checks.
Configured safety checks precede the writable-destination probe, which tests writing,
renaming, and removal during full validation and before backup writes. In `per_file`
mode, the parent destination is checked before creating each child directory; child
directories do not need their own marker, and retain the filesystem type check.
A filesystem type check alone does not establish that a NAS is reachable.
These checks confirm conditions at the time of inspection; they do not prevent
mount changes or network failures during an operation.

### Desktop notifications

Backup notifications apply to manual, scheduled, and triggered executions. Trigger
notifications cover application monitoring, dispatch, and recovery. Each category
is optional and defaults to disabled.

1. Install the desktop notification command if needed:

   ```bash
   sudo apt-get install libnotify-bin
   ```

   From the backup owner's graphical session, test delivery:

   ```bash
   notify-send "Vaultkeep" "Desktop notifications are available"
   ```

   Expect a desktop notification unless desktop settings suppress it. The desktop
   must run as the backup owner in the same host or container. Session discovery
   uses `busctl`, supplied by systemd, and also supports NoMachine sessions in LXC.

2. Add this top-level section to the job YAML, enabling the categories you want:

   ```yaml
   notifications:
     command: notify-send
     timeout_ms: 5000
     backup:
       enabled: true
     trigger:
       enabled: true
   ```

   `backup.enabled` works with manual-only jobs and scheduled jobs as well as Trigger.
   `trigger.enabled` here controls Trigger alerts; enabling application monitoring
   still requires the job's separate `trigger.enabled` setting and an installed monitor.
   The delivery command and timeout apply to both notification categories.

3. Validate the edited job using your chosen mode. For a user-mode job:

   ```bash
   vaultkeep --user --config "$job_config" validate
   ```

   Future backups read these settings automatically. If this job has an installed
   Trigger monitor, also apply the change to it:

   ```bash
   vaultkeep --user --config "$job_config" trigger update
   ```

Backup alerts begin after the job lock is acquired. They update the execution phase,
refresh long phases with elapsed time every ten seconds, and end with the actual
result, local completion time, and a concise failure reason when applicable. Per-file
jobs share one notification lifecycle for the whole job. With `notify-send`, the final
result replaces progress and requests that it remain until dismissed. Trigger sends
separate monitoring and dispatch alerts, without repeating backup execution results.

Delivery is best-effort; unavailable desktops and notification failures do not affect
backups. Visibility and expiration depend on desktop settings. See the
[notification reference](docs/notifications.md) for session selection, custom commands,
bounded final delivery, and troubleshooting.

### CLI reference

Put global options before the command name: `--user`, `--config`, and any repeated
`--param KEY=VALUE` selections, plus `--no-color` when needed. `--config` is required
for operations on one job.
Action-specific options, such as `status --json`, follow the action.

```text
vaultkeep [-h|--help]
vaultkeep --version

vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] validate [--schema-only]
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] run
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] status [--json] [--verbose]
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] list
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] verify
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] prune [--dry-run]

vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] schedule install
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] schedule update
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] schedule next
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] schedule disable
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] schedule remove

vaultkeep --user --config <job.yaml> [--param KEY=VALUE ...] trigger install
vaultkeep --user --config <job.yaml> [--param KEY=VALUE ...] trigger update
vaultkeep --user --config <job.yaml> [--param KEY=VALUE ...] trigger disable
vaultkeep --user --config <job.yaml> [--param KEY=VALUE ...] trigger remove
vaultkeep --user --config <job.yaml> [--param KEY=VALUE ...] trigger monitor

vaultkeep [--user] schedules list
vaultkeep [--user] schedules validate
vaultkeep [--user] schedules sync [--dry-run]
```

Every command and action supports `-h` or `--help` in its own position, for example
`vaultkeep schedule --help`, `vaultkeep schedule install --help`, or
`vaultkeep status --help`. Version inspection uses `vaultkeep --version`.

Use `--user` for per-user job configuration, per-user state, and systemd user timers. Omit it for root mode. Root-mode operations that read protected sources, write root-owned state, manage system timers, or use root-owned secrets must be run with `sudo`.

#### Terminal output

Human-readable reports use short sections and aligned fields. Validation puts the
overall result first and separates configuration checks, backup history, and enabled
automation. For example:

```text
Vaultkeep — Validation

Job:     electrum-main
Mode:    user
Result:  SETUP INCOMPLETE

Configuration
  Configuration  VALID
  Backup paths   OK

Backup
  History  no completed run recorded

Trigger monitoring
  Trigger                        ENABLED
  Trigger runtime prerequisites  OK
  Monitor service                NOT INSTALLED
  Monitor updates                no recorded state

Issue: Monitor service is not installed; automatic backups are unavailable.
Next: vaultkeep --user --config /path/to/job.yaml trigger install
```

Reports remain visible at every `logging.level`. Set the job's `logging.level` to
`warning` or `error` for concise reports, `info` for additional operational diagnostics,
or `debug` for internal checks. Diagnostic messages go to stderr with their severity
(`DEBUG`, `INFO`, `WARNING`, `ERROR`); report fields have no logging prefixes. Full
systemd unit names and detailed prerequisite information appear in diagnostics.
Actionable failures and warnings remain visible even at `logging.level: error`.
`trigger monitor` continues to log its ongoing operational events at the configured
level. These settings do not change command results or exit codes.

Hook execution summaries are logged at `INFO`. To also log captured hook stdout and
stderr, set `logging.include_command_output: true` and `logging.level` to `info` or
`debug`. Output is limited to 1 MiB per stream and decoded as UTF-8, replacing invalid
bytes. Hook output is omitted by default; enable it only when it contains no secrets.

Colors highlight status values: green for success, yellow for warnings or incomplete
setup, red for failure, and cyan for running stages. Headings, paths, timestamps,
and ordinary values use the default terminal color. Redirected or captured output
and `TERM=dumb` use plain text. Disable colors with `--no-color`, or set a nonempty
`NO_COLOR` environment variable:

```bash
sudo ./install.sh install --no-color
sudo ./install.sh update --no-color
vaultkeep --no-color --user --config /path/to/job.yaml validate
vaultkeep --no-color --user --config /path/to/job.yaml trigger install
```

The installer shows each stage as `RUNNING...` immediately, with elapsed seconds,
then `OK` or `FAILED`. Capable interactive terminals refresh the current line;
redirected output keeps ordinary stage lines, including activity updates every ten
seconds. Completed stages stay in order. `--no-color` disables colors while retaining
interactive line updates. Routine apt/pip output goes into the complete diagnostic
transcript, whose path is printed at the start and end. A failed installation names
the stage, shows recent diagnostic context, and retains the transcript after rollback.
Command failures include the full command and available diagnostic output.
The transcript is a private file in the temporary directory (normally
`/tmp/vaultkeep-install.*.log`); inspect it before temporary-file cleanup or reboot,
and remove it when no longer needed. `--dry-run` continues to show detailed planned
commands without creating a transcript or changing the deployment.

Validation reports remain on stdout, with detailed errors on stderr. Version output,
Job status JSON, schedule listings, and backup result markers such as
`run: created` remain suitable for scripts. Use `status --json` for backup activity
and results, with separate Trigger and Schedule information when configured.

### Check job status

1. Inspect the selected job using the same mode, configuration path, and runtime
   parameters used to run it:

   ```bash
   vaultkeep --user --config /path/to/job.yaml status
   # For a root-mode job:
   sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml status
   ```

   Backup activity is always shown. A running backup reports its phase, phase elapsed
   time, and total elapsed time. `Previous backup` reports the previous execution's
   result, completion time, and duration independently of current activity.
   `SUCCESS` means an archive was created; `UNCHANGED` means no new archive was needed.
   Failures and interruptions include a concise reason and a diagnostic log when
   available. `UNKNOWN` means the evidence cannot establish activity or completion.
   A reliably free job lock means `IDLE`, including before the first execution;
   uncertainty about a previous outcome does not make current activity unknown.

2. Check the configured automation section: Trigger shows monitor freshness,
   application detection, pending requests, dispatch blocks, and safety warnings.
   Schedule shows timer installation, health, enablement, and the next execution.
   Manual-only jobs omit these sections.
   Historical Trigger safety warnings show a review count; use `status --verbose`
   for affected attempt IDs, periods, reasons, and diagnostic paths.

3. For scripts, request the same report as JSON:

   ```bash
   vaultkeep --user --config /path/to/job.yaml status --json
   ```

Status only reads local evidence and systemd information. It does not require the
sources or destination to be online and never starts, stops, repairs, or reconciles
anything. Missing or damaged evidence remains explicit. Exit 0 means the report was
readable, including a previous failed backup; inspection errors use exit 15 and
configuration errors use exit 3. Inspect the JSON result fields to decide job health.
See the [status reference](docs/status.md) for fields, uncertainty, and diagnostics.

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

Use complete validation before running a backup or installing automation, and later
as a health check. It validates the whole job and checks the runtime prerequisites,
installation, and health of its enabled Trigger or Schedule in the selected user or
system mode. Trigger requires user mode and a non-root application owner.

The report shows `Trigger` or `Schedule` as `ENABLED`, or both as `DISABLED`. It includes
backup source/destination checks and any recorded last-run failure. For Trigger it
checks the monitor service, recent monitor updates, the loaded configuration, process
observations, and recovery warnings. For Schedule it checks the timer, next scheduled
run, and backup service result. Missing backup history alone is not a failure.

Before the job's automation is installed, expect `NOT INSTALLED` and
`Result: SETUP INCOMPLETE`; the other available checks still run. Correct failed
prerequisites, then use `trigger install` or `schedule install` explicitly. Re-run
validation after installation. A missing, disabled, stopped, or unhealthy monitor/timer
is reported; validation never installs, starts, restarts, or repairs services or state.
A health report describes the state observed during the check, not future execution.
Complete validation exits zero only when all requested checks pass. Missing installation
and automation health failures exit nonzero (normally 3); backup destination/source and
state errors retain their specific [exit codes](#logs-and-troubleshooting). After starting
a new monitor, allow it to record its first observation before expecting a healthy result.

A `Configuration` value of `VALID` means the configuration passed its checks. Overall `OK` means
all applicable backup and automation checks passed. `SETUP INCOMPLETE` identifies
missing automation installation; other reported failures still need attention.
`FAILED` means a required validation or health check failed. Missing monitor history
is shown as context when the monitor has not been installed, rather than as a second
independent error. These outcomes preserve the existing exit codes.

Use `--schema-only` for YAML structure checks on a machine without the job's source,
destination, or systemd environment. Its output explicitly says runtime checks were
skipped; its `VALID` result applies only to configuration structure and does not
establish readiness or health.

Complete validation creates a temporary directory and file in the destination, checks
the same atomic directory rename used to finalize backups, then removes the probe.
Backup runs and destructive pruning perform this check before creating archives or
deleting backups. Failures identify the operation and path; if cleanup is denied,
remove the reported probe after correcting destination permissions. Schema-only
validation does not access the destination.

Run validation as the account that runs the backup, using `--user` for user-mode jobs.
Successful validation confirms access at that moment; permissions and mount conditions
can change afterward.

### Start backups in the chosen mode

These procedures use a user-owned job. Set `job_config` to its actual YAML path:

```bash
job_config="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs/app.yaml"
```

For root Manual or Schedule jobs, use `/etc/vaultkeep/jobs/app.yaml`, omit `--user`,
and prefix privileged commands with `sudo`. Trigger requires a non-root user.
User automation requires a reachable systemd user manager; Trigger also requires
an account-owned `XDG_RUNTIME_DIR`. Validation reports missing runtime prerequisites.

#### Manual

1. Keep `schedule.enabled: false` and `trigger.enabled: false` (omitting `trigger`
   also leaves it disabled).
2. Validate, run the backup, and verify its archives:

   ```bash
   vaultkeep --user --config "$job_config" validate
   vaultkeep --user --config "$job_config" run
   vaultkeep --user --config "$job_config" verify
   ```

   Validation should report both automation settings disabled and `Result: OK`.
   A successful run reports `run: created` or `run: unchanged`. No service installation
   is needed for manual operation.

#### Schedule

1. Set `trigger.enabled: false` and replace the existing `schedule` block, for example:

   ```yaml
   schedule:
     enabled: true
     interval: daily
     at: "03:30"
     persistent: true
   ```

2. Validate the job, then install and inspect its timer:

   ```bash
   vaultkeep --user --config "$job_config" validate
   vaultkeep --user --config "$job_config" schedule install
   vaultkeep --user --config "$job_config" status
   vaultkeep --user --config "$job_config" schedule next
   vaultkeep --user --config "$job_config" validate
   ```

   Before installation, expect `NOT INSTALLED` for `Timer` and
   `Result: SETUP INCOMPLETE`. Fix any other failed checks before installing. After setup, expect
   an enabled, active timer, a next run time, and `Result: OK`.
   [Schedule options](#configure-a-job-timer) include hourly, weekly, monthly, and
   randomized time windows.

#### Trigger

1. Set `schedule.enabled: false`. Configure the supported application, data directory,
   and process matcher as described in [Configure and validate Trigger](#configure-and-validate-trigger),
   then set `trigger.enabled: true`.
2. Validate, install, and inspect its shutdown monitor:

   ```bash
   vaultkeep --user --config "$job_config" validate
   vaultkeep --user --config "$job_config" trigger install
   vaultkeep --user --config "$job_config" status
   vaultkeep --user --config "$job_config" validate
   ```

   Before installation, expect `NOT INSTALLED` for `Monitor service` and
   `Result: SETUP INCOMPLETE`. Fix any other failed checks before installing. Allow the monitor
   to record its first observation, then expect `Result: OK`. It must observe
   the application running before a later confirmed shutdown can request a backup.
   For interactive foreground monitoring, use `trigger monitor` instead of installing
   a service. Run only one monitor for the selected job.

Trigger and Schedule share the lifecycle actions:

| Action | Purpose |
| --- | --- |
| `install` | Create and enable this job's automation. |
| `update` | Refresh generated units after editing the configuration. |
| `disable` | Stop and disable future automatic starts. |
| `remove` | Disable and remove the job's generated automation units. |

Use the same global `--config` and `--param` selections for every action. Schedule's
`update` preserves timer enablement; Trigger's `update` enables and restarts the monitor.
Disabling or removing automation preserves configuration, backup archives, and job
state, and does not terminate a backup already running. Changing YAML alone does
not stop installed automation. Before switching between Schedule and Trigger, disable
the previous mechanism and let its backup work finish.

### Update a job configuration

After changing a job configuration:

```text
vaultkeep [--user] --config <job.yaml> validate
```

For a schedule change:

```text
vaultkeep [--user] --config <job.yaml> schedule update
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

When stderr is a terminal, `run` displays the current stage and elapsed time,
including source hashing, compression, verification, and retention. Long stages
repeat every 10 seconds with a `still running` message. Elapsed time is for the
whole run; these updates indicate the active stage, not a completion percentage.
Terminal updates are omitted when stderr is redirected or captured. Every run,
including scheduled execution, still publishes progress for `vaultkeep status`. The final result remains on stdout as `run: created` or `run: unchanged`.
After `run: created`, the terminal also shows the archive path, archive size,
total run duration, and number of backups removed by retention. This summary
uses stderr and is omitted when stderr is redirected or captured.

Possible successful results:

- `created`: a new backup was finalized;
- `unchanged`: the current source and backup-relevant configuration match the last successful backup.

An unchanged run does not create an archive or apply retention.

Temporary backups use `~$.partial-vaultkeep-<job>-<backup-id>` directory names.
On destinations managed by synchronization software, verify that temporary
directories are excluded and completed backups are uploaded.

If a run fails before the final directory rename, Vaultkeep removes that attempt's
`~$.partial-vaultkeep-*` directory. If cleanup also fails, a warning identifies the
remaining path and the original backup error is still reported. Committed backups
are preserved if a later operation fails. An abrupt termination, such as a power
loss or forced kill, can leave partial directories for manual cleanup once no
backup is using them; later runs do not remove older partial directories.

### List job backups

```text
vaultkeep [--user] --config <job.yaml> list
```

`list` checks backup directory layouts, manifests, and checksum sidecars without
reading archive contents or changing state. Use `verify` to detect archive content
corruption.

When stderr is a terminal, `list` displays its current stage with elapsed time.
Long stages repeat every 10 seconds with a `still running` message. Progress is
omitted when stderr is redirected or captured. The final result remains on stdout
as `list: listed`.

### Verify job backups

```text
vaultkeep [--user] --config <job.yaml> verify
```

Verification checks:

- the backup manifest and filenames;
- the backup directory layout;
- the SHA-256 checksum against the sidecar and manifest;
- the complete Zstandard stream and TAR structure for `.tar.zst`;
- the configured password, complete 7-Zip archive, encrypted headers, and single
  inner `{job}.tar` member for `.tar.7z`;
- TAR member paths, rejecting duplicate, absolute, and parent-traversal paths.

Verification works without the original sources and never changes backups, local
state, or retention. Encrypted jobs require a password file containing the archive's
passphrase, with the same ownership and permissions required by `run`.

Each verified archive is reported on stderr. Verification continues after individual
backup failures and reports their paths and reasons; any failure produces a nonzero
exit status.

When stderr is a terminal, `verify` displays configuration validation, destination
checks, and backup verification stages with elapsed time. Long stages repeat every
10 seconds with a `still running` message, as with `run`. Progress is omitted when
stderr is redirected or captured. The final result remains on stdout as
`verify: verified`.

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

Retention runs automatically only after a new backup is finalized. Time passing or an unchanged run does not delete backups. Retention never deletes unrelated, temporary, or malformed destination entries.

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
vaultkeep [--user] --config <job.yaml> schedule install
```

For a parameterized job, pass the same parameters to every timer command:

```text
vaultkeep [--user] --config <job.yaml> --param repo=nas-a schedule install
vaultkeep [--user] --config <job.yaml> --param repo=nas-a status
vaultkeep [--user] --config <job.yaml> --param repo=nas-a schedule remove
```

Update the timer after changing the job schedule:

```text
vaultkeep [--user] --config <job.yaml> schedule update
```

Install and update validate the generated calendar and staged service/timer units
before replacing the managed files. Update preserves timer enablement and restarts
an enabled timer. If applying either command fails or is interrupted with Ctrl-C,
Vaultkeep restores the preceding managed files, ownership registry, and timer
enablement/activity. Restoration errors are reported as an incomplete rollback.

Inspect the timer:

```text
vaultkeep [--user] --config <job.yaml> status
vaultkeep [--user] --config <job.yaml> schedule next
```

Stop the timer without removing its Vaultkeep-managed systemd schedule file:

```text
vaultkeep [--user] --config <job.yaml> schedule disable
```

Remove, or uninstall, the timer:

```text
vaultkeep [--user] --config <job.yaml> schedule remove
```

`schedule remove` disables and stops the timer, removes the per-job systemd drop-in, reloads the appropriate systemd manager, and unregisters the timer from Vaultkeep's timer ownership registry. It does not delete the job YAML file, local job state, or backup archives.

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

Manual, scheduled, and shutdown-triggered runs execute the same backup workflow. A per-job lock prevents concurrent execution of the same job.

### Manage all job timers

Commands covering every job:

```text
vaultkeep [--user] schedules list
vaultkeep [--user] schedules validate
vaultkeep [--user] schedules sync --dry-run
vaultkeep [--user] schedules sync
```

`schedules list` shows each selected job instance's configured `enabled` setting,
managed-file `installed` state, systemd `active` state, `next` trigger (`n/a` when
unavailable), and `drift`. Drift reports missing or changed generated files,
timer enablement that differs from the job setting, or a timer still active for
a disabled schedule. Disabled jobs retain their timer files, so their previous
calendar is not compared. Operator-owned overrides are outside this comparison.
Listing is read-only and fails if systemd cannot be inspected.

`schedules sync` creates or updates timers for jobs with `schedule.enabled: true`
and disables timers for jobs with `schedule.enabled: false`. It also stops,
disables, and removes registered timers whose job YAML has been deleted, clearing
their persistent timestamps. Unrelated unit files and overrides are retained.
Removal attempts timestamp cleanup even if the generated timer file is already missing;
cleanup errors fail sync and trigger rollback.
Use `--dry-run` to preview the actions without applying changes.

Bulk operations use all jobs in the selected scope's jobs directory and take no
`--config` or `--param` selection. `schedules validate` checks job configuration and
schedule calendars; use `vaultkeep --config JOB.yaml validate` (with `--user` for
user jobs) for complete runtime and health checks of one job.

For parameterized jobs, bulk commands reuse the parameters saved by each installed
timer and report each instance separately. `schedules sync` updates or disables
those selections. Add a new selection explicitly:

```text
vaultkeep [--user] --config <job.yaml> --param repo=nas-a schedule install
```

Templates with no installed parameter selection produce a `skip` message, and
the remaining jobs continue. Skipped templates are not fully validated; validate
one with its intended `--param` values before installing it. Sync checks all
selected configurations and saved parameter selections before changing timers.
Invalid configurations or unreadable saved selections stop sync during this check.
In root mode, a timer without a service override uses `<instance>.yaml`, as defined
by the shared service template. Its name is not used to infer saved parameters.
If applying the plan fails or is interrupted with Ctrl-C, sync restores the previous
generated files and ownership registry, reloads systemd, and restores timer
enablement and activity before reporting the failure. Any errors during restoration
are reported as an incomplete rollback.
Persistent timestamps already cleared during sync are not restored.

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

Vaultkeep passes the password to `/usr/bin/7z` through a private terminal with echo disabled, after the password prompt appears. It does not place the password in command arguments, environment variables, manifests, or logs.

To preserve TAR filesystem semantics, Vaultkeep creates a private plaintext TAR below the mode-specific temporary workspace, encrypts it with AES-256 and encrypted headers, verifies the result, and removes the plaintext TAR before committing the backup. Secure physical erasure is not guaranteed on journaling filesystems, copy-on-write filesystems, or SSDs.

V1 uses one password for a job and destination namespace. Password rotation requires a new job ID, password file, and destination namespace. Retain the old password file while old encrypted backups are needed.

When the password file changes or local backup state must be reconstructed, Vaultkeep tests the newest valid encrypted backup with the current password before deciding whether to create a backup. If the test fails, the run stops before archive creation or automatic retention. Replacing the password file with the same passphrase is supported.

### Lifecycle hooks

Hooks run under the job's execution identity: root in root mode, or the calling user in user mode. Each hook phase accepts one hook object. Its `command` is a shell command string executed with `/bin/sh -c`.

```yaml
hooks:
  before_check:
    command: '/usr/local/sbin/prepare-app-backup --job "{job}"'
    timeout_seconds: 300

  before_archive: null
  after_archive: null
  on_success:
    command: 'telegram-notify --level info "Backup completed for {job} on {hostname}"'
    timeout_seconds: 60
  on_failure:
    command: 'telegram-notify --level error "Backup failed for {job} on {hostname}: $VAULTKEEP_ERROR"'
    timeout_seconds: 60
  on_unchanged: null
```

The supported hook fields are:

- `command`: required non-blank shell command string. YAML lists are not accepted. Shell built-ins such as `cd`, `export`, and `printf`, variable expansion, command substitution, pipelines, redirection, and command sequences are supported. Use POSIX shell syntax; Bash-specific syntax requires explicitly invoking Bash.
- `timeout_seconds`: optional integer from 1 through 3600. The default is 300 seconds. If the hook does not finish before the timeout, Vaultkeep terminates the hook process group and treats the hook as failed.

Hook commands support the same built-in values as destination names:

| Placeholder | Value |
| --- | --- |
| `{job}` | Job ID. |
| `{hostname}` | Hostname used for this run. |
| `{timestamp}` | Time in the local timezone; supports formats such as `{timestamp:%Y-%m-%d %H:%M:%S%z}`. |
| `{timestamp_utc}` | The same time in UTC; for example `{timestamp_utc:%Y%m%dT%H%M%SZ}`. |
| `{source_hash}` | Source SHA-256 without the `sha256:` prefix; `{source_hash:.12}` takes the first 12 characters. Empty until hashing completes. |
| `{format}` | Configured archive format: `tar.zst` or `tar.7z`. |

Placeholders expand in unquoted text and shell double quotes. Use double quotes around messages to keep each message as one argument. Shell single quotes keep placeholders literal: `printf '%s' '{job}'` prints `{job}`. The single quotes around the complete YAML `command` value in the example are YAML quoting; they do not prevent shell expansion.

Existing `${VAR}` expressions and shell braces are preserved. Only recognized placeholders are expanded; other brace text stays literal. Placeholder conversions and nested format fields are not supported. Quoted here-document delimiters disable expansion in their bodies, following shell behavior.

For a created backup, timestamps match the backup filename and manifest. Before that timestamp is assigned, hooks use the time the hook lifecycle started; an unchanged run uses its check time. A failure hook receives the source hash if hashing completed before the failure.

Available phases:

- `before_check`: prepare dumps or source material before discovery;
- `before_archive`: quiesce an application after changes are detected;
- `after_archive`: release quiescing or cleanup after source reads;
- `on_success`: notification after backup and retention succeed;
- `on_failure`: notification after a workflow failure;
- `on_unchanged`: notification after an unchanged run.

Hook failures stop the current workflow phase. If `before_check` fails, source discovery does not start. If `before_archive` fails, archive creation does not start; Vaultkeep still attempts `after_archive` so partially applied quiescing can be released, then runs `on_failure`. If archive creation fails after `before_archive` succeeds, Vaultkeep also attempts `after_archive` and then runs `on_failure`.

The notification example requires `telegram-notify` to be installed and configured. Other hooks can use any installed command. Commands without an absolute path are found using the fixed `PATH` of `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`.

Hooks start in `/` with no standard input. They receive `LANG=C.UTF-8` and Vaultkeep's context variables, including `VAULTKEEP_JOB`, `VAULTKEEP_RESULT`, `VAULTKEEP_STAGE`, `VAULTKEEP_FAILED_STAGE`, and `VAULTKEEP_ERROR`. Other environment variables and shell startup files are not inherited. Quote variable expansions as in the example above.

The hook's result is the shell's exit status. Use `&&` to stop a sequence when an earlier command fails; a pipeline normally reports the last command's status. Each phase starts a separate shell, so `cd` and `export` affect only that hook.

Hook configuration is trusted executable code. Vaultkeep validates the ownership and writability of the shell and its path; commands and scripts invoked by the shell must be managed securely by the job owner. Missing commands and shell syntax errors are reported when the hook runs. Keep passwords and tokens out of command strings and arguments because they can appear in the process list.

Set variables inside a hook when they are needed. For example, `$HOSTNAME` is not supplied by Vaultkeep; assign it before using it alongside the provided `$VAULTKEEP_*` variables:

```yaml
on_failure:
  command: >-
    HOSTNAME=$(hostname) &&
    export HOSTNAME &&
    telegram-notify --level error
    "Backup failed for $VAULTKEEP_JOB on $HOSTNAME: $VAULTKEEP_ERROR"
  timeout_seconds: 60
```

The YAML `>-` joins equally indented lines with spaces, so no trailing backslashes are needed. Keep all command lines at the same indentation. Double quotes allow variable expansion and keep the message as one argument. `export HOSTNAME` makes the variable available to child processes, including scripts called by the hook. Variables set in one hook are not available in another hook.

Call your own scripts with an explicit interpreter. These examples pass the job ID as the script's first argument (`$1`):

```yaml
on_success:
  command: '/bin/sh /usr/local/libexec/vaultkeep/notify-backup.sh "{job}"'
  timeout_seconds: 60

on_failure:
  command: '/bin/bash /usr/local/libexec/vaultkeep/notify-backup.bash "{job}"'
  timeout_seconds: 60
```

Replace these paths with your scripts. Use `/bin/sh` for a POSIX shell script and `/bin/bash` for a script using Bash features. With an explicit interpreter, the script must be readable by the job's execution user; executable permission on the script is not required. Scripts also receive the `$VAULTKEEP_*` environment variables. Vaultkeep expands placeholders in `command`; inside the script, use `$1` or `$VAULTKEEP_JOB` to access the job ID.

For longer commands, YAML `|` preserves newlines, allowing several shell commands in one hook. A trailing `\` continues the same command on the next line; it must be the last character on its line, with no spaces after it. This example uses a pipeline and command substitution to include the first global-scope IPv4 address reported by `ip`. It requires `ip` from `iproute2` and `awk`:

```yaml
on_failure:
  command: |
    HOSTNAME=$(hostname) || HOSTNAME=unknown
    IP=$(ip -4 -o addr show scope global 2>/dev/null |
      awk '{split($4, address, "/"); print address[1]; exit}')
    IP=${IP:-unavailable}
    telegram-notify --level error \
      "Backup failed for {job} on $HOSTNAME (IPv4: $IP): $VAULTKEEP_ERROR"
  timeout_seconds: 60
```

This reports a local interface address. If several interfaces have addresses, it uses the first one listed. If the lookup returns no address, the notification uses `unavailable`. The shell's `${IP:-unavailable}` expansion and the single-quoted `awk` program are preserved alongside Vaultkeep's `{job}` placeholder. Choose one command per phase when adapting these examples.

### Restore an unencrypted backup

Restore into an empty staging directory:

```bash
mkdir -p /restore/staging
zstd --decompress --stdout \
  /path/to/backups/app/backup-app-20260723T090000Z/backup-app-20260723T090000Z.tar.zst \
  | tar --extract --file=- --directory=/restore/staging
```

Use `sudo` when restoring root-owned backups or extracting to a privileged location. Inspect the restored content before copying it to the final location.

### Restore an encrypted backup

Extract the inner TAR; 7-Zip prompts for the password:

```bash
mkdir -p /restore/staging /restore/work
cd /restore/work
7z x /path/to/backups/app/backup-app-20260723T090000Z/backup-app-20260723T090000Z.tar.7z
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
vaultkeep [--user] --config <job.yaml> status
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
| 3 | Invalid configuration or validation health/setup failure |
| 5 | Destination, mount, finalization, or retention error |
| 7 | Source, archive, verification, or generic execution failure |
| 10 | Job lock already held |
| 11 | Hook execution failure |
| 14 | Timer management failure |
| 15 | Local state or status-inspection error |
| 130 | Interrupted by SIGINT |
| 143 | Interrupted by SIGTERM |

Local job state is stored in the mode-specific state directory:

```text
root mode: /var/lib/vaultkeep/jobs
user mode: ${XDG_STATE_HOME:-~/.local/state}/vaultkeep/jobs
```

If a job's `state.json` is missing or unusable, Vaultkeep reconstructs it automatically from valid destination manifests. Do not edit destination manifests to recreate local state.

## Optional Trigger

Trigger supports **user mode only**. Run it as the application owner with `--user`,
without `sudo`. Root execution is rejected. Root-mode jobs support manual backups
and system timers; the administrator installs the shared software for both modes.

Trigger is included in Vaultkeep and runs only when explicitly enabled for a job.
It observes Electrum, Bisq 1, or Wasabi and requests a user-mode backup after a
confirmed shutdown. Starting a monitor with the app already stopped requests nothing:
the monitor must first observe it running. Every job has its own monitor service,
lock, and recovery state. Other backup jobs need no Trigger configuration.

A job cannot enable both `schedule.enabled` and `trigger.enabled`. Before switching
mechanisms, disable the existing timer or monitor; editing YAML alone does not stop
an installed service. Manual backups remain available. Check process identity, data
directory, related writers, and destination independence before enabling a trigger;
see [process checks](docs/trigger/process-detection.md) and
[consistency limits](docs/trigger/consistency.md).

### Configure and validate Trigger

Run as the application and backup owner. Monitoring requires an active systemd user
manager and a private, account-owned `XDG_RUNTIME_DIR`; a desktop session is optional.

1. Choose the complete YAML example for your application:

   | Application | Example | Configuration guide |
   | --- | --- | --- |
   | Electrum | [`electrum-trigger.yaml.example`](examples/electrum-trigger.yaml.example) | [Electrum](docs/trigger/apps/electrum.md) |
   | Bisq | [`bisq-trigger.yaml.example`](examples/bisq-trigger.yaml.example) | [Bisq 1](docs/trigger/apps/bisq.md) |
   | Wasabi | [`wasabi-trigger.yaml.example`](examples/wasabi-trigger.yaml.example) | [Wasabi](docs/trigger/apps/wasabi.md) |
   | Scheduled/manual backup | [`vaultkeep-schedule.yaml.example`](examples/vaultkeep-schedule.yaml.example) | [Scheduling](docs/trigger/configuration.md) |

   For example, copy the Electrum job without overwriting an existing file:

   ```bash
   jobs_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs"
   install -d -m 700 "$jobs_dir"
   job_config="$jobs_dir/electrum-main.yaml"
   if [ ! -e "$job_config" ]; then
       install -m 600 /opt/vaultkeep/current/src/examples/electrum-trigger.yaml.example "$job_config"
   fi
   ```

2. Edit this single YAML file: set the backup sources/destination and the
   `trigger.app`, `trigger.data_dir`, and `trigger.matcher` fields. Keep
   `schedule.enabled: false`. The examples leave triggers disabled until their
   deployment checks pass. For Bisq, every GUI or API-daemon launch for the
   monitored instance must include the same explicit `--appDataDir` argument. See
   the application guide and [configuration](docs/trigger/configuration.md).

3. After reviewing the process identity and backup scope, set `trigger.enabled: true`
   and validate:

   ```bash
   vaultkeep --user --config "$job_config" validate
   ```

   For shared job templates, put the selected string parameters in `trigger.params`;
   validation uses these defaults and accepts `--param KEY=VALUE` overrides. Supply
   matching parameters to manual runs. Before installation, expect
   `NOT INSTALLED` for `Monitor service` and `Result: SETUP INCOMPLETE`. Fix any prerequisite
   failures before continuing to installation. Runtime checks inspect the user manager,
   worker template, permissions, and actual job availability. They do not certify
   application consistency or matcher coverage.

### Start Trigger explicitly

Install and activate this job's user monitor only after validation:

```bash
vaultkeep --user --config "$job_config" trigger install
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" status
```

`install` creates and enables a service named
`vaultkeep-trigger@<job-id>-<identity>.service`. `status --verbose` prints its exact
name and state path in diagnostics. Repeat these steps for each job; monitors operate independently.
Installing Vaultkeep itself starts no monitor, enables no service, and does not
configure lingering. Enable optional [desktop notifications](#desktop-notifications)
separately. See [service lifetime](docs/trigger/service-operation.md#lifecycle-limits).

### Trigger operation and health

```bash
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" status
systemctl --user list-units --all 'vaultkeep-trigger@*.service' 'vaultkeep-trigger-exec@*.service'
```

The [job status report](#check-job-status) separates Backup activity and the previous
execution result from Trigger monitoring and dispatch. Read `Monitor` with its update
age and `Application` with its observation age. `RUNNING` confirms that the monitor
actually detected the application. `CONFIRMING STOP`, `HAS STOPPED`, and
`NOT YET DETECTED` describe the confirmed lifecycle; stale or unavailable evidence is
`UNKNOWN`. A stopped application at monitor startup is not a confirmed shutdown.

Pending requests, blocked dispatches, and unresolved source-consistency warnings
appear under Trigger. A dispatch blocked by the application's final recheck is not
a failed backup. Receipts and launch records remain available for recovery; status
never reconciles them. See [status meanings](docs/status.md) and
[recovery](docs/trigger/recovery.md).

Run `status --verbose` for monitor unit names, state paths,
observations, and bounded backup diagnostic tails. Use the displayed `Log` path for
full retained failure details. Review logs before sharing them.

Use the monitor service name from debug diagnostics to inspect logs:

```bash
journalctl --user -u 'vaultkeep-trigger@<job-id>-<identity>.service' -n 100 --no-pager
```

After editing the job, validate again and restart its monitor explicitly. Until
updated, validation can report that the monitor is using the previous configuration:

```bash
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" trigger update
vaultkeep --user --config "$job_config" validate
```

Stop and disable only this monitor, or remove its generated unit:

```bash
vaultkeep --user --config "$job_config" trigger disable
vaultkeep --user --config "$job_config" trigger remove
```

These operations preserve recovery state and leave already dispatched workers
running. Drain workers before switching to scheduled execution. For a temporary
maintenance pause, use `systemctl --user stop <monitor-unit>` to preserve enablement.

`status` is read-only. Unknown outcomes or review warnings require
[evidence review](docs/trigger/recovery.md). A readable report does not establish
that starting an application is safe; another process can initiate a backup after
inspection. Use `status --json` for programmatic checks; its `backup`, `trigger`, and
`schedule` sections use the same interpretation as the terminal report.

## Maintenance and removal

### Pause and resume for maintenance

`install.sh install` and `install.sh update` handle maintenance automatically:

1. Inventory existing root and user Vaultkeep timers and Trigger monitors, recording
   their active states and enablement, including runtime enablement.
2. Stop active timers and monitors. Running backup services and workers are never
   stopped. If backups, workers, queued work, held job locks, or inconclusive activity
   remain, refuse the operation and resume the recorded launchers.
3. Hold the deployment maintenance lock, install or repair the release, and refresh
   existing managed job units using their original YAML paths and parameters.
   Refreshing units does not enable or start them.
4. Reload systemd, validate all discovered jobs and runtime readiness for jobs being
   resumed, then resume only previously active timers and monitors. Previously
   inactive schedules remain inactive. Enablement and persistent timer timestamps
   are preserved; a persistent calendar timer may catch up a missed event.

This includes running and lingering user managers and jobs in the standard unit
locations of accounts without a running manager. Stopped user managers remain
stopped. For custom XDG locations, keep the owning manager running with its XDG
roots exported during maintenance. Unreachable managers, local unit overrides,
changed job identities, and standalone Trigger installations require resolution
before automatic maintenance can proceed. YAML jobs, secrets, recovery evidence,
wallet applications, and backup archives are preserved. New jobs remain opt-in.

Normal Vaultkeep backup, verification, pruning, and service-management commands
refuse to start while the maintenance lock is held. Source checkouts, older
versions, custom launchers, and direct Python API calls may not participate in that
lock. During the first upgrade from an older release, keep manual/custom launchers
and new user sessions paused; process and job-lock checks remain necessary.

Ordinary failures and handled signals restore deployment artifacts and generated
job units, then resume the original active launchers if restoration succeeds.
Incomplete restoration leaves schedules paused and retains recovery snapshots.
A resume failure after successful deployment keeps the installed release and
reports the private resume journal path. Resolve the reported service problem,
then retry resumption from the same checkout:

```bash
sudo python3 scripts/install_maintenance.py resume '/opt/vaultkeep/.maintenance.<id>/journal.json'
```

After successful resumption, remove only that reported maintenance directory. A
retained journal blocks another install/update until recovery is reviewed. Never
resume from an incomplete rollback before resolving its restoration errors.

For a deliberate manual maintenance pause, inventory actual unit names and stop
only the relevant timers and monitors in their owning managers:

```bash
sudo systemctl list-units --all 'vaultkeep@*.timer' 'vaultkeep@*.service' 'user@*.service'
systemctl --user list-units --all 'vaultkeep@*.timer' 'vaultkeep@*.service' 'vaultkeep-trigger*'
sudo systemctl stop vaultkeep@app.timer
systemctl --user stop 'vaultkeep-trigger@<job-id>-<identity>.service'
```

Record active states first and resume those units explicitly with `systemctl start`
after maintenance. Do not use `schedule disable` for a temporary pause: it changes
enablement and clears persistent timer timestamps. Let backups and workers finish.
For removal, follow the additional [uninstall steps](#uninstall-the-application).

### Refresh existing monitor commands

Commands use `schedule` and `schedules` in place of `timer` and `timers`. Update any
saved shell commands or scripts. Put `--user`, `--config`, and `--param` before
`trigger` or `schedule`; version inspection uses `vaultkeep --version`.

Install and update automatically refresh existing managed per-job monitor commands,
including monitors installed with the previous option order. Disabled and inactive
monitors stay disabled or inactive.

After editing a job outside an application update, refresh it explicitly as the
application owner, using its original YAML path and parameters:

```bash
vaultkeep --user --config "$job_config" trigger update
vaultkeep --user --config "$job_config" validate
```

`trigger update` enables and starts that monitor. Run it only when monitoring is
wanted. Existing scheduled backup units continue to invoke `run`; their systemd
unit names remain `vaultkeep@…timer` and `vaultkeep@…service`.

### Reinstall the same application version

When rebuilding the same release version from a refreshed checkout, run `install` again. The script [pauses and resumes existing schedules automatically](#pause-and-resume-for-maintenance):

```bash
cd /path/to/vaultkeep
git pull --ff-only
sudo ./install.sh install --dry-run
sudo ./install.sh install
```

If the installed version matches the checkout version but the source content changed, `install` stages the checkout as a complete replacement for the existing release directory, validates it, and replaces `/opt/vaultkeep/releases/<version>` in a rollback-protected transaction. Existing jobs, secrets, state, timer registry, and backup destinations are preserved.

If the installed version is different from the checkout version, `install` stops. Use `update` for a newer release version.

### Update the application

Use Debian 13 for application updates.

Fetch and select the newer release in the source checkout. The installer [pauses and resumes existing schedules automatically](#pause-and-resume-for-maintenance) and refuses active backup work:

```bash
cd /path/to/vaultkeep
git fetch --tags --prune
git pull
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
- refreshes existing managed schedule and monitor units and reloads systemd;
- validates jobs and resumes only previously active timers/monitors;
- preserves user jobs, secrets, enablement, and persistent timer timestamps;
- retains the complete preceding release for rollback.

`install.sh update` does not download source code; it installs the checkout from which it is executed. The same version and source digest produces an idempotent verification with no release switch. The same version with different source content is not an update; use `install` to refresh the installed code or bump the project version before running `update`.

All installations include Trigger support; `--with-trigger` is no longer an installer
option. Monitoring remains opt-in for each YAML job. Upgrades replace the standalone
Trigger command with `vaultkeep --user trigger` and preserve user files and recovery evidence.

An exact version/source match checks packages, locked versions, commands,
and managed artifacts. A damaged application environment is rebuilt; missing owned
links/templates are reconciled. Modified units, unrelated links, masks, and conflicting
overrides require operator resolution. These checks do not prove arbitrary Python
file integrity.

Failed staging leaves the active release intact. Ordinary failures and handled INT,
TERM, or HUP signals restore managed deployment artifacts and generated job units,
then resume original active schedules if restoration succeeds. Failed restoration
leaves schedules paused for recovery. Apt changes are outside rollback. After power loss or SIGKILL,
inspect `current`, `install-manifest.json`, retained releases, and `.staging` or
`.transaction.*` or `.maintenance.*` directories before retrying. Preserve recovery snapshots if rollback
reports a failure; never erase Trigger evidence to force an update.

Confirm the installed version:

```bash
vaultkeep --version
```

Validate root-mode jobs and inspect root-mode timer changes:

```bash
sudo vaultkeep schedules validate
sudo vaultkeep schedules sync --dry-run
```

Existing registered timers are refreshed automatically. To add or remove schedules
after configuration changes, review the sync preview above, then apply it explicitly:

```bash
sudo vaultkeep schedules sync
```

### Uninstall the application

1. In every affected account, disable user core timers and the Trigger monitor,
   including accounts not currently logged in. Stop root timers too. Record the
   actual unit names with `list-unit-files`; disable each relevant timer explicitly.

   ```bash
   systemctl --user disable --now 'vaultkeep-trigger@<job-id>-<identity>.service'
   systemctl --user list-unit-files 'vaultkeep@*.timer'
   sudo systemctl list-unit-files 'vaultkeep@*.timer'
   systemctl --user list-units --all 'vaultkeep-trigger-exec@*.service'
   systemctl --user list-jobs --no-pager
   ```

   Run the Trigger command only where it is installed. Drain backups, workers, and
   queued jobs; preserve unresolved evidence for [review](docs/trigger/recovery.md).
   The installer refuses active work and enabled schedules in running managers. It
   does not remove user-owned enablement links, masks, overrides, or local units.

2. Preview the complete uninstall plan:

```bash
sudo /opt/vaultkeep/current/src/install.sh uninstall --dry-run
```

3. Remove the installed package, managed command link, and shared templates, along with temporary data and the deployment tree:

```bash
sudo /opt/vaultkeep/current/src/install.sh uninstall
```

Normal uninstall preserves job configurations, secrets, and per-job local state for a later reinstall. It also preserves every backup destination.

To remove configuration, secrets, and local state as well:

```bash
sudo /opt/vaultkeep/current/src/install.sh uninstall --purge
```

`--purge` affects only managed core `/etc/vaultkeep` and `/var/lib/vaultkeep`.
Every user's per-job Trigger state directories, receipts, launch records, logs,
blocks, review warnings, and user-mode core data survive normal uninstall and purge.
It does not remove backup archives, wallets, or hook executables. Installer-added Debian packages remain installed because other applications can use them.

Each account should inspect `systemctl --user list-unit-files 'vaultkeep-trigger*'`
and remove only its obsolete local units/drop-ins after backing them up. Reload that
user manager. Keep Trigger data unless it is deliberately retired after all work is
gone and evidence reviewed; see [recovery](docs/trigger/recovery.md).

## Design and implementation reference

This guide intentionally omits internal module boundaries, typed models, hashing encodings, atomic-commit mechanics, manifest validation algorithms, systemd rendering details, security rationale, and implementation milestones.

The authoritative specification is [architecture_and_design.md](docs/design/architecture_and_design.md).
