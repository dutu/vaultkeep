# Resumable retention cleanup and recovery-point eligibility

## Objective and scope

Make interrupted retention deletion resumable through a per-backup
`.vaultkeep-prune-pending` marker. Exclude incomplete, invalid, suspect, and
pending-deletion backups from retention tiers and recovery-point replacements.
Keep a completed backup successful when filesystem cleanup is deferred.

Implementation is a separate task. No archive-format,
staging, retention-tier algorithm, dependency, scheduler, or database changes are
required. Do not add retry loops, directory renaming, recursive deletion, minimum
ages, or `retention.last`.

## Findings from the current implementation

- `destination/discovery.py` requires a valid manifest and exact regular artifacts.
  Partial deletion therefore becomes malformed discovery on the next execution.
  Staging directories are already excluded. Suspect backups remain separately
  identifiable, and their state records are excluded from healthy reconstruction.
- `destination/pruning.py` groups healthy backups by `ReplacementIdentity`,
  protects suspect replacement dependencies, and verifies checksums of surviving
  recovery points before deleting anything. `_remove_valid_backup()` unlinks
  artifacts and removes the directory without persisting deletion intent.
- `build_prune_plan()` and execution reject matching malformed entries. Preserve
  this conservative destructive-operation barrier rather than silently relaxing it.
- Inventory and manual/automatic pruning use `metadata_only=True` discovery.
  This validates layout and manifests but skips checksum-sidecar validation;
  retention previews can therefore include invalid checksum metadata. Execution
  validates checksum metadata later, which prevents deletion but does not correct
  the earlier eligibility calculation.
- `backup/workflow.py` applies retention after committing and checking the new
  backup. `CommandResult.cleanup_errors`, execution records, status, and backup
  notifications already separate cleanup errors from backup results.
- `cli/status.py` already has `destination.cleanup_pending`, currently used for
  suspects with compatible replacement references. Extend it compatibly.
- `state/atomic.py` provides file replacement with file and directory flushing;
  `destination/suspect.py` provides bounded, strict marker reads without following
  links. Reuse suitable patterns without changing unrelated local-state behavior.

## Safety model

Classify matching destination entries into distinct collections:

| Classification | Retention/replacement eligibility | Cleanup authorization |
| --- | --- | --- |
| ELIGIBLE: committed, not suspect | Eligible after required metadata checks; not a claim of archive verification | Complete validated retention plan |
| Suspect | Never eligible | Existing compatible, verified replacement rules |
| Valid PRUNE_PENDING intent | Never eligible, even with all artifacts present | Original intent plus fresh safety checks |
| EMPTY_ORPHAN | Never eligible | Matching direct child, confirmed empty, destination lock held |
| INVALID: unknown malformed/incomplete | Never eligible | None; retain diagnostics and destructive-operation barrier |

Unknown malformed entries continue to block destructive cleanup in their affected
destination/history scope, including pending cleanup. Independently locked per-file
destinations can continue as they do today. Do not infer a malformed entry's history
from its name, or use it as evidence that recovery points exist.

The marker is an allowlist of intended artifacts, not standalone authorization.
Every cleanup must revalidate destination containment, directory type and naming,
job and history identity, remaining entries, and healthy surviving dependencies.
On supported platforms, open the validated destination parent and backup directory
using directory-only/no-follow flags. Inspect entries, open marker/artifact files,
unlink artifacts, and flush relative to those descriptors. Validate the actual
opened objects with `fstat`, rather than repeatedly resolving their paths. Before
removing the directory through its validated parent's descriptor, check that the
parent entry still identifies the opened directory. Refuse changed identities.
Do not fall back to check-then-path-unlink operations where descriptor-relative
safety cannot be provided; defer cleanup. Do not rely on stable CIFS inode numbers
across runs; descriptor identity checks apply within the current execution.

## Implementation sequence

### 1. Define and validate deletion intent

Add a small marker module within `destination/`, used by existing discovery and
pruning rather than a separate cleanup subsystem. Define a bounded, versioned,
strict JSON record containing:

