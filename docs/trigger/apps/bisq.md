# Bisq 1 Trigger setup

Configure one Vaultkeep job to back up a specific Bisq 1 data directory after
its application stops. A job can recognize the Bisq GUI, the API daemon, or
both, provided their launches identify the same explicit data directory.

The [Bisq job example](../../../examples/bisq-trigger.yaml) is a
starting template, not a verified configuration for your installation. Bisq 2
uses a different process and configuration model and is not supported by this
guide.

## Setup

Follow these steps in order. Run Vaultkeep as the **non-root user who owns the
Bisq process**, without `sudo`. For example, if the daemon runs as `bisquser`,
log in as `bisquser` to configure and run its backup job. Trigger cannot inspect
processes owned by another user.

### 1. Check prerequisites

- Have an administrator [install Vaultkeep](../../../README.md#installation).
- Use one Vaultkeep job and monitor for each independently monitored Bisq data
  directory. Different jobs need independent source scopes and destinations.
- Ensure the application can shut down cleanly and identify **all processes
  that can write to the selected files**. A stopped GUI alone is not enough if
  another writer remains active.
- Ensure the backup destination is accessible to the Bisq owner. Mount network
  shares through the operating system before running Vaultkeep.

Confirm that Vaultkeep is installed:

```bash
vaultkeep --version
```

### 2. Identify the Bisq instance and its launch commands

Start the Bisq GUI or API daemon you intend to monitor. As the application
owner, inspect its actual process command line:

```bash
ps -u "$(id -un)" -o pid=,args= | grep -E '[B]isq(App|Daemon)Main|[d]aemon.jar|/opt/bisq/bin/[B]isq'
```

Record both:

- The **canonical data directory** used by that instance.
- The actual **GUI or daemon entrypoint**, including any additional launch
  forms you intend the monitor to recognize.

Every monitored launch must explicitly include the selected directory in one
of these forms:

```text
--appDataDir=/data/bisq/Bisq_Alice
--appDataDir /data/bisq/Bisq_Alice
```

Replace the example directory with the real absolute path. If a launcher does
not pass `--appDataDir`, correct the launcher and restart Bisq before continuing;
Trigger will not infer the directory from other Bisq options or defaults.
Check each GUI and API-daemon launch form you intend to use, and identify other
writers of the same files. Process command lines can reveal private information:
review output locally before sharing it.

See [data-directory identity](#data-directory-identity) and
[entrypoint matching](#gui-and-api-daemon-entrypoints) for details.

### 3. Create a job from the example

Create a user-owned job without overwriting an existing file:

```bash
jobs_dir="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs"
install -d -m 700 "$jobs_dir"
job_config="$jobs_dir/bisq-main.yaml"
if [ ! -e "$job_config" ]; then
    install -m 600 /opt/vaultkeep/current/src/examples/bisq-trigger.yaml "$job_config"
fi
nano "$job_config"
```

Use the complete example. Set `job.id` to the YAML filename stem (`bisq-main`
in this example), and retain its other required settings.

### 4. Configure the sources, destination, and process matcher

Edit the job YAML:

1. Set `sources[].path` to the Bisq data you need to back up. Include the
   selected data directory and account for all processes that can change the
   selected files.
2. Set `destination.root` to an **existing**, writable backup directory. Verify
   any CIFS/NFS mount before use. The optional
   [`marker_file` and `expected_fstype` checks](../../configuration.md#destination-safety-checks)
   help prevent writing to an unintended local directory.
3. Set `trigger.data_dir` to the **same canonical directory** confirmed in step 2.
4. Set `trigger.matcher.entrypoints` to **only the launch forms actually
   verified** in step 2. Keep the appropriate entries for the GUI, daemon, or
   both; remove entries that do not apply to your installation.
5. Review retention and archive protection. The example creates **unencrypted**
   `tar.zst` files; [enable password protection](../../configuration.md#password-protected-backups)
   when the destination must not contain plaintext wallet data.
6. Leave both `schedule.enabled: false` and `trigger.enabled: false` for the
   initial manual test.

For example, this **Trigger fragment** shows possible GUI and API-daemon
entrypoints. It is not a complete job and must be adapted to the installed
processes:

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

Do not enable two Trigger monitors for the same directory. For multiple Bisq
instances, see [multiple-instance behavior](#multiple-bisq-instances).

### 5. Validate and test a manual backup

Stop Bisq and **all related writers** normally so the selected source is stable.
With both automation flags still disabled, run:

```bash
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" run
vaultkeep --user --config "$job_config" verify
```

Expect `Result: OK`, then `run: created` for a new baseline (or
`run: unchanged` if the sources already match a successful backup), and
verification `Result: VERIFIED`. **Confirm that the expected backup archive
actually exists at the destination**: verification can succeed even if there are zero archives.
An unchanged run neither creates a new archive nor applies retention.

Do not enable automatic backups until this manual test succeeds with the
intended Bisq data.

### 6. Enable and install Trigger

Change the YAML to `trigger.enabled: true` and keep
`schedule.enabled: false`. Then run:

```bash
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" trigger install
vaultkeep --user --config "$job_config" status
vaultkeep --user --config "$job_config" validate
```

The first validation can report `Monitor service: NOT INSTALLED` and
`SETUP INCOMPLETE`; this is expected before installation. Resolve any other
failed prerequisites before installing. Trigger requires an active systemd
user manager and a private, account-owned `XDG_RUNTIME_DIR`. After installation,
allow the monitor to record its first observation before expecting a healthy
validation result.

### 7. Test backup after shutdown

1. Start the selected Bisq instance.
2. Run `vaultkeep --user --config "$job_config" status` and confirm it shows
   **fresh** `Application: RUNNING` evidence for that instance.
3. Close Bisq normally, ensuring all related writers stop. Expect
   `CONFIRMING STOP`, then `HAS STOPPED`, a backup request, and a Backup result.
4. Run `vaultkeep --user --config "$job_config" verify` and confirm the
   expected backup exists at the destination.

A monitor that starts while Bisq is already stopped does not request a backup.
It must first observe the application running. A healthy configuration check
does not prove that every writer was identified or that the application shut
down consistently; see [limitations](#limitations-and-consistency).

### 8. Update or disable monitoring

After changing the YAML or a Bisq launch command, check the configuration and
refresh the installed monitor **when you intend monitoring to remain enabled**:

```bash
vaultkeep --user --config "$job_config" validate
vaultkeep --user --config "$job_config" trigger update
vaultkeep --user --config "$job_config" validate
```

`trigger update` enables and restarts monitoring. To stop future automatic
starts **instead**, use:

```bash
vaultkeep --user --config "$job_config" trigger disable
```

Disabling monitoring does not terminate running backup workers or erase
recovery evidence. Use `status --verbose` for exact service names and log paths.
See [automation lifecycle](../../../README.md#change-or-stop-automation-safely)
for safe switching, and [user-manager lifetime](../../user-mode.md#configure-user-mode-automatic-backups)
for operation across logout.

## Reference and explanations

### Data-directory identity

Bisq accepts several directory-related options:

- `--appDataDir`
- `--userDataDir`
- `--appName`
- `--configFile`

Trigger intentionally recognizes the instance **only through explicit
`--appDataDir` evidence**. It does not infer the default directory or re-read
`bisq.properties`, which might have changed since the application started.
`appName` alone is insufficient because it can be omitted, defaulted, or reused
with a different parent directory. An unidentifiable launch remains
`ambiguous` and does not arm or dispatch a backup.

### GUI and API-daemon entrypoints

The GUI and API daemon may expose different process identities:

| Launch form | Matching entrypoint |
| --- | --- |
| Packaged GUI script | `kind: script`, such as `/opt/bisq/bin/Bisq` |
| GUI classpath/development launch | `kind: java_main`, `bisq.desktop.app.BisqAppMain` |
| `java -jar .../daemon.jar` | `kind: jar`, with the verified daemon JAR path |
| API daemon classpath launch | `kind: java_main`, `bisq.daemon.app.BisqDaemonMain` |

For `java -jar`, the main class may appear only in the JAR manifest, so the
matcher uses the verified JAR path instead. `entrypoints` is an **OR list**:
any listed entrypoint may match, but all other matcher predicates must also
match. An update to the launcher, installation path, JAR, or daemon distribution
may require editing the matcher and running `trigger update`.

### Multiple Bisq instances

Each job watches only its canonical `trigger.data_dir`. For example:

```text
bisq-sell0.yaml  -> /data/bisq/Bisq_sellXmr0
bisq-sell1.yaml  -> /data/bisq/Bisq_sellXmr1
bisq-buy.yaml    -> /data/bisq/Bisq_buyXmr
```

Each job requires its own `job.id`, source scope, destination, and monitor. Do
not combine several instance directories into one Trigger job or configure two
monitors for the same directory. Trigger does not automatically detect
matcher overlap between separate jobs.

For the selected job:

- It is **running** while any recognized GUI or daemon process uses the selected
  directory, even if other instances are also running.
- It is **stopped** when no recognized process uses that directory, even if other
  Bisq instances remain running.
- If both a GUI and daemon use the same directory, it remains running until
  both processes exit.

### Shutdown confirmation and handoffs

The example uses `poll_seconds: 2` and `confirm_seconds: 10`. A GUI-to-daemon
handoff shorter than the confirmation period does not cause a backup; a longer
gap is considered a shutdown. This reduces accidental triggers during mode
changes but cannot eliminate them.

Polling can miss a shutdown followed by a restart between samples. Unknown or
unreadable process evidence blocks dispatch. A final offline check cannot
prevent a writer from restarting after the check.

### Limitations and consistency

- Detecting process disappearance does **not** prove a clean Bisq shutdown.
- Configuration validation does not certify process identity, complete writer
  coverage, safe source consistency, or absence of overlapping matchers.
- Bisq's shutdown and related-writer behavior must be reviewed before relying
  on automatic backup execution.
- Backup archive verification checks archive integrity, not the application's
  internal data consistency.

For the underlying rules, see [process detection](../process-detection.md),
[Trigger configuration](../configuration.md), and
[consistency limits](../consistency.md).

[Back to the Vaultkeep Trigger guide](../../../README.md#trigger)
