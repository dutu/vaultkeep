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
  changes during normal monitor polling; heartbeats and repeated polls do not resend
  the same phase. Short phases between polls may not be displayed.
- A terminal result after the worker and any queued job have ended: created, unchanged,
  result_unknown, failure, or blocked. A receipt arriving earlier is recorded without
  prematurely announcing completion.
- `review_required`, when an observation gap or restart overlapped execution.
- `outcome_unknown`, when a worker is absent with no completion evidence.
- An overdue notification when an attempt exceeds its advisory deadline.
- A blocked-wait notification. Pending-request supersession is recorded in lifecycle
  state/logs; a dedicated supersession desktop alert is not currently implemented.

## Updating the activity notification

With `notify-send` (including an absolute path whose filename is `notify-send`),
the start, phase, and final-result messages share one notification per instance.
Trigger uses `--print-id` and `--replace-id` to update it. While work is pending or
running, it requests no automatic expiration; the final result restores the desktop's
default timeout. The desktop controls actual visibility, expiration, and whether
updates produce another popup. Do Not Disturb settings can suppress popups.
Other alerts, such as overdue and review-required warnings, remain separate.

Replacement IDs and the last displayed phase are kept only in monitor memory. A
monitor restart can create a new activity notification for an ongoing backup. An
unavailable desktop, failed delivery, or user dismissal does not stop a backup.

Activity uses the same attempt-specific progress records as `trigger status`. Invalid,
stale, or mismatched records and unavailable processes do not establish a running
phase. If previously displayed activity becomes unavailable, the notification reports
that progress is unavailable while awaiting the result. A finished progress record
means only that the workflow ended: completion is announced after a receipt is
reconciled and the worker and any queued job have ended.

Custom notification commands retain their existing interface: title and body as two
arguments, with no extra options. They receive phase changes as individual messages;
notification replacement is available only with `notify-send`.

## Deduplication and escalation

- Delivery-decision markers are persisted so receipt recovery does not resend known
  lifecycle transitions. Phase deduplication is in memory and does not add durable
  state. Delivery is best-effort, not exactly-once; a crash around delivery can lose an
  alert.
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

A `systemd --user` service may lack `DBUS_SESSION_BUS_ADDRESS`. Delivery failure, a
missing `notify-send`, an unavailable session, or a timeout is silently ignored by
the current notifier. Nonzero delivery exits are also ignored; there is no delivery
confirmation or notification-failure log.
Deployment may explicitly import the variable from the graphical session before
restarting the monitor:

```sh
systemctl --user import-environment DBUS_SESSION_BUS_ADDRESS
vaultkeep --user --config /absolute/path/job.yaml trigger update
```

Run the import from the graphical session where the variable is set, and apply the
normal config validation gate before the restart. Importing it does not update an
already running process. It changes the user manager's environment for subsequently
started services. Trigger never imports the
entire shell environment as a hidden validation side effect.

Notifications target the backup owner's desktop session. Root-mode services and
manual or timer-driven backups do not gain phase notifications from this setting.
It does not select another logged-in user or route alerts between multiple graphical
sessions.

[User guide](../../README.md#trigger-operation-and-health)
