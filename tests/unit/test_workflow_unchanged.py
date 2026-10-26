"""Latest-backup verification before skipping archive creation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.archive import ArchiveArtifact, ArchiveBuildRequest, checksums
from vaultkeep.backup import workflow
from vaultkeep.destination.suspect import SUSPECT_MARKER_NAME, mark_backup_suspect
from vaultkeep.errors import ArchiveVerificationError, DestinationError
from vaultkeep.state.models import LocalState


def _run(job: Path) -> workflow.CommandResult:
    return workflow.run_backup(
        job,
        paths=workflow.WorkflowPaths(
            job.parent / "state", job.parent / "cache", job.parent / "locks"
        ),
        user_mode=True,
    )


@pytest.fixture(params=["combined", "per_file"])
def history(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, list[Path]]:
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "backups").mkdir()
    valid_config["backup_mode"] = request.param
    valid_config["sources"][0]["path"] = str(source)
    valid_config["destination"]["root"] = str(tmp_path / "backups")
    job = tmp_path / "app.yaml"
    job.write_text(json.dumps(valid_config))

    class Clock:
        current = datetime(2026, 10, 26, tzinfo=UTC)

        @classmethod
        def now(cls) -> datetime:
            cls.current += timedelta(hours=1)
            return cls.current

    def build(backup: ArchiveBuildRequest, **kwargs: Any) -> ArchiveArtifact:
        backup.archive_path.write_bytes(backup.expected_source_digest.encode())
        digest = checksums.calculate_archive_sha256(backup.archive_path)
        checksums.write_checksum_sidecar(backup.archive_path, backup.checksum_path, digest)
        return ArchiveArtifact(
            archive_format=backup.archive_format,
            archive_path=backup.archive_path,
            checksum_path=backup.checksum_path,
            sha256=digest,
            size=backup.archive_path.stat().st_size,
            source_digest=backup.expected_source_digest,
        )

    monkeypatch.setattr(workflow, "datetime", Clock)
    monkeypatch.setattr(workflow, "build_archive", build)
    archives = []
    for number in range(3):
        (source / "data").write_text(str(number))
        result = _run(job)
        assert result.result == "created"
        archive = result.files[0].archive_path if result.files else result.archive_path
        assert archive is not None
        archives.append(archive)
    assert all(archive.is_file() for archive in archives)
    return job, archives


@pytest.mark.parametrize("state_status", ["loaded", "missing", "corrupt", "stale"])
@pytest.mark.parametrize("older_corrupted", [False, True])
def test_unchanged_reads_only_latest_archive(
    history: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
    state_status: str,
    older_corrupted: bool,
) -> None:
    job, archives = history
    (state_path,) = (job.parent / "state").rglob("state.json")
    if state_status == "missing":
        state_path.unlink()
    elif state_status == "corrupt":
        state_path.write_text("invalid state")
    elif state_status == "stale":
        state = LocalState.model_validate_json(state_path.read_bytes())
        state_path.write_text(
            state.model_copy(
                update={"last_successful_backup": None, "last_run": None}
            ).model_dump_json()
        )
    if older_corrupted:
        archives[0].write_bytes(b"corrupted older archive")
    original_open = Path.open
    opened = []

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path in archives:
            assert path == archives[-1], "Change detection must not read older archives"
            opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    result = _run(job)

    assert result.result == "unchanged"
    assert opened == [archives[-1]]
    assert len(list((job.parent / "backups").rglob("*.tar.zst"))) == 3
    assert LocalState.model_validate_json(state_path.read_bytes()).last_run.result == "unchanged"


@pytest.mark.parametrize("state_status", ["loaded", "missing", "corrupt"])
def test_corrupt_latest_archive_creates_replacement_and_cleans_suspect(
    history: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch, state_status: str
) -> None:
    job, archives = history
    # Even an older backup of identical sources must not justify skipping the latest.
    older_manifest = next(archives[0].parent.glob("*.json"))
    latest_manifest = json.loads(next(archives[-1].parent.glob("*.json")).read_text())
    document = json.loads(older_manifest.read_text())
    document["source_digest"] = latest_manifest["source_digest"]
    older_manifest.write_text(json.dumps(document))
    archives[-1].write_bytes(b"corrupted latest archive")
    original_artifacts = {
        path: path.read_bytes() for path in archives[-1].parent.iterdir() if path.is_file()
    }
    (state_path,) = (job.parent / "state").rglob("state.json")
    if state_status == "missing":
        state_path.unlink()
    elif state_status == "corrupt":
        state_path.write_text("invalid state")
    phases = []
    original_hook = workflow._run_configured_hook

    def hook(phase: str, *args: Any, **kwargs: Any) -> None:
        phases.append(phase)
        original_hook(phase, *args, **kwargs)

    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert path not in archives[:-1], "Replacement must not read older archive contents"
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded_open)
        result = _run(job)

    assert result.result == "created"
    replacement = result.files[0].archive_path if result.files else result.archive_path
    assert replacement is not None and replacement not in archives
    assert replacement.is_file()
    assert phases == ["before_check", "before_archive", "after_archive", "on_success"]
    marker = archives[-1].parent / SUSPECT_MARKER_NAME
    assert not marker.parent.exists()
    assert all(
        path.read_bytes() == contents
        for path, contents in original_artifacts.items()
        if path.parent != marker.parent
    )
    state = LocalState.model_validate_json(state_path.read_bytes())
    assert state.last_run.result == "created"
    assert state.last_successful_backup.backup_path == str(replacement)
    assert state.last_unchanged_at_utc is None
    assert len(list((job.parent / "backups").rglob("*.tar.zst"))) == 3
    assert _run(job).result == "unchanged"


def test_marked_latest_backup_retries_creation_after_a_failed_replacement(
    history: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    job, archives = history
    archives[-1].write_bytes(b"damaged")
    original_build = workflow.build_archive

    def fail(*args: Any, **kwargs: Any) -> ArchiveArtifact:
        raise ArchiveVerificationError("replacement failed")

    with monkeypatch.context() as patch:
        patch.setattr(workflow, "build_archive", fail)
        if json.loads(job.read_text())["backup_mode"] == "combined":
            with pytest.raises(ArchiveVerificationError, match="replacement failed"):
                _run(job)
        else:
            assert _run(job).result == "failed"
    marker = archives[-1].parent / SUSPECT_MARKER_NAME
    assert marker.is_file()
    assert archives[-1].read_bytes() == b"damaged"
    # Match an older healthy backup; the newer damage still requires a replacement.
    (job.parent / "source" / "data").write_text("0")
    monkeypatch.setattr(workflow, "build_archive", original_build)
    assert _run(job).result == "created"
    assert not marker.exists()


def test_damaged_backup_is_preserved_by_retention_but_does_not_fill_healthy_slots(
    history: tuple[Path, list[Path]],
) -> None:
    job, archives = history
    archives[-1].write_bytes(b"damaged")
    mark_backup_suspect(archives[-1].parent, "checksum mismatch")
    document = json.loads(job.read_text())
    document["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    job.write_text(json.dumps(document))

    assert _run(job).result == "created"
    assert not archives[-1].exists()
    assert not any(archive.exists() for archive in archives[:-1])
    assert len(list((job.parent / "backups").rglob("*.tar.zst"))) == 1
    assert workflow.prune_backups(job, dry_run=False).removed == 0


def test_verify_reports_marked_damage_without_reading_the_damaged_archive(
    history: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    job, archives = history
    mark_backup_suspect(archives[-1].parent, "checksum mismatch")
    monkeypatch.setattr(workflow, "verify_tar_zstd", lambda *args, **kwargs: None)
    with pytest.raises(DestinationError, match="SUSPECT"):
        workflow.verify_backups(job)


def test_archive_read_failure_does_not_mark_damage_or_create_replacement(
    history: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    job, archives = history

    def unreadable(*args: Any, **kwargs: Any) -> str:
        raise ArchiveVerificationError("Cannot read archive: permission denied")

    monkeypatch.setattr(workflow, "verify_backup_checksum", unreadable)
    if json.loads(job.read_text())["backup_mode"] == "combined":
        with pytest.raises(ArchiveVerificationError, match="permission denied"):
            _run(job)
    else:
        assert _run(job).result == "failed"
    assert not (archives[-1].parent / SUSPECT_MARKER_NAME).exists()
    assert len(list((job.parent / "backups").rglob("*.tar.zst"))) == 3


@pytest.mark.parametrize("stage", ["mark", "record"])
def test_damage_persistence_failure_preserves_backups_and_retries(
    history: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    job, archives = history
    archives[-1].write_bytes(b"damaged")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise DestinationError("damage metadata write denied")

    with monkeypatch.context() as patch:
        patch.setattr(
            workflow,
            "mark_backup_suspect" if stage == "mark" else "record_suspect_replacement",
            fail,
        )
        if stage == "record":
            result = _run(job)
            assert result.result == "created" and result.cleanup_errors
        elif json.loads(job.read_text())["backup_mode"] == "combined":
            with pytest.raises(DestinationError, match="write denied"):
                _run(job)
        else:
            assert _run(job).result == "failed"
    assert all(archive.exists() for archive in archives)
    assert archives[-1].read_bytes() == b"damaged"
    expected_count = 3 if stage == "mark" else 4
    assert len(list((job.parent / "backups").rglob("*.tar.zst"))) == expected_count
    assert _run(job).result == "created"
    assert _run(job).result == "unchanged"


@pytest.mark.parametrize("older_corrupted", [False, True])
def test_creation_verifies_new_archive_without_reading_existing_archive_contents(
    history: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch, older_corrupted: bool
) -> None:
    job, archives = history
    if older_corrupted:
        archives[0].write_bytes(b"corrupted older archive")
    # Matching an older backup still requires creation when the latest differs.
    (job.parent / "source" / "data").write_text("0")
    original_build = workflow.build_archive
    original_calculate = checksums.calculate_archive_sha256
    original_open = Path.open
    hashed = []

    def build(backup: ArchiveBuildRequest, **kwargs: Any) -> ArchiveArtifact:
        assert hashed == [], "Changed sources need no pre-creation archive checksum reads"
        return original_build(backup, **kwargs)

    def calculate(path: Path, **kwargs: Any) -> str:
        hashed.append(path)
        return original_calculate(path, **kwargs)

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert path not in archives, "Creation and retention must not read existing archives"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(workflow, "build_archive", build)
    monkeypatch.setattr(checksums, "calculate_archive_sha256", calculate)
    monkeypatch.setattr(Path, "open", guarded_open)

    result = _run(job)

    assert result.result == "created"
    created = result.files[0].archive_path if result.files else result.archive_path
    assert created is not None
    assert created in hashed
    assert all(path not in archives for path in hashed)


def test_run_retention_prunes_older_archives_without_reading_contents(
    history: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    job, archives = history
    document = json.loads(job.read_text())
    document["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    job.write_text(json.dumps(document))
    (job.parent / "source" / "data").write_text("changed source")
    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert path not in archives, "Retention must not open older archive contents"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    result = _run(job)

    assert result.result == "created"
    assert result.removed == 3
    assert not any(archive.exists() for archive in archives)
    created = result.files[0].archive_path if result.files else result.archive_path
    assert created is not None and created.is_file()


def test_corrupt_finalized_archive_fails_before_retention(
    history: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    job, archives = history
    document = json.loads(job.read_text())
    document["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    job.write_text(json.dumps(document))
    (job.parent / "source" / "data").write_text("changed source")
    original_commit = workflow.commit_archive_artifact

    def corrupt_after_commit(paths: Any, *args: Any, **kwargs: Any) -> Any:
        manifest = original_commit(paths, *args, **kwargs)
        (paths.final_directory / paths.archive_path.name).write_bytes(b"corrupt finalized archive")
        return manifest

    monkeypatch.setattr(workflow, "commit_archive_artifact", corrupt_after_commit)

    if document["backup_mode"] == "combined":
        with pytest.raises(ArchiveVerificationError, match="checksum does not match"):
            _run(job)
    else:
        assert _run(job).result == "failed"
    assert all(archive.is_file() for archive in archives)
    (state_path,) = (job.parent / "state").rglob("state.json")
    state = LocalState.model_validate_json(state_path.read_bytes())
    assert state.last_run.result == "failed"
    assert state.last_successful_backup.backup_path == str(archives[-1])
