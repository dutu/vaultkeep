# Job status

Use the same mode, YAML path, and runtime parameters as the backup invocation:

```sh
vaultkeep --user --config JOB.yaml status
vaultkeep --user --config JOB.yaml status --verbose
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

## Invalid configurations

The `Configuration` section reports installed-schema and runtime-parameter validation
as `VALID` or `INVALID`, with individual error paths and messages. It does not check
runtime backup prerequisites; use `validate` for that.

With invalid job settings, status still inspects resources when `job.id`, the original
YAML path, and the parameter selection remain recognizable. It reports available
backup evidence and checks existing monitor/timer ownership before trusting systemd
state. Recovery reports include both automation sections so installed resources remain
visible even if their YAML settings were removed. In these reports, automation
`Status` is actual systemd enablement, with `UNKNOWN` for unverified ownership or
unavailable systemd. Application observations remain `UNKNOWN`. A monitor waiting for
automatic restart is `RESTARTING`; `Last exit` shows the service exit code when available.

Unknown properties, obsolete fields, and missing backup settings can be diagnosed
without repairing the configuration first. Malformed YAML, missing/unreadable files,
or invalid identity fields fail safely; filenames alone never select a resource.
Use [recovery disable commands](cli.md#service-management) to stop verified automation,
then fix and validate the job before enabling it again.

## Backup

Backup is always present. Manual `run`, systemd timers, and Trigger workers all use
the same execution lifecycle, lock, progress stream, and completion evidence.

| Activity | Meaning |
| --- | --- |
| `RUNNING: <phase>` | The job lock, execution identity, live process identity, and fresh progress agree. |
| `FINISHING` | The owning process finished its phase stream and is recording its outcome. |
| `IDLE` | The job lock is reliably free, independently of execution history. A job that has never executed is idle. |
| `UNKNOWN` | Lock ownership cannot be established, the execution changes during inspection, or a held lock lacks verified process identity and fresh, consistent progress. |

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

Completion time and duration are shown when known. Failed, interrupted, and unknown
previous executions show `Log` in normal output whenever the log is available;
`--verbose` adds log excerpts and detailed diagnostics. Failures also include an exit
code or signal and a concise reason. Atomic-rename
errors are finalization failures. A post-commit failure preserves the committed
archive; check it rather than assuming it is absent or corrupt. Archive manifests
and archive state remain the source of facts about the last successfully created
backup. An active execution does not hide the preceding result.

If archive cleanup also fails during an interruption, the result remains
`INTERRUPTED` and retains its interruption exit code and signal. The execution log
reports cleanup failures, including any private plaintext TAR that could not be removed.

Stale progress cannot override a completion record. A released lock or a finished
progress stream alone does not establish success or failure. Missing completion
evidence remains unknown, with no invented completion timestamp. Status never
reconstructs results from destination contents or Trigger receipts.
An idle job can therefore have a previous result of `UNKNOWN`. If history is missing
but the lock stamp identifies a prior execution, its outcome is `UNKNOWN`, not `NONE`.

The engine keeps private `execution.json`, `progress.json`, and `execution.log` in
the selected job's local state directory. The log rotates at 64 KiB with one retained
segment. Use `status --verbose` to show the selected execution-record path and up to
ten diagnostic log lines, each bounded to 500 characters. `logging.level: debug` also
enables these diagnostics. Inspect the displayed log for full retained context. Logs can
contain sensitive operational details; review before sharing.
Older Trigger receipts can exist without common execution records, for example if
those attempts preceded common execution reporting. `NONE` reports the absence of
execution evidence, not proof that no backup ever ran. For unexpected missing results,
use `--verbose` to check the execution-record path and retained logs. Check that the
mode, YAML path, runtime parameters, and XDG state environment match the execution.

## Trigger

Shown when the job configures application monitoring, including a disabled trigger,
or retains monitoring/recovery evidence. An unused `trigger: {enabled: false}`
placeholder is omitted.
Inspect this section as the non-root application owner with `--user`.

- `Status` reports configured enablement.
- `Monitor` reports service/lock health and the age of the last update. A held lock
  with stale updates is `UNKNOWN`; it does not prove a responsive monitor.
- `Application` uses `RUNNING`, `HAS STOPPED`, `CONFIRMING STOP`,
  `NOT RUNNING`, or `UNKNOWN`. Fresh observations must belong to the current
  monitor session and configuration. `RUNNING` includes observation recency.
  `NOT RUNNING` means the application is reliably observed stopped with no shutdown
  confirmation in progress. Retained requests, attempts, and recovery records do not
  override that observation. `HAS STOPPED` requires a confirmed shutdown transition
  in the current session; after a monitor restart, fresh stopped observations report
  `NOT RUNNING` until a new shutdown transition is confirmed. `CONFIRMING STOP`
  covers shutdown confirmation; `UNKNOWN` covers unreliable, stale, ambiguous, or
  unavailable observations.
- `Dispatch` describes requests and worker orchestration: idle, pending, active,
  awaiting reconciliation, blocked, or unknown. Pending requests and worker health
  remain separate from actual backup activity.
- Diagnostics retain rejected submissions, final-recheck blocks, uncertain
  dispatches, and unresolved safety warnings. A blocked final recheck did not run a
  backup. Historical safety warnings are summarized as `REVIEW REQUIRED (N previous
  attempts)` with an action to review affected executions, even after a later
  successful execution. Current safety warnings remain explicit. Use `status --verbose`
  for historical attempt IDs, execution intervals, reasons, and recovery guidance.

Verbose and debug details include monitor service and state paths, observations, and arming
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
| `configuration` | `status` (`VALID` or `INVALID`) and `errors`, each with `path` and `message`. |
| `backup` | Always present: `activity`, `current`, `previous`, and `diagnostics`. |
| `trigger` | Present when configured/retained or during user-mode recovery inspection; enablement, monitor and application state, dispatch, diagnostics, and available safety annotations. |
| `schedule` | Present when configured or during recovery inspection; enablement, `timer`, `installed`, `health`, `timer_enabled`, `next_execution`, and diagnostics. |

`backup.current` is null without verified live progress. Otherwise it contains
`execution_id`, `phase`, `started_at`, `phase_started_at`, `phase_elapsed_seconds`,
and `elapsed_seconds`. Durations are numbers in seconds.

`backup.previous` is null without execution evidence. Otherwise it contains
`execution_id`, `started_at`, `result`, `completed_at`, `duration_seconds`,
`exit_code`, `signal`, and `reason`, plus `log_path` when available. Unknown facts
are null; an uncertain execution has result `UNKNOWN`, never an assumed success.
Verbose and debug reports can add `backup.details`, `backup.diagnostic_tail`, and
`trigger.details`.

Trigger fields include `enabled`, `monitor`, `monitor_updated_seconds_ago`,
`application`, `application_detail`, `dispatch`, `pending_request_id`,
`active_attempt_id`, `diagnostics`, and `safety`. A root-mode inspection reports
Trigger as unknown with an instruction to inspect the owner's user-mode job.
Worker and dispatch-age fields are included when observed.
Recovery inspection adds `last_exit` when systemd provides it. Its automation `enabled`
fields reflect actual systemd enablement and are null when unverified. Normal reports
retain configured enablement. Recovery reports can omit unavailable observation and
safety fields; available backup history remains independent of configuration validity.
The `safety` array always retains detailed warnings, including attempt IDs and
execution periods when available. Each warning's `current` flag separates current
safety concerns from historical review requirements. `safety_action` and
`safety_guidance` provide review instructions when warnings exist. `--verbose` can be
combined with `--json`; normal JSON already includes historical safety details.

Missing/unavailable next-execution information is null. A subsystem inspection error
sets its `read_error` field to true and retains diagnostics. Exit 0 means collection
was readable, even with a failed previous backup, blocked dispatch, or unknown
activity from missing evidence. Inspection errors return 15 while preserving the
available JSON report. Invalid job configuration or parameters return 3, retaining
the available report when identity can be resolved. Check the
section fields to decide operational health rather than treating exit 0 as a
successful backup.

[User guide](../README.md#check-job-status)
