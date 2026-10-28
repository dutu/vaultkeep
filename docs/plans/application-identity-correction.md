# Application identity correction plan

## Objective and scope

Separate raw application configuration from resolved runtime identity and make
execution and validation entry points apply consistent ownership rules.

This document specifies work to implement; it does not describe the correction as
already available. Preserve supported root and user execution, process isolation,
offline safety checks, Trigger dispatch protections, and backup transaction guarantees.

Do not change replacement-history identity, archive metadata, manifests, archive
formats, compatibility rules, retention, or pruning semantics. Do not redesign the
installer or Trigger. Preserve installer rollback and transactional safety. Do not
add compatibility mechanisms for unreleased design changes. Preserve existing
uncommitted and untracked work, including `tests/unit/test_application.py`. Leave
implementation changes uncommitted and do not create or switch branches.

## Verified findings

The relevant implementation is in
[application resolution](../../src/vaultkeep/application.py),
[configuration models](../../src/vaultkeep/config/models.py),
[backup workflow](../../src/vaultkeep/backup/workflow.py),
[schedule management](../../src/vaultkeep/schedule/__init__.py),
[Trigger configuration](../../src/vaultkeep/trigger/config.py), and
[installer maintenance](../../src/vaultkeep/maintenance.py).

| Finding | Verification and qualification |
| --- | --- |
| Configuration and resolved identity are mixed | `ApplicationConfig` is already frozen, but declares private `_owner_uid` state. `resolve_application()` returns another `ApplicationConfig`, populates `user`, and sets `_owner_uid`. The resolver uses that field to distinguish an input override from a resolved username. The original raw object remains unchanged. |
| Reconstruction changes validation behavior | Under non-root execution, resolving an already-resolved object succeeds. Reconstructing that object through `model_dump()` and `model_validate()` loses `_owner_uid`; resolving the reconstructed object raises `user_unsupported`. This was reproduced in memory. |
| Scope and credentials can disagree | `_runtime_owner(False)` returns `(0, 0)`, while application resolution independently uses the effective UID. Backup execution lacks an early system-scope privilege guard. The installer and schedule CLI already enforce root privileges, and Trigger already requires non-root user mode. These existing boundaries limit, but do not eliminate, the inconsistency. |
| Schedule identity validation is missing | `_load_managed()` and `_managed_jobs()` perform parameter and semantic validation without application resolution. Mocked checks accepted an unsupported user-mode override and a nonexistent root-mode target without an account lookup. Installation and bulk validation use these paths. |
| Unsupported-property errors have incorrect precedence | Account lookup precedes the user-mode override check. Existing, missing, and unavailable account lookups produce `user_unsupported`, `user_missing`, and `user_lookup`, respectively. |
| Some ownership state is redundant | The private UID and separately returned UID represent the same resolution result. Canonical username, numeric UID, and the UID frozen into Trigger policy serve distinct purposes. The private UID also contributes to the configuration fingerprint and cannot simply be deleted without preserving that input. |
| Replacement identity uses usernames | Offline replacement identity contains application name, canonical username, and normalized directory, without UID. Account recreation and rename can therefore affect history identity differently from runtime process ownership. This separate design concern remains outside this correction. |
| Minor cleanup and working-tree findings | `SUPPORTED_APPS` has no repository references beyond its declaration in `trigger/config.py`. The untracked application tests were inspected and included in focused validation. |

The fingerprint dependency is in
[source hashing](../../src/vaultkeep/sources/hashing.py). Replacement identity and
history comparison are implemented in
[destination identity](../../src/vaultkeep/destination/identity.py) and
`backup.workflow._active_history()`.

Two additional dependencies must be addressed. `list_backups()` and
`verify_backups()` currently use `load_validated_config()`, which resolves application
accounts even though archive inspection itself does not require them. Thus the
current loader can already block inspection when an account is unavailable; this
must not be mistaken for a necessary archive-verification requirement.
`require_application_offline()` imports Trigger modules from `application.py`, and
Trigger's loader imports the policy constructor from the backup workflow. Those
reverse dependencies must not remain in the identity/preparation dependency chain.

