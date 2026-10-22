"""Tests for template-derived manifests, discovery, and guarded pruning."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from vaultkeep.archive.checksums import calculate_archive_sha256, write_checksum_sidecar
from vaultkeep.config.models import JobConfig
from vaultkeep.destination.discovery import discover_backups
from vaultkeep.destination.manifest import BackupManifest, write_manifest
from vaultkeep.destination.pruning import build_prune_plan, execute_prune_plan
from vaultkeep.destination.templates import allocate_job_backup_paths, create_staging_directory
from vaultkeep.errors import DestinationTemplateError, PruneError


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
            archive_format="tar.zst",
            encrypted=False,
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
