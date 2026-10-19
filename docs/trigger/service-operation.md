# Service operation

## Units

- `vaultkeep-trigger.service` — foreground monitor, restart-on-failure, journald output,
  `UMask=0077`. The [configuration procedure](../../README.md#configure-and-validate-trigger) uses default config resolution or an explicit per-user `--config` drop-in.
- `vaultkeep-trigger-exec@.service` — `Type=oneshot`, `Restart=no`,
  `TimeoutStartSec=infinity`, `UMask=0077`, `ExecStart=... exec-worker %i`. It is
  independent of the monitor's stop/restart lifecycle. Do not enable individual
  attempt instances.

Only the monitor submits worker starts, using
`systemctl --user start --no-block vaultkeep-trigger-exec@<attempt-id>.service`
without a shell.

The shared installer installs templates at `/etc/systemd/user` with the fixed
`/usr/local/bin/vaultkeep-trigger` command. Installing the Python distribution alone
does not install these units. Workers use saved requests,
so their fixed command has no `--config` argument. Custom configuration belongs on
the monitor command only. Keep both units' executable paths consistent.

## State and runtime locations

Trigger's own state root is fixed at `<account-home>/.local/state/vaultkeep-trigger`,
resolved from the operating-system account record for the effective UID. `HOME`,
`XDG_STATE_HOME`, and configuration cannot relocate it, so the monitor, CLI, and
workers agree on it. State directories use mode `0700` and files `0600`.

```text
<state-root>/
  instances/<instance-id>.json
  blocked/<instance-id>.json       # durable state-error diagnostic, when needed
  attempts/<attempt-id>/
    request.json
    launch.json
    receipt.json
    log.txt
```

Runtime holds only `<XDG_RUNTIME_DIR>/vaultkeep-trigger/monitor.lock`. VaultKeep's own
user-mode paths continue to follow its validated `HOME`/XDG environment; Trigger passes
the user's own runtime directory to VaultKeep unmodified so its job lock is shared with
manual and timer-driven runs.

The worker's VaultKeep child environment is frozen in its request: `HOME` is the
account home; absolute `XDG_STATE_HOME`/`XDG_CACHE_HOME` values are kept, otherwise
account-home defaults are used; `XDG_RUNTIME_DIR` remains the user's runtime directory;
`PATH` is fixed to `/usr/local/bin:/usr/bin:/bin`; `LANG` is copied if present. Arbitrary
service/shell variables, including credential variables, are not passed through.
Review hooks, helper executables, and credentials under this environment. Shell checks
use the invoking shell's selected environment; the real monitor uses its service
environment, so both must agree on relevant XDG paths.

Only paths/settings are frozen: replacing the VaultKeep binary, changing a job file,
or retargeting a symlink can change a later invocation. Do not replace installation
files under active workers; follow the [update procedure](../../README.md#update-the-application).

## Internal constants

These are not user configuration. They are documented here so operators can reason about
timing:

| Constant | Value | Effect |
| --- | ---: | --- |
| Blocked-wait default | 3600 s | One wait notification when a pending request waits behind an attempt that has no frozen deadline. |
| Consecutive-rejection escalation | 5 | One best-effort escalation at the fifth consecutive proven submission rejection. |
| Contention retry delay | 30 s | Minimum delay before retrying a deferred (exit 10) or proven-rejected request. |
| Diagnostics cap | 256 KiB | Combined stored child output per attempt; stdout is always drained. |
| Completed-attempt retention | 50 | Most recent cleanup-eligible completed attempts per instance. |
| Uncertain-history cap | 20 | Completed, reconciled, summarized `result_unknown`/`outcome_unknown`/`blocked` records per instance, within completed-file retention. |

The history cap never authorizes deleting active or unresolved recovery records. If a
bound cannot be enforced because protected records must be retained, the affected
instance's dispatch is blocked instead of deleting recovery evidence.

Proven pre-queue rejections are summarized and eligible for completed-record cleanup;
their per-request rejection count and notification decisions survive detailed history
expiry. Receipt presence alone does not make a still-running worker eligible for cleanup.

Notification markers for expired detailed history are pruned after cleanup. Decisions
needed by retained attempts, active/blocking attempts, pending requests, and the current
request's rejection bookkeeping remain protected. Necessary safety summaries survive
detail and marker expiry.

These bounds apply to detailed attempt directories. Persisted instance summaries are
not pruned by the same caps. Dispatch is currently held when more than 70 protected
records are associated with an instance; this check preserves records rather than
deleting them. A history-bound hold is logged; it does not create the persistent
state-error diagnostic used for `dispatch blocked` in status.

The blocked-wait default applies only when the blocking attempt's frozen request has
no deadline. Otherwise its overdue alert covers the wait. A persistent marker prevents
another wait alert behind the same attempt even if pending work is cleared, replaced,
or recovered after restart.

Child diagnostics share the combined 256 KiB cap across stdout and stderr. The final
nonblank stdout line is independently limited to 4096 bytes; an oversized line yields
no result marker rather than parsing a misleading truncated suffix. Both streams are
drained even after the storage cap is reached.

## Deployment checks

Before enabling automatic backup for an instance:

1. Verify the installed wallet's identity, explicit data-directory evidence, metadata
   accessibility, and every source writer using the [process checklist](process-detection.md#validation-before-enabling).
   Passing `check-config --runtime` does not perform this review.
2. Review the VaultKeep YAML job and runtime parameters, source scope, distinct effective
   destination roots, hooks/credentials, and external timers/direct runs. Compare the
   service and interactive runtime/cache namespace so VaultKeep's locks are shared.
3. Check the actual installed VaultKeep version and validation/CLI contract. A version
   match alone does not prove reviewed behavior. See the source-reviewed
   [integration contract](../design/trigger.md#verified-integration-findings).
4. In a separately scoped disposable deployment, exercise the static worker template
   with a fake job: queued/activating states, receipts, failed or absent units,
   rejection diagnostics/codes, and monitor-independent lifetime. Confirm the deployed
   systemctl start codes rather than using its service-status exit table. The current
   adapter classifies codes 4/5/6 as rejections without validating diagnostics, so these
   semantics are an explicit deployment assumption.
5. Verify optional notifications, logout/user-manager lifetime, and shutdown behavior.
   Keep production instances disabled until the relevant gates are met.

There is no built-in deployment-test or dry-run-backup command. Automated tests mock
service operations; live unit validation remains open. Read-only `check-config --runtime`
does not substitute for a real worker exercise.

## Inspect workers without discarding evidence

```sh
systemctl --user list-units --all 'vaultkeep-trigger-exec@*.service'
systemctl --user list-jobs --no-pager
journalctl --user -u 'vaultkeep-trigger-exec@*.service' -n 100 --no-pager
```

For a particular attempt, use its UUID from `status` to select
`vaultkeep-trigger-exec@<attempt-id>.service`. `Type=oneshot` can remain `activating`
for the whole backup. Submission success is not completion. Even a receipt does not
release its slot until both the worker and queued job are confirmed gone. Reconcile
evidence before a manual `systemctl --user reset-failed <unit>`; the monitor does not
currently reset failed units automatically.

## Lifecycle limits

An executing worker survives a **monitor restart** through independent systemd
supervision. That is different from surviving logout, user-manager termination, or host
shutdown. These units do not keep the user manager alive after logout and do not
provide host-shutdown completion guarantees. A user-manager stop or host shutdown can
interrupt work; preserve state and let recovery reconcile it on the next start.

Inspect lingering, a separate system deployment choice, with:

```sh
loginctl show-user "$(id -un)" --property=Linger
```

If continuous operation after logout is required, an administrator/user may explicitly
configure `loginctl enable-linger "$(id -un)"` under the host's policy. This keeps the
user manager available beyond login sessions, but does not guarantee a graphical
notification session or survival of host shutdown. Trigger does not configure it.
Validate the selected session/lifetime behavior before production use.

[User guide](../../README.md#trigger-operation-and-health)
