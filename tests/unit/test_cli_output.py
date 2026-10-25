"""Severity labels and terminal color selection, including CLI propagation."""

from __future__ import annotations

import io
import sys

import pytest

from vaultkeep.cli import main
from vaultkeep.cli.output import Console


@pytest.mark.parametrize(
    "level,color", [("DEBUG", None), ("INFO", None), ("WARNING", "1;33"), ("ERROR", "1;31")]
)
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

    prefix = f"\033[{color}m{level}\033[0m:" if colored and color else f"{level}:"
    assert stream.getvalue() == f"{prefix} first\n{prefix} second\n"


@pytest.mark.parametrize("no_color", [False, True])
def test_validate_colors_and_levels_reach_cli(monkeypatch, no_color):
    from pathlib import Path

    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    config = Path(__file__).parents[2] / "examples/vaultkeep-schedule.yaml"

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
    valid = "VALID" if no_color else "\033[32mVALID\033[0m"
    assert f"Result:  {valid}\n" in output
    assert "Runtime checks  skipped (--schema-only)\n" in output
    assert "INFO:" not in output and "ERROR:" not in output
    assert ("\033[" in output) == (not no_color)


def test_invalid_config_has_error_level_on_report_and_diagnostic(tmp_path, capsys):
    config = tmp_path / "invalid.yaml"
    config.write_text("not: valid\n")

    assert main(["--no-color", "--config", str(config), "validate"]) == 3

    output = capsys.readouterr()
    assert output.out == (
        "Vaultkeep — Validation\n\nMode:    system\nResult:  FAILED\n"
        "\nConfiguration\n  Configuration  FAILED\n"
    )
    assert output.err.startswith("Issue: ")
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
    monkeypatch.setattr(cli, "runtime_preflight", lambda *args, **kwargs: None)
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
    status = "OK" if no_color else "\033[32mOK\033[0m"
    assert stream.getvalue() == f"Trigger {action}:  {status}\n"
    assert calls == [action]


@pytest.mark.parametrize("terminal", [False, True])
def test_reports_color_only_status_values(monkeypatch, terminal):
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: terminal)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    output = Console()
    output.heading("Test")
    output.fields(
        [
            ("Configuration", "VALID"),
            ("Setup", "SETUP INCOMPLETE"),
            ("Operation", "FAILED"),
            ("Monitor", "RUNNING"),
            ("Path", "/backup/job"),
        ],
        section="Checks",
    )
    rendered = stream.getvalue()
    assert rendered.startswith("Vaultkeep — Test\n\n\nChecks\n")
    assert "  Path           /backup/job\n" in rendered
    for status, color in [
        ("VALID", "32"),
        ("SETUP INCOMPLETE", "1;33"),
        ("FAILED", "1;31"),
        ("RUNNING", "36"),
    ]:
        value = f"\033[{color}m{status}\033[0m" if terminal else status
        assert value + "\n" in rendered
    if not terminal:
        assert "\033" not in rendered


def test_cli_logging_does_not_change_host_logging(caplog):
    import logging
    from pathlib import Path

    logger = logging.getLogger("vaultkeep")
    before = (logger.handlers[:], logger.level, logger.propagate)
    config = Path(__file__).parents[2] / "examples/vaultkeep-schedule.yaml"
    assert main(["--config", str(config), "validate", "--schema-only"]) == 0
    assert (logger.handlers, logger.level, logger.propagate) == before
    logger.warning("host diagnostics still propagate")
    assert "host diagnostics still propagate" in caplog.text
