# Electrum Trigger configuration

This guide explains how to configure one Vaultkeep job to trigger after a
specific Electrum instance stops. Use one job per independently monitored
Electrum data directory.

Keep the trigger disabled until the installed process model and all backup
writers have been verified. The shipped
[Electrum example](../../../examples/electrum-trigger.yaml) is a
template, not a verified preset.

## Prerequisites

- Vaultkeep and Trigger run as the same user that owns the Electrum process.
- The Electrum launch exposes its data directory through `ELECTRUMDIR`,
  `-D`, or `--dir`.
- Select the files needed for recovery. The matcher must cover every Electrum or
  related process that can still write those selected files during shutdown.
- The job has a destination that is independent from other Trigger-enabled jobs.

Trigger does not inspect processes owned by other users.

## Prepare the backup job

First have an administrator [install Vaultkeep](../../../README.md#installation).
Then run as the non-root application owner, without sudo:

```bash
vaultkeep --version
jobs_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs"
install -d -m 700 "$jobs_dir"
job_config="$jobs_dir/electrum-main.yaml"
if [ ! -e "$job_config" ]; then
    install -m 600 /opt/vaultkeep/current/src/examples/electrum-trigger.yaml "$job_config"
fi
nano "$job_config"
```

Keep the complete example. The filename stem must match `job.id` (`electrum-main` here).
Replace every example path: `sources[].path` selects the files to back up,
`destination.root` is an existing writable directory, and `application.data_dir`
identifies the application instance. Prepare and verify a supported local filesystem or CIFS/NFS share first;
optional [destination safety checks](../../configuration.md#destination-safety-checks)
can reject an unintended local directory. Keep destinations independent between jobs.

Leave `schedule.enabled: false` and `trigger.enabled: false` while preparing and
manually testing the job. The example creates unencrypted `tar.zst` archives;
[configure password protection](../../configuration.md#password-protected-backups)
if the destination must not hold plaintext wallet data. Review retention and the
following application-specific identity requirements before enabling monitoring.

## Process identity

Use the installed launcher script as the strong identity. If the process runs
through another verified entrypoint, such as a wrapper or daemon executable,
add it as another entry in `entrypoints`:

```yaml
application:
  name: electrum
  data_dir: /home/alice/.electrum
  matcher:
    entrypoints:
      - kind: script
        value: /home/alice/.local/opt/electrum/current/run_electrum

trigger:
  enabled: false
  poll_seconds: 2
  confirm_seconds: 5
```

`entrypoints` is an OR list. All other matcher fields remain required predicates.
Prefer exact script, jar, Java main-class, or argv checks. Do not use a Python
interpreter path by itself as the application identity.

The data directory is already a required predicate, so an `argv_tokens` entry for
the same directory is usually unnecessary. It can also make a separate-argument
matcher fail when Electrum uses `--dir=/path`; add such predicates only after
checking the exact installed command line.

## Data-directory identity

Trigger resolves the effective data directory in this order:

1. `ELECTRUMDIR`
2. `-D` or `--dir`

Both separate arguments and `--dir=/path` are accepted. The resolved directory
is canonicalized and compared with `application.data_dir`.

If Electrum uses its default directory without exposing one of those values,
launch it with an explicit `--dir` argument or configure `ELECTRUMDIR` for the
application process. If the directory cannot be resolved, the observation is
`ambiguous` and Vaultkeep does not trigger.

## Separate wallet backups

One Electrum data directory can contain multiple wallets. Keep one job and one
shutdown monitor for that directory, and select independent wallet archives with:

```yaml
backup_mode: per_file
sources:
  - path: /home/alice/.electrum/wallets
    archive_path_mode: prefix
    archive_prefix: wallets
    exclude:
      - "*.lock"
      - "*.tmp"
```

Keep `application.data_dir: /home/alice/.electrum`; it identifies the application
instance, while `sources` selects the wallet files. Set the destination to a new,
independent root and retain the complete job's archive, encryption, retention,
and trigger settings. Each wallet gets its own history and only new or changed
wallets receive a new archive. Newly created wallets are discovered automatically.
This backs up wallet files only; Electrum configuration and other data are outside
this job's selection.

The trigger waits for the matched application instance to stop. Closing one wallet
window while a matched Electrum process remains running does not trigger a backup.
It runs automatically without the old wrapper's confirmation prompt. The AppImage's
actual process identity still needs to be checked as described above; the example
script entrypoint is not an AppImage preset.

See [per-file operation](../../configuration.md#back-up-directory-files-separately) for
validation, destination layout, failure reporting, and history management.

## Multiple Electrum instances

Use a separate job and monitor for each instance:

```yaml
# electrum-main.yaml
application:
  name: electrum
  data_dir: /home/alice/.electrum-mainnet
  matcher:
    entrypoints:
      - kind: script
        value: /home/alice/.local/opt/electrum/current/run_electrum

trigger:
  enabled: false
```

```yaml
# electrum-test.yaml
application:
  name: electrum
  data_dir: /home/alice/.electrum-testnet
  matcher:
    entrypoints:
      - kind: script
        value: /home/alice/.local/opt/electrum/current/run_electrum

trigger:
  enabled: false
```

Give the jobs distinct `job.id` values and destinations. Other Electrum
processes do not keep the selected instance running once its data directory is
absent.

## Validate and enable

1. With the application running, inspect its actual process command lines:

   ```sh
   ps -u "$(id -un)" -o pid=,args= | grep '[r]un_electrum'
   ```

   Confirm the real launcher/daemon identity and `ELECTRUMDIR` or `-D`/`--dir`
   evidence. Adapt the inspection filter to the actual installation. A forwarded GUI
   request is not necessarily the long-lived writer; do not identify Electrum using
   the Python interpreter alone.
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
   baseline), and verification `Result: VERIFIED`. Confirm an archive exists for the selected
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

- Electrum may forward GUI or CLI requests to an existing long-lived process.
  The monitored process must be the process that owns and writes the data directory.
- Multiple loaded wallets do not necessarily represent separate processes.
- Polling can miss a very short shutdown and restart between samples.
- A GUI-to-daemon handoff shorter than `confirm_seconds` does not produce a
  backup; a longer gap is interpreted as a real shutdown.

See [process detection](../process-detection.md),
[complete configuration](../configuration.md), and
[consistency limits](../consistency.md).

[User guide](../../../README.md#trigger)
