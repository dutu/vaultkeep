"""Real GNU TAR, Zstandard, and Debian 7-Zip integration tests."""

from __future__ import annotations

import os
import stat
import sys
from contextlib import suppress
from copy import deepcopy
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.archive import ArchiveTools, PasswordSecret, verify_tar_7z, verify_tar_zstd
from vaultkeep.archive.process import run_pipeline
from vaultkeep.archive.tar_7z import (
    cleanup_private_inner_tar,
    encrypt_inner_tar,
)
from vaultkeep.archive.tar_zstd import create_inner_tar, create_tar_zstd
from vaultkeep.config import JobConfig
from vaultkeep.sources import SourceEntryType, SourceSnapshot, discover_sources

TOOLS = ArchiveTools()
REQUIRED_TOOLS = (TOOLS.tar, TOOLS.zstd, TOOLS.seven_zip)
HAS_LINUX_TOOLS = sys.platform == "linux" and all(path.is_file() for path in REQUIRED_TOOLS)

pytestmark = pytest.mark.skipif(
    not HAS_LINUX_TOOLS,
    reason="Real Linux archive tools are unavailable",
)


@cache
def _is_debian() -> bool:
    try:
        return any(
            line.strip() == "ID=debian"
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        return False


def _snapshot(
    tmp_path: Path,
    valid_config: dict[str, Any],
) -> tuple[Path, SourceSnapshot]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "empty").mkdir()
    (source / "regular.txt").write_text("content", encoding="utf-8")
    (source / "space and newline\n.txt").write_text("unusual", encoding="utf-8")
    with suppress(OSError):
        (source / "link").symlink_to("regular.txt")
    candidate = deepcopy(valid_config)
    candidate["sources"] = [{"path": str(source)}]
    candidate["exclude"] = []
    candidate["source_options"]["follow_symlinks"] = False
    config = JobConfig.model_validate(candidate)
    return source, discover_sources(config)


def _restore_tar_zstd(archive: Path, restore_root: Path) -> None:
    run_pipeline(
        (TOOLS.zstd, "-q", "-d", "-c", archive),
        (TOOLS.tar, "--extract", "--file=-", f"--directory={restore_root}"),
    )


def _restore_tar_7z(archive: Path, restore_root: Path, password: PasswordSecret) -> None:
    run_pipeline(
        (TOOLS.seven_zip, "x", "-so", "-bd", "-p", "--", archive),
        (TOOLS.tar, "--extract", "--file=-", f"--directory={restore_root}"),
        producer_terminal_input=password.terminal_input(confirm=False),
    )


def _assert_restored_snapshot(snapshot: SourceSnapshot, restore_root: Path) -> None:
    compare_owner = hasattr(os, "geteuid") and os.geteuid() == 0
    for entry in snapshot.entries:
        restored = restore_root / entry.archive_path
        status = restored.lstat()
        assert stat.S_IMODE(status.st_mode) == entry.mode
        if compare_owner:
            assert status.st_uid == entry.uid
            assert status.st_gid == entry.gid
        if entry.entry_type is SourceEntryType.DIRECTORY:
            assert restored.is_dir()
        elif entry.entry_type is SourceEntryType.SYMLINK:
            assert restored.is_symlink()
            assert os.readlink(restored) == entry.link_target
        else:
            assert restored.is_file()
            assert restored.read_bytes() == entry.absolute_path.read_bytes()
        if entry.entry_type is not SourceEntryType.SYMLINK:
            assert abs(status.st_mtime_ns - entry.mtime_ns) <= 1_000_000_000


def test_real_tar_zstd_creation_and_verification(
    tmp_path: Path,
    valid_config: dict[str, Any],
) -> None:
    _, snapshot = _snapshot(tmp_path, valid_config)
    archive = tmp_path / "backup.tar.zst"

    create_tar_zstd(
        snapshot,
        archive,
        compression_level=6,
        tools=TOOLS,
    )
    verify_tar_zstd(archive, snapshot, tools=TOOLS)

    assert archive.stat().st_size > 0


def test_real_tar_zstd_restore_drill(
    tmp_path: Path,
    valid_config: dict[str, Any],
) -> None:
    _, snapshot = _snapshot(tmp_path, valid_config)
    archive = tmp_path / "backup.tar.zst"
    restore_root = tmp_path / "restore"
    restore_root.mkdir()

    create_tar_zstd(
        snapshot,
        archive,
        compression_level=6,
        tools=TOOLS,
    )
    _restore_tar_zstd(archive, restore_root)

    _assert_restored_snapshot(snapshot, restore_root)


@pytest.mark.skipif(not _is_debian(), reason="Interactive 7-Zip integration is Debian-only")
def test_real_header_encrypted_tar_7z_creation_and_verification(
    tmp_path: Path,
    valid_config: dict[str, Any],
) -> None:
    _, snapshot = _snapshot(tmp_path, valid_config)
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    inner_tar = private_directory / "app.tar"
    archive = tmp_path / "backup.tar.7z"
    password = PasswordSecret("test-pässphrase".encode())

    try:
        create_inner_tar(snapshot, inner_tar, tools=TOOLS)
        encrypt_inner_tar(
            inner_tar,
            archive,
            compression_level=6,
            password=password,
            tools=TOOLS,
        )
        verify_tar_7z(
            archive,
            snapshot,
            job_id="app",
            password=password,
            tools=TOOLS,
        )
        cleanup_private_inner_tar(inner_tar)
    finally:
        password.clear()
        with suppress(FileNotFoundError):
            inner_tar.unlink()
        with suppress(FileNotFoundError):
            private_directory.rmdir()

    assert archive.stat().st_size > 0
    assert b"app.tar" not in archive.read_bytes()


@pytest.mark.skipif(not _is_debian(), reason="Interactive 7-Zip integration is Debian-only")
def test_real_header_encrypted_tar_7z_restore_drill(
    tmp_path: Path,
    valid_config: dict[str, Any],
) -> None:
    _, snapshot = _snapshot(tmp_path, valid_config)
    private_directory = tmp_path / "private"
    private_directory.mkdir(mode=0o700)
    inner_tar = private_directory / "app.tar"
    archive = tmp_path / "backup.tar.7z"
    restore_root = tmp_path / "restore"
    restore_root.mkdir()
    password = PasswordSecret("test-pässphrase".encode())

    try:
        create_inner_tar(snapshot, inner_tar, tools=TOOLS)
        encrypt_inner_tar(
            inner_tar,
            archive,
            compression_level=6,
            password=password,
            tools=TOOLS,
        )
        cleanup_private_inner_tar(inner_tar)
        inner_tar = private_directory / "app.tar"
        _restore_tar_7z(archive, restore_root, password)
    finally:
        password.clear()
        with suppress(FileNotFoundError):
            inner_tar.unlink()
        with suppress(FileNotFoundError):
            private_directory.rmdir()

    _assert_restored_snapshot(snapshot, restore_root)
