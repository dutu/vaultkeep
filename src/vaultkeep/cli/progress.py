"""CLI progress presentation, independent of persisted backup progress."""

from __future__ import annotations

import os
import sys
import unicodedata
from dataclasses import dataclass
from threading import Event, Lock, Thread
from time import monotonic
from types import TracebackType
from typing import TextIO

from vaultkeep.backup.progress import HEARTBEAT_SECONDS

_HEARTBEAT_SECONDS = HEARTBEAT_SECONDS
_REFRESH_SECONDS = 0.1


@dataclass(frozen=True)
class ProgressMetrics:
    """Optional caller-supplied measurements; no progress is inferred by the UI."""

    percent: float | None = None
    completed: int | None = None
    total: int | None = None
    bytes_processed: int | None = None
    bytes_total: int | None = None

    def format(self) -> str:
        parts = []
        if self.percent is not None:
            parts.append(f"{self.percent:.0f}%")
        if self.completed is not None:
            counter = f"{self.completed:,}"
            if self.total is not None:
                counter += f"/{self.total:,}"
            parts.append(counter)
        if self.bytes_processed is not None:
            size = f"{self.bytes_processed:,}"
            if self.bytes_total is not None:
                size += f"/{self.bytes_total:,}"
            parts.append(f"{size} bytes")
        return " | ".join(parts)


class CliProgress:
    """One CLI operation with serialized status and permanent messages on stderr.

    Use as a context manager. ``update`` starts lazily and can serve directly as
    a workflow's stage callback. Metrics are snapshots; an empty ProgressMetrics
    clears them, and a new description resets them. Route permanent output through
    ``message`` while active. A single waiting worker animates blocking operations
    or emits log heartbeats; it is always joined before leaving the context.
    """

    def __init__(self, *, stream: TextIO | None = None) -> None:
        self._stream = sys.stderr if stream is None else stream
        self._interactive = self._stream.isatty() and os.environ.get("TERM", "dumb") not in (
            "",
            "dumb",
        )
        self._frames = "|/-\\"
        try:
            "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏".encode(self._stream.encoding or "ascii")
        except (UnicodeError, LookupError):
            pass
        else:
            self._frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._lock = Lock()
        self._stopped = Event()
        self._worker: Thread | None = None
        self._description: str | None = None
        self._metrics = ProgressMetrics()
        self._started = monotonic()
        self._last_logged = self._started
        self._frame = 0
        self._visible = False
        self._disabled = False

    def __enter__(self) -> CliProgress:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                self.fail("Interrupted" if issubclass(exc_type, KeyboardInterrupt) else "Failed")
        finally:
            self.close()

    def start(self, description: str, *, metrics: ProgressMetrics | None = None) -> None:
        """Start reporting an operation; subsequent stage changes use update."""
        self.update(description, metrics=metrics)

    def update(
        self, description: str | None = None, *, metrics: ProgressMetrics | None = None
    ) -> None:
        """Report stages immediately; coalesce metric-only updates between frames."""
        with self._lock:
            if self._stopped.is_set() or self._disabled:
                return
            if self._description is None and description is not None:
                self._started = monotonic()
            changed = description is not None and description != self._description
            if changed:
                self._description = description
                self._metrics = ProgressMetrics()
            if metrics is not None:
                self._metrics = metrics
            if self._description is None:
                return
            if changed:
                self._render()
                self._last_logged = monotonic()
            if self._worker is None and not self._disabled:
                self._worker = Thread(target=self._refresh, name="vaultkeep-progress", daemon=True)
                self._worker.start()

    def message(self, message: str) -> None:
        """Clear the active line, print permanent output, and restore the spinner."""
        with self._lock:
            self._clear()
            self._write(self._safe_text(message) + "\n")
            if self._description is not None and not self._stopped.is_set() and self._interactive:
                self._render()

    def succeed(self, message: str | None = None) -> None:
        self._finish("SUCCESS", message)

    def fail(self, message: str | None = None) -> None:
        self._finish("FAILED", message)

    def _finish(self, status: str, message: str | None) -> None:
        with self._lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
            self._clear()
            if message is not None or self._description is not None:
                self._write(
                    f"{self._elapsed()} {status}: "
                    f"{self._safe_text(message or self._description or '')}\n"
                )
        self.close()

    def close(self) -> None:
        """Stop updates and clear the active line, including on interruption."""
        with self._lock:
            self._stopped.set()
            self._clear()
        if self._worker is not None:
            self._worker.join()

    def _refresh(self) -> None:
        interval = _REFRESH_SECONDS if self._interactive else _HEARTBEAT_SECONDS
        while not self._stopped.wait(interval):
            with self._lock:
                if self._stopped.is_set() or self._disabled:
                    return
                if self._interactive:
                    self._render()
                elif monotonic() - self._last_logged >= _HEARTBEAT_SECONDS:
                    self._render(heartbeat=True)
                    self._last_logged = monotonic()
                if not self._interactive:
                    interval = max(0.001, _HEARTBEAT_SECONDS - (monotonic() - self._last_logged))

    def _elapsed(self) -> str:
        minutes, seconds = divmod(int(monotonic() - self._started), 60)
        return f"[{minutes:02d}:{seconds:02d}]"

    def _render(self, *, heartbeat: bool = False) -> None:
        text = f"{self._elapsed()} {self._description}"
        details = self._metrics.format()
        if details:
            text += f" ({details})"
        text = self._safe_text(text)
        if self._interactive:
            text = f"{self._frames[self._frame % len(self._frames)]} {text}"
            self._frame += 1
            try:
                columns = os.get_terminal_size(self._stream.fileno()).columns
            except (OSError, ValueError):
                columns = 80
            # Leave the final column unused so the terminal cannot wrap the line.
            remaining = max(0, columns - 1)
            fitted = ""
            for char in text:
                width = (
                    0
                    if unicodedata.combining(char)
                    else (2 if unicodedata.east_asian_width(char) in ("W", "F") else 1)
                )
                if width > remaining:
                    break
                fitted += char
                remaining -= width
            self._write("\r\033[2K" + fitted)
            self._visible = True
        else:
            self._write(text + (" (still running)" if heartbeat else "") + "\n")

    def _clear(self) -> None:
        if self._visible:
            self._write("\r\033[2K")
            self._visible = False

    def _safe_text(self, text: str) -> str:
        text = "".join(char if char.isprintable() else " " for char in text)
        encoding = self._stream.encoding or "ascii"
        return text.encode(encoding, errors="replace").decode(encoding)

    def _write(self, text: str) -> None:
        if not self._disabled:
            try:
                self._stream.write(text)
                self._stream.flush()
            except (OSError, ValueError):
                # Optional presentation must not fail the underlying operation.
                self._disabled = True
                self._stopped.set()
