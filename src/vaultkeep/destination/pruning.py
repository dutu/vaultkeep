"""Guarded destination pruning based on a complete retention plan."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.archive.checksums import read_checksum_sidecar, verify_checksum_sidecar
from vaultkeep.config.models import RetentionConfig
from vaultkeep.destination.discovery import DiscoveredBackup, DiscoveryResult
from vaultkeep.destination.manifest import load_manifest
from vaultkeep.destination.pending import (
    PendingBackup,
    PruneMarker,
    check_directory,
    opened_directory,
    read_regular,
    remove_backup,
    remove_orphan,
    validate_entries,
)
from vaultkeep.destination.suspect import (
    SUSPECT_MARKER_NAME,
    SuspectMarker,
    mark_backup_suspect,
    read_suspect_marker,
)
from vaultkeep.errors import (
    ArchiveChecksumMismatchError,
    ArchiveVerificationError,
    DestinationError,
    PruneError,
)
from vaultkeep.locking import destination_lock, held_destination_filesystem
from vaultkeep.progress import VerificationProgress, report_progress
from vaultkeep.retention.planner import (
    RetentionBackup,
    RetentionPlan,
    RetentionTier,
    plan_retention,
)


@dataclass(frozen=True, slots=True)
class DestinationPrunePlan:
    """Complete pruning decision referring only to validated destination backups."""

    retention: RetentionPlan
    backups_to_delete: tuple[DiscoveredBackup, ...]
    replacements: tuple[tuple[DiscoveredBackup, DiscoveredBackup], ...] = ()
    pending: tuple[PendingBackup, ...] = ()
    orphans: tuple[Path, ...] = ()


def build_prune_plan(
    discovery: DiscoveryResult, retention: RetentionConfig
) -> DestinationPrunePlan:
    """Create a side-effect-free plan, refusing destructive work around malformed entries."""
    if discovery.malformed:
        raise PruneError(
            "Matching malformed destination entries block destructive pruning: "
            + "; ".join(entry.reason for entry in discovery.malformed)
        )
    ids = [backup.manifest.backup_id for backup in discovery.backups]
    ids.extend(item.manifest.backup_id for item in discovery.pending)
    if len(ids) != len(set(ids)):
        raise PruneError("Duplicate backup identities block pruning")
    healthy = tuple(backup for backup in discovery.backups if backup.suspect_reason is None)
    by_id = {backup.manifest.backup_id: backup for backup in healthy}
    replacements = tuple(
        (backup, replacement)
        for backup in discovery.backups
        if backup.suspect_reason is not None
        and (replacement := valid_replacement(backup, discovery)) is not None
    )
    pending = tuple(item for item in discovery.pending if item.active)
    pending_survivors: list[DiscoveredBackup] = []
    for item in pending:
        survivor = pending_replacement(item, discovery)
        if survivor is None:
            raise PruneError(
                f"Pending cleanup has no eligible recovery point in its history: {item.directory}"
            )
        pending_survivors.append(survivor)
    # A reference dependency is protected even if the suspect deletion later fails.
    protected_ids = {replacement.manifest.backup_id for _, replacement in replacements}
    protected_ids.update(backup.manifest.backup_id for backup in pending_survivors)
    groups: dict[str, list[DiscoveredBackup]] = {}
    for backup in healthy:
        groups.setdefault(backup.manifest.replacement_identity.model_dump_json(), []).append(backup)
    plans = [
        plan_retention(
            tuple(
                RetentionBackup(
                    backup_id=backup.manifest.backup_id,
                    directory=backup.directory,
                    created_at=backup.manifest.created_datetime,
                    created_at_utc=backup.manifest.created_datetime_utc,
                )
                for backup in group
            ),
            retention,
        )
        for group in groups.values()
    ]
    tiers: tuple[RetentionTier, ...] = ("hourly", "daily", "weekly", "monthly", "yearly")
    selections = {
        tier: tuple(backup for plan in plans for backup in plan.selections.get(tier, ()))
        for tier in tiers
    }
    plan = RetentionPlan(
        retain=tuple(
            backup
            for plan in plans
            for backup in (
                *plan.retain,
                *(item for item in plan.delete if item.backup_id in protected_ids),
            )
        ),
        delete=tuple(
            backup
            for plan in plans
            for backup in plan.delete
            if backup.backup_id not in protected_ids
        ),
        selections=selections,
    )
    return DestinationPrunePlan(
        plan,
        tuple(backup for backup, _ in replacements)
        + tuple(by_id[backup.backup_id] for backup in plan.delete),
        replacements,
        pending,
        discovery.orphans if discovery.active_orphans is None else discovery.active_orphans,
    )


def valid_replacement(
    backup: DiscoveredBackup, discovery: DiscoveryResult
) -> DiscoveredBackup | None:
    """Resolve metadata only; a reference is never deletion authorization by itself."""
    matches = tuple(
        candidate
        for candidate in discovery.backups
        if candidate.manifest.backup_id == backup.replacement_backup_id
    )
    if backup.suspect_reason is None or len(matches) != 1:
        return None
    candidate = matches[0]
    if (
        candidate.directory == backup.directory
        or candidate.suspect_reason is not None
        or candidate.directory.parent != backup.directory.parent
        or candidate.manifest.job != backup.manifest.job
        or candidate.manifest.replacement_identity != backup.manifest.replacement_identity
    ):
        return None
    return candidate


def pending_replacement(
    backup: PendingBackup, discovery: DiscoveryResult, *, original_only: bool = False
) -> DiscoveredBackup | None:
    """Resolve compatible recovery evidence without making pending intent evidence."""
    candidates = tuple(
        candidate
        for candidate in discovery.backups
        if candidate.suspect_reason is None
        and candidate.directory.parent == backup.directory.parent
        and candidate.manifest.job == backup.manifest.job
        and candidate.manifest.replacement_identity == backup.manifest.replacement_identity
        and candidate.manifest.backup_id != backup.manifest.backup_id
    )
    original = tuple(c for c in candidates if c.manifest.backup_id == backup.replacement_backup_id)
    if len(original) == 1:
        return original[0]
    if original_only:
        return None
    return max(candidates, key=lambda c: c.manifest.created_datetime_utc, default=None)


def verify_backup_checksum(
    backup: DiscoveredBackup, *, on_progress: Callable[[VerificationProgress], None] | None = None
) -> None:
    """Require agreement with both immutable digest and checksum sidecar."""
    digest = verify_checksum_sidecar(
        backup.archive_path, backup.checksum_path, on_progress=on_progress
    )
    if backup.manifest.archive_digest != f"sha256:{digest}":
        raise ArchiveChecksumMismatchError("Archive SHA-256 differs from immutable manifest")
    report_progress(
        on_progress,
        VerificationProgress(
            "backup",
            "SHA-256",
            "ok",
            backup.archive_path,
            detail=f"Manifest and sidecar SHA-256: {digest}",
        ),
    )


def _verify_survivor(backup: DiscoveredBackup) -> None:
    """Pin archive/checksum reads to the directory before destructive cleanup."""
    with opened_directory(backup.directory) as (parent, descriptor):
        intent = _deletion_intent(backup)
        validate_entries(descriptor, intent, pending=False)
        payload = read_regular(descriptor, backup.checksum_path.name)
        expected = backup.manifest.archive_digest.removeprefix("sha256:")
        if payload != expected.encode() + b"  " + os.fsencode(backup.archive_path.name) + b"\n":
            raise ArchiveChecksumMismatchError("Checksum sidecar differs from immutable manifest")
        fd = os.open(
            backup.archive_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        with os.fdopen(fd, "rb") as stream:
            original = os.fstat(stream.fileno())
            if not stat.S_ISREG(original.st_mode) or original.st_nlink != 1:
                raise ArchiveVerificationError("Archive is not a single-link regular file")
            hasher = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
            current = os.stat(backup.archive_path.name, dir_fd=descriptor, follow_symlinks=False)
            for observed in (os.fstat(stream.fileno()), current):
                if any(
                    getattr(observed, field) != getattr(original, field)
                    for field in (
                        "st_dev",
                        "st_ino",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                        "st_nlink",
                    )
                ):
                    raise PruneError(
                        f"Recovery point changed during checksum verification: {backup.directory}"
                    )
        check_directory(parent, descriptor, backup.directory.name)
        validate_entries(descriptor, intent, pending=False)
        if read_regular(descriptor, backup.checksum_path.name) != payload:
            raise PruneError("Recovery-point checksum metadata changed during verification")
        digest = hasher.hexdigest()
        if digest != expected:
            raise ArchiveChecksumMismatchError("Archive checksum does not match archive content")


def execute_prune_plan(plan: DestinationPrunePlan, discovery: DiscoveryResult) -> tuple[Path, ...]:
    """Hold destination locks even for direct executor callers."""
    roots = {backup.directory.parent for backup in discovery.backups}
    roots.update(item.directory.parent for item in discovery.pending)
    roots.update(path.parent for path in discovery.orphans)
    if len(roots) > 1:
        raise PruneError("Execute pruning independently in each destination history")
    with ExitStack() as stack:
        for root in sorted(roots):
            if held_destination_filesystem(root) is None:
                stack.enter_context(destination_lock(root))
        return _execute_prune_plan(plan, discovery)


def _execute_prune_plan(plan: DestinationPrunePlan, discovery: DiscoveryResult) -> tuple[Path, ...]:
    """Delete only artifacts from the exact validated discovery result."""
    if discovery.malformed:
        raise PruneError(
            "Matching malformed destination entries block destructive pruning: "
            + "; ".join(entry.reason for entry in discovery.malformed)
        )
    discovered = {backup.directory: backup for backup in discovery.backups}
    if any(discovered.get(backup.directory) != backup for backup in plan.backups_to_delete):
        raise PruneError("Prune plan does not match the complete discovery result")
    if any(item not in discovery.pending or not item.active for item in plan.pending):
        raise PruneError("Pending plan differs from discovery")
    allowed_orphans = (
        discovery.orphans if discovery.active_orphans is None else discovery.active_orphans
    )
    if any(path not in allowed_orphans for path in plan.orphans):
        raise PruneError("Orphan plan differs from discovery")
    for orphan in plan.orphans:
        with opened_directory(orphan) as (_, descriptor):
            if os.listdir(descriptor):
                raise PruneError(f"Empty orphan is no longer empty: {orphan}")
    ids = [backup.manifest.backup_id for backup in discovery.backups]
    ids.extend(item.manifest.backup_id for item in discovery.pending)
    if len(ids) != len(set(ids)):
        raise PruneError("Duplicate backup identities block pruning")
    for item in plan.pending:
        with opened_directory(item.directory) as (_, descriptor):
            validate_entries(descriptor, item.marker, pending=True)
    # Validate the complete plan and every dependency before the first deletion.
    for backup in discovery.backups:
        _validate_candidate(backup)
    verified_replacements: set[str] = set()
    for suspect, replacement in plan.replacements:
        if valid_replacement(suspect, discovery) != replacement:
            raise PruneError("Invalid suspect replacement relationship")
        _validate_candidate(replacement)
        if replacement.manifest.backup_id in verified_replacements:
            continue
        try:
            _verify_survivor(replacement)
            verified_replacements.add(replacement.manifest.backup_id)
        except ArchiveChecksumMismatchError as error:
            raise _record_corruption(replacement, error) from error
    remaining = [
        backup
        for backup in discovery.backups
        if backup.suspect_reason is None and backup not in plan.backups_to_delete
    ]
    for backup in plan.backups_to_delete:
        if not any(
            candidate.manifest.replacement_identity == backup.manifest.replacement_identity
            for candidate in remaining
        ):
            raise PruneError("Cleanup must preserve an eligible recovery point in every history")
    for item in plan.pending:
        survivor = pending_replacement(item, discovery)
        if survivor is None or survivor not in remaining:
            raise PruneError(
                "Pending cleanup must preserve an eligible recovery point in its history"
            )
        _validate_candidate(survivor)
        try:
            _verify_survivor(survivor)
        except ArchiveChecksumMismatchError as error:
            raise _record_corruption(survivor, error) from error
    checked_classes: set[str] = set()
    for candidate in sorted(
        remaining, key=lambda backup: backup.manifest.created_datetime_utc, reverse=True
    ):
        identity = candidate.manifest.replacement_identity.model_dump_json()
        if identity in checked_classes or not any(
            backup.manifest.replacement_identity == candidate.manifest.replacement_identity
            for backup in plan.backups_to_delete
        ):
            continue
        _validate_candidate(candidate)
        try:
            if candidate.manifest.backup_id not in verified_replacements:
                _verify_survivor(candidate)
        except ArchiveChecksumMismatchError as error:
            raise _record_corruption(candidate, error) from error
        checked_classes.add(identity)
    removed: list[Path] = []
    try:
        for item in plan.pending:
            survivor = pending_replacement(item, discovery)
            if survivor is None or survivor not in remaining:
                raise PruneError("Pending recovery point disappeared")
            _validate_candidate(survivor)
            try:
                _verify_survivor(survivor)
            except ArchiveChecksumMismatchError as error:
                raise _record_corruption(survivor, error) from error
            remove_backup(item.directory, item.marker, pending=True)
            removed.append(item.directory)
        for orphan in plan.orphans:
            remove_orphan(orphan)
            removed.append(orphan)
        for backup in plan.backups_to_delete:
            _validate_candidate(backup)
            current_replacement = next(
                (candidate for suspect, candidate in plan.replacements if suspect == backup), None
            )
            if current_replacement is None:
                current_replacement = max(
                    (
                        candidate
                        for candidate in remaining
                        if candidate.manifest.replacement_identity
                        == backup.manifest.replacement_identity
                    ),
                    key=lambda candidate: candidate.manifest.created_datetime_utc,
                )
            if current_replacement is not None:
                _validate_candidate(current_replacement)
                try:
                    _verify_survivor(current_replacement)
                except ArchiveChecksumMismatchError as error:
                    raise _record_corruption(current_replacement, error) from error
            _remove_valid_backup(backup)
            removed.append(backup.directory)
    except (DestinationError, ArchiveVerificationError, OSError) as error:
        if isinstance(error, PruneError):
            removed.extend(Path(path) for path in error.removed)
        raise PruneError(
            f"Cleanup stopped after {len(removed)} deletions: {error}",
            removed=tuple(str(path) for path in removed),
            corrupt_backup_ids=error.corrupt_backup_ids if isinstance(error, PruneError) else (),
        ) from error
    return tuple(removed)


def _record_corruption(backup: DiscoveredBackup, error: ArchiveChecksumMismatchError) -> PruneError:
    message = f"Backup is corrupt: {backup.directory}: {error}"
    try:
        mark_backup_suspect(backup.directory, str(error))
    except (DestinationError, OSError) as marker_error:
        message += f"; cannot record suspect marker: {marker_error}"
    return PruneError(message, corrupt_backup_ids=(backup.manifest.backup_id,))


def _validate_candidate(backup: DiscoveredBackup) -> None:
    _validate_artifacts(backup)
    if load_manifest(backup.manifest_path) != backup.manifest:
        raise PruneError(f"Manifest changed since discovery: {backup.directory}")
    if backup.suspect_reason is None:
        try:
            digest = read_checksum_sidecar(backup.archive_path, backup.checksum_path)
        except ArchiveVerificationError as error:
            raise PruneError(
                f"Malformed checksum metadata in {backup.directory}: {error}"
            ) from error
        if backup.manifest.archive_digest != f"sha256:{digest}":
            mismatch = ArchiveChecksumMismatchError(
                "Checksum metadata does not match immutable manifest"
            )
            raise _record_corruption(backup, mismatch) from mismatch
    marker = read_suspect_marker(backup.directory)
    if (
        (marker.reason if marker else None) != backup.suspect_reason
        or (marker.replacement_backup_id if marker else None) != backup.replacement_backup_id
        or (marker.detected_at if marker else None) != backup.suspect_detected_at
    ):
        raise PruneError(f"Suspect marker changed since discovery: {backup.directory}")
    _validate_artifacts(backup)


def _deletion_intent(backup: DiscoveredBackup) -> PruneMarker:
    suspect = None
    if backup.suspect_reason is not None:
        if backup.suspect_detected_at is None:
            raise PruneError("Missing original suspect detection timestamp")
        suspect = SuspectMarker(
            reason=backup.suspect_reason,
            detected_at=backup.suspect_detected_at,
            replacement_backup_id=backup.replacement_backup_id,
        )
    return PruneMarker(
        version=1, directory_name=backup.directory.name, manifest=backup.manifest, suspect=suspect
    )


def _remove_valid_backup(backup: DiscoveredBackup) -> None:
    remove_backup(backup.directory, _deletion_intent(backup), pending=False)


def _validate_artifacts(backup: DiscoveredBackup) -> None:
    directory = backup.directory
    try:
        status = directory.lstat()
    except OSError as error:
        raise PruneError(f"Cannot inspect backup directory {directory}: {error}") from error
    if not stat.S_ISDIR(status.st_mode) or directory.is_symlink():
        raise PruneError(f"Backup directory changed since discovery: {directory}")
    expected = {backup.archive_path.name, backup.checksum_path.name, backup.manifest_path.name}
    if backup.suspect_reason is not None:
        expected.add(SUSPECT_MARKER_NAME)
    try:
        children = {child.name: child for child in directory.iterdir()}
    except OSError as error:
        raise PruneError(f"Cannot inspect backup directory {directory}: {error}") from error
    if set(children) != expected:
        raise PruneError(f"Backup directory changed since discovery: {directory}")
    for artifact in children.values():
        try:
            artifact_status = artifact.lstat()
        except OSError as error:
            raise PruneError(f"Cannot inspect backup artifact {artifact}: {error}") from error
        if not stat.S_ISREG(artifact_status.st_mode):
            raise PruneError(f"Backup artifact changed since discovery: {artifact}")
