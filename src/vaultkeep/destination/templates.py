"""Safe, deterministic destination names for one backup."""

from __future__ import annotations

import os
import string
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vaultkeep.config.models import DestinationConfig
from vaultkeep.errors import DestinationTemplateError

_FORMATTER = string.Formatter()
_BACKUP_ID_LENGTH = 32


@dataclass(frozen=True, slots=True)
class BackupPaths:
    """All paths allocated for one not-yet-committed backup."""

    root: Path
    base_name: str
    backup_id: str
    archive_format: str
    staging_directory: Path
    final_directory: Path
    archive_path: Path
    checksum_path: Path
    manifest_path: Path


def render_backup_base_name(
    template: str,
    *,
    job_id: str,
    hostname: str,
    created_at: datetime,
    source_digest: str,
    archive_format: str,
) -> str:
    """Render and validate the safe common base for all backup artifacts."""
    if created_at.tzinfo is None:
        raise DestinationTemplateError("Backup creation timestamp must be timezone-aware")
    source_hash = _source_hash(source_digest)
    values = {
        "job": job_id,
        "hostname": hostname,
        "timestamp": created_at,
        "timestamp_utc": created_at.astimezone(UTC),
        "source_hash": source_hash,
        "format": archive_format,
    }
    try:
        fields = tuple(_FORMATTER.parse(template))
    except ValueError as error:
        raise DestinationTemplateError(f"Invalid destination name template: {error}") from error
    if not any(field in {"timestamp", "timestamp_utc"} for _, field, _, _ in fields):
        raise DestinationTemplateError("Destination name template must include a timestamp field")
    for _, field, _, conversion in fields:
        if field is None:
            continue
        if field not in values:
            raise DestinationTemplateError(f"Unsupported destination template field: {field}")
        if conversion is not None:
            raise DestinationTemplateError("Destination template conversions are not supported")
    try:
        rendered = template.format(**values)
    except (KeyError, ValueError) as error:
        raise DestinationTemplateError(
            f"Cannot render destination name template: {error}"
        ) from error
    _validate_base_name(rendered)
    return rendered


def allocate_job_backup_paths(
    destination: DestinationConfig,
    *,
    job_id: str,
    backup_id: str,
    hostname: str,
    created_at: datetime,
    source_digest: str,
    archive_format: str,
) -> BackupPaths:
    """Allocate all artifact paths for a job without creating filesystem entries."""
    _validate_backup_id(backup_id)
    if archive_format not in {"tar.zst", "tar.7z"}:
        raise DestinationTemplateError(f"Unsupported archive format: {archive_format}")
    root = Path(destination.root)
    base_name = render_backup_base_name(
        destination.name_template,
        job_id=job_id,
        hostname=hostname,
        created_at=created_at,
        source_digest=source_digest,
        archive_format=archive_format,
    )
    final_directory = root / f"{base_name}-{backup_id}"
    staging_directory = root / f".partial-vaultkeep-{job_id}-{backup_id}"
    archive_name = f"{base_name}.{archive_format}"
    return BackupPaths(
        root=root,
        base_name=base_name,
        backup_id=backup_id,
        archive_format=archive_format,
        staging_directory=staging_directory,
        final_directory=final_directory,
        archive_path=staging_directory / archive_name,
        checksum_path=staging_directory / f"{archive_name}.sha256",
        manifest_path=staging_directory / f"{base_name}.json",
    )


def create_staging_directory(paths: BackupPaths) -> None:
    """Create the private hidden staging directory without overwriting anything."""
    if paths.final_directory.exists() or paths.final_directory.is_symlink():
        raise DestinationTemplateError(
            f"Final backup directory already exists: {paths.final_directory}"
        )
    try:
        paths.staging_directory.mkdir(mode=0o700)
    except OSError as error:
        raise DestinationTemplateError(
            f"Cannot create backup staging directory {paths.staging_directory}: {error}"
        ) from error


def _source_hash(source_digest: str) -> str:
    prefix = "sha256:"
    value = source_digest.removeprefix(prefix)
    if (
        not source_digest.startswith(prefix)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DestinationTemplateError("Source digest must be a lowercase sha256 digest")
    return value


def _validate_backup_id(backup_id: str) -> None:
    if len(backup_id) != _BACKUP_ID_LENGTH or any(
        character not in "0123456789abcdef" for character in backup_id
    ):
        raise DestinationTemplateError("Backup ID must contain 32 lowercase hexadecimal characters")


def _validate_base_name(value: str) -> None:
    if not value:
        raise DestinationTemplateError("Destination name template rendered an empty name")
    if "/" in value or "\\" in value or ".." in value or "\x00" in value:
        raise DestinationTemplateError("Rendered destination name contains an unsafe path sequence")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DestinationTemplateError("Rendered destination name contains a control character")
    if os.path.basename(value) != value:
        raise DestinationTemplateError("Rendered destination name escapes the destination root")
