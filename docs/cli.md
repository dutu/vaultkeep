# CLI reference

Use the [README](../README.md) for installation and complete Manual, Schedule, and
Trigger setup procedures. This reference defines commands and their operational
behavior. Scope-specific permissions and paths are in the [root](root-mode.md) and
[user](user-mode.md) guides; settings are in [configuration](configuration.md).

Put global options before the command name: `--user`, `--config`, and any repeated
`--param KEY=VALUE` selections, plus `--no-color` when needed. `--config` is required
for operations on one job.
Action-specific options, such as `status --json`, follow the action.

```text
vaultkeep [-h|--help]
vaultkeep --version
vaultkeep [--user] [--config <job.yaml>] notify [external arguments ...]

vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] validate [--schema-only]
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] run
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] status [--json] [--verbose]
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] list
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] verify [--verbose]
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] prune [--dry-run]

vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] schedule install
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] schedule update
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] schedule next
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] schedule disable
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] schedule remove

vaultkeep --user --config <job.yaml> [--param KEY=VALUE ...] trigger install
vaultkeep --user --config <job.yaml> [--param KEY=VALUE ...] trigger update
vaultkeep --user --config <job.yaml> [--param KEY=VALUE ...] trigger disable
vaultkeep --user --config <job.yaml> [--param KEY=VALUE ...] trigger remove
vaultkeep --user --config <job.yaml> [--param KEY=VALUE ...] trigger monitor

vaultkeep [--user] schedules list
vaultkeep [--user] schedules validate
vaultkeep [--user] schedules sync [--dry-run]
```

Job management commands and actions support `-h` or `--help` in their own position, for example
`vaultkeep schedule --help`, `vaultkeep schedule install --help`, or
`vaultkeep status --help`. Version inspection uses `vaultkeep --version`.

Use `--user` for per-user job configuration, per-user state, and systemd user timers.
Omit it for root mode. Root-mode backup execution, full validation, inventory,
verification, pruning, and timer management require root privileges; use `sudo`.

## Contents

