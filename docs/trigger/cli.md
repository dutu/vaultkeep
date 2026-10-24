# Trigger commands

Trigger is part of the `vaultkeep` command:

```text
vaultkeep --user --config JOB.yaml [--param KEY=VALUE ...] trigger monitor
vaultkeep --user --config JOB.yaml [--param KEY=VALUE ...] validate [--schema-only]
vaultkeep --user --config JOB.yaml [--param KEY=VALUE ...] status [--json]
vaultkeep --user --config JOB.yaml [--param KEY=VALUE ...] trigger install
vaultkeep --user --config JOB.yaml [--param KEY=VALUE ...] trigger update
vaultkeep --user --config JOB.yaml [--param KEY=VALUE ...] trigger disable
vaultkeep --user --config JOB.yaml [--param KEY=VALUE ...] trigger remove
vaultkeep --version
```

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
[terminal output](../../README.md#terminal-output) for color and redirection rules.

## Monitoring and checks

`monitor` runs the selected job's foreground monitor. It requires an enabled trigger,
acquires that job's lifetime lock, performs runtime preflight, reconciles saved
attempts, and starts observation. SIGTERM/SIGINT stop the monitor while workers
continue. Configuration is read once. A second monitor for the same identity is
refused before backup validation runs. Different jobs have independent locks.

Use `vaultkeep --user --config JOB.yaml validate` to validate the complete job and
check Trigger runtime prerequisites, installation, and health. It reports enabled
features explicitly, checks the monitor's systemd enablement/activity, recent updates
(with a freshness limit of twice the poll interval), configuration fingerprint,
process observation, recovery blocks/review warnings, and recorded backup failures.
Missing state or ambiguous/unavailable process observations cannot establish health.
Backup history is read without reconstruction or repair. Use matching `--param`
values; enabled Trigger's `trigger.params` defaults apply to this validation command.

Before installation, it reports `Monitor service` as `NOT INSTALLED` and
`Result: SETUP INCOMPLETE`, while continuing other available checks. Install the
monitor explicitly, then validate again. Validation never starts, enables, restarts,
or repairs services or recovery state. Full backup validation may create and remove
a temporary destination writability probe. `--schema-only` skips all runtime,
installation, history, and health checks, and says so in the result. See the
[validation user guide](../../README.md#validate-a-job-configuration).

Complete validation exits nonzero for missing installation or failed health checks
(normally 3; backup and state errors retain their core codes). This is independent
of the Trigger service/worker exit table below. A just-started monitor may not yet
have recorded its first update; wait for an observation and validate again.

Validation does not certify wallet process identity, writer coverage, matcher overlap,
or destination independence. Version checks compare the configured invocation with
the current Vaultkeep distribution; mismatches produce a warning. Internal backup
validation/version calls have a 60-second timeout; systemd probes are bounded and
synchronous. Health checks observe state without an execution lock, so concurrent
transitions can occur during or immediately after a report.

## Service management

`install` performs runtime checks, writes this job's user service, reloads the user
manager, and explicitly enables/starts its monitor. `update` requires an existing
managed service, refreshes its command and restarts the monitor. Both require
`trigger.enabled: true`. Monitor units are named
`vaultkeep-trigger@<job-id>-<identity>.service`; `status --verbose` includes the exact
unit name in diagnostics.

`disable` stops and disables this monitor. `remove` also removes its generated unit.
Both preserve recovery state and leave workers running. Drain workers and review
outcomes before switching to scheduled execution. For maintenance pauses that should
preserve enablement, use `systemctl --user stop <monitor-unit>` directly.

Service management refuses an unrelated file, symlink, or locally overridden monitor
unit rather than adopting or overwriting it. Job changes require explicit validation
and `update`; there is no automatic reload. Identity changes require retiring the old
service separately. See [service operation](service-operation.md).

## Job status

Use `vaultkeep --user --config JOB.yaml status` or `status --json` to inspect backup
execution, monitor freshness, application detection, and dispatch recovery. Backup
results come from the common backup engine. Trigger contributes its own monitoring,
requests, blocks, and source-consistency warnings. See the [status reference](../status.md).
Use `status --verbose` for historical safety-warning details and diagnostic paths.

## Exit codes

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

## Internal worker

The shared service template invokes:

```text
vaultkeep --user trigger exec-worker <identity>_<attempt-uuid>
```

The identity selects the job's recovery directory; the attempt selects its frozen
request. Workers do not load current YAML or infer paths from arbitrary strings.
Namespaces and UUIDs are validated before reading state. Do not start workers or
enable individual attempt units manually.

[User guide](../../README.md#optional-trigger)
