"""Tests for version reporting."""

from __future__ import annotations

import subprocess
import sys

import pytest

from vaultkeep.cli import main
from vaultkeep.version import installed_version


def test_version_matches_project_metadata() -> None:
    assert installed_version() == "0.1.0.dev0"


def test_version_cli_prints_only_version(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--version"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "0.1.0.dev0\n"
    assert captured.err == ""


def test_module_entry_point_prints_only_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "vaultkeep", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == "0.1.0.dev0\n"
    assert result.stderr == ""
