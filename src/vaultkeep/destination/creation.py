"""Bridge archive artifacts to a committed backup directory."""

from __future__ import annotations

from datetime import UTC, datetime

from vaultkeep.archive.base import ArchiveArtifact
from vaultkeep.destination.finalize import finalize_backup_directory
from vaultkeep.destination.identity import ReplacementIdentity
from vaultkeep.destination.manifest import BackupManifest, write_manifest
from vaultkeep.destination.templates import BackupPaths
from vaultkeep.errors import ManifestError


def commit_archive_artifact(
    paths: BackupPaths,
    artifact: ArchiveArtifact,
    *,
    application_version: str,
    job_id: str,
    hostname: str,
    created_at: datetime,
    config_fingerprint: str,
    replacement_identity: ReplacementIdentity,
) -> BackupManifest:
    """Write the immutable manifest then atomically commit the staging directory."""
    if artifact.archive_path != paths.archive_path or artifact.checksum_path != paths.checksum_path:
        raise ManifestError("Archive artifact paths do not match the allocated backup paths")
    if artifact.archive_format != paths.archive_format:
        raise ManifestError("Archive artifact format does not match the allocated backup paths")
    manifest = BackupManifest(
        manifest_version=2,
        replacement_identity=replacement_identity,
        application_version=application_version,
        backup_id=paths.backup_id,
        job=job_id,
        created_at=created_at.isoformat(timespec="microseconds"),
        created_at_utc=created_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        source_digest=artifact.source_digest,
        config_fingerprint=config_fingerprint,
        archive=artifact.archive_path.name,
        archive_digest=f"sha256:{artifact.sha256}",
        archive_format=artifact.archive_format,
        encrypted=artifact.archive_format == "tar.7z",
        hostname=hostname,
    )
    write_manifest(paths.manifest_path, manifest)
    finalize_backup_directory(paths.staging_directory, paths.final_directory)
    return manifest
