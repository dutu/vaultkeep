"""CLI progress presentation, independent of persisted backup progress."""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from types import TracebackType
from typing import ClassVar, TextIO

from vaultkeep.backup.progress import HEARTBEAT_SECONDS
from vaultkeep.cli.output import format_size, format_table, safe_text, terminal_columns, text_cells
from vaultkeep.progress import VerificationProgress

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
    ``keep_completed`` leaves ticked stages in interactive terminal history.
    """

    def __init__(
        self, *, stream: TextIO | None = None, animate: bool = True, keep_completed: bool = False
    ) -> None:
        self._stream = sys.stderr if stream is None else stream
        self._interactive = (
            animate
            and self._stream.isatty()
            and os.environ.get("TERM", "dumb")
            not in (
                "",
                "dumb",
            )
        )
        self._frames = "|/-\\"
        try:
            "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏".encode(self._stream.encoding or "ascii")
        except (UnicodeError, LookupError):
            pass
        else:
            self._frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._keep_completed = keep_completed and self._interactive
        self._tick = "✓"
        try:
            self._tick.encode(self._stream.encoding or "ascii")
        except (UnicodeError, LookupError):
            self._tick = "[OK]"
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
        self._lines: tuple[str, ...] = ()
        self._detail: str | None = None
        self._visible_lines: tuple[str, ...] = ()

    def __enter__(self) -> CliProgress:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            with suppress(Exception):
                if exc_type is not None:
                    self.fail(
                        "Interrupted" if issubclass(exc_type, KeyboardInterrupt) else "Failed"
                    )
        finally:
            with suppress(Exception):
                self.close()

    def start(self, description: str, *, metrics: ProgressMetrics | None = None) -> None:
        """Start reporting an operation; subsequent stage changes use update."""
        self.update(description, metrics=metrics)

    def update(
        self,
        description: str | None = None,
        *,
        metrics: ProgressMetrics | None = None,
        lines: tuple[str, ...] | None = None,
        detail: str | None = None,
        log: bool = True,
    ) -> None:
        """Report stages immediately; coalesce metric-only updates between frames."""
        with self._lock:
            if self._stopped.is_set() or self._disabled:
                return
            if self._worker is None and self._description is None and description is not None:
                self._started = monotonic()
            changed = description is not None and description != self._description
            if changed:
                # Cleanup callbacks run inside exception handlers. Advancing to
                # cleanup does not establish that the preceding stage succeeded.
                if sys.exception() is None:
                    self._retain_stage()
                self._description = description
                self._metrics = ProgressMetrics()
            if metrics is not None:
                self._metrics = metrics
            if lines is not None:
                self._lines = lines
            self._detail = detail
            if self._description is None:
                return
            if changed and (self._interactive or log):
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

    def checkpoint(self, message: str, *, log_only: bool = False) -> None:
        """Finish the current stage with a timestamped line, keeping the timer alive."""
        with self._lock:
            self._clear()
            self._description = None
            self._metrics = ProgressMetrics()
            if not log_only or not self._interactive:
                self._write(f"{self._elapsed()} {self._safe_text(message)}\n")

    def succeed(self, message: str | None = None) -> None:
        self._finish("SUCCESS", message)

    def fail(self, message: str | None = None) -> None:
        self._finish("FAILED", message)

    def _finish(self, status: str, message: str | None) -> None:
        with self._lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
            if status == "SUCCESS":
                self._retain_stage()
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

    def _retain_stage(self) -> None:
        """Leave a completed stage above the active spinner, outside its redraw area."""
        if self._keep_completed and self._description is not None:
            self._clear()
            text = f"{self._tick} {self._status_text()}"
            self._write(self._fit(text, self.columns - 1) + "\n")

    def _status_text(self) -> str:
        text = f"{self._elapsed()} {self._description}"
        details = self._metrics.format()
        if details:
            text += f" ({details})"
        return self._safe_text(text)

    def _render(self, *, heartbeat: bool = False) -> None:
        if self._description is None:
            return
        text = self._status_text()
        if self._interactive:
            text = f"{self._frames[self._frame % len(self._frames)]} {text}"
            self._frame += 1
            columns = self.columns
            lines = (*self._lines, text)
            if self._detail:
                lines += (f"    {self._detail}",)
            fitted = tuple(self._fit(self._safe_text(line), columns - 1) for line in lines)
            self._clear()
            self._write("\r\033[2K" + "\n".join(fitted))
            self._visible_lines = fitted
            self._visible = True
        else:
            if heartbeat and self._detail:
                text += " | " + self._safe_text(self._detail)
            self._write(text + (" (still running)" if heartbeat else "") + "\n")

    def _clear(self) -> None:
        if self._visible:
            self._write("\r\033[2K")
            # A resize may have reflowed previously drawn lines. Clear their
            # physical rows using the current width before drawing the next frame.
            columns = self.columns
            rows = sum(
                max(1, (self._cells(line) + columns - 1) // columns) for line in self._visible_lines
            )
            for _ in range(rows - 1):
                self._write("\033[1A\r\033[2K")
            self._visible = False
            self._visible_lines = ()

    @property
    def columns(self) -> int:
        return terminal_columns(self._stream)

    @staticmethod
    def _cells(text: str) -> int:
        return text_cells(text)

    @classmethod
    def _fit(cls, text: str, width: int) -> str:
        fitted = ""
        for char in text:
            cells = cls._cells(char)
            if cells > width:
                break
            fitted += char
            width -= cells
        return fitted

    def _safe_text(self, text: str, *, stream: TextIO | None = None) -> str:
        return safe_text(text, self._stream if stream is None else stream)

    def _write(self, text: str) -> None:
        if not self._disabled:
            try:
                self._stream.write(text)
                self._stream.flush()
            except (OSError, ValueError):
                # Optional presentation must not fail the underlying operation.
                self._disabled = True
                self._stopped.set()


@dataclass
class _VerificationRow:
    directory: Path
    created_at: str | None = None
    backup_id: str | None = None
    manifest: str = "NOT CHECKED"
    checksum: str = "NOT CHECKED"
    archive: str = "NOT CHECKED"


class VerificationDisplay:
    """Present only executed checks, using validated metadata for backup labels."""

    _PHASES: ClassVar[dict[int, str]] = {1: "Manifests & SHA-256", 2: "Archive integrity"}

    def __init__(
        self, progress: CliProgress, *, verbose: bool = False, stream: TextIO | None = None
    ) -> None:
        self._progress = progress
        self._stream = sys.stdout if stream is None else stream
        self._verbose = verbose
        self._started = monotonic()
        self._destination: Path | None = None
        self._rows: dict[Path, _VerificationRow] = {}
        self._phase = 0
        self._completed: dict[int, str] = {}
        self._failures: list[tuple[Path | None, str, str, Path | None]] = []
        self._unknown_total = False
        self._last_operation: tuple[Path | None, str] | None = None

    def stage(self, description: str) -> None:
        self._progress.update(description, lines=self._header())

    def _header(self) -> tuple[str, ...]:
        lines: tuple[str, ...] = ("Vaultkeep — Verify", "")
        if self._destination is not None:
            lines += (f"Destination: {self._destination}",)
        count = str(len(self._rows)) if self._phase else "?"
        lines += (f"Backups:     {count}", "")
        return lines + tuple(self._completed.values())

    def _labels(self) -> dict[Path, str]:
        labels = {}
        for index, row in enumerate(self._rows.values(), 1):
            labels[row.directory] = (
                datetime.fromisoformat(row.created_at).strftime("%Y-%m-%d %H:%M")
                if row.created_at
                else f"Unknown #{index}"
            )
        groups: dict[str, list[Path]] = {}
        for path, label in labels.items():
            groups.setdefault(label, []).append(path)
        for paths in groups.values():
            if len(paths) < 2:
                continue
            identifiers = {path: self._rows[path].backup_id or "unknown" for path in paths}
            suffixes = {path: value[:8] for path, value in identifiers.items()}
            if len(set(suffixes.values())) < len(paths):
                suffixes = {path: value[-8:] for path, value in identifiers.items()}
            for index, path in enumerate(paths, 1):
                suffix = suffixes[path]
                if len(set(suffixes.values())) < len(paths):
                    suffix += f"-{index}"
                labels[path] += f" [{suffix}]"
        return labels

    def _row(self, event: VerificationProgress) -> _VerificationRow | None:
        directory = event.backup_directory
        if directory is None and event.path is not None:
            # Older callers can identify a backup by its directory or archive.
            directory = (
                event.path
                if event.operation == "Manifest/directory validation"
                else event.path.parent
            )
        if directory is None:
            return None
        row = self._rows.setdefault(directory, _VerificationRow(directory))
        row.created_at = event.created_at or row.created_at
        row.backup_id = event.backup_id or row.backup_id
        return row

    def _snapshot(self, event: VerificationProgress | None = None, *, log: bool = False) -> None:
        phase = self._phase
        if not phase:
            return
        rows = list(self._rows.values())
        eligible = [row for row in rows if row.manifest == row.checksum == "OK"]
        passed = len(eligible) if phase == 1 else sum(row.archive == "OK" for row in rows)
        total: int | str = len(rows) if phase == 1 else len(eligible)
        if phase == 1 and self._unknown_total:
            total = "?"
        if phase == 2 and event is not None and event.total is not None:
            total = event.total
        description = f"Phase {phase}/2: {self._PHASES[phase]} | {passed}/{total} verified"
        detail = None
        if event is not None:
            row = self._row(event)
            label = self._labels().get(row.directory, "Unknown") if row else "Unknown"
            operation = event.operation if self._verbose or phase == 1 else "Verifying"
            index = "?" if event.index is None else str(event.index)
            event_total = "?" if event.total is None else str(event.total)
            detail = f"{label} | {index}/{event_total} | {operation}"
            if event.bytes_processed is not None:
                detail += f" | {format_size(event.bytes_processed, byte_unit='bytes')}"
                if event.bytes_total is not None:
                    detail += f" / {format_size(event.bytes_total, byte_unit='bytes')}"
                    if event.bytes_total:
                        detail += f" ({100 * event.bytes_processed / event.bytes_total:.0f}%)"
        self._progress.update(description, lines=self._header(), detail=detail, log=log)

    def report(self, event: VerificationProgress) -> None:
        phase = 2 if event.phase == "archive" else 1
        if event.status == "complete":
            if self._phase != phase:
                self._phase = phase
                self._snapshot(log=True)
            completed = event.completed or 0
            total = "?" if event.total is None else str(event.total)
            marker = "✓" if event.completed == event.total and event.total is not None else "!"
            self._completed[phase] = (
                f"[{marker}] {phase}/2  {self._PHASES[phase]}  {completed}/{total} passed"
            )
            self._progress.checkpoint(f"Phase {phase}/2: {completed}/{total} passed", log_only=True)
            return

        if event.phase == "discovery":
            if event.operation == "Verification destination":
                self._destination = event.path
                return
            if event.operation == "Verification history":
                self._completed.clear()
                return
            if event.status == "failed":
                self._unknown_total = True
                self._failure(None, "Discovery", event)
                return
            if self._destination is None:
                self._destination = event.path
            for directory in event.candidates:
                self._rows.setdefault(directory, _VerificationRow(directory))
            self._progress.checkpoint(f"Found {event.total} backups", log_only=True)
            changed = self._phase != 1
            self._phase = 1
            self._snapshot(log=changed)
            return

        phase = 1 if event.phase == "backup" else 2
        changed = self._phase != phase
        self._phase = phase
        row = self._row(event)
        check = (
            "Archive" if phase == 2 else ("SHA-256" if event.operation == "SHA-256" else "Manifest")
        )
        if row is not None and event.status in ("ok", "failed"):
            status = "OK" if event.status == "ok" else "FAILED"
            if check == "Manifest":
                row.manifest = status
                if status == "FAILED":
                    row.checksum = row.archive = "SKIPPED"
            elif check == "SHA-256":
                row.checksum = status
                if status == "FAILED":
                    row.archive = "SKIPPED"
            elif event.status == "failed" or event.operation == "Archive verification":
                # Passing a stream test alone does not prove TAR integrity.
                row.archive = status
        if event.status == "failed":
            self._failure(row.directory if row else None, check, event)
        if self._verbose:
            current = (event.path, event.operation)
            if event.status != "running" or current != self._last_operation or event.detail:
                self._progress.message(
                    f"{event.operation}: {event.status.upper()} | {event.path}"
                    + (f" | {event.detail}" if event.detail else "")
                )
            self._last_operation = current
        self._snapshot(event, log=changed)

    def _failure(self, directory: Path | None, check: str, event: VerificationProgress) -> None:
        error = event.error or event.operation
        self._failures.append((directory, check, error, event.path))
        label = self._labels().get(directory, "Unknown") if directory else "Destination"
        self._progress.checkpoint(f"FAILED: {label} | {check} | {error} | {event.path}")

    def summary(self, *, success: bool, error: BaseException | None = None) -> None:
        self._progress.close()
        labels = self._labels()
        rows = sorted(
            self._rows.values(), key=lambda row: (row.created_at or "~", labels[row.directory])
        )
        lines = ["Vaultkeep — Verify", ""]
        if self._destination is not None:
            lines += [f"Destination: {self._destination}", ""]
        table: list[tuple[str, ...]] = [
            (labels[row.directory], row.manifest, row.checksum, row.archive) for row in rows
        ]
        headings = ("Backup", "Manifest", "SHA-256", "Archive")
        lines += format_table(headings, table, stream=self._stream)
        verified = sum(row.manifest == row.checksum == row.archive == "OK" for row in rows)
        elapsed = max(0, int(monotonic() - self._started))
        minutes, seconds = divmod(elapsed, 60)
        total = f"{len(rows)} backups"
        if self._unknown_total or 1 not in self._completed:
            total = f"{len(rows)} known backups (total unknown)"
        complete = all(row.manifest == row.checksum == row.archive == "OK" for row in rows)
        result = "VERIFIED" if success and complete and not self._unknown_total else "FAILED"
        lines += [
            f"Verified:  {verified}/{total}",
            f"Duration:  {minutes:02d}:{seconds:02d}",
            f"Result:    {result}",
        ]
        for directory, check, reason, path in self._failures:
            lines += [
                "",
                "Failure:",
                f"  Backup: {labels.get(directory, 'Unknown') if directory else 'Destination'}",
                f"  Check:  {check}",
                f"  Error:  {reason}",
                f"  Path:   {path}",
            ]
        if error is not None:
            message = str(error) or type(error).__name__
            lines += ["", f"Error: {message}"]
        for line in lines:
            self._stream.write(self._progress._safe_text(line, stream=self._stream) + "\n")
        self._stream.flush()
