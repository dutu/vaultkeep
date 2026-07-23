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
from vaultkeep.errors import PruneError


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


def test_matching_malformed_entry_blocks_pruning(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    config = _config(tmp_path, valid_config)
    _create_backup(config, 1, datetime(2026, 7, 23, 12, tzinfo=UTC))
    broken = tmp_path / "backup-app-20260724T120000Z-00000000000000000000000000000002"
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
    newest = _create_backup(config, 2, datetime(2026, 7, 23, 12, tzinfo=UTC))
    unrelated = tmp_path / "operator-data"
    unrelated.mkdir()
    discovered = discover_backups(config)

    plan = build_prune_plan(discovered, config.retention)
    removed = execute_prune_plan(plan, discovered)

    assert removed == (old,)
    assert not old.exists()
    assert newest.exists()
    assert unrelated.exists()
