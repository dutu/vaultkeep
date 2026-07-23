"""Secure v1 password-file loading and best-effort secret cleanup."""

from __future__ import annotations

import importlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from vaultkeep.errors import PasswordFileError
from vaultkeep.state.models import CredentialFingerprint

_MAX_PASSWORD_FILE_SIZE = 64 * 1024


class PasswordSecret:
    """Mutable in-process passphrase buffer with redacted representation."""

    __slots__ = ("_value",)

    def __init__(self, value: bytes) -> None:
        if not value:
            raise PasswordFileError("Password secret cannot be empty")
        if b"\0" in value or b"\r" in value or b"\n" in value:
            raise PasswordFileError(
                "Password secret cannot contain null, carriage-return, or newline bytes"
            )
        try:
            value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise PasswordFileError("Password secret must be valid UTF-8") from error
        self._value = bytearray(value)

    def __enter__(self) -> PasswordSecret:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.clear()

    def __repr__(self) -> str:
        return "PasswordSecret(<redacted>)"

    def pipe_input(self) -> bytes:
        """Create the one-line input expected by Debian's 7z prompt."""
        if not self._value:
            raise PasswordFileError("Password secret has already been cleared")
        return bytes(self._value) + b"\n"

    def clear(self) -> None:
        """Overwrite the owned mutable buffer and release it."""
        for index in range(len(self._value)):
            self._value[index] = 0
        self._value.clear()


@dataclass(frozen=True, slots=True)
class LoadedPassword:
    """Validated secret and its non-secret local generation fingerprint."""

    secret: PasswordSecret
    fingerprint: CredentialFingerprint


def load_password_file(path: Path) -> LoadedPassword:
    """Validate and read one root-owned Debian password file without following links."""
    if os.name != "posix":
        raise PasswordFileError("Password files are supported only on Debian/POSIX systems")
    if not path.is_absolute():
        raise PasswordFileError("Password file path must be absolute")
    _disable_core_dumps()
    initial = _validate_password_path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PasswordFileError(f"Cannot securely open password file {path}: {error}") from error
    try:
        opened = os.fstat(descriptor)
        _require_same_file(initial, opened, path)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            raw = stream.read(_MAX_PASSWORD_FILE_SIZE + 1)
            final = os.fstat(stream.fileno())
        descriptor = -1
    except OSError as error:
        raise PasswordFileError(f"Cannot read password file {path}: {error}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _require_same_file(initial, final, path)
    passphrase = _validate_passphrase(raw)
    return LoadedPassword(
        secret=PasswordSecret(passphrase),
        fingerprint=CredentialFingerprint(
            device=final.st_dev,
            inode=final.st_ino,
            size=final.st_size,
            mtime_ns=final.st_mtime_ns,
            ctime_ns=final.st_ctime_ns,
        ),
    )


def _disable_core_dumps() -> None:
    try:
        resource = importlib.import_module("resource")
        core_limit = resource.RLIMIT_CORE
        _, hard_limit = resource.getrlimit(core_limit)
        resource.setrlimit(core_limit, (0, hard_limit))
    except (ImportError, OSError, ValueError) as error:
        raise PasswordFileError(
            "Cannot disable process core dumps before reading password"
        ) from error


def _validate_password_path(path: Path) -> os.stat_result:
    try:
        status = path.lstat()
    except OSError as error:
        raise PasswordFileError(f"Cannot inspect password file {path}: {error}") from error
    if not stat.S_ISREG(status.st_mode):
        raise PasswordFileError("Password file must be a regular file, not a symbolic link")
    if status.st_uid != 0 or status.st_gid != 0:
        raise PasswordFileError("Password file must be owned by root:root")
    if stat.S_IMODE(status.st_mode) != 0o600:
        raise PasswordFileError("Password file mode must be exactly 0600")

    parent = path.parent
    while True:
        try:
            parent_status = parent.lstat()
        except OSError as error:
            raise PasswordFileError(
                f"Cannot inspect password-file parent {parent}: {error}"
            ) from error
        if not stat.S_ISDIR(parent_status.st_mode):
            raise PasswordFileError(f"Password-file parent is not a directory: {parent}")
        if stat.S_IMODE(parent_status.st_mode) & 0o022:
            raise PasswordFileError(
                f"Password-file parent is writable by group or other users: {parent}"
            )
        if parent == parent.parent:
            break
        parent = parent.parent
    return status


def _require_same_file(
    expected: os.stat_result,
    actual: os.stat_result,
    path: Path,
) -> None:
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(expected, field) != getattr(actual, field) for field in fields):
        raise PasswordFileError(f"Password file changed while being read: {path}")


def _validate_passphrase(raw: bytes) -> bytes:
    if len(raw) > _MAX_PASSWORD_FILE_SIZE:
        raise PasswordFileError("Password file exceeds the 64 KiB limit")
    passphrase = raw[:-1] if raw.endswith(b"\n") else raw
    if not passphrase:
        raise PasswordFileError("Passphrase must not be empty")
    if b"\0" in passphrase or b"\r" in passphrase or b"\n" in passphrase:
        raise PasswordFileError(
            "Passphrase must not contain null, carriage-return, or embedded newline bytes"
        )
    try:
        passphrase.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise PasswordFileError("Passphrase must be valid UTF-8") from error
    return passphrase