## Root cause and architecture

`ApplicationConfig.user` currently represents either an optional operator override
or a resolved canonical username. `_owner_uid` supplies the provenance needed to
tell those meanings apart. Downstream consumers consequently depend on receiving
a specially prepared `JobConfig` whose application fields contain runtime facts.

Moving the unsupported-property check alone fixes error precedence but leaves this
ambiguity. Removing only `_owner_uid` breaks repeated resolution and removes UID
information from change detection. The correction uses one separate runtime value
and keeps resolution outside the configuration model.

### Dependency direction and responsibilities

Identity-required operations follow one dependency direction:

```text
Backup / Schedule / Trigger / Installer
                  ↓
         Shared job preparation
                  ↓
    Application identity resolution
                  ↓
        Execution-scope checks
```

Shared preparation belongs under `validation` and orchestrates existing loading,
parameter, semantic, and identity mechanisms. It owns no account-selection or
path-resolution implementation. `validation` may import `application`;
`application` must not import `validation`, backup workflows, or Trigger adapters,
including through deferred imports.

Keep execution-scope checks and the resolved identity in the existing lower-level
`vaultkeep.application` module. Put the pure canonical identity/policy snapshot
construction there as well, so Trigger does not import backup workflow code to
build its policy.

Process identification is a shared lower-level capability used by both offline
backup enforcement and Trigger. The existing `detect_spec()` already accepts an
application, directory, matcher, and provider independently of a Trigger instance.
Its remaining coupling is through matcher types in `trigger.config`, observation
types in `trigger.models`, and the provider/matcher imports. Separate those reusable
pieces from Trigger configuration and lifecycle code; keep the thin
`detect(instance, provider)` adapter with Trigger.

Use the existing application module as the shared home for the necessary process
identity types and implementation. Keep offline enforcement there, with backup
owning when it is invoked. Reuse the current detection algorithms and provider;
do not implement a second detector, add a wrapper layer, or redesign monitoring.
This removes application/backup imports of Trigger detection and configuration.
The repository has no other suitable shared inspection module: `archive/process.py`
executes archive-tool subprocesses rather than identifying application processes.

Keep schema models and errors below these operations. Reuse existing modules;
no new process package or generic process framework is needed. Archive inspection
uses configuration loading without making current application identity a prerequisite.

### Raw configuration

Retain the existing frozen `ApplicationConfig` fields:

- `name`: required application selector for detection and environment rules.
- `user`: optional root/system-scope account override.
- `data_dir`: configured application directory.
- `matcher`: configured process identity predicates.

Remove `_owner_uid`. Resolution must not mutate raw configuration, including nested
matcher values, or insert resolved runtime values into `JobConfig`. Retain existing
configuration collection conventions. Frozen Pydantic fields do not make their
nested lists and dictionaries immutable; runtime independence must be established
explicitly at the resolution boundary.

### Resolved application

Introduce one independent frozen `ResolvedApplication` value containing:

- Application name.
- Canonical username.
- Numeric owner UID.
- Canonical absolute data directory.
- Matcher with account-relative paths resolved.

The runtime type must not inherit from `ApplicationConfig`. Change
`resolve_application()` to accept raw application configuration and explicit
`user_mode`, returning this value instead of an application/UID tuple.

The resolved value owns a detached snapshot of all nested matcher structures,
including entrypoints, argument tokens, and environment predicates. A deep copy of
the existing matcher representation at this boundary is sufficient; do not build a
generic immutability framework or duplicate matcher hierarchy. Consumers treat the
snapshot as read-only, and canonical serialization returns fresh containers.
Mutating raw nested values or a serialized dictionary must not alter an existing
resolved identity, its policy, or fingerprint inputs.

Repeated resolution of equivalent raw input produces equivalent results under
unchanged credentials, account records, and paths. Raw serialization and
reconstruction preserve configuration meaning. Runtime serialization preserves its
explicit identity, without private provenance or reinterpretation as YAML input.

