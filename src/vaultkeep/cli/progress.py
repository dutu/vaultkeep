"""Terminal stage reporting and optional live progress for triggered backups."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

from vaultkeep.cli.output import Console
from vaultkeep.progress import HEARTBEAT_SECONDS, ProgressReporter

_HEARTBEAT_SECONDS = HEARTBEAT_SECONDS


@contextmanager
def terminal_progress(
    *, no_color: bool = False, progress_path: Path | None = None
) -> Iterator[Callable[[str], None] | None]:
    """Report real workflow phases to a terminal and/or a private progress record."""
    stream = sys.stderr
    output = Console(no_color=no_color)
    terminal = stream.isatty()
    if not terminal and progress_path is None:
        yield None
        return

    reporter = ProgressReporter(progress_path, state="running") if progress_path else None
    started = monotonic()
    stopped = Event()
    lock = Lock()
    current_stage: str | None = None

    def write(message: str) -> None:
        nonlocal terminal
        if not terminal:
            return
        elapsed = int(monotonic() - started)
        minutes, seconds = divmod(elapsed, 60)
        try:
            output.print(f"[{minutes:02d}:{seconds:02d}] {message}", file=stream)
        except OSError:
            # Losing the progress stream must not interrupt the command.
            terminal = False

    def report(message: str) -> None:
        nonlocal current_stage
        with lock:
            current_stage = message
            if not stopped.is_set():
                if reporter:
                    reporter.report(message)
                write(message)

    def heartbeat() -> None:
        while not stopped.wait(_HEARTBEAT_SECONDS):
            with lock:
                if current_stage is not None and not stopped.is_set():
                    if reporter:
                        reporter.heartbeat()
                    write(f"{current_stage} (still running)")

    worker = Thread(target=heartbeat, name="vaultkeep-progress", daemon=True)
    worker.start()
    try:
        yield report
    finally:
        stopped.set()
        worker.join()
        if reporter:
            reporter.finish()
