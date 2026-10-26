# Backup safety, retention and reporting redesign

## Objective and scope

Implement one shared backup execution workflow for manual, Schedule and Trigger
execution, optional application-offline validation, one destination-authoritative
exceptional state (`SUSPECT`), explicit suspect replacement, and guarded automatic
cleanup after reliable archive creation. Simplify inventory and operational reports.

This document specifies the implemented redesign. Operational procedures are documented
in the README and topic references.

Use configuration schema version 2 and manifest/layout version 2. Deployments use
fresh configuration, destinations and local state. Do not implement migration,
version-1 manifest interpretation, legacy configuration or marker support, historical
Trigger review migration, old execution-state adapters, or a compatibility framework.
Unsupported existing repositories must never be silently modified or deleted.

Reuse the tested archive builder, source hashing, atomic finalization, locking,
guarded deletion and Trigger worker-recovery infrastructure. Do not add dependencies,
background services, a replacement database, maintenance commands or unrelated
refactoring. Do not stage, commit or push changes.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| Backup engine | Discover sources; enforce consistency policy; create, verify and classify archives; decide `UNCHANGED`; manage suspect markers and replacement; plan and execute retention and cleanup. |
| Trigger | Observe the application; confirm shutdown; perform its final pre-dispatch check; request execution; prevent duplicates; track and recover workers and dispatch. |
| Schedule | Initiate the same backup workflow at configured times. |
| Destination | Authoritative manifests, archive artifacts, suspect classification and replacement relationships. |
| Local operational state | Progress, execution outcomes, cleanup diagnostics and operational recovery; never an override for destination health. |

Do not associate Trigger attempt IDs with backup IDs. Retain request and attempt IDs
required for idempotency, systemd worker management and crash recovery. Trigger must
not classify archives, manage suspect markers or perform separate pruning.

## Configuration and application detection

### Shared application identity

Define application identity once in an optional top-level `application` section:

```yaml
config_version: 2

consistency:
  mode: application_offline

application:
  name: bisq
  user: bisquser
  data_dir: /home/bisquser/.local/share/Bisq
  matcher:
    entrypoints:
      - kind: java_main
        value: bisq.desktop.app.BisqAppMain

trigger:
  enabled: true
  poll_seconds: 2
  confirm_seconds: 5
```

This is a configuration fragment, not a complete job. Preserve existing strong
entrypoint, executable, command-line, allowed environment and data-directory
matching semantics. Remove application identity fields from `trigger`; its remaining
settings govern enablement, dispatch selection and monitoring timing. Preserve
separate backup and Trigger notification switches and existing Schedule/Trigger
enablement constraints.

`consistency.mode` accepts `files` and `application_offline`. Omitting the section
selects `files`. Use strict models and reject unknown keys, incomplete identities,
unsupported versions and missing required application configuration.

| Policy | Trigger enabled | Application required | Engine inspects application processes |
| --- | --- | --- | --- |
| `files` | No | No | No |
| `files` | Yes | Yes | No |
| `application_offline` | No | Yes | Yes |
| `application_offline` | Yes | Yes | Yes |

With Trigger and `files`, Trigger requests execution after confirmed shutdown, but
the engine permits the application to run during archival if file checks succeed.
Offline enforcement must not depend on Trigger enablement or monitoring history.

Validate the configured owner and data directory. Resolve the account to its UID
and inspect that owner's processes, including when root backs up another user's
application. Do not restrict detection to the executing UID. Reuse one shared
identity/detection implementation for Trigger and the engine. Preserve PID-reuse
protections and revalidation around metadata and environment reads.

Fail closed when the intended process scope cannot be established, metadata is
inaccessible, or detection is ambiguous or unavailable. Inaccessible process
information is not evidence of absence. Keep metadata-only inventory independent
of source availability and live application inspection.

### Execution snapshot and configuration fingerprint

Each execution uses one authoritative, immutable effective configuration, including
runtime parameter resolution, consistency policy and resolved application identity.
Trigger's frozen dispatch identity and the policy loaded by the child must agree.
Use an immutable execution snapshot or compare the frozen policy/identity with the
child's loaded snapshot before it proceeds; checking the file and later reloading it
without comparison is insufficient. Unexpected setup changes fail safely. Manual
and scheduled runs likewise retain one consistent policy throughout execution.

