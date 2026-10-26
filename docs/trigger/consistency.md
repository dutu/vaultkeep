# Consistency and safety boundaries

Trigger requests a backup after a wallet's process disappears. Process disappearance
establishes that a process is absent — not that the wallet shut down cleanly or that its
data is consistent.

## What Trigger does not do

- It does not change wallet applications or their startup procedures.
- It does not read wallet secrets or inspect job contents.
- It does not implement a backup engine, restore, or a network API.
- It does not kill a running backup, delete its archive, or alter VaultKeep's state.

## Offline safety

- The worker performs an authoritative final recheck from the request snapshot before
  invoking VaultKeep. Reliable `stopped` evidence is required; running, ambiguous, or
  unavailable evidence yields a blocked receipt without invoking VaultKeep.
- The final recheck cannot prevent a writer from restarting afterwards. Polling can miss
  short lifecycles, and digest/source-change checks are not database snapshots.
- The detector observes only the configured identity and directory predicates; it does
  not enumerate all writers or validate cross-instance overlap. Runtime config checks
  do not establish these properties. Review the [process limitations](process-detection.md)
  and [deployment requirements](service-operation.md#deployment-checks).
- Archive consistency is enforced by the backup engine according to the shared
  `consistency.mode`. Offline policy requires reliable stopped observations before
  discovery, before archival, and after source reads. The final check precedes
  `after_archive`, which may restart the application; no source reads follow it.
- Process absence does not prove clean shutdown, transactional consistency, or the
  absence of short restarts. File hashing remains necessary. A monitoring gap alone
  does not classify a backup or add a historical archive-health warning.

## Deadlines

An optional `deadline_seconds` marks an attempt overdue and sends one best-effort
notification. It never terminates the worker and never releases the active slot.
Deadlines are advisory; hung executions require manual operator action.
Overdue and blocked-wait alerts use persisted UTC timestamps and are affected by
wall-clock changes; shutdown confirmation uses a process-local monotonic clock.

The backup child runs without an execution timeout. Closing stdout or stderr does not
terminate it. Explicit timeouts apply only to preflight calls, whose child processes
may be terminated when their limit expires.

## Safety-relevant design contract

- VaultKeep's locks are preserved: Trigger passes the user's own runtime directory and
  never reimplements VaultKeep's lock identity.
- Enabled instances' jobs must use **distinct effective destination roots**. Account for
  runtime parameters and path aliases, and include external timers and direct runs in
  the review. This is a conservative v1 requirement, not a universal VaultKeep
  prohibition.
- A created archive is committed and retention may already have run before VaultKeep
  returns. Trigger cannot undo retention or discard an archive safely.

[User guide](../../README.md)
