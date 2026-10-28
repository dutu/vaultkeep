"""Cleanup of an attempt's staging directory across the backup commit point."""

from __future__ import annotations

import errno
import json
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.archive.base import ArchiveArtifact, ArchiveBuildRequest
from vaultkeep.archive.checksums import calculate_archive_sha256, write_checksum_sidecar
from vaultkeep.backup import workflow
from vaultkeep.backup.status import backup_status
from vaultkeep.destination import finalize as finalize_module
from vaultkeep.errors import (
    DestinationCommitDurabilityError,
    DestinationFinalizeError,
    DestinationTemplateError,
    HookError,
)
from vaultkeep.locking import DESTINATION_LOCK_NAME
from vaultkeep.state.identity import job_identity_hash


@pytest.fixture
def job(tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"source data")
    destination = tmp_path / "backups"
    destination.mkdir()
    valid_config["sources"][0].update(path=str(source))
    valid_config["destination"].update(root=str(destination))
    config_path = tmp_path / "app.yaml"
    config_path.write_text(json.dumps(valid_config))

    def build(request: ArchiveBuildRequest, **kwargs: Any) -> ArchiveArtifact:
        request.archive_path.write_bytes(b"archive")
        digest = calculate_archive_sha256(request.archive_path)
        write_checksum_sidecar(request.archive_path, request.checksum_path, digest)
        return ArchiveArtifact(
            archive_format=request.archive_format,
            archive_path=request.archive_path,
            checksum_path=request.checksum_path,
            sha256=digest,
            size=request.archive_path.stat().st_size,
            source_digest=request.expected_source_digest,
        )

    monkeypatch.setattr(workflow, "build_archive", build)
    # Cleanup tests create placeholder archive bytes rather than compressed TARs.
    monkeypatch.setattr(workflow, "verify_tar_zstd", lambda *args, **kwargs: None)
    return config_path


def _run(job: Path) -> workflow.CommandResult:
    return workflow.run_backup(
        job,
        paths=workflow.WorkflowPaths(
            job.parent / "state", job.parent / "cache", job.parent / "locks"
        ),
        user_mode=True,
    )


