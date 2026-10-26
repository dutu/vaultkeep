"""Selection, naming, and configuration boundaries for independent file backups."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from vaultkeep.backup import workflow
from vaultkeep.config.models import JobConfig
from vaultkeep.errors import DestinationError, SourceDiscoveryError
from vaultkeep.file_backups import file_config, file_destinations, file_key, select_files
from vaultkeep.state.identity import job_identity_hash


def _config(valid_config: dict[str, Any], source: Path, destination: Path) -> JobConfig:
    valid_config["backup_mode"] = "per_file"
    valid_config["sources"][0].update(path=str(source), archive_prefix="wallets")
    valid_config["destination"].update(root=str(destination))
    return JobConfig.model_validate(valid_config)


def test_mode_defaults_to_combined_and_rejects_unsupported_sources(
    valid_config: dict[str, Any],
) -> None:
    assert JobConfig.model_validate(valid_config).backup_mode == "combined"
    valid_config["backup_mode"] = "per_file"
    valid_config["sources"].append(valid_config["sources"][0].copy())
    with pytest.raises(ValidationError, match="exactly one source directory"):
        JobConfig.model_validate(valid_config)
    valid_config["sources"].pop()
    valid_config["source_options"]["follow_symlinks"] = True
    with pytest.raises(ValidationError, match="follow_symlinks: false"):
        JobConfig.model_validate(valid_config)


def test_selection_is_shallow_regular_files_only_and_applies_both_exclusions(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    source = tmp_path / "wallets"
    source.mkdir()
    for name in ("wallet B", "wallet A", "ignored.tmp", "ignored.lock"):
        (source / name).write_text("disposable")
    (source / "nested").mkdir()
    (source / "nested" / "wallet").touch()
    (source / "link").symlink_to(source / "wallet A")
    (source / "broken").symlink_to(source / "absent")
    valid_config["sources"][0]["exclude"] = ["*.lock"]
    config = _config(valid_config, source, tmp_path / "backups")

    assert [path.name for path in select_files(config)] == ["wallet A", "wallet B"]


@pytest.mark.parametrize("kind", ["file", "link", "missing"])
def test_source_must_be_a_real_accessible_directory(
    tmp_path: Path, valid_config: dict[str, Any], kind: str
) -> None:
    source = tmp_path / "wallets"
    if kind == "file":
        source.touch()
    elif kind == "link":
        source.symlink_to(tmp_path, target_is_directory=True)
    config = _config(valid_config, source, tmp_path / "backups")
    with pytest.raises(SourceDiscoveryError):
        select_files(config)
    if kind == "missing":
        valid_config["source_options"]["ignore_missing"] = True
        assert select_files(_config(valid_config, source, tmp_path / "backups")) == ()


def test_names_remain_distinct_bounded_and_share_safe(tmp_path: Path) -> None:
    names = ("Wallet", "wallet", "wallet.", "CON", "duțu", "a" * 255, "a?b", "a:b")
    keys = [file_key(name) for name in names]
    assert len(set(keys)) == len(names)
    assert all(re.fullmatch(r"file-[a-z0-9-]{1,40}-[0-9a-f]{64}", key) for key in keys)
    assert all(len(key) < 255 for key in keys)
    identities = {job_identity_hash(tmp_path / "app.yaml", "app", file_key=key) for key in keys}
    assert len(identities) == len(keys)
    assert job_identity_hash(tmp_path / "app.yaml", "app") not in identities


@pytest.mark.parametrize("prefix", ["wallets", ""])
def test_child_config_preserves_file_name_and_parent_guards(
    tmp_path: Path, valid_config: dict[str, Any], prefix: str
) -> None:
    config = _config(valid_config, tmp_path / "wallets", tmp_path / "backups")
    config = config.model_copy(
        update={"sources": [config.sources[0].model_copy(update={"archive_prefix": prefix})]}
    )
    config = config.model_copy(
        update={
            "destination": config.destination.model_copy(
                update={"marker_file": "marker", "expected_fstype": "cifs"}
            )
        }
    )
    source = tmp_path / "wallets" / "wallet A"
    child = file_config(config, source)
    assert child.sources[0].path == str(source)
    assert child.sources[0].archive_prefix == (f"{prefix}/wallet A" if prefix else "wallet A")
    assert child.destination.root == str(tmp_path / "backups" / file_key("wallet A"))
    assert child.destination.marker_file is None
    assert config.destination.marker_file == "marker"
    assert child.destination.expected_fstype == "cifs"
    assert child.job == config.job


def test_preserve_layout_keeps_absolute_source_path(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    config = _config(valid_config, tmp_path / "wallets", tmp_path / "backups")
    config = config.model_copy(
        update={
            "sources": [
                config.sources[0].model_copy(
                    update={"archive_path_mode": "preserve", "archive_prefix": None}
                )
            ]
        }
    )
    child = file_config(config, tmp_path / "wallets" / "wallet")
    assert child.sources[0].archive_prefix is None
    assert child.sources[0].archive_path_mode == "preserve"


def test_discovery_ignores_unrelated_entries_and_refuses_managed_symlinks(
    tmp_path: Path, valid_config: dict[str, Any]
) -> None:
    config = _config(valid_config, tmp_path / "missing-source", tmp_path)
    (tmp_path / "unrelated").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "file-looks-managed").touch()
    assert file_destinations(config) == ()
    (tmp_path / file_key("wallet")).symlink_to(tmp_path, target_is_directory=True)
    from vaultkeep.destination.discovery import discover_backups

    (destination,) = file_destinations(config)
    with pytest.raises(DestinationError, match="not a real directory"):
        discover_backups(destination, metadata_only=True)


def test_early_file_failure_reports_failure_before_continuing(
    tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "wallets"
    destination = tmp_path / "backups"
    source.mkdir()
    destination.mkdir()
    for name in ("a", "b"):
        (source / name).touch()
    config = _config(valid_config, source, destination)
    error = DestinationError("file destination unavailable")
    events: list[tuple[str, BaseException | None]] = []

    def run_single(config_path, child, **kwargs):
        if child.sources[0].path == str(source / "a"):
            raise error
        return workflow.CommandResult("run", "unchanged")

    monkeypatch.setattr(workflow, "_run_single_backup", run_single)
    result = workflow._run_file_backups(
        tmp_path / "app.yaml",
        config,
        runtime_params=None,
        paths=workflow.WorkflowPaths(tmp_path / "state", tmp_path / "cache", tmp_path / "locks"),
        user_mode=True,
        progress=lambda stage: events.append((stage, sys.exception())),
    )

    assert result.result == "failed"
    assert [outcome.result for outcome in result.files] == ["failed", "unchanged"]
    assert events[-2:] == [("Handling backup failure", error), ("Backing up 'b'", None)]
