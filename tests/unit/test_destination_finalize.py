"""Tests for the no-overwrite atomic backup commit point."""

from __future__ import annotations

from pathlib import Path

import pytest

from vaultkeep.destination import finalize as finalize_module
from vaultkeep.destination import finalize_backup_directory
from vaultkeep.errors import (
    DestinationCommitDurabilityError,
    DestinationFinalizeError,
)


def _staging(tmp_path: Path) -> Path:
    staging = tmp_path / ".partial-vaultkeep-app-abc"
    staging.mkdir()
    (staging / "archive.tar.zst").write_bytes(b"archive")
    (staging / "archive.tar.zst.sha256").write_text("checksum", encoding="utf-8")
    (staging / "backup.json").write_text("{}", encoding="utf-8")
    return staging


def test_finalize_atomically_moves_complete_staging_directory(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    final = tmp_path / "backup-app-id"

    finalize_backup_directory(staging, final)

    assert not staging.exists()
    assert (final / "archive.tar.zst").read_bytes() == b"archive"


def test_finalize_never_overwrites_existing_final_directory(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    final = tmp_path / "backup-app-id"
    final.mkdir()
    marker = final / "operator-data"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(DestinationFinalizeError, match="already exists"):
        finalize_backup_directory(staging, final)

    assert staging.exists()
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_finalize_rejects_cross_directory_move(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    other = tmp_path / "other"
    other.mkdir()

    with pytest.raises(DestinationFinalizeError, match="share"):
        finalize_backup_directory(staging, other / "final")


def test_finalize_rejects_nonregular_staging_content(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    (staging / "nested").mkdir()

    with pytest.raises(DestinationFinalizeError, match="non-regular"):
        finalize_backup_directory(staging, tmp_path / "final")


def test_finalize_requires_hidden_partial_prefix(tmp_path: Path) -> None:
    staging = tmp_path / "ordinary"
    staging.mkdir()

    with pytest.raises(DestinationFinalizeError, match="prefix"):
        finalize_backup_directory(staging, tmp_path / "final")


def test_post_commit_directory_flush_failure_is_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = _staging(tmp_path)
    final = tmp_path / "backup-app-id"
    calls = 0
    original = finalize_module._fsync_directory

    def fail_after_commit(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("forced parent flush failure")
        original(path)

    monkeypatch.setattr(finalize_module, "_fsync_directory", fail_after_commit)

    with pytest.raises(DestinationCommitDurabilityError, match="committed"):
        finalize_backup_directory(staging, final)

    assert final.exists()
