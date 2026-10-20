"""Process inspection provider backed by psutil.

Detection reads a frozen snapshot and re-validates identity around metadata reads so
that an exit/replacement race cannot silently substitute a different process.
"""

from __future__ import annotations

import os
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

    def alive(self, pid: int, create_time: float) -> bool | None:
        """Return whether ``pid`` is still the same process (pid + create time)."""


class PsutilProcessProvider:
    """Real process provider using psutil."""

    def __init__(self) -> None:
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
            if uids.real != os.geteuid():
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

    @staticmethod
    def alive(pid: int, create_time: float) -> bool | None:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return False
            return abs(proc.create_time() - create_time) < 0.01
        except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            return False
        except psutil.AccessDenied:
            return None
