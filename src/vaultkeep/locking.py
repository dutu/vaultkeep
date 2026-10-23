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
        and action not in ("next", "list", "validate")
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
        descriptor = os.open(
            self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise LockError(f"Invalid job lock: {self.path}")
            fcntl = __import__("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise LockError(f"Backup job is already running: {self.path}") from error
        except BaseException:
            os.close(descriptor)
            raise
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

    def record_execution(self, execution_id: str) -> None:
        """Stamp the held lock so failed telemetry cannot expose an old result as current."""
        if self._descriptor is None:
            return
        os.lseek(self._descriptor, 0, os.SEEK_SET)
        if os.write(self._descriptor, execution_id.encode("ascii")) != len(execution_id):
            raise OSError("Incomplete execution lock stamp")
        os.ftruncate(self._descriptor, len(execution_id))


def job_lock_execution(path: Path) -> str | None:
    """Read the lock's execution stamp; empty means absent, None means unavailable."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        value = os.read(descriptor, 33).decode("ascii")
        return value if len(value) == 32 and all(c in "0123456789abcdef" for c in value) else None
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(descriptor)


def job_lock_path(*, root: Path, job_id: str, identity_hash: str) -> Path:
    """Return the design-specified stable per-job lock path."""
    return root / f"{job_id}-{identity_hash}.lock"


def job_lock_owner(path: Path) -> int | None:
    """Read Linux flock ownership without acquiring a lock or creating files.

    Zero means unowned, None means unavailable. A status probe must never briefly
    hold a shared lock that could reject a backup starting at the same instant.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        with open("/proc/locks", encoding="ascii") as stream:
            for line in stream:
                fields = line.split()
                if len(fields) < 8 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
                    continue
                major, minor, inode = fields[5].split(":")
                if (int(major, 16), int(minor, 16), int(inode)) == (
                    os.major(info.st_dev),
                    os.minor(info.st_dev),
                    info.st_ino,
                ):
                    pid = int(fields[4])
                    return pid if pid > 0 else None
        return 0
    except (OSError, ValueError, UnicodeError):
        return None
    finally:
        os.close(descriptor)