- Backup ID, job ID, exact directory name and backup base name.
- Full existing `ReplacementIdentity` to preserve history isolation.
- Exact archive, checksum, and manifest basenames, stored archive format, and
  sufficient immutable manifest facts to compare any remaining manifest.
- The complete original suspect marker, if present: reason, detection timestamp,
  and replacement backup ID. Preserve all existing suspect change detection.

Derive artifact names from validated layout rules, then compare recorded names;
never accept arbitrary marker paths, traversal components, or absolute paths.
Reject unsupported versions, oversized/truncated records, duplicate artifact names,
links, nonregular markers, and inconsistent identities. Compare surviving manifest
and all three suspect-marker fields against recorded intent where present. Missing
expected artifacts are allowed only after validated pending intent is established.

Write the temporary regular file in a private temporary location on the same
destination filesystem, outside committed backup directories. Validate that location
without following unexpected links. Flush the file's content, atomically publish
the marker into the backup directory, and flush the affected directories before
unlinking any artifact. No artifact deletion may begin before durable publication.
Publish with Linux `renameat2(RENAME_NOREPLACE)` relative to validated temporary
and backup directory descriptors. Do not use `os.replace()` or an existence check
followed by overwriting rename. No directory is renamed. If atomic no-replace
publication is unsupported, defer cleanup without a weaker fallback. Revalidate
before publication and never overwrite any existing marker. An existing marker,
including one published concurrently, stops this attempt; validate it only during
a subsequent cleanup invocation rather than retrying publication or deletion.
Any write, publication, or flush failure aborts artifact deletion.
Temporary files are unpublished preparation only, never deletion authorization or
a separate cleanup journal. Interruption before publication must leave the healthy
backup directory unchanged and eligible.

Use the exact destination's validated held lock and confirm the opened parent
identifies that destination before preparation. Request owner-only permissions for
the preparation directory and files. On local and NFS filesystems, enforce directory
ownership by the execution user and no group/other permission bits. On validated
CIFS/SMB (`cifs` or `smb3`) mounts, ownership and modes may be mount-defined; rely on
the destination's existing server/share ACLs and mount access controls for privacy.
Retain same-filesystem, descriptor, exclusive-creation, atomic no-replace, and
flush checks on every filesystem. No configuration bypass is introduced.

### 2. Separate discovery from recovery-point eligibility

Extend `DiscoveryResult` and inventory facts with pending and empty-orphan entries.
Check pending intent before requiring a manifest or complete artifacts. Invalid
markers and pending directories containing unexpected entries remain protected
diagnostics and never fall back to valid-backup classification.

For retention decisions, validate required artifacts, manifest, suspect state, and
checksum-sidecar syntax/agreement without hashing archive contents. Keep ordinary
listing free of archive-content reads. Feed only eligible committed backups into
the unchanged tier planner. Keep suspect replacement resolution restricted to
eligible backups, and exclude pending entries from state reconstruction and
unchanged-backup evidence.

Do not describe metadata eligibility as archive verification: checksum hashing and
archive-content verification remain explicit, separate checks. Preserve existing
corruption detection and suspect-marker behavior during destructive validation.

#### Active-history boundaries and replacement detection

Retain the full classified inventory for diagnostics. `_active_history()` must
preserve pending and orphan collections when rebuilding `DiscoveryResult`, while
separately identifying entries authorized for the current cleanup plan. Preserve
these collections in inventory aggregation and `cli/status.py` reconstruction too.

Apply existing `ReplacementIdentity` compatibility rules to pending intent, using
the identity stored in the marker. A change to source coverage, application identity,
or another replacement-identity field does not authorize finishing deletion of an
incompatible history. Deleted or excluded per-file histories remain visible in
list/status/verify, but neither automatic nor manual prune deletes their pending
entries. They become actionable only when compatible active configuration is
restored and a healthy survivor is verified; do not add an inactive-history override.
Preserve orphan diagnostics throughout filtering; remove them only in an active
destination namespace under the empty-orphan rules, never by traversing a deleted
or excluded per-file history just to perform cleanup.

