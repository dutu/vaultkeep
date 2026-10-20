"""Tests for command parsing and public error mapping."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vaultkeep import workflow
from vaultkeep.cli.commands import main
from vaultkeep.config import JobConfig
from vaultkeep.destination import finalize as finalize_module
from vaultkeep.errors import DestinationError

EXAMPLE_CONFIG = Path(__file__).parents[2] / "examples" / "vaultkeep-schedule.yaml.example"


def test_version_does_not_require_a_command(capsys: Any) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip()


def test_schema_only_validation_accepts_disabled_template(capsys: Any) -> None:
    assert main(["--config", str(EXAMPLE_CONFIG), "validate", "--schema-only"]) == 0
    assert capsys.readouterr().out.strip() == "validate: valid"


def test_schema_only_validation_uses_config_path(tmp_path: Path, capsys: Any) -> None:
    config = tmp_path / "app.yaml"
    lines = ["config_version: 1", "job:", "  id: app"]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert main(["--config", str(config), "validate", "--schema-only"]) == 3
    assert "Configuration contains" in capsys.readouterr().err


def test_invalid_command_configuration_maps_to_exit_three(tmp_path: Path, capsys: Any) -> None:
    config = tmp_path / "app.yaml"
    config.write_text("not: a-complete-config\n", encoding="utf-8")

    assert main(["--config", str(config), "validate", "--schema-only"]) == 3
    assert "Configuration contains" in capsys.readouterr().err


def test_runtime_validation_probes_destination_writability(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_data = {
        **valid_config,
        "destination": {
            **valid_config["destination"],
            "root": str(tmp_path),
            "mount_point": None,
        },
    }
    config = JobConfig.model_validate(config_data)

    def fail_tempfile(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(finalize_module.tempfile, "mkdtemp", fail_tempfile)

    with pytest.raises(DestinationError, match="cannot create a probe directory: denied"):
        workflow._validate_runtime(
            config,
            require_sources=False,
            require_writable_destination=True,
        )


@pytest.fixture
def local_job(tmp_path: Path, valid_config: dict[str, Any]) -> Path:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    valid_config["sources"][0]["path"] = str(source)
    valid_config["destination"]["root"] = str(destination)
    valid_config["destination"]["mount_point"] = None
    config = tmp_path / "app.yaml"
    config.write_text(json.dumps(valid_config), encoding="utf-8")
    return config


def test_full_validation_probes_destination_and_leaves_no_artifacts(
    local_job: Path, capsys: Any
) -> None:
    assert main(["--config", str(local_job), "validate"]) == 0
    assert capsys.readouterr().out.strip() == "validate: valid"
    assert list((local_job.parent / "destination").iterdir()) == []


@pytest.mark.parametrize("command", ["validate", "run"])
def test_validation_and_backup_reject_denied_atomic_rename_before_archiving(
    local_job: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any, command: str
) -> None:
    def deny_rename(source: Path, destination: Path) -> None:
        raise PermissionError("rename denied")

    def unexpected_archive(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Archive creation must not start after a failed destination probe")

    monkeypatch.setattr(finalize_module, "_rename_noreplace", deny_rename)
    monkeypatch.setattr(workflow, "build_archive", unexpected_archive)
    monkeypatch.setenv("XDG_STATE_HOME", str(local_job.parent / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(local_job.parent / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(local_job.parent / "runtime"))

    assert main(["--user", "--config", str(local_job), command]) == 5
    error = capsys.readouterr().err
    assert "cannot atomically rename a probe directory: rename denied" in error
    assert str(local_job.parent / "destination") in error
    assert list((local_job.parent / "destination").iterdir()) == []


def test_schema_only_validation_never_probes_destination(
    local_job: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_probe(root: Path) -> None:
        pytest.fail("Schema-only validation must not probe the destination")

    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)
    (local_job.parent / "destination").rmdir()

    assert main(["--config", str(local_job), "validate", "--schema-only"]) == 0


@pytest.mark.parametrize("command", [["list"], ["verify"], ["prune", "--dry-run"]])
def test_read_only_commands_never_probe_destination(
    local_job: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    def unexpected_probe(root: Path) -> None:
        pytest.fail("Read-only commands must not probe the destination")

    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)

    assert main(["--config", str(local_job), *command]) == 0
    assert list((local_job.parent / "destination").iterdir()) == []


@pytest.mark.parametrize("malformed", [False, True])
def test_verify_reports_stages_without_changing_verification_result(
    local_job: Path, malformed: bool
) -> None:
    if malformed:
        (local_job.parent / "destination" / ("backup-app-20260724T120000Z-" + "0" * 32)).mkdir()
    stages: list[str] = []

    if malformed:
        with pytest.raises(DestinationError, match="malformed"):
            workflow.verify_backups(local_job, progress=stages.append)
    else:
        assert workflow.verify_backups(local_job, progress=stages.append) == workflow.CommandResult(
            "verify", "verified", backups=0
        )

    assert stages == [
        "Loading and validating configuration",
        "Checking destination",
        "Verifying backup manifests and checksums",
    ]


def test_runtime_validation_checks_marker_before_writing_probe(
    tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    valid_config["destination"].update(
        root=str(tmp_path), mount_point=None, marker_file="missing-marker"
    )

    def unexpected_probe(root: Path) -> None:
        pytest.fail("Missing marker must prevent destination writes")

    monkeypatch.setattr(workflow, "probe_destination_writability", unexpected_probe)

    with pytest.raises(DestinationError, match="marker is missing"):
        workflow._validate_runtime(
            JobConfig.model_validate(valid_config),
            require_sources=False,
            require_writable_destination=True,
        )


def test_runtime_validation_accepts_destination_below_mounted_parent(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount_point = tmp_path / "share"
    root = mount_point / "app"
    root.mkdir(parents=True)
    config_data = {
        **valid_config,
        "destination": {
            **valid_config["destination"],
            "root": str(root),
            "mount_point": str(mount_point),
        },
    }
    config = JobConfig.model_validate(config_data)

    monkeypatch.setattr(workflow.os.path, "ismount", lambda path: Path(path) == mount_point)

    workflow._validate_runtime(
        config,
        require_sources=False,
        require_writable_destination=False,
    )


def test_runtime_validation_rejects_unmounted_configured_mount_point(
    tmp_path: Path,
    valid_config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount_point = tmp_path / "share"
    root = mount_point / "app"
    root.mkdir(parents=True)
    config_data = {
        **valid_config,
        "destination": {
            **valid_config["destination"],
            "root": str(root),
            "mount_point": str(mount_point),
        },
    }
    config = JobConfig.model_validate(config_data)

    monkeypatch.setattr(workflow.os.path, "ismount", lambda path: False)

    with pytest.raises(DestinationError, match="Configured mount point is not mounted"):
        workflow._validate_runtime(
            config,
            require_sources=False,
            require_writable_destination=False,
        )


def test_user_workflow_paths_use_xdg_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    paths = workflow.user_workflow_paths()

    assert paths.state_root == tmp_path / "state" / "vaultkeep" / "jobs"
    assert paths.local_temp_root == tmp_path / "cache" / "vaultkeep" / "tmp"
    assert paths.lock_root == tmp_path / "runtime" / "vaultkeep" / "locks"
