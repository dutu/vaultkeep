"""V1 archive creation, verification, checksum, and credential APIs."""

from vaultkeep.archive.base import (
    ArchiveArtifact,
    ArchiveBuildRequest,
    ArchiveFormat,
    ArchiveTools,
)
from vaultkeep.archive.builder import build_archive
from vaultkeep.archive.checksums import (
    calculate_archive_sha256,
    verify_checksum_sidecar,
    write_checksum_sidecar,
)
from vaultkeep.archive.passwords import LoadedPassword, PasswordSecret, load_password_file
from vaultkeep.archive.tar_7z import (
    estimate_inner_tar_size,
    private_inner_tar_path,
    validate_private_capacity,
)
from vaultkeep.archive.verification import (
    encrypted_backup_credential_verifier,
    test_7z_password,
    verify_tar_7z,
    verify_tar_zstd,
)

__all__ = [
    "ArchiveArtifact",
    "ArchiveBuildRequest",
    "ArchiveFormat",
    "ArchiveTools",
    "LoadedPassword",
    "PasswordSecret",
    "build_archive",
    "calculate_archive_sha256",
    "encrypted_backup_credential_verifier",
    "estimate_inner_tar_size",
    "load_password_file",
    "private_inner_tar_path",
    "test_7z_password",
    "validate_private_capacity",
    "verify_checksum_sidecar",
    "verify_tar_7z",
    "verify_tar_zstd",
    "write_checksum_sidecar",
]
