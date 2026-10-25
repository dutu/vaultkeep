"""Terminal progress, heartbeat lifetime, and CLI output compatibility."""

from __future__ import annotations

import io
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from vaultkeep.backup.workflow import CommandResult
from vaultkeep.cli import commands
from vaultkeep.cli import progress as progress_module
from vaultkeep.errors import DestinationError


class TerminalOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.heartbeat_seen = Event()
        self.frame_seen = Event()

    def isatty(self) -> bool:
        return True

    def write(self, message: str) -> int:
        written = super().write(message)
        if "(still running)" in message:
            self.heartbeat_seen.set()
        if message.startswith("\r\033[2K") and len(message) > 5:
            self.frame_seen.set()
        return written


@pytest.fixture
def progress_threads(monkeypatch: pytest.MonkeyPatch) -> list[Thread]:
    threads: list[Thread] = []

    def create_thread(**kwargs: Any) -> Thread:
        thread = Thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(progress_module, "_REFRESH_SECONDS", 0.01)
    monkeypatch.setattr(progress_module, "Thread", create_thread)
    return threads


def test_long_stage_repeats_with_elapsed_time_and_stops_on_exit(
    monkeypatch: pytest.MonkeyPatch, progress_threads: list[Thread]
) -> None:
    output = TerminalOutput()
    now = [0.0]
    monkeypatch.setattr(progress_module.sys, "stderr", output)
    monkeypatch.setattr(progress_module, "monotonic", lambda: now[0])
    monkeypatch.setattr(progress_module, "_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setenv("TERM", "dumb")

    with progress_module.CliProgress() as progress:
        progress.update("Hashing source files")
        assert output.getvalue() == "[00:00] Hashing source files\n"
        now[0] = 62.0
        assert output.heartbeat_seen.wait(timeout=2)

    assert "[01:02] Hashing source files (still running)\n" in output.getvalue()
    assert len(progress_threads) == 1
    assert not progress_threads[0].is_alive()


@pytest.mark.parametrize("result", ["created", "unchanged", "failed"])
def test_terminal_run_reports_progress_and_preserves_final_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    progress_threads: list[Thread],
    result: str,
) -> None:
    output = TerminalOutput()
    monkeypatch.setattr(progress_module.sys, "stderr", output)
    monkeypatch.setattr(commands, "monotonic", iter([10.0, 12.5]).__next__)

    def run(config: Path, **kwargs: Any) -> CommandResult:
        assert config == Path("app.yaml")
        kwargs["progress"]("Checking existing backups")
        return CommandResult(
            "run", result, removed=2, archive_path=Path("/backups/app.tar.zst"), archive_size=1536
        )

    monkeypatch.setattr(commands, "run_backup", run)

    assert commands.main(["--config", "app.yaml", "run"]) == (7 if result == "failed" else 0)
    assert capsys.readouterr().out == f"run: {result}\n"
    assert "Checking existing backups" in output.getvalue()
    assert "\r\033[2K" in output.getvalue()
    completion = {
        "created": "SUCCESS: Backup complete",
        "unchanged": "SUCCESS: Backup unchanged",
        "failed": "FAILED: Backup failed",
    }[result]
    assert completion + "\n" in output.getvalue()
    if result == "created":
        assert output.getvalue().endswith(
            "Archive:                       /backups/app.tar.zst\n"
            "Size:                          1.5 KiB\n"
            "Duration:                      2.5s\n"
            "Backups removed by retention:  2\n"
        )
    else:
        assert "Archive:" not in output.getvalue()
        assert "Duration:" not in output.getvalue()
    assert not progress_threads[0].is_alive()


@pytest.mark.parametrize(
    ("command", "handler", "result", "stage"),
    [
        ("list", "list_backups", "listed", "Listing backup metadata"),
    ],
)
def test_terminal_read_only_command_animates_and_preserves_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    progress_threads: list[Thread],
    command: str,
    handler: str,
    result: str,
    stage: str,
) -> None:
    output = TerminalOutput()
    monkeypatch.setattr(progress_module.sys, "stderr", output)

    def execute(config: Path, **kwargs: Any) -> Any:
        assert config == Path("app.yaml")
        assert kwargs["runtime_params"] == {"wallet": "app"}
        kwargs["progress"](stage)
        output.frame_seen.clear()
        assert output.frame_seen.wait(timeout=2)
        outcome = CommandResult(command, result, backups=2)
        return (outcome, ()) if command == "list" else outcome

    monkeypatch.setattr(commands, handler, execute)

    assert commands.main(["--config", "app.yaml", "--param", "wallet=app", command]) == 0
    assert capsys.readouterr().out == f"{command}: {result}\n"
    assert f"[00:00] {stage}" in output.getvalue()
    assert "\r\033[2K" in output.getvalue()
    assert "still running" not in output.getvalue()
    assert "SUCCESS:" in output.getvalue()
    assert len(progress_threads) == 1
    assert not progress_threads[0].is_alive()


