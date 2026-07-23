"""Archive SHA-256 sidecar creation and verification."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from vaultkeep.errors import ArchiveCreationError, ArchiveVerificationError

_CHUNK_SIZE = 1024 * 1024


def calculate_archive_sha256(path: Path) -> str:
    """Stream one archive and return its lowercase SHA-256 digest."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_CHUNK_SIZE):
                digest.update(chunk)
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


def verify_checksum_sidecar(archive_path: Path, checksum_path: Path) -> str:
    """Strictly validate the checksum sidecar and archive content."""
    try:
        payload = checksum_path.read_bytes()
    except OSError as error:
        raise ArchiveVerificationError(
            f"Cannot read archive checksum {checksum_path}: {error}"
        ) from error
    prefix = b"  " + os.fsencode(archive_path.name) + b"\n"
    if len(payload) != 64 + len(prefix) or payload[64:] != prefix:
        raise ArchiveVerificationError("Archive checksum sidecar has invalid format")
    try:
        declared = payload[:64].decode("ascii")
    except UnicodeDecodeError as error:
        raise ArchiveVerificationError("Archive checksum is not ASCII") from error
    if any(character not in "0123456789abcdef" for character in declared):
        raise ArchiveVerificationError("Archive checksum is not lowercase hexadecimal")
    try:
        actual = calculate_archive_sha256(archive_path)
    except ArchiveCreationError as error:
        raise ArchiveVerificationError(f"Cannot verify archive checksum: {error}") from error
    if declared != actual:
        raise ArchiveVerificationError("Archive checksum does not match archive content")
    return actual
