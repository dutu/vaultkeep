"""Find a same-user desktop notification bus without changing service environments."""

from __future__ import annotations

import logging
import os
import shlex
import socket
import struct
import subprocess
from pathlib import Path
from time import monotonic
from typing import Literal
from urllib.parse import quote, unquote

import psutil  # type: ignore[import-untyped]

_LOG = logging.getLogger("vaultkeep.notifications")
_BUS_ENV = "DBUS_SESSION_BUS_ADDRESS"
_NOTIFICATIONS = "org.freedesktop.Notifications"


def _runtime_dir() -> Path | None:
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value:
        return None
    path = Path(value)
    try:
        if path.is_absolute() and path.is_dir() and path.stat().st_uid == os.geteuid():
            return path
    except OSError:
        pass
    return None


def _local_address(address: str) -> tuple[str, str] | None:
    """Accept one local Unix socket; normalize addresses with/without a bus GUID."""
    if not address.startswith("unix:") or len(address) > 4096 or ";" in address:
        return None
    options: dict[str, str] = {}
    for item in address[5:].split(","):
        key, separator, value = item.partition("=")
        if not separator or key in options or key not in ("path", "abstract", "guid"):
            return None
        options[key] = unquote(value)
    if ("path" in options) == ("abstract" in options):
        return None
    kind = "path" if "path" in options else "abstract"
    value = options[kind]
    if not value or "\0" in value or (kind == "path" and not os.path.isabs(value)):
        return None
    return f"unix:{kind}={quote(value, safe='/')}", value if kind == "path" else "\0" + value


def _query(address: str, method: str, deadline: float, *arguments: str) -> bytes | None:
    remaining = min(0.5, deadline - monotonic())
    if remaining <= 0:
        return None
    try:
        result = subprocess.run(
            [
                "busctl",
                f"--address={address}",
                f"--timeout={remaining}s",
                "--auto-start=no",
                "--allow-interactive-authorization=no",
                "call",
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                method,
                *arguments,
            ],
            capture_output=True,
            timeout=remaining,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None
    except FileNotFoundError:
        _LOG.warning("Desktop notification discovery requires busctl (provided by systemd)")
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _notification_service(
    address: str, deadline: float
) -> Literal["running", "activatable"] | None:
    local = _local_address(address)
    remaining = min(0.2, deadline - monotonic())
    if local is None or remaining <= 0:
        return None
    try:
        # Never send backup details to a socket owned by another account. This
        # also supports abstract sockets, which have no filesystem ownership.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(remaining)
            peer.connect(local[1])
            credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack("3i", credentials)[1] != os.geteuid():
                return None
    except (OSError, ValueError):
        return None
    owned = _query(local[0], "NameHasOwner", deadline, "s", _NOTIFICATIONS)
    if owned is not None and owned.strip() == b"b true":
        return "running"
    if owned is None or owned.strip() != b"b false":
        return None
    # Preserve normal desktop activation when its notification daemon is started
    # on demand; discovery itself never starts a daemon or creates a new bus.
    activatable = _query(local[0], "ListActivatableNames", deadline)
    try:
        if activatable is not None and _NOTIFICATIONS in shlex.split(activatable.decode()):
            return "activatable"
    except (UnicodeError, ValueError):
        pass
    return None


def _desktop_addresses(deadline: float) -> set[str] | None:
    addresses: set[str] = set()
    uid = os.geteuid()
    try:
        for process in psutil.process_iter(["uids"]):
            if monotonic() >= deadline:
                return None
            try:
                uids = process.info["uids"]
                if uids is None or uids.real != uid or uids.effective != uid:
                    continue
                environment = process.environ()
                # Revalidate identity/ownership after the environment read.
                if not process.is_running() or process.uids().effective != uid:
                    continue
                if not (environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY")):
                    continue
                local = _local_address(environment.get(_BUS_ENV, ""))
                if local:
                    addresses.add(local[0])
            except (psutil.Error, OSError):
                continue
    except (psutil.Error, OSError):
        return None
    return addresses


def notification_bus(deadline: float) -> str | None:
    """Prefer a working configured/user bus, otherwise require one desktop bus.

    This runs in the notifier's background worker, with a shared deadline. No
    session address is persisted or imported into the systemd user manager.
    """
    preferred: list[str] = []
    inherited = _local_address(os.environ.get(_BUS_ENV, ""))
    if inherited:
        preferred.append(inherited[0])
    runtime = _runtime_dir()
    if runtime is not None:
        preferred.append(f"unix:path={quote(str(runtime / 'bus'), safe='/')}")
    checked: set[str] = set()
    activatable: list[str] = []
    for address in preferred:
        if address not in checked:
            service = _notification_service(address, deadline)
            if service == "running":
                return address
            if service == "activatable":
                activatable.append(address)
        checked.add(address)
    candidates = _desktop_addresses(deadline)
    if candidates is None and activatable and monotonic() < deadline:
        # Preserve configured desktop activation when process discovery is unavailable.
        return activatable[0]
    if candidates is not None:
        available = []
        for address in sorted(candidates - checked):
            if monotonic() >= deadline:
                break
            service = _notification_service(address, deadline)
            if service == "running":
                available.append(address)
            elif service == "activatable":
                activatable.append(address)
        else:
            if len(available) == 1:
                return available[0]
            # A session-specific running daemon takes precedence over a generic
            # bus that merely advertises an activatable desktop service.
            if not available:
                for address in preferred:
                    if address in activatable:
                        return address
                if len(activatable) == 1:
                    return activatable[0]
                available = activatable
            if len(available) > 1:
                _LOG.warning("Desktop notification skipped: multiple graphical session buses found")
                return None
    _LOG.warning("Desktop notification skipped: no accessible graphical notification bus found")
    return None