Pending suspects in a compatible active history participate in unresolved-replacement
detection before deciding UNCHANGED. They never provide recovery-point evidence.
Resolve the recorded designated replacement only if it is unique, compatible,
eligible, and passes actual archive-checksum verification. Missing, malformed,
suspect, pending, incompatible, or corrupt replacements force a fresh backup even
when sources are unchanged. Metadata ambiguity retains the existing destructive
barrier and must not be treated as successful replacement resolution.

After successful fresh creation, the new eligible backup can serve as the replacement
under the existing compatible replacement rules. Resolve and protect that dependency
in the current complete plan and verify it before cleanup. Preserve the original
intent and complete original suspect snapshot; do not rewrite them to name the new
replacement. Each later invocation must independently resolve and verify a safe
replacement again. If the original designated replacement remains healthy, preserve
its existing dependency protection. Inactive pending suspects do not force backups
for a different active history.

### 3. Extend the existing pruning plan and executor

Represent pending completions and empty-orphan removals alongside ordinary pruning
work, keeping them outside tier selection. Preserve original pending intent even
if the backup would otherwise occupy a tier now.

Before the first deletion, validate the complete plan, duplicate identities across
classifications, every dependency, and surviving recovery points. For every
affected history, require a healthy eligible survivor outside all planned deletion
sets, then verify its archive checksum against sidecar and immutable manifest.
Hash the actual surviving archive; metadata agreement alone is insufficient. If
verification fails, abort pruning before publishing a candidate's marker or
otherwise modifying that candidate. Apply this gate to resumed cleanup as well.
Preserve and verify designated or freshly resolved suspect replacements; protect
these dependencies from ordinary retention deletion. Never substitute a different history.

For each new deletion:

1. Revalidate the candidate and dependencies and persist durable deletion intent.
2. Delete only the explicitly recognized regular artifacts, in a fixed order.
3. Recheck entries as cleanup proceeds; stop on changed or unexpected entries.
4. Flush artifact removals while the intent marker still exists.
5. Confirm only the marker remains, remove it, and flush the directory.
6. Remove the now-empty directory and flush its parent.

For resumed deletion, use the same executor with the validated marker. Treat
already-absent expected artifacts as completed deletions; never require the old
archive, checksum, or manifest. Do not hash a partly deleted candidate.

Any filesystem failure stops that cleanup pass in the affected destination,
including further candidates. Report operation, failing path, and completed
directory count through `PruneError`. Do not retry, recreate a removed marker,
or use recursive deletion. Independent per-file destinations retain their existing
isolation. Revalidate before each destructive step, not only at discovery time.

### 4. Recover empty directories conservatively

A marker-free directory left after final `rmdir()` failure is an empty orphan,
not a valid backup. Under the destination lock, confirm it is a real direct child
of the validated destination, matches existing backup naming rules, and is empty;
use only `rmdir()`. Recheck immediately before removal. If another entry appears,
stop and report it rather than deleting its contents.

Because an empty directory contains no history evidence, remove only its directory
entry; do not fabricate a backup identity or recovery-point dependency. A nonempty
directory without valid intent remains unknown and protected.

### 5. Integrate automatic and manual cleanup

After a new backup is committed and passes the existing post-hook integrity checks,
discover and plan pending completions, orphan removals, and new retention deletions
in one pass under the existing destination lock. Process prior pending work before
new deletions, with all dependencies validated before execution starts.

Resume only on a later newly committed successful backup or explicit manual prune.
An UNCHANGED or failed run does not initiate resumption. No second cleanup pass
occurs during the same execution after failure. A history without a safe survivor
remains deferred, even if its marker was valid when originally written.

Reuse `cleanup_errors` and existing successful result handling. Filesystem cleanup
failures preserve `created`/`SUCCESS`, success hooks, and successful notification
classification, with a cleanup warning. Retain the existing exception for evidence
that the newly committed backup itself is corrupt: that is an integrity failure,
not merely cleanup deferral.

Manual prune uses identical validation and marker handling. Dry-run performs only
reads: no lock-file creation, marker writes, suspect writes, unlinks, or directory
removals. It reports planned pending/orphan work without claiming execution or
cryptographic verification. Preserve existing manual-prune failure exit behavior.

