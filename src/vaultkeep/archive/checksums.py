"""Archive SHA-256 sidecar creation and verification."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO

from vaultkeep.errors import (
    ArchiveChecksumMismatchError,
    ArchiveCreationError,
    ArchiveVerificationError,
)
from vaultkeep.progress import VerificationProgress, report_progress

_CHUNK_SIZE = 1024 * 1024


@contextmanager
def _open_regular_file(path: Path) -> Iterator[BinaryIO]:
    """Pin the artifact directory and reject links and special files before reading."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError(errno.EINVAL, "Checksum artifact is not a regular file", str(path))
            yield stream
    finally:
        os.close(directory)


def calculate_archive_sha256(
    path: Path, *, on_progress: Callable[[VerificationProgress], None] | None = None
) -> str:
    """Stream one archive and return its lowercase SHA-256 digest."""
    digest = hashlib.sha256()
    try:
        with _open_regular_file(path) as stream:
            total = None
            if on_progress is not None:
                with suppress(OSError):
                    total = os.fstat(stream.fileno()).st_size
            processed = 0

            def report() -> None:
                if on_progress is not None:
                    report_progress(
                        on_progress,
                        VerificationProgress(
                            "backup",
                            "SHA-256",
                            path=path,
                            bytes_processed=processed,
                            bytes_total=total,
                        ),
                    )

            report()
            while chunk := stream.read(_CHUNK_SIZE):
                digest.update(chunk)
                processed += len(chunk)
                report()
    except OSError as error:
        raise ArchiveCreationError(f"Cannot calculate archive checksum {path}: {error}") from error
    return digest.hexdigest()


def write_checksum_sidecar(archive_path: Path, checksum_path: Path, digest: str) -> None:
    """Write one exclusive, flushed checksum sidecar."""
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Archive SHA-256 must contain 64 lowercase hexadecimal characters")
    payload = digest.encode("ascii") + b"  " + os.fsencode(archive_path.name) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = -1
    created = False
    try:
        descriptor = os.open(checksum_path, flags, 0o644)
        created = True
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            try:
                checksum_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        raise ArchiveCreationError(
            f"Cannot write archive checksum {checksum_path}: {error}"
        ) from error


def read_checksum_sidecar(archive_path: Path, checksum_path: Path) -> str:
    """Validate the sidecar format and return its digest without reading the archive."""
    prefix = b"  " + os.fsencode(archive_path.name) + b"\n"
    try:
        with _open_regular_file(checksum_path) as stream:
            payload = stream.read(64 + len(prefix) + 1)
    except OSError as error:
        raise ArchiveVerificationError(
            f"Cannot read archive checksum {checksum_path}: {error}"
        ) from error
    if len(payload) != 64 + len(prefix) or payload[64:] != prefix:
        raise ArchiveVerificationError("Archive checksum sidecar has invalid format")
    try:
        declared = payload[:64].decode("ascii")
    except UnicodeDecodeError as error:
        raise ArchiveVerificationError("Archive checksum is not ASCII") from error
    if any(character not in "0123456789abcdef" for character in declared):
        raise ArchiveVerificationError("Archive checksum is not lowercase hexadecimal")
    return declared


def verify_checksum_sidecar(
    archive_path: Path,
    checksum_path: Path,
    *,
    on_progress: Callable[[VerificationProgress], None] | None = None,
) -> str:
    """Strictly validate the checksum sidecar and archive content."""
    declared = read_checksum_sidecar(archive_path, checksum_path)
    try:
        actual = (
            calculate_archive_sha256(archive_path, on_progress=on_progress)
            if on_progress is not None
            else calculate_archive_sha256(archive_path)
        )
    except ArchiveCreationError as error:
        raise ArchiveVerificationError(f"Cannot verify archive checksum: {error}") from error
    report_progress(
        on_progress,
        VerificationProgress(
            "backup",
            "SHA-256",
            path=archive_path,
            detail=f"Sidecar SHA-256: {declared}; calculated SHA-256: {actual}",
        ),
    )
    if declared != actual:
        raise ArchiveChecksumMismatchError("Archive checksum does not match archive content")
    return actual
