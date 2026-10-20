# Troubleshooting

Use the executable and config path selected during [installation](../../README.md#install-the-application).
The examples below assume `vaultkeep` is on your `PATH`; otherwise use the
absolute installed console-script path. For nondefault config, put `--config PATH`
before `check-config`, `monitor`, or `status`. The job YAML selects its recovery namespace.

## Installer reports `inconclusive unit properties`

Installer failures are written to stderr with an explicit `ERROR:` prefix, shown
in red on terminals. Redirected logs stay plain text. Set `NO_COLOR=1` to disable
color; terminals with `TERM=dumb` also use plain text.

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
vaultkeep --user trigger --config /absolute/path/job.yaml status
systemctl --user list-units --all 'vaultkeep-trigger-exec@*.service'
systemctl --user list-jobs --no-pager
```

Read observation and last-update ages, not just service activity or the monitor lock.
A successful `status` exit does not mean the monitor or backups are healthy. Child
diagnostics under the fixed state root can contain job output; review/redact them
before sharing. Preserve requests/receipts when investigating uncertain execution.

## `check-config` exits 2

The configuration, a matcher, or a job validation failed. The message names the
offending key or instance. Unknown keys are rejected by design; use
[configuration](configuration.md) for the supported schema.
Invalid command-line arguments also exit 2; check that global `--config` precedes
the subcommand. Job validation exit 2 from VaultKeep itself is a Trigger integration
error (exit 6), rather than a normal config failure.

## `check-config --runtime` or `monitor` exits 3

The systemd user manager is unreachable, the worker template/executable is unavailable
or incompatible, `XDG_RUNTIME_DIR` is unset/unusable, or the state location or its
existing parent is not writable. Runtime preflight fails closed because VaultKeep's lock
namespace depends on the runtime directory.

```sh
systemctl --user is-system-running
systemctl --user cat 'vaultkeep-trigger@<job-id>-<identity>.service' vaultkeep-trigger-exec@.service
test -n "$XDG_RUNTIME_DIR" && test -d "$XDG_RUNTIME_DIR" && test -O "$XDG_RUNTIME_DIR"
```

Check that both installed `ExecStart` commands have the actual executable path and
that the worker is `Type=oneshot`, `Restart=no`, `TimeoutStartSec=infinity`. Do not
print the entire manager environment into logs to debug bus access. Failed config
validation should be fixed before restarting; the monitor unit retries failed starts.

## A second monitor refuses to start (exit 3)

Another monitor holds `monitor.lock`. The message names the owning PID. `status` reports
lock ownership and observation age independently; a held lock does not by itself prove
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

## A backup is "blocked"

A pending request is waiting behind an active or unresolved attempt for the same
instance. `status` shows the blocking attempt ID and wait age. Trigger does not
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

A user service may lack `DBUS_SESSION_BUS_ADDRESS`. Check this job's notification
enablement, the configured command, and the graphical session. The notifier silently
ignores delivery errors and does not log success/failure. See
[session setup](notifications.md#session-bus).

## A result is `result_unknown`

VaultKeep exited zero without a recognized trailing `run: created`/`run: unchanged`
marker. This is recorded as successful execution with an unknown result and makes no
new-archive claim.

## `outcome_unknown` or `review required`

`outcome_unknown` means the worker is confirmed absent but execution completion could
not be established. `review_required` is a separate safety warning, for example after
a restart or monitoring gap overlapped execution. Neither means a backup is safe to
replay or an archive is safe to discard. A later `unchanged` result does not clear a
consistency warning. Preserve state and follow [manual review](recovery.md#operator-review-and-repair).

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