Include the effective policy and complete application identity, including process
owner and matcher, in the normal configuration fingerprint. Changing from `files`
to `application_offline` must force a new archive. Preserve existing content,
credential and destination checks for `UNCHANGED`. The ordinary configuration
fingerprint and replacement identity serve different purposes.

## Histories and immutable destination metadata

### Format and discovery

Version 2 manifests retain the archive's immutable identity, creation timestamps,
source digest, normal configuration fingerprint, archive name, SHA-256, format,
encryption and producer metadata. Add an immutable replacement identity describing
source scope and required consistency policy. Never modify a committed manifest.
Do not accept version-1 manifests as new-format backups.
Require explicit `manifest_version: 2` and replacement `identity_version: 1` fields;
missing versions are malformed metadata rather than implicit current versions.

Preserve strict discovery, exact permitted artifact sets, manifest/directory identity
checks, filesystem validation and symlink rejection. Recognized matching unsupported
or malformed entries block destructive operations in their affected history. Never
skip a matching invalid entry and proceed with deletion there. Unrelated entries
outside the recognized namespace remain untouched and do not block unrelated
histories. If an invalid entry's narrower history cannot be established safely,
block the affected containing namespace rather than guessing its class.

Discover supported new-format archives using their stored format, so switching
between `tar.zst` and `tar.7z` does not hide compatible replacement candidates.
Support both current archive formats; removing legacy interpretation does not mean
removing either supported compression format.

### Replacement compatibility

Derive a deterministic, versioned source-scope identity from the effective:

- Job identity and backup mode.
- Independent per-file history identity, where applicable.
- Configured source roots and their archive paths/layout.
- Source selection, exclusions and missing-source policies.
- Symlink and filesystem traversal policies.
- Other settings that determine source coverage.

This identity describes configured coverage, not the files' current content digest.
Store enough immutable identity metadata to compare candidates without the original
YAML. The manifest's replacement identity also records the consistency mode and,
for offline backups, logical application name, owner and data directory.

Automatic replacement requires the same source scope and consistency mode. Offline
replacement additionally requires the same logical application identity; matcher
implementation details need not match. Do not require equal source contents,
retention, compression, archive format, password-file paths, Trigger polling,
notifications or logging. Archive-format and encryption changes are allowed only
when the replacement is independently verified and recoverable.

Treat a different replacement class as a separate history for replacement and
cleanup. Preserve incompatible historical backups and suspects. They must neither
authorize deletion in the new class nor force endless archive creation there.
Managing an incompatible class with its original compatible configuration or manual
removal is acceptable; no new maintenance command is needed.

## Shared locking

`run`, mutating `verify`, and actual `prune` use one mutually exclusive locking
protocol for every overlapping physical backup history. A coarse lock for the whole
destination/job namespace is sufficient, including all its per-file histories.

Establish a stable, validated shared lock anchor independent of configuration path
and local reporting identity. Different YAML paths, destination aliases, or root
versus user execution must not provide independent locks for overlapping histories.
Per-file parent and child namespace overlap must either resolve to the same shared
lock or be rejected. Reject configurations/filesystems for which reliable shared
mutual exclusion cannot be established. Preserve filesystem and lock-file safety.

Local destinations use shared physical ancestor directory locks and an exclusive
destination directory lock. CIFS/SMB and NFS destinations use one persistent regular
`.vaultkeep.lock` at the mounted whole-share/export root, with an exclusive
server-backed lock shared across machines and all its histories. Reject mounts
that disable server locking, unsafe anchors, subtree/nested layouts, and lock
failures. Never remove or replace the persistent lock file during operation.

Keep native no-overwrite atomic directory commits for local/CIFS destinations.
When NFS rejects the no-overwrite rename flag, allow an ordinary atomic directory
rename only under the covering shared destination lock after checking the target
is absent. Existing nonempty backup directories remain protected; an unrelated
process racing to create an empty target directory can have it replaced. Validation
probes use the same lock and fallback. Other rename failures remain errors.

Acquire the lock before discovering or validating destination records that may later
be modified or deleted. Hold it through marker updates, post-commit verification,
replacement validation and cleanup. Automatic cleanup uses the lock already held
by execution and must not reacquire a non-reentrant lock.

Preserve local execution/progress ownership and Trigger dispatch recovery while
extending destination mutual exclusion. `list`, `status` and `prune --dry-run` may
remain read-only without exclusive locking; their observations never authorize a
later deletion without refreshed, locked validation.

## Source consistency and exact execution lifecycle

