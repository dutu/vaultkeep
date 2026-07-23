"""Private inner-TAR lifecycle and password-protected 7-Zip creation."""

from __future__ import annotations

import math
import os
import re
import shutil
import stat
from pathlib import Path

from vaultkeep.archive.base import ArchiveTools
from vaultkeep.archive.passwords import PasswordSecret
from vaultkeep.archive.process import run_command
from vaultkeep.archive.tar_zstd import create_inner_tar
from vaultkeep.errors import ArchiveCreationError, PlaintextCleanupError
from vaultkeep.sources.entries import SourceEntryType, SourceSnapshot

_JOB_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_IDENTITY_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_BACKUP_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_TAR_BLOCK = 512
_TAR_RECORD = 10 * 1024
_MINIMUM_HEADROOM = 64 * 1024 * 1024


def private_inner_tar_path(
    *,
    temp_root: Path,
    job_id: str,
    job_identity_hash: str,
    backup_id: str,
) -> Path:
    """Derive the exact private plaintext-TAR path."""
    if _JOB_PATTERN.fullmatch(job_id) is None:
        raise ValueError("Invalid job ID for private TAR path")
    if _IDENTITY_PATTERN.fullmatch(job_identity_hash) is None:
        raise ValueError("Invalid job identity hash for private TAR path")
    if _BACKUP_PATTERN.fullmatch(backup_id) is None:
        raise ValueError("Invalid backup ID for private TAR path")
    return temp_root / f"{job_id}-{job_identity_hash}" / backup_id / f"{job_id}.tar"


def prepare_private_workspace(inner_tar_path: Path) -> None:
    """Create and validate the root-owned 0700 private workspace."""
    if os.name != "posix":
        raise ArchiveCreationError("Password-protected archives require Debian/POSIX")
    job_directory = inner_tar_path.parent.parent
    backup_directory = inner_tar_path.parent
    _validate_private_directory(job_directory.parent)
    _create_or_validate_directory(job_directory)
    try:
        backup_directory.mkdir(mode=0o700)
    except FileExistsError as error:
        raise ArchiveCreationError(
            f"Private backup workspace already exists: {backup_directory}"
        ) from error
    except OSError as error:
        raise ArchiveCreationError(
            f"Cannot create private backup workspace {backup_directory}: {error}"
        ) from error
    _validate_private_directory(backup_directory)


def estimate_inner_tar_size(snapshot: SourceSnapshot) -> int:
    """Return a conservative logical-space estimate for a GNU TAR."""
    size = 2 * _TAR_BLOCK
    for entry in snapshot.entries:
        size += _TAR_BLOCK
        if len(entry.raw_archive_path) > 100:
            size += _TAR_BLOCK + _round_tar_block(len(entry.raw_archive_path) + 1)
        if (
            entry.entry_type is SourceEntryType.SYMLINK
            and entry.link_target is not None
            and len(os.fsencode(entry.link_target)) > 100
        ):
            size += _TAR_BLOCK + _round_tar_block(len(os.fsencode(entry.link_target)) + 1)
        if entry.entry_type is SourceEntryType.FILE:
            size += _round_tar_block(entry.size)
    return math.ceil(size / _TAR_RECORD) * _TAR_RECORD


def validate_private_capacity(snapshot: SourceSnapshot, workspace: Path) -> int:
    """Require estimated TAR space plus the fixed v1 safety margin."""
    estimate = estimate_inner_tar_size(snapshot)
    required = estimate + max(_MINIMUM_HEADROOM, math.ceil(estimate * 0.10))
    try:
        available = shutil.disk_usage(workspace).free
    except OSError as error:
        raise ArchiveCreationError(
            f"Cannot inspect private workspace capacity {workspace}: {error}"
        ) from error
    if available < required:
        raise ArchiveCreationError(
            f"Insufficient private TAR space: need {required} bytes, have {available} bytes"
        )
    return required


def create_private_inner_tar(
    snapshot: SourceSnapshot,
    inner_tar_path: Path,
    *,
    tools: ArchiveTools,
) -> None:
    """Capacity-check and create the mode-0600 private GNU TAR."""
    validate_private_capacity(snapshot, inner_tar_path.parent)
    create_inner_tar(snapshot, inner_tar_path, tools=tools)


def encrypt_inner_tar(
    inner_tar_path: Path,
    output_path: Path,
    *,
    compression_level: int,
    password: PasswordSecret,
    tools: ArchiveTools,
) -> None:
    """Encrypt one completed inner TAR with AES and encrypted headers."""
    if not 1 <= compression_level <= 9:
        raise ValueError("tar.7z compression level must be between 1 and 9")
    try:
        output_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ArchiveCreationError(
            f"Cannot inspect archive output {output_path}: {error}"
        ) from error
    else:
        raise ArchiveCreationError(f"Archive output path already exists: {output_path}")

    command = (
        tools.seven_zip,
        "a",
        "-t7z",
        "-mhe=on",
        "-sccUTF-8",
        "-bd",
        f"-mx={compression_level}",
        "-p",
        "--",
        output_path,
        inner_tar_path.name,
    )
    try:
        run_command(
            command,
            input_data=password.pipe_input(),
            cwd=inner_tar_path.parent,
            sensitive_input=True,
        )
        with output_path.open("rb+") as stream:
            status = os.fstat(stream.fileno())
            if status.st_size <= 0:
                raise ArchiveCreationError(f"7z created an empty archive: {output_path}")
            os.fsync(stream.fileno())
    except BaseException as error:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            raise ArchiveCreationError(
                f"Cannot remove partial encrypted archive {output_path}: {cleanup_error}"
            ) from error
        raise


def cleanup_private_inner_tar(inner_tar_path: Path) -> None:
    """Remove plaintext TAR and its per-backup workspace before commit."""
    try:
        inner_tar_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise PlaintextCleanupError(
            f"Cannot remove private plaintext TAR {inner_tar_path}: {error}"
        ) from error
    try:
        inner_tar_path.parent.rmdir()
    except FileNotFoundError:
        return
    except OSError as error:
        raise PlaintextCleanupError(
            f"Cannot remove private plaintext workspace {inner_tar_path.parent}: {error}"
        ) from error


def _create_or_validate_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise ArchiveCreationError(f"Cannot create private directory {path}: {error}") from error
    _validate_private_directory(path)


def _validate_private_directory(path: Path) -> None:
    try:
        status = path.lstat()
    except OSError as error:
        raise ArchiveCreationError(f"Cannot inspect private directory {path}: {error}") from error
    if not stat.S_ISDIR(status.st_mode):
        raise ArchiveCreationError(f"Private path is not a directory: {path}")
    if status.st_uid != 0 or status.st_gid != 0 or stat.S_IMODE(status.st_mode) != 0o700:
        raise ArchiveCreationError(f"Private directory must be root:root mode 0700: {path}")


def _round_tar_block(size: int) -> int:
    return math.ceil(size / _TAR_BLOCK) * _TAR_BLOCK
