# Wasabi Trigger configuration

This guide explains how to configure one VaultKeep job to trigger after a
specific Wasabi GUI or daemon instance stops. Use one job per independently
monitored Wasabi data directory.

Keep the trigger disabled until the installed executable, directory evidence,
and all writers have been verified. The shipped
[Wasabi example](../../../examples/wasabi-trigger.yaml.example) is a template,
not a verified preset.

## Prerequisites

- VaultKeep and Trigger run as the same user that owns the Wasabi process.
- The Wasabi launch exposes its data directory through `WASABI_DATADIR` or
  `--datadir`.
- The backup sources include the configured Wasabi data directory and every
  other file that Wasabi may still write during shutdown.
- The job has a destination independent from other Trigger-enabled jobs.

Trigger does not inspect processes owned by other users.

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

Inspect the actual GUI or daemon process:

```sh
ps -u "$(id -un)" -o pid=,args= | grep -i '[w]asabi'
```

Confirm the executable path and directory evidence before enabling the trigger:

```sh
vaultkeep --user --config /absolute/path/wasabi-main.yaml validate
vaultkeep --user trigger --config /absolute/path/wasabi-main.yaml check-config
vaultkeep --user trigger --config /absolute/path/wasabi-main.yaml check-config --runtime
vaultkeep --user trigger --config /absolute/path/wasabi-main.yaml install
```

`check-config` does not certify the installed GUI/daemon model or writer
coverage.

## Limitations

- GUI and daemon packages may use different executable names.
- Auxiliary backend processes may need separate writer-coverage review.
- Environment-based directory evidence requires same-user access to the process.
- Polling can miss a shutdown and restart between samples.

See [process detection](../process-detection.md),
[complete configuration](../configuration.md), and
[consistency limits](../consistency.md).

[User guide](../../../README.md#optional-trigger)
