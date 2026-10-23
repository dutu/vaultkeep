# Troubleshooting

Use the executable and config path selected during [installation](../../README.md#install-the-application).
The examples below assume `vaultkeep` is on your `PATH`; otherwise use the
absolute installed console-script path. For nondefault config, put `--config PATH`
before `validate`, or before Trigger's `monitor` or `status`. The job YAML selects its recovery namespace.

## Installer candidate fails `--version`

The installer checks the candidate in staging before activating it. If a command
fails, the installer marks its stage `FAILED` and shows recent diagnostic context.
The complete ordered subprocess output is retained in the private transcript named
by `Diagnostics:`; inspect that file if the displayed context is insufficient. A successful `vaultkeep --version` after a staging failure
checks the existing installation and does not mean the candidate passed.

For `ModuleNotFoundError: No module named 'vaultkeep.cli.output'`, the source
checkout is incomplete. From the checkout directory, check:

```sh
test -f src/vaultkeep/cli/output.py
git ls-files --error-unmatch src/vaultkeep/cli/output.py
```

Both commands should succeed. If the file exists locally but is absent from Git,
include it in the source commit before updating the target host. With a complete
checkout, repeat `sudo ./install.sh install`. The installer pauses existing
launchers, refuses active backup work, refreshes managed units, validates jobs, and
resumes only previously active launchers after success or completed rollback.

## Installer reports `inconclusive unit properties`

The normal installer report names the failed stage and shows `Result: FAILED`.
Detailed subprocess errors go to stderr and remain in the diagnostic transcript;
those diagnostic messages may use severity prefixes. Colors highlight status values.
Redirected output stays plain text. Set `NO_COLOR=1` or add `--no-color` after the
install/update/uninstall mode to disable color; `TERM=dumb` also uses plain text.
See [terminal output](../../README.md#terminal-output).

Before the worker template has been installed, the user manager can report the
worker instance as `not-found`, `inactive`, and `dead` without an `ExecStart`
property. This is normal before Trigger's shared units are installed. The corrected
installer accepts that confirmed absence; it still requires command properties for
loaded services and refuses failed or incomplete manager queries.

Inspect the response as the affected account:

```sh
systemctl --user show vaultkeep-trigger-exec@installer-check.service --all --no-pager \
    --property=LoadState,ActiveState,SubState,FragmentPath,DropInPaths,ExecStart,UnitFileState
```

If an older installer refuses that absent-unit response, use the corrected checkout
and repeat `sudo ./install.sh install`. This preflight refusal leaves
the existing core deployment active; no unit or state cleanup is needed. Other
responses, especially active work or an unreachable user manager, require resolving
the reported condition before retrying.

## First checks

```sh
systemctl --user status --no-pager 'vaultkeep-trigger@<job-id>-<identity>.service'
journalctl --user -u 'vaultkeep-trigger@<job-id>-<identity>.service' -n 100 --no-pager
vaultkeep --user --config /absolute/path/job.yaml trigger status
systemctl --user list-units --all 'vaultkeep-trigger-exec@*.service'
systemctl --user list-jobs --no-pager
```

Read the monitor update age and monitoring-health section, not just service activity or the lock.
A successful `status` exit does not mean the monitor or backups are healthy. Child
diagnostics under the fixed state root can contain job output; review/redact them
before sharing. Preserve requests/receipts when investigating uncertain execution.

## Validation reports failed or incomplete setup

Run the complete check as the application owner:

```sh
vaultkeep --user --config /absolute/path/job.yaml validate
```

Read each named check. A missing monitor has `Monitor service` set to `NOT INSTALLED`
and `Result: SETUP INCOMPLETE`; correct any runtime failures and explicitly run
`vaultkeep --user --config /absolute/path/job.yaml trigger install`, then validate again.
For an installed monitor, disabled/inactive/failed systemd state, stale monitor updates,
a configuration fingerprint mismatch, recovery blocks/review warnings, and recorded
backup failures require attention. Check its exact service name and journal. Validation
only reports evidence; it does not repair it. A schema-only pass skips these checks.

Trigger requires an accessible systemd user manager, the installed worker template and
executable, an account-owned `XDG_RUNTIME_DIR`, and writable state/runtime locations.

```sh
systemctl --user is-system-running
systemctl --user cat 'vaultkeep-trigger@<job-id>-<identity>.service' vaultkeep-trigger-exec@.service
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

## `status` exits 2

Persisted state is corrupt or has an incompatible schema version. Trigger does not
migrate, repair, or reinitialize it; the diagnosis material is preserved for review.
Other readable instances are still shown by the status query.
An unknown selected ID also exits 2 when other records exist. IDs come from persisted
state, so an enabled instance may not be recorded until the monitor first runs.

## An instance reports `dispatch blocked`

A state error has durably blocked that instance without stopping independent instances.
The reason is stored in `<state-root>/blocked/<instance-id>.json`; the original instance
and attempt records are preserved. Review and repair the underlying evidence before
removing only that blocking diagnostic and restarting the monitor. Restart alone does
not clear the block. See [per-instance recovery](recovery.md#per-instance-state-errors).

## No backup was requested

Check that the instance is enabled; the shipped example has all entries disabled.
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

Run `vaultkeep --user --config /absolute/path/job.yaml trigger status`. `Monitor:
RUNNING` describes the shutdown monitor. `Current activity` describes the backup
separately. `RUNNING: Creating and compressing archive (45s / 1m 31s elapsed)` reports
the actual backup phase, time in that phase, and total backup command time. `PREPARING`
reports the worker's application/executable checks; `FINISHING` awaits a recorded result.
Long phases refresh their progress every 10 seconds. Missing or stale progress and an
unavailable reporting process produce `UNKNOWN`; inspect the worker journal. Attempts
started before upgrading may lack progress. systemd's oneshot service state alone cannot
distinguish backup phases. `COMPLETED` means a receipt records child completion
while the worker awaits reconciliation. `STOPPED` or `FAILED` with an unresolved
attempt is not a backup success claim, and `UNKNOWN`
means execution could not be confirmed. These are snapshots, not archive progress.

Set `logging.level: debug` in the job and repeat `status` for exact worker units and
raw service states alongside the readable report.

Use the exact `Worker service` from that report to inspect the attempt:

```sh
systemctl --user status --no-pager '<worker-service>'
journalctl --user -u '<worker-service>' -n 100 --no-pager
```

The monitor journal records detection and request events. Backup child output is
retained separately in the attempt's private `log.txt` under the reported Trigger
state root; the worker journal may contain only its final summary. These reports
show workflow phases and elapsed time, without a live archive progress percentage.
Preserve recovery evidence when
investigating an unresolved attempt.

## Why did a backup fail?

1. Run `vaultkeep --user --config /absolute/path/job.yaml trigger status`.
   Read `Previous attempt`, the original exit code or signal, `Reason`, and the
   existing `Log` path when available. A failed finalization does not establish
   corruption. Current work and the previous result are reported separately.

2. For full context, set the job's `logging.level: debug` and rerun `status`.
   Read `Attempt diagnostics`, then copy `Diagnostics path`:

   ```sh
   tail -n 40 '<diagnostics-path>'
   ```

   Debug output includes at most ten nonblank lines, each capped at 500 characters.
   The private log retains up to 256 KiB of combined child output. Restore the desired
   logging level after diagnosis; review and redact output before sharing.

3. If diagnostics were not recorded or are no longer retained, use the reported attempt
   ID and job identity from debug diagnostics to inspect the worker journal:

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

Check `trigger.notifications.enabled: true`, `notify-send`, and systemd's `busctl`.
The desktop must run as the backup owner, in the same host/container as the monitor.
For NoMachine, test from a terminal opened inside that desktop:

```sh
id -un
printf 'Desktop bus: %s\n' "${DBUS_SESSION_BUS_ADDRESS:-unset}"
notify-send "Vaultkeep" "Desktop notification test"
```

The monitor discovers session-specific buses automatically; an absent
`/run/user/<uid>/bus` does not by itself prevent delivery. After installing an updated
Vaultkeep, validate the job and run `trigger update` to restart its installed monitor.

Inspect warnings and confirm the monitor is running:

```sh
systemctl --user list-units --all 'vaultkeep-trigger@*.service'
journalctl --user -u 'vaultkeep-trigger@*.service' -n 100 --no-pager
```

Warnings identify missing tools, an unavailable graphical bus, ambiguous desktop
sessions, timeouts, and command failures. Fallback discovery needs readable
same-account graphical process environments; container or `/proc` restrictions may
prevent this. When several desktop buses exist, a working inherited
`DBUS_SESSION_BUS_ADDRESS` can select the intended session explicitly. Popup visibility
still follows the desktop's notification settings. See [session selection](notifications.md#session-bus).

## A result is `result_unknown`

VaultKeep exited zero without a recognized trailing `run: created`/`run: unchanged`
marker. This is recorded as successful execution with an unknown result and makes no
new-archive claim.

## `outcome_unknown` or `review required`

`outcome_unknown` means the worker is confirmed absent but execution completion could
not be established. `review_required` is a separate safety warning, for example after
a restart or monitoring gap overlapped execution. Neither means a backup is safe to
replay or an archive is safe to discard. A later `unchanged` result does not clear a
consistency warning; neither does a later newly created archive. A final-check refusal
can resolve its own execution risk because the child never ran, while keeping the
historical annotation. See [warning evidence](recovery.md#execution-review-warnings).
Preserve state and follow [manual review](recovery.md#operator-review-and-repair).

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
