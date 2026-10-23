"""Terminal stage reporting, independent of persisted backup progress."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Lock, Thread
from time import monotonic

from vaultkeep.backup.progress import HEARTBEAT_SECONDS
from vaultkeep.cli.output import Console

_HEARTBEAT_SECONDS = HEARTBEAT_SECONDS


@contextmanager
def terminal_progress(*, no_color: bool = False) -> Iterator[Callable[[str], None] | None]:
    """Present workflow phases on an interactive terminal."""
    stream = sys.stderr
    output = Console(no_color=no_color)
    terminal = stream.isatty()
    if not terminal:
        yield None
        return

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
                write(message)

    def heartbeat() -> None:
        while not stopped.wait(_HEARTBEAT_SECONDS):
            with lock:
                if current_stage is not None and not stopped.is_set():
                    write(f"{current_stage} (still running)")

    worker = Thread(target=heartbeat, name="vaultkeep-progress", daemon=True)
    worker.start()
    try:
        yield report
    finally:
        stopped.set()
        worker.join()