Provide one canonical application snapshot for policy and fingerprint consumers.
Preserve the existing application dictionary fields and values; retain UID in the
existing separate policy and fingerprint fields.

### Execution scope

Use the existing `user_mode` flag and actual process credentials. A shared
runtime-owner helper validates scope and supplies the UID/GID ownership policy:

| Scope | Execution requirement | Application owner |
| --- | --- | --- |
| User | Use the process's effective UID | Execution account; reject `application.user` |
| System | Effective UID must be 0 | Root by default; an explicit existing account is allowed |
| Trigger | Retain the existing non-root user-mode restriction | Validated user execution account |

Reject invalid system scope before selecting privileged runtime paths or creating
execution state, acquiring operation locks, staging files, or changing services.
This guard also applies when no application is configured and to inspection paths
before any existing lock or suspect-marker writes. It checks credentials without
requiring an application account. Reject user-mode `application.user` before any
application account lookup, including lookup of a default account.
Resolve the default account by validated execution UID, using account records for
home expansion rather than environment variables or YAML file location.

Replace the independent assumptions in `_runtime_owner()` with the shared helper.
Preserve current hook and password UID/GID rules. Keep application-account selection
in the shared resolver; retain the installer's service account/group checks and
offline identity revalidation for their separate purposes.

No execution-context class, account cache, generic account abstraction, or second
complete job model is required.

## Shared validation and affected components

Add a small job-preparation function under `validation` that composes existing
runtime-parameter and semantic validation with application identity resolution.
Keep pure semantic validation separate from account and environment checks.

### Preparation contract and validation order

The identity-required preparation contract is:

```python
tuple[JobConfig, ResolvedApplication | None]
```

| Input | Result |
| --- | --- |
| No application configured and application identification is optional | Configuration plus `None` |
| Application configured and valid for the execution context | Configuration plus one `ResolvedApplication` |
| Application required by Trigger or offline consistency but absent | Validation error through the existing required-application checks |
| Application configured but its context or identity is invalid | Validation error; never return `None` or fabricate an identity |

`JobConfig` retains raw application fields; runtime-parameter expansion continues
to use existing configuration-copy behavior. Callers pass scope explicitly and
must not use filesystem location or environment variables to infer account identity.

Validate schema and execution scope, reject unsupported contextual properties,
then resolve identity and perform the relevant parameter/semantic checks before
dependent runtime operations. Keep independent template checks available when
runtime parameter expansion is deferred. Configuration-only identity validation
does not require an installed executable, process observation, source availability,
application-directory existence/ownership, or destination readiness. Full runtime
validation and execution apply their existing readiness requirements afterwards,
using the resolved result rather than resolving raw configuration again.

### Single resolution and deliberate revalidation

Normal preparation resolves each configured application once per operation and
execution context. Downstream policy, hashing, directory checks, history selection,
and per-file workflows receive that same value. They do not reload or reinterpret
raw application configuration. Avoid loader/validator/adapter chains that each call
the resolver. Configuration-only callers may discard the result after validation.

Safety revalidation is different from preparing another identity: offline checks
compare current accounts, process ownership, and directories against the captured
value and fail closed rather than silently replacing it. Trigger captures its
frozen identity in the parent, and the child independently prepares its own current
identity and compares policies. Parent/child checks and separate preflight
invocations are intentional boundaries, not duplicate resolution within one
ordinary preparation chain.

Preserve all-zero retention handling for listing and the existing treatment of
uninstalled parameterized templates. Identity checks that do not depend on runtime
parameters must still run when missing parameters defer other validation.

