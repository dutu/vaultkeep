"""Animated CLI rendering, plain logs, and safe operation lifetimes."""

from __future__ import annotations

import io
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from vaultkeep.backup.workflow import CommandResult
from vaultkeep.cli import commands
from vaultkeep.cli import progress as progress_module
from vaultkeep.cli.progress import CliProgress, ProgressMetrics


class Output(io.StringIO):
    def __init__(self, *, tty: bool = True, encoding: str = "utf-8") -> None:
        super().__init__()
        self.tty = tty
        self._encoding = encoding
        self.frame_seen = Event()
        self.heartbeat_seen = Event()

    @property
    def encoding(self) -> str:
        return self._encoding

    def isatty(self) -> bool:
        return self.tty

    def write(self, text: str) -> int:
        result = super().write(text)
        if text.startswith("\r\033[2K") and len(text) > 5:
            self.frame_seen.set()
        if "still running" in text:
            self.heartbeat_seen.set()
        return result


@pytest.fixture
def workers(monkeypatch: pytest.MonkeyPatch) -> list[Thread]:
    threads: list[Thread] = []

    def create(**kwargs: Any) -> Thread:
        worker = Thread(**kwargs)
        threads.append(worker)
        return worker

    monkeypatch.setattr(progress_module, "Thread", create)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(progress_module, "_REFRESH_SECONDS", 0.01)
    yield threads
    assert all(not worker.is_alive() for worker in threads)


def test_spinner_animates_elapsed_time_and_clears_shorter_status(monkeypatch, workers):
    output = Output()
    now = [0.0]
    monkeypatch.setattr(progress_module, "monotonic", lambda: now[0])
    with CliProgress(stream=output) as progress:
        progress.start("Verifying backup manifests and checksums")
        assert output.getvalue() == "\r\033[2K⠋ [00:00] Verifying backup manifests and checksums"
        output.frame_seen.clear()
        now[0] = 62.0
        assert output.frame_seen.wait(2)
        assert "⠙ [01:02] Verifying" in output.getvalue()
        progress.update("Done")
        progress.succeed()
    assert "\r\033[2K" in output.getvalue().split("Done")[0]
    assert output.getvalue().endswith("\r\033[2K[01:02] SUCCESS: Done\n")
    assert output.getvalue().count("\n") == 1
    assert len(workers) == 1


@pytest.mark.parametrize("tty,term", [(False, "xterm"), (True, "dumb"), (True, ""), (True, None)])
def test_unsuitable_terminals_log_immediate_stages_and_heartbeats(monkeypatch, workers, tty, term):
    if term is None:
        monkeypatch.delenv("TERM", raising=False)
    else:
        monkeypatch.setenv("TERM", term)
    monkeypatch.setattr(progress_module, "_HEARTBEAT_SECONDS", 0.01)
    output = Output(tty=tty)
    with CliProgress(stream=output) as progress:
        progress.start("Loading configuration")
        progress.update("Verifying backups")
        progress.update("Verifying backups")
        assert output.getvalue() == "[00:00] Loading configuration\n[00:00] Verifying backups\n"
        assert output.heartbeat_seen.wait(2)
        progress.message("Verified: archive.tar.zst")
        progress.succeed("Verification complete")
    text = output.getvalue()
    assert "[00:00] Verifying backups (still running)\n" in text
    assert "Verified: archive.tar.zst\n" in text
    assert text.endswith("[00:00] SUCCESS: Verification complete\n")
    assert "\033" not in text and "\r" not in text
    assert len(workers) == 1


def test_metrics_are_coalesced_and_reset_on_stage_changes(workers):
    output = Output()
    with CliProgress(stream=output) as progress:
        progress.start(
            "Copying",
            metrics=ProgressMetrics(
                percent=50, completed=2, total=4, bytes_processed=1024, bytes_total=2048
            ),
        )
        assert "50% | 2/4 | 1,024/2,048 bytes" in output.getvalue()
        output.frame_seen.clear()
        progress.update(metrics=ProgressMetrics(percent=75, completed=3, total=4))
        assert output.frame_seen.wait(2)
        assert "75% | 3/4" in output.getvalue()
        progress.update(metrics=ProgressMetrics())
        progress.message("Counters cleared")
        assert output.getvalue().endswith("[00:00] Copying")
        progress.update("Checking result")
        assert output.getvalue().endswith("Checking result")


def test_permanent_messages_clear_spinner_then_resume(workers):
    output = Output()
    with CliProgress(stream=output) as progress:
        progress.start("Verifying")
        progress.message("Verified: archive.tar.zst")
        assert "\r\033[2KVerified: archive.tar.zst\n\r\033[2K" in output.getvalue()
    assert output.getvalue().endswith("\r\033[2K")


