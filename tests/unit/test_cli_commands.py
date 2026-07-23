"""Tests for command parsing and public error mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vaultkeep import workflow
from vaultkeep.cli.commands import main
from vaultkeep.config import JobConfig
from vaultkeep.errors import DestinationError


def test_version_does_not_require_a_command(capsys: Any) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip()


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
