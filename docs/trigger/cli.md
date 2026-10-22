# Trigger commands

Trigger is part of the `vaultkeep` command:

```text
vaultkeep --user --config JOB.yaml [--param KEY=VALUE ...] trigger monitor
vaultkeep --user --config JOB.yaml [--param KEY=VALUE ...] validate [--schema-only]
vaultkeep --user --config JOB.yaml [--param KEY=VALUE ...] trigger status [--json]
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
`vaultkeep --user --config JOB.yaml --param NETWORK=mainnet trigger status`.
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
of the Trigger service/status/worker exit table below. A just-started monitor may not yet
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
`vaultkeep-trigger@<job-id>-<identity>.service`; `status` includes the exact unit
name in diagnostics at `logging.level: debug`.

`disable` stops and disables this monitor. `remove` also removes its generated unit.
Both preserve recovery state and leave workers running. Drain workers and review
outcomes before switching to scheduled execution. For maintenance pauses that should
preserve enablement, use `systemctl --user stop <monitor-unit>` directly.

Service management refuses an unrelated file, symlink, or locally overridden monitor
unit rather than adopting or overwriting it. Job changes require explicit validation
and `update`; there is no automatic reload. Identity changes require retiring the old
service separately. See [service operation](service-operation.md).

## Status

`status` is a read-only report for the selected job. It separates trigger configuration,
monitor runtime and freshness, current activity, monitoring health, the latest completed
backup or attempt, and outstanding execution risks. Empty history and healthy observation
sections are omitted. A normal stopped application is not a monitoring failure.

```text
Vaultkeep — Trigger status

Job:      app
Trigger:  ENABLED
Monitor:  RUNNING (updated 1s ago)

Current activity
  Backup         IDLE
  Trigger armed  YES

Last backup
  Result     SUCCESS
  Completed  2026-10-22 09:47:00 UTC