| Component | Required changes |
| --- | --- |
| Backup execution | Use shared preparation once, enforce privileges before execution state creation, and pass the same resolved application through combined and per-file workflows. |
| Full validation | Use shared preparation before dependent operations. Do not report configuration as valid before identity validation succeeds. Apply runtime directory checks to the resolved result without repeating resolution. |
| Trigger loader and preflight | Use shared preparation and build instances and frozen policy from its resolved result without rewriting `JobConfig` or importing backup policy construction. Retain full job validation through backup preflight, request checks, and child-policy comparison. |
| Schedule installation and update | Use shared preparation through `_load_managed()` before calendar validation, staging, or systemd changes. |
| Bulk schedule operations | Use shared preparation through `_managed_jobs()`, including identity validation for disabled jobs and independent checks on parameterized templates reported as skipped. Preserve saved selections and preflight before mutation. |
| Installer maintenance | Use the same preparation rules in configuration checking, planning, and resume validation. Preserve account execution through `runuser`, preflight before mutation, rollback, and transactional ordering. |
| Offline checks and process identification | Keep independent enforcement against explicit resolved identity. Share existing lower-level process identification with Trigger, without importing Trigger configuration or lifecycle code. |
| Fingerprints and execution policy | Consume the resolved value while preserving existing canonical payloads and UID inputs. |
| Replacement identity and history helpers | Consume resolved identity where current-history authorization needs it, preserving stored fields and comparison rules. |
| Inventory and verification | Separate archive inspection from preparation requiring current application identity; reuse existing loading and reporting mechanisms. |
| Prune and other destructive callers | Retain their existing scope, identity, destination, locking, and cleanup authorization. |
| CLI and internal callers | Propagate scope, adapt return-value handling, and reuse prepared identity across validation reports and adapters instead of reloading it. Preserve recovery/status paths that intentionally report partial information when identity is unavailable. |

Schedule and installer configuration preflight check account/context validity
without introducing backup-time source, mount, or directory availability
requirements. Full backup validation retains its runtime readiness checks.
Schema-only validation remains explicitly limited to YAML/schema checks and performs
no account lookup or runtime readiness validation.

Keep errors compact, with an absolute YAML path, the relevant property, and a concise
reason. Existing and nonexistent user-mode override values must both report
`application.user` as unsupported. Preserve structured installer diagnostics and
concise CLI reporting without tracebacks for expected configuration failures.

Remove the unused `SUPPORTED_APPS` declaration as the only incidental cleanup.

### Archive inspection

Reuse the existing configuration loading, parameter, and semantic checks separately
from preparation that requires current application identity. `list` and `verify`
must not run backup-execution validation or require the original application,
processes, source directories, or account merely to inspect existing archives.
Keep preparation's `None` contract unchanged; do not fabricate identities or add
an optional-identity processing framework for listing.

Preserve archive selection, normal CLI output, listing's all-zero retention
allowance, and source-independent per-file history discovery. Reuse existing
reporting when a nonessential current-identity calculation cannot be performed;
do not add annotation states, history-classification rules, or fallback identities.
This is a limited separation of existing responsibilities, not an inventory redesign.

Keep destination guards, credential checks, archive/member verification, locks,
and suspect marking where currently required. `prune` and its preview retain
their separate identity, coverage, retention, and replacement checks; execution
retains fail-closed deletion authorization and transactional protections.
Inspection availability must not authorize deletion or replacement. Preserve
supported pruning without source availability and add no application-runtime
prerequisites to it.

## Safety and compatibility

Continue constructing `PsutilProcessProvider` with the intended UID. Preserve its
filtering of other owners before reading process metadata, UID-aware liveness
checks, and uncertainty handling. See
[process inspection](../../src/vaultkeep/application.py).

Retain offline checks at the existing source-read and archive workflow boundaries.
They must revalidate canonical username against the resolved UID, authorize the
inspecting process, verify the strict canonical directory and its ownership, and
fail closed on unavailable or ambiguous observations. Account removal or remapping
after initial resolution must remain detectable.

Trigger retains its frozen UID, frozen matcher and directory, request consistency
checks, and comparison with the child's freshly resolved policy. Similar fields in
the request are intentional dispatch safety boundaries. See
[worker execution](../../src/vaultkeep/trigger/executor.py).

