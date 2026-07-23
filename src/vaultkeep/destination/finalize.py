"""No-overwrite atomic destination-directory commit."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from pathlib import Path

from vaultkeep.errors import (
    DestinationCommitDurabilityError,
    DestinationFinalizeError,
)

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


def finalize_backup_directory(staging_path: Path, final_path: Path) -> None:
    """Flush staging and atomically rename it without replacing any final path."""
    if staging_path.parent.absolute() != final_path.parent.absolute():
        raise DestinationFinalizeError(
            "Staging and final backup directories must share one destination parent"
        )
    if not staging_path.name.startswith(".partial-vaultkeep-"):
        raise DestinationFinalizeError(
            "Staging directory name must use the hidden .partial-vaultkeep- prefix"
        )
    _validate_staging_directory(staging_path)
    _flush_staging_files(staging_path)
    _fsync_directory(staging_path)
    try:
        _rename_noreplace(staging_path, final_path)
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise DestinationFinalizeError(
                f"Final backup path already exists: {final_path}"
            ) from error
        raise DestinationFinalizeError(
            f"Cannot atomically finalize backup {final_path}: {error}"
        ) from error
    try:
        _fsync_directory(final_path.parent)
    except OSError as error:
        raise DestinationCommitDurabilityError(
            f"Backup committed at {final_path}, but destination flush failed: {error}"
        ) from error


def _validate_staging_directory(path: Path) -> None:
    try:
        status = path.lstat()
    except OSError as error:
        raise DestinationFinalizeError(
            f"Cannot inspect staging directory {path}: {error}"
        ) from error
    if not stat.S_ISDIR(status.st_mode):
        raise DestinationFinalizeError(f"Staging path is not a directory: {path}")


def _flush_staging_files(directory: Path) -> None:
    try:
        children = tuple(directory.iterdir())
    except OSError as error:
        raise DestinationFinalizeError(
            f"Cannot inspect staging contents {directory}: {error}"
        ) from error
    for child in children:
        try:
            status = child.lstat()
            if not stat.S_ISREG(status.st_mode):
                raise DestinationFinalizeError(f"Staging contains a non-regular entry: {child}")
            with child.open("rb+") as stream:
                os.fsync(stream.fileno())
        except DestinationFinalizeError:
            raise
        except OSError as error:
            raise DestinationFinalizeError(
                f"Cannot flush staging artifact {child}: {error}"
            ) from error


def _rename_noreplace(source: Path, destination: Path) -> None:
    if sys.platform == "linux":
        library = ctypes.CDLL(None, use_errno=True)
        try:
            renameat2 = library.renameat2
        except AttributeError as error:
            raise OSError(errno.ENOSYS, "libc does not expose renameat2") from error
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        result = renameat2(
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), destination)
        return

    # Non-Linux execution exists only for portable unit tests. Debian uses renameat2.
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), destination)
    os.rename(source, destination)


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
