"""Tests for MS6 command parsing and public error mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vaultkeep.cli.commands import main


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
