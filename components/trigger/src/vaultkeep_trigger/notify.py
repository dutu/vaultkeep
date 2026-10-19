"""Best-effort desktop notifications delivered without blocking the monitor loop."""

from __future__ import annotations

import subprocess
import threading
from typing import Protocol

from vaultkeep_trigger.config import NotificationConfig


class Notifier(Protocol):
    """Interface for best-effort notifications."""

    def notify(self, title: str, body: str) -> None:
        """Deliver a notification without raising on failure."""


class NullNotifier:
    """Notifier that does nothing (notifications disabled)."""

    def notify(self, title: str, body: str) -> None:
        return None


class NotifySendNotifier:
    """Deliver notifications with a bounded background subprocess."""

    def __init__(self, config: NotificationConfig) -> None:
        self._config = config

    def notify(self, title: str, body: str) -> None:
        if not self._config.enabled:
            return
        thread = threading.Thread(target=self._deliver, args=(title, body), daemon=True)
        thread.start()

    def _deliver(self, title: str, body: str) -> None:
        timeout = self._config.timeout_ms / 1000.0
        try:
            subprocess.run(
                [self._config.command, title, body],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Delivery failure, missing binary, or absent session bus: log and ignore.
            return


def build_notifier(config: NotificationConfig) -> Notifier:
    """Return the notifier for the given configuration."""
    if config.enabled:
        return NotifySendNotifier(config)
    return NullNotifier()
