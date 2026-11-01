"""Latest-backup verification before skipping archive creation."""

from __future__ import annotations

import json
import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vaultkeep import hooks
from vaultkeep.archive import ArchiveArtifact, ArchiveBuildRequest, checksums
from vaultkeep.backup import workflow
from vaultkeep.backup.results import failure_exit_code
from vaultkeep.destination.suspect import SUSPECT_MARKER_NAME, mark_backup_suspect
from vaultkeep.errors import (
    ArchiveChecksumMismatchError,
    ArchiveVerificationError,
    DestinationError,
    HookError,
)
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
    original_open = checksums._open_regular_file
    opened = []

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path in archives:
            assert path == archives[-1], "Change detection must not read older archives"
            opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(checksums, "_open_regular_file", guarded_open)

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
    original_open = checksums._open_regular_file

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert path not in archives[:-1], "Replacement must not read older archive contents"
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(checksums, "_open_regular_file", guarded_open)
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
    assert workflow.prune_backups(job, dry_run=False, user_mode=True).removed == 0


def test_verify_reports_marked_damage_without_reading_the_damaged_archive(
    history: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    job, archives = history
    mark_backup_suspect(archives[-1].parent, "checksum mismatch")
    monkeypatch.setattr(workflow, "verify_tar_zstd", lambda *args, **kwargs: None)
    with pytest.raises(DestinationError, match="SUSPECT"):
        workflow.verify_backups(job, user_mode=True)


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


@pytest.mark.parametrize("after_archive", [False, True])
@pytest.mark.parametrize("on_success", [False, True])
@pytest.mark.parametrize("older_corrupted", [False, True])
def test_creation_verifies_new_archive_without_reading_existing_archive_contents(
    history: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
    older_corrupted: bool,
    after_archive: bool,
    on_success: bool,
) -> None:
    job, archives = history
    document = json.loads(job.read_text())
    for phase, enabled in (("after_archive", after_archive), ("on_success", on_success)):
        document["hooks"][phase] = {"command": "true"} if enabled else None
    job.write_text(json.dumps(document))
    monkeypatch.setattr(workflow, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    if older_corrupted:
        archives[0].write_bytes(b"corrupted older archive")
    # Matching an older backup still requires creation when the latest differs.
    (job.parent / "source" / "data").write_text("0")
    original_build = workflow.build_archive
    original_calculate = checksums.calculate_archive_sha256
    original_open = checksums._open_regular_file
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

    original_hook = workflow._run_configured_hook

    def hook(phase: str, *args: Any, **kwargs: Any) -> None:
        if phase == "on_success" and on_success:
            assert len(hashed) == 1 + after_archive, "Success-hook check must follow the hook"
        original_hook(phase, *args, **kwargs)

    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    monkeypatch.setattr(workflow, "build_archive", build)
    monkeypatch.setattr(checksums, "calculate_archive_sha256", calculate)
    monkeypatch.setattr(checksums, "_open_regular_file", guarded_open)

    result = _run(job)

    assert result.result == "created"
    created = result.files[0].archive_path if result.files else result.archive_path
    assert created is not None
    assert (created in hashed) == on_success
    assert all(path.name == created.name for path in hashed)
    assert all(path not in archives for path in hashed)
    # One initial digest, plus one integrity check after each configured hook.
    assert len(hashed) == 1 + after_archive + on_success


def test_run_retention_prunes_older_archives_without_reading_contents(
    history: tuple[Path, list[Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    job, archives = history
    document = json.loads(job.read_text())
    document["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    job.write_text(json.dumps(document))
    (job.parent / "source" / "data").write_text("changed source")
    original_open = checksums._open_regular_file

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        assert path not in archives, "Retention must not open older archive contents"
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(checksums, "_open_regular_file", guarded_open)

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
    document["hooks"]["on_success"] = {"command": "true"}
    job.write_text(json.dumps(document))
    monkeypatch.setattr(workflow, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
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
    assert state.last_successful_backup.backup_path != str(archives[-1])
    assert list((job.parent / "backups").rglob(SUSPECT_MARKER_NAME))


@pytest.mark.parametrize("phase", ["after_archive", "on_success"])
@pytest.mark.parametrize("artifact", ["archive", "checksum", "all"])
def test_hook_artifact_corruption_blocks_retention(
    history: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    artifact: str,
) -> None:
    job, archives = history
    document = json.loads(job.read_text())
    document["hooks"][phase] = {"command": "true"}
    document["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    job.write_text(json.dumps(document))
    (job.parent / "source" / "data").write_text("changed source")
    original_hook = workflow._run_configured_hook
    monkeypatch.setattr(workflow, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))

    def hook(hook_phase: str, *args: Any, **kwargs: Any) -> None:
        original_hook(hook_phase, *args, **kwargs)
        if hook_phase == phase:
            (created,) = (
                path for path in (job.parent / "backups").rglob("*.tar.zst") if path not in archives
            )
            if artifact == "archive":
                created.write_bytes(b"corrupted by hook")
            elif artifact == "checksum":
                sidecar = Path(str(created) + ".sha256")
                sidecar.write_text("0" * 64 + "  " + created.name + "\n")
            else:
                # Even a self-consistent rewritten archive/sidecar/manifest must
                # not override the digest established before the hook.
                created.write_bytes(b"corrupted by hook")
                digest = checksums.calculate_archive_sha256(created)
                Path(str(created) + ".sha256").write_text(digest + "  " + created.name + "\n")
                if phase == "on_success":
                    (manifest_path,) = created.parent.glob("*.json")
                    manifest = json.loads(manifest_path.read_text())
                    manifest["archive_digest"] = "sha256:" + digest
                    manifest_path.write_text(json.dumps(manifest))

    monkeypatch.setattr(workflow, "_run_configured_hook", hook)

    if document["backup_mode"] == "combined":
        with pytest.raises((ArchiveVerificationError, DestinationError)):
            _run(job)
    else:
        assert _run(job).result == "failed"
    assert all(archive.is_file() for archive in archives)
    if phase == "on_success" and artifact in {"archive", "all"}:
        assert list((job.parent / "backups").rglob(SUSPECT_MARKER_NAME))


@pytest.mark.parametrize(
    "archive_status", ["valid", "corrupt", "missing", "manifest_missing", "unreadable"]
)
def test_failed_success_hook_still_checks_integrity_and_blocks_retention(
    history: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    archive_status: str,
) -> None:
    job, archives = history
    document = json.loads(job.read_text())
    document["hooks"]["on_success"] = {"command": "exit 9"}
    document["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    job.write_text(json.dumps(document))
    (job.parent / "source" / "data").write_text("changed source")
    monkeypatch.setattr(workflow, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    original_hook = workflow._run_configured_hook
    original_verify = workflow.verify_backup_checksum
    original_open = checksums._open_regular_file
    phases = []
    checked = []
    created_archive = None

    def hook(phase: str, *args: Any, **kwargs: Any) -> None:
        nonlocal created_archive
        phases.append(phase)
        try:
            original_hook(phase, *args, **kwargs)
        finally:
            if phase == "on_success":
                (created_archive,) = (
                    path
                    for path in (job.parent / "backups").rglob("*.tar.zst")
                    if path not in archives
                )
                if archive_status == "corrupt":
                    created_archive.write_bytes(b"corrupted by failing hook")
                elif archive_status == "missing":
                    created_archive.unlink()
                elif archive_status == "manifest_missing":
                    (manifest,) = created_archive.parent.glob("*.json")
                    manifest.unlink()

    def verify(backup: Any, **kwargs: Any) -> None:
        checked.append(backup.archive_path)
        original_verify(backup, **kwargs)

    def open_archive(path: Path, *args: Any, **kwargs: Any) -> Any:
        if archive_status == "unreadable" and path == created_archive:
            raise PermissionError("archive read denied")
        return original_open(path, *args, **kwargs)

    def prune(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Retention must not run after hook or integrity failure")

    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    monkeypatch.setattr(workflow, "verify_backup_checksum", verify)
    monkeypatch.setattr(workflow, "execute_prune_plan", prune)
    monkeypatch.setattr(checksums, "_open_regular_file", open_archive)

    if document["backup_mode"] == "combined":
        with pytest.raises(HookError, match="on_success hook exited with status 9") as raised:
            _run(job)
        assert failure_exit_code(raised.value) == 11
        message = str(raised.value)
    else:
        result = _run(job)
        assert result.result == "failed"
        message = result.files[0].error
    assert ("post-hook backup integrity check failed" in message) == (archive_status != "valid")
    assert "on_success hook failed:" in caplog.text
    assert ("Post-hook backup integrity check failed:" in caplog.text) == (
        archive_status != "valid"
    )
    assert checked == [created_archive]
    assert "on_failure" not in phases
    assert all(archive.is_file() for archive in archives)
    markers = list((job.parent / "backups").rglob(SUSPECT_MARKER_NAME))
    assert bool(markers) == (archive_status == "corrupt")
    (state_path,) = (job.parent / "state").rglob("state.json")
    state = LocalState.model_validate_json(state_path.read_bytes())
    assert state.last_run.result == "failed"
    assert state.last_run.hooks[-1].phase == "on_success"
    assert state.last_run.hooks[-1].exit_code == 9


@pytest.fixture
def checksum_read_deadline():
    """Make an accidentally blocking FIFO regression fail instead of hanging pytest."""
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(signum, frame):
        pytest.fail("Checksum verification blocked while reading an unsafe artifact")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


@pytest.mark.usefixtures("checksum_read_deadline")
@pytest.mark.parametrize("hook_fails", [False, True])
@pytest.mark.parametrize("artifact", ["archive", "checksum"])
@pytest.mark.parametrize("replacement", ["fifo", "symlink", "directory_symlink"])
def test_post_hook_rejects_unsafe_artifacts_without_blocking(
    history: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
    hook_fails: bool,
    artifact: str,
    replacement: str,
) -> None:
    job, archives = history
    document = json.loads(job.read_text())
    document["hooks"]["on_success"] = {"command": "exit 9" if hook_fails else "true"}
    document["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    job.write_text(json.dumps(document))
    (job.parent / "source" / "data").write_text("changed source")
    monkeypatch.setattr(workflow, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    original_hook = workflow._run_configured_hook

    def hook(phase: str, *args: Any, **kwargs: Any) -> None:
        try:
            original_hook(phase, *args, **kwargs)
        finally:
            if phase == "on_success":
                (created,) = (
                    path
                    for path in (job.parent / "backups").rglob("*.tar.zst")
                    if path not in archives
                )
                target = created if artifact == "archive" else Path(str(created) + ".sha256")
                if replacement == "directory_symlink":
                    moved = job.parent / "moved-backup"
                    created.parent.rename(moved)
                    created.parent.symlink_to(moved, target_is_directory=True)
                else:
                    payload = target.read_bytes()
                    target.unlink()
                    if replacement == "fifo":
                        os.mkfifo(target)
                    else:
                        regular = job.parent / "hook-target"
                        regular.write_bytes(payload)
                        target.symlink_to(regular)

    def prune(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Unsafe artifacts must block retention")

    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    monkeypatch.setattr(workflow, "execute_prune_plan", prune)
    if document["backup_mode"] == "combined":
        with pytest.raises(HookError if hook_fails else ArchiveVerificationError) as raised:
            _run(job)
        message = str(raised.value)
        assert failure_exit_code(raised.value) == (11 if hook_fails else 7)
    else:
        result = _run(job)
        assert result.result == "failed"
        message = result.files[0].error
    assert "checksum" in message.lower()
    assert all(archive.is_file() for archive in archives)
    assert not list((job.parent / "backups").rglob(SUSPECT_MARKER_NAME))


@pytest.mark.parametrize("hook_fails", [False, True])
def test_checksum_failure_survives_suspect_marker_write_failure(
    history: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    hook_fails: bool,
) -> None:
    job, archives = history
    document = json.loads(job.read_text())
    document["hooks"]["on_success"] = {"command": "exit 9" if hook_fails else "true"}
    document["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    job.write_text(json.dumps(document))
    (job.parent / "source" / "data").write_text("changed source")
    monkeypatch.setattr(workflow, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    monkeypatch.setattr(hooks, "validate_hook_shell", lambda **kwargs: Path("/bin/sh"))
    original_hook = workflow._run_configured_hook

    def hook(phase: str, *args: Any, **kwargs: Any) -> None:
        try:
            original_hook(phase, *args, **kwargs)
        finally:
            if phase == "on_success":
                (created,) = (
                    path
                    for path in (job.parent / "backups").rglob("*.tar.zst")
                    if path not in archives
                )
                created.write_bytes(b"corrupted by hook")

    def mark(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("marker write denied")

    def prune(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Checksum mismatch must block retention even without a suspect marker")

    monkeypatch.setattr(workflow, "_run_configured_hook", hook)
    monkeypatch.setattr(workflow, "mark_backup_suspect", mark)
    monkeypatch.setattr(workflow, "execute_prune_plan", prune)
    if document["backup_mode"] == "combined":
        with pytest.raises(HookError if hook_fails else ArchiveChecksumMismatchError) as raised:
            _run(job)
        message = str(raised.value)
        assert failure_exit_code(raised.value) == (11 if hook_fails else 7)
    else:
        result = _run(job)
        assert result.result == "failed"
        message = result.files[0].error
    assert "Archive checksum does not match archive content" in message
    assert "cannot record SUSPECT marker:" in message
    assert "marker write denied" in caplog.text
    assert ("on_success hook exited with status 9" in message) == hook_fails
    assert "Cannot record SUSPECT marker after checksum failure:" in caplog.text
    assert "Post-hook backup integrity check failed: Archive checksum does not match" in caplog.text
    assert all(archive.is_file() for archive in archives)
