"""Guarded destination pruning based on a complete retention plan."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.config.models import RetentionConfig
from vaultkeep.destination.discovery import DiscoveredBackup, DiscoveryResult
from vaultkeep.errors import PruneError
from vaultkeep.retention.planner import RetentionBackup, RetentionPlan, plan_retention


@dataclass(frozen=True, slots=True)
class DestinationPrunePlan:
    """Complete pruning decision referring only to validated destination backups."""

    retention: RetentionPlan
    backups_to_delete: tuple[DiscoveredBackup, ...]


def build_prune_plan(
    discovery: DiscoveryResult, retention: RetentionConfig
) -> DestinationPrunePlan:
    """Create a side-effect-free plan, refusing destructive work around malformed entries."""
    if discovery.malformed:
        raise PruneError("Matching malformed destination entries block destructive pruning")
    by_id = {backup.manifest.backup_id: backup for backup in discovery.backups}
    plan = plan_retention(
        tuple(
            RetentionBackup(
                backup_id=backup.manifest.backup_id,
                directory=backup.directory,
                created_at=backup.manifest.created_datetime,
                created_at_utc=backup.manifest.created_datetime_utc,
            )
            for backup in discovery.backups
        ),
        retention,
    )
    return DestinationPrunePlan(plan, tuple(by_id[backup.backup_id] for backup in plan.delete))


def execute_prune_plan(plan: DestinationPrunePlan, discovery: DiscoveryResult) -> tuple[Path, ...]:
    """Delete only artifacts from the exact validated discovery result."""
    if discovery.malformed:
        raise PruneError("Matching malformed destination entries block destructive pruning")
    discovered = {backup.directory: backup for backup in discovery.backups}
    removed: list[Path] = []
    for backup in plan.backups_to_delete:
        if discovered.get(backup.directory) != backup:
            raise PruneError("Prune plan does not match the complete discovery result")
        _remove_valid_backup(backup)
        removed.append(backup.directory)
    return tuple(removed)


def _remove_valid_backup(backup: DiscoveredBackup) -> None:
    """Remove the three known regular artifacts and then their directory."""
    directory = backup.directory
    try:
        status = directory.lstat()
    except OSError as error:
        raise PruneError(f"Cannot inspect backup directory {directory}: {error}") from error
    if not stat.S_ISDIR(status.st_mode) or directory.is_symlink():
        raise PruneError(f"Backup directory changed since discovery: {directory}")
    expected = {backup.archive_path.name, backup.checksum_path.name, backup.manifest_path.name}
    try:
        children = {child.name: child for child in directory.iterdir()}
    except OSError as error:
        raise PruneError(f"Cannot inspect backup directory {directory}: {error}") from error
    if set(children) != expected:
        raise PruneError(f"Backup directory changed since discovery: {directory}")
    for artifact in (backup.archive_path, backup.checksum_path, backup.manifest_path):
        try:
            artifact_status = artifact.lstat()
        except OSError as error:
            raise PruneError(f"Cannot inspect backup artifact {artifact}: {error}") from error
        if not stat.S_ISREG(artifact_status.st_mode):
            raise PruneError(f"Backup artifact changed since discovery: {artifact}")
    try:
        for artifact in (backup.archive_path, backup.checksum_path, backup.manifest_path):
            artifact.unlink()
        directory.rmdir()
        _fsync_directory(directory.parent)
    except OSError as error:
        raise PruneError(
            f"Cannot remove validated backup directory {directory}: {error}"
        ) from error


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
