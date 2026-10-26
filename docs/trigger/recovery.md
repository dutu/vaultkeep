# Recovery

Trigger is conservative across crashes. It never replays an uncertain execution and
never claims an archive outcome it cannot establish.

## Durable transitions

For each eligible pending request the monitor:

1. allocates a fresh attempt UUID and durably writes its immutable request;
2. durably associates the attempt with the instance and clears only its matching pending
   request;
3. durably writes `launch.json` **before** the start call;
4. submits `systemctl --user start --no-block` and classifies the result;
5. reconciles the worker's receipt and releases the instance's active slot once the
   worker and any queued start job have ended.

## Recovery matrix

| Evidence | Action |
| --- | --- |
| Valid request, no launch marker or receipt | Recover matching current pending work; summarize unreferenced obsolete requests as never submitted and make them cleanup-eligible. |
| Valid receipt | Preserve the execution result; reconcile without rerunning. |
| Launch marker, no receipt, queued start job or executing worker | Keep the active association; wait for completion, including `activating`/`start` for a oneshot worker. |
| Launch marker, no receipt, persisted pre-queue rejection classification, no contradictory evidence | Record a blocked result; return the still-current request to the delayed pending path. |
| Launch marker, no receipt, manager unavailable or inconclusive probe | Keep execution unresolved; retain the association and block new dispatch for that instance. |
| Launch marker, no receipt, no outstanding job/worker, no proof of rejection | Record monitor-owned `outcome_unknown`; never replay this request. |

A template instance's existence or `LoadState=loaded` does not prove execution; the
monitor examines start jobs and `ActiveState`/`SubState`. A receipt is authoritative for
the child outcome, while the monitor may add a separate safety annotation.

The unit probe includes its queued job as well as `ActiveState`/`SubState`. Missing job
evidence or an unavailable manager leaves the attempt unresolved. A receipt may be
summarized while its worker is still exiting, but its active slot and recovery records
remain protected until both the worker and any queued job have ended.

Trigger's blocked-dispatch, integration-error, and uncertain-result notifications
wait for worker/job termination. Reading a receipt can record a child result while its
supervisor is still active. Backup sends its own execution result when the common
execution lifecycle ends; Trigger sends any separate recovery warning after reconciliation.

