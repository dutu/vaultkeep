"""Immutable, strict backup manifests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints, model_validator

from vaultkeep.errors import ManifestError
from vaultkeep.state.models import BackupId, Digest, UtcTimestamp, utc_timestamp

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
ArchiveName = Annotated[str, StringConstraints(min_length=1, pattern=r"^[^/\\\x00]+$")]


def _validate_local_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("created_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("created_at must include a UTC offset")
    return value


LocalTimestamp = Annotated[str, AfterValidator(_validate_local_timestamp)]


class BackupManifest(BaseModel):
    """All immutable facts recorded for a committed backup."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    manifest_version: Literal[1] = 1
    application: Literal["vaultkeep"] = "vaultkeep"
    application_version: NonEmptyString
    backup_id: BackupId
    job: NonEmptyString
    created_at: LocalTimestamp
    created_at_utc: UtcTimestamp
    source_digest: Digest
    config_fingerprint: Digest
    archive: ArchiveName
    archive_digest: Digest
    archive_format: Literal["tar.zst", "tar.7z"]
    encrypted: bool
    hostname: NonEmptyString

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        local_time = datetime.fromisoformat(self.created_at)
        if utc_timestamp(local_time) != self.created_at_utc:
            raise ValueError("created_at and created_at_utc must describe the same instant")
        expected_suffix = f".{self.archive_format}"
        if not self.archive.endswith(expected_suffix):
            raise ValueError("archive name does not match archive_format")
        if self.encrypted != (self.archive_format == "tar.7z"):
            raise ValueError("encrypted must match the selected archive format")
        return self

    @property
    def created_datetime(self) -> datetime:
        """Return the offset-aware local creation time."""
        return datetime.fromisoformat(self.created_at)

    @property
    def created_datetime_utc(self) -> datetime:
        """Return the normalized UTC creation time."""
        return datetime.fromisoformat(self.created_at_utc.removesuffix("Z") + "+00:00").astimezone(
            UTC
        )


def write_manifest(path: Path, manifest: BackupManifest) -> None:
    """Exclusively write and flush an immutable manifest before directory commit."""
    payload = (
        json.dumps(manifest.model_dump(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise ManifestError(f"Cannot write backup manifest {path}: {error}") from error


def load_manifest(path: Path) -> BackupManifest:
    """Load exactly one strict UTF-8 JSON manifest."""
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ManifestError(f"Cannot read backup manifest {path}: {error}") from error
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"Invalid JSON backup manifest {path}: {error}") from error
    if not isinstance(decoded, dict):
        raise ManifestError(f"Backup manifest {path} must contain a JSON object")
    try:
        return BackupManifest.model_validate(decoded)
    except ValueError as error:
        raise ManifestError(f"Invalid backup manifest {path}: {error}") from error