### 6. Update diagnostics and documentation

- `list`: use ELIGIBLE for metadata-only validated backups. Keep PRUNE_PENDING,
  INVALID, and EMPTY_ORPHAN distinguishable, with suspect details; show pending
  cleanup separately from ordinary tier decisions. Reserve VERIFIED for successful
  archive verification; metadata-only listing must never assign that label.
  Classification is a separate column from the existing KEEP/PRUNE/PROTECTED/UNKNOWN
  retention decision and tier columns; preserve those decisions and their meanings.
- `verify`: report pending entries as deferred cleanup and exclude them from
  checksum/archive verification and verified-success counts. Report malformed
  entries through existing unsuccessful verification diagnostics. Pending presence
  alone is a warning, not a claim of corruption or successful verification.
- `status`: populate cleanup-pending diagnostics from destination discovery,
  including after an interrupted execution; retain the actual backup result and
  existing JSON fields. Distinguish unsafe pending state from valid intent.
- Summary totals: separate eligible, suspect, pending, orphan, and invalid entries;
  never count the latter categories as successful recovery points.
- Update [README](../../README.md), [CLI reference](../cli.md),
  [status reference](../status.md), and [troubleshooting](../troubleshooting.md)
  where affected. Explain deferred cleanup, later-success/manual resumption, and
  protected malformed entries without presenting planned behavior as available
  before implementation.
- Update [architecture specification](../design/architecture_and_design.md),
  especially sections 13.4 and 14.3, during implementation. Integrate pending intent,
  eligibility, replacement-loss/UNCHANGED behavior, active-history filtering,
  descriptor safety, publication, and verification contracts into the existing
  sections. Keep the operational documentation consistent with implemented behavior.

#### Inventory presentation contract

The classification and retention columns are independent. Representative rows
below omit timestamps, sizes, and paths for clarity; retain those existing details
where available. A dash means no ordinary retention decision or tier applies.

```text
Classification  Decision   Tiers    Detail
ELIGIBLE        KEEP       daily    Metadata eligible; archive not verified
ELIGIBLE        PRUNE      —        Not selected by a retention tier
SUSPECT         PROTECTED  —        No healthy compatible replacement
SUSPECT         PRUNE      —        Compatible replacement; guarded deletion planned
PRUNE_PENDING   —          —        Cleanup deferred
EMPTY_ORPHAN    —          —        Empty directory; guarded removal planned
INVALID         —          —        Excluded; diagnostic reason
```

Eligible entries whose retention plan is blocked still show ELIGIBLE classification
and UNKNOWN decision; eligibility does not imply that deletion is authorized.
Inactive pending/orphan entries show a preserved/inactive detail instead of a
planned removal. VERIFIED is available only in actual verification output, never
in metadata-only list output.

#### Verification result and exit contract

Keep verification separate from backup execution results. Count a backup as
VERIFIED only after both checksum and archive-content verification succeed.
Pending/orphan entries are skipped with explicit diagnostics and separate totals.
Use existing exception-to-exit-code mapping, without a new backup result type:

| History contents/outcome | Command outcome | Summary | Exit code |
| --- | --- | --- | --- |
| Pending only, no verified recovery point | Incomplete verification (`DestinationError`); never `verified` | `Verified: 0; PRUNE_PENDING: N; no recovery point verified` | 5 |
| One or more successfully verified backups plus pending entries, no failures | `CommandResult("verify", "verified")` with warning | `Verified: N; PRUNE_PENDING: M; cleanup deferred` | 0 |
| Malformed entries, with or without pending or verified backups | Verification fails (`DestinationError`) | Successful count plus INVALID and PRUNE_PENDING totals and reasons; no overall VERIFIED success | 5 |
| Actual archive checksum/content verification failure | Verification fails (`ArchiveVerificationError`) | Successful count plus verification failures and skipped-entry totals | 7 |

Archive verification failures take precedence over destination-only failures, as
they do today. Suspect entries retain existing unsuccessful verification behavior.
A history containing only pending and/or empty-orphan entries also has no verified
recovery point and exits 5. Do not change the existing behavior for a genuinely empty
destination as part of this task.

