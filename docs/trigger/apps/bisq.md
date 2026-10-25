# Bisq 1 Trigger configuration

This guide explains how to configure one Vaultkeep job to trigger after a
specific Bisq 1 instance stops. The [Bisq example](../../../examples/bisq-trigger.yaml.example)
is an illustrative template requiring local verification. The same job can recognize either a Bisq GUI or
an API daemon, provided both launches identify the same explicit data directory.

Use one job per independently monitored Bisq data directory. Bisq 2 has a
different process and configuration model and is outside Trigger's current scope.

## Prerequisites

- Vaultkeep and Trigger run as the same user that owns the Bisq process.
- Every GUI or API-daemon launch for this instance includes the same explicit
  `--appDataDir=/absolute/path` argument.
- The backup sources include that data directory and all related writers.
- The job has a destination independent from other Trigger-enabled jobs.

Trigger does not inspect processes owned by other users. If a daemon runs as
`bisquser`, run the monitor as `bisquser`.

## Prepare the backup job

First have an administrator [install Vaultkeep](../../../README.md#installation).
Then run as the non-root application owner, without sudo:

```bash
vaultkeep --version
jobs_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs"
install -d -m 700 "$jobs_dir"
job_config="$jobs_dir/bisq-main.yaml"
if [ ! -e "$job_config" ]; then
    install -m 600 /opt/vaultkeep/current/src/examples/bisq-trigger.yaml.example "$job_config"
fi
nano "$job_config"
```

Keep the complete example. The filename stem must match `job.id` (`bisq-main` here).
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

1. With the application running, inspect its actual process command lines:

   ```sh
   ps -u "$(id -un)" -o pid=,args= | grep -E '[B]isq(App|Daemon)Main|[d]aemon.jar|/opt/bisq/bin/[B]isq'
   ```

   Confirm the actual GUI/daemon entrypoint and the explicit `--appDataDir` on every
   launch form. Update `trigger.matcher.entrypoints` to include only verified identities;
   `trigger.data_dir` must match the selected canonical application directory.
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

[User guide](../../../README.md#trigger)
