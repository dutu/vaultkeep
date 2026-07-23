"""Format-independent MS4 archive build and verification workflow."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from vaultkeep.archive.base import ArchiveArtifact, ArchiveBuildRequest
from vaultkeep.archive.checksums import (
    calculate_archive_sha256,
    verify_checksum_sidecar,
    write_checksum_sidecar,
)
from vaultkeep.archive.passwords import PasswordSecret
from vaultkeep.archive.tar_7z import (
    cleanup_private_inner_tar,
    create_private_inner_tar,
    encrypt_inner_tar,
    prepare_private_workspace,
    private_inner_tar_path,
)
from vaultkeep.archive.tar_zstd import create_tar_zstd
from vaultkeep.archive.verification import verify_tar_7z, verify_tar_zstd
from vaultkeep.errors import (
    ArchiveCreationError,
    ArchiveVerificationError,
    PlaintextCleanupError,
)
from vaultkeep.sources.hashing import calculate_source_digest

SourceDigestCalculator = Callable[..., str]


def build_archive(
    request: ArchiveBuildRequest,
    *,
    password: PasswordSecret | None = None,
    source_digest_calculator: SourceDigestCalculator = calculate_source_digest,
) -> ArchiveArtifact:
    """Build, consistency-check, verify, checksum, and flush one archive."""
    _validate_request(request, password)
    inner_tar: Path | None = None
    archive_created = False
    checksum_created = False
    try:
        if request.archive_format == "tar.zst":
            create_tar_zstd(
                request.snapshot,
                request.archive_path,
                compression_level=request.compression_level,
                tools=request.tools,
            )
            archive_created = True
            source_digest = source_digest_calculator(request.snapshot)
            _require_source_consistency(request.expected_source_digest, source_digest)
            verify_tar_zstd(request.archive_path, request.snapshot, tools=request.tools)
        else:
            inner_tar = private_inner_tar_path(
                temp_root=request.local_temp_root,
                job_id=request.job_id,
                job_identity_hash=request.job_identity_hash,
                backup_id=request.backup_id,
            )
            prepare_private_workspace(inner_tar)
            create_private_inner_tar(request.snapshot, inner_tar, tools=request.tools)
            source_digest = source_digest_calculator(request.snapshot)
            _require_source_consistency(request.expected_source_digest, source_digest)
            if password is None:
                raise AssertionError("validated tar.7z request has no password")
            encrypt_inner_tar(
                inner_tar,
                request.archive_path,
                compression_level=request.compression_level,
                password=password,
                tools=request.tools,
            )
            archive_created = True
            verify_tar_7z(
                request.archive_path,
                request.snapshot,
                job_id=request.job_id,
                password=password,
                tools=request.tools,
            )
            cleanup_private_inner_tar(inner_tar)
            inner_tar = None

        digest = calculate_archive_sha256(request.archive_path)
        write_checksum_sidecar(request.archive_path, request.checksum_path, digest)
        checksum_created = True
        verified_digest = verify_checksum_sidecar(request.archive_path, request.checksum_path)
        try:
            size = request.archive_path.stat().st_size
        except OSError as error:
            raise ArchiveCreationError(
                f"Cannot inspect completed archive {request.archive_path}: {error}"
            ) from error
        return ArchiveArtifact(
            archive_format=request.archive_format,
            archive_path=request.archive_path,
            checksum_path=request.checksum_path,
            sha256=verified_digest,
            size=size,
            source_digest=source_digest,
        )
    except BaseException as error:
        cleanup_error = _cleanup_failed_build(
            request,
            inner_tar,
            archive_created=archive_created,
            checksum_created=checksum_created,
        )
        if cleanup_error is not None:
            raise cleanup_error from error
        raise


def _validate_request(
    request: ArchiveBuildRequest,
    password: PasswordSecret | None,
) -> None:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", request.expected_source_digest) is None:
        raise ValueError("Expected source digest must use canonical SHA-256 format")
    if request.archive_path.parent != request.checksum_path.parent:
        raise ValueError("Archive and checksum must use the same staging directory")
    if request.checksum_path.name != request.archive_path.name + ".sha256":
        raise ValueError("Checksum name must equal archive basename plus .sha256")
    if request.archive_format == "tar.zst":
        if password is not None:
            raise ValueError("tar.zst does not accept a password")
        if not 1 <= request.compression_level <= 19:
            raise ValueError("tar.zst compression level must be between 1 and 19")
        if request.archive_path.name.endswith(".tar.zst") is False:
            raise ValueError("tar.zst archive path must end in .tar.zst")
    else:
        if password is None:
            raise ValueError("tar.7z requires a password")
        if not 1 <= request.compression_level <= 9:
            raise ValueError("tar.7z compression level must be between 1 and 9")
        if request.archive_path.name.endswith(".tar.7z") is False:
            raise ValueError("tar.7z archive path must end in .tar.7z")


def _require_source_consistency(expected: str, actual: str) -> None:
    if actual != expected:
        raise ArchiveVerificationError("Source digest changed during archive creation")


def _cleanup_failed_build(
    request: ArchiveBuildRequest,
    inner_tar: Path | None,
    *,
    archive_created: bool,
    checksum_created: bool,
) -> PlaintextCleanupError | ArchiveCreationError | None:
    cleanup_errors: list[str] = []
    plaintext_cleanup_failed = False
    if inner_tar is not None:
        try:
            cleanup_private_inner_tar(inner_tar)
        except PlaintextCleanupError as error:
            cleanup_errors.append(str(error))
            plaintext_cleanup_failed = True
    owned_paths = (
        (request.checksum_path, checksum_created),
        (request.archive_path, archive_created),
    )
    for path, owned in owned_paths:
        if not owned:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            cleanup_errors.append(f"Cannot remove partial artifact {path}: {error}")
    if not cleanup_errors:
        return None
    message = "; ".join(cleanup_errors)
    if plaintext_cleanup_failed:
        return PlaintextCleanupError(message)
    return ArchiveCreationError(message)