Apply the no-recovery-point rule to each discovered independent per-file history;
successful verification of a sibling does not turn a pending-only history into a
successful verification. Aggregate counts and warnings across histories and retain
failure precedence. Carry counts/diagnostics through failure reporting rather than
losing successfully verified counts when an exception is raised. Both interactive
and plain output must show the outcome and never label skipped entries VERIFIED.

## Regression tests and validation

Use existing disposable destination/backup helpers and simulated filesystem errors.
Extend discovery, safety, inventory, workflow, verification, status, notification,
and integration tests; keep marker/executor tests focused in a destination test
module if useful.

1. Normal pruning: exact artifacts removed and surviving history remains usable.
2. Parameterized failures deleting archive, checksum, manifest, suspect marker,
   pending marker, and final directory (`ENOTEMPTY`); assert exact residual state,
   one attempt, failing operation/path, and no later candidate deletion.
3. Marker creation/publication/file-flush/directory-flush failures: no artifact
   deletion, no unsafe fallback, and a completed backup remains successful.
   Terminate before publication and assert that temporary files are outside
   committed backup directories and the original backup remains eligible.
4. Resume each partial state in a fresh invocation after a successful new backup;
   include missing archive/checksum/manifest and marker-only state.
5. Simulate termination at marker publication, between unlinks, before marker
   removal, and before/after directory removal. Include a process-termination
   integration case proving recovery does not depend on in-memory state.
6. Missing, invalid, oversized, unsupported, conflicting, or linked markers;
   wrong job/history/directory/backup identity; unsafe artifact names; surviving
   manifest disagreement; duplicate backup IDs across valid and pending entries.
7. Unexpected files, subdirectories, symlinks, and changes between planning and
   deletion: retain contents and refuse unsafe work. Assert no outside deletion.
8. Empty-orphan cleanup with destination lock, plus nonempty, nonmatching,
   linked, outside-destination, and concurrently changed counterexamples.
9. Tier exclusion for staging, missing/invalid manifest, missing artifacts,
   invalid checksum metadata, suspect, and pending entries, including the A/B/C/D
   example. Keep pure tier-selection behavior unchanged.
10. Invalid/pending entries cannot replace suspects or satisfy survivor checks;
    protect last healthy points and designated replacements. Exercise independent
    source identities, formats, per-file histories, and unsafe sibling histories.
    Corrupt a survivor's archive while leaving its checksum metadata consistent;
    assert actual checksum verification fails before any candidate modification,
    including marker publication, for both new and resumed deletion.
11. Successful backup plus failed cleanup yields SUCCESS, cleanup diagnostics,
    successful exit and notification classification, with no failure hook/alert.
    UNCHANGED/failed executions do not resume cleanup.
12. List/status/verify labels and totals; dry-run snapshots prove no mutations;
    listing does not hash archives, and full verification retains its checks.
    Assert metadata-only listing uses ELIGIBLE, never VERIFIED, and distinguishes
    PRUNE_PENDING, INVALID, and EMPTY_ORPHAN.
13. Pending suspect with unchanged sources and a missing, malformed, suspect,
    pending, or checksum-corrupt designated replacement forces fresh creation;
    the new compatible verified survivor enables cleanup without rewriting original
    intent. A healthy replacement permits UNCHANGED without cleanup. Incompatible
    pending suspects do not force a backup for the active history.
14. Changed source/application identities and deleted/excluded per-file histories:
    pending entries remain visible but untouched by automatic and manual pruning.
    Restoring compatible configuration makes cleanup reachable only with a verified
    healthy survivor. Assert pending/orphan collections survive `_active_history()`,
    inventory aggregation, and status reconstruction.
15. Duplicate IDs across two pending entries and across pending/ordinary entries
    block destructive work. Changes to any surviving original suspect-marker field
    (reason, timestamp, replacement ID) invalidate pending revalidation.
16. Preexisting and concurrently published markers are never overwritten, including
    identical markers. Assert atomic no-replace publication, no artifact deletion on
    collision or unsupported publication, and no same-invocation fallback/retry.
