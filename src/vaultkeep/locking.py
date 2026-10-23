"""Advisory per-job locks shared by manual and systemd executions."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.errors import LockError


def requires_deployment_lock(command: str | None, action: str | None) -> bool:
    """Identify commands that must not overlap deployment maintenance."""
    return command in ("run", "prune", "verify") or (
        command in ("schedule", "schedules", "trigger")
        and action not in ("status", "next", "list", "validate")
    )


@contextmanager
def deployment_lock() -> Iterator[None]:
    """Keep installed code in place while a command uses it; fail without waiting."""
    path = Path(os.environ.get("VAULTKEEP_INSTALL_PREFIX", "/opt/vaultkeep")) / "maintenance.lock"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        # Source checkouts and installations predating automatic maintenance.
        yield
        return
    except OSError as error:
        raise LockError(f"Cannot open deployment maintenance lock: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise LockError(f"Invalid deployment maintenance lock: {path}")
        try:
            fcntl = __import__("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as error:
            raise LockError(
                "Vaultkeep installation/update is in progress; retry afterward"
            ) from error
        yield
    finally:
        os.close(descriptor)


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
