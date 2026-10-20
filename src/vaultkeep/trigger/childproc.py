"""Run a child process without a shell, draining output and bounding storage."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from vaultkeep.trigger.version import DIAGNOSTICS_CAP_BYTES


@dataclass(frozen=True, slots=True)
class ChildResult:
    """Outcome of one child-process invocation."""

    exit_code: int | None
    signal_number: int | None
    stdout_tail: str | None
    diagnostics: bytes
    diagnostics_truncated: bool
    started_at: str
    ended_at: str
    duration_seconds: float
    spawn_error: str | None = None


class ChildRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str],
        timeout: float | None = None,
    ) -> ChildResult:
        """Run ``argv`` and return its result."""


class RealChildRunner:
    """Adapter shared by production workers and preflight."""

    @staticmethod
    def run(
        argv: Sequence[str], *, env: dict[str, str], timeout: float | None = None
    ) -> ChildResult:
        return run_child(argv, env=env, timeout=timeout)


class _BoundedBuffer:
    """Keep only the last ``cap`` bytes that were written."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._data = bytearray()
        self.truncated = False
        self._lock = threading.Lock()

    def write(self, chunk: bytes) -> None:
        with self._lock:
            self._data.extend(chunk)
            if len(self._data) > self._cap:
                self.truncated = True
                del self._data[: len(self._data) - self._cap]

    def value(self) -> bytes:
        with self._lock:
            return bytes(self._data)


def _drain(stream, buffer: _BoundedBuffer, tail: dict[str, str | None]) -> None:
    line_cap = 4096
    pending = b""
    oversized = False
    nonblank = False
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            buffer.write(chunk)
            parts = chunk.split(b"\n")
            for index, part in enumerate(parts):
                nonblank = nonblank or bool(part.strip())
                oversized = oversized or len(pending) + len(part) > line_cap
                pending = (pending + part)[:line_cap]
                if index < len(parts) - 1:
                    if nonblank:
                        tail["value"] = None if oversized else pending.decode("utf-8", "replace")
                    pending, oversized, nonblank = b"", False, False
        if nonblank:
            tail["value"] = None if oversized else pending.decode("utf-8", "replace")
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def run_child(
    argv: Sequence[str],
    *,
    env: dict[str, str],
    timeout: float | None = None,
    cap: int = DIAGNOSTICS_CAP_BYTES,
) -> ChildResult:
    """Run a child process with an explicit environment and bounded diagnostics."""
    started = _stamp()
    monotonic_start = time.monotonic()
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
        )
    except OSError as error:
        ended = _stamp()
        return ChildResult(
            exit_code=None,
            signal_number=None,
            stdout_tail=None,
            diagnostics=b"",
            diagnostics_truncated=False,
            started_at=started,
            ended_at=ended,
            duration_seconds=0.0,
            spawn_error=str(error),
        )

    diagnostics_buffer = _BoundedBuffer(cap)
    stdout_tail: dict[str, str | None] = {"value": None}
    threads = [
        threading.Thread(
            target=_drain, args=(process.stdout, diagnostics_buffer, stdout_tail), daemon=True
        ),
        threading.Thread(target=_drain, args=(process.stderr, diagnostics_buffer, {}), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = monotonic_start + timeout if timeout is not None else None
    try:
        remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
        return_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        # Only explicitly bounded preflight calls may terminate their child.
        process.kill()
        return_code = process.wait()
    for thread in threads:
        remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
        thread.join(remaining)

    ended = _stamp()
    signal_number: int | None = None
    exit_code: int | None = return_code
    if return_code is not None and return_code < 0:
        signal_number = -return_code
        exit_code = None

    diagnostics = diagnostics_buffer.value()
    return ChildResult(
        exit_code=exit_code,
        signal_number=signal_number,
        stdout_tail=stdout_tail["value"],
        diagnostics=diagnostics,
        diagnostics_truncated=diagnostics_buffer.truncated,
        started_at=started,
        ended_at=ended,
        duration_seconds=time.monotonic() - monotonic_start,
    )
