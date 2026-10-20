# Notifications

Optional desktop notifications are configured per job under YAML `trigger.notifications`
and delivered best-effort by the monitor through `notify-send` with a bounded timeout.
The configured command is invoked directly with title and body as two arguments;
it is not a shell command string. Trigger does not block the monitoring loop waiting
for delivery. Each job has its own notification settings.

## What is delivered

- One start notification per logical request. A contention retry or a proven-rejection
  retry creates a new attempt but does **not** repeat the start alert.
- A terminal result after the worker and any queued job have ended: created, unchanged,
  result_unknown, failure, or blocked. A receipt arriving earlier is recorded without
  prematurely announcing completion.
- `review_required`, when an observation gap or restart overlapped execution.
- `outcome_unknown`, when a worker is absent with no completion evidence.
- An overdue notification when an attempt exceeds its advisory deadline.
- A blocked-wait notification. Pending-request supersession is recorded in lifecycle
  state/logs; a dedicated supersession desktop alert is not currently implemented.

## Deduplication and escalation

- Delivery-decision markers are persisted so receipt recovery does not resend known
  transitions. Delivery is best-effort, not exactly-once; a crash around delivery can
  lose an alert.
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
vaultkeep --user trigger --config /absolute/path/job.yaml update
```

Run the import from the graphical session where the variable is set, and apply the
normal config validation gate before the restart. Importing it does not update an
already running process. It changes the user manager's environment for subsequently
started services. Trigger never imports the
entire shell environment as a hidden validation side effect.

[User guide](../../README.md#trigger-operation-and-health)
