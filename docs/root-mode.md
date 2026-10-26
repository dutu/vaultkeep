# Vaultkeep root mode guide

Root mode is for administrator-managed backups of protected/system data, root-owned
secrets, mounted shares, and administrator-controlled hooks. It uses the centrally
installed `vaultkeep` command without `--user`; run privileged operations with `sudo`.
Manual and Schedule activation are supported. Trigger rejects root execution.

| Purpose | Default location |
| --- | --- |
| Job configurations | `/etc/vaultkeep/jobs` |
| Secrets | `/etc/vaultkeep/secrets` |
| Local state | `/var/lib/vaultkeep/jobs` |
| Temporary encrypted-archive workspaces | `/var/lib/vaultkeep/tmp` |
| Locks | `/run/lock/vaultkeep` |
| Systemd units and drop-ins | `/etc/systemd/system` |

Root mode does not read user jobs, state, locks, timer registries, or user unit files.
Root schedules can coexist with user-owned Trigger jobs. Each job/machine must use
an independent destination namespace, even when jobs run under different accounts.

## Install or update Vaultkeep

An administrator [installs Vaultkeep once](../README.md#installation) for all scopes.
Use the [README update procedure](../README.md#update-the-application) for newer
versions; [maintenance](maintenance.md) explains custom paths, same-version reinstall,
and recovery. With a custom layout, substitute the selected command/configuration
paths below and repeat installer overrides for updates or uninstall.

## Create a root-mode job

1. Copy an installed example without overwriting an existing job, then edit as root:

   ```bash
   sudo cp --no-clobber /etc/vaultkeep/jobs/vaultkeep-schedule.yaml.example /etc/vaultkeep/jobs/app.yaml
   sudo nano /etc/vaultkeep/jobs/app.yaml
   ```

2. Set `job.id: app` to match `app.yaml`, replace source/destination paths, and review
   retention. Keep `schedule.enabled: false` and `trigger.enabled: false` for the
   first manual test. Use absolute paths and retain the complete example's required
   sections; see [configuration](configuration.md#job-structure).

3. Mount the intended supported local filesystem or CIFS/NFS share through the operating system first. Validate the intended
   destination from the same environment that executes Vaultkeep. Optional
   `destination.expected_fstype: ext4` can reject an unintended host-local directory
   exposed when the intended filesystem/share is absent; `destination.marker_file` independently checks
   for a regular marker you create on the verified target. Follow the complete
   [destination safety procedure](configuration.md#destination-safety-checks).

Configuration and hooks are trusted administrator input; do not allow untrusted
users to edit root jobs or scripts they invoke.

## Validate and run manually

Use [README's Manual procedure](../README.md#manual) with these root commands:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml run
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml status
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml verify
```

Full validation creates a temporary destination probe. Schema-only validation can
run without root if the configuration is readable, but skips runtime readiness:

```bash
vaultkeep --config /etc/vaultkeep/jobs/app.yaml validate --schema-only
```

For routine list/prune commands and restoration, use the
[README operations](../README.md#check-health-and-perform-everyday-operations) and
[restore procedure](../README.md#restore-a-backup), with `sudo` where access requires it.

## Configure root-mode automatic backups

Follow [README's Schedule procedure](../README.md#schedule), omit `--user`, and prefix
commands with `sudo`. This installs system `vaultkeep@<instance>.timer` and `.service`
units, rather than user units. Before installation, enabled scheduling reports
`NOT INSTALLED` and `SETUP INCOMPLETE`; resolve other failed prerequisites, install
explicitly, and validate again. Use the same runtime parameters for every command.

Inspect one job or preview all root schedule changes:

```bash
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml status
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml schedule next
sudo vaultkeep schedules list
sudo vaultkeep schedules sync --dry-run
```

The [CLI reference](cli.md#manage-all-job-timers) explains bulk synchronization,
parameterized jobs, enablement, and persistent timestamp behavior. Preview before
applying `schedules sync`.

To use shutdown Trigger, create a separate [user-owned job](user-mode.md) and run it
as the non-root application owner. Do not use sudo to monitor another user's wallet.

## Secrets and hooks

Root-mode password files must be regular files owned by `root:root` with mode exactly
`0600`, normally under `/etc/vaultkeep/secrets`. Parents must not be group/other-writable.
Use the [password-file creation procedure](configuration.md#create-a-password-file)
and retain old passphrases for old archives. Password rotation requires a new job ID,
password file, and destination namespace.

Hooks run as root through `/bin/sh -c`. Vaultkeep checks the shell and its path for
root ownership and unsafe writability; it does not individually check every command
or script invoked by the shell. Secure those scripts and use the documented fixed
PATH/environment. See [hook execution and failure semantics](configuration.md#lifecycle-hooks).
