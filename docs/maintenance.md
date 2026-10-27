# Installation and maintenance reference

The [README installation](../README.md#installation),
[update](../README.md#update-the-application), and
[uninstall](../README.md#uninstall-the-application) procedures cover standard use.
This reference covers custom layouts, maintenance coordination, and failure recovery.
Use Debian 13 for current installer operations; Debian 12 installations must remain
on 0.1.0 and use their installed uninstall script until the OS is upgraded.

## Installation layout and custom paths

`/path/to/vaultkeep` is only the administrator's source checkout. It is not an installed application directory and can be removed after installation. Vaultkeep copies the selected source into the active versioned release below `/opt/vaultkeep`. A later same-version reinstall can use the same refreshed checkout. A later version update can use the same checkout or another trusted checkout of the newer release.

The installer:

- stages the checkout and virtual environment as one versioned release;
- checks existing system and user job YAML against the candidate release's schema before pausing schedules or activating the release;
- atomically activates that release through `/opt/vaultkeep/current`;
- creates `/usr/local/bin/vaultkeep`;
- creates configuration, secrets, state, and temporary directories;
- installs inactive `.example` job templates;
- installs and validates the shared systemd service and timer templates;
- refreshes existing managed job units, validates them, and resumes only previously active timers and monitors;
- runs `vaultkeep --version`.

Application code, virtual environments, and installer ownership metadata are consolidated below:

```text
/opt/vaultkeep/
├── releases/
│   └── <version>/
│       ├── src/
│       ├── venv/
│       └── deployment.json
├── current -> releases/<version>
└── install-manifest.json
```

By default, Debian keeps the remaining files in standard purpose-specific locations:

| Purpose | Location |
|---|---|
| Command | `/usr/local/bin/vaultkeep` |
| Jobs and secrets | `/etc/vaultkeep` |
| Local state and temporary files | `/var/lib/vaultkeep` |
| Core system templates | `/etc/systemd/system/vaultkeep@.service` and `vaultkeep@.timer` |
| Trigger worker template | `/etc/systemd/user/vaultkeep-trigger-exec@.service` |

The ownership manifest allows the installer to identify its files without scanning the system or guessing from filenames.

To use a custom command or root configuration directory, pass absolute, normalized paths to the installer:

```sh
sudo env VAULTKEEP_BIN_LINK=/usr/local/bin/vaultkeep-custom \
  VAULTKEEP_CONFIG_ROOT=/etc/vaultkeep-custom ./install.sh install
```

`VAULTKEEP_INSTALL_PREFIX` and `VAULTKEEP_VAR_ROOT` similarly override the application directory and root state/temporary directory. Repeat the same overrides for `update`, reinstallation, and `uninstall`; changing them does not migrate an existing installation. Generated services and installed commands retain the chosen layout without requiring these variables in user sessions or systemd environments. Use the chosen command and configuration paths in the procedures below.

Systemd does not allow quotes or backslashes in executable paths; the installer rejects those command paths. Keep shared unit directories at their defaults unless systemd is separately configured to search the chosen locations.

Installation does not start a backup. Example files are copied to the root-mode jobs directory with the `.example` extension, so they are not active jobs:

```text
/etc/vaultkeep/jobs/*.example
```

The installer copies every `examples/*.yaml` file from the release into the root-mode jobs directory, appending `.example` to its filename. Every successful installation or update, including a same-version refresh, overwrites these `.yaml.example` files with the current release templates and restores missing copies. User-created `.yaml` job files are not overwritten; copy an example to a job YAML before customizing it.

## Pause and resume for maintenance

`install.sh install` and `install.sh update` handle maintenance automatically:

1. Prepare the candidate release in staging and validate existing job configurations
   with its schema, resolve application users and service accounts/groups, and
   render existing managed jobs without applying changes. Check `.yaml` files in
   managed job directories and the original paths and saved parameters referenced
   by installed timers and monitors, including offline accounts. An exact
   version/source match uses the verified installed release. These checks do not
   run hooks, modify job state, or contact application processes.
2. Inventory existing root and user Vaultkeep timers and Trigger monitors, recording
   their active states and enablement, including runtime enablement.
3. Stop active timers and monitors. Running backup services and workers are never
   stopped. If backups, workers, queued work, held job locks, or inconclusive activity
   remain, refuse the operation and resume the recorded launchers.
4. Hold the deployment maintenance lock, install or repair the release, and refresh
   existing managed job units using their original YAML paths and parameters.
   Refreshing units does not enable or start them.
5. Reload systemd, validate all discovered jobs and runtime readiness for jobs being
   resumed, then resume only previously active timers and monitors. Previously
   inactive schedules remain inactive. Enablement and persistent timer timestamps
   are preserved; a persistent calendar timer may catch up a missed event.

This includes running and lingering user managers and jobs in the standard unit
locations of accounts without a running manager. Stopped user managers remain
stopped. For custom XDG locations, keep the owning manager running with its XDG
roots exported during maintenance. Unreachable managers, local unit overrides,
changed job identities, and standalone Trigger installations require resolution
before automatic maintenance can proceed. YAML jobs, secrets, recovery evidence,
wallet applications, and backup archives are preserved. New jobs remain opt-in.

Normal Vaultkeep backup, verification, pruning, and service-management commands
refuse to start while the maintenance lock is held. Source checkouts, older
versions, custom launchers, and direct Python API calls may not participate in that
lock. During the first upgrade from an older release, keep manual/custom launchers
and new user sessions paused; process and job-lock checks remain necessary.

Ordinary failures and handled signals restore deployment artifacts and generated
job units, then resume the original active launchers if restoration succeeds.
Incomplete restoration leaves schedules paused and retains recovery snapshots.
A resume failure after successful deployment keeps the installed release and
reports the private resume journal path. Resolve the reported service problem,
then retry resumption from the same checkout:

```bash
sudo python3 scripts/install_maintenance.py resume '/opt/vaultkeep/.maintenance.<id>/journal.json'
```

After successful resumption, remove only that reported maintenance directory. A
retained journal blocks another install/update until recovery is reviewed. Never
resume from an incomplete rollback before resolving its restoration errors.

For a deliberate manual maintenance pause, inventory actual unit names and stop
only the relevant timers and monitors in their owning managers:

```bash
sudo systemctl list-units --all 'vaultkeep@*.timer' 'vaultkeep@*.service' 'user@*.service'
systemctl --user list-units --all 'vaultkeep@*.timer' 'vaultkeep@*.service' 'vaultkeep-trigger*'
sudo systemctl stop vaultkeep@app.timer
systemctl --user stop 'vaultkeep-trigger@<job-id>-<identity>.service'
```

Record active states first and resume those units explicitly with `systemctl start`
after maintenance. Do not use `schedule disable` for a temporary pause: it changes
enablement and clears persistent timer timestamps. Let backups and workers finish.
For removal, follow the additional [uninstall steps](../README.md#uninstall-the-application).

## Refresh existing monitor commands

Commands use `schedule` and `schedules` in place of `timer` and `timers`. Update any
saved shell commands or scripts. Put `--user`, `--config`, and `--param` before
`trigger` or `schedule`; version inspection uses `vaultkeep --version`.

Install and update automatically refresh existing managed per-job monitor commands,
including monitors installed with the previous option order. Disabled and inactive
monitors stay disabled or inactive.

After editing a job outside an application update, refresh it explicitly as the
application owner, using its original YAML path and parameters:

```bash
job_config=/absolute/path/job.yaml
vaultkeep --user --config "$job_config" trigger update
vaultkeep --user --config "$job_config" validate
```

`trigger update` enables and starts that monitor. Run it only when monitoring is
wanted. Existing scheduled backup units continue to invoke `run`; their systemd
unit names remain `vaultkeep@…timer` and `vaultkeep@…service`.

## Reinstall the same application version

Select the intended reviewed source at the same project version, then run `install`
again. The script [pauses and resumes existing schedules automatically](#pause-and-resume-for-maintenance):

```bash
cd /path/to/vaultkeep
git fetch --tags --prune
git checkout <reviewed-tag-or-commit-with-the-same-version>
sudo ./install.sh install --dry-run
sudo ./install.sh install
```

If the installed version matches the checkout version but the source content changed, `install` stages the checkout as a complete replacement for the existing release directory, validates it, and replaces `/opt/vaultkeep/releases/<version>` in a rollback-protected transaction. Existing jobs, secrets, state, timer registry, and backup destinations are preserved.

If the installed version is different from the checkout version, `install` stops. Use `update` for a newer release version.

## Update behavior and failure recovery

The update mode:

- requires an existing installation and accepts a newer candidate release or an exact version-and-source match for idempotent verification;
- stages the source and virtual environment as a complete versioned release;
- validates the executable, example configurations, and systemd units;
- validates existing job YAML, system identities, and managed-job generation before pausing schedules;
- atomically switches `/opt/vaultkeep/current` to the new release;
- refreshes existing managed schedule and monitor units and reloads systemd;
- validates jobs and resumes only previously active timers/monitors;
- preserves user jobs, secrets, enablement, and persistent timer timestamps;
- retains the complete preceding release for rollback.

`install.sh update` does not download source code; it installs the checkout from which it is executed. The same version and source digest produces an idempotent verification with no release switch. The same version with different source content is not an update; use `install` to refresh the installed code or bump the project version before running `update`.

All installations include Trigger support; `--with-trigger` is no longer an installer
option. Monitoring remains opt-in for each YAML job. Upgrades replace the standalone
Trigger command with `vaultkeep --user trigger` and preserve user files and recovery evidence.

An exact version/source match checks packages, locked versions, commands,
and managed artifacts. A damaged application environment is rebuilt; missing owned
links/templates are reconciled. Modified units, unrelated links, masks, and conflicting
overrides require operator resolution. These checks do not prove arbitrary Python
file integrity.

Failed staging leaves the active release intact. Ordinary failures and handled INT,
TERM, or HUP signals restore managed deployment artifacts and generated job units,
then resume original active schedules if restoration succeeds. Failed restoration
leaves schedules paused for recovery. Apt changes are outside rollback. After power loss or SIGKILL,
inspect `current`, `install-manifest.json`, retained releases, and `.staging` or
`.transaction.*` or `.maintenance.*` directories before retrying. Preserve recovery snapshots if rollback
reports a failure; never erase Trigger evidence to force an update.

## Installer-specific diagnosis

### Configuration validation fails

Both `install` and `update` abort before activation or schedule changes if an existing
job YAML is missing, unreadable, malformed, or incompatible with the selected
release's schema, or references a nonexistent application user or required service
account/group. Installed job generation is also checked before schedules pause, including
conflicts between disabled configuration and installed schedules or active monitors.
Disabled jobs are included; `application.user` must exist even with Trigger disabled.
The report shows the absolute configuration path and a concise explanation with
its YAML property when available:

```text
Issue: /home/bisquser/.config/vaultkeep/jobs/bisq-main.yaml
       User 'missing-user' does not exist (field: application.user).
Installation aborted. Previous installation preserved.
```

Execution groups come from the system backup service (`Group=root`) or the owning
user account's primary group; there is no YAML group property. Errors for these
requirements identify the group without inventing a configuration property.
Schema errors include short invalid values when safe to display. Values from
password settings, commands, environment matches, and parameter selections are
omitted, as are values that appear to contain credentials. YAML syntax errors
identify the line and column and the enclosing property when identifiable;
source excerpts stay in the private `Diagnostics:` log. That log also retains
full commands, tool output, exceptions, and tracebacks.

Correct each reported file as its owner, then rerun the same installation command.
For example, an obsolete `trigger.notifications` property must be removed or replaced
with the supported [notification settings](notifications.md).
The installer does not migrate, rewrite, or skip invalid jobs. The existing release,
job files, and schedules remain unchanged on this refusal; prepared staging files
are cleaned up. Package-manager changes made while preparing dependencies are
outside deployment rollback.

These checks do not require backup sources or destinations to be available.
Configuration validation during managed-job refresh and runtime readiness checks
for schedules being resumed remain in place, including application directory
ownership. This catches changes made after preflight. Failures after maintenance
begins use the rollback and resume procedure described above.

The final report distinguishes an abort before activation, successful rollback,
and incomplete rollback. `Previous installation preserved` is reported only after
cleanup or restoration and schedule resumption succeed. Incomplete rollback retains
recovery snapshots and reports their paths; some schedules may remain paused.
A fresh-install abort reports that no deployment was activated.

### Installer candidate fails `--version`

The installer checks the candidate in staging before activating it. If a command
fails, the installer marks its stage `FAILED` and shows the actionable cause.
The complete subprocess output is retained in the private transcript named
by `Diagnostics:`; inspect that file if the displayed context is insufficient. A successful `vaultkeep --version` after a staging failure
checks the existing installation and does not mean the candidate passed.

For `ModuleNotFoundError: No module named 'vaultkeep.cli.output'`, the source
checkout is incomplete. From the checkout directory, check:

```sh
test -f src/vaultkeep/cli/output.py
git ls-files --error-unmatch src/vaultkeep/cli/output.py
```

Both commands should succeed. If the file exists locally but is absent from Git,
include it in the source commit before updating the target host. With a complete
checkout, repeat `sudo ./install.sh install`. The installer pauses existing
launchers, refuses active backup work, refreshes managed units, validates jobs, and
resumes only previously active launchers after success or completed rollback.

<a id="installer-reports-inconclusive-unit-properties"></a>
### Installer reports unexpected systemd state or a failed property query

The installer identifies the affected account and unit, then gives the failed
condition and actual systemd state values. For example:

```text
  Configuration validation FAILED (1s)
Issue: User 'bisquser', unit 'vaultkeep-trigger@Bisq_sd0-srv_backups-633490673caaf382.service'
       Unexpected systemd state: activating/auto-restart.
Installation aborted. Previous installation preserved.
```

The stage includes checks of installed launchers as well as YAML. A systemd-state
refusal does not mean the YAML is invalid. Inspect the named unit as its owning
account, resolve the reported state, then retry. Query failures identify missing
properties or the manager's error. Missing files, access failures, and internal
failures are reported separately from invalid configuration. Technical details
remain in the diagnostic transcript. Colors highlight status values.
Redirected output stays plain text. Set `NO_COLOR=1` or add `--no-color` after the
install/update/uninstall mode to disable color; `TERM=dumb` also uses plain text.
See [terminal output](cli.md#terminal-output).

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

## Uninstall unit checklist

Before [uninstalling](../README.md#uninstall-the-application), inventory every affected
account, including accounts without a current login. Prefer `schedule disable` or
`trigger disable` with each job's actual YAML path and parameter selection. If the
job file is missing or a unit remains, inspect and disable its exact systemd name.

1. In each owning user manager, list timer and monitor units, then disable each
   relevant installed unit (replace the example names):

   ```bash
   systemctl --user list-unit-files 'vaultkeep@*.timer' 'vaultkeep-trigger@*.service'
   systemctl --user disable --now vaultkeep@app.timer
   systemctl --user disable --now 'vaultkeep-trigger@<job-id>-<identity>.service'
   systemctl --user list-units --all 'vaultkeep@*.service' 'vaultkeep-trigger-exec@*.service'
   systemctl --user list-jobs --no-pager
   ```

2. Inventory root timers and disable each relevant installed instance:

   ```bash
   sudo systemctl list-unit-files 'vaultkeep@*.timer'
   sudo systemctl disable --now vaultkeep@app.timer
   sudo systemctl list-units --all 'vaultkeep@*.service'
   sudo systemctl list-jobs --no-pager
   ```

3. Let backups, workers, and queued work finish. A stopped monitor does not establish
   that its workers have stopped. Resolve unreachable managers and retain uncertain
   outcomes for [evidence review](trigger/recovery.md); do not bypass installer refusals.
   For custom unit names, paths, or parameterized jobs, inspect the exact names in
   `status --verbose` or the unit inventory instead of guessing.

After uninstall, each account should inspect remaining local units and drop-ins:

```bash
systemctl --user list-unit-files 'vaultkeep*'
```

Back them up and remove only confirmed obsolete local units/drop-ins, preserving
unrelated files, then run `systemctl --user daemon-reload` in that manager. User-owned
enablement links, masks, and overrides are not removed by the installer. Keep user
core data and Trigger recovery evidence unless deliberately retiring them after
all work is gone and outcomes have been reviewed. See the cleanup boundaries below.

## Uninstall boundaries and recovery evidence

[Disable automation and drain work before uninstalling](../README.md#uninstall-the-application),
including affected accounts without a current login. The installer refuses active
work and enabled schedules in running managers; it does not remove user-owned
links, masks, overrides, or local units. Inspect each user's unit files and remove
only obsolete managed monitor units/drop-ins after backing them up, then reload
that manager. Preserve unresolved work and [Trigger evidence](trigger/recovery.md).

Normal uninstall removes deployment files, the managed command link, shared templates,
and managed temporary data, while preserving job configuration, secrets, local job
state, and every backup destination. `--purge` additionally removes managed core
configuration and state (`/etc/vaultkeep` and `/var/lib/vaultkeep` by default).
It preserves every user's core data and Trigger state, requests, launch records,
receipts, logs, and recovery blocks. Neither mode removes archives, wallets,
hook executables, or installer-added Debian packages.

Retire user data only deliberately, after workers and queued work are gone and all
uncertain outcomes have been reviewed. Never erase recovery evidence to bypass an
installer refusal. A custom layout requires the same installer overrides used for
installation, including during uninstall.