Preserve source discovery and metadata validation, initial SHA-256 hashing, final
source rehash/comparison, archive structural and stream verification, archive
checksums, atomic commitment with the filesystem-specific overwrite guarantees
defined above, flushing and incomplete-artifact cleanup.
Do not replace these checks with process monitoring.

Use this lifecycle for each independently created archive:

1. Load and validate the execution configuration and establish the required locks
   before mutable destination discovery.
2. Run the existing pre-check phase (`before_check`).
3. Perform preliminary application-offline validation when required, before examining
   sources.
4. Discover and hash sources for change detection; discover the relevant destination
   history and evaluate eligible recovery points and replacement requirements.
5. If eligible, complete the existing `UNCHANGED` lifecycle, including its hook and
   state recording, and return without automatic pruning.
6. Run `before_archive`.
7. Repeat application-offline validation when configured, then refresh the source
   snapshot and digest before archiving. Use this refreshed snapshot/digest for the
   archive and committed metadata.
8. Create and verify the staging archive, including the final source rehash.
9. Perform the final application-offline check.
10. Run `after_archive` to release quiescing or restart the application.
11. Recheck staged archive integrity if necessary, without reading sources again.
12. Atomically commit and durably finalize the archive.
13. Rediscover and verify the committed archive.
14. Persist required successful backup state.
15. Run `on_success`.
16. Record compatible suspect replacement relationships.
17. Rediscover the destination and build and validate the complete cleanup plan for
    the independent history.
18. Execute guarded suspect cleanup and ordinary retention pruning.
19. Persist/report final execution and cleanup results.

The final offline check occurs after all source reads and before `after_archive`.
The release hook may restart the application: no source reads occur afterward. It
must not modify staging or other backup artifacts. Any required post-hook integrity
check reads backup artifacts only. Do not move `on_success` before commitment.

Arm and attempt `after_archive` exactly once as appropriate when quiescing or source
processing was attempted, including failure paths. Do not leave the application
quiesced because archive creation, source verification or the final offline check
failed. Preserve the primary failure and report any additional release-hook error.
Apply preliminary and repeated checks as applicable, and the final check independently
to every archive in `per_file` mode.

`files` performs no application-process checks. `application_offline` requires
reliable stopped observations at the specified boundaries; running, ambiguous and
unavailable observations fail. A late offline failure is a failed backup execution,
not Trigger's `BLOCKED` receipt, which means execution was prevented before starting.

Before commitment, failures return `FAILED`, remove incomplete owned artifacts and
preserve all existing backups. Never commit a known-invalid archive merely to mark
it suspect. After commitment, preserve the archive and mark it suspect only when
integrity or consistency evidence justifies that classification.

### Guarantees and limits

Source hashing detects many concurrent modifications, but not every possible
change. Absence at two observation points neither proves a clean shutdown nor
excludes a short restart between them. The protected source-processing window ends
before `after_archive`; an application restart there is permitted. File consistency
is not application-level transactional consistency. Do not claim absolute
consistency or that a marker's absence proves full current integrity.

## SUSPECT markers and verification

Use one exceptional committed-backup classification: `SUSPECT`. A normal backup has
no suspect marker. Store the marker inside the backup directory without renaming it:

```text
<backup-directory>/.vaultkeep-suspect
```

```json
{
  "reason": "Archive SHA-256 checksum mismatch",
  "detected_at": "2026-10-26T18:00:00Z",
  "replacement_backup_id": "f314c0e7a3294bd69c00b01fc47a25e8"
}
```

The replacement field is optional. Preserve the original reason and detection time
when updating a reference. Create/update markers atomically and durably, reject
malformed, unreadable, oversized or unsafe entries, and never follow unexpected
symlinks. Explicitly allow the marker in strict backup artifact validation. Remove
the old damage implementation rather than maintaining parallel health systems.

Mark demonstrated corruption or established post-commit consistency problems found
by explicit `verify`, newest-archive checks before `UNCHANGED`, post-commit checks,
and other supported archive-integrity validation. Wrong credentials, missing tools,
temporary I/O failures, inaccessible destinations and incomplete inspection produce
operational/verification errors rather than automatic corruption classification.

`verify` retains full archive verification, may write suspect markers under the
shared lock, and never modifies archive contents. `list`, `status` and dry-run must
not mark or repair anything. If recording a newly established safety problem fails,
report that failure and prohibit destructive cleanup in the affected history. Older
archives may become suspect; multiple suspect archives are valid.