17. Swap parent/directory entries or introduce links between inspection and unlink;
    assert descriptor-relative operations target only validated objects and refuse
    mismatched parent entries. Assert unsupported safe operations defer cleanup.
18. Pending-only verification, mixed verified/pending, pending plus malformed, and
    pending plus archive failure: assert command results, exact exit codes, totals,
    warnings, and absence of false VERIFIED output. Repeat across independent
    per-file histories, including a pending-only sibling of a verified history.
19. CIFS/SMB mount-defined owner/mode values permit marker publication and deferred
    cleanup/resumption only under the exact validated destination lock. Local/NFS
    owner-only checks remain strict. Reject symlinks, mismatched parent descriptors,
    and different-filesystem temporary storage even on CIFS/SMB.

Run focused affected unit/integration tests, then repository checks from
[testing guidance](../development/testing.md):

```sh
ruff format --check .
ruff check .
mypy
pytest --no-cov -q
python scripts/check_packages.py
git diff --check
```

Report failures, skipped environment-dependent tests, and unavailable prerequisites
explicitly. No real SMB server or cloud account is required for new regressions.

## Acceptance criteria

1. Transient cleanup failures leave validated pending intent or a safely removable
   empty orphan, with no retry during the same execution.
2. A subsequent newly committed successful backup or explicit manual prune can
   resume cleanup using the original intent and current safety checks.
3. Before deleting a backup, verify at least one surviving eligible recovery point
   for the same history against its actual archive checksum, sidecar, and immutable
   manifest. If verification fails, abort pruning before modifying the candidate.
   This applies to both new deletion and resumed pending cleanup.
4. No artifact deletion begins before durable marker publication. Temporary marker
   files remain outside committed backup directories on the same destination
   filesystem, so interruption before publication cannot invalidate a healthy backup.
5. Incomplete, invalid, suspect, and pending backups never satisfy retention tiers,
   replacement dependencies, or healthy-survivor requirements.
6. A completed backup remains SUCCESS when filesystem cleanup is deferred, with
   cleanup warnings and no backup-failure notification.
7. Metadata-only inventory uses ELIGIBLE; VERIFIED is reserved for successful
   archive verification. PRUNE_PENDING, INVALID, and EMPTY_ORPHAN remain distinct
   and excluded from successful recovery-point totals.
8. Existing locks, identity checks, history isolation, malformed-entry protections,
   and exact artifact deletion remain effective. Dry-run is read-only, no separate
   persistent journal is introduced, and relevant automated checks pass.
9. Compatible active pending suspects with unresolved replacement dependencies
   force fresh creation despite unchanged sources; pending entries never become
   recovery evidence. Inactive, excluded, deleted, and incompatible histories remain
   visible and protected. Resumption requires a currently compatible healthy survivor.
10. The complete original suspect marker is recorded and revalidated. Destructive
    operations use validated descriptors where supported, and publication is atomic
    and no-replace; unsafe or unsupported operations defer cleanup.
11. Verification outcomes and exit codes follow the explicit contract above;
    classification stays separate from retention decisions. Architecture sections
    13.4 and 14.3 and affected operational references reflect the implemented design.

## Boundaries and limitations

No additional persistent state is needed once valid deletion intent is durable.
Safe recovery depends on the filesystem honoring successful file/directory flushes;
flush failures defer deletion rather than weakening durability requirements.
Filesystems without supported atomic no-replace file publication or required
descriptor-relative operations also defer cleanup; no unsafe compatibility fallback
is introduced. The private temporary location remains preparation storage only.

A process killed before atomic marker publication can leave an unpublished
temporary file in the private temporary location. It provides no deletion
authorization and cannot affect committed-backup discovery or eligibility.
Unexpected synchronization files inside a backup after marker removal prevent
the directory from qualifying as an empty orphan and may require operator
inspection, as required by the protection of unknown nonempty entries.

External synchronization software does not honor VaultKeep's lock. Revalidation
and exact no-follow operations limit destructive work; they cannot guarantee
availability or prevent a remote client from changing already-checked contents.
No claim of cryptographic eligibility is made without the corresponding check.
