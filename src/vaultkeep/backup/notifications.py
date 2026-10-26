"""Backup-owned notification messages, driven by the execution lifecycle."""

from __future__ import annotations

import logging
from datetime import datetime
from time import monotonic

from vaultkeep.backup.progress import HEARTBEAT_SECONDS
from vaultkeep.backup.records import Attempt
from vaultkeep.diagnostics import concise, parse_utc
from vaultkeep.notifications.notifier import NotificationConfig, build_notifier

_LOG = logging.getLogger(__name__)


class BackupNotifications:
    def __init__(
        self, config: NotificationConfig, job_id: str, execution_id: str, *, per_file: bool
    ) -> None:
        self._notifier = build_notifier(config)
        self._enabled = config.enabled
        self._job_id = job_id
        self._key = f"backup:{execution_id}"
        self._per_file = per_file
        self._phase = ""
        self._phase_started = 0.0
        self._notified = 0.0

    def _send(
        self,
        title: str,
        body: str,
        *,
        key: str,
        finished: bool = False,
        replace_pending: bool = False,
    ) -> None:
        if not self._enabled:
            return
        try:
            self._notifier.notify(
                f"VaultKeep: Backup {title}",
                f"job {self._job_id}: {body}",
                key=key,
                finished=finished,
                replace_pending=replace_pending,
            )
        except Exception as error:
            _LOG.warning("Backup notification unavailable: %s", error)

    def started(self) -> None:
        self._notified = monotonic()
        self._send("started", "execution started", key=self._key)

    def report(self, phase: str) -> None:
        if not self._enabled:
            return
        phase = concise(phase)
        now = monotonic()
        changed = phase != self._phase
        if changed:
            self._phase = phase
            self._phase_started = now
        # Per-file workflows repeat phases rapidly: cap all job-level updates.
        if (self._per_file or not changed) and now - self._notified < HEARTBEAT_SECONDS:
            return
        self._notified = now
        elapsed = max(0, int(now - self._phase_started))
        body = phase if changed else f"{phase} ({elapsed}s)"
        self._send("running", body, key=self._key, replace_pending=True)

    def heartbeat(self) -> None:
        if self._phase:
            self.report(self._phase)

    def completed(self, attempt: Attempt) -> None:
        body = {
            "SUCCESS": "new archive created",
            "UNCHANGED": "sources unchanged; no new archive",
            "FAILED": "failed",
            "INTERRUPTED": "interrupted",
            "UNKNOWN": "outcome unknown",
        }[attempt.result or "UNKNOWN"]
        if attempt.reason:
            body += f": {concise(attempt.reason)}"
        if attempt.cleanup_errors:
            body += f"; cleanup failed: {concise(attempt.cleanup_errors[0])}"
        ended = parse_utc(attempt.completed_at)
        local_time = ended.astimezone() if ended else datetime.now().astimezone()
        body += f"\n{local_time:%Y-%m-%d %H:%M:%S %Z} (local time)"
        self._send("completed", body, key=f"backup:{attempt.execution_id}", finished=True)

    def close(self) -> None:
        try:
            self._notifier.close()
        except Exception as error:
            _LOG.warning("Backup notification shutdown unavailable: %s", error)
