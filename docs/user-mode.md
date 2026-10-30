# Vaultkeep user mode guide

User mode lets an ordinary user configure, run, and automate backups after central
installation by an administrator. It can read only files and write only destinations
already accessible to that user. Use the shared `vaultkeep` command with `--user`,
without sudo, for user-owned jobs.

Execution scope is independent of activation: user jobs support Manual, Schedule,
and Trigger. Root jobs and other users' jobs have separate configuration, state,
locks, timer registries, and units. Each job/machine needs its own destination namespace.

## Administrator prerequisite

An administrator must [install Vaultkeep and Debian dependencies](../README.md#installation)
once for the host. User mode does not install packages or create system mounts.
The default shared command is `/usr/local/bin/vaultkeep`; if the administrator chose a
[custom command path](maintenance.md#installation-layout-and-custom-paths), use that
command instead. Generated monitors and timers use the selected command automatically.

## User-mode locations

Core user paths follow the selected XDG environment:

| Purpose | Default location |
| --- | --- |
| Job configurations | `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/jobs` |
| Password files | `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/secrets` |
| Local state | `${XDG_STATE_HOME:-~/.local/state}/vaultkeep/jobs` |
| Temporary encrypted-archive workspaces | `${XDG_CACHE_HOME:-~/.cache}/vaultkeep/tmp` |
| Locks | `$XDG_RUNTIME_DIR/vaultkeep/locks`, falling back to `${XDG_CACHE_HOME:-~/.cache}/vaultkeep/locks` |
| Systemd user units | `${XDG_CONFIG_HOME:-~/.config}/systemd/user` |
| Timer registry | `${XDG_STATE_HOME:-~/.local/state}/vaultkeep/systemd-instances.json` |

Keep relevant XDG settings consistent across interactive commands, timers, and monitors
so they use the same state and lock namespace. Trigger additionally requires a private
account-owned `XDG_RUNTIME_DIR` and has a **separate fixed recovery root** under the
OS account home, independent of `XDG_STATE_HOME`. See
[Trigger environment and locations](trigger/service-operation.md#state-and-runtime-locations).

## Create a user-mode job

1. Run as the backup owner and copy the installed example safely:

   ```bash
   jobs_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs"
   install -d -m 700 "$jobs_dir"
   job_config="$jobs_dir/app.yaml"
   if [ ! -e "$job_config" ]; then
       install -m 600 /opt/vaultkeep/current/src/examples/vaultkeep-schedule.yaml "$job_config"
   fi
   nano "$job_config"
   ```

2. Set `job.id: app`, use absolute readable source paths, and select an existing writable
   destination. For a local example you can create `$HOME/.local/share/backups/app`
   with `install -d -m 700 "$HOME/.local/share/backups/app"`; use its absolute path
   in YAML. Mount and verify network destinations first instead of creating a directory
   over a missing mount. Keep both automation settings disabled for the first test.

Use a filename matching `job.id` for manual/Schedule jobs. Trigger-enabled jobs with
Schedule disabled may use an independent filename; update the Trigger after changing `job.id`. Keep the complete example's other required
sections; use the [configuration reference](configuration.md) for all settings.
Examples under `/etc/vaultkeep/jobs` are administrator-owned; user jobs normally copy
from `/opt/vaultkeep/current/src/examples`. For wallet Trigger jobs, use the complete
[Electrum](trigger/apps/electrum.md), [Bisq 1](trigger/apps/bisq.md), or
[Wasabi](trigger/apps/wasabi.md) guide instead.

## Validate and run manually

Apply [README's Manual procedure](../README.md#manual) as this user:

```bash
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" run
vaultkeep --user --config "$job_config" status
vaultkeep --user --config "$job_config" verify
```

Stop application writers or otherwise provide a stable source first. Full validation
writes a temporary destination probe. `validate --schema-only` checks YAML structure
without proving source, mount, or automation readiness.
See [everyday operations](../README.md#check-health-and-perform-everyday-operations)
for list/prune commands, and [restore](../README.md#restore-a-backup) for extraction.

## Configure user-mode automatic backups

Use the single [Schedule](../README.md#schedule) or [Trigger](../README.md#trigger)
activation procedure, with `--user` and the same YAML path/parameters as the manual
backup. Trigger and Schedule cannot both be enabled. Before installation, full
validation reports `NOT INSTALLED` and `SETUP INCOMPLETE`; resolve other failures,
install explicitly, and validate again.

User timers and monitors depend on the owning systemd user manager. A login session
normally supplies it. To inspect operation beyond logout:

```bash
loginctl show-user "$(id -un)" --property=Linger
```

An administrator may explicitly enable lingering for an account:

```bash
sudo loginctl enable-linger alice
```

Replace `alice` with the account name. Lingering keeps the user manager available
beyond login sessions; it does not guarantee desktop notifications or completion
during host shutdown. Vaultkeep does not configure it automatically. Trigger workers
survive a monitor restart, but may be interrupted by logout/user-manager termination
or host shutdown; see [lifecycle limits](trigger/service-operation.md#lifecycle-limits).

Bulk schedule commands operate only on this user's jobs. Use
`vaultkeep --user schedules list` or preview with `vaultkeep --user schedules sync --dry-run`.
Read [bulk behavior](cli.md#manage-all-job-timers) before applying synchronization.

## Password-protected user-mode backups

Follow the [password-file setup](configuration.md#create-a-password-file) for a
user-owned file under `${XDG_CONFIG_HOME:-~/.config}/vaultkeep/secrets`, then select
`tar.7z` and `encryption.mode: password`. Use its absolute path in YAML. The regular
file must be owned by the executing user with mode exactly `0600`; parent directories
must not be group/other-writable. Keep an independent passphrase copy for restoration.
Password rotation requires a new job ID, password file, and destination namespace;
keep old credentials for old archives. See [encryption restrictions](configuration.md#password-protected-backups).

## User-mode hooks

Hooks run as the calling user through `/bin/sh -c`, with the documented fixed PATH
and context variables rather than the interactive shell's environment. Shell and path
ownership may be root or the calling user and must not be group/other-writable.
Commands/scripts invoked by the shell are not individually checked: secure them as
trusted executable configuration. Keep secrets out of command strings and arguments.
The complete [hook reference](configuration.md#lifecycle-hooks) defines placeholders,
quoting, environments, timeouts, and failure/cleanup behavior.

## User-mode limitations

User mode cannot install packages, elevate access to files/secrets/destinations,
mount filesystems without operating-system authorization, or manage root jobs/system timers.
Use root scope when protected system resources require administrator privileges.
Trigger monitoring runs in non-root user mode. Inspection is restricted to the
job execution account. Omit `application.user` in user mode.
Optional [notifications](notifications.md) target a desktop
owned by this account in the same host/container.
