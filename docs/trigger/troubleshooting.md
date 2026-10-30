# Troubleshooting

Use the executable and config path selected during [installation](../../README.md#install-the-application).
The examples below assume `vaultkeep` is on your `PATH`; otherwise use the
absolute installed console-script path. For nondefault config, put `--config PATH`
before `validate`, `status`, or `trigger monitor`. The job YAML selects its recovery namespace.

## Installer candidate fails `--version`

See [candidate validation diagnosis](../troubleshooting.md#installer-candidate-fails---version).

## Installer reports `inconclusive unit properties`

See [installer unit-property diagnosis](../troubleshooting.md#installer-reports-inconclusive-unit-properties).

## First checks

```sh
systemctl --user status --no-pager 'vaultkeep-trigger@<yaml-filename>-<path-hash>.service'
journalctl --user -u 'vaultkeep-trigger@<yaml-filename>-<path-hash>.service' -n 100 --no-pager
vaultkeep --user --config /absolute/path/job.yaml status
systemctl --user list-units --all 'vaultkeep-trigger-exec@*.service'
systemctl --user list-jobs --no-pager
```

Read the monitor update age and monitoring-health section, not just service activity or the lock.
A successful `status` exit does not mean the monitor or backups are healthy. Child
diagnostics under the fixed state root can contain job output; review/redact them
before sharing. Preserve requests/receipts when investigating uncertain execution.

## Edited YAML or legacy monitor identity

Use `status --verbose` to compare the installed execution identity/parameters with the
current desired selection. `UPDATE REQUIRED` or an execution-identity startup error
requires `trigger update`; restarting systemd alone does not authorize a switch.
If validation reports the mismatch, correct other configuration/runtime errors first,
then update and validate again. Active backups, pending/unreconciled work, corrupt
state, and unresolved safety warnings must be resolved before the transition.

A single verified legacy service migrates during `trigger update` or `trigger install`.
Multiple services referencing one path are a conflict. Preserve their definitions and
recovery directories, inspect each unit's original command and worker state, and stop
and retire each only after its work is safely resolved. Do not delete state or simply
start another monitor. Status/validation never perform migration.

To disable or remove after invalid edits or deletion, use the original canonical path:

```sh
vaultkeep --user --config /original/canonical/path/job.yaml trigger remove
```

The command recovers job ID, parameters, and execution namespace from the verified unit.
Missing or inconsistent metadata, unreviewed overrides, or unverifiable effective units
fail closed. Review `systemctl --user cat <unit>` and the error; do not recreate obsolete
YAML or guess an execution namespace. For a retargeted/missing symlink, use its original
resolved target. Moving YAML is a separate installation, not an automatic rename.

After a failed migration/systemd operation, inspect `systemctl --user list-unit-files
'vaultkeep-trigger@*.service'`, the unit content, and journal. The monitor may remain
stopped. Correct the manager failure, review/reload restored files if requested, and
retry update. If stopping the new monitor failed, its definition remains available;
resolve that failure before attempting another replacement.

## Validation reports failed or incomplete setup

Run the complete check as the application owner:

```sh
vaultkeep --user --config /absolute/path/job.yaml validate
```

Read each named check. A missing monitor has `Monitor service` set to `NOT INSTALLED`
and `Result: SETUP INCOMPLETE`; correct any runtime failures and explicitly run
`vaultkeep --user --config /absolute/path/job.yaml trigger install`, then validate again.
For an installed monitor, disabled/inactive/failed systemd state, stale monitor updates,
a configuration fingerprint mismatch, recovery blocks, and recorded
backup failures require attention. Check its exact service name and journal. Validation
only reports evidence; it does not repair it. A schema-only pass skips these checks.

Trigger requires an accessible systemd user manager, the installed worker template and
executable, an account-owned `XDG_RUNTIME_DIR`, and writable state/runtime locations.

```sh
systemctl --user is-system-running
systemctl --user cat 'vaultkeep-trigger@<yaml-filename>-<path-hash>.service' vaultkeep-trigger-exec@.service
test -n "$XDG_RUNTIME_DIR" && test -d "$XDG_RUNTIME_DIR" && test -O "$XDG_RUNTIME_DIR"
```

Check that both installed `ExecStart` commands have the actual executable path and
that the worker is `Type=oneshot`, `Restart=no`, `TimeoutStartSec=infinity`. Failed config
validation should be fixed before restarting; the monitor unit retries failed starts.

## A second monitor refuses to start (exit 3)

Another monitor holds `monitor.lock`. The message names the owning PID. `status` reports
lock ownership and update age together; a held lock does not by itself prove
responsive monitoring.
Do not remove a held lock file to bypass ownership. Stop the extra monitor normally
and keep workers independent; unlinking a live lock can permit two monitor owners.

<a id="status-exits-2"></a>
## Status reports an inspection or configuration error

The unified `vaultkeep --user --config JOB.yaml status` returns 15 for inspection
errors and 3 for invalid configuration/parameters. Corrupt or incompatible recovery
state remains explicit in the available report; preserve the records for review.
Exit 0 means readable collection, including unknown activity or failed previous work.
There is no separate Trigger status command or instance-selection argument.
See [status semantics](../status.md#json) and [recovery](recovery.md).

## An instance reports `dispatch blocked`

A state error has durably blocked that instance without stopping independent instances.
The reason is stored in `<state-root>/blocked/<instance-id>.json`; the original instance
and attempt records are preserved. Review and repair the underlying evidence before
removing only that blocking diagnostic and restarting the monitor. Restart alone does
not clear the block. See [per-instance recovery](recovery.md#per-instance-state-errors).

## No backup was requested

Check that the instance is enabled; the shipped examples have `trigger.enabled: false`.
Only verified `running` evidence arms detection, and the app must have been observed
running before its shutdown. Starting the monitor while an app is already stopped
creates no request. An `ambiguous` or `inspection_unavailable` observation blocks
automatic dispatch — see [process detection](process-detection.md).
There is no directory-default inference or built-in writer discovery. Config checks
cannot turn an illustrative matcher into a verified preset. Each job uses its own poll interval; synchronous service probes can add delays.
See the application guides for [Bisq](apps/bisq.md),
[Electrum](apps/electrum.md), and [Wasabi](apps/wasabi.md).

## Application status changes during shutdown

`RUNNING` → `CONFIRMING STOP` → `HAS STOPPED` is the normal shutdown sequence.
After first observing the application absent, the monitor waits for continuous reliable
absence for `trigger.confirm_seconds` (default: 5 seconds), then confirms at a subsequent
poll. Backup dispatch waits for confirmation. A restart or inconclusive observation
interrupts confirmation.

`NOT RUNNING` means the monitor reliably observes the application stopped without
a shutdown confirmation in progress. This includes starting or restarting the monitor
after a completed backup while the application remains stopped. Historical requests,
backup attempts, and retained recovery warnings do not make that observation unknown.
`HAS STOPPED` requires a shutdown transition confirmed in the current monitor session.
`NOT RUNNING` alone does not request another backup or clear recovery warnings.

If `Monitor` becomes `UNKNOWN` with `updates stale or unavailable`, no fresh update
has been recorded within twice `trigger.poll_seconds` (default: 4 seconds), or its
timestamp is missing or invalid. `Application` also becomes `UNKNOWN` because its
observation cannot be trusted as current. This differs from shutdown confirmation.
Synchronous systemd probes can delay the monitor; an individual probe can wait up to
15 seconds. Timed-out probes log `systemd probe timed out` with the command that was
being queried. A later `RUNNING` means updates resumed, but does not establish what caused
the gap. Use the [first checks](#first-checks) to inspect the monitor journal around
that time. A previous attempt's failure reason describes that attempt and does not by
itself explain a monitoring gap.

## Is a backup running?

Run `vaultkeep --user --config /absolute/path/job.yaml status`. The Backup section
reports the execution phase and both elapsed times. It applies equally to manual,
scheduled, and triggered runs. Monitor health and application detection are separate
fields under Trigger. A reliably free job lock means `IDLE`, even without execution
history. `UNKNOWN` activity requires investigation; stale progress, a released
lock, or a systemd service state alone cannot establish completion.

See the [status reference](../status.md). Use `status --verbose` to include exact
monitor units and state paths. Backup failures name the engine's private execution
log. Trigger worker diagnostics remain in the attempt's `log.txt` for dispatch and
recovery investigation. Preserve both when investigating uncertain dispatches.

## Why did a backup fail?

1. Run `vaultkeep --user --config /absolute/path/job.yaml status`.
   Read `Previous backup`, the original exit code or signal, `Reason`, and the
   existing `Log` path when available. A failed finalization does not establish
   corruption. Current work and the previous result are reported separately.

2. For full context, rerun `status --verbose`.
   Read `Backup diagnostics`, then use the displayed `Log` path:

   ```sh
   tail -n 40 '<diagnostics-path>'
   ```

   Verbose output includes at most ten nonblank log lines, each capped at 500 characters.
   The backup log rotates at 64 KiB with one retained segment. Trigger additionally
   retains up to 256 KiB of combined child output per dispatch attempt. Review and
   redact output before sharing.

3. If diagnostics were not recorded or are no longer retained, use the reported attempt
   ID and job identity from verbose diagnostics to inspect the worker journal:

   ```sh
   journalctl --user -u 'vaultkeep-trigger-exec@<identity>_<attempt-uuid>.service' --no-pager
   ```

   Missing or unreadable diagnostics do not change the saved outcome or establish
   a cause. Summary records can outlive detailed logs; preserve recovery state.

## A backup is "blocked"

A pending request is waiting behind an active or unresolved attempt for the same
instance. `status` shows the pending backup waiting for the previous attempt;
debug output identifies the active attempt and its worker unit. Trigger does not
terminate the blocking worker; see [recovery](recovery.md) and
[service operation](service-operation.md).

A systemd oneshot worker can remain `activating` for the entire backup. The monitor
waits for both worker and queued-start termination even if a receipt already exists.
An overdue notification does not kill it or free its slot. Inspect its journal and
private diagnostics, then follow [operator review](recovery.md#operator-review-and-repair).

## Repeated submission rejections or contention

Lock contention (VaultKeep exit 10) and classified pre-queue rejections retry after
at least 30 seconds, only while the request remains current. Other terminal failures
do not auto-retry. Check unit installation/policy and external timers/direct runs
sharing the job. At five consecutive classified rejections there is one best-effort
escalation; status retains the count. See [retry evidence](recovery.md#retries).

## Dispatch is held by the history bound

This is logged when too many recovery-protected records remain. Preserve those
records and resolve outstanding associations before expecting new dispatch. It is
separate from the durable state-error block and is not currently printed as a
dedicated status reason. See [retention](service-operation.md#internal-constants).

## Notifications do not appear

Follow the [notification troubleshooting procedure](../notifications.md#troubleshooting).
Backup and Trigger alerts have separate enablement flags; neither is required for
execution. Check the backup log for execution alerts and monitor journal for Trigger
alerts. Delivery failures do not change backup or monitoring results.

## A result is `result_unknown`

VaultKeep exited zero without a recognized trailing `run: created`/`run: unchanged`
marker. This is recorded as successful execution with an unknown result and makes no
new-archive claim.

## `outcome_unknown` or `review required`

`outcome_unknown` means the worker is confirmed absent but completion could not be
established. Preserve dispatch records and follow [manual review](recovery.md#operator-review-and-repair).
Never replay that attempt or infer archive health from its timestamp. Trigger has no
archive-review flags; the engine enforces configured consistency and records
suspect classification in the destination.

## Runtime config checks pass but backup fails

Full validation checks destination creation, writing, atomic directory rename, and
cleanup using a temporary probe. Permissions or mount conditions can change afterward.
It does not check configured hook executables.
The service's selected XDG paths, fixed child `PATH`, credentials, and helper locations
can differ from an interactive shell. Requests freeze paths and parameters, not job
contents. Review the [child environment](service-operation.md#state-and-runtime-locations),
receipt's original exit/signal, and job diagnostics; Trigger does not reinterpret the
job or repair VaultKeep state.

[User guide](../../README.md#logs-and-troubleshooting)