Replacement identity continues to use canonical username, application name, and
normalized data directory for offline jobs. UID remains excluded. Preserve identical
serialized identity values, existing-history selection, retention grouping, and
replacement cleanup authorization. The account-recreation/rename concern requires a
separate design decision and is deliberately unresolved here.

Retain the current canonical application payload and owner UID contribution to
configuration fingerprints. Do not change fingerprint format versions, archive or
manifest schemas, transaction ordering, durability barriers, or cleanup protections.
Preserve installer preflight, quiescence, refresh, resume, and rollback guarantees,
including restoration after failures or signals. Schedule installation/update and
bulk preflight must still validate before mutation and restore owned files and
service state on failure. Changes in code placement must not move these safety
boundaries or add new state transitions.

## Implementation sequence

1. Establish the one-way module dependencies in existing modules. Introduce the
   detached runtime identity and shared scope guard. Share the existing process
   identification capability below Backup and Trigger, retaining Trigger's instance
   adapter. Remove provenance state and incorrect lookup precedence.
2. Separate configuration-only loading from identity-required shared preparation
   with its explicit tuple contract. Route inspection through the former and
   identity-required callers through the latter without fake or unchecked results.
3. Adapt backup, validation, Trigger, fingerprints, policies, offline checks, and
   history helpers to consume one prepared identity. Keep archive inspection
   separate from identity-required operations without a new listing framework.
4. Integrate schedule and installer preparation, including disabled/deferred
   templates. Preserve preflight ordering, transactional changes, and rollback.
5. Adapt existing tests, add the regressions below, and update only affected
   documentation. Exercise both successful and failing paths for each contract.
6. Run focused and complete checks, then critically review dependencies, complexity,
   identity flow, inspection availability, and safety boundaries independently of
   test results.

## Tests and verification

Preserve and adapt the existing application, scheduling, installer/maintenance,
workflow, hashing, validation-health, Trigger, and replacement-safety tests.

Required regression coverage:

1. Raw application configuration remains unchanged after resolution and the runtime
   value has frozen fields. Mutate raw entrypoints, argument tokens, and environment
   dictionaries after resolution; captured identity, policy, and fingerprint inputs
   must remain unchanged. Mutating a serialized snapshot must also have no effect.
2. Equivalent raw inputs, raw serialization/reconstruction, repeated resolution,
   and runtime snapshot reconstruction remain consistent under unchanged execution
   conditions, without private provenance or shared mutable matcher structures.
3. User ownership derives from the correct execution UID despite misleading
   `HOME`, `USER`, `LOGNAME`, `SUDO_USER`, or configuration file location.
4. System execution defaults to root and permits an explicit existing account.
   Missing accounts and account-service failures produce actionable errors.
5. Consistent scope enforcement across backup, installer, Trigger, schedule, and
   full validation rejects non-root system scope before privileged operations,
   state, or service mutation. Include optional-application jobs and inspection
   paths with existing locks or suspect-marker writes. Preserve Trigger's additional
   non-root restriction and successful supported scopes.
6. Existing and nonexistent user-mode override values produce the same unsupported
   property error with neither target-name nor default-UID account lookup.
   Diagnostics include the absolute YAML path and `application.user`.
7. Schedule installation, update, and bulk validation reject invalid identities.
   Cover disabled jobs and templates with missing runtime parameters.
8. Installer, backup execution, full validation, and Trigger resolve equivalent
   ownership for the same valid execution context. Exercise preparation's three
   outcomes: absent optional application, valid identity, and error for missing
   required/invalid identity; never synthesize an identity or return `None` for an
   invalid one.
9. Similar applications owned by multiple users remain isolated, including root
   execution targeting another account.
10. Trigger preserves its validated frozen UID and rejects request or child-policy
    identity mismatches.
11. Offline checks detect account removal/remapping, directory ownership changes,
    symlink changes, and uncertain process observations at existing safety
    boundaries, including independent per-file checks.
12. Canonical policy payloads and fingerprint inputs remain equivalent, including
    implicit versus explicit root ownership and the fingerprint's UID contribution.
