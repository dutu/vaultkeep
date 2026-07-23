"""GNU TAR plus Zstandard archive creation."""

from __future__ import annotations

import os
from pathlib import Path

from vaultkeep.archive.base import ArchiveTools
from vaultkeep.archive.process import run_command, run_pipeline
from vaultkeep.archive.tar_input import (
    snapshot_uses_followed_symlinks,
    tar_member_input,
)
from vaultkeep.errors import ArchiveCreationError
from vaultkeep.sources.entries import SourceSnapshot


def create_tar_zstd(
    snapshot: SourceSnapshot,
    output_path: Path,
    *,
    compression_level: int,
    tools: ArchiveTools,
) -> None:
    """Stream the immutable source list through GNU TAR and Zstandard."""
    if not 1 <= compression_level <= 19:
        raise ValueError("tar.zst compression level must be between 1 and 19")
    _require_new_output_path(output_path)
    tar_command = _tar_create_command(
        tools.tar,
        archive_path=None,
        dereference=snapshot_uses_followed_symlinks(snapshot),
    )
    zstd_command = (
        tools.zstd,
        "--quiet",
        "--threads=0",
        f"-{compression_level}",
        "--output",
        output_path,
    )
    try:
        run_pipeline(
            tar_command,
            zstd_command,
            producer_input=tar_member_input(snapshot),
        )
        _flush_nonempty_file(output_path)
    except BaseException as error:
        try:
            _remove_partial(output_path)
        except ArchiveCreationError as cleanup_error:
            raise cleanup_error from error
        raise


def create_inner_tar(
    snapshot: SourceSnapshot,
    output_path: Path,
    *,
    tools: ArchiveTools,
) -> None:
    """Create the private uncompressed GNU TAR used by tar.7z."""
    _require_new_output_path(output_path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = -1
    try:
        descriptor = os.open(output_path, flags, 0o600)
        os.close(descriptor)
        descriptor = -1
        command = _tar_create_command(
            tools.tar,
            archive_path=output_path,
            dereference=snapshot_uses_followed_symlinks(snapshot),
        )
        run_command(command, input_data=tar_member_input(snapshot))
        os.chmod(output_path, 0o600)
        _flush_nonempty_file(output_path)
    except BaseException as error:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            _remove_partial(output_path)
        except ArchiveCreationError as cleanup_error:
            raise cleanup_error from error
        raise


def _tar_create_command(
    tar_path: Path,
    *,
    archive_path: Path | None,
    dereference: bool,
) -> tuple[str | Path, ...]:
    command: list[str | Path] = [
        tar_path,
        "--format=gnu",
        "--create",
        f"--file={archive_path}" if archive_path is not None else "--file=-",
        "--directory=/",
        "--null",
        "--verbatim-files-from",
        "--no-recursion",
        "--sparse",
    ]
    if dereference:
        command.append("--dereference")
    command.append("--files-from=-")
    return tuple(command)


def _require_new_output_path(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ArchiveCreationError(f"Cannot inspect archive output path {path}: {error}") from error
    raise ArchiveCreationError(f"Archive output path already exists: {path}")


def _flush_nonempty_file(path: Path) -> None:
    try:
        with path.open("rb+") as stream:
            if os.fstat(stream.fileno()).st_size <= 0:
                raise ArchiveCreationError(f"Archive tool created an empty file: {path}")
            os.fsync(stream.fileno())
    except ArchiveCreationError:
        raise
    except OSError as error:
        raise ArchiveCreationError(f"Cannot flush archive output {path}: {error}") from error


def _remove_partial(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ArchiveCreationError(f"Cannot remove partial archive {path}: {error}") from error