The probe requests empty properties explicitly (`show --all`): systemd prints an empty
`Job=` when no job is associated, while a queued job has a numeric ID. An omitted
property remains inconclusive. See the reviewed [systemd property printer](https://raw.githubusercontent.com/systemd/systemd/v257/src/systemctl/systemctl-show.c)
and [empty-property handling](https://raw.githubusercontent.com/systemd/systemd/v257/src/shared/bus-print-properties.c).

A `LoadState=not-found` response with reliable inactive/job-absence evidence is
confirmed absence, and a launch without a receipt then resolves to `outcome_unknown`.
If the property lookup fails, successful filtered unit and job listings must establish
absence before releasing the slot. Failed listings, an existing unit, or malformed job
output remain inconclusive; a nonzero lookup code alone never proves absence. Live
deployment validation of these paths remains open.

## Per-instance state errors

A missing active request or corrupt/incompatible/contradictory state blocks the affected
instance. The monitor preserves its evidence and writes a versioned diagnostic at
`blocked/<instance-id>.json`, separate from the original instance record so even an
unreadable record can be preserved. Other instances continue monitoring and dispatch.
Failure to persist the blocking decision still stops the monitor.

Persisted field types are validated during deserialization. Fields read as floating-point
numbers must also be finite; overflowing integers, infinity, and NaN are rejected. A
malformed type (for example, a non-string attempt ID) or invalid numeric value is treated
as corrupt state and blocks only that instance; it never authorizes a decision from
partially understood evidence.

Frozen request matchers must also satisfy the job's process-matcher schema, including
nonempty entrypoints, correctly typed predicates, and a valid regular expression.
An empty or malformed matcher blocks execution instead of treating the application as
stopped. Repair the underlying request evidence before removing the instance's block.

These blocks survive restart and do not auto-clear. After reviewing and repairing the
underlying evidence, the operator removes that instance's diagnostic and restarts the
monitor to re-evaluate it. This is manual recovery; v1 adds no repair/replay command.

Unreferenced requests with neither a launch nor a receipt cannot represent submitted
work under this protocol. Startup summarizes them before cleanup, without recreating
their pending requests. Requests that match current pending work, or have launch/receipt
evidence, remain subject to conservative reconciliation.

## Boot and configuration fingerprints

- A boot-ID change invalidates persisted running evidence and undispatched pending
  requests. On the same boot with compatible configuration, persisted verified running
  evidence plus current reliable absence re-arms a full confirmation delay.
- Confirmation uses a process-local monotonic clock. Saved UTC candidate timestamps
  never count toward a new monitor's confirmation interval. A compatible pending
  request keeps its logical ID but must pass a full fresh interval after recovery;
  ambiguous or unavailable observations restart that interval.
- A relevant configuration change invalidates obsolete undispatched requests. Active
  attempts continue using their frozen request.
- A verified restart before dispatch clears the pending request and arms the next
  lifecycle. Repeated identical observations do not recreate or replace a request.
- Disabled and removed instances retain reconciliation of their frozen active attempts,
  while obsolete pending work and retries are invalidated. Their results remain visible
  in `status`; disabling an instance never stops its worker.

## Retries

VaultKeep exit 10 records a deferred attempt; its logical request is retained for retry
no sooner than the contention delay with a new attempt UUID and another final offline
check. A proven pre-queue submission rejection is also retryable through that delayed
pending path. Other terminal failures do not auto-retry. An older blocked/deferred
result never clears or requeues a newer pending request.

A local systemctl spawn failure is distinct from a timeout after spawning. A timeout
does not prove rejection and cannot authorize another attempt. Proven rejected attempts
receive durable summaries and are counted once during recovery, so continued retries
do not accumulate protected records or repeat escalation alerts. Summaries are persisted
before eligible attempt files are removed.

The current systemctl adapter classifies a local spawn failure and start exit codes
4 (policy refusal), 5 (missing unit), and 6 (load failure) as pre-queue rejections.
It persists the code and capped diagnostic text but does not validate that text before
retrying. Numeric codes alone are not independent proof; verify these semantics on
the deployed systemd version before enabling execution. Code 2 is an integration error;
generic failures, timeouts, and unclassified codes remain inconclusive. This is a
deployment assumption and a remaining validation gap, not a diagnostic proof engine.

## Unknown execution outcomes

Unknown worker outcomes and unresolved dispatch retain their operational evidence
and require investigation without automatic replay. Trigger does not maintain
archive-consistency review flags or associate attempt IDs with backup IDs. A restart
or observation gap alone says nothing conclusive about a committed archive.

Inspect destination-derived suspect markers using `list`, full contents using
`verify`, and backup execution/cleanup diagnostics using `status`. The engine owns
policy enforcement, suspect replacement, and retention for every initiator.

## Operator review and repair

1. Inspect `vaultkeep --user --config JOB.yaml status`, the monitor journal, the selected worker unit/job,
   and private attempt records. A stopped monitor is not evidence that its workers
   stopped. If the user manager is unreachable, retain the active association and
   wait for reliable evidence rather than assuming absence.
2. For `outcome_unknown`, review VaultKeep's own archive/state and
   the job's consistency method. Archive creation and retention may have occurred
   before a failure or lost receipt. Do not replay an old request by invoking
   `exec-worker`, editing IDs, deleting receipts, or resetting all state. A later,
   independently observed lifecycle can create a new request once the old worker is
   confirmed gone; it cannot establish the old archive's safety.
3. For a durable per-instance state-error block, stop the monitor before modifying
   state and preserve a private copy of the affected records. Confirm the status of
   any worker/start job independently. Review and repair the specific underlying
   evidence, remove only that instance's `blocked/<instance-id>.json` after repair,
   then run the runtime config gate, restart, and inspect status. Restart alone does
   not clear the block. No general repair, migration, replay, or acknowledgement
   command exists; do not invent valid-looking records to bypass missing evidence.
4. For a hung or overdue backup, inspect the worker and VaultKeep diagnostics first.
   Deadlines do not kill it. Any deliberate manual termination can leave an uncertain
   result and requires subsequent review; do not release the slot or delete evidence
   simply to let another backup start.

There is no manual replay command. `outcome_unknown` requires operator review; after an unknown attempt's worker is
confirmed gone, a later independently observed lifecycle may create a new request.

[User guide](../../README.md#logs-and-troubleshooting)
