# Trigger commands

Trigger is part of the `vaultkeep` command:

```text
vaultkeep --user trigger --config JOB.yaml [--param KEY=VALUE ...] monitor
vaultkeep --user trigger --config JOB.yaml [--param KEY=VALUE ...] check-config [--runtime]
vaultkeep --user trigger --config JOB.yaml [--param KEY=VALUE ...] status [--json]
vaultkeep --user trigger --config JOB.yaml [--param KEY=VALUE ...] install
vaultkeep --user trigger --config JOB.yaml [--param KEY=VALUE ...] update
vaultkeep --user trigger --config JOB.yaml [--param KEY=VALUE ...] disable
vaultkeep --user trigger --config JOB.yaml [--param KEY=VALUE ...] remove
vaultkeep --user trigger --version
```

All operational Trigger commands require `--user` before `trigger` and must run as
a non-root application owner. Root execution is rejected before configuration,
state or services are accessed. `--help` and `--version` remain available to root.
Root-mode backup jobs use manual execution or system timers.

`--config` selects the complete YAML job. Options may also precede `trigger`, as in
`vaultkeep --user --config JOB.yaml trigger status`. Trigger always uses the systemd
user manager and user-mode backups. Use one `--config` selection, before or after
`trigger`. Parameters override the job's `trigger.params` defaults.

## Monitoring and checks

`monitor` runs the selected job's foreground monitor. It requires an enabled trigger,
acquires that job's lifetime lock, performs runtime preflight, reconciles saved
attempts, and starts observation. SIGTERM/SIGINT stop the monitor while workers
continue. Configuration is read once. A second monitor for the same identity is
refused before backup validation runs. Different jobs have independent locks.

`check-config` validates the job YAML and process matcher and performs schema-only
backup validation for an enabled trigger. Disabled triggers are structurally checked
without probing their backup sources. `--runtime` additionally checks user-manager
reachability, worker-template readiness, runtime/state parent permissions, and full
backup validation. It does not start a service or initialize recovery state. Full
backup validation may create and remove a temporary destination writability probe.

Neither check certifies wallet process identity, writer coverage, matcher overlap,
or destination independence. Version checks compare the configured invocation with
the current Vaultkeep distribution; mismatches produce a warning. Validation/version
calls have a 60-second timeout; systemd probes are bounded and synchronous.

## Service management

`install` performs runtime checks, writes this job's user service, reloads the user
manager, and explicitly enables/starts its monitor. `update` requires an existing
managed service, refreshes its command and restarts the monitor. Both require
`trigger.enabled: true`. Monitor units are named
`vaultkeep-trigger@<job-id>-<identity>.service`; `status` prints the exact unit name.

`disable` stops and disables this monitor. `remove` also removes its generated unit.
Both preserve recovery state and leave workers running. Drain workers and review
outcomes before switching to scheduled execution. For maintenance pauses that should
preserve enablement, use `systemctl --user stop <monitor-unit>` directly.

Service management refuses an unrelated file, symlink, or locally overridden monitor
unit rather than adopting or overwriting it. Job changes require explicit validation
and `update`; there is no automatic reload. Identity changes require retiring the old
service separately. See [service operation](service-operation.md).

## Status

`status` reads the selected job's recovery namespace. It prints its service name,
state root, lock status, observation/update ages, pending and active attempt IDs,
latest result, and review/block diagnostics. It creates no state or runtime files.
It requires the YAML job so it can resolve the identity, even if the trigger is disabled.

Exit 0 means a readable report, including when the last backup failed. It does not
prove that monitoring is responsive or backups are healthy. Corrupt/incompatible
records produce exit 2; review warnings and unknown outcomes require evidence review.

### JSON status

For scripts, add `--json`:

```sh
vaultkeep --user trigger --config JOB.yaml status --json
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