@pytest.mark.parametrize(
    "stage", ["before_archive", "archive", "after_archive", "manifest", "rename"]
)
def test_precommit_failure_removes_only_current_partial_and_preserves_previous_backup(
    job: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    previous = _run(job)
    assert previous.archive_path is not None
    destination = job.parent / "backups"
    older_partial = destination / ".partial-vaultkeep-app-older"
    older_partial.mkdir()
    (older_partial / "data").write_bytes(b"preserve")
    (job.parent / "source" / "data").write_bytes(b"changed")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise HookError(f"forced {stage} failure")

    with monkeypatch.context() as patch:
        if stage in {"before_archive", "after_archive"}:
            original = workflow._run_configured_hook

            def hook(phase: str, *args: Any, **kwargs: Any) -> None:
                if phase == stage:
                    fail()
                original(phase, *args, **kwargs)

            patch.setattr(workflow, "_run_configured_hook", hook)
        elif stage == "archive":
            patch.setattr(workflow, "build_archive", fail)
        elif stage == "manifest":
            patch.setattr("vaultkeep.destination.creation.write_manifest", fail)
        else:
            rename = finalize_module._rename_noreplace

            def deny_backup_rename(source: Path, target: Path) -> None:
                if source.name.startswith("~$.partial-vaultkeep-"):
                    raise PermissionError(errno.EACCES, "rename denied", str(target))
                rename(source, target)

            patch.setattr(finalize_module, "_rename_noreplace", deny_backup_rename)

        with pytest.raises((HookError, DestinationFinalizeError)):
            _run(job)

    report = backup_status(
        workflow.WorkflowPaths(job.parent / "state", job.parent / "cache", job.parent / "locks"),
        "app",
        job_identity_hash(job, "app"),
    )
    assert report["previous"]["result"] == "FAILED"
    if stage == "rename":
        assert report["previous"]["reason"] == "Backup finalization failed"
    assert set(destination.iterdir()) == {
        previous.archive_path.parent,
        older_partial,
        destination / DESTINATION_LOCK_NAME,
    }
    assert previous.archive_path.read_bytes() == b"archive"
    assert (older_partial / "data").read_bytes() == b"preserve"
    assert _run(job).result == "created"


def test_cleanup_failure_reports_remaining_path_and_preserves_original_error(
    job: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    original_error = HookError("archive failed")

    def fail_build(*args: Any, **kwargs: Any) -> None:
        raise original_error

    def deny_cleanup(path: Path) -> None:
        raise PermissionError(errno.EACCES, "cleanup denied")

    monkeypatch.setattr(workflow, "build_archive", fail_build)
    monkeypatch.setattr(workflow.shutil, "rmtree", deny_cleanup)

    with pytest.raises(HookError) as caught:
        _run(job)

    assert caught.value is original_error
    (partial,) = (
        path for path in (job.parent / "backups").iterdir() if path.name != DESTINATION_LOCK_NAME
    )
    assert partial.name.startswith("~$.partial-vaultkeep-")
    assert f"Cannot remove partial backup directory {partial}" in caplog.text
    assert "cleanup denied" in caplog.text
    log = next((job.parent / "state").rglob("execution.log"))
    assert "cleanup denied" in log.read_text()
    assert "archive failed" in log.read_text()


def test_cleanup_does_not_follow_symlinks_inside_staging(
    job: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = job.parent / "outside"
    outside.mkdir()
    (outside / "data").write_bytes(b"preserve")

    def fail_build(request: ArchiveBuildRequest, **kwargs: Any) -> None:
        (request.archive_path.parent / "link").symlink_to(outside, target_is_directory=True)
        raise HookError("archive failed")

    monkeypatch.setattr(workflow, "build_archive", fail_build)

    with pytest.raises(HookError, match="archive failed"):
        _run(job)

    assert {path.name for path in (job.parent / "backups").iterdir()} == {DESTINATION_LOCK_NAME}
    assert (outside / "data").read_bytes() == b"preserve"


def test_failed_staging_creation_does_not_remove_existing_directory(
    job: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = workflow.create_staging_directory
    existing: list[Path] = []

    def collide(paths: Any) -> None:
        paths.staging_directory.mkdir()
        (paths.staging_directory / "data").write_bytes(b"preserve")
        existing.append(paths.staging_directory)
        original(paths)

    monkeypatch.setattr(workflow, "create_staging_directory", collide)

    with pytest.raises(DestinationTemplateError, match="Cannot create backup staging directory"):
        _run(job)

    assert (existing[0] / "data").read_bytes() == b"preserve"


def test_parent_flush_failure_preserves_committed_backup(
    job: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsync_directory = finalize_module._fsync_directory
    destination = job.parent / "backups"

    def fail_parent_flush(path: Path) -> None:
        if path == destination:
            raise OSError("parent flush failed")
        fsync_directory(path)

    monkeypatch.setattr(finalize_module, "_fsync_directory", fail_parent_flush)

    with pytest.raises(DestinationCommitDurabilityError, match="committed"):
        _run(job)

    (backup,) = (path for path in destination.iterdir() if path.name != DESTINATION_LOCK_NAME)
    assert not backup.name.startswith("~$.partial-")
    assert len(list(backup.iterdir())) == 3
    assert workflow.verify_backups(job, user_mode=True).backups == 1


def test_failure_after_commit_preserves_backup(job: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("retention failed")

    monkeypatch.setattr(workflow, "execute_prune_plan", fail)

    result = _run(job)
    assert result.result == "created"
    assert result.cleanup_errors == ("retention failed",)

    (backup,) = (
        path for path in (job.parent / "backups").iterdir() if path.name != DESTINATION_LOCK_NAME
    )
    assert not backup.name.startswith("~$.partial-")
    assert workflow.verify_backups(job, user_mode=True).backups == 1