## Replacement references and UNCHANGED

A suspect is internally unresolved when it has no currently valid reliable
replacement, or superseded when its reference resolves to one. Both remain publicly
`SUSPECT`. Local state and Trigger history cannot establish these relationships.

A valid replacement has a specific, unambiguous backup ID, exists in the same
compatible independently managed history, is fully committed and verified, meets
the required consistency identity, and is not suspect. A bare reference is not
proof. Reject missing, forged, incompatible, cross-history, self-referential,
circular or conflicting relationships before authorizing deletion.
Do not impose timestamp ordering on replacement relationships: clock corrections
must not invalidate an independently verified compatible replacement.

After reliable creation and successful required state persistence and hooks:

1. Identify compatible unresolved suspects.
2. Validate the new replacement.
3. Atomically write the new ID into their markers while preserving diagnostics.
4. Confirm the required writes succeeded.
5. Rediscover destination metadata; do not reuse the pre-update snapshot.
6. Validate the relationships and complete deletion plan before cleanup.

If A references B and B disappears, becomes incompatible or becomes suspect, A is
unresolved again. The invalid reference authorizes no deletion. A later reliable C
may replace it, preserving A's original reason and timestamp. Partial marker updates
do not permit partial destructive cleanup when the complete affected plan cannot
be validated.

A valid replacement reference survives a failed deletion. A remains suspect but
superseded while B remains valid. B may satisfy future `UNCHANGED` checks, and A may
be removed by explicit prune or a later reliable archive-creating execution.

Never use a suspect as the reusable successful backup or local-state reconstruction
candidate. An unresolved suspect in the active compatible history forces a new full
archive even when contents are unchanged. A superseded suspect must not force
another archive merely because its deletion failed. `UNCHANGED` is neither a
replacement nor an automatic cleanup trigger. Validate destination relationships
when evaluating the existing `replacement_required` gate.

## Retention and guarded cleanup

Use the existing ordinary retention policy and shared workflow for all initiators.
Suspects do not fill ordinary eligible recovery-point retention slots. Superseded
suspects are cleanup candidates regardless of ordinary retention counts; unresolved
suspects remain protected.

| Independent history result | Automatic cleanup |
| --- | --- |
| Reliable new archive created | Yes |
| `UNCHANGED` | No |
| `FAILED` | No |
| Committed archive classified `SUSPECT` | No |

Build and validate the complete deletion plan per independent history before deleting
anything in that history. Validate malformed-entry barriers, duplicate identities,
exact artifacts, reference dependencies and the continued availability of an
eligible recovery point. Never delete the only remaining recovery point or let an
unrelated history's successful backup authorize deletion.

If A still depends on B, retention must preserve B until A is successfully deleted
or another eligible replacement has been established and validated. A plan that
would delete B cannot proceed on the assumption that deleting A will succeed.
Execute dependent operations in safe order and preserve B if A's removal fails.
Account for these dependencies before any deletion and revalidate candidates
immediately before removing them.

Use a dedicated guarded suspect-cleanup path where needed; do not relax the normal
pruner into accepting arbitrary marked directories. Delete only the exact known
regular artifacts and validated marker, then the directory, preserving existing
filesystem checks and durability handling. Never use unrestricted recursive
deletion or delete unrecognized artifacts.

### Metadata versus destructive validation

Inventory and retention previews validate manifest structure, identity, source-scope
compatibility, reference structure/existence, marker status and retention
eligibility without scanning archive contents. These observations do not authorize
deletion. Malformed or uncertain relationships cannot be reported as safely prunable.

Before deleting a superseded suspect, hold the shared exclusive lock, refresh
discovery, validate the exact marker/artifacts and same-history relationship, and
confirm the replacement is not suspect. Verify its current archive SHA-256 against
both immutable manifest digest and checksum sidecar. Revalidate the candidate
immediately before removal. Any mismatch or unavailable required verification
prevents deletion; record demonstrated corruption through the suspect mechanism.

Do not add a full decompression/stream-verification pass to every prune. The archive
received structural and stream verification at creation; explicit `verify` remains
the full verification operation.

### Explicit prune and per-file independence

`vaultkeep prune` uses the same guarded deletion and dependency rules. It may remove
validly superseded suspects and ordinary retention candidates. Unresolved suspects
remain protected. `prune --dry-run` performs no writes, marker updates, repairs,
exclusive-lock creation or deletion, and its observations are not reusable deletion
authorization.

