"""Tests for command parsing and public error mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vaultkeep import workflow
from vaultkeep.cli.commands import main
from vaultkeep.config import JobConfig
from vaultkeep.errors import DestinationError

EXAMPLE_CONFIG = Path(__file__).parents[2] / "examples" / "vaultkeep-job.yaml.disabled"


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

    monkeypatch.setattr(workflow.tempfile, "TemporaryFile", fail_tempfile)

    with pytest.raises(DestinationError, match="Destination root is not writable"):
        workflow._validate_runtime(
            config,
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
