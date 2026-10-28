"""Tests for the no-overwrite atomic backup commit point."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from vaultkeep.destination import finalize as finalize_module
from vaultkeep.destination import finalize_backup_directory, probe_destination_writability
from vaultkeep.errors import (
    DestinationCommitDurabilityError,
    DestinationError,
    DestinationFinalizeError,
)
from vaultkeep.locking import DESTINATION_LOCK_NAME


def test_destination_probe_checks_nonempty_directory_rename_and_preserves_existing_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "operator-data"
    existing.write_bytes(b"preserve")
    original = finalize_module._rename_noreplace
    renames: list[tuple[Path, Path]] = []

    def record_rename(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent == tmp_path
        assert (source / "probe").read_bytes()
        renames.append((source, destination))
        original(source, destination)

    monkeypatch.setattr(finalize_module, "_rename_noreplace", record_rename)

    probe_destination_writability(tmp_path)

    assert len(renames) == 1
    assert set(tmp_path.iterdir()) == {existing, tmp_path / DESTINATION_LOCK_NAME}
    assert existing.read_bytes() == b"preserve"


@pytest.mark.parametrize("operation", ["mkdir", "write", "rename", "unlink", "rmdir"])
def test_destination_probe_reports_permission_failures_and_cleanup_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError(errno.EACCES, "denied")

    with monkeypatch.context() as patch:
        if operation == "mkdir":
            patch.setattr(finalize_module.tempfile, "mkdtemp", denied)
        elif operation == "write":
            patch.setattr(Path, "write_bytes", denied)
        elif operation == "rename":
            patch.setattr(finalize_module, "_rename_noreplace", denied)
        else:
            patch.setattr(Path, operation, denied)

        with pytest.raises(DestinationError) as caught:
            probe_destination_writability(tmp_path)

    message = str(caught.value)
    assert str(tmp_path) in message
    assert "denied" in message
    assert (tmp_path / DESTINATION_LOCK_NAME).is_file()
    leftovers = [path for path in tmp_path.iterdir() if path.name != DESTINATION_LOCK_NAME]
    if operation in {"unlink", "rmdir"}:
        assert len(leftovers) == 1
        assert f"Cannot remove destination probe {leftovers[0]}" in message
    else:
        assert leftovers == []


def test_destination_probe_preserves_rename_error_when_cleanup_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def deny_rename(source: Path, destination: Path) -> None:
        raise PermissionError("rename denied")

    def deny_removal(path: Path) -> None:
        raise PermissionError("removal denied")

    monkeypatch.setattr(finalize_module, "_rename_noreplace", deny_rename)
    monkeypatch.setattr(Path, "rmdir", deny_removal)

    with pytest.raises(DestinationError) as caught:
        probe_destination_writability(tmp_path)

    assert "removal denied" in str(caught.value)
    assert "could not atomically rename a probe directory: rename denied" in str(caught.value)


def test_destination_probe_never_removes_an_existing_rename_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = finalize_module._rename_noreplace
    collisions: list[Path] = []

    def collide(source: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "operator-data").write_bytes(b"preserve")
        collisions.append(destination)
        original(source, destination)

    monkeypatch.setattr(finalize_module, "_rename_noreplace", collide)

    with pytest.raises(DestinationError, match="atomically rename"):
        probe_destination_writability(tmp_path)

    assert set(tmp_path.iterdir()) == {*collisions, tmp_path / DESTINATION_LOCK_NAME}
    assert (collisions[0] / "operator-data").read_bytes() == b"preserve"


def _staging(tmp_path: Path) -> Path:
    staging = tmp_path / "~$.partial-vaultkeep-app-abc"
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


@pytest.mark.parametrize("name", ["ordinary", ".partial-vaultkeep-app-abc"])
def test_finalize_requires_staging_prefix(tmp_path: Path, name: str) -> None:
    staging = tmp_path / name
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
