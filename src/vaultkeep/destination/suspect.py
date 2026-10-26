"""Destination-authoritative suspect markers; immutable archives are never rewritten."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from vaultkeep.errors import DestinationError, StateError
from vaultkeep.state.atomic import write_json
from vaultkeep.state.models import BackupId, NonEmptyString, StateModel, UtcTimestamp, utc_timestamp

SUSPECT_MARKER_NAME = ".vaultkeep-suspect"
_MAX_MARKER_BYTES = 4096


class SuspectMarker(StateModel):
    """Original diagnostic plus an optional compatible replacement reference."""

    reason: NonEmptyString
    detected_at: UtcTimestamp
    replacement_backup_id: BackupId | None = None


def read_suspect_marker(directory: Path) -> SuspectMarker | None:
    """Read a bounded regular marker without following a symbolic link."""
    marker = directory / SUSPECT_MARKER_NAME
    try:
        descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DestinationError(f"Cannot inspect suspect marker {marker}: {error}") from error
    try:
        with os.fdopen(descriptor, "rb") as stream:
            status = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
                or status.st_size > _MAX_MARKER_BYTES
            ):
                raise DestinationError(f"Suspect marker must be a small regular file: {marker}")
            payload = stream.read(_MAX_MARKER_BYTES + 1)
            if len(payload) > _MAX_MARKER_BYTES:
                raise ValueError("marker is oversized")
        return SuspectMarker.model_validate_json(payload)
    except (OSError, ValueError) as error:
        raise DestinationError(f"Invalid suspect marker {marker}: {error}") from error


def _write_marker(directory: Path, marker: SuspectMarker) -> None:
    if (
        len(
            (
                json.dumps(marker.model_dump(), ensure_ascii=True, sort_keys=True, indent=2) + "\n"
            ).encode()
        )
        > _MAX_MARKER_BYTES
    ):
        raise DestinationError(f"Suspect diagnostic is too long: {directory}")
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise DestinationError(f"Suspect backup must be a real directory: {directory}")
    try:
        write_json(directory / SUSPECT_MARKER_NAME, marker.model_dump())
    except StateError as error:
        raise DestinationError(f"Cannot record suspect marker in {directory}: {error}") from error


def mark_backup_suspect(directory: Path, reason: str) -> None:
    """Atomically record evidence, preserving the first diagnostic and detection time.

    The caller holds the destination lock for the entire inspection/update lifecycle.
    """
    if read_suspect_marker(directory) is None:
        _write_marker(
            directory, SuspectMarker(reason=reason, detected_at=utc_timestamp(datetime.now(UTC)))
        )


def record_suspect_replacement(directory: Path, backup_id: str) -> None:
    """Replace an absent or stale reference, retaining the original diagnostic."""
    marker = read_suspect_marker(directory)
    if marker is None:
        raise DestinationError(f"Suspect marker disappeared from {directory}")
    _write_marker(
        directory,
        SuspectMarker(
            reason=marker.reason, detected_at=marker.detected_at, replacement_backup_id=backup_id
        ),
    )
