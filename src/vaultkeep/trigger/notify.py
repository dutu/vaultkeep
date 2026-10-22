"""Best-effort desktop notifications delivered without blocking the monitor loop."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from time import monotonic
from typing import Protocol

from vaultkeep.trigger.config import NotificationConfig
from vaultkeep.trigger.desktop import notification_bus

_LOG = logging.getLogger("vaultkeep.trigger.notifications")


class Notifier(Protocol):
    """Interface for best-effort notifications."""

    def notify(
        self, title: str, body: str, *, key: str | None = None, finished: bool = False
    ) -> None:
        """Deliver a notification without raising on failure."""


class NullNotifier:
    """Notifier that does nothing (notifications disabled)."""

    def notify(
        self, title: str, body: str, *, key: str | None = None, finished: bool = False
    ) -> None:
        return None


class NotifySendNotifier:
    """Deliver notifications with a bounded background subprocess."""

    def __init__(self, config: NotificationConfig) -> None:
        self._config = config
        self._replace_supported = Path(config.command).name == "notify-send"
        self._notification_ids: dict[str, int] = {}
        self._bus_address: str | None = None
        self._pending: deque[tuple[str, str, str | None, bool]] = deque()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def notify(
        self, title: str, body: str, *, key: str | None = None, finished: bool = False
    ) -> None:
        if not self._config.enabled:
            return
        # One worker preserves ordering: a slow phase update cannot overwrite the
        # terminal result. Idle notifiers retain no waiting thread.
        with self._lock:
            self._pending.append((title, body, key, finished))
            if self._worker is None:
                self._worker = threading.Thread(target=self._drain, daemon=True)
                try:
                    self._worker.start()
                except RuntimeError:
                    self._worker = None
                    self._pending.clear()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._worker = None
                    return
                notification = self._pending.popleft()
            self._deliver(*notification)

    def _deliver(self, title: str, body: str, key: str | None, finished: bool) -> None:
        timeout = self._config.timeout_ms / 1000.0
        deadline = monotonic() + timeout
        environment: dict[str, str] | None = None
        if self._replace_supported:
            address = notification_bus(monotonic() + min(2.0, timeout / 2))
            if address != self._bus_address:
                self._notification_ids.clear()
                self._bus_address = address
            if address is None:
                if finished and key is not None:
                    self._notification_ids.pop(key, None)
                return
            environment = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address)
        argv = [self._config.command]
        replace = self._replace_supported and key is not None
        if replace:
            argv.append("--print-id")
            if key in self._notification_ids:
                argv.append(f"--replace-id={self._notification_ids[key]}")
            argv.extend([f"--expire-time={-1 if finished else 0}", "--"])
        argv.extend([title, body])
        remaining = deadline - monotonic()
        if remaining <= 0:
            _LOG.warning("Desktop notification delivery deadline exceeded")
            if finished and key is not None:
                self._notification_ids.pop(key, None)
            return
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=remaining,
                check=False,
                env=environment,
            )
            if result.returncode:
                detail = result.stderr.decode("utf-8", errors="replace")[:400]
                detail = "".join(
                    character if character.isprintable() else " " for character in detail
                )
                detail = " ".join(detail.split())
                _LOG.warning(
                    "Desktop notification command failed (exit %s): %s", result.returncode, detail
                )
            if replace and key is not None:
                notification_id = int(result.stdout.strip()) if result.returncode == 0 else 0
                if 0 < notification_id <= 0xFFFFFFFF:
                    self._notification_ids[key] = notification_id
                else:
                    self._notification_ids.pop(key, None)
                    if result.returncode == 0:
                        _LOG.warning(
                            "Desktop notification command returned an invalid notification ID"
                        )
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            _LOG.warning("Desktop notification delivery failed: %s", error)
            if key is not None:
                self._notification_ids.pop(key, None)
        finally:
            if finished and key is not None:
                self._notification_ids.pop(key, None)


def build_notifier(config: NotificationConfig) -> Notifier:
    """Return the notifier for the given configuration."""
    if config.enabled:
        return NotifySendNotifier(config)
    return NullNotifier()
