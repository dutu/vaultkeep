# Vaultkeep release notes

## 0.3.0

### What's new

- Back up automatically after Electrum, Bisq 1, Wasabi, or Monero shuts down. For Monero, close all configured Wallet GUI and Wallet RPC processes. [Trigger setup](README.md#trigger)
- Give each file its own archive and retention history with `backup_mode: per_file`. Require an application to be stopped with `consistency.mode: application_offline`. [Job configuration](docs/configuration.md#job-structure)
- See retention decisions in `list` and verify full archives without the original source. Interrupted cleanup can resume, and pruning protects suspect backups. [List and verify](docs/cli.md#list-job-backups)
- Follow live progress, check job health with `status --json`, and set up desktop or external notifications. [Status](docs/status.md) · [Notifications](docs/notifications.md)
- Schedule installation checks backup readiness. Updates refresh managed timers and monitors and resume those that were active. [Update guide](README.md#update-or-reinstall-vaultkeep)

### Upgrading from 0.2.0

- Use Debian 13 on amd64 or arm64. [Requirements](README.md#requirements)
- Recreate version 1 jobs with the current examples and use fresh destinations. Keep old archives for restores. [Job setup](README.md#create-a-backup-job) · [Restore guide](README.md#restore-a-backup)
- Remove `application.user` from user-mode jobs. If you used standalone Trigger, preserve its settings, review unfinished attempts, and move settings into the job YAML. [Trigger upgrade steps](docs/maintenance.md#legacy-trigger-recovery-evidence)
- Replace `timer`, `timers`, and `timer status` in scripts with `schedule`, `schedules`, and `status`, respectively. [CLI reference](docs/cli.md#configure-a-job-timer)
- Remove `destination.mount_point`. CIFS/SMB and NFS need server-backed locking. For shared histories, pause automation and let active jobs finish on every writer, then upgrade them all before resuming. [Destination checks](docs/configuration.md#destination-safety-checks)
