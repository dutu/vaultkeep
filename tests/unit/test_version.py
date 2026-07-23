"""Tests for version reporting."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from vaultkeep.cli import main
from vaultkeep.version import installed_version

ROOT = Path(__file__).resolve().parents[2]


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as file:
        data = tomllib.load(file)
    return str(data["project"]["version"])


def test_version_matches_project_metadata() -> None:
    assert installed_version() == project_version()


def test_version_cli_prints_only_version(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == f"{installed_version()}\n"
    assert captured.err == ""


def test_module_entry_point_prints_only_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "vaultkeep", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == f"{installed_version()}\n"
    assert result.stderr == ""
