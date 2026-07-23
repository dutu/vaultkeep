"""Advisory per-job locks shared by manual and systemd executions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.errors import LockError


@dataclass(slots=True)
class JobLock:
    """One non-blocking process lock backed by a stable lock file."""

    path: Path
    _descriptor: int | None = None

    def acquire(self) -> None:
        """Acquire without waiting, or report that another job run owns the lock."""
        if os.name != "posix":
            return
        self.path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl = __import__("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise LockError(f"Backup job is already running: {self.path}") from error
        self._descriptor = descriptor

    def release(self) -> None:
        """Release the owned lock file descriptor."""
        if self._descriptor is None:
            return
        descriptor = self._descriptor
        self._descriptor = None
        try:
            fcntl = __import__("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def job_lock_path(*, root: Path, job_id: str, identity_hash: str) -> Path:
    """Return the design-specified stable per-job lock path."""
    return root / f"{job_id}-{identity_hash}.lock"
