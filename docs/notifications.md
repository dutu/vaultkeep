# Desktop notifications

Notifications are optional and configured per job in a top-level YAML section:

```yaml
notifications:
  command: notify-send
  timeout_ms: 5000
  backup:
    enabled: true
  trigger:
    enabled: true
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `backup.enabled` | `false` | Backup start, execution phases, and authoritative results. |
| `trigger.enabled` | `false` | Application monitoring, dispatch, and recovery alerts. |
| `command` | `notify-send` | Shared delivery executable, invoked directly without a shell. |
| `timeout_ms` | `5000` | Positive integer; shared discovery and delivery budget per message. |

Both categories work independently. Backup alerts work for manual, scheduled, and
triggered executions, including jobs with no automatic initiation enabled. Trigger
alerts require the job's monitor to be active, and work with Backup alerts disabled.
Schedule only arranges execution; it has no separate notification category.

## Enable notifications

Backup notifications apply to manual, scheduled, and triggered executions. Trigger
notifications cover application monitoring, dispatch, and recovery. Each category
is optional and defaults to disabled.

1. Install the desktop notification command if needed:

   ```bash
   sudo apt-get install libnotify-bin
   ```

   From the backup owner's graphical session, test delivery:

   ```bash
   notify-send "Vaultkeep" "Desktop notifications are available"
   ```

   Expect a desktop notification unless desktop settings suppress it. The desktop
   must run as the backup owner in the same host or container. Session discovery
   uses `busctl`, supplied by systemd, and also supports NoMachine sessions in LXC.

2. Add the top-level YAML section shown above to the job, enabling the categories
   you want. Both categories default to disabled.

   `backup.enabled` works with manual-only jobs and scheduled jobs as well as Trigger.
   `trigger.enabled` here controls Trigger alerts; enabling application monitoring
   still requires the job's separate `trigger.enabled` setting and an installed monitor.
   The delivery command and timeout apply to both notification categories.

3. Set `job_config` to the actual YAML path, then validate as its owner. For example:

   ```bash
   job_config="${XDG_CONFIG_HOME:-$HOME/.config}/vaultkeep/jobs/app.yaml"
   vaultkeep --user --config "$job_config" validate
   ```

   Future backups read these settings automatically. If this job has an installed
   Trigger monitor, also apply the change to it:

   ```bash
   vaultkeep --user --config "$job_config" trigger update
   ```

## Backup notifications

Backup generates messages directly from its common execution lifecycle:

- `VaultKeep: Backup started` after acquiring the job lock and establishing execution.
  A rejected lock acquisition produces no execution notifications.
- `VaultKeep: Backup running` on phase changes, using existing workflow phase names.
  Unchanged phases refresh on the ten-second execution heartbeat with elapsed time,
  such as `Checking existing backups (20s)`. The first phase update omits elapsed time.
- `VaultKeep: Backup completed` with the execution result: new archive created,
  sources unchanged, failed, interrupted, or outcome unknown. Failures include the
  existing concise execution diagnostic. The result includes the local completion time.

Per-file backups have one job-level start and result, rather than one stream per
file. All their progress updates are limited to one per ten seconds because the
workflow repeats phases for each file. The overall result includes a failure reason
if any file failed. Short jobs can finish before any progress update is displayed.

Completion messages reflect the result known by the executing process even if
optional progress or completion-record storage is unavailable. They do not replace
`vaultkeep status` or repair missing evidence. When a later execution acquires the
same job lock and discovers an unfinished prior execution record, it reports that
prior outcome as unknown, using the time uncertainty was established. A process that
can handle interruption reports it; abrupt termination, including SIGKILL or host
shutdown, can prevent a final alert.

## Trigger notifications

Trigger alerts cover its own observations and orchestration:

- Changes to the `Application` state shown by `vaultkeep status`: `RUNNING`,
  `CONFIRMING STOP`, `HAS STOPPED`, `NOT RUNNING`, or `UNKNOWN`. The first fresh
  observation after startup establishes the baseline without an alert; unchanged
  observations do not resend it.
- One `Dispatch requested` message per logical request after confirmed application
  shutdown. It means dispatch was requested; it does not establish execution start.
  Contention and proven-rejection retries do not repeat this alert.
- `Dispatch blocked` for a refused final process recheck, `Dispatch failed` for an
  integration error, and `Dispatch result unknown` for a child without a recognized
  result. These receipt-based alerts wait until the worker and any queued job end.
- Recovery warnings when an observation gap or restart overlapped possible execution,
  or a worker is absent without completion evidence. These are orchestration warnings
  separate from Backup's execution result.
- One advisory overdue alert, and one blocked-wait alert when a pending request waits
  behind an attempt without a deadline. Alerts never stop workers or free active slots.

Trigger keeps reconciling receipts and protecting recovery evidence for every outcome.
It does not read Backup progress to generate notifications or announce ordinary backup
success, unchanged sources, or execution failure. Pending-request supersession remains
recorded in lifecycle state/logs without a dedicated desktop alert.

### Deduplication and escalation

Trigger persists existing delivery-decision markers so receipt recovery does not resend
known orchestration transitions. Markers follow detailed attempt retention, preserving
those still referenced by active, pending, blocking, or rejection recovery. Application
state deduplication stays in monitor memory. Delivery is best-effort, not exactly-once;
a crash around delivery can lose an alert. No notification state is added to backup
execution history.

At five consecutive proven submission rejections for the same current request, Trigger
makes at most one escalation attempt and suppresses further rejection reminders while
retrying on the delayed pending path. Proven acceptance resets the count without
clearing that request's decisions. A new logical request starts fresh. Overdue and
recovery alerts remain deliverable. This threshold and the
[blocked-wait timer](trigger/service-operation.md#internal-constants) are internal constants.

## Delivery and replacement

Backup and Trigger use the same shared delivery implementation. Delivery is asynchronous
and ordered within each process, through one worker with bounded command timeouts.
Missing tools, unavailable sessions, timeouts, and nonzero exits never determine a
backup, monitoring, or dispatch result. Delivery does not update activity, successful
backup history, scheduling, or Trigger recovery state.

With `notify-send`, including an absolute path ending in `notify-send`, Backup start,
progress, and final-result messages share one replacement identity per execution.
`--print-id` and `--replace-id` update the existing notification, and
`--expire-time=0` requests that it remain until dismissed, including after completion.
A later execution starts a new notification. Trigger uses separate identities for
orchestration outcomes; its application and request alerts use the desktop's default
expiration. The desktop controls actual visibility, expiration, and whether updates
create another popup. Do Not Disturb can suppress popups.

Replacement IDs are process-local and cleared when the selected session bus changes.
Queued progress can be replaced by newer progress or the final result so a slow
notification command does not create a backlog of obsolete phases. A dismissed or
unavailable desktop does not stop a backup.

Short-lived backup processes keep delivery asynchronous during execution, then drain
at shutdown for at most twice `timeout_ms` (ten seconds by default), after releasing
the job lock. This allows an in-flight message and the final result their normal
budgets. The shared deadline bounds any queued delivery; remaining messages can be
dropped when it expires. No service guarantees delivery after process death.

Custom commands receive only title and body as two arguments, with no extra options.
They receive phase, elapsed-time, result, and Trigger messages as separate invocations;
notification replacement and expiration requests are available only with `notify-send`.

## Session bus

With `notify-send`, the notifier resolves the desktop notification bus automatically
for each delivery. It first looks for a running notification service on the inherited
`DBUS_SESSION_BUS_ADDRESS`, then the standard `$XDG_RUNTIME_DIR/bus`. If neither has
a running notification service, it reads
graphical process environments belonging to the backup account and selects a single
usable notification bus. This supports session-specific `/tmp/dbus-...` buses, such
as the NoMachine desktop inside an LXC container, without manual imports or a process
restart after the desktop session changes.

Discovery accepts local Unix sockets only, checks the bus peer's user identity, and
uses systemd's `busctl` to verify that `org.freedesktop.Notifications` is running or
available for activation. It ignores processes without `DISPLAY` or `WAYLAND_DISPLAY`,
other users, unreadable process environments, and stale/unusable buses. If fallback
discovery finds multiple usable desktop buses, it skips delivery and logs the
ambiguity. A running service on the inherited or standard user bus takes precedence
over discovery. If no running daemon is found, an activatable service can be used,
preferring the inherited/standard user bus.
No session address is saved to disk, no new bus is created, and the systemd manager's
environment is not changed. Only the notification child receives the selected address;
backup operations and custom notification commands retain their existing environments.

Discovery and delivery share `timeout_ms`. Discovery takes at most half that budget,
capped at two seconds, and individual probes are bounded. Both run in the notification
worker and never block backup monitoring. Each new message resolves the session again;
an unavailable desktop does not queue notifications for a later login.

Missing commands, unavailable or ambiguous sessions, delivery timeouts, invalid
notification IDs, and nonzero exits are logged as warnings in the backup log or monitor journal.
Command failure details are limited to one bounded line. Delivery remains best-effort;
successful command execution does not confirm that the desktop displayed a popup.
See [troubleshooting](#troubleshooting).

Notifications target a desktop running as the backup owner in the same environment.
For LXC, that means a desktop inside the container. Routing to a host or SSH client's
desktop requires separate integration. A service running under another account,
including root, does not gain access to the backup owner's desktop automatically.

## Troubleshooting

Check `notifications.backup.enabled` for execution alerts or
`notifications.trigger.enabled` for monitoring/dispatch alerts, plus `notify-send` and
systemd's `busctl`.
The desktop must run as the backup owner, in the same host/container as the backup or monitor.
For NoMachine, test from a terminal opened inside that desktop:

```sh
id -un
printf 'Desktop bus: %s\n' "${DBUS_SESSION_BUS_ADDRESS:-unset}"
notify-send "Vaultkeep" "Desktop notification test"
```

Notification delivery discovers session-specific buses automatically; an absent
`/run/user/<uid>/bus` does not by itself prevent delivery. After installing an updated
Vaultkeep, validate the job and run `trigger update` to restart its installed monitor.

Inspect the backup journal/execution log for Backup delivery warnings. For Trigger
alerts, inspect warnings and confirm the monitor is running:

```sh
systemctl --user list-units --all 'vaultkeep-trigger@*.service'
journalctl --user -u 'vaultkeep-trigger@*.service' -n 100 --no-pager
```

Warnings identify missing tools, an unavailable graphical bus, ambiguous desktop
sessions, timeouts, and command failures. Fallback discovery needs readable
same-account graphical process environments; container or `/proc` restrictions may
prevent this. When several desktop buses exist, a working inherited
`DBUS_SESSION_BUS_ADDRESS` can select the intended session explicitly. Popup visibility
still follows the desktop's notification settings. See [session selection](#session-bus).

[User guide](../README.md#desktop-notifications)

A reliable new archive with a cleanup failure still sends a successful backup
completion with the cleanup diagnostic. Trigger does not duplicate backup-health
alerts or add archive-review warnings for application restarts or monitoring gaps.
