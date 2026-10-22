"""Best-effort desktop notifications delivered without blocking the monitor loop."""

from __future__ import annotations

import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Protocol

from vaultkeep.trigger.config import NotificationConfig


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
        argv = [self._config.command]
        replace = self._replace_supported and key is not None
        if replace:
            argv.append("--print-id")
            if key in self._notification_ids:
                argv.append(f"--replace-id={self._notification_ids[key]}")
            argv.extend([f"--expire-time={-1 if finished else 0}", "--"])
        argv.extend([title, body])
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if replace and key is not None:
                notification_id = int(result.stdout.strip()) if result.returncode == 0 else 0
                if 0 < notification_id <= 0xFFFFFFFF:
                    self._notification_ids[key] = notification_id
                else:
                    self._notification_ids.pop(key, None)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            # Missing binary/session bus, timeout, or invalid notification ID.
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
