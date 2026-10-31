# Trigger architecture and execution contract

This document describes Trigger's monitoring, durable request ownership, worker
dispatch, and recovery design. Backup execution and shared application identity are
specified in the [main architecture](architecture_and_design.md); operational
procedures are in the [user guide](../../README.md#trigger).

Core monitoring, persistence, dispatch, workers, and CLI exist. Remaining implementation
and deployment gates are tracked in the [validation plan](../plans/trigger-validation.md).
The limitations noted below are not completed safety guarantees.

## Glossary

- **Instance** — one configured wallet installation with a unique ID, application identity, data directory, and one reviewed VaultKeep job.
- **Observation** — one sanitized poll result: `running`, `stopped`, `ambiguous`, or `inspection_unavailable`.
- **Shutdown request** — a confirmed shutdown with a stable UUIDv4 `request_id` and an immutable snapshot of its execution settings.
- **Pending request** — the single undispatched shutdown request retained for an instance.
- **Attempt** — one worker launch for a request, with a fresh UUIDv4 `attempt_id`; a contention retry is a new attempt for the same request.
- **Receipt** — the worker's immutable persisted outcome, including VaultKeep exit/signal, created/unchanged/unknown result, duration, version, bounded marker line, and diagnostics reference.
- **Review required** — a monitor-owned safety/uncertainty annotation, separate from an attempt's immutable execution result.
- **Effective job identity** — VaultKeep's lock identity: canonical configuration path, YAML job ID, and runtime parameters. Trigger preserves its locks without reimplementing this identity.

## Objective and boundaries

The built-in Trigger feature monitors configured Electrum,
Bisq desktop/daemon, and Wasabi GUI/daemon instances and invokes their VaultKeep jobs
after confirmed process disappearance. Application-specific enablement still depends
on installed process-model validation.

Trigger lives in `src/vaultkeep/trigger`. Each canonical YAML path has one foreground monitor and lifetime lock; execution identities retain their own recovery namespaces. The job YAML contains its optional trigger settings; scheduling and shutdown triggering cannot both be enabled. Independently supervised workers execute backups and write receipts. Keep the monitoring loop responsive while workers, systemctl commands, and notification deliveries are pending.

**Platform and scope.** Linux with a systemd user manager; user-mode VaultKeep only (`--user`), with no root operation, sudo, or privilege elevation. Each job covers the selected instance's reviewed source scope, including all related writer processes; offline operation only. Independent instances can back up concurrently, bounded by the number of enabled instances, with no global concurrency setting. Each instance permits one active attempt and one pending request.

**Concurrency boundary.** Enabled instances require independent jobs with distinct
effective destination roots and no shared mutable resources requiring serialization.
Trigger does not inspect job internals or enforce source scope and destination
independence. This conservative v1 restriction and its operational checks are described
in [deployment requirements](../trigger/service-operation.md#deployment-checks).

**Deferred.** Manual `run`, socket or manual spool submission, online backups, multi-instance jobs, live reload, global concurrency controls, conflict groups, and VaultKeep compromise-marking extensions. V1 provides no operator-initiated backup or explicit replay command for an uncertain request; manual review and direct VaultKeep operation remain operator actions.

**Other non-goals.** Changes to wallet applications or their startup procedures; a separate backup engine or restore; credential duplication; a network API; Bisq 2, Windows, macOS, or non-systemd init. The backup workflow remains behind the normal Vaultkeep CLI; process isolation and durable worker receipts are retained within the unified application.

## Backup execution boundary

Trigger and Schedule are parallel initiation components. Both invoke the common
[Backup lifecycle](architecture_and_design.md#171-common-execution-and-job-status).
Trigger freezes the canonical job path and runtime parameters, but does not copy or
reinterpret sources, destinations, credentials, archive settings, retention, or hooks.
Backup owns execution outcomes and archive health; Trigger receipts govern worker
orchestration and recovery.

The worker invokes the recorded absolute executable with an argument array, without
a shell:

```text
/absolute/path/vaultkeep --user --config /absolute/path/job.yaml [--param KEY=VALUE ...] run
```

The child runs synchronously within the independently supervised worker. Trigger
classifies its exit and trailing result marker as specified under
[results](#results-safety-and-advisory-deadlines); JSON output is not required.
The full [CLI exit contract](../cli.md#exit-codes) and
[shared locking model](architecture_and_design.md#20-locking) remain Backup concerns.
Trigger preserves those locks rather than replacing them.

### Execution environment

Resolve Trigger's home from the operating system account record for its effective UID.
Its fixed recovery namespace is specified under [durable ownership](#fixed-state-and-durable-ownership).

For the VaultKeep child, validate and snapshot an explicit environment: `HOME` set to account home, the user's own `XDG_RUNTIME_DIR` passed unmodified so VaultKeep's `vaultkeep/locks` namespace matches manual and timer-driven runs, `XDG_STATE_HOME` and `XDG_CACHE_HOME` using absolute values from the monitor's environment or account-home defaults, and the fixed `PATH=/usr/local/bin:/usr/bin:/bin`. `LANG` is copied when present. Trigger's own `<XDG_RUNTIME_DIR>/vaultkeep-trigger/jobs/<path-hash>/` subdirectory holds `monitor.lock` and lifecycle `service.lock` and is never passed as the child's `XDG_RUNTIME_DIR`. The runtime directory is required for `monitor.lock` and VaultKeep's preferred lock namespace. Fail closed if it cannot be resolved. Record these values in each request and pass the mapping explicitly to the VaultKeep subprocess, without inheriting arbitrary worker-unit variables. The absolute resolved VaultKeep executable is recorded in the request; do not resolve it through the worker's `PATH`.

Trigger does not control the environment of manual or timer-driven runs. Their
lock-namespace requirements follow the [shared locking model](architecture_and_design.md#20-locking).

### Application identity

Trigger adapts each instance to the
[shared application detector](architecture_and_design.md#213-shared-application-identity).
It retains its own request, attempt, receipt, persisted-state, monitor, and lifecycle
models. Workers bind execution to the frozen application identity and policy.
Wallet-specific identity findings and enablement gates are maintained in
[process detection](../trigger/process-detection.md#validation-before-enabling).

## Architecture and interfaces

Keep configuration, process detection, lifecycle, persistence, monitor, worker/executor, notifications, and CLI responsibilities small. There is no socket server, manual submission spool, or general shared-resource scheduler.

Each monitor tick first reconciles disabled/removed instances. For each enabled instance whose poll is due, it reconciles its active attempt, samples processes, updates lifecycle/alerts, dispatches eligible work, persists state, and cleans eligible history. Backup execution and notification delivery run independently. Systemctl calls are synchronous with bounded waits and can delay observation; fully responsive polling across slow manager calls remains a design requirement.

### Configuration and matchers

The closed job YAML schema, defaults, path handling, and disabled-trigger
rules are specified in [configuration](../trigger/configuration.md). Strong identity,
alternative entrypoint forms, directory predicates, metadata races, and validation
limitations are specified in [process detection](../trigger/process-detection.md).
The `matcher.entrypoints` list establishes identity when any configured entrypoint
matches; all other matcher predicates remain mandatory. Application-specific guides
under [docs/trigger/apps](../trigger/apps/) cover Bisq GUI/API daemon,
Electrum, and Wasabi configuration.

### Observation and lifecycle

- Return `running`, `stopped`, `ambiguous`, or `inspection_unavailable`, with sanitized evidence and reasons. Track PID, creation time, and boot ID, validating identity around metadata reads to handle exit/replacement races.
- Only verified running evidence arms automatic detection. Fresh startup while stopped creates no request. Keep observations, pending requests, and active attempts separate.
- Confirm continuous reliable absence for the configured delay using an in-process monotonic clock. A restart cancels the candidate; ambiguity/unavailable evidence clears the candidate, with a full delay required once reliable absence returns.
- Each confirmed shutdown receives a request ID and frozen job, matcher, environment, executable/version, boot ID, and relevant configuration fingerprint. Persist the pending request before dispatch.
- At most one active attempt and one pending request per instance. Independent instances may dispatch concurrently; there is no shared FIFO/global execution slot. Retain only the latest confirmed pending shutdown, recording supersession and a drop count; the monitor logs lifecycle events. A dedicated deduplicated supersession notification remains an unimplemented design requirement.
- A verified restart before dispatch clears the pending request and arms the next lifecycle. Repeated identical observations do not recreate or replace a request.
- An app restart may arm a later lifecycle while the active worker finishes. Before dispatch, wait for that worker to end and revalidate request currency and stopped evidence. The shared backup engine enforces the configured consistency policy and owns archive health.
- SIGTERM/SIGINT stops monitoring without terminating workers. Polling can miss an entire lifecycle between samples; disappearance does not establish a clean exit or complete event capture.

### Fixed state and durable ownership

Resolve account home from the effective UID's operating system account record. All commands and workers use `<account-home>/.local/state/vaultkeep-trigger/jobs/<identity>`; `HOME`, `XDG_STATE_HOME`, and configuration cannot relocate it. Runtime uses `<XDG_RUNTIME_DIR>/vaultkeep-trigger/jobs/<path-hash>/monitor.lock` for monitor exclusivity, independently of the execution-state namespace.

```text
<state-root>/
  instances/<instance-id>.json
  blocked/<instance-id>.json
  attempts/<attempt-id>/
    request.json
    launch.json
    receipt.json
    log.txt
```

- State directories use mode 0700 and files 0600. Use versioned JSON, atomic replacement, file fsync, and directory fsync for durable transitions. Pending request snapshots live in instance state until copied into an immutable attempt request.
- The monitor alone writes instance state: observation/evidence timestamps, boot/config fingerprints, arming/confirmation state, pending request, active attempt association, request retry/supersession state, per-request consecutive-rejection count and processed-attempt evidence, safety/uncertainty summaries, blocked-wait start time and blocking attempt ID, and per-request start/escalation and other notification-delivery markers. Workers write their own receipts and capped diagnostics; they never clear pending state.
- Request records carry `schema_version`, request/attempt/instance IDs, confirmation time, boot/config fingerprint, frozen matcher/data directory, configured absolute job path and parameter mapping (VaultKeep canonicalizes its own lock identity), absolute VaultKeep executable, selected environment, preflight version, and effective deadline.
- The launch marker records the attempt/unit name and dispatch-intent timestamp. It means the start may have been submitted, not that VaultKeep began or completed. The monitor durably adds submission-rejection classification and diagnostics to this record before requeueing a request classified as not accepted; deployed code semantics still require verification.
- Receipts carry IDs/schema version, start/end UTC timestamps and duration, attempt outcome, VaultKeep exit code or signal, actual execution version, classified outcome, raw trailing result/marker fields (null if absent), diagnostic reference/truncation flag, and blocked/integration-error details when applicable.
- Receipts are authoritative for worker orchestration, while unified job status reads
  backup outcomes directly from Backup. The monitor does not poll Backup status for
  notifications. Notification delivery never authorizes dispatch, replay, recovery,
  or successful completion; shared delivery and execution alerts follow the
  [common execution contract](architecture_and_design.md#171-common-execution-and-job-status).
- Persist UTC timestamps; use monotonic timing only within the current process. Never reuse a saved monotonic timestamp after restart. Unknown schema versions block dispatch and preserve evidence; no legacy state migration is provided.
- Corrupt or contradictory state preserves diagnosis material and blocks uncertain replay for its owning instance. Persist a separate versioned `blocked/<instance-id>.json` diagnostic without rewriting the original state, and continue monitoring healthy instances. The block survives restart until the operator reviews and repairs the evidence and removes the diagnostic. Failure to persist a block prevents further dispatch. The current loader validates schema/types and association checks. A request-file size cap was part of the design requirements but is not currently implemented; it must not be advertised as an enforced bound.
- Retain the 50 most recent cleanup-eligible completed attempts per instance, excluding active, pending, unreconciled, or otherwise recovery-referenced records. Preserve necessary safety/uncertainty summaries in instance state beyond diagnostic retention.
- An `outcome_unknown` attempt becomes eligible for cleanup only once it has ended, reconciliation is complete, its summary is durably recorded in instance state, and no recovery association still needs its record. Retain at most 20 completed, reconciled, summarized uncertain-attempt records per instance, removing the oldest eligible records first; this internal history cap does not apply to active or genuinely unresolved execution records. Preserve necessary worker-uncertainty diagnostics in instance state when detailed history is removed. Detailed completed history is limited to 50 eligible records per instance, with a 20-record sub-cap for `result_unknown`/`outcome_unknown`/`blocked` outcomes. Summaries are retained independently. The current dispatch guard holds new work when more than 70 records are protected; it never deletes recovery evidence to satisfy a bound.
- Prune notification markers when their detailed attempt/request history expires, preserving markers referenced by retained records, active or blocking attempts, current/latest requests, pending requests, and rejection recovery.

### Dispatch and restart recovery

The monitor acquires `monitor.lock` with nonblocking `flock` before full runtime/job preflight or owning instance state; a second monitor refuses startup without spawning VaultKeep validation. Perform runtime preflight, reconcile existing attempts, and only then admit new dispatch. Hold the lock for the monitor's lifetime, with no descriptor inheritance into workers.

Monitor/install preflight invokes a bounded backup-validation child in the selected
environment using the internal, help-hidden `validate --backup-only` option. This
avoids recursive automation checks and allows startup before monitor state exists.
When public validation has already checked Backup, Trigger skips that redundant
child. Public validation remains read-only with respect to services and recovery
state; its checks and results are specified in the
[CLI reference](../cli.md#validate-a-job-configuration).

For each eligible pending request:

1. Allocate a fresh attempt UUID and durably write its immutable request.
2. Durably associate the attempt with the instance and clear only its matching pending request. Reconciliation adopts a request written before this association, preventing a second allocation for the same request.
3. Durably write `launch.json` before the potentially irreversible start call.
4. Submit `systemctl --user start --no-block vaultkeep-trigger-exec@<identity>_<attempt-id>.service` without a shell. Submission success is not worker start or backup completion. The adapter classifies exit 0 as accepted, spawn failure and codes 4/5/6 as rejected, code 2 as an integration error, and other results as inconclusive. The monitor immediately persists/requeues classified rejections and retains inconclusive associations for receipt/unit/job reconciliation. Because systemctl can fail after queueing work, the design requires affirmative pre-queue rejection evidence; the current direct code mapping relies on deployment verification rather than a diagnostic check. A generic nonzero result followed by absent jobs/unit state alone never authorizes replay.
5. Reconcile the worker's receipt, then release the instance's active slot once the worker and any queued start job have ended.

Crash recovery must inspect all attempt records, including those not yet associated in instance state:

| Evidence | Recovery action |
| --- | --- |
| Valid request, no launch marker or receipt, matching pending request | No start was submitted by this protocol; adopt the association, summarize it as unsubmitted, and requeue only still-current safe work through a fresh delayed attempt |
| Valid request, no launch marker or receipt, no active/blocking association and no matching pending request | Summarize as an unsubmitted blocked attempt and allow bounded cleanup; it does not protect history or block dispatch |
| Valid receipt | Preserve its execution result; reconcile instance/safety state without rerunning it |
| Launch marker, no receipt, queued start job or executing worker | Keep the active association; wait for completion, including `activating`/`start` for a oneshot worker |
| Launch marker, no receipt, durable rejection classification, with no contradictory execution evidence | Record a blocked result; return the still-current request to the normal pending path for a delayed fresh-attempt retry |
| Launch marker, no receipt, manager unavailable or an inconclusive submission probe | Keep execution unresolved while a queued start or worker remains possible; retain the association, continue reconciliation, and block that instance's new dispatch |
| Launch marker, no receipt, no outstanding start job or executing worker confirmed, and no proof of rejection before queueing | Record monitor-owned `outcome_unknown`; never replay this request |

A template instance's existence or `LoadState=loaded` does not prove execution; examine start jobs and ActiveState/SubState rather than testing for a missing unit. A receipt remains authoritative for the child outcome, while the monitor may add a separate safety/uncertainty annotation when completion evidence is absent.

Accept explicit complete `LoadState=not-found` properties as absence evidence. If a unit lookup fails without those properties, require successful empty filtered unit and job listings before confirming absence; manager failure or incomplete/malformed evidence keeps the association unresolved. Confirmed absence without a receipt or proven rejection resolves to `outcome_unknown`.

Absent current jobs or unit state does not prove that the worker never executed: completed unit state can have been unloaded. Retryable rejection must come from a verified failure class that establishes the start was not accepted. Qualifying classes are failure to spawn the local `systemctl` process, and an explicit pre-queue manager refusal: an absent unit or template (`EXIT_NOTINSTALLED`, 5), a policy denial (`EXIT_NOPERMISSION`, 4), or a unit load failure (`EXIT_NOTCONFIGURED`, 6). Non-qualifying results are a generic failure (including code 1), any other unclassified nonzero result, a timeout, or a lost bus response; none of these prove non-execution. `EXIT_INVALIDARGUMENT` (2) indicates a Trigger argument-construction defect and is an integration error, not a rejection.

The adapter currently classifies codes 4/5/6 directly without validating diagnostic
text. This is a deployment assumption, not runtime proof of pre-queue rejection.
The [service deployment checks](../trigger/service-operation.md#deployment-checks)
cover the start-command mapping and required installed-system verification.

Preserve verified rejection evidence before releasing the active association; a crash before that write leaves the attempt uncertain. Inconclusive probes are not terminal `outcome_unknown` while execution remains possible; finalize that outcome only after worker/start-job absence is established without a receipt or proven rejection.

After an unknown attempt's worker is confirmed gone, preserve the warning but permit a later independently observed lifecycle to create a new request. Do not replay the old request or claim its archive outcome. V1 has no manual rerun/acknowledgement command.

Pending requests survive monitor restart only with compatible boot/config evidence and a fresh confirmation of reliable absence before dispatch. A boot-ID change invalidates old observation and pending evidence. Relevant config changes or disabled/removed instances invalidate obsolete undispatched requests; preserve and reconcile every active attempt using its frozen request, even for a removed instance.

On the same boot with compatible configuration, persisted verified running evidence plus current reliable absence re-arms a full confirmation delay. Reuse an already persisted compatible pending request rather than generating another request for the same observed shutdown.

VaultKeep exit 10 records a deferred attempt. Retain its logical request for retry no sooner than 30 seconds after completion, with a new attempt UUID and another final offline check. A submission classified as a pre-queue rejection is also retryable through that delayed pending path once its evidence is durable. Retry only if that request remains current; a restart or newer shutdown invalidates it. Other terminal failures do not auto-retry. An older blocked/deferred result must never clear or requeue a newer pending request.

### Worker and systemd units

Ship an installation-resolved absolute Trigger console-script path in both unit files:

- `vaultkeep-trigger@<yaml-filename>-<path-hash>.service`: foreground `vaultkeep --user --config JOB.yaml [--param ...] trigger monitor --expected-identity <execution-hash>`, restart-on-failure, journald output, UMask 0077. The unit records the installed job ID and effective selection, independently of current YAML. The path hash is the first 16 hex characters of SHA-256 of the canonical absolute path; the escaped filename omits `.yaml`/`.yml`.
- `vaultkeep-trigger-exec@.service`: `Type=oneshot`, `Restart=no`, `TimeoutStartSec=infinity`, UMask 0077, and `ExecStart=/usr/local/bin/vaultkeep --user trigger exec-worker %i`. Keep it independent of the monitor's stop/restart lifecycle. No terminating execution runtime limit, socket activation, dynamically created units, or enablement of individual attempt instances.

Lifecycle operations discover units by their verified installed canonical config path.
They validate ownership, generated structure, effective fragment/overrides, and the
installed execution namespace. Update/disable/remove hold the original backup job
lock, check recovery evidence, stop/disable the old monitor, acquire its path lifetime
lock (and legacy execution lifetime lock), and recheck state before replacing/removing
its definition. Pending, active, blocked, corrupt, and unresolved uncertain work blocks
retirement. Execution directories and worker identities are never renamed or deleted.

Explicit update can switch the installed execution identity at the same service name.
Monitor startup checks its pinned execution hash and any installed selection under
the path lifetime lock. Foreground monitors with different parameters share that lock.
A single verified legacy unit is migrated by install/update; conflicting units fail
closed. Stop the old unit before starting the replacement. If activation fails, stop
the replacement before restoring the previous unit; retain a verifiable definition if
safe rollback cannot be established. Status/validation inspect without migration.
Installer refresh preserves installed execution identity and legacy naming; only
explicit Trigger lifecycle operations perform migration or identity transitions.

Only the monitor submits worker starts. The worker reads and validates its request (including UUID and association checks), refuses to replay an existing terminal receipt, and performs the authoritative final process/identity recheck from the request snapshot. Reliable stopped evidence is required; running, ambiguous, or unavailable evidence yields a blocked receipt without invoking VaultKeep. The monitor then reconciles only the matching request.

The worker invokes the recorded absolute VaultKeep executable with `--user --config <job> [--param KEY=VALUE ...] run` using an argv array, no shell, and the request's explicit validated environment. Capture the actual VaultKeep version immediately before invocation and include it in the receipt; compare it with the request/preflight version and the running Vaultkeep distribution, logging a sanitized mismatch warning.

Always drain stdout/stderr even after the approximately 256 KiB combined diagnostics cap. Independently retain a bounded trailing stdout line for exact `run: created` / `run: unchanged` parsing, including when the result arrives after the log cap. Earlier markers or stderr markers do not determine success. After a normal final-recheck or child outcome, the worker writes and fsyncs its receipt before exit. An unreadable/malformed request can fail before receipt creation; conservative monitor recovery then applies.

Failed-unit inspection and cleanup procedures belong in
[service operation](../trigger/service-operation.md#inspect-workers-without-discarding-evidence).
Cleanup must preserve evidence needed for reconciliation.

### Results, safety, and advisory deadlines

| Attempt evidence | Recorded outcome |
| --- | --- |
| Exit 0, trailing `run: created` | Successful execution, `created` |
| Exit 0, trailing `run: unchanged` | Successful execution, `unchanged`; no new archive claim |
| Exit 0, absent/unrecognized trailing marker | Successful execution, `result_unknown`; retain bounded raw line, no new archive claim |
| Exit 10 | `deferred`, eligible for delayed retry of the same current request |
| Exit 2 or missing/exec-failing VaultKeep binary | `integration_error`, distinct from backup failure |
| Other nonzero exit or signal termination | `backup_failed`, with original code/signal and diagnostics |
| Missing completion evidence after worker absence is established | Monitor-owned `outcome_unknown`, manual review, no replay |

Contradictory ownership or receipt records create a persistent per-instance state-error block instead of being interpreted as a completed unknown outcome.

Keep worker results immutable. The request freezes the shared consistency policy,
complete application identity, and resolved process-owner UID. The worker checks
that its frozen identity and policy agree; the backup child compares that policy
with its own single effective loaded configuration before proceeding.

Trigger does not maintain archive-consistency review flags, create suspect markers,
prune backups, or associate attempt IDs with backup IDs. Restart/gap observations
alone do not classify archives. Preserve genuine unknown worker outcomes, blocked
execution, and uncertain dispatch without automatically replaying an old attempt.
Backup consistency boundaries are specified in the
[consistency reference](../trigger/consistency.md#offline-safety).

An optional positive deadline marks an attempt overdue and sends one best-effort notification; it never terminates the worker and never releases the active slot. While recovering an executing attempt, use persisted UTC start/launch evidence conservatively for overdue reporting; deadlines remain advisory. Hung executions require manual operator action.

A pending request waiting behind an active or unresolved attempt for the same instance is reported by `status` as blocked, with the blocking attempt ID and wait age. Use the effective deadline frozen in the blocking attempt's request, not the instance's current configuration. When that attempt has no deadline, emit at most one best-effort wait notification for that blocking attempt once the wait exceeds the internal `blocked_wait_seconds` default of 3600. When it has a deadline, its existing overdue notification covers the blockage and no second alert is sent.

Persist `blocked_wait_started_at` in UTC, the blocking attempt ID, and the wait-notification decision in instance state. Reuse them after monitor restart; replacing a pending request while the same continuous blockage remains must not reset the timer or repeat its alert. Clearing the pending request ends the wait; preserve any notification decision for the blocking attempt so a later wait behind it does not send the alert again. The current blocked-wait and overdue timers use persisted UTC timestamps; only shutdown confirmation uses the process-local monotonic clock. Wall-clock adjustments can affect alerts. This threshold is an internal constant, not a configuration setting.

The final recheck cannot prevent a subsequent writer restart. Polling can miss short lifecycles, and digest/source-change checks are not database snapshots. Never certify clean shutdown or source consistency beyond the reviewed job method and available observations.

### Notifications and diagnostics

Trigger emits monitoring, dispatch, and recovery notifications through the shared
notification component. It does not duplicate Backup execution-result alerts.
Delivery, deduplication, rejection escalation, and failure behavior are specified in
the [notification reference](../notifications.md).

Monitor transitions and warnings go to stderr/journald. Workers retain capped private
child diagnostics with their attempts. Diagnostic handling and operational inspection
are covered in [service operation](../trigger/service-operation.md#inspect-workers-without-discarding-evidence).

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Wrong/inaccessible process identity | Unsafe automatic backup | Verified strong matchers, writer-process review, fail-closed dispatch |
| App restart after final recheck | Engine policy applies | Offline boundary checks fail execution if running; files policy retains source checks |
| Shared destination/mutable resource | Cross-job archive/retention conflict | Operator-reviewed distinct destination roots and independent jobs; v1 does not enforce job internals |
| External VaultKeep lock namespace differs | Duplicate same-job executions | Explicit selected HOME/XDG environment; review external direct/timer runs |
| Crash across request/association/start | Lost request or duplicate execution | Stable request IDs, durable attempt adoption, launch marker, conservative reconciliation |
| Worker result missing or manager unreachable | Uncertain execution | Retain active association until absence confirmed; no replay of outcome_unknown |
| Monitor gap during backup | Worker outcome may need recovery | Reconcile dispatch and receipts; do not infer archive health from monitoring gaps |
| Retention deletes recovery records | Lost associations or safety warnings | Clean only eligible completed attempts; preserve referenced records and summaries |
| Notification unavailable | Missed alert | Best-effort delivery; diagnostics/status retain outcome |

[User guide](../../README.md)
