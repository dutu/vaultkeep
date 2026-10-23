# Job status

Use the same mode, YAML path, and runtime parameters as the backup invocation:

```sh
vaultkeep --user --config JOB.yaml status
vaultkeep --user --config JOB.yaml --param repo=nas-a status --json
sudo vaultkeep --config /etc/vaultkeep/jobs/app.yaml status
```

For an enabled Trigger, `trigger.params` supplies the default parameter selection;
explicit `--param` values override it, as with job validation. Direct manual runs
still require their parameters explicitly.

Status reads local execution evidence and systemd information. Sources, destination
mounts, archive tools, and hook executables do not need to be available. It creates
no files, takes no execution or monitor locks, changes no timestamps, and never
reconciles requests or repairs records. Reports are snapshots; execution can change
during or after inspection. An idle report is not permission to start a writer.

## Backup

Backup is always present. Manual `run`, systemd timers, and Trigger workers all use
the same execution lifecycle, lock, progress stream, and completion evidence.

| Activity | Meaning |
| --- | --- |
| `RUNNING: <phase>` | The job lock, execution identity, live process identity, and fresh progress agree. |
| `FINISHING` | The owning process finished its phase stream and is recording its outcome. |
| `IDLE` | No execution holds the lock and reliable completion evidence matches the latest execution stamp. |
| `UNKNOWN` | Activity cannot be established: evidence is missing, stale, damaged, inaccessible, inconsistent, or process/lock ownership cannot be verified. |

The first elapsed time is time in the current phase; the second is total execution
time. Phases include preparation, source discovery and hashing, archive creation,
verification, finalization, state persistence, retention, hooks, and failure handling.
Updates occur on phase changes and every ten seconds. Progress older than thirty
seconds does not establish activity. Telemetry is best-effort and cannot fail a backup.

`Previous backup` describes the preceding execution attempt independently of the
current phase. It is not a claim that a new backup archive was created on every run.

| Result | Meaning |
| --- | --- |
| `SUCCESS` | The execution created an archive and completed its remaining lifecycle. |
| `UNCHANGED` | The execution completed without needing a new archive. |
| `FAILED` | Execution failed, including runtime validation, finalization, retention, hooks, or cleanup. An archive may already have been committed. |
| `INTERRUPTED` | A caught termination or interruption ended execution. |
| `UNKNOWN` | Completion cannot be established; for example, the process was killed or completion persistence failed. |
| `NONE` | No previous execution evidence is available. |

Completion time and duration are shown when known. Failures include an exit code or
signal, a concise reason, and an existing log path when available. Atomic-rename
errors are finalization failures. A post-commit failure preserves the committed
archive; check it rather than assuming it is absent or corrupt. Archive manifests
and archive state remain the source of facts about the last successfully created
backup. An active execution does not hide the preceding result.

Stale progress cannot override a completion record. A released lock or a finished
progress stream alone does not establish success or failure. Missing completion
evidence remains unknown, with no invented completion timestamp. Status never
reconstructs results from destination contents or Trigger receipts.

The engine keeps private `execution.json`, `progress.json`, and `execution.log` in
the selected job's local state directory. The log rotates at 64 KiB with one retained
segment. Set `logging.level: debug` for up to ten diagnostic lines, each bounded to
500 characters, and inspect the displayed log for full retained context. Logs can
contain sensitive operational details; review before sharing.

## Trigger

Shown when the job configures application monitoring, including a disabled trigger,
or retains monitoring/recovery evidence. An unused `trigger: {enabled: false}`
placeholder is omitted.
Inspect this section as the non-root application owner with `--user`.

- `Status` reports configured enablement.
- `Monitor` reports service/lock health and the age of the last update. A held lock
  with stale updates is `UNKNOWN`; it does not prove a responsive monitor.
- `Application` uses `RUNNING`, `HAS STOPPED`, `CONFIRMING STOP`,
  `NOT YET DETECTED`, or `UNKNOWN`. Fresh observations must belong to the current
  monitor session and configuration. `RUNNING` includes observation recency.
- `Dispatch` describes requests and worker orchestration: idle, pending, active,
  awaiting reconciliation, blocked, or unknown. Pending requests and worker health
  remain separate from actual backup activity.
- Diagnostics retain rejected submissions, final-recheck blocks, uncertain
  dispatches, and unresolved safety warnings. A blocked final recheck did not run a
  backup. An unresolved warning remains visible with its attempt and execution
  interval when available, even after a later successful execution.

Debug details include monitor service and state paths, observations, and arming
state. Request, launch, receipt, and capped child logs remain Trigger recovery
evidence. Status reads them without adopting attempts or releasing active slots.
See [Trigger recovery](trigger/recovery.md) and [troubleshooting](trigger/troubleshooting.md).

## Schedule

Shown when scheduling is configured with an interval or enabled. It reports configured
enablement, installed timer information, timer health, actual systemd enablement,
and the next execution when the manager provides one. It uses bounded `systemctl show`
queries, without full service-status output or timer-management operations. A failed
timer is a scheduler problem, separate from the backup result. No countdown is
invented when there is no scheduled next execution.

`schedule next` remains available for a standalone next-run query; schedule lifecycle
actions remain `install`, `update`, `disable`, and `remove`.

## JSON

The human report and JSON use the same collected interpretation. The JSON object has:

| Field | Value |
| --- | --- |
| `schema_version` | `1` for this job-status schema. |
| `job_id` | Selected YAML job ID. The YAML path, mode, and runtime parameters select the storage namespace. |
| `backup` | Always present: `activity`, `current`, `previous`, and `diagnostics`. |
| `trigger` | Present only when configured; enablement, monitor and application state, dispatch, request/attempt IDs, diagnostics, and safety annotations. |
| `schedule` | Present only when configured; enablement, `timer`, `installed`, `health`, `timer_enabled`, `next_execution`, and diagnostics. |

`backup.current` is null without verified live progress. Otherwise it contains
`execution_id`, `phase`, `started_at`, `phase_started_at`, `phase_elapsed_seconds`,
and `elapsed_seconds`. Durations are numbers in seconds.

`backup.previous` is null without execution evidence. Otherwise it contains
`execution_id`, `started_at`, `result`, `completed_at`, `duration_seconds`,
`exit_code`, `signal`, and `reason`, plus `log_path` when available. Unknown facts
are null; an uncertain execution has result `UNKNOWN`, never an assumed success.
Debug reports can add `backup.diagnostic_tail` and `trigger.details`.

Trigger fields include `enabled`, `monitor`, `monitor_updated_seconds_ago`,
`application`, `application_detail`, `dispatch`, `pending_request_id`,
`active_attempt_id`, `diagnostics`, and `safety`. A root-mode inspection reports
Trigger as unknown with an instruction to inspect the owner's user-mode job.
Worker and dispatch-age fields are included when observed.

Missing/unavailable next-execution information is null. A subsystem inspection error
sets its `read_error` field to true and retains diagnostics. Exit 0 means collection
was readable, even with a failed previous backup, blocked dispatch, or unknown
activity from missing evidence. Inspection errors return 15 while preserving the
available JSON report. Invalid job configuration or parameters return 3. Check the
section fields to decide operational health rather than treating exit 0 as a
successful backup.

[User guide](../README.md#check-job-status)
