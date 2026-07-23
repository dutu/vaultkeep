"""Tests for bounded, shell-free archive subprocess execution."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

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


def test_pipeline_rejects_mixed_pipe_and_terminal_input() -> None:
    with pytest.raises(ValueError, match="both pipe input and terminal input"):
        run_pipeline(
            ("/bin/true",),
            ("/bin/true",),
            producer_input=b"one",
            producer_terminal_input=b"two",
        )
