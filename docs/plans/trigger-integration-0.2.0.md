# Optional Trigger integration for Vaultkeep 0.2.0

## Objective and release scope

Integrate Vaultkeep Trigger into this repository as a second, separately buildable
and installable Python distribution. Release `vaultkeep` and `vaultkeep-trigger`
together as `0.2.0`, using one repository, one release tag, and the existing root-run
`install.sh`.

The default installation contains only Vaultkeep core. `--with-trigger` adds Trigger,
its Python dependencies, its command, and its user service templates. Both components
use the same versioned deployment and virtual environment below `/opt/vaultkeep`.
Installing software never starts or enables Trigger monitoring.

Maintenance is operator-managed. Before changing an existing deployment, operators
pause its core timers and Trigger monitors and let backups/workers finish. The
installer verifies quiescence, preserves enablement, and leaves schedules paused
after success or rollback; operators validate and resume them explicitly.

Both packages target Debian 13 `trixie` with Python 3.13 or newer. Existing component
selection survives updates and same-version reinstalls. An existing core-only
installation can upgrade to 0.2.0 with or without Trigger, and Trigger can be added
to an existing 0.2.0 core installation without changing the release version.

Preserve Trigger's existing public command, configuration semantics, paths, unit
names, recovery protocol, and application support: Electrum, Bisq 1, and Wasabi.
Broader application support, new backup modes, a new scheduler, automatic interpreter
provisioning, and fixes to unrelated Trigger limitations are outside this integration.
The installer continues to uninstall the whole managed deployment; a new command for
removing only Trigger is outside this release's scope.

