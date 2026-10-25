"""Tests for template-derived manifests, discovery, and guarded pruning."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.archive import LoadedPassword, PasswordSecret
from vaultkeep.archive import checksums as checksums_module
from vaultkeep.archive.checksums import calculate_archive_sha256, write_checksum_sidecar
from vaultkeep.backup import workflow
from vaultkeep.backup.workflow import list_backups, prune_backups, verify_backups
from vaultkeep.config.models import JobConfig
from vaultkeep.destination.discovery import discover_backups
from vaultkeep.destination.manifest import BackupManifest, write_manifest
from vaultkeep.destination.pruning import build_prune_plan, execute_prune_plan
from vaultkeep.destination.templates import allocate_job_backup_paths, create_staging_directory
from vaultkeep.errors import (
    ArchiveVerificationError,
    DestinationError,
    DestinationTemplateError,
    PasswordFileError,
    PruneError,
)
from vaultkeep.file_backups import file_config
from vaultkeep.progress import VerificationProgress
from vaultkeep.state.models import CredentialFingerprint


def _config(tmp_path: Path, valid_config: dict[str, Any]) -> JobConfig:
    valid_config["destination"]["root"] = str(tmp_path)
    valid_config["retention"] = {"hourly": 0, "daily": 1, "weekly": 0, "monthly": 0, "yearly": 0}
    return JobConfig.model_validate(valid_config)


def _create_backup(config: JobConfig, number: int, created_at: datetime) -> Path:
    backup_id = f"{number:032x}"
    paths = allocate_job_backup_paths(
        config.destination,
        job_id=config.job.id,
        backup_id=backup_id,
        hostname="host",
        created_at=created_at,
        source_digest="sha256:" + "a" * 64,
        archive_format=config.archive.format,
    )
    create_staging_directory(paths)
    paths.archive_path.write_bytes(f"archive-{number}".encode())
    digest = calculate_archive_sha256(paths.archive_path)
    write_checksum_sidecar(paths.archive_path, paths.checksum_path, digest)
    write_manifest(
        paths.manifest_path,
        BackupManifest(
            application_version="0.1.0.dev0",
            backup_id=backup_id,
            job=config.job.id,
            created_at=created_at.isoformat(timespec="seconds"),
            created_at_utc=created_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            source_digest="sha256:" + "a" * 64,
            config_fingerprint="sha256:" + "b" * 64,
            archive=paths.archive_path.name,
            archive_digest=f"sha256:{digest}",
            archive_format=config.archive.format,
            encrypted=config.archive.format == "tar.7z",
            hostname="host",
        ),
    )
    paths.staging_directory.rename(paths.final_directory)
    return paths.final_directory


def test_discovery_returns_valid_records_and_ignores_unrelated(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    config = _config(tmp_path, valid_config)
    _create_backup(config, 1, datetime(2026, 7, 23, 12, tzinfo=UTC))
    (tmp_path / "operator-data").mkdir()
    (tmp_path / ".partial-vaultkeep-app-orphan").mkdir()

    discovered = discover_backups(config)

    assert len(discovered.backups) == 1
    assert discovered.malformed == ()
    assert discovered.state_records[0].job_id == "app"


@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
def test_verify_checks_all_archives_and_reports_all_failures(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    backup_mode: str,
) -> None:
    valid_config["backup_mode"] = backup_mode
    config = _config(tmp_path, valid_config)
    destinations = (
        [file_config(config, Path("/missing-source") / name) for name in ("one", "two")]
        if backup_mode == "per_file"
        else [config, config]
    )
    archives = []
    malformed = []
    for number, destination in enumerate(destinations, start=1):
        root = Path(destination.destination.root)
        root.mkdir(exist_ok=True)
        directory = _create_backup(destination, number, datetime(2026, 7, 23, number, tzinfo=UTC))
        archives.append(next(directory.glob("*.tar.zst")))
        bad = root / f"backup-app-20260723T{number:02}3000Z"
        bad.mkdir()
        malformed.append(bad)
    good_directory = _create_backup(destinations[-1], 3, datetime(2026, 7, 23, 3, tzinfo=UTC))
    good = next(good_directory.glob("*.tar.zst"))
    config_path = tmp_path / "app.yaml"
    config_path.write_text(config.model_dump_json())
    inspected = []
    verified = []

    def inspect(path: Path, *, tools: Any, on_progress: Any) -> None:
        inspected.append(path)
        if path != good:
            raise ArchiveVerificationError(f"invalid stream {path.name}")

    monkeypatch.setattr(workflow, "verify_tar_zstd", inspect)

    with pytest.raises(ArchiveVerificationError) as caught:
        verify_backups(config_path, on_verified=verified.append)

    assert set(inspected) == {*archives, good}
    assert verified == [good]
    for path in (*archives, *malformed):
        assert str(path) in str(caught.value)
    assert "invalid stream" in str(caught.value)
    assert "malformed backup" in str(caught.value)


@pytest.mark.parametrize("user_mode", [False, True])
@pytest.mark.parametrize("outcome", ["verified", "failed", "interrupted"])
def test_verify_loads_one_password_for_all_backups_and_always_clears_it(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    user_mode: bool,
    outcome: str,
) -> None:
    valid_config["archive"]["format"] = "tar.7z"
    valid_config["encryption"] = {
        "mode": "password",
        "password_file": str(tmp_path / "password"),
    }
    config = _config(tmp_path, valid_config)
    for number in (1, 2):
        _create_backup(config, number, datetime(2026, 7, 23, number, tzinfo=UTC))
    config_path = tmp_path / "app.yaml"
    config_path.write_text(config.model_dump_json())
    secret = PasswordSecret(b"secret")
    fingerprint = CredentialFingerprint(device=1, inode=2, size=6, mtime_ns=3, ctime_ns=4)
    loaded = []
    inspected = []
    monkeypatch.setattr(workflow.os, "geteuid", lambda: 1234)

    def load(path: Path, *, owner_uid: int, owner_gid: int | None) -> LoadedPassword:
        loaded.append((path, owner_uid, owner_gid))
        return LoadedPassword(secret, fingerprint)

    def inspect(
        path: Path, *, job_id: str, password: PasswordSecret, tools: Any, on_progress: Any
    ) -> None:
        assert job_id == "app"
        assert password is secret
        assert password.terminal_input() == b"secret\n"
        inspected.append(path)
        if outcome == "failed":
            raise ArchiveVerificationError("Wrong password")
        if outcome == "interrupted":
            raise KeyboardInterrupt

    monkeypatch.setattr(workflow, "load_password_file", load)
    monkeypatch.setattr(workflow, "verify_tar_7z", inspect)
    if outcome == "verified":
        assert verify_backups(config_path, user_mode=user_mode).backups == 2
    else:
        error = ArchiveVerificationError if outcome == "failed" else KeyboardInterrupt
        with pytest.raises(error):
            verify_backups(config_path, user_mode=user_mode)
    assert loaded == [(tmp_path / "password", 1234 if user_mode else 0, None if user_mode else 0)]
    assert len(inspected) == (1 if outcome == "interrupted" else 2)
    with pytest.raises(PasswordFileError, match="cleared"):
        secret.terminal_input()


@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
def test_list_never_reads_archive_contents_but_verify_and_prune_still_check_them(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    backup_mode: str,
) -> None:
    valid_config["backup_mode"] = backup_mode
    config = _config(tmp_path, valid_config)
    destination = (
        file_config(config, tmp_path / "missing-source" / "wallet")
        if backup_mode == "per_file"
        else config
    )
    Path(destination.destination.root).mkdir(exist_ok=True)
    directory = _create_backup(destination, 1, datetime(2026, 7, 23, 12, tzinfo=UTC))
    archive = next(directory.glob("*.tar.zst"))
    archive.write_bytes(b"corrupted archive")
    config_path = tmp_path / "app.yaml"
    config_path.write_text(config.model_dump_json())
    stages: list[str] = []

    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == archive:
            pytest.fail("Listing must never open an archive")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", guarded_open)
        result, backups = list_backups(config_path, progress=stages.append)

    assert result.backups == 1
    assert backups[0].archive_path == archive
    assert stages == [
        "Loading and validating configuration",
        "Checking destination",
        "Listing backup metadata",
    ]
    with pytest.raises(DestinationError, match="malformed"):
        verify_backups(config_path)
    with pytest.raises(PruneError, match="malformed"):
        prune_backups(config_path, dry_run=False)
    assert archive.read_bytes() == b"corrupted archive"


@pytest.mark.parametrize("verify_archives", [False, True])
@pytest.mark.parametrize(
    "damage",
    ["checksum-format", "checksum-digest", "archive-missing", "archive-link", "extra-file"],
)
def test_discovery_preserves_metadata_checks_without_archive_verification(
    tmp_path: Path, valid_config: dict[str, Any], verify_archives: bool, damage: str
) -> None:
    config = _config(tmp_path, valid_config)
    directory = _create_backup(config, 1, datetime(2026, 7, 23, 12, tzinfo=UTC))
    archive = next(directory.glob("*.tar.zst"))
    sidecar = next(directory.glob("*.sha256"))
    if damage == "checksum-format":
        sidecar.write_text("invalid checksum\n")
    elif damage == "checksum-digest":
        sidecar.write_text(f"{'0' * 64}  {archive.name}\n")
    elif damage == "archive-missing":
        archive.unlink()
    elif damage == "archive-link":
        target = tmp_path / "unrelated-archive"
        archive.rename(target)
        archive.symlink_to(target)
    else:
        (directory / "extra.txt").touch()

    discovered = discover_backups(config, verify_archives=verify_archives)

    assert discovered.backups == ()
    assert len(discovered.malformed) == 1


def test_default_discovery_still_recomputes_archive_checksums(
    tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, valid_config)
    directory = _create_backup(config, 1, datetime(2026, 7, 23, 12, tzinfo=UTC))
    archive = next(directory.glob("*.tar.zst"))
    calculated: list[Path] = []

    def calculate(path: Path) -> str:
        calculated.append(path)
        return "0" * 64

    monkeypatch.setattr(checksums_module, "calculate_archive_sha256", calculate)

    discovered = discover_backups(config)

    assert calculated == [archive]
    assert discovered.backups == ()
    assert "does not match archive content" in discovered.malformed[0].reason


def test_discovery_ignores_staging_even_when_it_matches_the_final_template(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    valid_config["destination"]["name_template"] = "{timestamp_utc}"
    config = _config(tmp_path, valid_config)
    committed = _create_backup(config, 1, datetime(2026, 7, 23, 12, tzinfo=UTC))
    for name in ("~$.partial-vaultkeep-app-orphan", ".partial-vaultkeep-app-orphan"):
        (tmp_path / name).mkdir()

    discovered = discover_backups(config)

    assert [backup.directory for backup in discovered.backups] == [committed]
    assert discovered.malformed == ()


@pytest.mark.parametrize(
    "template",
    [
        "backup-{job}-{timestamp_utc:%Y%m%dT%H%M%SZ}",
        "backup-{job}-{timestamp_utc}",
        "backup-{job}-{timestamp_utc:%Y%m%d}-{hostname}-2",
    ],
)
def test_same_name_backups_use_numbered_directories_and_unchanged_artifact_names(
    tmp_path: Path, valid_config: dict[str, Any], template: str
) -> None:
    valid_config["destination"]["name_template"] = template
    config = _config(tmp_path, valid_config)
    created_at = datetime(2026, 7, 23, 12, tzinfo=UTC)
    first = _create_backup(config, 1, created_at)
    second = _create_backup(config, 2, created_at)
    third = _create_backup(config, 3, created_at)

    assert second.name == f"{first.name}-2"
    assert third.name == f"{first.name}-3"
    expected = {f"{first.name}{suffix}" for suffix in (".tar.zst", ".tar.zst.sha256", ".json")}
    for directory in (first, second, third):
        assert {child.name for child in directory.iterdir()} == expected
    discovered = discover_backups(config)
    assert discovered.malformed == ()
    assert {backup.directory for backup in discovered.backups} == {first, second, third}
    assert {record.backup_id for record in discovered.state_records} == {
        f"{number:032x}" for number in (1, 2, 3)
    }


def test_allocation_skips_files_directories_and_dangling_symlinks(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    config = _config(tmp_path, valid_config)
    base_name = "backup-app-20260723T120000Z"
    (tmp_path / base_name).write_bytes(b"preserve")
    (tmp_path / f"{base_name}-2").mkdir()
    (tmp_path / f"{base_name}-3").symlink_to(tmp_path / "missing")

    paths = allocate_job_backup_paths(
        config.destination,
        job_id="app",
        backup_id="a" * 32,
        hostname="host",
        created_at=datetime(2026, 7, 23, 12, tzinfo=UTC),
        source_digest="sha256:" + "a" * 64,
        archive_format="tar.zst",
    )

    assert paths.final_directory == tmp_path / f"{base_name}-4"
    assert paths.staging_directory == tmp_path / f"~$.partial-vaultkeep-app-{'a' * 32}"
    assert (tmp_path / base_name).read_bytes() == b"preserve"
    assert (tmp_path / f"{base_name}-2").is_dir()
    assert (tmp_path / f"{base_name}-3").is_symlink()
    # A late collision must still be rejected without overwriting the new entry.
    paths.final_directory.mkdir()
    with pytest.raises(DestinationTemplateError, match="already exists"):
        create_staging_directory(paths)
    assert not paths.staging_directory.exists()


@pytest.mark.parametrize("legacy_id", ["0" * 31 + "1", "a" * 32])
def test_discovery_preserves_legacy_directory_identity_checks(
    tmp_path: Path, valid_config: dict[str, Any], legacy_id: str
) -> None:
    config = _config(tmp_path, valid_config)
    directory = _create_backup(config, 1, datetime(2026, 7, 23, 12, tzinfo=UTC))
    legacy = directory.with_name(f"{directory.name}-{legacy_id}")
    directory.rename(legacy)

    discovered = discover_backups(config)

    if legacy_id == "0" * 31 + "1":
        assert discovered.malformed == ()
        assert discovered.backups[0].directory == legacy
    else:
        assert discovered.backups == ()
        assert "identity" in discovered.malformed[0].reason


@pytest.mark.parametrize("suffix", ["", "-2", "-10", "-" + "a" * 32])
def test_matching_malformed_entry_blocks_pruning(
    tmp_path: Path, valid_config: dict[str, Any], suffix: str
) -> None:
    config = _config(tmp_path, valid_config)
    _create_backup(config, 1, datetime(2026, 7, 23, 12, tzinfo=UTC))
    broken = tmp_path / f"backup-app-20260724T120000Z{suffix}"
    broken.mkdir()

    discovered = discover_backups(config)

    assert len(discovered.malformed) == 1
    with pytest.raises(PruneError, match="malformed"):
        build_prune_plan(discovered, config.retention)


def test_prune_removes_only_valid_unselected_backup(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    config = _config(tmp_path, valid_config)
    old = _create_backup(config, 1, datetime(2026, 7, 22, 12, tzinfo=UTC))
    legacy = old.with_name(f"{old.name}-{1:032x}")
    old.rename(legacy)
    old = legacy
    same_name = _create_backup(config, 2, datetime(2026, 7, 23, 12, tzinfo=UTC))
    newest = _create_backup(config, 3, datetime(2026, 7, 23, 12, tzinfo=UTC))
    unrelated = tmp_path / "operator-data"
    unrelated.mkdir()
    discovered = discover_backups(config)

    plan = build_prune_plan(discovered, config.retention)
    removed = execute_prune_plan(plan, discovered)

    assert set(removed) == {old, same_name}
    assert not old.exists()
    assert newest.exists()
    assert unrelated.exists()


def test_discovery_progress_tracks_backups_bytes_and_manifest_failure(
    tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, valid_config)
    directories = [
        _create_backup(config, n, datetime(2026, 7, 23, n, tzinfo=UTC)) for n in (1, 2, 3)
    ]
    next(directories[-1].glob("*.json")).write_text("{invalid")
    archives = {next(directory.glob("*.tar.zst")) for directory in directories[:2]}
    events: list[VerificationProgress] = []
    reads: list[Path] = []
    original_open = Path.open

    def track(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path in archives:
            reads.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track)
    monkeypatch.setattr(checksums_module, "_CHUNK_SIZE", 3)
    discovered = discover_backups(config, on_progress=events.append)

    assert len(discovered.backups) == 2
    assert len(discovered.malformed) == 1
    assert events[0].total == 3
    assert len(reads) == 2 and set(reads) == archives
    starts = [
        e
        for e in events
        if e.operation == "Manifest/directory validation" and e.status == "running"
    ]
    assert [e.index for e in starts] == [1, 2, 3]
    assert {e.path for e in starts} == set(directories)
    for archive in archives:
        updates = [e for e in events if e.path == archive and e.bytes_processed is not None]
        assert [e.bytes_processed for e in updates] == [0, 3, 6, 9]
        assert all(e.bytes_total == 9 and e.total == 3 for e in updates)
        assert len({e.index for e in updates}) == 1
    failures = [e for e in events if e.status == "failed"]
    assert len(failures) == 1
    assert failures[0].operation == "Manifest/directory validation"
    assert failures[0].path == directories[-1]
    assert failures[0].error
    assert len([e for e in events if e.operation == "SHA-256" and e.status == "ok"]) == 2


@pytest.mark.parametrize("damage", ["content", "manifest-digest", "sidecar"])
def test_checksum_progress_reports_failure_without_claiming_success(
    tmp_path: Path, valid_config: dict[str, Any], damage: str
) -> None:
    import json

    config = _config(tmp_path, valid_config)
    directory = _create_backup(config, 1, datetime(2026, 7, 23, 1, tzinfo=UTC))
    archive = next(directory.glob("*.tar.zst"))
    if damage == "content":
        archive.write_bytes(b"corrupt")
    elif damage == "sidecar":
        next(directory.glob("*.sha256")).write_text("invalid")
    else:
        manifest = next(directory.glob("*.json"))
        data = json.loads(manifest.read_text())
        data["archive_digest"] = "sha256:" + "0" * 64
        manifest.write_text(json.dumps(data))
    events: list[VerificationProgress] = []
    result = discover_backups(config, on_progress=events.append)
    assert len(result.malformed) == 1
    assert events[-1].status == "failed"
    assert events[-1].operation == "SHA-256"
    assert events[-1].path == archive
    assert not any(e.operation == "SHA-256" and e.status == "ok" for e in events)


@pytest.mark.parametrize("backup_mode", ["combined", "per_file"])
@pytest.mark.parametrize("failure", [None, "stream", "tar", "manifest", "discovery"])
def test_verification_phase_totals_and_operation_failures(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    backup_mode: str,
    failure: str | None,
) -> None:
    from vaultkeep.archive import verification
    from vaultkeep.errors import ArchiveCreationError

    valid_config["backup_mode"] = backup_mode
    config = _config(tmp_path, valid_config)
    destinations = (
        [file_config(config, Path("/missing") / name) for name in ("one", "two")]
        if backup_mode == "per_file"
        else [config, config]
    )
    directories = []
    for n, destination in enumerate(destinations, 1):
        Path(destination.destination.root).mkdir(exist_ok=True)
        directories.append(_create_backup(destination, n, datetime(2026, 7, 23, n, tzinfo=UTC)))
    bad = next(directories[0].glob("*.tar.zst"))
    if failure == "manifest":
        next(directories[0].glob("*.json")).write_text("{}")
    config_path = tmp_path / "app.yaml"
    config_path.write_text(config.model_dump_json())
    events: list[VerificationProgress] = []
    calls: list[str] = []
    original_discover = workflow.discover_backups

    def discover(destination: JobConfig, **kwargs: Any) -> Any:
        if (
            failure == "discovery"
            and destination.destination.root == destinations[0].destination.root
        ):
            raise DestinationError("Cannot list destination")
        return original_discover(destination, **kwargs)

    def command(args: Any, **kwargs: Any) -> None:
        assert events[-1].operation == "Zstandard stream test"
        calls.append("stream")
        if failure == "stream" and args[-1] == bad:
            raise ArchiveCreationError("invalid compressed bytes")

    def pipeline(producer: Any, consumer: Any, **kwargs: Any) -> None:
        assert events[-1].operation == "TAR inspection (Zstandard decompression)"
        calls.append("tar")
        listing = b"../unsafe\n" if failure == "tar" and producer[-1] == bad else b"safe/path\n"
        kwargs["consumer_stdout"].write(listing)

    monkeypatch.setattr(workflow, "discover_backups", discover)
    monkeypatch.setattr(verification, "run_command", command)
    monkeypatch.setattr(verification, "run_pipeline", pipeline)
    if failure:
        error_type = ArchiveVerificationError if failure in ("stream", "tar") else DestinationError
        with pytest.raises(error_type):
            verify_backups(config_path, on_progress=events.append)
    else:
        assert verify_backups(config_path, on_progress=events.append).backups == 2
    discovery_done = next(
        i for i, e in enumerate(events) if e.phase == "discovery" and e.status == "complete"
    )
    assert all(e.phase != "archive" for e in events[:discovery_done])
    assert all(e.phase != "backup" for e in events[discovery_done:])
    discovery = events[discovery_done]
    eligible = (
        1
        if failure == "manifest" or (failure == "discovery" and backup_mode == "per_file")
        else 0
        if failure == "discovery"
        else 2
    )
    assert discovery.completed == eligible
    assert discovery.total == (None if failure == "discovery" else 2)
    assert events[-1].phase == "archive" and events[-1].status == "complete"
    assert events[-1].total == eligible
    assert events[-1].completed == eligible - (failure in ("stream", "tar"))
    if failure in ("stream", "tar"):
        failed = next(e for e in events if e.status == "failed")
        assert failed.path == bad and failed.total == 2
        assert failed.operation == (
            "Zstandard stream test"
            if failure == "stream"
            else "TAR inspection (Zstandard decompression)"
        )
    assert calls.count("stream") == eligible
    assert calls.count("tar") == eligible - (failure == "stream")


@pytest.mark.parametrize("corrupt", [False, True])
def test_progress_callback_errors_preserve_verification_result(
    tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch, corrupt: bool
) -> None:
    from vaultkeep.archive import verification

    config = _config(tmp_path, valid_config)
    directory = _create_backup(config, 1, datetime(2026, 7, 23, 1, tzinfo=UTC))
    if corrupt:
        next(directory.glob("*.tar.zst")).write_bytes(b"bad")
    config_path = tmp_path / "app.yaml"
    config_path.write_text(config.model_dump_json())

    def broken(*args: Any) -> None:
        raise RuntimeError("renderer unavailable")

    monkeypatch.setattr(verification, "run_command", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        verification,
        "run_pipeline",
        lambda *args, **kwargs: kwargs["consumer_stdout"].write(b"safe\n"),
    )
    if corrupt:
        with pytest.raises(DestinationError, match="does not match archive content"):
            verify_backups(config_path, progress=broken, on_progress=broken, on_verified=broken)
    else:
        assert (
            verify_backups(
                config_path, progress=broken, on_progress=broken, on_verified=broken
            ).backups
            == 1
        )
