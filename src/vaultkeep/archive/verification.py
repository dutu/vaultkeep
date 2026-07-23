"""Archive stream, member-structure, and credential verification."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from vaultkeep.archive.base import ArchiveTools
from vaultkeep.archive.passwords import PasswordSecret
from vaultkeep.archive.process import run_command, run_pipeline
from vaultkeep.archive.tar_input import compare_tar_members, parse_gnu_tar_listing
from vaultkeep.errors import (
    ArchiveCreationError,
    ArchiveVerificationError,
)
from vaultkeep.sources.entries import SourceSnapshot
from vaultkeep.state.models import BackupStateRecord


def verify_tar_zstd(
    archive_path: Path,
    snapshot: SourceSnapshot,
    *,
    tools: ArchiveTools,
) -> None:
    """Verify the complete Zstandard stream and exact GNU TAR member set."""
    _require_nonempty_archive(archive_path)
    try:
        run_command((tools.zstd, "--test", "--quiet", archive_path))
        with tempfile.TemporaryFile() as listing:
            run_pipeline(
                (tools.zstd, "--decompress", "--stdout", "--quiet", archive_path),
                (
                    tools.tar,
                    "--list",
                    "--file=-",
                    "--quoting-style=escape",
                ),
                consumer_stdout=listing,
            )
            listing.seek(0)
            members = parse_gnu_tar_listing(listing.read())
        compare_tar_members(snapshot, members)
    except ArchiveVerificationError:
        raise
    except ArchiveCreationError as error:
        raise ArchiveVerificationError(f"tar.zst verification failed: {error}") from error


def verify_tar_7z(
    archive_path: Path,
    snapshot: SourceSnapshot,
    *,
    job_id: str,
    password: PasswordSecret,
    tools: ArchiveTools,
) -> None:
    """Verify encryption, outer membership, inner TAR stream, and source members."""
    _require_nonempty_archive(archive_path)
    inner_name = f"{job_id}.tar"
    password_input = password.terminal_input()
    try:
        run_command(
            (tools.seven_zip, "t", "-bd", "-sccUTF-8", "-p", "--", archive_path),
            terminal_input=password_input,
        )
        unauthenticated = run_command(
            (tools.seven_zip, "l", "-slt", "-ba", "-sccUTF-8", "--", archive_path),
            capture_stdout=True,
            allowed_returncodes=range(0, 256),
        )
        exposed = unauthenticated.stdout + b"\n" + unauthenticated.stderr
        if inner_name.encode("utf-8") in exposed:
            raise ArchiveVerificationError(
                "7z header encryption did not conceal the inner TAR name"
            )
        listing = run_command(
            (
                tools.seven_zip,
                "l",
                "-slt",
                "-ba",
                "-sccUTF-8",
                "-p",
                "--",
                archive_path,
            ),
            terminal_input=password_input,
            capture_stdout=True,
        )
        if _seven_zip_member_paths(listing.stdout) != (inner_name,):
            raise ArchiveVerificationError(
                f"7z archive must contain exactly one member named {inner_name}"
            )
        with tempfile.TemporaryFile() as tar_listing:
            run_pipeline(
                (
                    tools.seven_zip,
                    "x",
                    "-so",
                    "-bd",
                    "-sccUTF-8",
                    "-p",
                    "--",
                    archive_path,
                    inner_name,
                ),
                (
                    tools.tar,
                    "--list",
                    "--file=-",
                    "--quoting-style=escape",
                ),
                producer_terminal_input=password_input,
                consumer_stdout=tar_listing,
            )
            tar_listing.seek(0)
            members = parse_gnu_tar_listing(tar_listing.read())
        compare_tar_members(snapshot, members)
    except ArchiveVerificationError:
        raise
    except ArchiveCreationError as error:
        raise ArchiveVerificationError(f"tar.7z verification failed: {error}") from error


def test_7z_password(
    archive_path: Path,
    password: PasswordSecret,
    *,
    tools: ArchiveTools,
) -> bool:
    """Return whether the current password opens one complete encrypted archive."""
    try:
        run_command(
            (tools.seven_zip, "t", "-bd", "-sccUTF-8", "-p", "--", archive_path),
            terminal_input=password.terminal_input(),
        )
    except ArchiveCreationError:
        return False
    return True


def encrypted_backup_credential_verifier(
    password: PasswordSecret,
    *,
    tools: ArchiveTools,
) -> Callable[[BackupStateRecord], bool]:
    """Adapt real 7z password testing to the state-reconciliation callback."""

    def verify(record: BackupStateRecord) -> bool:
        if not record.encrypted:
            return False
        return test_7z_password(Path(record.backup_path), password, tools=tools)

    return verify


def _seven_zip_member_paths(output: bytes) -> tuple[str, ...]:
    try:
        text = output.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ArchiveVerificationError("7z listing is not valid UTF-8") from error
    return tuple(
        line.removeprefix("Path = ") for line in text.splitlines() if line.startswith("Path = ")
    )


def _require_nonempty_archive(path: Path) -> None:
    try:
        if path.stat().st_size <= 0:
            raise ArchiveVerificationError(f"Archive is empty: {path}")
    except ArchiveVerificationError:
        raise
    except OSError as error:
        raise ArchiveVerificationError(f"Cannot inspect archive {path}: {error}") from error