This document specifies the 0.2.0 integration. Implemented procedures are in the
[root user guide](../../README.md); local checks and remaining live release gates
are recorded in [development/testing](../development/testing.md#validation-record).

## Source baseline and verified constraints

The repositories were inspected at Vaultkeep revision `f4e84f9` and Trigger revision
`b24ebda`, including both root `AGENTS.md` files, packaging, source, tests, service
templates, and operational/design documentation. Both working trees were clean at
inspection. The import source is `../vaultkeep-trigger`; integration work must copy
from it without modifying it. Preserve any subsequent working-tree changes in either
repository. Work stays uncommitted and unstaged on the current branch.

Relevant findings from the existing implementation:

| Area | Verified behavior | Required integration change |
| --- | --- | --- |
| Core package | Root `pyproject.toml`, `src/vaultkeep`, Hatchling, version 0.1.0, Python >=3.11 | Preserve the core distribution boundary; raise its baseline to 3.13 and coordinate version 0.2.0 |
| Trigger package | Own Hatchling project, `src/vaultkeep_trigger`, version 0.1.0, Python >=3.13, `psutil>=6.1,<8` | Import as a separate subproject; preserve its CLI and process boundary |
| Deployment | `install.sh` stages source plus a venv, moves it to `releases/<version>`, then reinstalls the package to repair console-script paths | Stage the selected packages together and repair both entry points after a move |
| Selection | Version/source matches skip staging; no component selection exists | Compare version, source digest, and components; a component addition requires staging |
| Metadata | Install manifest and `deployment.json` use schema 1; uninstall accepts an exact manifest field set | Introduce component-aware metadata with explicit legacy-schema support |
| Rollback | Transaction snapshots cover the core link, templates, manifest, and timer registry | Include Trigger artifacts and selection, and clean up failures before activation too |
| Activity checks | Only active system `vaultkeep@*.service` instances are checked | Also check Trigger monitors, workers, and queued jobs in running user managers |
| Trigger deployment | Current instructions manually install per-user units pointing into a checkout venv | Provide shared user templates and an explicit migration procedure for overriding local units |
| Trigger persistence | Fixed account-home state, durable requests/launches/receipts/blocks, separate runtime lock | Leave these paths and schemas unchanged; installer rollback and purge must not remove evidence |
| Tests | Core has installer dry-run/static assertions and opt-in Debian gates; Trigger has fake-process/systemd tests | Add executed installer transaction coverage and installed cross-package tests |

Core references: [packaging](../../pyproject.toml), [installer](../../install.sh),
[installer tests](../../tests/unit/test_installer_script.py),
[release gates](../../tests/integration/test_release_gates.py),
[CLI dispatch](../../src/vaultkeep/cli/commands.py),
[workflow](../../src/vaultkeep/workflow.py), and
[current architecture](../../architecture_and_design.md).

Trigger source references relative to its repository root are `pyproject.toml`,
`src/vaultkeep_trigger/{cli,config,paths,environment,preflight,systemd,monitor,executor,state,models,version}.py`,
`systemd/`, `tests/`, `README.md`, and `docs/`. Preserve the substantive operational
references and remaining deployment checks when importing its documentation.

## Platform and Python policy

Debian 12's default Python is 3.11; Debian 13's is 3.13. Raising core's requirement
therefore ends Debian 12 support for new 0.2.0 installations and updates, including
core-only installations. This is a deliberate release compatibility change, not an
incidental consequence of selecting Trigger. See Debian's package references for
[bookworm Python](https://packages.debian.org/bookworm/python3) and
[trixie Python](https://packages.debian.org/trixie/python3).

Implementation must:

- Set both distributions' `requires-python` to `>=3.13`; align core's Ruff target,
  mypy Python version, CI, dependency locks, examples, and release documentation.
- Restrict 0.2.0 install/update host checks to Debian 13. Verify the actual interpreter
  used to create the release venv is >=3.13; an unexpected older `python3` must fail
  clearly before apt, staging, or deployment mutation. If Python is absent on a clean
  Debian 13 host, apt may install the declared prerequisite; check its version before
  creating a venv. Use Debian's interpreter and `python3-venv`, without provisioning
  another Python or replacing system Python.
- Retain amd64 and arm64 as the release validation architectures. Keep real systemd,
  archive-tool, CIFS/NFS, encryption, and restore gates relevant to core.
- Reject a Debian 12 install/update before apt or deployment mutations. Tell existing
  users to upgrade the OS to Debian 13 first, then run the 0.2.0 update. The existing
  0.1.0 installation and its installed uninstall procedure remain available on
  Debian 12; do not claim 0.2.0 support there with a manually installed interpreter.
- Read schema-1 installation metadata on a supported host so an existing 0.1.0 core
  deployment can upgrade without reinstalling or moving its configuration/state.

Keep packaging's Python minimum separate from the tested platform matrix: declaring
`>=3.13` does not claim every future Python or Debian release is validated.

## Repository and package layout

Keep the current core layout. Import Trigger into a self-contained subproject:

```text
pyproject.toml                         # vaultkeep distribution
src/vaultkeep/
install.sh                            # sole production installer
requirements.in / requirements.lock   # core production/build dependencies
requirements-trigger.in / requirements-trigger.lock
requirements-dev.in / requirements-dev.lock
components/trigger/
  pyproject.toml                       # vaultkeep-trigger distribution
  README.md                            # concise package description
  LICENSE                              # matching license/attribution
  src/vaultkeep_trigger/
  examples/config.toml                 # all three instances disabled
  systemd/vaultkeep-trigger.service
  systemd/vaultkeep-trigger-exec@.service
  tests/
docs/trigger/                          # detailed operator references
docs/design/trigger.md
docs/development/testing.md
docs/plans/trigger-validation.md
```

Use normal Python distributions rather than a core extra that bundles Trigger into
the core wheel. Installing the root project must not install `vaultkeep_trigger`,
`psutil`, or the `vaultkeep-trigger` entry point. Installing the Trigger project
provides only its own package, dependencies, and console script. Its Vaultkeep
integration remains an external executable configured by absolute path, without
imports from core internals or a new Python dependency on `vaultkeep`.

Both project metadata versions, Trigger's reported version, and its reviewed
Vaultkeep contract version become `0.2.0`. Keep core's installed-metadata version
lookup. Add release assertions that all reported and declared versions agree; do not
introduce a shared version service or custom build system. Trigger retains
warning-only handling of a reviewed-contract mismatch for configured external core
executables.

Point the imported project's repository, homepage, and issue URLs at the Vaultkeep
repository. Preserve the imported code's applicable license and attribution.

Build a wheel and sdist for each distribution. Each wheel must contain only its own
Python package and entry point. Make Trigger's sdist buildable independently, with
no files referenced outside its source root. The root source distribution must also
contain the optional subproject, both production locks, examples, templates, and
installer support files so `install.sh --with-trigger` works from an extracted root
sdist without the neighboring repository. Keep detailed user documentation centralized
under root `docs/`; the component README is not another installation guide.

Retain the component's own test configuration and run its suite in a separate pytest
process from its project directory. This preserves its existing `tests.conftest` and
`tests.test_monitor` imports without collision with core's test tree. Root CI must
explicitly execute both suites and the cross-package tests; root pytest alone must
not be described as exercising Trigger.

## Dependencies and staging

Keep the current hashed-lock installation model. The core lock contains core runtime
dependencies and the build backend. A combined lock contains those same pins plus
Trigger's `psutil` dependency and its transitive requirements, if any. Constrain the
combined resolution to core's pins so selecting Trigger cannot silently change core
dependency versions. Generate and verify hashes for Python 3.13 on both supported
architectures, including binary availability for `psutil`.

The installer chooses the lock from the resolved component selection, installs it
with `--require-hashes --only-binary=:all:`, then installs the selected local projects
with `--no-deps --no-build-isolation`. Do not resolve dependencies opportunistically
during package installation. Keep developer/test tools in the development lock; optional
desktop notifications continue to work without making `notify-send` a required
dependency. No compiler toolchain is added solely to build `psutil` on release hosts.

Use one fresh venv per complete deployment. A combined deployment contains core and
Trigger in that same venv. Never add Trigger with pip directly into the active venv.
After staging moves to its final location, reinstall every selected local package
to repair absolute console-script paths and verify both commands there. Perform
`pip check`, package/version checks, core example schema validation, and validation
of the disabled Trigger example with the staged core executable, without invoking
monitoring, user jobs, or backups.

The Trigger example uses `/usr/local/bin/vaultkeep` for deployed operation. During
staging, validate an isolated temporary copy pointing at the candidate executable;
do not accidentally validate against the old active core or require an already
installed core on a fresh host. Do not modify the shipped example or user config to
perform this check.

## Installer interface and component selection

Extend the existing modes without adding another installer:

```text
sudo ./install.sh install [--with-trigger] [--dry-run]
sudo ./install.sh update [--with-trigger] [--dry-run]
sudo /opt/vaultkeep/current/src/install.sh uninstall [--dry-run] [--purge]
```

`--with-trigger` is additive and valid only for install/update. Its absence never
removes a previously selected component. Component selection is global to the
deployment; monitoring configuration and enablement remain per-user.

| Existing deployment | Invocation | Resulting components |
| --- | --- | --- |
| None | `install` | Core |
| None | `install --with-trigger` | Core + Trigger |
| Legacy schema 1 core | `update` from 0.2.0 | Core |
| Legacy schema 1 core | `update --with-trigger` from 0.2.0 | Core + Trigger |
| 0.2.0 core, matching source | `install --with-trigger` or `update --with-trigger` | Rebuild as core + Trigger at 0.2.0 |
| Core + Trigger | `install` or `update`, without the flag | Preserve core + Trigger |
| Either selection, matching version/source/components | `install` or `update` | Idempotent verification/reconciliation |

A matching version, source digest, and component selection does not by itself prove
the deployment is usable. Before taking the no-rebuild path, verify:

- The selected distribution names/versions and locked dependency versions in the
  release venv, with no unselected Trigger distribution, `psutil`, or Trigger entry
  point in a core-only venv. Account for the expected venv bootstrap/build tools.
- `pip check`, package imports, and both applicable console-script versions and
  final-release interpreter paths.
- The current pointer, managed command-link targets, and selected managed templates
  against the manifest and trusted source; check effective units in live managers
  for overrides and conflicts without starting them.

Rebuild a missing or inconsistent installer-owned application environment through
the same protected transaction used for a same-version refresh, retaining the
manifest's component selection. Reconcile missing managed links/templates only after
ownership validation and quiescence. Unrelated links, modified user templates, masks,
and drop-ins remain conflicts to resolve explicitly; rebuilding the venv does not
authorize overwriting them. Exact-match verification and repair do not weaken the
existing rejection of a same-version, changed-source `update`.

These checks detect package, dependency, entry-point, and managed-artifact
inconsistency. Package versions and `pip check` do not establish arbitrary installed
Python file integrity; do not claim comprehensive tamper detection or add another
persistent integrity inventory for this release.

Continue rejecting `update` without a manifest, downgrades, and same-version updates
with a different source digest. Preserve the existing `install` behavior that allows
a same-version source refresh. Reinstall/update must resolve persisted selection
before deciding whether the candidate matches; version and source equality alone
are insufficient.

Validate component metadata rather than guessing from the filesystem or installed
Python packages. Schema 1 means core-only. Unknown schemas, invalid component types,
duplicates, unknown names, or Trigger without core are errors before deployment
mutation. A broken schema-2 manifest must never silently become a core-only install.

Dry runs must show the previous and desired selections, interpreter/dependency plan,
release destination, links/templates, activity checks, retained release, and preserved
data. They must not run apt, build packages, create locks/directories, modify files,
reload managers, or change services. Report activity-check limitations explicitly;
an apply operation always repeats live checks.

## Deployment layout and metadata

Keep `/opt/vaultkeep/current` as the single active-deployment pointer. Use distinct
final deployment paths for the two selections:

```text
/opt/vaultkeep/releases/0.2.0/                # core
/opt/vaultkeep/releases/0.2.0-with-trigger/   # core + Trigger
```

These are deployment identifiers; both distributions still report `0.2.0`. Adding
Trigger at the same version can therefore stage a complete second deployment and
retain the previous core deployment without modifying it. Do not derive an active
release path solely from the package version. Read and validate the actual path
from deployment metadata/current. Future versions use the same naming rule.

Continue using `install-manifest.json` for installer ownership and `deployment.json`
for per-release identity. Add schema-2 fields for the normalized selected component
list and component-owned artifacts. Keep the existing core manifest fields to reduce
migration churn; Trigger's optional command and unit files belong in the existing
typed `owned_artifacts` records. Record the actual active/retained deployment paths,
source digest, version, and selection consistently. The per-release metadata must
make a retained deployment's component set discoverable.

Read schema 1 as core-only; write schema 2 only as part of a successful transaction.
Support uninstall validation of both known schemas on supported hosts. Update the
current exact-field-set validator deliberately, keeping schema-specific allowed
fields and path allowlists. Validate prefix confinement, command-link targets,
artifact types, and the two explicit systemd roots. Never use manifest contents to
authorize arbitrary paths or deletion inside user homes.

Create these command links when their component is selected:

- `/usr/local/bin/vaultkeep` -> `/opt/vaultkeep/current/venv/bin/vaultkeep`
- `/usr/local/bin/vaultkeep-trigger` -> `/opt/vaultkeep/current/venv/bin/vaultkeep-trigger`

Refuse collisions with unrelated files or symlinks. Preserve user-managed masks,
overrides, and locally modified templates; report a conflict for operator resolution
rather than silently overwriting them. Managed unchanged artifacts can be reconciled
from the candidate. Include all new paths in installer testing overrides so tests
cannot accidentally write real user templates or executable links.

Retain the previous active deployment after a successful version upgrade or component
addition. Exact-match reconciliation must not rotate retention to itself. Preserve
the current same-version source-refresh semantics, including transaction rollback of
the replaced directory and the existing retained release. Do not add an unrelated
release garbage collector.

## User services and operational boundaries

When Trigger is selected, install root-owned, mode-0644 shared user templates at:

- `/etc/systemd/user/vaultkeep-trigger.service`
- `/etc/systemd/user/vaultkeep-trigger-exec@.service`

Render their fixed executable as `/usr/local/bin/vaultkeep-trigger`. The monitor uses
the existing default configuration resolution; a user can select a custom config
through a documented per-user drop-in resetting `ExecStart` and supplying `--config`
before `monitor`. The worker stays exactly `exec-worker %i`, reading its frozen
request rather than the user's current configuration.

Preserve the monitor's restart behavior and the worker's `Type=oneshot`, `Restart=no`,
`TimeoutStartSec=infinity`, `UMask=0077`, and independence from monitor stop/restart.
Do not add lifecycle dependencies that terminate workers with the monitor.

The installer must not start/enable monitors or workers, apply presets, enable
lingering, create user config/state, or run production jobs. Ship the disabled TOML
example within the release. Users explicitly copy it to their config path without
overwriting an existing file, review identities/jobs, run static/runtime checks, and
enable monitoring only when their deployment checks pass. Installation needs no
graphical session, logged-in target user, or reachable user manager on a clean host.

User-owned copies and masks take precedence over shared templates. Migration and
health checks must inspect the effective `FragmentPath`, drop-ins, and `ExecStart`;
installing a shared file cannot update a legacy per-user copy. This precedence is
documented in the [systemd unit search-path reference](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html).
Validate behavior on Debian 13's shipped systemd rather than treating the latest
upstream manual as evidence of an executed deployment test.

## Maintenance and activity checks

Before an update, same-version replacement, component addition, detected environment
repair, or uninstall changes an existing shared deployment, operators inventory its
root and user core timers and Trigger monitors. Record which schedules were active
and their enablement states, then stop the active timers and monitors and let running
backups, workers, and queued work finish. Monitor stops do not cancel workers. Pause
timers with `systemctl stop`, not the core CLI's `timer disable`, which also disables
the timer and clears its persistent timestamp.

During install/update, preserve enablement links and timer timestamps in place,
including runtime enablement, masks, and user overrides. Leave paused schedules
stopped after either success or rollback; operators validate the installed or
restored deployment and explicitly resume only the schedules they previously had
active. The installer does not wait for work to drain or pause/resume schedules.
Active work causes a clear refusal, and the installer never terminates it.
Uninstall additionally requires users to disable their schedules so user-owned
enablement does not point at removed software.

Extend the installer activity check as follows:

1. Check root `vaultkeep@*.timer` and `vaultkeep@*.service` instances using the shared
   deployment, including loaded state and queued jobs. For transactions changing an
   existing shared deployment, and fresh installs introducing shared Trigger templates,
   enumerate running systemd user managers, including lingering users, through the
   system manager rather than checking only the invoking user's session. This also
   protects standalone Trigger installations using a legacy shared core.
2. Query each manager as its account with its UID, validated `XDG_RUNTIME_DIR`, and
   user-bus context. Use bounded commands and explicit unit/property/job checks for
   user `vaultkeep@*.timer`, `vaultkeep@*.service`, Trigger monitors, and worker
   instances. Refuse active or transitioning schedules, running/activating/stopping
   backups or workers, queued relevant jobs, or ambiguous results. An unreachable
   running manager is an error, not proof that its work is absent. Do not collapse
   `is-enabled` into a Boolean or treat a failed query as a disabled unit.
3. Check existing Trigger monitor locks in the enumerated users' runtime directories
   as a guard against foreground monitors outside the standard unit. Do not create,
   write, or unlink lock files, or signal processes to force a check to pass. Require
   operators to stop custom launchers and standalone/manual core runs too; service
   and runtime-lock checks cannot prove arbitrary custom executions are absent.
   Supplement them with held core job-lock checks in known locations and read-only
   process checks for recognizable foreground monitors/manual core runs. Positive
   evidence blocks apply; an empty scan never proves race-free quiescence. Keep
   diagnostics limited to the account, unit or PID, and reason for refusal.
4. Repeat the checks immediately before activation/replacement/removal. The installer
   lock serializes installer transactions; it does not lock users out of starting
   programs. Document that users must not start monitors, jobs, or new relevant
   sessions during the maintenance window. Refresh the live-manager inventory before
   the final check so a newly appeared manager is not silently omitted.
5. Reload the system manager for core templates and the running user managers for
   changed shared user templates, without starting services. A reload failure during
   install/update is a transaction failure. Report the affected account/action.

A fresh core-only install does not require user-manager checks. No running user
manager is a valid clean-host situation, not an error. Quiescence checks do not
require clearing durable completed/unresolved history; preserve those records for
normal read-only inspection and reconciliation.

Manual resumption preserves normal scheduling semantics. Core's persistent calendar
timers can catch up a run missed while paused. The installer never explicitly starts
a completed backup service or replays a Trigger attempt; that is separate from a
legitimate new calendar activation or Trigger's existing pending-request recovery.
Document and test catch-up rather than suppressing it or clearing timer timestamps
to claim that no backup can run immediately after resumption.

## Transaction and rollback behavior

Extend the existing shell transaction rather than creating a second deployment
manager. Keep the currently installed selection and artifacts intact until candidate
packages and templates have passed staging checks.

The apply sequence is:

1. Validate arguments, host, source, existing metadata, requested selection, and
   artifact ownership; acquire the installer lock and perform activity checks.
2. For an exact version/source/component match, verify the existing deployment as
   specified above. When installation, source/component changes, or detected damage
   require a candidate, install system dependencies and build a complete deployment
   from the selected hash-locked dependencies and local distributions.
3. Validate the staged console scripts, selected package versions, examples,
   dependency consistency, package inventory/isolation, and systemd templates without
   starting any service. Skip candidate-only checks when verification found no need
   to rebuild.
4. Recheck quiescence. Snapshot the previous manifest, current pointer, both command
   links as applicable, core templates/registry, and Trigger templates. For a
   same-version source refresh, preserve the directory being replaced until commit.
5. Move the candidate to its final path, repair and validate all selected entry
   points, then install selected managed templates and command links, atomically
   switch `current`, and write component-aware metadata. If a same-version refresh
   replaces the active directory, its replacement and finalization occur inside this
   protected transaction. Reload affected managers and run read-only post-activation
   checks of the selected commands, package inventory/dependencies, links, templates,
   and effective managed files. These checks all finish while schedules are paused.
6. Commit the transaction only after every required validation succeeds; retain the
   previous deployment for version/component transitions. Clean transaction scratch
   files without changing user evidence. Report that operators must validate their
   actual jobs/configuration and resume schedules explicitly. Operator restart
   failures after installer success do not trigger an automatic software rollback
   beneath any newly started backup.

If no candidate is needed, reconcile only missing owned artifacts through the
protected transaction; do not replace the active release or rotate its retained
release. A healthy exact-match verification requires no deployment mutation.

Handle `ERR`, `INT`, `TERM`, and `HUP` during staging and the protected transaction.
Prevent reentrant cleanup and return a nonzero result for failure or interruption.
On ordinary command failure or a handled interruption, restore the previous pointer,
command links, templates, manifest/selection, and core registry, remove only
transaction-created artifacts, reload affected managers, and report rollback results.
A failed first combined install must leave no Trigger command or installed templates;
a failed Trigger addition must leave a working core-only deployment and its original
manifest. A failed combined update must leave both old packages selected and usable.
Attempt each required restoration even if another restoration fails. Surface every
rollback failure with the affected path, account, or unit; retain necessary recovery
snapshots and report their location instead of deleting them or claiming success.
Schedules stay paused, and user configuration/state is never restored from an older
snapshot over newer evidence.

Cover failures before the current commit trap as well as after activation: venv
creation, dependency installation, package installation, post-move entry-point
repair, template validation, and staging cleanup currently fall partly outside the
commit snapshot. Track staged/final candidate paths explicitly. Never remove a
pre-existing release simply because a candidate used the same directory name.

The rollback boundary covers managed deployment artifacts. Apt changes cannot be
rolled back as part of this transaction. Packaged inactive examples may be refreshed;
user TOML/YAML, credentials, archives, and Trigger recovery evidence are never rollback
scratch space. Do not claim atomic recovery from SIGKILL or power loss across every
external artifact: retain the prior release, identify incomplete staging, and document
inspection/reconciliation without deleting runtime evidence or replaying attempts.

## Runtime and recovery compatibility

Import Trigger's behavior without merging it into core or changing its persistence
formats. Keep all existing schema constants at 1 unless a separately justified data
format change is required; a package version bump alone does not change a schema.

Preserve:

- `vaultkeep-trigger --version`, `--config`, `monitor`, `check-config`, read-only
  `status [instance-id]`, and internal `exec-worker <attempt-id>`, with their current
  exit semantics and argument order.
- Config resolution through explicit `--config`, absolute `XDG_CONFIG_HOME`, and the
  account-home fallback; fixed Trigger state at
  `<account-home>/.local/state/vaultkeep-trigger`, independent of `HOME` and
  `XDG_STATE_HOME`; the lock at
  `$XDG_RUNTIME_DIR/vaultkeep-trigger/monitor.lock`.
- Instance IDs, application values, confirmation/retry rules, private permissions,
  request/attempt UUIDs, frozen environments/settings, receipts, launch evidence,
  per-instance durable blocks, safety annotations, summaries, and retention rules.
- Invocation of the configured absolute Vaultkeep executable through
  `--user --config <job> [--param KEY=VALUE ...] run`, with argument arrays and the
  user's existing runtime lock namespace.
- Core's trailing `run: created` / `run: unchanged` results, exit-10 contention,
  conservative unknown outcomes, warning-only version checks, and advisory deadlines
  that never automatically kill a worker.

Re-review the 0.2.0 installed CLI contract before updating the reviewed-version
constant. Core `validate` currently does not probe destination writability or execute
hooks; Trigger runtime preflight cannot certify source consistency, wallet identity,
writer coverage, or destination independence. Keep these limitations accurate.

Carry forward Trigger's documented outstanding validation gaps, including matcher
overlap, generic interpreter entrypoints, unbounded regex work, numeric systemctl
rejection classification, synchronous manager probes, and pending deployment checks.
Do not silently fix or advertise these as solved by repository integration. Actual
wallet identities and all writers must still be reviewed before an instance is
enabled. Release tests use disposable fake applications/jobs, not production wallets.

## Migration procedures

### Existing core-only installation

Document OS prerequisites first. On Debian 13, a 0.1.0 schema-1 deployment can use
`install.sh update` from the 0.2.0 checkout to remain core-only, or add
`--with-trigger` to upgrade both components together. Preserve root/user jobs,
secrets, state, timers and their enablement, destinations, and the previous deployment.
No jobs or state directories move.

For an existing 0.2.0 core deployment, document both
`install.sh install --with-trigger` and `install.sh update --with-trigger` with matching
source, including dry runs. Explain that this stages another complete deployment;
it is not an in-place pip install. Later plain updates preserve Trigger automatically.

### Existing standalone Trigger installation

Provide an explicit per-account procedure:

1. Inventory the effective unit files, custom config/drop-ins, selected executable,
   and current enablement. Stop monitoring, drain workers/start jobs, inspect status,
   and preserve a private copy of config and fixed state. An uncertain attempt needs
   the existing recovery review; never clear it to simplify migration.
2. Install or update the shared deployment with `--with-trigger`. Keep the standalone
   checkout/venv and original units available until the shared deployment is verified.
3. Back up and remove only the legacy per-user unit files being replaced so the
   shared templates can load. Preserve required custom configuration through a
   reviewed drop-in; resolve executable overrides and masks explicitly. Disable and
   later re-enable old unit links when needed to retarget them; record prior intent.
   The root installer does not edit user homes to perform this migration.
4. Keep TOML, YAML job paths, and the fixed state root in place. Retain a configured
   absolute external Vaultkeep path when still valid. If switching that path to the
   shared command, edit current config explicitly after drain; never rewrite frozen
   historical requests to point at another executable. Retain old executables while
   any protected evidence may still need them.
5. Reload that user's manager; inspect both effective units and run the shared command's
   version, static check, runtime check, and read-only status. Verify existing state
   remains readable and pending/active/review evidence is preserved under the same
   IDs. Configuration changes follow existing invalidation/reconciliation rules.
6. Start/enable the monitor explicitly only after validation and instance deployment
   checks pass. Remove the old environment only after no unit, protected record, or
   operator tooling depends on it. Preserve the source repository itself.

Migration rollback restores the saved per-user units and prior software after
quiescence; it preserves current recovery evidence. Never restore an old state copy
over newer execution records after monitoring has resumed.

## Uninstall and data preservation

Normal uninstall removes all selected, installer-owned application artifacts: shared
release tree, command links, core system templates, selected Trigger user templates,
and existing core temporary/registry files. It must use the actual manifest paths and
support both component selections. Refuse active/queued work before any removal;
failed quiescence must leave the installation intact.

Before combined uninstall, each affected user disables their monitor and drains
workers. The root installer removes its shared files and reloads running managers,
but never recursively edits user homes or removes user-owned masks, drop-ins, or
enablement links. Document explicit per-user unit/link cleanup, including offline
accounts, and verify no obsolete enablement will activate a later reinstall.

Normal uninstall preserves core config/job state as today, and preserves every user's
Trigger config, state, receipts, launch records, logs, blocks, and recovery warnings.
`--purge` retains its core meaning for managed `/etc/vaultkeep` and
`/var/lib/vaultkeep`; it never traverses homes to purge Trigger state or user-mode
core data. Wallet data and backup destinations are untouched in every mode. System
dependencies are not automatically removed.

Document Trigger data cleanup as a separate explicit user operation after evidence
backup, worker absence, and recovery review. Do not present Trigger state as a
disposable cache like core's reconstructible local state. A corrupt manifest or
ownership/path conflict must fail safely rather than authorize broader deletion.

## Documentation changes during implementation

Update user-facing documentation when the integration is implemented. Keep the root
[README](../../README.md) the operational guide, with numbered executable procedures
and expected results in this order: overview, Debian/Python prerequisites, both install
variants, configuration/validation, explicit enablement, health checks, maintenance,
troubleshooting, migration/update, and uninstall/optional cleanup.

Include enough core and Trigger instructions for normal operation without requiring
users to reconstruct the workflow from design documents. Explain the Debian 12
compatibility break, all-disabled examples, additive selection, same-version addition,
shared commands, user service scope, manual maintenance coordination, and retained
evidence. Include root/user timer and monitor inventory, pause/drain checks, unchanged
enablement, explicit post-validation resumption, normal timer catch-up, and recovery
when software rollback or a later manual restart fails.

Supporting changes:

- Update [root mode](../root-mode.md) and [user mode](../user-mode.md) for the new
  baseline and optional component, keeping root jobs distinct from Trigger's user jobs.
- Import Trigger's CLI, configuration, consistency, process detection, notification,
  service operation, recovery, and troubleshooting references into `docs/trigger/`.
  Adapt installation paths and links while preserving technical detail and limitations.
  Put standalone-to-shared instructions in `docs/trigger/migration.md` and summarize
  the ordinary migration steps in README.
- Update `architecture_and_design.md` in place for two distributions, component-aware
  deployment/rollback, and the new platform. Import Trigger's architecture into
  `docs/design/trigger.md` and link the two designs. Replace obsolete standalone-only
  assumptions with the integrated public CLI contract; do not reorganize unrelated
  core architecture sections.
- Add `docs/development/testing.md` covering both project installs, dependency-lock
  generation, separate suite commands, cross-package tests, and disposable-host gates.
  Carry remaining Trigger deployment checks into `docs/plans/trigger-validation.md`;
  historical standalone test results are not proof of the new integrated release.
- Update [RELEASE.md](../../RELEASE.md) for the coordinated `v0.2.0` tag, two wheel/sdist
  pairs, matching versions, Debian 13 amd64/arm64, both installation selections,
  upgrades, migrations, rollback, and uninstall. Distinguish the core configuration
  schema's v1 terminology from the application release number 0.2.0.
- Correct affected stale claims, including README's statement that the installer
  validates existing jobs and synchronizes timers: the current script does neither
  and does not activate timers. Keep post-install validation and scheduling explicit.

Verify repository-relative links and anchors after importing/moving documentation.
Validate documented shell commands and path quoting. Do not describe proposed or
unverified capabilities as installed functionality.

## Implementation sequence

| Step | Work | Completion criterion |
| --- | --- | --- |
| 1 | Import Trigger source, tests, example, templates, and substantive docs into the stated layout; retain behavior and attribution | Core suite and imported Trigger suite run independently; source repository is unchanged |
| 2 | Set coordinated versions/platform metadata; define core and combined locks; build both projects | Both wheel/sdist pairs build; clean core install contains no Trigger package/dependency/command; combined dependencies are consistent |
| 3 | Implement selection resolution, schema-1 compatibility, schema-2 metadata, and selection-aware release paths | Selection/legacy/error cases and dry runs pass without changing active deployments |
| 4 | Extend staging, exact-match verification/repair, final-path entry-point repair, managed links/templates, root/user activity checks, and rollback | Both install variants and same-version Trigger addition pass executed transaction tests; command failures and handled signals restore the prior deployment with schedules left paused |
| 5 | Integrate user-service operation, standalone migration, and whole-deployment uninstall | Disposable user-manager tests show explicit activation, effective-unit migration, evidence preservation, and safe removal |
| 6 | Recheck the installed 0.2.0 core/Trigger contract; finish operational/design/development/release docs and CI | Both suites, cross-package tests, packaging checks, links, and documented procedures pass |
| 7 | Execute the Debian 13 release matrix with disposable users/apps/jobs and real archive destinations | All release gates pass for both selections on amd64/arm64; no unverified production enablement is implied |

Keep changes focused on integration and necessary regression coverage. Do not broaden
core's strict mypy check to all imported Trigger modules through a blanket setting
that forces an unrelated typing rewrite. Preserve core's existing quality gates;
apply the component's Ruff/format checks and existing tests, with necessary mechanical
import/packaging adjustments. CI must visibly run every required suite.

## Verification and acceptance matrix

Extend `tests/unit/test_installer_script.py` beyond source-string assertions. Use
temporary install/config/state/bin/systemd roots, controlled fake host/apt/systemctl
commands, and explicit failure injection to execute installer behavior. Test candidate
package installation and entry-point relocation in real temporary venvs as well;
mocked pip alone cannot prove installed distributions or shebangs work. Real systemd
tests belong on disposable Debian 13 hosts with dedicated user accounts.

| Area | Required scenarios and assertions |
| --- | --- |
| Package isolation | Build/install each wheel independently; root wheel imports only core, installs no psutil/Trigger command; Trigger wheel contains its namespace and command without core internals; dependency metadata/version/readme/license are correct |
| Source artifacts | Build each sdist independently; extracted root sdist supports both installer variants; extracted Trigger sdist builds its wheel; no neighboring checkout, caches, local venvs, private files, or Git metadata are needed |
| Fresh installs | Core-only and combined, including a host without a running user manager; expected venv packages/commands/units and schema-2 selection; disabled example only; no enable/start/preset/linger calls or home-directory writes |
| Platform | Debian 13/Python 3.13 succeeds for both selections; Debian 12 and an existing incompatible interpreter are rejected before apt/staging/deployment mutation; a clean supported host can bootstrap missing Python and verifies its version before venv creation |
| Legacy upgrades | Realistic 0.1.0 schema-1 fixtures and disposable deployed installations upgrade to core or combined; old current/release, jobs, credentials, registries, timer enablement, and archives are preserved appropriately |
| Selection preservation | Plain update and same-version install keep Trigger selected; repeated flag is idempotent; later-version core and combined upgrades preserve their respective selections; selection is never inferred from stray files |
| Exact-match verification/repair | Matching version/source/selection with missing or wrong package/dependency versions, missing imports, failing pip check, unexpected Trigger/psutil in core, broken entry points, and missing managed links/templates; protected rebuild or ownership-safe reconciliation restores the declared selection; unrelated links and modified templates remain conflicts; package metadata checks are not represented as arbitrary source-file integrity verification |
| Same-version addition | Both install/update with matching source add Trigger to 0.2.0 core; stage a new complete deployment, retain the old core, repair both entry points, persist combined selection, and preserve all user data |
| Version/source guards | Missing update manifest, older candidate, same-version changed-source update, and install across versions fail as specified; same-version source-refresh install retains selected components and rolls back failures |
| Metadata/ownership | Schema-1 and schema-2 parsing, malformed JSON/types, unknown schema/components, duplicates, missing core, invalid paths, tampered links, unrelated command files, modified templates, masks, and optional artifact allowlists fail or preserve safely |
| Dry runs | All modes/selections, additions, and legacy upgrades; correct planned selection/paths; snapshots confirm no writes, apt, package build, reload, service activation, or lock creation |
| Maintenance | Root and multiple user managers, including lingering accounts; active core timers/services, Trigger monitors, foreground locks/processes, manual core runs, activating/stopping workers, queued core/Trigger jobs, unreachable managers, and inconclusive properties block apply without stopping work; no manager is acceptable; final inventory/check catches changed activity or a newly running manager |
| Manual schedule preservation | Start with enabled-active, enabled-inactive, disabled-active, and disabled-inactive timers/monitors; operators stop active schedules before apply; paused activity, exact enablement links, runtime enablement, masks/drop-ins, and timer timestamps remain unchanged through success, refusal, interruption, and rollback; no installer start/stop/enable/disable calls occur during install/update |
| Manual resumption/catch-up | Operators validate and resume only previously active schedules; previously inactive schedules stay inactive and enablement stays unchanged; a persistent calendar timer catches up a missed event normally; installer never directly reruns a completed service or replays an attempt; Trigger retains its existing pending-recovery rules; a failed later manual restart does not roll software back |
| Rollback | Inject failure at dependency/venv/package installation, each entry-point repair, package inventory/import/dependency/example/unit verification, each managed artifact/manifest write, activation, and system/user reload; cover fresh combined install, legacy upgrade, core-to-combined addition, combined update, same-version refresh, and exact-match environment repair |
| Handled interruption | Send INT, TERM, and HUP during staging and at protected replacement/activation/reload checkpoints; verify nonzero exit, appropriate cleanup/restoration, unchanged enablement, paused schedules, retained evidence, and no reentrant cleanup; SIGKILL/power-loss recovery remains a documented operational limitation |
| Rollback evidence | Old pointer, both applicable commands, unit content, manifest/selection, registry, and retained-release bookkeeping are restored; new artifacts are absent; old commands execute; user config/state/evidence byte snapshots remain identical; injected restoration failures are reported individually with affected paths/accounts/units and useful snapshots retained |
| Installed contract | Trigger invokes installed core with --user/config/params; schema-only/full validation behavior, version warning, exact created/unchanged markers, exit-10 contention, other errors/signals, and shared runtime lock contention are verified with disposable jobs |
| Trigger regressions | Preserve current detection/lifecycle/worker/recovery/notifications/retention tests, including read-only status, corrupt-state isolation, unknown-schema blocking, no replay, frozen requests, monitor-gap annotations, and independent worker lifetime |
| Existing state migration | Retained schema-1 instance/request/launch/receipt/block records and safety summaries load under 0.2.0 with the same IDs; pending work rechecks absence; completed work is not rerun; unknown/corrupt evidence remains blocked rather than reset |
| User service migration | Legacy local templates, custom --config, drop-ins/masks, old enablement links, and multiple users; effective units select the shared executable after explicit migration; installer never enables monitoring |
| Uninstall | Both selections, dry-run/normal/purge, schema-1/schema-2, after upgrades/addition, active/queued work refusal, ownership conflicts, and absent optional files; remove only managed artifacts, preserve all Trigger data even under purge, preserve user-mode core data/wallets/backups and unrelated services |
| Documentation/release | Local links/anchors, shell syntax/quoting, both installation procedures, explicit enablement, migration/update/uninstall walkthroughs, consistent 0.2.0 metadata and platform claims, and truthful remaining deployment limitations |

For real release execution, run both installation selections on Debian 13 amd64 and
arm64. Include real first install, 0.1.0-to-0.2.0 upgrade, same-version addition,
later-version update fixtures, injected activation failure, normal uninstall, and
purge. Keep core's local/CIFS/NFS restore drills for `.tar.zst` and encrypted `.tar.7z`.
Use fake wallet processes and dedicated temporary backup jobs to exercise real user
service loading, queued workers, receipts, rejection/absence semantics, and worker
survival across monitor stop. Exercise root and multiple users' timers, manual
pause/drain/validation/resumption, persistent catch-up, exact-match repair, and handled
interruption on disposable hosts. Record exact candidate, host, interpreter, commands,
results, and remaining deployment limits; do not substitute mocked success for these
release gates.

The integration is complete when both packages and all supported transitions meet
this matrix, the operational documentation matches the executed behavior, and the
source Trigger repository and unrelated working-tree changes remain untouched.
