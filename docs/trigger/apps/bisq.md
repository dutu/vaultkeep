# Bisq 1 Trigger configuration

This guide explains how to configure one VaultKeep job to trigger after a
specific Bisq 1 instance stops. The same job can recognize either a Bisq GUI or
an API daemon, provided both launches identify the same explicit data directory.

Use one job per independently monitored Bisq data directory. Bisq 2 has a
different process and configuration model and is outside Trigger's current scope.

## Prerequisites

- VaultKeep and Trigger run as the same user that owns the Bisq process.
- Every GUI or API-daemon launch for this instance includes the same explicit
  `--appDataDir=/absolute/path` argument.
- The backup sources include that data directory and all related writers.
- The job has a destination independent from other Trigger-enabled jobs.

Trigger does not inspect processes owned by other users. If a daemon runs as
`bisquser`, run the monitor as `bisquser`.

## Why the data directory is the instance identity

Bisq accepts these data-directory-related options:

- `--appDataDir`
- `--userDataDir`
- `--appName`
- `--configFile`

Trigger intentionally uses only explicit `--appDataDir` evidence. It does not
infer Bisq's default directory and does not re-read `bisq.properties`, because
the file may change after Bisq has already selected its startup directory.

The launch command must therefore contain one of:

```text
--appDataDir=/data/bisq/Bisq_Alice
--appDataDir /data/bisq/Bisq_Alice
```

`appName` is not sufficient for automatic detection because it can be omitted,
defaulted, or reused with another parent directory.

## Process identity

Bisq GUI and API daemon launches can present different process identities.
List every verified launch form in `entrypoints`; any one is accepted:

```yaml
trigger:
  enabled: false
  app: bisq
  data_dir: /data/bisq/Bisq_Alice
  matcher:
    entrypoints:
      - kind: script
        value: /opt/bisq/bin/Bisq
      - kind: java_main
        value: bisq.desktop.app.BisqAppMain
      - kind: jar
        value: /home/bisquser/bisq-daemon/daemon.jar
      - kind: java_main
        value: bisq.daemon.app.BisqDaemonMain
    argv_tokens: []
  poll_seconds: 2
  confirm_seconds: 10
```

Use only the forms that match the installed launch commands:

- Packaged GUI launcher: usually matches the `/opt/bisq/bin/Bisq` script path.
- Classpath or development GUI launch: usually exposes
  `bisq.desktop.app.BisqAppMain`.
- `java -jar .../daemon.jar`: matches the configured jar path because the main
  class appears only in the jar manifest.
- Classpath API-daemon launch: exposes `bisq.daemon.app.BisqDaemonMain`.

`entrypoints` is an OR list. Other matcher predicates apply to every accepted
identity and must also match.

## Behavior with several Bisq instances

Each job watches only its canonical `trigger.data_dir`:

- The selected instance is running if any recognized GUI or daemon process has
  that directory, even when other Bisq applications run elsewhere.
- The selected instance is stopped when no recognized process has that
  directory, even if other Bisq applications remain running.
- If both GUI and daemon processes unexpectedly use the same data directory, the
  instance remains running until both exit.

A GUI-to-daemon handoff shorter than `confirm_seconds` does not create a backup.
A longer gap is treated as a real shutdown. The example uses ten seconds to
reduce accidental triggers during a deliberate mode change.

## Multiple Bisq applications

Create a separate job for each application:

```text
bisq-sell0.yaml  -> /data/bisq/Bisq_sellXmr0
bisq-sell1.yaml  -> /data/bisq/Bisq_sellXmr1
bisq-buy.yaml    -> /data/bisq/Bisq_buyXmr
```

Each job must have a unique `job.id`, destination, source scope, and monitor.
Do not place multiple data directories in one trigger job and do not enable two
monitors for the same directory.

## Validate and enable

Inspect the real Bisq command lines:

```sh
ps -u "$(id -un)" -o pid=,args= | grep -E '[B]isq(App|Daemon)Main|[d]aemon.jar'
```

Confirm that each process contains the expected `--appDataDir`, and check which
GUI or daemon entrypoint form is actually present. Then validate:

```sh
vaultkeep --user --config /absolute/path/bisq-sell0.yaml validate
vaultkeep --user --config /absolute/path/bisq-sell0.yaml trigger install
```

`vaultkeep validate` validates the matcher structure and job but does not prove that
the installed launcher always supplies the required directory evidence.

Before installation, validation reports the missing monitor and `setup incomplete`.
Fix any prerequisite failures, install the monitor explicitly, then run the same
validation command again to check its health.

## Limitations

- Launch paths that do not expose `--appDataDir` remain `ambiguous`; they do not
  arm or dispatch automatic backups.
- A launcher rewrite, package update, jar move, or different daemon distribution
  can require matcher updates.
- Polling can miss a shutdown and restart between samples.
- Trigger does not automatically detect overlapping matchers across separate jobs.
- Bisq's shutdown and related writer behavior must still be reviewed before
  enabling automatic backup execution.

See [process detection](../process-detection.md),
[complete configuration](../configuration.md), and
[consistency limits](../consistency.md).

[User guide](../../../README.md#optional-trigger)
