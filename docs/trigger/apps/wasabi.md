# Wasabi Trigger configuration

This guide explains how to configure one Vaultkeep job to trigger after a
specific Wasabi GUI or daemon instance stops. Use one job per independently
monitored Wasabi data directory.

Keep the trigger disabled until the installed executable, directory evidence,
and all writers have been verified. The shipped
[Wasabi example](../../../examples/wasabi-trigger.yaml) is a template,
not a verified preset.

## Prerequisites

- Vaultkeep and Trigger run as the same user that owns the Wasabi process.
- The Wasabi launch exposes its data directory through `WASABI_DATADIR` or
  `--datadir`.
- The backup sources include the configured Wasabi data directory and every
  other file that Wasabi may still write during shutdown.
- The job has a destination independent from other Trigger-enabled jobs.

Trigger does not inspect processes owned by other users.

## Prepare the backup job

First have an administrator [install Vaultkeep](../../../README.md#installation).
Then run as the non-root application owner, without sudo:

```bash
vaultkeep --version
jobs_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs"
install -d -m 700 "$jobs_dir"
job_config="$jobs_dir/wasabi-main.yaml"
if [ ! -e "$job_config" ]; then
    install -m 600 /opt/vaultkeep/current/src/examples/wasabi-trigger.yaml "$job_config"
fi
nano "$job_config"
```

Keep the complete example. The filename stem must match `job.id` (`wasabi-main` here).
Replace every example path: `sources[].path` selects the files to back up,
`destination.root` is an existing writable directory, and `trigger.data_dir`
identifies the application instance. Mount and verify any network share first;
optional [destination safety checks](../../configuration.md#destination-safety-checks)
can reject an unintended local directory. Keep destinations independent between jobs.

Leave `schedule.enabled: false` and `trigger.enabled: false` while preparing and
manually testing the job. The example creates unencrypted `tar.zst` archives;
[configure password protection](../../configuration.md#password-protected-backups)
if the destination must not hold plaintext wallet data. Review retention and the
following application-specific identity requirements before enabling monitoring.

## Process identity

Add every verified GUI or daemon entrypoint for this installation to
`entrypoints`. The list uses OR semantics:

```yaml
trigger:
  enabled: false
  app: wasabi
  data_dir: /home/alice/.walletwasabi/client
  matcher:
    entrypoints:
      - kind: script
        value: /opt/wasabi/WalletWasabi
    argv_tokens: []
  poll_seconds: 2
  confirm_seconds: 5
```

For a separately packaged daemon, add its observed executable path as another
entrypoint:

```yaml
  matcher:
    entrypoints:
      - kind: script
        value: /opt/wasabi/WalletWasabi
      - kind: script
        value: /absolute/path/to/observed-wasabi-daemon
```

Replace the placeholder with the path from the installed application. Do not
assume that helper or backend processes share the GUI executable identity.

## Data-directory identity

Trigger checks the process environment for `WASABI_DATADIR` first and then the
`--datadir` command-line option. Separate option/value arguments and
`--datadir=/path` are accepted. The effective directory is canonicalized and
compared with `trigger.data_dir`.

`matcher.env_eq` can require the same directory when the environment key is
available:

```yaml
  matcher:
    entrypoints:
      - kind: script
        value: /opt/wasabi/WalletWasabi
    env_eq:
      WASABI_DATADIR: /home/alice/.walletwasabi/client
```

If the process environment is unreadable, detection fails closed rather than
assuming Wasabi stopped.

## Multiple Wasabi instances

Create a separate job for each data directory, with a unique `job.id`,
destination, and monitor. Other Wasabi processes do not keep the selected
instance running.

Do not enable two jobs for the same data directory. That creates overlapping
observers and can produce duplicate or competing backups.

## Validate and enable

1. With the application running, inspect its actual process command lines:

   ```sh
   ps -u "$(id -un)" -o pid=,args= | grep -i '[w]asabi'
   ```

   Confirm the actual GUI/daemon executable, `WASABI_DATADIR` or `--datadir`
   evidence, and related writers. Add verified daemon entrypoints when needed; do not
   assume helpers use the GUI identity.
   Review every process that can write the selected sources. Process arguments can
   contain private information; inspect locally and review before sharing output.

2. Stop the application and related writers normally. With both automation flags
   still disabled, test the complete backup as its owner:

   ```bash
   vaultkeep --user --config "$job_config" validate
   vaultkeep --user --config "$job_config" run
   vaultkeep --user --config "$job_config" verify
   ```

   Expect `Result: OK`, then `run: created` (or `run: unchanged` for an existing
   baseline), and `verify: verified`. Confirm an archive exists for the selected
   data; verification can succeed with zero archives. An unchanged run does not
   apply retention. Keep sources stable for this manual test.

3. Set `trigger.enabled: true`, keep `schedule.enabled: false`, then activate:

   ```bash
   vaultkeep --user --config "$job_config" validate
   vaultkeep --user --config "$job_config" trigger install
   vaultkeep --user --config "$job_config" status
   vaultkeep --user --config "$job_config" validate
   ```

   Before installation, `Monitor service: NOT INSTALLED` and `SETUP INCOMPLETE`
   are expected. Fix any other failed checks first. Allow the monitor's first
   observation before expecting healthy validation. It needs an active systemd user
   manager and private account-owned `XDG_RUNTIME_DIR`; installing Vaultkeep alone
   activates no monitor.

4. Start the application and confirm fresh `Application: RUNNING` in `status`.
   Close it normally and expect `CONFIRMING STOP`, then `HAS STOPPED`, a backup
   request, and a Backup result. Run `verify` again. Starting the monitor with the
   application already stopped requests nothing: running evidence must come first.

Validation checks the YAML and runtime readiness; it does not certify installed
process identity, writer coverage, clean shutdown, or application consistency.
A final offline recheck cannot prevent a writer restarting during archival. Unknown
or unreadable detection evidence blocks dispatch. Review the limitations below.

After configuration edits, validate and use `trigger update` to refresh, enable,
and restart the monitor, then validate again. `trigger disable` stops future
monitoring but leaves existing workers running and preserves recovery evidence.
Use `status --verbose` for exact units/log paths. Full lifecycle and safe switching
instructions are in [README](../../../README.md#change-or-stop-automation-safely).
For logout behavior see [user-manager lifetime](../../user-mode.md#configure-user-mode-automatic-backups).

## Limitations

- GUI and daemon packages may use different executable names.
- Auxiliary backend processes may need separate writer-coverage review.
- Environment-based directory evidence requires same-user access to the process.
- Polling can miss a shutdown and restart between samples.

See [process detection](../process-detection.md),
[complete configuration](../configuration.md), and
[consistency limits](../consistency.md).

[User guide](../../../README.md#trigger)
