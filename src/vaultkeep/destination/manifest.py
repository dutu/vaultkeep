"""Immutable, strict backup manifests."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    StringConstraints,
    field_validator,
    model_validator,
)

from vaultkeep.destination.identity import ReplacementIdentity
from vaultkeep.errors import ManifestError, UnsupportedManifestError
from vaultkeep.state.models import BackupId, Digest, UtcTimestamp

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

    manifest_version: Literal[2]
    replacement_identity: ReplacementIdentity
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

    @field_validator("manifest_version", mode="before")
    @classmethod
    def _version_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("manifest_version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        if json.loads(self.replacement_identity.source_scope)["job"] != self.job:
            raise ValueError("replacement source scope does not match manifest job")
        local_time = datetime.fromisoformat(self.created_at)
        if local_time.astimezone(UTC) != self.created_datetime_utc:
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
    payload = (json.dumps(manifest.model_dump(), sort_keys=True, indent=2) + "\n").encode("utf-8")
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
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ManifestError(f"Manifest must be a regular file: {path}")
            data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ManifestError(f"Manifest is oversized: {path}")
            payload = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ManifestError(f"Cannot read backup manifest {path}: {error}") from error
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"Invalid JSON backup manifest {path}: {error}") from error
    if not isinstance(decoded, dict):
        raise ManifestError(f"Backup manifest {path} must contain a JSON object")
    version = decoded.get("manifest_version")
    if version is not None and (type(version) is not int or version != 2):
        raise UnsupportedManifestError(
            f"Unsupported manifest_version {version!r} in {path}; "
            "expected 2 and refusing to modify this history"
        )
    try:
        return BackupManifest.model_validate(decoded)
    except ValueError as error:
        raise ManifestError(f"Invalid backup manifest {path}: {error}") from error
