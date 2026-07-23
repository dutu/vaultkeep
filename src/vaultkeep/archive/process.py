"""Bounded-output subprocess execution for archive tools."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from collections.abc import Collection, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

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
    terminal_input: bytes | bytearray | memoryview | None = None,
) -> CommandResult:
    """Run one absolute-path tool without a shell or inherited environment."""
    if input_data is not None and terminal_input is not None:
        raise ValueError("A command cannot receive both pipe input and terminal input")
    command = tuple(os.fspath(argument) for argument in arguments)
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        terminal = _SecretTerminal.open() if terminal_input is not None else None
        try:
            process = _start_process(
                command,
                stdin=(terminal.slave if terminal is not None else subprocess.PIPE)
                if (input_data is not None or terminal is not None)
                else subprocess.DEVNULL,
                stdout=stdout_file if capture_stdout else subprocess.DEVNULL,
                stderr=stderr_file,
                cwd=cwd,
            )
        except BaseException:
            if terminal is not None:
                terminal.close()
            raise
        try:
            if terminal is not None:
                if terminal_input is None:
                    raise AssertionError("Terminal input disappeared")
                terminal.write(bytes(terminal_input))
                terminal.close()
                process.wait()
            else:
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
            _failure_message(
                command,
                result,
                redact_diagnostic=sensitive_input or terminal_input is not None,
            )
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
    producer_terminal_input: bytes | bytearray | memoryview | None = None,
) -> None:
    """Run a two-process binary pipeline with bounded diagnostics."""
    if producer_input is not None and producer_terminal_input is not None:
        raise ValueError("A pipeline producer cannot receive both pipe input and terminal input")
    producer_command = tuple(os.fspath(argument) for argument in producer_arguments)
    consumer_command = tuple(os.fspath(argument) for argument in consumer_arguments)
    with tempfile.TemporaryFile() as producer_error, tempfile.TemporaryFile() as consumer_error:
        terminal = _SecretTerminal.open() if producer_terminal_input is not None else None
        try:
            producer = _start_process(
                producer_command,
                stdin=(terminal.slave if terminal is not None else subprocess.PIPE)
                if (producer_input is not None or terminal is not None)
                else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=producer_error,
                cwd=producer_cwd,
            )
        except BaseException:
            if terminal is not None:
                terminal.close()
            raise
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
            if terminal is not None:
                if producer_terminal_input is None:
                    raise AssertionError("Producer terminal input disappeared")
                terminal.write(bytes(producer_terminal_input))
                terminal.close()
            elif producer.stdin is not None:
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
                redact_diagnostic=producer_input_sensitive or producer_terminal_input is not None,
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


@dataclass(slots=True)
class _SecretTerminal:
    """A no-echo POSIX terminal for 7-Zip's password prompt."""

    master: int
    slave: int

    @classmethod
    def open(cls) -> _SecretTerminal:
        if os.name != "posix":
            raise ArchiveCreationError("Interactive archive passwords require Debian/POSIX")
        try:
            import termios as imported_termios

            terminal_api: Any = imported_termios
            open_pty: Any = vars(os)["openpty"]
            master, slave = open_pty()
            settings = terminal_api.tcgetattr(slave)
            settings[3] &= ~terminal_api.ECHO
            terminal_api.tcsetattr(slave, terminal_api.TCSANOW, settings)
            return cls(master, slave)
        except OSError as error:
            raise ArchiveCreationError(
                f"Cannot create secure archive password terminal: {error}"
            ) from error

    def write(self, value: bytes) -> None:
        try:
            os.write(self.master, value)
        except OSError as error:
            raise ArchiveCreationError(
                f"Cannot provide archive password to terminal: {error}"
            ) from error

    def close(self) -> None:
        for descriptor in (self.master, self.slave):
            with suppress(OSError):
                os.close(descriptor)


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