@pytest.mark.parametrize(
    ("command", "handler", "result"),
    [
        ("run", "run_backup", "created"),
        ("list", "list_backups", "listed"),
    ],
)
def test_captured_command_logs_progress_and_heartbeat_without_escapes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    progress_threads: list[Thread],
    command: str,
    handler: str,
    result: str,
) -> None:
    output = TerminalOutput()
    monkeypatch.setattr(output, "isatty", lambda: False)
    monkeypatch.setattr(progress_module.sys, "stderr", output)
    monkeypatch.setattr(progress_module, "_HEARTBEAT_SECONDS", 0.01)

    def execute(config: Path, **kwargs: Any) -> Any:
        kwargs["progress"]("Checking existing backups")
        assert output.heartbeat_seen.wait(timeout=2)
        outcome = CommandResult(
            command, result, archive_path=Path("/backups/app.tar.zst"), archive_size=1536
        )
        return (outcome, ()) if command == "list" else outcome

    monkeypatch.setattr(commands, handler, execute)

    assert commands.main(["--config", "app.yaml", command]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"{command}: {result}\n"
    assert output.getvalue().startswith("[00:00] Checking existing backups\n")
    assert "Checking existing backups (still running)\n" in output.getvalue()
    assert "SUCCESS:" in output.getvalue()
    assert "\033" not in output.getvalue() and "\r" not in output.getvalue()
    assert "Archive:" not in output.getvalue()
    assert len(progress_threads) == 1
    assert not progress_threads[0].is_alive()


@pytest.mark.parametrize("error", [DestinationError("unavailable"), KeyboardInterrupt()])
@pytest.mark.parametrize(
    ("command", "handler"),
    [("run", "run_backup"), ("list", "list_backups"), ("verify", "verify_backups")],
)
def test_failed_or_interrupted_command_stops_progress_before_error_handling(
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    progress_threads: list[Thread],
    error: BaseException,
    command: str,
    handler: str,
) -> None:
    output = TerminalOutput()
    monkeypatch.setattr(progress_module.sys, "stderr", output)

    def execute(config: Path, **kwargs: Any) -> CommandResult:
        kwargs["progress"]("Checking existing backups")
        raise error

    monkeypatch.setattr(commands, handler, execute)

    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            commands.main(["--config", "app.yaml", command])
    else:
        assert commands.main(["--config", "app.yaml", command]) == 5
        assert output.getvalue().endswith("unavailable\n")

    captured = capsys.readouterr()
    if command == "verify":
        assert "Result:    FAILED" in captured.out
    else:
        assert captured.out == ""
    assert not progress_threads[0].is_alive()


def test_summary_follows_result_when_output_streams_share_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TerminalOutput()
    monkeypatch.setattr(commands.sys, "stdout", output)
    monkeypatch.setattr(commands.sys, "stderr", output)
    monkeypatch.setattr(
        commands,
        "run_backup",
        lambda *args, **kwargs: CommandResult(
            "run", "created", archive_path=Path("/backups/app.tar.zst"), archive_size=0
        ),
    )

    assert commands.main(["--config", "app.yaml", "run"]) == 0
    assert output.getvalue().startswith(
        "[00:00] SUCCESS: Backup complete\n"
        "run: created\nArchive:                       /backups/app.tar.zst\n"
        "Size:                          0 B\n"
    )
    assert output.getvalue().endswith("Backups removed by retention:  0\n")


def test_no_color_reaches_progress_and_summary(monkeypatch, capsys, progress_threads):
    output = TerminalOutput()
    monkeypatch.setattr(commands.sys, "stderr", output)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)

    def run(config, **kwargs):
        kwargs["progress"]("Checking existing backups")
        return CommandResult("run", "created", archive_path=Path("/backups/app.tar.zst"))

    monkeypatch.setattr(commands, "run_backup", run)

    assert commands.main(["--no-color", "--config", "app.yaml", "run"]) == 0
    assert capsys.readouterr().out == "run: created\n"
    assert output.getvalue().startswith("\r\033[2K| [00:00] Checking existing backups")
    assert "Archive:                       /backups/app.tar.zst\n" in output.getvalue()
    assert "\033[" not in output.getvalue().replace("\033[2K", "")
    assert not progress_threads[0].is_alive()


def test_progress_stream_failure_does_not_interrupt_backup(
    monkeypatch: pytest.MonkeyPatch, progress_threads: list[Thread]
) -> None:
    class BrokenTerminal(TerminalOutput):
        def write(self, message: str) -> int:
            raise BrokenPipeError("terminal closed")

    monkeypatch.setattr(progress_module.sys, "stderr", BrokenTerminal())

    with progress_module.CliProgress() as progress:
        progress.update("Hashing source files")
        progress.update("Verifying archive")

    assert progress_threads == []