13. Replacement identity remains username-based with unchanged serialized values,
    existing-history compatibility, retention grouping, and cleanup protection.
14. Schema-only validation performs no identity or runtime checks, and recovery
    status retains useful local reporting when account resolution fails.
15. Count resolver invocations across normal loader/preparation/adapter chains,
    directory validation, combined and per-file execution: one resolution per
    prepared application. Separately assert that existing offline rechecks and
    Trigger parent/child policy verification still occur at required boundaries.
16. `list` and `verify` succeed with existing backups after source/application
    directory removal, absent application executables/processes, missing accounts,
    and account-service failures. Cover both backup modes and preserved listing
    output in ordinary supported cases. Inspection must not fabricate identity or
    authorize destructive operations when current identity is unavailable.
17. Inspection still rejects malformed manifests, corrupted archives, wrong
    credentials, and unsafe members as appropriate; destination guards and suspect
    marking remain effective. Permissive inventory access never enables pruning
    when identity or replacement authorization is unavailable. Prune's dry run and
    execution retain their existing safety checks and source-independent support.
18. Installer configuration failures occur before transactional mutation; failures
    and signals during refresh/resume preserve rollback and restoration. Schedule
    install/update and bulk operations preserve preflight, files, registry, service
    state, and rollback behavior when identity or later operations fail.
19. Import preparation and identity modules in different entry-point orders to
    catch circular imports. Review imports, including deferred imports, to verify
    the specified dependency direction and absence of low-level entry-point imports.
    Exercise the same shared detector through offline checks and Trigger, including
    unreadable metadata, ambiguous identity, account-relative paths, and PID reuse.

Use disposable paths and mocked accounts/processes/systemd for failure cases. Run
installed CLI/worker and installer coverage where its existing environment is
available. Follow the [development testing guide](../development/testing.md).

After focused suites, run:

```sh
ruff format --check .
ruff check .
mypy
pytest --no-cov -q
python scripts/check_packages.py
git diff --check
```

Run the existing installer verification and all repair variants in the prepared
environment. Record skips and limitations. Investigate any recurring installer
timeout, including the affected repair variant; a successful rerun does not make
an intermittent failure an unconditional pass.

The pre-implementation review baseline completed with **442 passed, 16 skipped**
across focused application, scheduling, Trigger, backup safety, hashing, validation,
installation, and maintenance suites. It included the untracked application tests.
This baseline does not establish full-suite, installer-repair, or package-check
results for the correction.

## Documentation and final review

Update user documentation only for affected supported behavior and configuration
requirements. Describe configuration directly without migration narratives or
historical implementation explanations. Keep README operational, avoid duplicating
reference material, and verify affected relative links and anchors.

The final review must establish that:

- Raw configuration and runtime identity are separate, without hidden provenance
  or runtime values inserted into configuration. Nested raw mutations cannot alter
  captured identity or policy.
- Execution and full-validation entry points use consistent scope and identity
  rules and predictable errors, with schema-only and partial recovery operations
  kept explicit. Ordinary preparation resolves identity once.
- Imports follow the one-way dependency structure. Preparation orchestrates;
  identity resolution owns account/path logic; scope checks remain below both.
  Shared process identification imports no Trigger configuration or lifecycle code.
- Inventory and integrity verification remain available without application runtime
  identity, without a new optional-identity or listing framework. Existing reporting
  and separate destructive authorization remain intact.
- The private UID and parallel resolution return are removed without losing UID
  inputs or intentional frozen-policy and offline revalidation boundaries.
- Shared preparation reduces repeated orchestration without introducing a context
  hierarchy, duplicate job model, compatibility layer, or account cache.
- Process isolation, frozen Trigger policy, offline revalidation, fingerprint
  semantics, backup durability, installer/schedule transactions and rollback, and
  history compatibility are preserved without new hidden state transitions.
- The username-based replacement-history concern is reported as a separate design
  issue rather than silently changed.

Passing tests alone is insufficient: review the data flow, failure ordering,
interface scope, and necessity of each added type and helper.
