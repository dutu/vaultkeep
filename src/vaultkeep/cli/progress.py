"""Terminal-only stage reporting for manual backup runs."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Lock, Thread
from time import monotonic

_HEARTBEAT_SECONDS = 10


@contextmanager
def terminal_progress() -> Iterator[Callable[[str], None] | None]:
    """Show stages and periodic elapsed-time updates while stderr is a terminal."""
    stream = sys.stderr
    if not stream.isatty():
        yield None
        return

    started = monotonic()
    stopped = Event()
    lock = Lock()
    current_stage: str | None = None

    def write(message: str) -> None:
        elapsed = int(monotonic() - started)
        minutes, seconds = divmod(elapsed, 60)
        try:
            print(f"[{minutes:02d}:{seconds:02d}] {message}", file=stream, flush=True)
        except OSError:
            # Losing the progress stream must not interrupt the backup.
            stopped.set()

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