Each file in a `per_file` job has an independent history. A successful file A may
complete cleanup even if B fails and the aggregate job is `FAILED`. B's failure does
not block safe independent A cleanup. Validate the complete plan for each history;
do not retain the old blanket requirement that every per-file history validate
before any independent history can be cleaned. Preserve deleted/excluded source
histories conservatively, and fail closed when boundaries cannot be established.

## Failure outcomes and operational reporting

| Failure | Execution result | Delete old backups? |
| --- | --- | --- |
| Archive creation or source validation before commit | `FAILED` | No |
| Final application-offline check | `FAILED` | No |
| Post-commit archive integrity | `FAILED`; mark committed archive suspect when justified | No |
| Required local state persistence | `FAILED`; preserve committed archive | No |
| `on_success` | `FAILED`; preserve committed archive | No |
| Replacement-marker update after reliable creation and successful hooks | `SUCCESS` with cleanup warning | No |
| Retention planning or pruning after reliable creation and successful hooks | `SUCCESS` with cleanup warning | No further destructive work after the failure |

Pruning may fail after earlier guarded deletions completed; report the partial
cleanup outcome accurately, preserve remaining dependencies and stop unsafe further
work. Do not claim that already completed filesystem deletions can be rolled back.

Post-commit operational failures must not delete the committed archive. State or
hook failures alone do not mark it suspect. Destination metadata remains the source
for subsequent reconciliation if local state persistence fails. Replacement-marker
write failures are cleanup errors, not corruption of the new archive. Required
success state must be saved before destructive work; final reporting persistence
does not retroactively invalidate safe completed cleanup.

For cleanup failures, return the existing successful `created` result and exit code,
emit an actionable human warning, and record the diagnostic in existing operational
execution/history state where possible. Notifications can report successful
creation together with cleanup failure. Do not add `run --json`. Expose cleanup
information through the revised documented `status --json` schema and preserve
Trigger's successful worker-result interpretation.

Distinguish destination-derived **Cleanup pending** from operationally recorded
**Previous cleanup error**. A surviving superseded suspect indicates retained work,
not proof that an earlier cleanup failed. Never invent an error when its operational
record is unavailable. Retry cleanup only on a later reliable newly created archive
or explicit prune, never on `UNCHANGED`.

## Trigger and Schedule integration

Keep shutdown confirmation, final pre-dispatch identity validation, dispatch
evidence, duplicate prevention, worker tracking and safe interrupted-execution
recovery. Never automatically replay an attempt whose outcome is uncertain. Keep
genuine unknown-worker outcomes, unresolved dispatch, blocked execution and recovery
diagnostics. Preserve advisory deadline behavior and necessary diagnostic evidence.

Remove obsolete archive-consistency `review_required` logic and historical
backup-health warnings from Trigger, validation, status and notifications. A restart
or monitoring gap does not independently classify an archive; the engine enforces
the selected boundary checks. Never attach historical warnings to archives by
timestamp proximity. Keep backup and Trigger notifications separately configurable
without duplicated backup-health alerts. Schedule continues to invoke the same
engine without its own retention logic.

## Inventory and status presentation

### List

Retain one read-only, metadata-only `list`, without a verbose mode or a new details
command. Display job, destination, configured retention, creation time in UTC,
archive size, retention decision, tiers and backup directory. Put a suspect's original
reason directly below its directory name. Do not require original source files.

| Decision | Meaning |
| --- | --- |
| `KEEP` | Selected by ordinary retention. |
| `PRUNE` | Metadata-based eligibility preview; deletion-time validation is still required. |
| `PROTECTED` | Unresolved suspect. |
| `UNKNOWN` | Retention or required relationship validity could not be determined. |
| `—` | Retention disabled or inapplicable. |

A suspect with a metadata-valid replacement may display `PRUNE` and still show its
`SUSPECT` reason. An unresolved suspect remains `PROTECTED`; malformed or uncertain
entries are not safely prunable. Show counts for backups, keep, prune, suspect and
unknown as applicable. Keep explicit errors for incomplete retention calculations.
Do not add a health column or label unmarked archives `OK`/`VERIFIED`. Remove the
redundant eligibility and no-deletion footers.

### Status

Keep normal human output focused on job, current backup activity, previous backup
result/time and relevant Trigger monitor/application/dispatch state. Show additional
configuration details only when invalid or actionable, and retain relevant Schedule
operational information.

