# Vaultkeep

Vaultkeep creates independent archive backups on Debian 13. Jobs run manually,
on a systemd schedule, or after a supported application stops. Unchanged sources
create no new archive. Each backup contains an archive, SHA-256 checksum, and JSON
manifest; standard tools can restore it without Vaultkeep.

## Table of contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Create a backup job](#create-a-backup-job)
- [Run and verify your first backup — Manual](#manual)
- [Enable automatic backups](#enable-automatic-backups)
  - [Schedule](#schedule)
  - [Trigger](#trigger)
  - [Change or stop automation safely](#change-or-stop-automation-safely)
- [Check health and perform everyday operations](#check-health-and-perform-everyday-operations)
- [Restore a backup](#restore-a-backup)
- [Troubleshooting](#troubleshooting)
- [Update or reinstall Vaultkeep](#update-the-application)
- [Uninstall and optional data cleanup](#uninstall-the-application)
- [Further references](#further-references)

<a id="what-vaultkeep-does"></a>
<a id="operating-modes"></a>
## Overview

Choose **execution scope** separately from **backup activation**:

| Execution scope | Use for | Commands and locations |
| --- | --- | --- |
| User | Files readable and destinations writable by the backup owner | `vaultkeep --user`; jobs under `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/jobs` |
| Root | System/protected data and administrator-managed jobs | `sudo vaultkeep`; jobs under `/etc/vaultkeep/jobs` |

| Activation | Availability | Behavior |
| --- | --- | --- |
| Manual | User and root | `run` starts one backup immediately. |
| Schedule | User and root | A per-job systemd timer starts backups. |
| Trigger | Non-root user only | A per-job monitor requests a backup after confirmed application shutdown. |

Schedule and Trigger cannot both be enabled for one job. Manual execution remains
available with either. All use the same per-job backup lock. Scopes have independent
configuration, state, and units; install Vaultkeep once, then configure each owner.
See the [root](docs/root-mode.md) and [user](docs/user-mode.md) scope guides.

<a id="how-a-backup-works"></a>
<a id="important-boundaries"></a>
Backups use `.tar.zst` (unencrypted) or `.tar.7z` (password-protected TAR) archives.
Each job and machine needs its own destination namespace. Vaultkeep is not a
filesystem snapshot system: provide stable application data through clean shutdown,
application dumps, <a id="lifecycle-hooks"></a>[hooks](docs/configuration.md#lifecycle-hooks),
or an external snapshot. Source-change checks and a
successful archive verification do not establish application-level consistency.

<a id="prerequisites"></a>
## Requirements

- Debian 13 `trixie`, amd64 or arm64, with systemd 247+ as the active manager.
- Root access for installation, a trusted source checkout, and network access to
  Debian repositories and the Python package index.
- Git and a text editor. These procedures use `nano`; substitute your preferred editor.
- Readable sources and an existing writable destination on a supported local Linux
  filesystem or mounted CIFS/SMB or NFS share with server locking enabled.
  Subdirectory mounts are supported; mounts nested inside a destination remain unsupported.

The installer provides Debian Python 3.13+, `python3-venv`, GNU TAR, Zstandard,
Debian `7zip`, `rsync`, and mount utilities including `util-linux`. It does not use
legacy `p7zip-full`. User automation needs a reachable systemd user manager;
Trigger also needs a private account-owned `XDG_RUNTIME_DIR`.

Upgrade Debian 12 before installing current Vaultkeep. Until then, retain version
0.1.0 and its installed uninstall script; the current installer is unsupported there.

## Installation

<a id="install-the-application"></a>
1. Install the checkout and editing tools if they are not already available:

   ```bash
   sudo apt-get update
   sudo apt-get install -y git nano
   ```

2. Replace `/path/to/vaultkeep` in both commands with your chosen local checkout
   directory, and `<release-tag-or-commit>` with a published release tag or reviewed
   commit. Then clone and select that version. A development commit is not a released version.

   ```bash
   git clone https://github.com/dutu/vaultkeep.git /path/to/vaultkeep
   cd /path/to/vaultkeep
   git checkout <release-tag-or-commit>
   ```

3. <a id="check-the-installed-version"></a>Preview and apply the installation:

   ```bash
   sudo ./install.sh install --dry-run
   sudo ./install.sh install
   vaultkeep --version
   ```

   Expect the selected version. Installation uses `/opt/vaultkeep/current` and
   `/usr/local/bin/vaultkeep`. A fresh install starts no backups or monitors and
   enables no jobs or lingering. Reinstallation/update automatically pauses,
   refreshes, validates, and resumes previously active managed automation.
   The success summary lists each refreshed timer or trigger monitor by owner and
   job instance, showing whether it resumed or was left inactive. When none are
   installed, it reports `No existing managed jobs to refresh.`

   Both `install` and `update` check existing system and user jobs before activation
   or changes to schedules. Invalid configurations abort installation and report
   the YAML path and affected property when available. Correct the reported files
   as their owner and rerun the same command. See
   [configuration validation failures](docs/maintenance.md#configuration-validation-fails)
   for account checks and [maintenance safety checks](docs/maintenance.md#pause-and-resume-for-maintenance)
   for launcher and runtime failures.

The checkout can be removed after installation; updates require another trusted
checkout. Custom paths, installed files, and recovery are described in the
[maintenance reference](docs/maintenance.md). Failed stages show relevant installer
errors and report a private `Diagnostics:` transcript containing full tool output;
retain it for diagnosis. Raw dependency output stays in that log. Add `--no-color`
if needed.

<a id="usage"></a>
<a id="choose-a-mode-specific-workflow"></a>
## Create a backup job

1. Choose the starting example. Application examples include disabled Trigger
   settings and require checking against the installed application's real processes.

   | Job | Complete example | Application preparation |
   | --- | --- | --- |
   | Manual or Schedule | [General job](examples/vaultkeep-schedule.yaml) | Steps below |
   | <a id="back-up-directory-files-separately"></a>Electrum | [Electrum job](examples/electrum-trigger.yaml) | [Electrum guide](docs/trigger/apps/electrum.md), including separate wallet archives |
   | Bisq 1 GUI/API daemon | [Bisq job](examples/bisq-trigger.yaml) | [Bisq guide](docs/trigger/apps/bisq.md); explicit `--appDataDir` required |
   | Wasabi GUI/daemon | [Wasabi job](examples/wasabi-trigger.yaml) | [Wasabi guide](docs/trigger/apps/wasabi.md) |

   Repository examples use `.yaml`. The installer copies them into
   `/etc/vaultkeep/jobs/` as `.yaml.example` and overwrites those templates on each
   installation or update. Copy a template to your own job YAML before editing it.

2. For a **user-owned job**, copy the installed general example without overwriting
   an existing job, then edit it as that user:

   ```bash
   jobs_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs"
   install -d -m 700 "$jobs_dir"
   job_config="$jobs_dir/app.yaml"
   if [ ! -e "$job_config" ]; then
       install -m 600 /opt/vaultkeep/current/src/examples/vaultkeep-schedule.yaml "$job_config"
   fi
   nano "$job_config"
   ```

   For an application job, substitute `electrum-trigger.yaml`,
   `bisq-trigger.yaml`, or `wasabi-trigger.yaml` and name the YAML
   to match its `job.id`, such as `electrum-main.yaml`.

   For a **root-owned job**, use:

   ```bash
   job_config=/etc/vaultkeep/jobs/app.yaml
   sudo cp --no-clobber /etc/vaultkeep/jobs/vaultkeep-schedule.yaml.example "$job_config"
   sudo nano "$job_config"
   ```

3. <a id="minimal-job-configuration"></a><a id="destination-name-templates"></a>Set `job.id` to the YAML filename stem, replace source paths and `destination.root`,
   and review retention. For a simple job, edit these existing fields:

   ```yaml
   job:
     id: app
   sources:
     - path: /absolute/path/to/source
       archive_path_mode: prefix
       archive_prefix: source
   destination:
     root: /absolute/path/to/backups/app
     name_template: "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}"
   schedule:
     enabled: false
   trigger:
     enabled: false
   ```

   This is an edit to the complete example, not a complete job by itself. Keep its
   other required sections. Begin with both automation settings disabled. The default
   <a id="password-protected-backups"></a>`tar.zst`/`encryption.mode: none` produces unencrypted archives; configure
   [password protection](docs/configuration.md#password-protected-backups) before
   backing up secrets to a destination that requires encryption.

4. <a id="destination-safety-checks"></a>Create a local destination if needed, as its owner, or mount and verify the intended
   supported local filesystem or CIFS/NFS share first. `destination.root` must already exist. Optional
   `destination.marker_file` and `destination.expected_fstype` checks can independently
   prevent writes to an unintended local directory. For a NAS/LXC setup, follow the
   [destination safety procedure](docs/configuration.md#destination-safety-checks)
   before writing. Vaultkeep never creates a configured marker for you.

   Vaultkeep creates a persistent `.vaultkeep.lock` in each destination history.
   Every backup account needs read/write access to that file. `per_file` jobs lock
   each child history separately; independent histories can run concurrently.
   Keep lock files in place, including after a crash.

   For CIFS/NFS, writers must reach the same remote lock file through the same
   server locking domain. Keep server locks enabled: omit CIFS `nobrl` and NFS
   `nolock`/`local_lock=all`/`local_lock=flock`. See
   [CIFS and NFS locking](docs/configuration.md#cifs-and-nfs-locking) for supported
   mount layouts, cross-machine requirements, and the NFS commit limitation.

<a id="start-backups-in-the-chosen-mode"></a>
<a id="run-a-job-manually"></a>
## Run and verify your first backup — Manual

<a id="manual"></a>
1. Ensure the selected source is stable. Stop applications and related writers when
   a live copy would be inconsistent. For wallet jobs, complete the application
   preparation linked above; the examples are templates, not verified presets.

2. Set `job_config` to the actual YAML path (`app.yaml` below is an example), then
   validate, run, inspect status, and verify as the backup owner:

   ```bash
   job_config="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs/app.yaml"
   vaultkeep --user --config "$job_config" validate
   vaultkeep --user --config "$job_config" run
   vaultkeep --user --config "$job_config" status
   vaultkeep --user --config "$job_config" list
   vaultkeep --user --config "$job_config" verify
   ```

   **For root jobs**, replace the assignment with `job_config=/etc/vaultkeep/jobs/app.yaml`,
   omit `--user`, and prefix commands with `sudo`, for example
   `sudo vaultkeep --config "$job_config" run`.
   This substitution also applies to Schedule and everyday commands below.

3. Expect validation `Result: OK` with automation disabled. A successful run reports
   `run: created` or `run: unchanged`; the latter creates no archive and applies no
   retention. Verification ends with a table for manifest, SHA-256, and archive
   checks, followed by verified counts, duration, and `Result: VERIFIED` on success.
   On the first run with selected files, confirm an archive was actually created;
   an empty `per_file` selection is unchanged and verification can find zero archives.

A created backup directory contains `<base>.tar.zst` (or `.tar.7z`), its `.sha256`,
and a version-2 `<base>.json` manifest. `list` shows UTC dates, sizes, retention
choices, and complete relative backup directories. Invalid entries produce warnings
and `UNKNOWN` decisions. `verify` checks archive contents and checksums without
requiring sources and may record demonstrated corruption as `SUSPECT`.

Unresolved suspects are protected and force a new compatible archive. Ordinary
cleanup failures leave `run: created` successful with a warning; retry with `prune`
or a later archive-creating run. Corruption found in the newly committed archive
fails the run, including during cleanup. Unchanged runs never prune. See
[suspect protection, replacement, and cleanup](docs/cli.md#run-a-job-manually)
before removing suspect backups.

Jobs and destinations use schema/layout version 2 and fresh local state; version-1
repositories have no migration support and must be preserved separately. Mutations
require a supported local Linux filesystem or CIFS/NFS share with reliable shared locks; see
[destination checks](docs/configuration.md#destination-safety-checks).

`run` and `verify` show an in-place spinner with elapsed time on interactive
terminals. `run` keeps completed stages listed with a tick. Redirected stderr uses
plain stage messages and a heartbeat every 10 seconds
during long stages. Redirecting stdout also disables verification animation. Use
`verify --verbose` for full paths and individual checks. See
[progress display](docs/cli.md#progress-display).
<a id="validate-a-job-configuration"></a>
Full validation writes/renames/removes a temporary destination probe; schema-only
validation skips runtime checks and does not establish readiness.

## Enable automatic backups

<a id="cli-reference"></a>
<a id="runtime-template-parameters"></a>
Use the same job and owner as the tested manual backup. Global options (`--user`,
`--config`, repeated `--param KEY=VALUE`) go before the command; action-specific
options such as `status --json` follow it. Keep parameter selections consistent.

<a id="configure-a-job-timer"></a>
### Schedule

1. Keep `trigger.enabled: false` and replace the existing schedule block:

   ```yaml
   schedule:
     enabled: true
     interval: daily
     at: "03:30"
     persistent: true
   ```

2. Set the actual YAML path (`app.yaml` is an example), then validate and install.
   For root jobs, replace the assignment with `job_config=/etc/vaultkeep/jobs/app.yaml`, omit `--user`, and
   prefix each command with `sudo`:

   ```bash
   job_config="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs/app.yaml"
   vaultkeep --user --config "$job_config" validate
   vaultkeep --user --config "$job_config" schedule install
   vaultkeep --user --config "$job_config" status
   vaultkeep --user --config "$job_config" schedule next
   vaultkeep --user --config "$job_config" validate
   ```

   Before installation, expect `Timer: NOT INSTALLED` and `SETUP INCOMPLETE`.
   Correct any other failed checks before continuing. Afterwards, expect an enabled,
   active timer, a next-run time, and `Result: OK`. Root jobs use system timers;
   user jobs use the owner's user manager. For operation after logout, review
   [user-manager lifetime and lingering](docs/user-mode.md#configure-user-mode-automatic-backups).

Weekly/monthly schedules and deterministic time windows are described in
[schedule settings](docs/configuration.md#schedule-settings). Persistent timers can
perform one catch-up activation after downtime.

<a id="optional-trigger"></a>
<a id="configure-and-validate-trigger"></a>
<a id="start-trigger-explicitly"></a>
### Trigger

Omit `application.user` in user-mode YAML; Vaultkeep derives the application owner
from the job execution account. Remove that field from existing user-mode jobs
before updating. Keep `application.name`: it selects application-specific
data-directory detection rules. Root-mode offline backups may use `application.user`
to target another account. See [application identity](docs/configuration.md#consistency-and-shared-application-identity).

Trigger supports **non-root user mode only**. Run as the application owner with
`--user`, without `sudo`. It needs an active systemd user manager and a private,
account-owned `XDG_RUNTIME_DIR`; a graphical desktop is optional.

1. Prepare and manually test a job using the [Electrum](docs/trigger/apps/electrum.md),
   [Bisq 1](docs/trigger/apps/bisq.md), or [Wasabi](docs/trigger/apps/wasabi.md) guide.
   Check that it identifies the correct application/data directory and covers every
   process that can write the selected files. Use one job/monitor per application
   directory and an independent destination. Examples must be verified locally;
   [process-detection details](docs/trigger/process-detection.md) remain in the reference.

2. Keep `schedule.enabled: false` and set `trigger.enabled: true` only after those
   checks. Set the actual job path (`electrum-main.yaml` is an example), then run:

   ```bash
   job_config="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs/electrum-main.yaml"
   vaultkeep --user --config "$job_config" validate
   vaultkeep --user --config "$job_config" trigger install
   vaultkeep --user --config "$job_config" status
   vaultkeep --user --config "$job_config" validate
   ```

   Before installation, `Monitor service: NOT INSTALLED` and `SETUP INCOMPLETE` are
   expected. Fix other failed checks first. After installation, allow the monitor's
   first observation before expecting `Result: OK`. Validation does not certify
   application consistency, writer coverage, matcher overlap, or destination independence.

3. Start the application and use `status` to confirm fresh `Application: RUNNING`.
   Close it normally; expect `CONFIRMING STOP`, then `HAS STOPPED` and a backup
   request. Check the Backup result and run `verify`. A monitor started with the
   application already stopped requests nothing: it must first observe it running.

Unreadable or uncertain process evidence blocks detection. Polling can miss short
lifecycles, and a stopped check cannot prevent a later restart. Process disappearance
does not prove clean shutdown; review the [consistency limits](docs/trigger/consistency.md).
For foreground monitoring, use `trigger monitor` instead of installing a service;
run only one monitor for the job. Trigger never configures lingering.

### Change or stop automation safely

| Action | Schedule | Trigger |
| --- | --- | --- |
| `install` | Create and enable/start timer | Create and enable/start monitor |
| `update` | Refresh timer; preserve enablement | Refresh, enable, and restart monitor |
| `disable` | Stop/disable timer; clear persistent timestamps | Stop/disable monitor |
| `remove` | Also remove managed schedule files/registration | Also remove generated monitor unit |

Use `vaultkeep --user --config /absolute/path/job.yaml schedule <action>`, replacing
`<action>` with the table entry; substitute `trigger` for monitor actions. For root
schedules, omit `--user`, use the root YAML path, and prefix the command with `sudo`.
Disable/remove preserve jobs, archives, and state and leave running backups/workers
alone. Monitor stop is refused while a backup is active or Trigger recovery/safety
state needs reconciliation; wait and review the reported state before retrying.
Editing YAML does not stop existing automation. Before switching mechanisms,
disable the previous timer/monitor and drain its workers and queued work. Moving a
Trigger job or changing its ID/parameters creates a new identity; retire the old one
separately and preserve [recovery evidence](docs/trigger/recovery.md).

<a id="check-job-status"></a>
<a id="trigger-operation-and-health"></a>
## Check health and perform everyday operations

Set the actual YAML path before using this table; replace `app.yaml` as needed:

```bash
job_config="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs/app.yaml"
```

For root jobs, set `job_config=/etc/vaultkeep/jobs/app.yaml`, omit `--user`, and
prefix commands with `sudo`. Keep any `--param KEY=VALUE` selections used by the job.
<a id="terminal-output"></a>
See [CLI output and options](docs/cli.md#terminal-output) for report conventions.

| Task | Command |
| --- | --- |
| Validate paths and automation health | `vaultkeep --user --config "$job_config" validate` |
| Read activity, previous result, and automation | `vaultkeep --user --config "$job_config" status` |
| Detailed diagnostics or script-readable status | Append `--verbose` or `--json` to `status` |
| Run now | `vaultkeep --user --config "$job_config" run` |
| <a id="list-job-backups"></a>List backups and preview retention | `vaultkeep --user --config "$job_config" list` |
| <a id="verify-job-backups"></a>Verify contents | `vaultkeep --user --config "$job_config" verify` |
| <a id="prune-job-backups"></a>Check retention plan without deleting | `vaultkeep --user --config "$job_config" prune --dry-run` |
| Apply retention | `vaultkeep --user --config "$job_config" prune` |
| <a id="manage-all-job-timers"></a>Inspect all timers in this scope | `vaultkeep --user schedules list` |
| Preview bulk schedule changes | `vaultkeep --user schedules sync --dry-run` |

`list` shows which tiers select each backup without deleting anything. For example,
with one hourly and one daily recovery point configured:

```text
Vaultkeep — Backups

Job:         app
Destination: /mnt/backups/app
Retention:   1 hourly, 1 daily, 0 weekly, 0 monthly, 0 yearly

Created (UTC)        Size       Decision  Tiers
────────────────────────────────────────────────────────────
2026-10-25 08:00:00  756.7 MiB  KEEP      daily, hourly
  Backup: backup-app-20261025T080000Z

2026-10-25 07:00:00  756.7 MiB  PRUNE     —
  Backup: backup-app-20261025T070000Z
────────────────────────────────────────────────────────────

Backups: 2 · Keep: 1 · Prune: 1

```

Inspect warnings before pruning; `UNKNOWN` means the retention preview is incomplete.
See [inventory and retention decisions](docs/cli.md#list-job-backups).

`status` is read-only and works without mounted destinations or available sources.
Exit 0 means the report was readable, including a previous failure. Read Backup
activity separately from the previous result and from Trigger/Schedule health.
`UNKNOWN` requires investigation; idle status is not a guarantee that starting an
application is safe. See [status meanings](docs/status.md).

Retention is calendar-bucketed; time passing and unchanged runs do not prune.
Deleting/excluding a per-file source preserves its history. Check the retention plan
before manual pruning. A failure can occur after archive commit, so inspect existing backups
rather than assuming none was created.

For application backups, set `consistency.mode: application_offline` and configure
the shared `application` identity. In user mode, omit `application.user`; the job's
execution account owns the application. In root mode, set `application.user` when
targeting another account. This policy applies equally to manual, scheduled, and
Trigger execution. `files` is the default and uses file consistency checks.
`after_archive` may restart the application after source reads finish. See
[consistency settings](docs/configuration.md#consistency-and-shared-application-identity).

### Update a job configuration

<a id="refresh-existing-monitor-commands"></a>
Use the actual YAML path with the command prefix
`vaultkeep --user --config /absolute/path/job.yaml` (for root, omit `--user` and use
`sudo`). Edit and validate the YAML as its owner. For an installed timer run `schedule update`;
for an installed monitor run `trigger update` when monitoring should resume, then
validate again. New content/archive settings can require a new backup; retention
changes alone do not. Apply changed retention with a prune preview followed by prune.
See [configuration changes](docs/configuration.md#apply-configuration-changes).

<a id="desktop-notifications"></a>
### Optional desktop notifications

Backup alerts and Trigger alerts are independently disabled by default. Follow
[notification setup](docs/notifications.md#enable-notifications) to install/test
`notify-send` and enable the desired category. Delivery requires a desktop session
for the backup owner in the same host/container and is best-effort; failure never
fails the backup. An installed monitor needs `trigger update` after settings change.

## Restore a backup

Vaultkeep has no restore command. Verify integrity **before extracting**, then restore
into an **empty staging directory** and inspect the result before replacing live files.
Stop the application and related writers before copying restored data into place.
Use `sudo` where archive access or extraction requires root.

**Prefer `vaultkeep verify` when the original job configuration is available:**

```bash
vaultkeep --user --config /absolute/path/job.yaml verify
```

Use the original `--param` selections; for root jobs, omit `--user` and use `sudo`.
Confirm the selected archive is reported as verified before extracting it.
[Vaultkeep verification](docs/cli.md#verify-job-backups) checks manifests, archive
structure, and TAR member paths as well as checksums and archive integrity.
Progress shows the current backup, SHA-256 bytes and percentage when available,
and archive validation operations, followed by validation and verification counts.

The standalone checks below support disaster recovery without Vaultkeep or its job
configuration. They detect corruption but do not perform all of Vaultkeep's structural
and path-safety checks.

<a id="restore-an-unencrypted-backup"></a>
### Unencrypted archive

1. Replace the directory and archive filenames with the selected backup's paths:

   ```bash
   cd /absolute/path/to/backup-directory
   sha256sum --check backup.tar.zst.sha256
   zstd --test backup.tar.zst
   ```

   Expect the checksum to report `OK` and the stream test to succeed. Stop if either
   fails; do not extract an archive whose integrity could not be verified.

2. From that backup directory, extract into a new staging directory:

   ```bash
   restore_dir="$HOME/vaultkeep-restore"
   mkdir -m 700 "$restore_dir"
   zstd --decompress --stdout backup.tar.zst \
     | tar --extract --file=- --directory="$restore_dir"
   ```

<a id="restore-an-encrypted-backup"></a>
### Encrypted archive

1. Replace the directory and archive filenames, then verify before extracting:

   ```bash
   cd /absolute/path/to/backup-directory
   sha256sum --check backup.tar.7z.sha256
   7z t backup.tar.7z
   ```

   7-Zip prompts for the original passphrase. Expect checksum `OK` and a successful
   archive test. Stop on any checksum, password, or archive-test failure.

2. From that backup directory, preserve the absolute archive path before changing
   directories. The inner TAR is named after `job.id` (`app.tar` in this example):

   ```bash
   archive="$PWD/backup.tar.7z"
   restore_root="$HOME/vaultkeep-restore-encrypted"
   mkdir -m 700 "$restore_root"
   mkdir -m 700 "$restore_root/staging" "$restore_root/work"
   cd "$restore_root/work"
   7z x "$archive"
   tar --extract --file=app.tar --directory="$restore_root/staging"
   ```

After checking the restored content, remove the plaintext working TAR when no longer
needed. Secure physical erasure is not guaranteed. Retain old passphrases for old
archives; [password rotation requires a new namespace](docs/configuration.md#password-protected-backups).
Application-specific recovery and uncertain Trigger outcomes need separate
[evidence review](docs/trigger/recovery.md#operator-review-and-repair).

<a id="logs-and-troubleshooting"></a>
## Troubleshooting

Use the actual job path below, adding its `--param` selections if needed. For root
jobs, omit `--user` and prefix commands with `sudo`:

```bash
vaultkeep --user --config /absolute/path/job.yaml validate
vaultkeep --user --config /absolute/path/job.yaml status --verbose
```

If a newer or broken job schema prevents validation, `status` still reports available
diagnostics when job identity is recognizable. Stop the existing automation with
`trigger disable` or `schedule disable` using the same options and YAML path. These
commands tolerate unrelated configuration errors, including unsupported properties,
while retaining identity, ownership, and runtime safety checks. They do not edit the
YAML. Malformed/unreadable YAML or invalid identity fields must be repaired before
disabling. See [Trigger recovery commands](docs/cli.md#service-management) and
[schedule recovery](docs/cli.md#configure-a-job-timer).

Read `Previous backup`, `Reason`, and the displayed `Log` path. Review logs before sharing.
For scheduled user jobs use `journalctl --user -u vaultkeep@app.service`;
root jobs use `sudo journalctl -u vaultkeep@app.service`. Parameterized unit names
can differ. For monitors, use the exact unit name shown in verbose status:

```bash
journalctl --user -u 'vaultkeep-trigger@<job-id>-<identity>.service' -n 100 --no-pager
systemctl --user list-units --all 'vaultkeep-trigger-exec@*.service'
systemctl --user list-jobs --no-pager
```

Preserve uncertain outcomes, receipts, and launch records. Do not delete Trigger
state to unblock dispatch. Local backup `state.json` is a separate reconstructable
cache; do not edit destination manifests to repair it. Full diagnosis is in
[general troubleshooting](docs/troubleshooting.md) and
[Trigger troubleshooting](docs/trigger/troubleshooting.md).

<a id="maintenance-and-removal"></a>
<a id="update-the-application"></a>
## Update or reinstall Vaultkeep

When upgrading from ancestor-directory or share-root locking, stop automation on
all machines writing to the affected destinations and wait for active operations
to finish. Upgrade every writer before resuming automation; old and new releases
use different locks. Backup files and configuration need no migration. See
[maintenance guidance](docs/maintenance.md#update-behavior-and-failure-recovery).

1. From a trusted checkout on Debian 13, select the intended newer source:

   ```bash
   cd /path/to/vaultkeep
   git fetch --tags --prune
   git checkout <new-release-tag-or-commit>
   sudo ./install.sh update --dry-run
   sudo ./install.sh update
   vaultkeep --version
   ```

2. Validate affected jobs in their owning scopes and inspect their status. Existing
   managed timers/monitors are refreshed automatically; only previously active
   launchers resume. New schedules remain an explicit choice. For root schedules:

   ```bash
   sudo vaultkeep schedules validate
   sudo vaultkeep schedules sync --dry-run
   ```

   Apply `schedules sync` only after reviewing intended additions/removals.

<a id="reinstall-the-same-application-version"></a>
The installer installs the selected checkout; it does not fetch source. For changed
source with the **same version**, use `install --dry-run`, then `install` instead of
`update`. Exact version/source matches perform idempotent verification.

<a id="pause-and-resume-for-maintenance"></a>
Maintenance pauses launchers and refuses active backups, workers, queued work, held
locks, or inconclusive activity; it never stops running backups. Preserve recovery
snapshots on failure. Rollback excludes apt changes; power loss/SIGKILL requires
inspection before retrying. If rollback or resumption fails, follow the reported
journal and [maintenance recovery](docs/maintenance.md#pause-and-resume-for-maintenance).
For old/custom launchers and custom XDG layouts, read that reference before upgrading.

<a id="uninstall-the-application"></a>
## Uninstall and optional data cleanup

1. Disable every job's installed automation in its owning account, including
   logged-out accounts. Run **only the applicable command for each job**, using its
   actual YAML path and original `--param` selections. Manual-only jobs need neither.

   For a Schedule job:

   ```bash
   vaultkeep --user --config /absolute/path/job.yaml schedule disable
   ```

   For a Trigger job:

   ```bash
   vaultkeep --user --config /absolute/path/job.yaml trigger disable
   ```

   For root timers, omit `--user`, use the root YAML path, and prefix with `sudo`.
   Let backups, workers, and queued work finish; stopping a monitor does not stop
   workers. Preserve unresolved evidence. For unit inventory, missing YAML files,
   and remaining local units/overrides, use the [unit checklist](docs/maintenance.md#uninstall-unit-checklist).

2. Preview, then remove the installation:

   ```bash
   sudo /opt/vaultkeep/current/src/install.sh uninstall --dry-run
   sudo /opt/vaultkeep/current/src/install.sh uninstall
   ```

   To also remove managed root configuration, secrets, and local state, choose
   `uninstall --purge --dry-run`, then `uninstall --purge` **instead**, before the
   installed script is removed. Custom layouts require the original installer overrides.

Normal uninstall preserves configuration, secrets, local state, and backups.
Purge additionally removes managed core `/etc/vaultkeep` and `/var/lib/vaultkeep`.
Both preserve user-mode data, Trigger evidence, archives, wallets, hook scripts,
and Debian dependencies. See [cleanup boundaries](docs/maintenance.md#uninstall-boundaries-and-recovery-evidence)
before deleting anything else.

<a id="design-and-implementation-reference"></a>
## Further references

| Topic | Complete reference |
| --- | --- |
| Sources, independent file histories, destinations, encryption, retention, hooks | [Configuration](docs/configuration.md) |
| Commands, validation, output, timer management, exit codes | [CLI](docs/cli.md) |
| Paths, privileges, and user-manager lifetime | [Root scope](docs/root-mode.md), [user scope](docs/user-mode.md) |
| Status and optional notifications | [Status](docs/status.md), [notifications](docs/notifications.md) |
| Installation layout, maintenance, rollback, and recovery | [Maintenance](docs/maintenance.md), [troubleshooting](docs/troubleshooting.md) |
| Trigger configuration and safety | [Configuration](docs/trigger/configuration.md), [process detection](docs/trigger/process-detection.md), [consistency](docs/trigger/consistency.md), [recovery](docs/trigger/recovery.md) |
| Development and technical design | [Testing](docs/development/testing.md), [architecture](docs/design/architecture_and_design.md), [Trigger design](docs/design/trigger.md) |
