"""Severity labels and terminal color selection, including CLI propagation."""

from __future__ import annotations

import io
import sys

import pytest

from vaultkeep.cli import main
from vaultkeep.cli.output import Console


@pytest.mark.parametrize("level,color", [("INFO", "36"), ("WARNING", "1;33"), ("ERROR", "1;31")])
@pytest.mark.parametrize(
    "terminal,term,no_color,flag,colored",
    [
        (True, "xterm", "", False, True),
        (False, "xterm", "", False, False),
        (True, "dumb", "", False, False),
        (True, "xterm", "1", False, False),
        (True, "xterm", "", True, False),
    ],
)
def test_level_and_color_selection(
    monkeypatch, level, color, terminal, term, no_color, flag, colored
):
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: terminal)
    monkeypatch.setenv("TERM", term)
    monkeypatch.setenv("NO_COLOR", no_color)

    Console(no_color=flag).print("first\nsecond", level=level, file=stream)

    prefix = f"\033[{color}m{level}:\033[0m" if colored else f"{level}:"
    assert stream.getvalue() == f"{prefix} first\n{prefix} second\n"


@pytest.mark.parametrize("no_color", [False, True])
def test_validate_colors_and_levels_reach_cli(monkeypatch, no_color):
    from pathlib import Path

    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    config = Path(__file__).parents[2] / "examples/vaultkeep-schedule.yaml.example"

    assert (
        main(
            [
                *(["--no-color"] if no_color else []),
                "--config",
                str(config),
                "validate",
                "--schema-only",
            ]
        )
        == 0
    )

    output = stream.getvalue()
    info = "INFO:" if no_color else "\033[36mINFO:\033[0m"
    warning = "WARNING:" if no_color else "\033[1;33mWARNING:\033[0m"
    assert f"{info} Configuration: valid\n" in output
    assert f"{warning} Runtime checks: skipped (--schema-only)\n" in output
    assert ("\033[" in output) == (not no_color)


def test_invalid_config_has_error_level_on_report_and_diagnostic(tmp_path, capsys):
    config = tmp_path / "invalid.yaml"
    config.write_text("not: valid\n")

    assert main(["--no-color", "--config", str(config), "validate"]) == 3

    output = capsys.readouterr()
    assert output.out == "ERROR: Configuration: invalid\nERROR: Result: failed\n"
    assert output.err.startswith("ERROR: ")
    assert "\033[" not in output.out + output.err


@pytest.mark.parametrize("action", ["install", "update"])
@pytest.mark.parametrize("no_color", [False, True])
def test_trigger_lifecycle_confirmation_uses_colors(monkeypatch, action, no_color):
    from vaultkeep.trigger import cli

    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli, "validate_vaultkeep_executable", lambda config: None)
    monkeypatch.setattr(cli, "runtime_preflight", lambda *args: None)
    monkeypatch.setattr(cli, "state_root", lambda scope: "/unused")
    monkeypatch.setattr(cli, "service_name", lambda config: "test.service")
    calls = []
    monkeypatch.setattr(cli, "manage_service", lambda config, command: calls.append(command))
    # Only the selected scope is needed by lifecycle dispatch here.
    from types import SimpleNamespace

    monkeypatch.setattr(cli, "_load", lambda args: SimpleNamespace(scope="test"))

    assert (
        main(
            [
                *(["--no-color"] if no_color else []),
                "--user",
                "--config",
                "job.yaml",
                "trigger",
                action,
            ]
        )
        == 0
    )
    prefix = "INFO:" if no_color else "\033[36mINFO:\033[0m"
    assert stream.getvalue() == f"{prefix} {action}: test.service\n"
    assert calls == [action]
