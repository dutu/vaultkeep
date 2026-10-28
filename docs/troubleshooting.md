# Troubleshooting

Start with the selected job, using the same scope, YAML path, runtime parameters,
and XDG environment as its execution. These examples use a user-owned job; for root
jobs omit `--user`, use `/etc/vaultkeep/jobs/app.yaml`, and run privileged operations
with `sudo`.

```bash
vaultkeep --version
vaultkeep --user --config /absolute/path/job.yaml validate
vaultkeep --user --config /absolute/path/job.yaml status --verbose
```

## Validation and status

`validate --schema-only` checks YAML structure without runtime checks. Full validation
also checks sources, destination safety/writability, saved backup history, and enabled
automation health. An absent timer or monitor reports `NOT INSTALLED` and
`SETUP INCOMPLETE`; fix other failed checks, install the selected automation explicitly,
then validate again. Read every issue, because incomplete setup can coexist with other
failures. See [validation semantics](cli.md#validate-a-job-configuration).

`status` is read-only and does not require sources or destination mounts to be online.
Exit 0 means the report could be read, rather than that a backup succeeded. Inspect
Backup, Trigger, and Schedule separately. `UNKNOWN` preserves missing or inconclusive
evidence; a free lock means current activity is idle even if a previous outcome is
unknown. See [status meanings](status.md).

## Backup failures and logs

Read `Previous backup`, `Reason`, exit code/signal, and the displayed `Log` path.
Use `status --verbose` for bounded excerpts and exact paths, then inspect the retained
log for full context. Logs can include sensitive operational details: review before
sharing. A failure after commit can leave a valid archive, including when required state persistence,
hooks, or final durability checks failed. Inspect and verify it before taking action.

For scheduled jobs, inspect the owning manager:

```bash
systemctl --user status vaultkeep@app.timer vaultkeep@app.service
journalctl --user -u vaultkeep@app.service -n 100 --no-pager
```

For root jobs use `sudo systemctl`/`sudo journalctl` without `--user`.
Parameterized jobs have distinct unit names; inspect `status --verbose` or
`schedules list` rather than guessing. For Trigger use its
[specialist diagnosis](trigger/troubleshooting.md) and [recovery](trigger/recovery.md).

## Destination, mount, or permission errors

The destination must already exist and be accessible as the job owner. Mount the
intended supported local filesystem or CIFS/NFS share, inspect it from the execution environment, and check
configured marker files and filesystem types. Full validation writes, renames, and
removes a temporary probe; it is not read-only. If cleanup fails, correct permissions
and remove only the reported probe. Schema-only validation skips these checks.

[Destination safety checks](configuration.md#destination-safety-checks) explain
supported filesystems and why unsafe locking options and nested destination mounts
reject mutation. Subdirectory mounts are supported. Checks establish conditions at
inspection time; they cannot prevent later mount changes or storage failures.

For CIFS/NFS, a destination-lock error can mean another machine holds the history
directory's `.vaultkeep.lock`, server locking is disabled/unavailable, or the account
cannot create/open the shared anchor. Keep CIFS `nobrl` and NFS
`nolock`/`local_lock=all`/`local_lock=flock` disabled and grant all backup accounts
read/write access to the same history lock file, including each managed child of a
`per_file` job. A persistent lock file alone does not mean a writer is active. Never delete it to bypass contention;
stop or wait for the actual writer. Full validation also takes this lock for its probe.
A CIFS message saying the history lock is "busy or denied by the server" can indicate
remote contention or a server locking policy; inspect the other writers and server
permissions. Ordinary lock-file open/access failures remain destination errors.

## Missing or unusable local backup state

Local `state.json` is a reconstructable cache in the scope-specific
[root](root-mode.md) or [user](user-mode.md#user-mode-locations) state directory.
A backup run reconstructs it from valid destination manifests when required.
Do not edit destination manifests to recreate local state. Status does not reconstruct
execution results or repair files. Trigger recovery state is separate and must never
be deleted as a cache; follow [Trigger recovery](trigger/recovery.md).

## Interrupted backups and partial directories

Before commit, ordinary failures remove the attempt's
`~$.partial-vaultkeep-*` directory. A cleanup failure reports the remaining path while
preserving the original error. Committed archives survive later failures.
Power loss or SIGKILL can leave older partial directories; subsequent runs do not
remove them. Confirm no backup or worker uses a directory before manually removing it.
Check logs for any private plaintext TAR left by failed encrypted-archive cleanup.
Synchronization tools should exclude temporary directories and upload completed backups.

## Scheduling does not run as expected

Inspect `status`, `schedule next`, and `schedules list` in the correct scope.
YAML changes alone do not update installed units; use `schedule update` or preview
bulk changes with `schedules sync --dry-run`. User timers require a reachable user
manager; [lingering](user-mode.md#configure-user-mode-automatic-backups) is a separate
administrator choice. Persistent timers can catch up one activation after downtime.
A temporary maintenance pause should stop/start the timer without disabling it;
disable/remove clear persistent timestamps. See [schedule commands](cli.md#configure-a-job-timer).

## Installer candidate fails `--version`

See [candidate validation diagnosis](maintenance.md#installer-candidate-fails---version)
for incomplete checkouts and the installer diagnostic transcript.

<a id="installer-reports-inconclusive-unit-properties"></a>
## Installer reports unexpected systemd state or a failed property query

See [installer unit-property diagnosis](maintenance.md#installer-reports-inconclusive-unit-properties)
for unexpected launcher states, absent worker templates, older installers, and
failed manager queries. The report names the account and unit and gives the actual
state values or missing properties; this does not necessarily indicate invalid YAML.

## Notifications do not appear

Test delivery in the backup owner's desktop and inspect the backup log or monitor
journal. Missing tools, an unavailable/ambiguous session bus, and desktop settings
can prevent popups without failing backups. See the complete
[notification troubleshooting procedure](notifications.md#troubleshooting)
and [notification setup](notifications.md#enable-notifications).

## Update, rollback, or uninstall refuses to proceed

Read the installer transcript named by `Diagnostics:` and retain reported
`.transaction.*`/`.maintenance.*` recovery directories. Resolve active work,
unreachable managers, overrides, or identity conflicts before retrying. Failed
rollback and failed resumption have different recovery requirements; follow
[maintenance recovery](maintenance.md#pause-and-resume-for-maintenance). Do not erase
Trigger evidence or resume from an incomplete rollback.

## Restoration and application recovery

Use [README's restore procedures](../README.md#restore-a-backup) to extract into
an empty staging directory, verify the archive, and inspect the result. Stop the
application and related writers before replacing live data; follow the application's
own recovery requirements. Vaultkeep does not repair wallet/application data or
provide a restore command. Unknown Trigger outcomes need
[evidence review](trigger/recovery.md#operator-review-and-repair), independently of
whether an archive extracts successfully.

## Suspect backups and cleanup warnings

1. Inspect `vaultkeep --user --config JOB.yaml list` for each suspect's original
   reason and retention decision. `PROTECTED` needs a reliable compatible replacement.
2. Run `vaultkeep --user --config JOB.yaml verify` for full integrity checks. Wrong
   passwords and missing tools require correcting setup; they are not corruption proof.
3. Run a new backup with the same source scope and consistency policy to replace an
   unresolved suspect. Required state writes and `on_success` must succeed before cleanup.
4. If `run: created` reports a cleanup warning, keep the new archive and inspect
   `status --json` and the execution log. Retry with `prune --dry-run`, then `prune`,
   or on a later newly created archive. An unchanged run never retries cleanup.

A malformed marker, unsupported matching manifest, or unexpected artifact blocks
deletion in that history. Preserve the evidence and correct the specific problem;
never relabel an old manifest or delete a marker to manufacture a healthy backup.
