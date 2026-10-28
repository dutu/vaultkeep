"""Job ownership and destination locks shared by manual and systemd executions."""

from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.errors import DestinationError, LockError
from vaultkeep.installation import installation_path

DESTINATION_LOCK_NAME = ".vaultkeep.lock"
_NETWORK_FILESYSTEMS = frozenset({"cifs", "smb3", "nfs", "nfs4"})
_LOCAL_FILESYSTEMS = frozenset({"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "tmpfs", "overlay"})
_HELD_DESTINATION: ContextVar[tuple[Path, str] | None] = ContextVar(
    "vaultkeep_destination_lock", default=None
)


@dataclass(frozen=True, slots=True)
class _DestinationMount:
    mount_id: int
    directory: Path
    root: str
    filesystem: str
    options: frozenset[str]


def held_destination_filesystem(root: Path) -> str | None:
    """Return the filesystem only for the exact locked history directory."""
    held = _HELD_DESTINATION.get()
    if held is not None and root.resolve(strict=True) == held[0]:
        return held[1]
    return None


def requires_deployment_lock(command: str | None, action: str | None) -> bool:
    """Identify commands that must not overlap deployment maintenance."""
    return command in ("run", "prune", "verify") or (
        command in ("schedule", "schedules", "trigger")
        and action not in ("next", "list", "validate")
    )


@contextmanager
def deployment_lock() -> Iterator[None]:
    """Keep installed code in place while a command uses it; fail without waiting."""
    path = installation_path("VAULTKEEP_INSTALL_PREFIX") / "maintenance.lock"
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
        if not value:
            return ""
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


@contextmanager
def destination_lock(root: Path) -> Iterator[None]:
    """Serialize writers independently of YAML path, user, and destination aliases.

    Every history uses a persistent regular lock file in its own directory.
    Network clients must expose the same file through server-backed locking.
    """
    import fcntl

    descriptors: list[int] = []
    try:
        canonical = root.resolve(strict=True)
        mounted = _validate_destination_mounts(canonical)
        if mounted.filesystem in _NETWORK_FILESYSTEMS:
            _validate_network_locking(mounted)
        elif mounted.filesystem not in _LOCAL_FILESYSTEMS:
            raise DestinationError(
                f"Reliable shared locking is unsupported on destination filesystem: {root}"
            )
        lock = canonical / DESTINATION_LOCK_NAME
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o666)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DestinationError(f"Shared destination lock must be a regular file: {lock}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except PermissionError as error:
            # Linux SMB also maps remote lock conflicts to EACCES.
            # Anchor open/access failures above remain destination errors.
            if mounted.filesystem in {"cifs", "smb3"} and error.errno == errno.EACCES:
                raise LockError(
                    f"Destination history lock is busy or denied by the server: {lock}"
                ) from error
            raise
        current = lock.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise DestinationError(f"Shared destination lock changed during acquisition: {lock}")
    except BaseException as error:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        if isinstance(error, BlockingIOError):
            raise LockError(f"Destination history is already locked: {root}") from error
        if isinstance(error, FileNotFoundError):
            raise DestinationError(
                f"Destination root is not an accessible directory: {root}"
            ) from error
        if isinstance(error, OSError):
            raise DestinationError(
                f"Cannot establish destination locking at {root}: {error}"
            ) from error
        raise
    token = _HELD_DESTINATION.set((canonical, mounted.filesystem))
    try:
        yield
    finally:
        _HELD_DESTINATION.reset(token)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _validate_network_locking(mount: _DestinationMount) -> None:
    """Reject mount settings that silently turn shared locks into client-local locks."""
    if "ro" in mount.options:
        raise DestinationError(f"Destination share is mounted read-only: {mount.directory}")
    if mount.filesystem in {"cifs", "smb3"}:
        unsafe = "nobrl" in mount.options
    else:
        unsafe = bool({"nolock", "local_lock=all", "local_lock=flock"} & mount.options)
    if unsafe:
        raise DestinationError(
            f"Destination mount disables cross-machine locking: {mount.directory}; "
            "enable server locks (CIFS without nobrl; NFS without nolock or local_lock=all/flock)"
        )


def _validate_destination_mounts(root: Path) -> _DestinationMount:
    """Inspect the visible mount and reject mounts nested inside a history."""

    def decode(value: str) -> str:
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)

    try:
        entries = []
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            before, after = line.split(" - ", 1)
            fields = before.split()
            filesystem, _source, options = after.split()[:3]
            if len(fields) < 6:
                raise ValueError("incomplete mount record")
            entries.append(
                _DestinationMount(
                    int(fields[0]),
                    Path(decode(fields[4])),
                    decode(fields[3]),
                    filesystem,
                    frozenset((*fields[5].split(","), *options.split(","))),
                )
            )
        mount_id = _destination_mount_id(root)
        containing = [
            mount
            for mount in entries
            if mount.mount_id == mount_id and root.is_relative_to(mount.directory)
        ]
        if len(containing) != 1:
            raise ValueError("destination mount cannot be established")
        mounted = containing[0]
        if any(
            mount.directory != root and mount.directory.is_relative_to(root) for mount in entries
        ):
            raise ValueError("nested destination mounts require independent lock anchors")
        return mounted
    except (OSError, ValueError) as error:
        raise DestinationError(
            f"Unsupported destination mount layout at {root}: {error}"
        ) from error


def _destination_mount_id(root: Path) -> int:
    """Use the opened directory's mount ID, including when mounts are stacked."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for line in (
            Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="ascii").splitlines()
        ):
            if line.startswith("mnt_id:"):
                return int(line.partition(":")[2].strip())
        raise ValueError("directory mount ID is unavailable")
    finally:
        os.close(descriptor)
