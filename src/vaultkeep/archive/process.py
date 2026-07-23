"""Bounded-output subprocess execution for archive tools."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from vaultkeep.errors import ArchiveCreationError

_OUTPUT_LIMIT = 64 * 1024
_SAFE_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Sanitized command result with bounded diagnostic output."""

    returncode: int
    stdout: bytes
    stderr: bytes


def run_command(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    input_data: bytes | bytearray | memoryview | None = None,
    cwd: Path | None = None,
    capture_stdout: bool = False,
    allowed_returncodes: Collection[int] = (0,),
    sensitive_input: bool = False,
) -> CommandResult:
    """Run one absolute-path tool without a shell or inherited environment."""
    command = tuple(os.fspath(argument) for argument in arguments)
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = _start_process(
            command,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=stdout_file if capture_stdout else subprocess.DEVNULL,
            stderr=stderr_file,
            cwd=cwd,
        )
        try:
            process.communicate(input=bytes(input_data) if input_data is not None else None)
        except BaseException:
            _terminate_process(process)
            raise
        result = CommandResult(
            process.returncode,
            _read_bounded(stdout_file) if capture_stdout else b"",
            _read_bounded(stderr_file),
        )
    if result.returncode not in allowed_returncodes:
        raise ArchiveCreationError(
            _failure_message(command, result, redact_diagnostic=sensitive_input)
        )
    return result


def run_pipeline(
    producer_arguments: Sequence[str | os.PathLike[str]],
    consumer_arguments: Sequence[str | os.PathLike[str]],
    *,
    producer_input: bytes | bytearray | memoryview | None = None,
    producer_cwd: Path | None = None,
    consumer_cwd: Path | None = None,
    consumer_stdout: IO[bytes] | int | None = None,
    producer_input_sensitive: bool = False,
) -> None:
    """Run a two-process binary pipeline with bounded diagnostics."""
    producer_command = tuple(os.fspath(argument) for argument in producer_arguments)
    consumer_command = tuple(os.fspath(argument) for argument in consumer_arguments)
    with tempfile.TemporaryFile() as producer_error, tempfile.TemporaryFile() as consumer_error:
        producer = _start_process(
            producer_command,
            stdin=subprocess.PIPE if producer_input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=producer_error,
            cwd=producer_cwd,
        )
        if producer.stdout is None:
            _terminate_process(producer)
            raise ArchiveCreationError("Archive pipeline did not expose producer output")
        try:
            consumer = _start_process(
                consumer_command,
                stdin=producer.stdout,
                stdout=consumer_stdout if consumer_stdout is not None else subprocess.DEVNULL,
                stderr=consumer_error,
                cwd=consumer_cwd,
            )
        except BaseException:
            producer.stdout.close()
            _terminate_process(producer)
            raise
        producer.stdout.close()
        try:
            if producer.stdin is not None:
                try:
                    if producer_input is not None:
                        producer.stdin.write(bytes(producer_input))
                        producer.stdin.flush()
                finally:
                    producer.stdin.close()
            producer.wait()
            consumer.wait()
        except OSError as error:
            _terminate_process(producer)
            _terminate_process(consumer)
            raise ArchiveCreationError(f"Archive pipeline I/O failed: {error}") from error
        except BaseException:
            _terminate_process(producer)
            _terminate_process(consumer)
            raise
        producer_result = CommandResult(
            producer.returncode,
            b"",
            _read_bounded(producer_error),
        )
        consumer_result = CommandResult(
            consumer.returncode,
            b"",
            _read_bounded(consumer_error),
        )
    if producer_result.returncode != 0:
        raise ArchiveCreationError(
            _failure_message(
                producer_command,
                producer_result,
                redact_diagnostic=producer_input_sensitive,
            )
        )
    if consumer_result.returncode != 0:
        raise ArchiveCreationError(_failure_message(consumer_command, consumer_result))


def _start_process(
    command: Sequence[str],
    *,
    stdin: int | IO[bytes],
    stdout: int | IO[bytes],
    stderr: IO[bytes],
    cwd: Path | None,
) -> subprocess.Popen[bytes]:
    if not command or not Path(command[0]).is_absolute():
        raise ArchiveCreationError("Archive tools must be invoked by absolute path")
    try:
        return subprocess.Popen(
            command,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=_SAFE_ENVIRONMENT,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        raise ArchiveCreationError(f"Cannot start archive tool {command[0]}: {error}") from error


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        _terminate_process_group(process)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        _kill_process_group(process)
        process.wait()


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        _posix_kill_process_group(process.pid, int(signal.SIGTERM))
    else:
        process.terminate()


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        _posix_kill_process_group(process.pid, int(getattr(signal, "SIGKILL", 9)))
    else:
        process.kill()


def _posix_kill_process_group(process_id: int, requested_signal: int) -> None:
    kill_process_group = getattr(os, "killpg", None)
    if kill_process_group is None:
        raise OSError("POSIX process-group signaling is unavailable")
    kill_process_group(process_id, requested_signal)


def _read_bounded(stream: IO[bytes]) -> bytes:
    stream.seek(0)
    content = stream.read(_OUTPUT_LIMIT + 1)
    if len(content) <= _OUTPUT_LIMIT:
        return content
    return content[:_OUTPUT_LIMIT] + b"\n[output truncated]"


def _failure_message(
    command: Sequence[str],
    result: CommandResult,
    *,
    redact_diagnostic: bool = False,
) -> str:
    if redact_diagnostic:
        summary = "diagnostic output withheld because the command received secret input"
    else:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        summary = diagnostic or "no diagnostic output"
    return f"Archive tool {command[0]} exited with status {result.returncode}: {summary}"