- [Terminal output](#terminal-output)
- [Validation](#validate-a-job-configuration)
- [Run](#run-a-job-manually)
- [List](#list-job-backups)
- [Verify](#verify-job-backups)
- [Prune](#prune-job-backups)
- [One job timer](#configure-a-job-timer)
- [All job timers](#manage-all-job-timers)
- [Trigger commands](#trigger-commands)
- [Job status](#check-job-status)
- [External notifications](#external-notifications)
- [Core exit codes](#exit-codes)
- [Trigger exit codes](#trigger-exit-codes)

## External notifications

```sh
vaultkeep --user --config ~/.config/vaultkeep/jobs/app.yaml notify --level info "Backup completed"
```

`notify` synchronously invokes the executable in the selected job's
`external_notifier.command`. It does not generate notifications automatically.
Everything after `notify` is passed unchanged, in order, as subprocess arguments;
Vaultkeep interprets no provider options, positional arguments, or `--` separators.
For example, `--level`, `--timeout`, and `--help` after `notify` belong to the provider.
Use `vaultkeep --help` for Vaultkeep help. `notify-telegram` is no longer a command.

Put Vaultkeep options before `notify`. An explicit `--config` overrides
`VAULTKEEP_CONFIG`; otherwise the helper uses that environment variable, supplied
by lifecycle hooks. Manual user-mode calls use `--user`. A hook running as a
non-root account automatically uses that account's scope when `VAULTKEEP_CONFIG`
is present. The helper never changes execution identity. Root-mode manual calls
require root as usual.

Use an absolute job-config path when starting a backup whose hooks use `notify`;
hooks execute from `/` and preserve the supplied `VAULTKEEP_CONFIG` path.

The existing YAML loader and template validation check the job without resolving
application accounts, reading sources or archive passwords, checking destination
mounts, acquiring destination locks, or writing backup state. Missing runtime
parameter values do not prevent notification-only use. `--param KEY=VALUE` retains
normal validation when supplied before `notify`.

| Result | Exit code |
| --- | --- |
| External command succeeds or has an expected delivery failure | `0` |
| Invalid Vaultkeep CLI arguments | `2` |
| Invalid/missing configuration or missing `external_notifier` | `3` |

Exit code 0 does not guarantee remote delivery. The helper invokes the provider
once without a shell, using its independent configured timeout (15 seconds by
default). It terminates the process group on timeout, with bounded escalation.
Provider argument errors are provider nonzero exits and therefore best-effort
delivery failures; Vaultkeep cannot validate an arbitrary executable's arguments.
Unexpected programming errors are not converted to success.

Warnings contain bounded execution errors, exit statuses, or timeout diagnostics,
plus hook context when available. Notification arguments and provider stdout/stderr
are not logged because they may contain credentials. Direct calls warn on stderr;
hooks also attempt a bounded system-log warning through `logger`, since hook output
is normally hidden. There are no retries, background workers, or delivery records.
See [external notification setup](configuration.md#external-notifications) for
configuration, hook dependencies, timeout budgets, and logging.

## Terminal output

Pressing Ctrl+C interrupts an operation without a Python traceback. The CLI reports
the interruption on stderr and returns exit code 130.
`trigger monitor` keeps its graceful signal handling described below.

Human-readable reports use short sections and aligned fields. Validation puts the
overall result first and separates configuration checks, backup history, and enabled
automation. For example:

```text
Vaultkeep — Validation

Job:     electrum-main
Mode:    user
Result:  SETUP INCOMPLETE

Configuration
  Configuration  VALID
  Backup paths   OK

Backup
  History  no completed run recorded

Trigger monitoring
  Trigger                        ENABLED
  Trigger runtime prerequisites  OK
  Monitor service                NOT INSTALLED
  Monitor updates                no recorded state

Issue: Monitor service is not installed; automatic backups are unavailable.
Next: vaultkeep --user --config /path/to/job.yaml trigger install
```

Reports remain visible at every `logging.level`. Set the job's `logging.level` to
`warning` or `error` for concise reports, `info` for additional operational diagnostics,
or `debug` for internal checks. Diagnostic messages go to stderr with their severity
(`DEBUG`, `INFO`, `WARNING`, `ERROR`); report fields have no logging prefixes. Full
systemd unit names and detailed prerequisite information appear in diagnostics.
Actionable failures and warnings remain visible even at `logging.level: error`.
`trigger monitor` continues to log its ongoing operational events at the configured
level. These settings do not change command results or exit codes.

Hook output is omitted by default. See [logging settings](configuration.md#logging)
for opt-in output capture, size limits, and secret-handling precautions.

Colors highlight status values: green for success, yellow for warnings or incomplete
setup, red for failure, and cyan for running stages. Headings, paths, timestamps,
and ordinary values use the default terminal color. Redirected or captured output
and `TERM=dumb` use plain text. Disable colors with `--no-color`, or set a nonempty
`NO_COLOR` environment variable:

```bash
sudo ./install.sh install --no-color
sudo ./install.sh update --no-color
vaultkeep --no-color --user --config /path/to/job.yaml validate
vaultkeep --no-color --user --config /path/to/job.yaml trigger install
```

The installer shows each stage as `RUNNING...` immediately, with elapsed seconds,
then `OK` or `FAILED`. Capable interactive terminals refresh the current line;
redirected output keeps ordinary stage lines, including activity updates every ten
seconds. Completed stages stay in order. `--no-color` disables colors while retaining
interactive line updates. Routine apt/pip output goes into the complete diagnostic
transcript, whose path is printed at the start and end. A failed installation names
the stage and exit code, shows relevant installer or configuration errors, and retains
the transcript after rollback. It does not replay raw tool output or output from
successful stages. If no structured error is available, inspect the transcript for
the failed operation's stdout and stderr, including dependency installation errors.
The transcript is a private file in the temporary directory (normally
`/tmp/vaultkeep-install.*.log`); inspect it before temporary-file cleanup or reboot,
and remove it when no longer needed. `--dry-run` continues to show detailed planned
commands without creating a transcript or changing the deployment.

Validation reports remain on stdout, with detailed errors on stderr. Version output,
Job status JSON, schedule listings, and backup result markers such as
`run: created` remain suitable for scripts. Use `status --json` for backup activity
and results, with separate Trigger and Schedule information when configured.

## Validate a job configuration

```text
vaultkeep [--user] --config <job.yaml> validate --schema-only
vaultkeep [--user] --config <job.yaml> validate
```

Use complete validation before running a backup or installing automation, and later
as a health check. It validates the whole job and checks the runtime prerequisites,
installation, and health of its enabled Trigger or Schedule in the selected user or
system mode. Trigger requires user mode and a non-root application owner.

The report shows `Trigger` or `Schedule` as `ENABLED`, or both as `DISABLED`. It includes
backup source/destination checks and any recorded last-run failure. For Trigger it
checks the monitor service, recent monitor updates, the loaded configuration, process
observations, and recovery warnings. For Schedule it checks the timer, next scheduled
run, and backup service result. Missing backup history alone is not a failure.

Before the job's automation is installed, expect `NOT INSTALLED` and
`Result: SETUP INCOMPLETE`; the other available checks still run. Correct failed
prerequisites, then use `trigger install` or `schedule install` explicitly. Re-run
validation after installation. A missing, disabled, stopped, or unhealthy monitor/timer
is reported; validation never installs, starts, restarts, or repairs services or state.
A health report describes the state observed during the check, not future execution.
Complete validation exits zero only when all requested checks pass. Missing installation
and automation health failures exit nonzero (normally 3); backup destination/source and
state errors retain their specific [exit codes](#exit-codes). After starting
a new monitor, allow it to record its first observation before expecting a healthy result.

A `Configuration` value of `VALID` means the configuration passed its checks. Overall `OK` means
all applicable backup and automation checks passed. `SETUP INCOMPLETE` identifies
missing automation installation; other reported failures still need attention.
`FAILED` means a required validation or health check failed when no missing-installation
condition takes precedence. Read all issues even when `SETUP INCOMPLETE` is displayed. Missing monitor history
is shown as context when the monitor has not been installed, rather than as a second
independent error. These outcomes preserve the existing exit codes.

Use `--schema-only` for YAML structure checks on a machine without the job's source,
destination, or systemd environment. Its output explicitly says runtime checks were
skipped; its `VALID` result applies only to configuration structure and does not
establish readiness or health.

Complete validation creates a temporary directory and file in the destination, checks
the same atomic directory rename used to finalize backups, then removes the probe.
Backup runs and destructive pruning perform this check before creating archives or
deleting backups. Failures identify the operation and path; if cleanup is denied,
remove the reported probe after correcting destination permissions. Schema-only
validation does not access the destination.

Run validation as the account that runs the backup, using `--user` for user-mode jobs.
Successful validation confirms access at that moment; permissions and mount conditions
can change afterward.

## Progress display

`run` and `verify` report progress on stderr. On an interactive terminal,
they show an animated spinner, elapsed time, and the current stage. `run`
keeps each completed stage listed with a tick, animating only the current stage.
Terminals without Unicode support use `[OK]` for completed stages. Failed or
interrupted stages receive no tick. `verify` uses a compact panel with
completed phase summaries and replaces it with a final results table on stdout.
The status fits the terminal width and uses an ASCII spinner when the terminal encoding cannot display Unicode.
Elapsed time is for the whole operation. Percentages appear only when measured byte
totals are available; stage names do not imply an overall completion percentage.

When stderr is redirected, piped, or captured, or `TERM` is unset, empty, or `dumb`,
progress uses plain timestamped lines with immediate stage changes and a
`still running` heartbeat every 10 seconds during long stages. This also applies
to systemd, cron, and SSH sessions without a terminal; SSH sessions with a suitable
terminal use the spinner. `--no-color` and `NO_COLOR` disable colors but allow
animation on suitable terminals. `verify` also requires stdout to be a terminal
for animation; redirecting stdout disables its ANSI output. Final command results
remain on stdout; verification uses the results table instead of a result marker.
`list` prints its completed inventory directly on stdout without progress timestamps.

## Run a job manually

```text
vaultkeep [--user] --config <job.yaml> run
```

`run` reports source hashing, compression, verification, and retention stages using
the shared [progress display](#progress-display). Every run, including scheduled
execution, also publishes progress for `vaultkeep status`. The final result remains
on stdout as `run: created`, `run: unchanged`, or `run: failed`.
After `run: created`, the terminal also shows the archive path, archive size,
total run duration, and number of backups removed by retention. This summary
uses stderr and is omitted when stderr is redirected or captured.

Possible successful results:

- `created`: a new backup was finalized;
- `unchanged`: the current source and backup-relevant configuration match the last successful backup.

An unchanged run does not create an archive or apply retention.
Change detection validates backup metadata and compares the sources against the
newest matching backup, ordered by manifest UTC creation time and then backup ID.
Before returning `unchanged`, it reads only that archive to verify its SHA-256
checksum. A content checksum mismatch marks the backup SUSPECT and creates a
fresh verified backup, reporting `created` on success. It does not skip creation
based on an older backup after detecting corruption. Permission and read errors still
fail the run, without marking the archive as corrupt.

A demonstrated checksum or archive-structure problem records a
`.vaultkeep-suspect` marker with its original reason and detection time. `list`
shows the `SUSPECT` reason directly below the backup directory. An unresolved
suspect is protected and forces a fresh archive in the same compatible history.
After archive verification, required state persistence, and successful hooks, the engine
records the replacement ID and may remove the superseded suspect. Suspects do not
fill ordinary retention slots. Ordinary cleanup failures preserve the new archive and report
`run: created` with an actionable warning; retry with explicit `prune` or a later
archive-creating run. `UNCHANGED` never performs cleanup.
Corruption found in the newly committed archive fails the run, including when
discovered during cleanup.

Replacement compatibility requires the same configured source coverage and
consistency policy; offline backups also require the same application name, owner,
and data directory. Changing compression, retention, archive format, or password-file
location does not define a different replacement class. Changed coverage or policy
preserves the incompatible history. A missing or suspect replacement makes the old
suspect unresolved again. Deletion rechecks the replacement's current SHA-256
against both the immutable manifest and sidecar under the shared destination lock.
`list`, `status`, and `prune --dry-run` inspect metadata without reading archives or
writing markers. `verify` performs full archive verification and may mark demonstrated
corruption; wrong passwords, unavailable tools, and transient I/O errors do not by
themselves establish corruption.

Temporary backups use `~$.partial-vaultkeep-<job>-<backup-id>` directory names.
On destinations managed by synchronization software, verify that temporary
directories are excluded and completed backups are uploaded.

If a run fails before the final directory rename, Vaultkeep removes that attempt's
`~$.partial-vaultkeep-*` directory. If cleanup also fails, a warning identifies the
remaining path and the original backup error is still reported. Committed backups
are preserved if a later operation fails. An abrupt termination, such as a power
loss or forced kill, can leave partial directories for manual cleanup once no
backup is using them; later runs do not remove older partial directories.

## List job backups

```text
vaultkeep [--user] --config <job.yaml> list
```

`list` prints a `Vaultkeep — Backups` inventory with the job ID, destination,
and configured retention counts. Rows show `Created (UTC)`, compressed archive
`Size`, `Decision`, and `Tiers`, newest first. Creation
facts come from stored manifests, including backups in either
supported format after an archive-setting change. Each row is followed by
`  Backup: <directory>` with the complete directory path relative to the destination.
Suspects show their original reason immediately below their directory. Unresolved
suspects show `PROTECTED`; a metadata-valid replacement permits a `PRUNE` preview.
An unmarked archive has no health label; marker absence is not full verification.
For `per_file` jobs, this includes the file history directory.

Retention decisions reuse the existing pruning planner, calculated independently
for each file history:

- `KEEP`: selected by at least one retention tier. `Tiers` lists every tier
  that actually selects this backup, rather than all calendar buckets it belongs to.
- `PRUNE`: eligible for removal under the current policy and inventory. Listing
  never deletes anything; pruning performs its own checks before deletion.
- `PROTECTED`: an unresolved suspect; no reliable compatible replacement is recorded.
- `—`: retention is disabled (except for unresolved suspects), or the history is
  incompatible with this configuration.
- `UNKNOWN`: a reliable plan cannot be established, for example because matching
  invalid entries or duplicate backup IDs make that history incomplete.

Selections use the manifest's recorded local calendar time, while the table shows
UTC. They are recalculated when the available backups or current policy change.
The final summary gives total backups and Keep/Prune counts, plus Unknown when
needed. An empty inventory says `No backups found.`

All-zero retention counts are shown as `Retention: disabled`, with `—` in `Tiers`
and no Keep/Prune/Unknown counts. `Decision` is `—` except for unresolved suspects,
which remain `PROTECTED`. This is supported for read-only inspection with `list`;
validation, `run`, and `prune` still require an enabled tier.

Missing or invalid metadata and archive layouts appear under `Warnings`, with
available diagnostics and a separate `Invalid entries` count. Invalid entries are
excluded from inventory rows. Affected histories have `UNKNOWN` decisions; other
file histories retain their independently calculated selections. Matching invalid
entries continue to block actual pruning. A missing required encryption field is
invalid, rather than inferred from the current job configuration. Unrelated entries
and partial backup directories are ignored.

The compact table uses the same formatting and narrow-terminal fallback as `verify`.
Backup directory lines do not widen the columns and wrap naturally on terminals.
Names are never truncated; control characters and characters unsupported by the
output encoding are escaped. There are no additional options, elapsed-time messages,
per-backup archive paths, or verbose metadata sections.

Listing reads manifests and filesystem metadata, requires the archive and checksum
sidecar to exist as regular files, and checks the expected directory layout. It
does not read archive or checksum sidecar contents, calculate SHA-256, load encryption
passwords, require original sources or a live application account, or modify backup
or local state. Current-history retention previews can be incomplete when application
identity is unavailable. **Being listed
does not establish archive integrity**; use `verify` to check contents and checksums.
A completed inventory exits zero, including empty inventories and incomplete previews
with warnings. Configuration and destination failures retain the
[core exit codes](#exit-codes).

## Verify job backups

```text
vaultkeep [--user] --config <job.yaml> verify [--verbose]
```

Verification checks:

- the backup manifest and filenames;
- the backup directory layout;
- the SHA-256 checksum against the sidecar and manifest;
- the complete Zstandard stream and TAR structure for `.tar.zst`;
- the configured password, complete 7-Zip archive, encrypted headers, and single
  inner `{job}.tar` member for `.tar.7z`;
- TAR member paths, rejecting duplicate, absolute, and parent-traversal paths.

Verification works without the original sources, application installation, processes,
or application account. It records a suspect marker when
checksum or structural checks demonstrate corruption, without rewriting archive
contents, updating local backup state, or running retention. Encrypted jobs require a password file containing the archive's
passphrase, with the same ownership and permissions required by `run`.
Suspects retain their marker and are reported as failures. Verification checks their
archive contents too; successful verification alone never clears a suspect marker. Verification continues checking the remaining backups and exits nonzero
while marked backups remain in the destination. A successful compatible replacement
can make a suspect eligible for guarded cleanup, including its marker.

Verification continues after individual backup failures. Invalid backups are excluded
from archive verification. Failures are reported immediately with their full paths
and reasons; exit codes are unchanged.

The shared [progress display](#progress-display) shows two phases:

1. **Manifests & SHA-256** validates backup directories, manifests, and compressed
   archive checksums against both the sidecar and manifest.
2. **Archive integrity** checks the compressed stream and TAR members, including
   path safety. Encrypted archives also undergo password, header, and member checks.

The active operation shows the backup timestamp, index/total, and elapsed time.
SHA-256 displays bytes read and percentage when the compressed size is known,
using the existing checksum read. Archive operations have no byte percentages.
Successful checks update the display without adding permanent lines. Noninteractive
logs print concise phase transitions and coalesce updates into 10-second heartbeats.
For `per_file` jobs, both phases run for one locked child history at a time;
indices and phase counts apply to that history. The final table and verified count
include all histories. A busy child is reported as a failure while other histories
continue.

The final stdout table reports `Manifest`, `SHA-256`, and `Archive` separately:

```text
Backup            Manifest  SHA-256  Archive
────────────────────────────────────────────
2026-10-22 23:57  OK        OK       OK
2026-10-23 00:50  OK        OK       OK
────────────────────────────────────────────
Verified:  2/2 backups
Duration:  04:22
Result:    VERIFIED
```

Labels use validated manifest creation times in their recorded local time zone,
displayed to the minute. Backups sharing a displayed timestamp receive a
short distinguishing identifier. Entries without validated metadata use `Unknown #N`;
their full paths remain in failure diagnostics. Narrow terminals use stacked results
when the table cannot fit.

`OK` means the check completed successfully. `FAILED` identifies a failed check;
`SKIPPED` means an earlier failure made it ineligible. `NOT CHECKED` marks checks
that did not finish, including unvisited backups after interruption. A successful
compressed stream test alone does not make the combined `Archive` result `OK`.
`Verified` counts backups that passed all three checks. Unknown discovery totals are
identified explicitly, and an empty destination shows `0/0`. `Result` is `VERIFIED`
only when verification succeeds; failures and interruptions show `FAILED` with
accurate partial results. These checks do not restore files or compare their contents
with a live filesystem.

Use detailed output when troubleshooting:

```sh
vaultkeep --user --config /absolute/path/job.yaml verify --verbose
```

Verbose output includes full backup/archive paths, individual operations, and
checksum diagnostics. Normal output omits successful per-backup messages and the
former `verify: verified` and `SUCCESS: Verification complete` lines.

## Prune job backups

Check the retention plan without deleting:

```text
vaultkeep [--user] --config <job.yaml> prune --dry-run
```

Apply:

```text
vaultkeep [--user] --config <job.yaml> prune
```

The [retention policy](configuration.md#retention) controls which backups are kept.
Pruning does not require the original sources. `--dry-run` validates the retention
plan and prints `prune: planned`; the current CLI does not print individual keep/delete
paths. Applying it prints `prune: pruned` and probes destination writability before
deletion. Matching malformed backups block plan calculation in both dry-run and
apply modes; use `verify` to identify them. Each independent per-file history is
planned and validated separately. A failed file does not prevent safe cleanup of
another file. Unresolved suspects are protected; superseded suspects can be removed
after validating the replacement and its current checksum. Retention preserves
replacement dependencies even if a suspect deletion fails. Malformed markers and
unexpected artifacts block destructive work in their affected history. Prune does
not add a decompression pass; use `verify` for full stream verification.

## Configure a job timer

Vaultkeep uses one systemd timer instance per job. Root mode manages system timers. User mode manages systemd user timers.

```text
<job.yaml>
-> vaultkeep@app.timer
-> vaultkeep@app.service
```

Parameterized jobs derive the timer instance from the job ID and sorted runtime parameters:

```text
<job.yaml> + --param repo=nas-a
-> vaultkeep@app--repo-nas-a--<hash>.timer
-> vaultkeep@app--repo-nas-a--<hash>.service
```

The generated service override stores the exact `vaultkeep --config ... --param ... run` command, so manual and scheduled runs use the same effective configuration.

Use [README's Schedule procedure](../README.md#schedule) to configure and activate
a timer. `schedule install` requires `schedule.enabled: true` and enables/starts it.
Schedule [settings and calendar examples](configuration.md#schedule-settings) belong
to the configuration reference.

For a parameterized job, pass the same parameters to every timer command:

```text
vaultkeep [--user] --config <job.yaml> --param repo=nas-a schedule install
vaultkeep [--user] --config <job.yaml> --param repo=nas-a status
vaultkeep [--user] --config <job.yaml> --param repo=nas-a schedule remove
```

Update the timer after changing the job schedule:

```text
vaultkeep [--user] --config <job.yaml> schedule update
```

Install and update validate the job's execution scope and application identity,
then the generated calendar and staged service/timer units
before replacing the managed files. Update preserves timer enablement and restarts
an enabled timer. If applying either command fails or is interrupted with Ctrl-C,
Vaultkeep restores the preceding managed files, ownership registry, and timer
enablement/activity. Restoration errors are reported as an incomplete rollback.

Inspect the timer:

```text
vaultkeep [--user] --config <job.yaml> status
vaultkeep [--user] --config <job.yaml> schedule next
```

Stop the timer without removing its Vaultkeep-managed systemd schedule file:

```text
vaultkeep [--user] --config <job.yaml> schedule disable
```

Remove, or uninstall, the timer:

```text
vaultkeep [--user] --config <job.yaml> schedule remove
```

`schedule remove` disables and stops the timer, removes the per-job systemd drop-in, reloads the appropriate systemd manager, and unregisters the timer from Vaultkeep's timer ownership registry. It does not delete the job YAML file, local job state, or backup archives.

`schedule update` preserves timer enablement. `schedule disable` stops/disables
future starts and clears persistent timer timestamps; `remove` also unregisters the
managed instance. Neither stops a backup already running. For a temporary pause,
use [maintenance stop/start instructions](maintenance.md#pause-and-resume-for-maintenance).
Manual, scheduled, and triggered execution share the same per-job backup lock.

`schedule disable` requires readable, well-formed YAML and a valid `job.id`, but
ignores unrelated schema errors, including unsupported properties and missing backup
settings. The managed jobs directory, filename, parameter selection, ownership registry,
and installed unit must still agree. It refuses unverified or locally overridden units
and reports service-manager failures. Repeated disabling succeeds. `install`, `update`,
`next`, and `remove` continue to require a fully valid configuration.

## Manage all job timers

Commands covering every job:

```text
vaultkeep [--user] schedules list
vaultkeep [--user] schedules validate
vaultkeep [--user] schedules sync --dry-run
vaultkeep [--user] schedules sync
```

`schedules list` shows each selected job instance's configured `enabled` setting,
managed-file `installed` state, systemd `active` state, `next` trigger (`n/a` when
unavailable), and `drift`. Drift reports missing or changed generated files,
timer enablement that differs from the job setting, or a timer still active for
a disabled schedule. Disabled jobs retain their timer files, so their previous
calendar is not compared. Operator-owned overrides are outside this comparison.
Listing is read-only and fails if systemd cannot be inspected.

`schedules sync` creates or updates timers for jobs with `schedule.enabled: true`
and disables timers for jobs with `schedule.enabled: false`. It also stops,
disables, and removes registered timers whose job YAML has been deleted, clearing
their persistent timestamps. Unrelated unit files and overrides are retained.
Removal attempts timestamp cleanup even if the generated timer file is already missing;
cleanup errors fail sync and trigger rollback.
Use `--dry-run` to preview the actions without applying changes.

Bulk operations use all jobs in the selected scope's jobs directory and take no
`--config` or `--param` selection. `schedules validate` checks job configuration and
schedule calendars; use `vaultkeep --config JOB.yaml validate` (with `--user` for
user jobs) for complete runtime and health checks of one job.

For parameterized jobs, bulk commands reuse the parameters saved by each installed
timer and report each instance separately. `schedules sync` updates or disables
those selections. Add a new selection explicitly:

```text
vaultkeep [--user] --config <job.yaml> --param repo=nas-a schedule install
```

Templates with no installed parameter selection produce a `skip` message, and
the remaining jobs continue. Skipped templates are not fully validated; validate
one with its intended `--param` values before installing it. Sync checks all
selected configurations and saved parameter selections before changing timers.
Invalid configurations or unreadable saved selections stop sync during this check.
In root mode, a timer without a service override uses `<instance>.yaml`, as defined
by the shared service template. Its name is not used to infer saved parameters.
If applying the plan fails or is interrupted with Ctrl-C, sync restores the previous
generated files and ownership registry, reloads systemd, and restores timer
enablement and activity before reporting the failure. Any errors during restoration
are reported as an incomplete rollback.
Persistent timestamps already cleared during sync are not restored.

## Trigger commands

All operational Trigger commands require `--user` before `trigger` and must run as
a non-root application owner. Root execution is rejected before configuration,
state or services are accessed. `--help` and `--version` remain available to root.
Root-mode backup jobs use manual execution or system timers.

`--config` selects the complete YAML job. Put all job options (`--user`, `--config`,
and `--param`) before `trigger`, as in
`vaultkeep --user --config JOB.yaml --param NETWORK=mainnet trigger monitor`.
Trigger always uses the systemd user manager and user-mode backups. Parameters
override the job's `trigger.params` defaults. Use the application-wide
`vaultkeep --version` to inspect the installed version.

Install/update confirmations use concise status fields. Diagnostic logging keeps
severity prefixes. Add
the global `--no-color` option before the command to disable terminal colors, for
example `vaultkeep --no-color --user --config JOB.yaml trigger install`. See
[terminal output](#terminal-output) for color and redirection rules.

### Monitoring and checks

`monitor` runs the selected job's foreground monitor. It requires an enabled trigger,
acquires that canonical YAML path's lifetime lock, performs runtime preflight, reconciles saved
attempts, and starts observation. SIGTERM/SIGINT stop the monitor while workers
continue. Configuration is read once. A second monitor for the same canonical YAML path is refused before backup
validation, including invocations with different parameters. Different paths have independent locks.

Use `vaultkeep --user --config JOB.yaml validate` to validate the complete job and
check Trigger runtime prerequisites, installation, and health. It reports enabled
features explicitly, checks the monitor's systemd enablement/activity, recent updates
(with a freshness limit of twice the poll interval), configuration fingerprint,
process observation, recovery blocks, and recorded backup failures.
Missing state or ambiguous/unavailable process observations cannot establish health.
Backup history is read without reconstruction or repair. Use matching `--param`
values; enabled Trigger's `trigger.params` defaults apply to this validation command.

Full validation and schema-only behavior, expected incomplete setup, and core exit
codes are described under [validation](#validate-a-job-configuration). A just-started
monitor may need its first observation before that check reports healthy status.

Validation does not certify wallet process identity, writer coverage, matcher overlap,
or destination independence. Version checks compare the configured invocation with
the current Vaultkeep distribution; mismatches produce a warning. Internal backup
validation/version calls have a 60-second timeout; systemd probes are bounded and
synchronous. Health checks observe state without an execution lock, so concurrent
transitions can occur during or immediately after a report.

### Service management

`install` validates the configuration and runtime prerequisites, writes the user service,
reloads systemd, and enables/starts the monitor. There is **one Trigger per canonical
YAML path**. A second installation for that path is refused, including when `--param`
values differ; use `update` instead. Both commands require `trigger.enabled: true`.

The service is `vaultkeep-trigger@<yaml-filename>-<path-hash>.service`. The filename
omits `.yaml`/`.yml` and is escaped for systemd; the hash is the first 16 hexadecimal
characters of SHA-256 of the canonical absolute path. Symlink aliases resolving to
that path identify the same service. YAML edits never change the name. Renaming or
moving YAML creates a different path identity; remove the old installation using its
original canonical path before installing the new one.

After editing job settings or parameters, run:

```sh
vaultkeep --user --config /absolute/path/job.yaml validate
vaultkeep --user --config /absolute/path/job.yaml trigger update
vaultkeep --user --config /absolute/path/job.yaml status --verbose
```

Validation may report an installed/desired identity mismatch until update succeeds.
`update` finds the installation by path, checks its original backup execution lock
and recovery state, stops the monitor, rechecks safety, and replaces its command
without renaming the service. Parameters still render backup destinations; they do
not create extra services. Backup locks, workers, fingerprints, and recovery directories
retain their existing execution identities. A restart cannot silently adopt a new
execution identity; explicit update is required.

`disable` stops/disables the monitor. `remove` also removes its managed unit and
reloads systemd. Both use verified installation metadata, independently of current
YAML contents, job ID, and parameters. They work after invalid edits or deletion:

```sh
vaultkeep --user --config /original/canonical/path/job.yaml trigger disable
vaultkeep --user --config /original/canonical/path/job.yaml trigger remove
```

The original application owner and canonical path must still be identifiable. If a
symlink was removed or retargeted, use its original resolved target path. Both commands
preserve YAML, archives, execution state, and recovery evidence and never stop workers.
They refuse active backups, pending requests, unreconciled attempts, blocked/corrupt
state, and unresolved safety warnings. Review [Trigger recovery](trigger/recovery.md)
before retrying. For a temporary maintenance pause that preserves enablement, use
`systemctl --user stop <monitor-unit>` directly.

A single verified legacy `vaultkeep-trigger@<job-id>-<execution-hash>.service` is
migrated by `trigger update` (or `install`). Its installed command and name establish
the original execution identity; current YAML is not used to reconstruct it. Migration
safely retires the old monitor before starting the new one and preserves all history.
Multiple services for the same path cause an explicit conflict; review and safely
resolve every installation rather than selecting one automatically. Legacy units
remain removable with `disable`/`remove` even if YAML is invalid or missing.

All lifecycle commands verify managed structure, file ownership, installed identity,
effective systemd fragment, and absence of unreviewed overrides. Changed unit files
requiring `daemon-reload` must be reviewed first. A failed update/migration may leave
the monitor stopped; inspect the reported unit and retry after correcting the failure.
If activation fails, the new monitor is stopped before restoring the previous unit.
If stopping or verification fails, its verifiable definition is retained for recovery.
Status and validation never migrate services or change recovery records. See
[service operation](trigger/service-operation.md).

### Job status

Use the shared [job status command](#check-job-status), including `--verbose` for
diagnostic paths and details. The [status reference](status.md) separates Backup
execution and cleanup diagnostics from Trigger monitoring, dispatch, and recovery blocks.

### Trigger exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Command succeeded. |
| 1 | Reserved usage-error vocabulary; an unhandled Python error may also exit 1. |
| 2 | Argument, job/trigger configuration, selection, or recovery-state error. |
| 3 | Blocked runtime prerequisite or failed service-manager action. |
| 4 | Internal worker's backup failed or was deferred. |
| 5 | Reserved; no Trigger command currently emits this code. |
| 6 | Trigger/backup integration error. |

Worker receipts preserve the actual backup exit code separately. These Trigger
command codes do not change the normal backup CLI's exit codes.

### Internal worker

`trigger exec-worker` is an internal service entry point. Do not run it or enable
individual worker instances manually. See [worker operation](trigger/service-operation.md#units)
and [recovery](trigger/recovery.md).

## Check job status

```text
vaultkeep [--user] --config <job.yaml> [--param KEY=VALUE ...] status [--json] [--verbose]
```

Status reads local execution evidence and systemd without accessing source contents
or requiring destination mounts. It never repairs or reconciles state. Exit zero means
the report was readable, including failed previous executions or unknown activity;
inspect its fields for health. When full validation fails but identity remains
recognizable, status still prints the configuration errors, available backup evidence,
and verified systemd monitor/timer state, including monitor automatic restarts and
last exit code. In this recovery report, automation `Status` describes actual systemd
enablement; unverified facts are `UNKNOWN`. No configurations or services are changed.
Malformed/unreadable YAML or unrecognizable identity fails safely without guessing a unit.
Inspection errors return 15; an invalid configuration returns 3 with the available
report. A configuration-valid status report is not a substitute for `validate`.
`--verbose` adds diagnostics and paths; it can be combined with JSON.
See the [status reference](status.md) for all fields, freshness, and uncertainty.

## Exit codes

Core commands (`run`, `validate`, `status`, `list`, `verify`, `prune`, `schedule`,
and `schedules`) use the following codes. Trigger subcommands have the distinct
[Trigger exit codes](#trigger-exit-codes) described above.

| Code | Meaning |
|---:|---|
| 0 | Success, including unchanged |
| 2 | Invalid command-line arguments |
| 3 | Invalid configuration or validation health/setup failure |
| 5 | Destination, mount, finalization, or retention error |
| 7 | Source, archive, verification, or generic execution failure |
| 10 | Job lock already held |
| 11 | Hook execution failure |
| 14 | Timer management failure |
| 15 | Local state or status-inspection error |
| 130 | Interrupted by SIGINT |
| 143 | Interrupted by SIGTERM |

Unhandled exceptions can exit 1; treat any other nonzero result as a failure and
retain diagnostics. A worker receipt stores the original backup exit code separately
from Trigger orchestration results. Full validation reports missing automation setup
and failed health checks as nonzero, normally 3; source/destination/state checks can
retain their specific codes. `--schema-only` does not establish runtime readiness.
