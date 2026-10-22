"""Best-effort live backup phases, separate from durable completion evidence."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import psutil  # type: ignore[import-untyped]

PROGRESS_PATH_ENV = "VAULTKEEP_PROGRESS_PATH"
HEARTBEAT_SECONDS = 10
STALE_SECONDS = 3 * HEARTBEAT_SECONDS
_RECORD_CAP = 4096
_LOG = logging.getLogger("vaultkeep.progress")

ProgressState = Literal["preparing", "running", "finished"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class BackupProgress:
    attempt_id: str
    state: ProgressState
    phase: str
    started_at: str
    phase_started_at: str
    updated_at: str
    pid: int
    create_time: float
    schema_version: int = 1


def read_progress(path: Path, attempt_id: str) -> BackupProgress | None:
    """Missing, incompatible or damaged telemetry must not block recovery or status."""
    try:
        with path.open("rb") as stream:
            payload = stream.read(_RECORD_CAP + 1)
        if len(payload) > _RECORD_CAP:
            return None
        data = json.loads(payload)
        if not isinstance(data, dict) or "schema_version" not in data:
            return None
        record = BackupProgress(**data)
        if (
            type(record.schema_version) is not int
            or record.schema_version != 1
            or record.attempt_id != attempt_id
            or record.state not in ("preparing", "running", "finished")
            or not isinstance(record.phase, str)
            or not 0 < len(record.phase) <= 160
            or not record.phase.isprintable()
            or not all(
                isinstance(value, str)
                for value in (record.started_at, record.phase_started_at, record.updated_at)
            )
            or type(record.pid) is not int
            or record.pid <= 0
            or type(record.create_time) not in (int, float)
            or not math.isfinite(record.create_time)
            or record.create_time <= 0
        ):
            return None
        return record
    except (OSError, ValueError, TypeError, OverflowError):
        return None


class ProgressReporter:
    """Publish atomic snapshots; failures in optional telemetry never fail a backup."""

    def __init__(self, path: Path, *, state: ProgressState) -> None:
        self._path = path
        self._state = state
        self._record: BackupProgress | None = None
        self._disabled = False

    def report(self, phase: str) -> None:
        phase = "".join(c if c.isprintable() else " " for c in phase).strip()[:160]
        if self._disabled or not phase:
            return
        now = _now()
        try:
            if self._record is None:
                self._record = BackupProgress(
                    attempt_id=self._path.parent.name,
                    state=self._state,
                    phase=phase,
                    started_at=now,
                    phase_started_at=now,
                    updated_at=now,
                    pid=os.getpid(),
                    create_time=psutil.Process().create_time(),
                )
            elif self._record.phase != phase:
                self._record.phase = phase
                self._record.phase_started_at = now
            self._publish(now)
        except (OSError, psutil.Error) as error:
            self._disable(error)

    def heartbeat(self) -> None:
        if self._record is not None and not self._disabled:
            try:
                self._publish(_now())
            except OSError as error:
                self._disable(error)

    def finish(self) -> None:
        if self._record is not None:
            self._record.state = "finished"
            self.heartbeat()

    def _disable(self, error: Exception) -> None:
        self._disabled = True
        _LOG.warning("Live backup progress unavailable: %s", error)

    def _publish(self, now: str) -> None:
        assert self._record is not None
        self._record.updated_at = now
        # Telemetry needs atomic visibility, not durable fsyncs on every heartbeat.
        # mkstemp keeps each snapshot private, including after replacement.
        descriptor, name = tempfile.mkstemp(prefix=".progress-", dir=self._path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(asdict(self._record), stream, ensure_ascii=True)
            os.replace(temporary, self._path)
        finally:
            with suppress(OSError):
                temporary.unlink()