@pytest.mark.parametrize("columns", [1, 8, 25, 80])
def test_terminal_width_uses_display_cells_and_adapts_to_resize(monkeypatch, workers, columns):
    output = Output()
    width = [columns]
    monkeypatch.setattr(Output, "fileno", lambda self: 2)
    monkeypatch.setattr(
        progress_module.os, "get_terminal_size", lambda fd: os.terminal_size((width[0], 24))
    )
    with CliProgress(stream=output) as progress:
        progress.start("Checking 界面 e\u0301 " * 30)
        frame = output.getvalue().split("\r\033[2K")[-1]
        cells = sum(
            0
            if unicodedata.combining(c)
            else 2
            if unicodedata.east_asian_width(c) in ("W", "F")
            else 1
            for c in frame
        )
        assert cells <= columns - 1
        width[0] = 10
        progress.update("Resized terminal")
        assert len(output.getvalue().split("\r\033[2K")[-1]) <= 9


def test_ascii_stream_replaces_unencodable_text_and_uses_ascii_spinner(workers):
    output = Output(encoding="ascii")
    with CliProgress(stream=output) as progress:
        progress.start("Checking 日本語")
        progress.message("Verified: 日本語.tar.zst")
        progress.succeed()
    text = output.getvalue()
    assert text.startswith("\r\033[2K| [00:00] Checking ???")
    assert "Verified: ???.tar.zst\n" in text
    text.encode("ascii")


def test_control_characters_cannot_inject_lines_or_terminal_commands(workers):
    output = Output(tty=False)
    with CliProgress(stream=output) as progress:
        progress.start("Stage\n\r\033[31m\ttext")
        progress.message("File\n\033[2J")
    assert "\033" not in output.getvalue()
    assert output.getvalue().count("\n") == 2


@pytest.mark.parametrize("error", [RuntimeError("failed"), KeyboardInterrupt(), SystemExit(2)])
def test_errors_and_interruptions_clean_up_and_propagate(workers, error):
    output = Output()
    with pytest.raises(type(error)), CliProgress(stream=output) as progress:
        progress.start("Verifying")
        raise error
    assert "\r\033[2K[00:00] FAILED:" in output.getvalue()
    assert output.getvalue().endswith(
        "Interrupted\n" if isinstance(error, KeyboardInterrupt) else "Failed\n"
    )


@pytest.mark.parametrize("on_flush", [False, True])
def test_lost_progress_stream_does_not_fail_operation(workers, on_flush):
    class BrokenOutput(Output):
        def write(self, text):
            if not on_flush:
                raise BrokenPipeError("closed")
            return super().write(text)

        def flush(self):
            raise OSError("closed")

    with CliProgress(stream=BrokenOutput()) as progress:
        progress.start("Verifying")
        progress.update("Still verifying")
        progress.succeed()
    assert workers == []


def test_concurrent_messages_and_updates_are_serialized(workers):
    output = Output()
    with CliProgress(stream=output) as progress:
        progress.start("Starting")

        def report(index):
            progress.update(f"Stage {index}")
            progress.message(f"Completed {index}")

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(report, range(100)))
        progress.succeed("Finished")
        snapshot = output.getvalue()
        progress.update("Too late")
        progress.close()
        assert output.getvalue() == snapshot
    lines = re.findall(r"\r\033\[2K(Completed \d+\n)", output.getvalue())
    assert sorted(lines) == sorted(f"Completed {index}\n" for index in range(100))
    assert len(workers) == 1


def test_unused_progress_does_not_create_worker(workers):
    output = Output()
    with CliProgress(stream=output):
        pass
    assert output.getvalue() == ""
    assert workers == []


def test_elapsed_time_starts_with_operation_not_renderer_construction(monkeypatch, workers):
    output = Output(tty=False)
    now = [0.0]
    monkeypatch.setattr(progress_module, "monotonic", lambda: now[0])
    with CliProgress(stream=output) as progress:
        now[0] = 60.0
        progress.start("Verifying")
        now[0] = 125.0
        progress.succeed("Complete")
    assert output.getvalue() == "[00:00] Verifying\n[01:05] SUCCESS: Complete\n"


@pytest.mark.parametrize("tty", [False, True])
def test_verify_uses_renderer_and_preserves_stdout(monkeypatch, capsys, workers, tty):
    output = Output(tty=tty)
    monkeypatch.setattr(progress_module.sys, "stderr", output)

    def verify(config, **kwargs):
        assert config == Path("app.yaml")
        assert kwargs["runtime_params"] == {"wallet": "app"}
        kwargs["progress"]("Checking destination")
        kwargs["progress"]("Verifying archive")
        kwargs["on_verified"]("/backups/archive.tar.zst")
        return CommandResult("verify", "verified", backups=1)

    monkeypatch.setattr(commands, "verify_backups", verify)
    assert (
        commands.main(["--no-color", "--config", "app.yaml", "--param", "wallet=app", "verify"])
        == 0
    )
    assert capsys.readouterr().out == "verify: verified\n"
    assert "Verified: /backups/archive.tar.zst\n" in output.getvalue()
    assert output.getvalue().endswith("SUCCESS: Verification complete\n")
    assert ("\033[2K" in output.getvalue()) is tty
    assert len(workers) == 1