Show current backup failures, unresolved suspects, cleanup pending, recorded previous
cleanup errors, monitoring/dispatch problems and uncertain worker outcomes. Do not
show superseded suspects as unresolved safety concerns or historical attempt UUIDs
and resolved monitoring events as current archive-health problems.

Destination inspection is read-only, metadata-only and best-effort. An unavailable
destination must not prevent local operational reporting. Preserve useful
machine-readable fields; deliberately revise and document the JSON schema and update
internal consumers/tests together. Do not mutate markers or execution records from
status collection.

## Implementation stages and repository touchpoints

Inspect the surrounding source and tests before changing each area. Keep stages
focused and reviewable; extend working infrastructure instead of replacing it.

| Stage | Work | Primary existing areas |
| --- | --- | --- |
| 1. Destination format and identity | Define version-2 manifest/layout, canonical replacement identity and strict suspect marker; remove the old damage representation. | `destination/manifest.py`, `destination/creation.py`, `destination/damage.py`, `destination/discovery.py`, source fingerprint helpers and state models. |
| 2. Locking, health and cleanup | Establish shared physical-namespace locks; integrate marking and rediscovery; validate replacement lifecycle/dependencies; implement guarded cleanup and per-history planning. | `locking.py`, `backup/execution.py`, `backup/workflow.py`, `destination/discovery.py`, `destination/pruning.py`, `retention/planner.py`, `state/unchanged.py`, `file_backups.py`. |
| 3. Policy and execution lifecycle | Add schema-2 shared application/consistency models, owner-aware detection, execution snapshot agreement, fingerprint changes, offline checks and exact hook lifecycle. | `config/models.py`, `config/loader.py`, `config/runtime.py`, `validation/semantic.py`, `sources/hashing.py`, `trigger/config.py`, `trigger/detection.py`, `trigger/process.py`, `backup/workflow.py`, `hooks.py`. |
| 4. Trigger simplification | Remove archive-health review behavior while retaining durable dispatch, worker idempotency, final rechecks and no-replay recovery. | `trigger/models.py`, `trigger/state.py`, `trigger/monitor.py`, `trigger/executor.py`, `trigger/service.py`, validation/status adapters. |
| 5. Reporting and documentation | Add cleanup outcome fields to existing operational records, human warnings, status JSON, notification results, compact inventory/status and accurate documentation/examples. | `backup/results.py`, `backup/records.py`, `backup/status.py`, `backup/execution.py`, backup notifications, `cli/commands.py`, `cli/inventory.py`, `cli/status.py`, `trigger/status.py`. |
| 6. Full validation | Run complete regression and package checks; inspect destructive operations, partial failure and crash boundaries; verify links and example schemas. | Unit, integration, Trigger and installed-contract suites; package validation and documentation. |

Paths in the table are relative to `src/vaultkeep/`. Keep shared matching independent
of Trigger enablement; extract existing identity/detection code only as needed.
Update [README](../../README.md), [configuration](../configuration.md),
[CLI](../cli.md), [status](../status.md), [notifications](../notifications.md),
[troubleshooting](../troubleshooting.md), affected retention/verification architecture
sections, Trigger design and operational topics, development testing guidance and
all example YAMLs. Update or remove obsolete plan material where affected; do not
rewrite unrelated documentation. Keep user procedures separate from this plan.

## Test and validation requirements

Use disposable sources, destinations, state and runtime directories, fake application
processes, and mocked systemd where appropriate. Do not run real wallet jobs, alter
live services or operate on existing user backup repositories. Replace obsolete
behavioral expectations with tests of the redesigned contracts rather than retaining
legacy compatibility tests.

