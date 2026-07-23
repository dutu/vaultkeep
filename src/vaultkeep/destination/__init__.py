"""Destination naming, manifest, discovery, and commit APIs."""

from vaultkeep.destination.creation import commit_archive_artifact
from vaultkeep.destination.discovery import DiscoveredBackup, DiscoveryResult, discover_backups
from vaultkeep.destination.finalize import finalize_backup_directory
from vaultkeep.destination.manifest import BackupManifest, load_manifest, write_manifest
from vaultkeep.destination.pruning import (
    DestinationPrunePlan,
    build_prune_plan,
    execute_prune_plan,
)
from vaultkeep.destination.templates import (
    BackupPaths,
    allocate_job_backup_paths,
    create_staging_directory,
    render_backup_base_name,
)

__all__ = [
    "BackupManifest",
    "BackupPaths",
    "DestinationPrunePlan",
    "DiscoveredBackup",
    "DiscoveryResult",
    "allocate_job_backup_paths",
    "build_prune_plan",
    "commit_archive_artifact",
    "create_staging_directory",
    "discover_backups",
    "execute_prune_plan",
    "finalize_backup_directory",
    "load_manifest",
    "render_backup_base_name",
    "write_manifest",
]
