# Notifications

Optional desktop notifications are configured per job under YAML `trigger.notifications`
and delivered best-effort by the monitor through `notify-send` with a bounded timeout.
The configured command is invoked directly, without a shell. Trigger does not block
the monitoring loop waiting for delivery. Deliveries run in order through one
background worker, with a timeout per command. Each job has its own notification
settings. Notifications remain disabled unless `trigger.notifications.enabled` is
`true`.

## What is delivered

- One start notification per logical request. A contention retry or a proven-rejection
  retry creates a new attempt but does **not** repeat the start alert.
- Current backup activity, including preparation, creating/compressing the archive,
  verification, and finishing while awaiting the result. Updates follow observed
  changes during normal monitor polling. The first notification for each phase omits
  elapsed time. An unchanged phase is resent every ten
  seconds with its elapsed time, for example `Checking existing backups (20s)`.
  The timer resets when the phase changes. Updates occur on the first monitor poll
  after the interval; short phases between polls may not be displayed.
- Changes to the Trigger `Application` state shown by `vaultkeep status`:
  `RUNNING`, `CONFIRMING STOP`, `HAS STOPPED`, `NOT YET DETECTED`, or `UNKNOWN`.
  The first fresh observation after monitor startup establishes the baseline without
  an alert. Later changes show the previous and new states; repeated observations
  of the same state do not resend an alert.
- A terminal result after the worker and any queued job have ended: created, unchanged,
  result_unknown, failure, or blocked. A receipt arriving earlier is recorded without
  prematurely announcing completion. The result includes the completion date and time
  in the monitor's local timezone. For an unknown outcome without a receipt, it shows
  the time the monitor established that outcome. The `review required` suffix applies
  only to warnings about that attempt. Older unresolved warnings remain in retained
  records and debug status without qualifying later results.
- `review_required`, when an observation gap or restart overlapped execution.
- `outcome_unknown`, when a worker is absent with no completion evidence.
- An overdue notification when an attempt exceeds its advisory deadline.
- A blocked-wait notification. Pending-request supersession is recorded in lifecycle
  state/logs; a dedicated supersession desktop alert is not currently implemented.

## Updating the activity notification

With `notify-send` (including an absolute path whose filename is `notify-send`),
the start, phase, and final-result messages share one notification per instance.
Trigger uses `--print-id` and `--replace-id` to update it. It requests no automatic
expiration (`--expire-time=0`), including for the final result, which remains until
you dismiss it. A later backup starts a new notification, preserving earlier final
results. The desktop controls actual visibility, expiration, and whether
updates produce another popup. Do Not Disturb settings can suppress popups.
Application-state changes and other alerts, such as overdue and review-required
warnings, remain separate and use the desktop's default timeout.

Replacement IDs, elapsed timers, and the last displayed phase and application state
are kept only in monitor memory. A monitor restart can create a new activity
notification for an ongoing backup. An
unavailable desktop, failed delivery, or user dismissal does not stop a backup.
Changing the selected session bus clears replacement IDs so another desktop does
not receive IDs allocated by the previous session.

Activity consumes the common backup engine's job status, including its lock ownership,
process identity, and progress freshness checks. Invalid,
stale, or mismatched records and unavailable processes do not establish a running
phase. If previously displayed activity becomes unavailable, the notification reports
that progress is unavailable while awaiting the result. A finished progress record
means only that the workflow ended: completion is announced after a receipt is
reconciled and the worker and any queued job have ended.

Custom notification commands retain their existing interface: title and body as two
arguments, with no extra options. They receive phase changes, ten-second elapsed
updates, application-state changes, and timestamped results as individual messages;
notification replacement and expiration requests are available only with `notify-send`.

## Deduplication and escalation

- Delivery-decision markers are persisted so receipt recovery does not resend known
  lifecycle transitions. Phase and application-state deduplication are in memory and
  do not add durable state. Delivery is best-effort, not exactly-once; a crash around
  delivery can lose an alert.
- Markers follow detailed attempt retention. Cleanup removes obsolete markers while
  preserving decisions for retained records and active/pending/rejection recovery,
  including wait alerts that must not repeat behind the same blocking attempt.
- At the internal threshold of five consecutive **proven** submission rejections for the
  same current request, the monitor makes at most one best-effort escalation attempt,
  then suppresses further rejection reminders for that request while retries continue
  on the delayed pending path. Terminal, overdue, and compromised-safety alerts are
  still delivered.
- A proven acceptance resets the consecutive count without clearing the request's
  start/escalation decisions. A new logical request starts with count zero and fresh
  decisions.

The escalation threshold and the [blocked-wait](service-operation.md) timer are internal
constants and are not configuration.

## Session bus

With `notify-send`, the monitor resolves the desktop notification bus automatically
for each delivery. It first looks for a running notification service on the inherited
`DBUS_SESSION_BUS_ADDRESS`, then the standard `$XDG_RUNTIME_DIR/bus`. If neither has
a running notification service, it reads
graphical process environments belonging to the backup account and selects a single
usable notification bus. This supports session-specific `/tmp/dbus-...` buses, such
as the NoMachine desktop inside an LXC container, without manual imports or a monitor
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
backup workers and custom notification commands retain their existing environments.

Discovery and delivery share `timeout_ms`. Discovery takes at most half that budget,
capped at two seconds, and individual probes are bounded. Both run in the notification
worker and never block backup monitoring. Each new message resolves the session again;
an unavailable desktop does not queue notifications for a later login.

Missing commands, unavailable or ambiguous sessions, delivery timeouts, invalid
notification IDs, and nonzero exits are logged as warnings in the monitor journal.
Command failure details are limited to one bounded line. Delivery remains best-effort;
successful command execution does not confirm that the desktop displayed a popup.
See [troubleshooting](troubleshooting.md#notifications-do-not-appear).

Notifications target a desktop running as the backup owner in the same environment.
For LXC, that means a desktop inside the container. Routing to the host or an SSH
client's desktop requires separate integration. Root-mode services and manual or
timer-driven backups do not gain phase notifications from this setting.

[User guide](../../README.md#trigger-operation-and-health)
