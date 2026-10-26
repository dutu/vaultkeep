"""Process inspection provider backed by psutil.

Detection reads a frozen snapshot and re-validates identity around metadata reads so
that an exit/replacement race cannot silently substitute a different process.
"""

from __future__ import annotations

import os
import pwd
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

import psutil

from vaultkeep.trigger.matcher import ProcessInfo

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def boot_id() -> str | None:
    """Return the kernel boot ID, or ``None`` when it cannot be read."""
    try:
        return _BOOT_ID_PATH.read_text(encoding="ascii").strip()
    except OSError:
        return None


class ProcessProvider(Protocol):
    """Interface for process inspection (replaceable in tests)."""

    def snapshot(self) -> list[ProcessInfo]:
        """Return a frozen snapshot of the current processes."""

    def read_env(self, pid: int, keys: Iterable[str]) -> dict[str, str] | None:
        """Return the requested environment keys, or ``None`` when unreadable."""

    def resolve_directory(self, pid: int, value: str) -> str | None:
        """Resolve a process-reported directory using verified owner/home/cwd metadata."""

    def alive(self, pid: int, create_time: float) -> bool | None:
        """Return whether ``pid`` is still the same process (pid + create time)."""


class PsutilProcessProvider:
    """Real process provider using psutil."""

    def __init__(self, *, owner_uid: int | None = None) -> None:
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self._last: dict[int, float] = {}

    def snapshot(self) -> list[ProcessInfo]:
        infos: list[ProcessInfo] = []
        seen: dict[int, float] = {}
        for proc in psutil.process_iter(["pid", "uids"]):
            info = proc.info
            pid = proc.pid
            uids = info.get("uids")
            if uids is None:
                raise psutil.AccessDenied(pid)
            if uids.real != self.owner_uid:
                continue
            try:
                create_time = proc.create_time()
                cmdline = tuple(proc.cmdline())
                cmdline_available = True
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError):
                create_time = proc.create_time()
                cmdline = ()
                cmdline_available = False
            try:
                exe = proc.exe()
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                exe = None
            if not self.alive(pid, float(create_time)):
                raise psutil.AccessDenied(pid)
            infos.append(
                ProcessInfo(
                    pid=pid,
                    create_time=float(create_time),
                    exe_realpath=exe,
                    cmdline=cmdline,
                    cmdline_available=cmdline_available,
                )
            )
            seen[pid] = float(create_time)
        self._last = seen
        return infos

    def read_env(self, pid: int, keys: Iterable[str]) -> dict[str, str] | None:
        wanted = set(keys)
        if not wanted:
            return {}
        try:
            expected = self._last.get(pid)
            if expected is None or not self.alive(pid, expected):
                return None
            environ = psutil.Process(pid).environ()
            if not self.alive(pid, expected):
                return None
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            return None
        return {key: environ[key] for key in wanted if key in environ}

    def resolve_directory(self, pid: int, value: str) -> str | None:
        """Never resolve process paths against the backup executor's home or cwd."""
        expected = self._last.get(pid)
        try:
            if expected is None or not self.alive(pid, expected):
                return None
            if value == "~" or value.startswith("~/"):
                home = pwd.getpwuid(self.owner_uid).pw_dir
                if not os.path.isabs(home):
                    return None
                expanded = home + value[1:]
            elif value.startswith("~"):
                return None
            elif os.path.isabs(value):
                expanded = value
            else:
                cwd = psutil.Process(pid).cwd()
                if not os.path.isabs(cwd):
                    return None
                expanded = os.path.join(cwd, value)
            resolved = os.path.realpath(expanded)
            return resolved if self.alive(pid, expected) else None
        except (KeyError, psutil.Error, OSError):
            return None

    def alive(self, pid: int, create_time: float) -> bool | None:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return False
            return (
                abs(proc.create_time() - create_time) < 0.01 and proc.uids().real == self.owner_uid
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            return False
        except psutil.AccessDenied:
            return None
