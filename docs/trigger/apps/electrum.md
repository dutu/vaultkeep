# Electrum Trigger configuration

This guide explains how to configure one VaultKeep job to trigger after a
specific Electrum instance stops. Use one job per independently monitored
Electrum data directory.

Keep the trigger disabled until the installed process model and all backup
writers have been verified. The shipped
[Electrum example](../../../examples/electrum-trigger.yaml.example) is a
template, not a verified preset.

## Prerequisites

- VaultKeep and Trigger run as the same user that owns the Electrum process.
- The Electrum launch exposes its data directory through `ELECTRUMDIR`,
  `-D`, or `--dir`.
- Select the files needed for recovery. The matcher must cover every Electrum or
  related process that can still write those selected files during shutdown.
- The job has a destination that is independent from other Trigger-enabled jobs.

Trigger does not inspect processes owned by other users.

## Process identity

Use the installed launcher script as the strong identity. If the process runs
through another verified entrypoint, such as a wrapper or daemon executable,
add it as another entry in `entrypoints`:

```yaml
trigger:
  enabled: false
  app: electrum
  data_dir: /home/alice/.electrum
  matcher:
    entrypoints:
      - kind: script
        value: /home/alice/.local/opt/electrum/current/run_electrum
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
is canonicalized and compared with `trigger.data_dir`.

If Electrum uses its default directory without exposing one of those values,
launch it with an explicit `--dir` argument or configure `ELECTRUMDIR` for the
application process. If the directory cannot be resolved, the observation is
`ambiguous` and VaultKeep does not trigger.

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

Keep `trigger.data_dir: /home/alice/.electrum`; it identifies the application
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

See [per-file operation](../../../README.md#back-up-directory-files-separately) for
validation, destination layout, failure reporting, and history management.

## Multiple Electrum instances

Use a separate job and monitor for each instance:

```yaml
# electrum-main.yaml
trigger:
  app: electrum
  data_dir: /home/alice/.electrum-mainnet
  matcher:
    entrypoints:
      - kind: script
        value: /home/alice/.local/opt/electrum/current/run_electrum
```

```yaml
# electrum-test.yaml
trigger:
  app: electrum
  data_dir: /home/alice/.electrum-testnet
  matcher:
    entrypoints:
      - kind: script
        value: /home/alice/.local/opt/electrum/current/run_electrum
```

Give the jobs distinct `job.id` values and destinations. Other Electrum
processes do not keep the selected instance running once its data directory is
absent.

## Validate and enable

Inspect the installed process before enabling the trigger:

```sh
ps -u "$(id -un)" -o pid=,args= | grep '[r]un_electrum'
```

Confirm that the launcher path, data-directory argument, and all writers match
the job. Then validate and install the monitor:

```sh
vaultkeep --user --config /absolute/path/electrum-main.yaml validate
vaultkeep --user trigger --config /absolute/path/electrum-main.yaml check-config
vaultkeep --user trigger --config /absolute/path/electrum-main.yaml check-config --runtime
vaultkeep --user trigger --config /absolute/path/electrum-main.yaml install
```

`check-config` validates the selected matcher and job but does not certify
Electrum's installed process model, writer coverage, or backup consistency.

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

[User guide](../../../README.md#optional-trigger)