```

Monitor lock ownership establishes runtime activity, not responsive observation.
Updates older than twice the configured poll interval, unavailable updates, and future
update timestamps require checking the monitor journal and running job validation.
When the monitor is not running, a systemd probe distinguishes a missing, failed, or
starting service when possible. Trigger `ENABLED` refers to job configuration, not
systemd enablement.

Current activity uses the same persisted evidence as [JSON status](#json-status),
including unassociated attempts awaiting recovery. Live worker probes refine it:

| Backup activity | Meaning |
| --- | --- |
| `IDLE` | No recorded pending or active attempt. |
| `PENDING` | A request is waiting for dispatch. |
| `STARTING` | systemd reports an activating worker. The oneshot worker can remain in this state throughout backup execution; this is not a claim that the backup child has not started. |
| `RUNNING` | systemd reports `active/running`. This does not establish backup completion. |
| `QUEUED` | A systemd job is pending without confirmed execution. |
| `STOPPING` | The worker is deactivating. |
| `STOPPED` / `FAILED` | The worker has ended; its attempt still awaits reconciliation. Neither establishes the backup outcome. |
| `UNKNOWN` | State or the worker probe is inconclusive or unavailable. |

A receipt can establish child completion while its worker remains active. It is shown
with current activity until reconciliation ends the attempt; it cannot replace the
previous completed result. Probes never start, stop, reset, or reconcile workers.
JSON status retains its existing schema and does not perform live service probes.

`Last backup` distinguishes `SUCCESS` (created), `UNCHANGED` (no new archive),
`SUCCESS (backup result unknown)` (exit zero without a recognized result marker),
and `FAILED`. Other outcomes appear under `Last completed attempt`: `BLOCKED`,
`DEFERRED`, `EXECUTION ERROR`, or `UNKNOWN`. Exit codes and signals belong to the
backup child. `Completed` comes from a retained receipt; `Recorded` is summary
recording time and is not presented as backup completion time. Neither is invented
when unavailable.

Failures include an attempt ID, a concise reason, and an action. Recognized finalization
errors include the exact destination backup path from retained output. Missing evidence
is identified as unavailable; a failed finalization does not establish corruption.

`Outstanding issue` identifies affected attempts whose execution or source consistency
remains uncertain. A monitoring gap during an attempt means offline observation was
interrupted, not that a backup trigger was missed or an archive was corrupted. Retained
receipt timestamps identify the execution period, not the exact monitoring gap.
When unavailable, the report says so. A completed result and an additional consistency
risk may both appear; the same unknown outcome is not repeated as a separate issue.

Review annotations are persisted on the attempt and in aggregate instance state. Resuming
monitoring or completing a later successful backup does not resolve an earlier attempt's
uncertainty. A matching final-check refusal receipt proves that attempt never invoked
backup and resolves its execution risk, preserving its historical review annotation.
Other risks and legacy aggregate warnings that cannot be attributed remain outstanding.
The monitor avoids creating gap warnings when recorded completion preceded the gap,
using actual process-observation evidence rather than a generic state-write timestamp.
No warning expires by age. There is no acknowledgement or manual warning-clear command;
see [recovery](recovery.md#execution-review-warnings).

Normal reports omit service names, state directories, diagnostic paths, raw tails,
and healthy attempt IDs. Set the job's `logging.level: debug` and rerun `status` for
those details alongside the readable report. Failure diagnostics include up to ten
nonblank lines, each capped at 500 characters, from the fixed per-attempt `log.txt`;
full retained output remains in that file. Missing or unreadable diagnostics do not
change the outcome. Status never follows a receipt-supplied diagnostic path.

Reports and operational problems remain visible at every logging level; see
[terminal output](../../README.md#terminal-output). Status creates no state or runtime
files and requires the job YAML even when its trigger is disabled. Exit 0 means a
readable report, including when the last backup failed; corrupt or incompatible
records produce exit 2. Neither exit 0 nor a running service certifies backup health.

### JSON status

For scripts, add `--json`:

```sh
vaultkeep --user --config JOB.yaml trigger status --json
```

The command writes one JSON object followed by a newline to stdout. Field order is
deterministic; diagnostics go to stderr. Omitting `--json` preserves the human-readable
report. The existing optional positional instance selection also works with JSON:
`status INSTANCE_ID --json`. Without an instance selection, JSON reports the configured
job's instance. Configuration and parameter selection resolve the same recovery
namespace as other Trigger commands.

Example for a job with readable idle state and a stopped monitor:

```json
{
  "active_attempt_id": null,
  "backup_activity": "idle",
  "job_id": "app",
  "monitor_status": "stopped",
  "pending_request_id": null,
  "schema_version": 1
}
```

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer schema version, currently `1`. |
| `job_id` | Job identifier from the selected YAML configuration. |
| `backup_activity` | `idle`, `pending`, `active`, or `unknown`, as defined below. |
| `pending_request_id` | Recorded pending request ID, or `null`. |
| `active_attempt_id` | Recorded active or unresolved recovery attempt ID, or `null`. |
| `monitor_status` | `running` when the monitor's lifetime lock is held, `stopped` when it is free or absent, or `unknown` when runtime/lock information is unavailable. |

Activity reflects persisted Trigger evidence:

- `idle`: Readable instance state records no pending request or active/unresolved
  attempt, and no state-error block prevents interpreting that evidence.
- `pending`: A pending request exists without an active/unresolved attempt.
- `active`: An active association or unresolved recovery attempt exists. This takes
  precedence over pending work and includes queued, executing, and unresolved workers.
  It does **not** verify live execution or mean an archive is currently being written.
- `unknown`: Evidence is insufficient, including missing, corrupt, incompatible,
  unreadable, or blocked state without a known active attempt.

JSON status uses the existing recovery rules to include unassociated attempts that
still require reconciliation. It does not reconcile or repair them, change persisted
state, take exclusive execution locks, or change services. A saved active association
remains `active` even if a receipt exists or the monitor is stopped; the monitor must
reconcile worker and queued-job termination before clearing that association.

Exit 0 means the report was read successfully, including `unknown` for missing state
or unavailable monitor information. State read/validation errors produce a JSON report
with `backup_activity: "unknown"`, diagnostics on stderr, and exit 2. Configuration or
instance-selection errors retain their nonzero exits and may produce no JSON. Check
the exit code before interpreting a report. Exit codes do not indicate backup activity.

Monitor health is separate from backup activity: lock ownership does not prove monitor
responsiveness, and a stopped monitor does not establish that independent workers
have finished. This report covers Trigger-managed work for the selected job; it does
not inspect backups started manually, by timers, or by other applications. It reads
records without an execution lock, so concurrent transitions can occur during or
immediately after the check. An `idle` result is not permission to start an application
and provides no guarantee that another process will not initiate a backup.

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Command succeeded or status was readable. |
| 1 | Reserved usage-error vocabulary; an unhandled Python error may also exit 1. |
| 2 | Argument, job/trigger configuration, selection, or recovery-state error. |
| 3 | Blocked runtime prerequisite or failed service-manager action. |
| 4 | Internal worker's backup failed or was deferred. |
| 5 | Unknown-outcome vocabulary. |
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
