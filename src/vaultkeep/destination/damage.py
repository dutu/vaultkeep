"""Persistent damage markers that leave committed backup artifacts intact."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from vaultkeep.errors import DestinationError, StateError
from vaultkeep.state.atomic import write_json
from vaultkeep.state.models import BackupId, NonEmptyString, StateModel

DAMAGE_MARKER_NAME = ".vaultkeep-damaged"
_MAX_MARKER_BYTES = 4096


class DamageMarker(StateModel):
    """Damage diagnostic and the backup that successfully replaced it."""

    reason: NonEmptyString
    replacement_backup_id: BackupId | None = None


def read_damage_marker(directory: Path) -> DamageMarker | None:
    """Return a recorded damage diagnostic without following marker symlinks."""
    marker = directory / DAMAGE_MARKER_NAME
    try:
        status = marker.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DestinationError(f"Cannot inspect damage marker {marker}: {error}") from error
    if not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_MARKER_BYTES:
        raise DestinationError(f"Damage marker must be a small regular file: {marker}")
    try:
        descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_MARKER_BYTES:
                raise DestinationError(f"Damage marker must be a small regular file: {marker}")
            payload = stream.read(_MAX_MARKER_BYTES).strip()
    except (OSError, UnicodeError) as error:
        raise DestinationError(f"Cannot read damage marker {marker}: {error}") from error
    # An interrupted marker write still excludes the backup from reuse and deletion.
    if not payload:
        return DamageMarker(reason="Marked damaged")
    try:
        return DamageMarker.model_validate_json(payload)
    except ValueError as error:
        raise DestinationError(f"Invalid damage marker {marker}: {error}") from error


def mark_backup_damaged(directory: Path, reason: str) -> None:
    """Exclusively write and flush a marker, preserving all existing artifacts."""
    marker = directory / DAMAGE_MARKER_NAME
    damage = DamageMarker(reason=f"{datetime.now(UTC).isoformat()}: {reason}")
    payload = (damage.model_dump_json() + "\n").encode()
    if len(payload) > _MAX_MARKER_BYTES:
        raise DestinationError(f"Damage diagnostic is too long for {marker}")
    try:
        with marker.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except FileExistsError:
        # Preserve the first recorded diagnostic; reject unsafe marker entries.
        read_damage_marker(directory)
    except OSError as error:
        raise DestinationError(f"Cannot mark backup as damaged {directory}: {error}") from error


def record_damage_replacement(directory: Path, backup_id: str) -> None:
    """Atomically record successful replacement so timestamp ties cannot trigger retries."""
    damage = read_damage_marker(directory)
    if damage is None:
        raise DestinationError(f"Damage marker disappeared from {directory}")
    if damage.replacement_backup_id is None:
        replacement = DamageMarker(reason=damage.reason, replacement_backup_id=backup_id)
        try:
            write_json(directory / DAMAGE_MARKER_NAME, replacement.model_dump())
        except StateError as error:
            raise DestinationError(
                f"Cannot record replacement for damaged backup {directory}: {error}"
            ) from error
