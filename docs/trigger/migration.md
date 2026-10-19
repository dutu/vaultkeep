# Migrate a standalone Trigger installation

Use the [shared installation guide](../../README.md#install-the-application) on
Debian 13. Keep the standalone checkout and environment available until no service,
protected frozen request, or operator tooling depends on their paths. The source
repository is not changed by the shared installer.

## Prepare each account

1. Record the effective units, custom config and exact enablement before pausing:

   ```sh
   systemctl --user show vaultkeep-trigger.service vaultkeep-trigger-exec@installer-check.service \
       --property=FragmentPath,DropInPaths,ExecStart,UnitFileState,ActiveState
   systemctl --user list-unit-files 'vaultkeep-trigger*'
   systemctl --user stop vaultkeep-trigger.service
   systemctl --user list-units --all 'vaultkeep-trigger-exec@*.service'
   systemctl --user list-jobs --no-pager
   ```

   Run the old environment's `vaultkeep-trigger status`. Drain workers/start jobs
   before changing units or software. Stopping a monitor does not cancel workers.
   Preserve uncertain attempts for [recovery review](recovery.md); do not clear them.
   Pause root and other users' core timers too, following
   [maintenance](../../README.md#pause-and-resume-for-maintenance).

2. With the old executable/interpreter paths selected, privately back up the current
   TOML, fixed account-home state directory, local unit files and drop-ins. Substitute
   your actual standalone environment and config path:

   ```sh
   old_python=/absolute/path/to/standalone/.venv/bin/python
   old_config=/absolute/path/to/config.toml
   trigger_state="$("$old_python" -c 'from vaultkeep_trigger.paths import state_root; print(state_root())')"
   backup_dir="$(dirname "$old_config")/migration-backup-$(date +%Y%m%d-%H%M%S)"
   install -d -m 700 "$backup_dir"
   cp -a -- "$old_config" "$backup_dir/config.toml"
   if [ -d "$trigger_state" ]; then cp -a -- "$trigger_state" "$backup_dir/state"; fi
   ```

   Verify the backup. `FragmentPath` and `DropInPaths` identify which local files
   need a private copy; do not recursively remove a systemd user directory.

3. Disable the old monitor if enabled, recording whether enablement was persistent
   or runtime. Move only the reviewed local monitor/worker copies out of the user
   unit search path into the backup directory. Preserve required custom config in a
   monitor drop-in using the shared executable (see
   [configuration](../../README.md#configure-and-validate-trigger)). Resolve masks or
   executable overrides explicitly, then run `systemctl --user daemon-reload`.

   Do this before applying the shared install: a loaded legacy fragment or conflicting
   command causes the installer to refuse replacement. Its absence is acceptable
   before the shared templates exist. The old environment remains available for
   recovery; the root installer never edits home directories.

## Install and validate

1. From the trusted 0.2.0 checkout, run either a fresh installation or an update:

   ```sh
   sudo ./install.sh install --with-trigger --dry-run
   sudo ./install.sh install --with-trigger
   ```

   Use `update` instead of `install` when upgrading core 0.1.0. The installer stages
   both packages together and leaves monitoring stopped.

2. Keep TOML, YAML paths, instance IDs and the fixed state root in place. Retain a
   valid configured absolute external Vaultkeep path. If switching it to
   `/usr/local/bin/vaultkeep`, edit current TOML explicitly after drain. Never rewrite
   frozen historical requests; keep old executables while protected evidence may
   require them. Config changes follow existing pending invalidation/reconciliation.

3. As each account, reload, inspect and validate:

   ```sh
   systemctl --user daemon-reload
   systemctl --user show vaultkeep-trigger.service vaultkeep-trigger-exec@installer-check.service \
       --property=FragmentPath,DropInPaths,ExecStart
   vaultkeep-trigger --version
   vaultkeep-trigger --config "$old_config" check-config
   vaultkeep-trigger --config "$old_config" check-config --runtime
   vaultkeep-trigger status
   ```

   Expect version `0.2.0`, shared `/etc/systemd/user` fragments, and the shared
   executable. Confirm existing IDs, pending/active records and review warnings
   remain readable. A custom config drop-in may change only the monitor's arguments;
   the worker stays `exec-worker %i` with its frozen request.

4. After deployment checks pass, restore the recorded enablement explicitly and
   start only a previously active monitor. For a formerly enabled active monitor:

   ```sh
   systemctl --user enable vaultkeep-trigger.service
   systemctl --user start vaultkeep-trigger.service
   vaultkeep-trigger status
   ```

   Use `enable --runtime` if that was the original intent. A previously disabled
   or inactive monitor should retain that state. Inspect observations, receipts and
   logs after the next intended lifecycle. Remove the old environment only when
   nothing depends on it. Do not remove the source repository as part of migration.

## Migration rollback and data cleanup

For rollback, pause/drain again, restore the saved per-user units and old software
paths, reload and validate before resuming. Preserve current state. Never copy an
old state snapshot over execution evidence recorded after migration.

The shared installer uninstalls both managed packages; normal uninstall and `--purge`
preserve every user's Trigger data. Review offline accounts' enablement too so a
later reinstall cannot unexpectedly activate a stale schedule.

Only after permanent retirement, worker/job absence and evidence review, an account
may deliberately remove its selected TOML and fixed state root. First save evidence
that must be retained. Resolve the path using the still available old or shared
interpreter, inspect it, and remove only that reviewed directory. State removal loses
requests, launches, receipts, logs, blocks and review warnings permanently. Never
unlink a held runtime lock. Wallets, YAML jobs, core user data and archives are
separate data and remain outside this cleanup.
