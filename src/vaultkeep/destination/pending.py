"""Durable per-backup deletion intent and descriptor-relative cleanup."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator

from vaultkeep.destination.manifest import BackupManifest
from vaultkeep.destination.suspect import SUSPECT_MARKER_NAME, SuspectMarker
from vaultkeep.errors import PruneError
from vaultkeep.locking import held_destination_filesystem
from vaultkeep.state.models import StateModel

PRUNE_MARKER_NAME = ".vaultkeep-prune-pending"
TEMP_DIRECTORY_NAME = ".vaultkeep-prune-tmp"
_MAX_BYTES = 1024 * 1024
_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class PruneMarker(StateModel):
    """Original immutable facts; names are derived, never arbitrary deletion paths."""

    version: Literal[1]
    directory_name: str
    manifest: BackupManifest
    suspect: SuspectMarker | None = None

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Prune marker version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_layout(self) -> PruneMarker:
        base = self.base_name
        if not base or base in {".", ".."} or any(c in base for c in "/\\\0"):
            raise ValueError("Invalid backup base name")
        suffix = self.directory_name.removeprefix(base)
        if not self.directory_name.startswith(base) or not (
            not suffix
            or suffix == f"-{self.manifest.backup_id}"
            or re.fullmatch(r"-(?:[2-9]|[1-9][0-9]+)", suffix)
        ):
            raise ValueError("Prune identity does not match directory")
        return self

    @property
    def base_name(self) -> str:
        return self.manifest.archive.removesuffix(f".{self.manifest.archive_format}")

    @property
    def artifacts(self) -> tuple[str, ...]:
        archive = self.manifest.archive
        return (archive, f"{archive}.sha256", f"{self.base_name}.json") + (
            (SUSPECT_MARKER_NAME,) if self.suspect is not None else ()
        )


@dataclass(frozen=True, slots=True)
class PendingBackup:
    directory: Path
    marker: PruneMarker
    active: bool = True

    @property
    def manifest(self) -> BackupManifest:
        return self.marker.manifest

    @property
    def suspect_reason(self) -> str | None:
        return self.marker.suspect.reason if self.marker.suspect else None

    @property
    def replacement_backup_id(self) -> str | None:
        return self.marker.suspect.replacement_backup_id if self.marker.suspect else None


@contextmanager
def opened_directory(path: Path) -> Iterator[tuple[int, int]]:
    """Pin the parent and directory without following a substituted final link."""
    parent = os.open(path.parent, _FLAGS)
    try:
        descriptor = os.open(path.name, _FLAGS, dir_fd=parent)
        try:
            check_directory(parent, descriptor, path.name)
            yield parent, descriptor
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def check_directory(parent: int, descriptor: int, name: str) -> None:
    expected = os.fstat(descriptor)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode) or (expected.st_dev, expected.st_ino) != (
        current.st_dev,
        current.st_ino,
    ):
        raise PruneError(f"Backup directory identity changed: {name}")


def read_regular(descriptor: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_BYTES:
            raise PruneError(f"Expected bounded single-link regular metadata: {name}")
        payload = stream.read(_MAX_BYTES + 1)
        if len(payload) > _MAX_BYTES:
            raise PruneError(f"Oversized metadata: {name}")
        return payload


def read_marker(descriptor: int) -> PruneMarker | None:
    try:
        payload = read_regular(descriptor, PRUNE_MARKER_NAME)
    except FileNotFoundError:
        return None
    try:
        return PruneMarker.model_validate_json(payload)
    except ValueError as error:
        raise PruneError(f"Invalid prune marker: {error}") from error


def validate_entries(descriptor: int, marker: PruneMarker, *, pending: bool) -> None:
    try:
        _validate_entries(descriptor, marker, pending=pending)
    except ValueError as error:
        raise PruneError(f"Invalid remaining backup metadata: {error}") from error


def _validate_entries(descriptor: int, marker: PruneMarker, *, pending: bool) -> None:
    expected = set(marker.artifacts)
    if pending:
        expected.add(PRUNE_MARKER_NAME)
    names = set(os.listdir(descriptor))
    if names - expected or (not pending and names != expected):
        raise PruneError("Unexpected or missing backup artifacts")
    for name in names:
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PruneError(f"Backup artifact is not a single-link regular file: {name}")
    manifest_name = f"{marker.base_name}.json"
    if (
        manifest_name in names
        and BackupManifest.model_validate_json(read_regular(descriptor, manifest_name))
        != marker.manifest
    ):
        raise PruneError("Manifest changed since deletion intent")
    if (
        SUSPECT_MARKER_NAME in names
        and SuspectMarker.model_validate_json(read_regular(descriptor, SUSPECT_MARKER_NAME))
        != marker.suspect
    ):
        raise PruneError("Suspect marker changed since deletion intent")
    if pending and read_marker(descriptor) != marker:
        raise PruneError("Prune marker changed since discovery")


def publish_noreplace(source_fd: int, name: str, target_fd: int) -> None:
    """Linux atomic file publication, deliberately without a weaker fallback."""
    library = ctypes.CDLL(None, use_errno=True)
    try:
        rename = library.renameat2
    except AttributeError as error:
        raise OSError(errno.ENOSYS, "Atomic no-replace publication unavailable") from error
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(name), target_fd, os.fsencode(PRUNE_MARKER_NAME), 1):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), PRUNE_MARKER_NAME)


def publish_marker(parent: int, descriptor: int, marker: PruneMarker) -> None:
    """Prepare outside the committed backup, then publish and flush before deletion."""
    with suppress(FileExistsError):
        os.mkdir(TEMP_DIRECTORY_NAME, mode=0o700, dir_fd=parent)
    temporary = os.open(TEMP_DIRECTORY_NAME, _FLAGS, dir_fd=parent)
    try:
        info = os.fstat(temporary)
        if info.st_dev != os.fstat(descriptor).st_dev:
            raise PruneError("Prune temporary location must share the backup filesystem")
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PruneError("Prune temporary directory must be private and owned by this user")
        name = uuid.uuid4().hex
        payload = (json.dumps(marker.model_dump(mode="json"), sort_keys=True) + "\n").encode()
        if len(payload) > _MAX_BYTES:
            raise PruneError("Prune marker exceeds metadata size limit")
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=temporary
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        check_directory(parent, descriptor, marker.directory_name)
        validate_entries(descriptor, marker, pending=False)
        publish_noreplace(temporary, name, descriptor)
        os.fsync(descriptor)
        os.fsync(temporary)
        os.fsync(parent)
    finally:
        os.close(temporary)


def remove_backup(directory: Path, marker: PruneMarker, *, pending: bool) -> None:
    """One attempt, exact allowlist only; leave durable intent on any interruption."""
    operation = "open backup directory"
    failing = directory
    removed = False
    try:
        if held_destination_filesystem(directory.parent) is None:
            raise PruneError(f"Destination lock required for cleanup: {directory.parent}")
        with opened_directory(directory) as (parent, descriptor):
            operation = "validate backup entries"
            validate_entries(descriptor, marker, pending=pending)
            if not pending:
                operation = "publish durable prune marker"
                failing = directory / PRUNE_MARKER_NAME
                publish_marker(parent, descriptor, marker)
            else:
                # A previous publication may have stopped at a failed directory
                # flush. Existence alone does not establish durable intent.
                operation = "flush pending deletion intent"
                failing = directory / PRUNE_MARKER_NAME
                fd = os.open(
                    PRUNE_MARKER_NAME,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=descriptor,
                )
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise PruneError("Prune marker is no longer a regular file")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.fsync(descriptor)
                os.fsync(parent)
            for name in marker.artifacts:
                operation = "validate remaining entries"
                failing = directory / name
                check_directory(parent, descriptor, directory.name)
                validate_entries(descriptor, marker, pending=True)
                try:
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                operation = "unlink artifact"
                os.unlink(name, dir_fd=descriptor)
            operation = "flush artifact removals"
            os.fsync(descriptor)
            validate_entries(descriptor, marker, pending=True)
            if set(os.listdir(descriptor)) != {PRUNE_MARKER_NAME}:
                raise PruneError("Artifacts appeared during cleanup")
            operation = "unlink prune marker"
            failing = directory / PRUNE_MARKER_NAME
            os.unlink(PRUNE_MARKER_NAME, dir_fd=descriptor)
            operation = "flush marker removal"
            os.fsync(descriptor)
            check_directory(parent, descriptor, directory.name)
            operation = "remove empty backup directory"
            failing = directory
            os.rmdir(directory.name, dir_fd=parent)
            removed = True
            operation = "flush destination directory"
            failing = directory.parent
            os.fsync(parent)
    except (OSError, ValueError, PruneError) as error:
        raise PruneError(
            f"Cannot {operation} at {failing}: {error}",
            removed=(str(directory),) if removed else (),
        ) from error


def remove_orphan(directory: Path) -> None:
    try:
        if held_destination_filesystem(directory.parent) is None:
            raise PruneError("Destination lock required for empty orphan cleanup")
        with opened_directory(directory) as (parent, descriptor):
            if os.listdir(descriptor):
                raise PruneError("Empty orphan is no longer empty")
            check_directory(parent, descriptor, directory.name)
            os.rmdir(directory.name, dir_fd=parent)
            try:
                os.fsync(parent)
            except OSError as error:
                raise PruneError(str(error), removed=(str(directory),)) from error
    except OSError as error:
        raise PruneError(f"Cannot remove empty orphan {directory}: {error}") from error