| Area | Required behavior and regressions |
| --- | --- |
| Configuration | Default `files`; both policies with Trigger enabled/disabled; shared identity required where appropriate; incomplete/unknown keys rejected; configured account/data directory validated; schema version rejected when unsupported. |
| Snapshot and identity | Fingerprint changes for policy/application/owner; no reuse under a weaker prior policy; configuration modification between dispatch and child setup; stable policy throughout manual, Schedule and Trigger runs. |
| Process inspection | Root checking a different owner; inaccessible/ambiguous metadata and unknown account fail closed; executable/argv/environment/directory matching; PID reuse; no engine process checks for `files`. |
| Source/archive safety | Unchanged source with eligible backup; refreshed pre-archive snapshot; source changes during creation; stream/structure/checksum failure; owned staging cleanup; no committed archive for pre-commit failure. |
| Offline/hooks | Preliminary and repeated checks; running at final check; application starts and stays running; final check per file; exact hook ordering; restart in `after_archive` with no later source reads; release exactly once on failure; original failure preserved alongside release error. |
| Locking | Concurrent `run`, mutating `verify` and actual `prune`; corruption discovered while pruning is attempted; distinct config paths and aliases targeting the same namespace; shared root/user coverage; unsupported overlap/locking rejected; automatic cleanup does not reacquire its lock. |
| Manifests/markers | Version-2 metadata and compatibility identity; version-1/unsupported matching entries block deletion; valid, invalid, unreadable and unsafe markers; atomic durable writes/updates; original diagnostic/time preserved; relocation/restart does not lose marker authority; multiple suspects. |
| Classification | Corruption through explicit verify, latest-backup checks and post-commit checks; wrong credentials, missing tools and temporary I/O not treated as corruption; marker-write failure prohibits destructive cleanup; immutable archives/manifests preserved. |
| Compatibility | Changed contents within the same scope replace successfully; roots/exclusions/coverage changes create independent classes; compression/retention/format/password-path changes do not define scope; independently verified encryption changes; policy/application changes separate classes; matcher details do not unnecessarily prevent offline replacement. |
| References | Valid same-history replacement; missing, forged, cross-history, incompatible, self/circular/conflicting IDs; replacement later corrupt/disappeared; reassignment to a fresh replacement; fresh discovery after updates; interrupted and partial marker writes. |
| Retention/dependencies | Reliable new archive prunes; unresolved suspect forces a new archive; superseded suspect does not; retention cannot remove a still-needed replacement; candidate checks and complete planning before deletion; suspect deletion fails followed by `UNCHANGED`; explicit cleanup retry; only recovery point protected. |
| Independent histories | Successful A cleanup despite failed B/aggregate failure; no cross-file authorization; incompatible classes preserved without endless backups; deleted/excluded source histories; malformed-entry barrier at the safely established affected history. |
| Failure outcomes | Source and state failures before/after commitment; post-commit corruption; failed required state write or `on_success`; successful archive plus marker/planning/pruning warning; partial deletions reported accurately; no cleanup for failed/suspect/unchanged histories. |
| Reporting | Successful `created` and exit code despite cleanup warning; separate pending work and recorded error; status JSON and notifications; no added run JSON; exact Trigger result-marker interpretation. |
| Inventory/read-only | Compact list and inline reason; every decision including unknown/disabled; metadata-only list/status versus deletion-time SHA-256; dry-run makes no writes; unavailable destination still permits local status; no obsolete footers/health claims/historical review warnings. |
| Trigger/Schedule | Shared engine behavior through all initiators; frozen identity agreement; worker crash/restart, duplicate prevention, dispatch uncertainty, final pre-dispatch refusal and no automatic replay; no Trigger-to-backup-ID relationship; distinct notification controls. |

Run relevant tests at each stage and full unit, integration, Trigger, installed CLI
and package checks before completion, following the
[development testing guide](../development/testing.md). Run formatting, linting,
type checks and `git diff --check`. Validate new-format fixtures, both supported
archive formats, all example YAMLs, local documentation links and changed anchors.
Do not claim live systemd, installed application identity, unsupported filesystem
locking or release-host gates passed when only mocked/local tests ran.

## Completion criteria

The implementation is complete when all initiators use the same policy-driven
engine; overlapping mutations serialize reliably; versioned destination metadata
authoritatively controls health and compatible replacement; source/hook boundaries
match the specified lifecycle; and deletion is guarded by complete per-history
plans, valid dependencies and current replacement checksums.

`UNCHANGED` never reuses a suspect or bypasses an unresolved compatible replacement
requirement. Automatic cleanup follows only reliable new creation and successful
required state/hooks. Cleanup errors preserve the successful creation result while
remaining actionable. Trigger retains correct dispatch recovery without archive
health ownership. Inventory/status remain concise, safe and read-only. Relevant
tests pass, obsolete implementations are removed, and documentation matches the
implemented behavior.

Finish implementation with a concise summary of changed files, observed behavior,
exact checks/tests and their results, failing or skipped checks, and remaining
limitations. Leave all changes uncommitted for manual review.
