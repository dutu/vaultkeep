"""Real GNU TAR, Zstandard, and Debian 7-Zip integration tests."""

from __future__ import annotations

import sys
from contextlib import suppress
from copy import deepcopy
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.archive import ArchiveTools, PasswordSecret, verify_tar_7z, verify_tar_zstd
from vaultkeep.archive.tar_7z import (
    cleanup_private_inner_tar,
    encrypt_inner_tar,
)
from vaultkeep.archive.tar_zstd import create_inner_tar, create_tar_zstd
from vaultkeep.config import JobConfig
from vaultkeep.sources import SourceSnapshot, discover_sources

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
