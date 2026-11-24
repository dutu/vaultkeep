# Vaultkeep

Vaultkeep creates independent archive backups on Debian 13, manually, on a systemd schedule, or after an application stops. Unchanged sources create no new archive. Each backup includes an archive, SHA-256 checksum and JSON manifest, and can be restored without Vaultkeep.

[Installation](#1-installation) · [Configuration](#2-configuration) · [First backup](#3-first-backup) · [Automation](#4-automatic-backups) · [CLI](#5-cli-reference) · [Restore](#6-restore) · [Maintenance](#7-maintenance) · [Appendix](#appendix)

| Mode | User | Root | Behavior |
| --- | --- | --- | --- |
| Manual | Yes | Yes | Run on demand |
| Schedule | Yes | Yes | systemd timer |
| Trigger | Yes | No | Backup after confirmed application shutdown |

Schedule and Trigger are mutually exclusive per job. Manual execution remains available with either.

## 1. Installation

**Requirements:** Debian 13 (amd64/arm64), active systemd 247+, root access for installation, network access to Debian repositories and the Python package index. The installer supplies runtime dependencies. Debian 12 users must retain version 0.1.0 until upgrading the OS.

```bash
sudo apt-get update
sudo apt-get install -y git nano
git clone https://github.com/dutu/vaultkeep.git
cd vaultkeep
git checkout <release-tag-or-commit>
sudo ./install.sh install --dry-run
sudo ./install.sh install
vaultkeep --version
```

Select a published release tag or reviewed commit before installing. Install once per machine; a fresh installation enables no jobs. The checkout can be removed afterwards. See [maintenance](docs/maintenance.md) for custom paths and installer diagnostics.

## 2. Configuration

### Choose execution scope

| | User (recommended for personal application data) | Root |
| --- | --- | --- |
| CLI | `vaultkeep --user` | `sudo vaultkeep` |
| Job directory | `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/jobs` | `/etc/vaultkeep/jobs` |
| Access | Backup owner's permissions | System/protected data |

Configure and run each job as its owner. See [user mode](docs/user-mode.md) and [root mode](docs/root-mode.md).

### Choose a template

| Job | YAML example | Setup guide |
| --- | --- | --- |
| General files | [vaultkeep-schedule.yaml](examples/vaultkeep-schedule.yaml) | Below |
| Electrum | [electrum-trigger.yaml](examples/electrum-trigger.yaml) | [Electrum](docs/trigger/apps/electrum.md) |
| Bisq 1 | [bisq-trigger.yaml](examples/bisq-trigger.yaml) | [Bisq](docs/trigger/apps/bisq.md) |
| Bisq 1 live schedule | [bisq-schedule.yaml](examples/bisq-schedule.yaml) | [Bisq](docs/trigger/apps/bisq.md); adjust source and hook paths |
| Wasabi | [wasabi-trigger.yaml](examples/wasabi-trigger.yaml) | [Wasabi](docs/trigger/apps/wasabi.md) |
| Monero GUI/RPC | [monero-trigger.yaml](examples/monero-trigger.yaml) | [Monero](docs/trigger/apps/monero.md) |

Installed templates are also available in `/etc/vaultkeep/jobs/` with the suffix `.yaml.example`. Copy before editing; updates overwrite templates.

**User job:**

```bash
jobs_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs"
install -d -m 700 "$jobs_dir"
job_config="$jobs_dir/app.yaml"
if [ ! -e "$job_config" ]; then
  install -m 600 /opt/vaultkeep/current/src/examples/vaultkeep-schedule.yaml "$job_config"
fi
nano "$job_config"
```

**Root job (alternative):**

```bash
job_config=/etc/vaultkeep/jobs/app.yaml
sudo cp --no-clobber /etc/vaultkeep/jobs/vaultkeep-schedule.yaml.example "$job_config"
sudo nano "$job_config"
```

For an application job, substitute the corresponding template and filename.

### Configure the job

Edit these fields in the complete template; retain its other required sections:

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

- Match `job.id` to the YAML filename stem and review retention in the template.
- Create the destination or mount the intended share first. Use an independent destination namespace per job and machine.
- For CIFS/NFS, enable server locking and configure destination checks to prevent accidental local writes. See [destination safety](docs/configuration.md#destination-safety-checks) and [locking](docs/configuration.md#cifs-and-nfs-locking).
- The default `tar.zst` archive is unencrypted. Configure [password protection](docs/configuration.md#password-protected-backups) before storing secrets on a destination requiring encryption.
- For wallet/application data, follow the application guide and configure [offline consistency](docs/configuration.md#consistency-and-shared-application-identity) where required. User jobs omit `application.user`.

Optional settings: [independent file backups](docs/configuration.md#back-up-directory-files-separately), [hooks](docs/configuration.md#lifecycle-hooks), [desktop notifications](docs/notifications.md#enable-notifications), [external notifications](docs/configuration.md#external-notifications).

## 3. First backup

Ensure the source is stable; stop applications and related writers if live reads would be inconsistent. Vaultkeep is not a filesystem snapshot system.

The following examples use the user scope and the `job_config` variable defined above. For root jobs, use `sudo vaultkeep` instead of `vaultkeep --user`. Set the actual YAML path in each new shell. Preserve any `--param KEY=VALUE` selections throughout.

```bash
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" run
vaultkeep --user --config "$job_config" status
vaultkeep --user --config "$job_config" list
vaultkeep --user --config "$job_config" verify
```

Confirm `Result: OK`, `run: created` and `Result: VERIFIED`, with at least one verified archive for a nonempty first backup. `run: unchanged` creates no archive and performs no retention; an empty per-file selection may verify zero archives.

## 4. Automatic backups

Enable automation only after testing the manual backup. Editing YAML alone does not change installed automation.

### Schedule

Set the existing schedule block, keeping `trigger.enabled: false`:

```yaml
schedule:
  enabled: true
  interval: daily
  at: "03:30"
  persistent: true
```

```bash
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" schedule install
vaultkeep --user --config "$job_config" schedule next
vaultkeep --user --config "$job_config" validate
```

Before installation, `Timer: NOT INSTALLED` and `SETUP INCOMPLETE` are expected; fix other failures first. Afterwards, confirm `Result: OK` and a next-run time.

See [schedule settings](docs/configuration.md#schedule-settings) for weekly/monthly schedules and time windows. For user jobs that must run after logout, review [lingering](docs/user-mode.md#configure-user-mode-automatic-backups).

### Trigger

Run as the non-root application owner with a reachable systemd user manager and private, account-owned `XDG_RUNTIME_DIR`.

1. Follow the selected application guide, verify the matcher and data directory, and cover every writer. Use one monitor per application directory and an independent destination.
2. Keep `schedule.enabled: false`; set `trigger.enabled: true` in the tested job.
3. Install and check the monitor:

```bash
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" trigger install
vaultkeep --user --config "$job_config" status
vaultkeep --user --config "$job_config" validate
```

Before installation, `Monitor service: NOT INSTALLED` and `SETUP INCOMPLETE` are expected; fix other failures first. Allow the first monitor observation before expecting `Result: OK`.

Start the application, confirm `Application: RUNNING`, close it normally and check for `CONFIRMING STOP`, then `HAS STOPPED` and a successful backup result. Run `verify`. A monitor started while the application is stopped must first observe it running before requesting a backup.

Trigger detection does not guarantee clean shutdown or prevent a restart during backup. See [consistency limits](docs/trigger/consistency.md).

### Change or stop automation

| Action | Schedule | Trigger |
| --- | --- | --- |
| `install` | Create and enable/start | Create and enable/start |
| `update` | Refresh; preserve enablement | Refresh, enable and restart |
| `disable` | Stop/disable timer; clear persistent timestamps | Stop/disable monitor |
| `remove` | Also remove managed files/registration | Also remove generated monitor unit |

After editing YAML, validate, run `schedule update` or `trigger update` as applicable, then validate again. Disable the previous mechanism and drain workers/queued work before switching. Disable/remove preserve jobs, archives and state and do not stop running backups. Trigger may refuse unsafe stops; resolve the reported condition before retrying.

## 5. CLI reference

```text
vaultkeep [global options] <command> [command options]
```

Global options precede the command; command-specific options follow it.

| Global option | Purpose |
| --- | --- |
| `--user` | User execution scope; omit for root scope with `sudo` |
| `--config PATH` | Job YAML path |
| `--param KEY=VALUE` | Runtime template parameter; repeat as needed |
| `--version` | Installed version and commit |

For job commands below, use `vaultkeep --user --config "$job_config"` (or the root equivalent) followed by the command shown.

| Command | Purpose |
| --- | --- |
| `validate` | Validate configuration and runtime readiness |
| `run` | Run backup now |
| `status` | Activity, previous result, automation health |
| `status --verbose` | Detailed diagnostics |
| `status --json` | Machine-readable status |
| `list` | List archives and preview retention |
| `verify` | Verify manifests, checksums and archives |
| `verify --verbose` | Detailed verification |
| `prune --dry-run` | Preview retention deletions |
| `prune` | Apply retention |
| `schedule install / update / disable / remove` | Manage timer (choose one action) |
| `schedule next` | Show next scheduled run |
| `trigger install / update / disable / remove` | Manage monitor (choose one action) |
| `trigger monitor` | Foreground monitoring instead of service |

Scope-wide commands do not use the per-job prefix:

| Command (after `vaultkeep --user` or `sudo vaultkeep`) | Purpose |
| --- | --- |
| `schedules list` | Inspect timers |
| `schedules validate` | Validate schedules |
| `schedules sync --dry-run` | Preview bulk schedule changes |
| `schedules sync` | Apply reviewed bulk changes |

`vaultkeep notify` is a lifecycle-hook helper for external notifications. See the [complete CLI reference](docs/cli.md) for additional options, validation modes, output and exit codes.

**Operational notes:** `status` exit 0 means the report was readable, not that the last backup succeeded. `list` is non-destructive; `ELIGIBLE` is not proof of archive integrity. Inspect warnings and `UNKNOWN` decisions before pruning. See [status meanings](docs/status.md).

## 6. Restore

There is no Vaultkeep restore command. Verify before extraction, restore into an empty staging directory, inspect the files, and stop all application writers before replacing live data.

When the original job is available, prefer `verify` using its original parameters and confirm the selected archive was verified. Standalone recovery below does not perform all Vaultkeep structural/path-safety checks. Extract only trusted archives; use root where necessary.

### Unencrypted archive

From the selected backup directory, substitute actual filenames. Stop if either verification fails.

```bash
sha256sum --check backup.tar.zst.sha256 && zstd --test backup.tar.zst
```

After successful verification:

```bash
restore_dir="$HOME/vaultkeep-restore"
mkdir -m 700 "$restore_dir" &&
  zstd --decompress --stdout backup.tar.zst | tar --extract --file=- --directory="$restore_dir"
```

### Encrypted archive

From the selected backup directory:

```bash
sha256sum --check backup.tar.7z.sha256 && 7z t backup.tar.7z
```

Proceed only after both checks succeed. 7-Zip prompts for the original passphrase. Substitute the inner TAR name (derived from `job.id`).

```bash
archive="$PWD/backup.tar.7z"
restore_root="$HOME/vaultkeep-restore-encrypted"
mkdir -m 700 "$restore_root" &&
  mkdir -m 700 "$restore_root/staging" "$restore_root/work" &&
  cd "$restore_root/work" &&
  7z x "$archive" &&
  tar --extract --file=app.tar --directory="$restore_root/staging"
```

Remove the plaintext working TAR when no longer needed. Secure physical erasure is not guaranteed. Retain old passphrases; [password rotation requires a new namespace](docs/configuration.md#password-protected-backups).

## 7. Maintenance

### Update or reinstall

Read the [release notes](CHANGELOG.md) and [maintenance reference](docs/maintenance.md) first. When upgrading from ancestor-directory/share-root locking, stop automation and drain operations on **all writers** before upgrading every writer and resuming.

```bash
cd /path/to/vaultkeep
git fetch --tags --prune
git checkout <new-release-tag-or-commit>
sudo ./install.sh update --dry-run
sudo ./install.sh update
vaultkeep --version
```

For changed source with the same version, use `install` instead of `update`, including the dry run. Existing managed automation is refreshed automatically; only previously active launchers resume. Validate and inspect affected jobs in their owning scopes.

### Uninstall

Disable applicable timers/monitors in every owning account, including logged-out accounts. Let backups, workers and queued work finish; preserve unresolved recovery evidence. See the [unit checklist](docs/maintenance.md#uninstall-unit-checklist).

```bash
sudo /opt/vaultkeep/current/src/install.sh uninstall --dry-run
sudo /opt/vaultkeep/current/src/install.sh uninstall
```

Normal uninstall preserves configuration, secrets, state and backups. To additionally remove managed root configuration/secrets/state, use `uninstall --purge --dry-run` followed by `uninstall --purge` **instead**. Both preserve user data, Trigger evidence, archives, wallets, hooks and Debian dependencies. See [cleanup boundaries](docs/maintenance.md#uninstall-boundaries-and-recovery-evidence).

## Appendix

### A. Destination and backup safety

- Supported destinations: local Linux filesystems and mounted CIFS/NFS shares with reliable server locking. Subdirectory mounts are supported; mounts nested inside a destination are not.
- `destination.marker_file` and `destination.expected_fstype` independently guard against unintended writes. Vaultkeep never creates a configured marker.
- Keep persistent `.vaultkeep.lock` files in place, including after crashes. Every backup account needs read/write access. CIFS must omit `nobrl`; NFS must omit `nolock`, `local_lock=all` and `local_lock=flock`. Writers must reach the same remote lock through the same server locking domain.
- Per-file jobs lock child histories independently and briefly lock the parent to update `.vaultkeep-files.json`. Preserve this index with the archives.
- Jobs and destinations use schema/layout version 2. Version-1 repositories have no migration support; preserve them separately.
- Full validation writes, renames and removes a temporary destination probe. Schema-only validation does not establish runtime readiness.

See [configuration](docs/configuration.md), including [destination checks](docs/configuration.md#destination-safety-checks) and [CIFS/NFS locking limitations](docs/configuration.md#cifs-and-nfs-locking).

### B. Backup integrity and retention

Each backup directory contains `.tar.zst` or `.tar.7z`, `.sha256` and a version-2 JSON manifest. Verification can run without sources and may record demonstrated corruption as `SUSPECT`. Unresolved suspects are protected and force a new compatible archive.

Retention uses calendar buckets. Unchanged runs and time passing do not prune; deleted/excluded per-file sources retain their history. Retention changes alone do not force new archives. Preview before manually pruning.

Ordinary cleanup failures leave a created backup successful with warnings; corruption of the newly committed archive fails the run. Interrupted deletion appears as `PRUNE_PENDING` and resumes from durable intent; `EMPTY_ORPHAN` identifies a matching empty directory eligible for guarded removal. Invalid/pending entries do not fill retention tiers. A failed run may have committed an archive; inspect inventory before assuming otherwise.

See [run and cleanup behavior](docs/cli.md#run-a-job-manually), [inventory](docs/cli.md#list-job-backups) and [verification](docs/cli.md#verify-job-backups).

### C. Application consistency and Trigger identity

Use `consistency.mode: application_offline` and the shared application identity where offline backups are required. User jobs derive the owner from the execution account; root jobs may set `application.user`. Keep `application.name` for application-specific detection. The policy applies to manual, scheduled and Trigger runs. `files` is the default. `after_archive` may restart the application after source reads finish.

Process evidence can be uncertain or unreadable; detection then blocks. Polling can miss short lifecycles. Verification and change detection do not establish application-level consistency.

Each canonical YAML path has one Trigger service (`vaultkeep-trigger@<yaml-filename>-<path-hash>.service`), including foreground monitoring. YAML/parameter changes do not change this identity; explicitly update installed execution settings. Remove the old installation using its original canonical path before moving YAML. Restart alone does not adopt a changed execution identity.

A single legacy service can migrate through install/update; multiple legacy services need explicit resolution. Disable/remove can use verified installed metadata even for invalid, unreadable or deleted YAML at its original canonical path. Preserve recovery evidence. Trigger never configures lingering.

See [application consistency](docs/configuration.md#consistency-and-shared-application-identity), [process detection](docs/trigger/process-detection.md), [service management](docs/cli.md#service-management) and [recovery](docs/trigger/recovery.md).

### D. Troubleshooting

```bash
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" status --verbose
```

Inspect `Previous backup`, `Reason` and `Log`. Review logs before sharing. Use the exact installed unit name (parameterized names may differ):

```bash
journalctl --user -u vaultkeep@app.service
journalctl --user -u 'vaultkeep-trigger@<yaml-filename>-<path-hash>.service' -n 100 --no-pager
systemctl --user list-units --all 'vaultkeep-trigger-exec@*.service'
systemctl --user list-jobs --no-pager
```

For root schedules, use `sudo journalctl` without `--user`. Broken job configurations may still permit status and automation disable; Schedule disable requires recognizable job identity and parameters. Trigger disable/remove use verified installed metadata.

Do not delete Trigger state to unblock dispatch or edit destination manifests to repair local state. Preserve uncertain outcomes, receipts and launch records. See [general troubleshooting](docs/troubleshooting.md), [Trigger troubleshooting](docs/trigger/troubleshooting.md) and [operator recovery](docs/trigger/recovery.md#operator-review-and-repair).

### E. Installation and maintenance behavior

Installation uses `/opt/vaultkeep/current` and `/usr/local/bin/vaultkeep`. Existing jobs are checked before activation or schedule changes; invalid configurations abort installation. Correct files as their owner and retry. Keep private `Diagnostics:` transcripts on failure; installer `--no-color` is available.

Maintenance pauses launchers but refuses active backups, workers, queued work, held locks or inconclusive activity. It never stops running backups. Preserve recovery snapshots on failure; rollback excludes apt changes. Power loss/SIGKILL requires inspection before retrying. Custom layouts require original installer overrides. See [maintenance recovery](docs/maintenance.md#pause-and-resume-for-maintenance).

### F. Documentation

| Topic | Reference |
| --- | --- |
| YAML, sources, destinations, encryption, retention, hooks | [Configuration](docs/configuration.md) |
| Commands, options, output, exit codes | [CLI](docs/cli.md) |
| Privileges, paths and user-manager lifetime | [Root mode](docs/root-mode.md), [user mode](docs/user-mode.md) |
| Status and notifications | [Status](docs/status.md), [notifications](docs/notifications.md) |
| Installation, rollback and recovery | [Maintenance](docs/maintenance.md), [troubleshooting](docs/troubleshooting.md) |
| Trigger | [Configuration](docs/trigger/configuration.md), [detection](docs/trigger/process-detection.md), [consistency](docs/trigger/consistency.md), [recovery](docs/trigger/recovery.md) |
| Development | [Testing](docs/development/testing.md), [architecture](docs/design/architecture_and_design.md), [Trigger design](docs/design/trigger.md) |
