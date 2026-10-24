"""Tests for bounded, shell-free archive subprocess execution."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from vaultkeep.archive import process as archive_process
from vaultkeep.archive.process import run_command, run_pipeline
from vaultkeep.errors import ArchiveCreationError


def test_command_runner_captures_stdout_and_uses_absolute_tool() -> None:
    result = run_command(
        (Path(sys.executable), "-c", "import sys; sys.stdout.buffer.write(b'output')"),
        capture_stdout=True,
    )
    assert result.stdout == b"output"


def test_command_failure_redacts_diagnostics_for_secret_input() -> None:
    with pytest.raises(ArchiveCreationError) as captured:
        run_command(
            (
                Path(sys.executable),
                "-c",
                "import sys; data=sys.stdin.buffer.read(); "
                "sys.stderr.buffer.write(data); raise SystemExit(7)",
            ),
            input_data=b"top-secret\n",
            sensitive_input=True,
        )
    assert "top-secret" not in str(captured.value)
    assert "withheld" in str(captured.value)


def test_relative_tool_path_is_rejected() -> None:
    with pytest.raises(ArchiveCreationError, match="absolute"):
        run_command(("python", "-V"))


def test_command_failure_includes_bounded_nonsecret_diagnostic() -> None:
    with pytest.raises(ArchiveCreationError) as captured:
        run_command(
            (
                Path(sys.executable),
                "-c",
                "import sys; sys.stderr.write('expected diagnostic'); raise SystemExit(4)",
            )
        )
    assert "status 4" in str(captured.value)
    assert "expected diagnostic" in str(captured.value)


def test_allowed_nonzero_command_result_is_returned() -> None:
    result = run_command(
        (Path(sys.executable), "-c", "raise SystemExit(3)"), allowed_returncodes=(0, 3)
    )
    assert result.returncode == 3


def test_binary_pipeline_transfers_producer_input() -> None:
    with tempfile.TemporaryFile() as output:
        run_pipeline(
            (
                Path(sys.executable),
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read().upper())",
            ),
            (
                Path(sys.executable),
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])",
            ),
            producer_input=b"archive",
            consumer_stdout=output,
        )
        output.seek(0)
        assert output.read() == b"EVIHCRA"


def test_pipeline_reports_consumer_failure() -> None:
    with tempfile.TemporaryFile() as output, pytest.raises(ArchiveCreationError, match="status 9"):
        run_pipeline(
            (Path(sys.executable), "-c", "import sys; sys.stdout.buffer.write(b'data')"),
            (Path(sys.executable), "-c", "raise SystemExit(9)"),
            consumer_stdout=output,
        )


def test_command_rejects_mixed_pipe_and_terminal_input() -> None:
    with pytest.raises(ValueError, match="both pipe input and terminal input"):
        run_command(("/bin/true",), input_data=b"one", terminal_input=b"two")


@pytest.mark.skipif(os.name != "posix", reason="secure terminal input is POSIX-specific")
@pytest.mark.parametrize("prompt_stream", ["stdout", "stderr"])
def test_terminal_command_waits_for_prompt_and_drains_captured_stdout(prompt_stream: str) -> None:
    result = run_command(
        (
            Path(sys.executable),
            "-c",
            "import signal, sys, termios, time; signal.alarm(3); time.sleep(0.05); "
            "termios.tcflush(0, termios.TCIFLUSH); "
            f"print('Enter password (will not be echoed):', file=sys.{prompt_stream}, flush=True); "
            "sys.stdin.readline(); sys.stdout.write('x' * 200000)",
        ),
        terminal_input=b"password\n",
        capture_stdout=True,
    )

    assert result.returncode == 0
    assert len(result.stdout) == 64 * 1024 + len(b"\n[output truncated]")


@pytest.mark.skipif(os.name != "posix", reason="secure terminal input is POSIX-specific")
def test_terminal_pipeline_waits_for_stderr_prompt_and_preserves_archive_stream() -> None:
    with tempfile.TemporaryFile() as output:
        run_pipeline(
            (
                Path(sys.executable),
                "-c",
                "import signal, sys, termios, time; signal.alarm(3); time.sleep(0.05); "
                "termios.tcflush(0, termios.TCIFLUSH); "
                "print('Enter password (will not be echoed):', file=sys.stderr, flush=True); "
                "assert sys.stdin.readline() == 'secret\\n'; sys.stdout.write('archive-data')",
            ),
            (Path(sys.executable), "-c", "import sys; sys.stdout.write(sys.stdin.read())"),
            producer_terminal_input=b"secret\n",
            consumer_stdout=output,
        )
        output.seek(0)
        assert output.read() == b"archive-data"


def test_terminal_command_reports_exit_before_password_prompt() -> None:
    with pytest.raises(ArchiveCreationError, match="status 9"):
        run_command((Path(sys.executable), "-c", "raise SystemExit(9)"), terminal_input=b"secret\n")


@pytest.mark.skipif(os.name != "posix", reason="secure terminal input is POSIX-specific")
@pytest.mark.parametrize("pipeline", [False, True])
@pytest.mark.parametrize("banner", ["", "Unexpected password prompt:"])
def test_password_prompt_timeout_reaps_command_and_pipeline(
    monkeypatch: pytest.MonkeyPatch, pipeline: bool, banner: str
) -> None:
    monkeypatch.setattr(archive_process, "_PASSWORD_PROMPT_TIMEOUT", 0.2)
    processes: list[subprocess.Popen[bytes]] = []
    start_process = archive_process._start_process

    def track_process(*args, **kwargs):
        process = start_process(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(archive_process, "_start_process", track_process)
    command = (
        Path(sys.executable),
        "-c",
        # Bound the fake child even if the prompt deadline regresses.
        "import signal, sys, time; signal.alarm(5); "
        f"print({banner!r}, file=sys.stderr, flush=True); time.sleep(60)",
    )
    with pytest.raises(ArchiveCreationError, match="Timed out waiting for archive password prompt"):
        if pipeline:
            run_pipeline(
                command,
                (Path(sys.executable), "-c", "import sys; sys.stdin.buffer.read()"),
                producer_terminal_input=b"secret\n",
            )
        else:
            run_command(command, terminal_input=b"secret\n")

    assert len(processes) == (2 if pipeline else 1)
    assert all(process.poll() is not None for process in processes)


def test_pipeline_rejects_mixed_pipe_and_terminal_input() -> None:
    with pytest.raises(ValueError, match="both pipe input and terminal input"):
        run_pipeline(
            ("/bin/true",),
            ("/bin/true",),
            producer_input=b"one",
            producer_terminal_input=b"two",
        )
