# Migrate a standalone or shared Trigger installation

Current Vaultkeep contains Trigger as a built-in feature. It reads each backup job's
YAML `trigger` section and runs one monitor per job. The former `vaultkeep-trigger`
command, separate Python project, TOML configuration, and shared monitor service are
not used by new installations. The installer accepts old schema-1/core and
schema-2/core-plus-Trigger deployments, preserves user files, and writes schema-3
ownership metadata for the unified release.

## Pause and preserve evidence

1. Before changing software, stop the old monitor in its owning user's manager:

   ```bash
   systemctl --user stop vaultkeep-trigger.service
   systemctl --user list-units --all 'vaultkeep-trigger-exec@*.service'
   systemctl --user list-jobs --no-pager
   ```

   Stop any foreground monitor as well. Let workers finish. With the old environment
   still available, inspect `vaultkeep-trigger status`, worker journals, saved requests,
   launches and receipts. Resolve pending/uncertain work and review warnings before
   enabling a new monitor. Do not erase evidence or rerun workers to force migration.

2. Disable the old monitor. Back up its TOML, user unit files/drop-ins, job YAML, and
   entire `<account-home>/.local/state/vaultkeep-trigger` directory. Use the account's
   operating-system home, not a temporary `HOME` override. Keep the old Python
   environment available until all evidence has been reviewed.

3. Remove or archive user-local old monitor/worker units and overrides after review;
   these take precedence over shared installer templates. Run
   `systemctl --user daemon-reload`. The installer refuses conflicting effective units
   or modified managed templates; it does not overwrite arbitrary user overrides.

## Update software and jobs

1. Follow the [maintenance and update procedure](../../README.md#update-the-application).
   Use `install.sh update` for a newer version, or `install.sh install` for a
   same-version source refresh. Do not pass `--with-trigger`. A managed combined
   installation's obsolete command link and shared monitor template are removed
   transactionally; its previous release remains retained. Legacy standalone virtual
   environments outside the managed prefix remain the operator's responsibility.

2. Transfer each old TOML instance into the corresponding backup YAML's `trigger`
   section. Map `instance.app`, `data_dir`, `matcher`, and timing fields directly;
   map `instance.job.params` to `trigger.params`. Use the backup's `job.id` as the
   instance name. Set `schedule.enabled: false`; leave `trigger.enabled: false` until
   review is complete. Transfer notification settings to `trigger.notifications`
   and use the job's `logging.level`. There is no executable setting: Trigger invokes
   this Vaultkeep installation's own command.

3. After resolving old recovery work, archive the reviewed legacy `instances`,
   `attempts`, and `blocked` directories outside the active state root. Keep the
   archive and old TOML for evidence. Do not move any new `jobs/` namespaces into
   another job. Runtime preflight blocks new monitors while legacy shared JSON
   records remain in those old directories. Historical records are preserved rather
   than automatically relabeled or replayed under new identities.

4. Select one job, review its app identity and related writers, enable its trigger,
   validate it and explicitly activate its monitor:

   ```bash
   job_config=/absolute/path/to/electrum-main.yaml
   vaultkeep --user --config "$job_config" validate
   vaultkeep --user trigger --config "$job_config" check-config --runtime
   vaultkeep --user trigger --config "$job_config" install
   vaultkeep --user trigger --config "$job_config" status
   ```

   Repeat for each job. Supply matching parameters for templated jobs. A fresh
   monitor must observe the application running before a later shutdown can request
   a backup; migration does not synthesize a missed shutdown event.

## Changing automatic execution or retiring a job

Before switching to scheduling, disable the trigger monitor and drain its workers.
Then set `trigger.enabled: false`, configure `schedule`, and install/update the
normal per-job timer. For the opposite transition, disable/remove the existing timer
before enabling the trigger. YAML validation refuses both mechanisms enabled together;
it cannot disable an already installed service merely because a file changed.

`vaultkeep --user trigger --config JOB.yaml remove` disables/removes only that monitor unit.
Recovery state remains available. Moving/renaming a job or changing its ID/parameters
changes its identity: disable/drain the old service and retain or archive its reviewed
state before activating the new identity. Never delete a held runtime lock.

[User guide](../../README.md#optional-trigger)
