"""The one lock-owning lifecycle used by every backup initiator."""

from __future__ import annotations

import logging
import os
import signal
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from threading import Event, Lock, Thread, current_thread, main_thread
from time import monotonic
from types import FrameType, TracebackType
from typing import Any

import psutil  # type: ignore[import-untyped]

from vaultkeep.backup.notifications import BackupNotifications
from vaultkeep.backup.paths import WorkflowPaths
from vaultkeep.backup.progress import HEARTBEAT_SECONDS, ProgressReporter
from vaultkeep.backup.records import Attempt, ExecutionHistory, read_history
from vaultkeep.backup.results import BackupInterrupted, failure_exit_code, failure_reason
from vaultkeep.diagnostics import concise, utc_now
from vaultkeep.errors import StateError
from vaultkeep.locking import JobLock, job_lock_path
from vaultkeep.notifications.notifier import NotificationConfig
from vaultkeep.state.atomic import write_json

_LOG = logging.getLogger(__name__)


class _ExecutionLog(RotatingFileHandler):
    def _open(self) -> TextIOWrapper[Any]:
        descriptor = os.open(
            self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        return os.fdopen(descriptor, "a", encoding="utf-8")

    def handleError(self, record: logging.LogRecord) -> None:
        # A full/unavailable log filesystem must never interrupt execution.
        pass


class Execution:
    """Own one execution lock and its lifecycle, with optional operational reporting."""

    def __init__(
        self,
        paths: WorkflowPaths,
        job_id: str,
        identity: str,
        *,
        progress: Callable[[str], None] | None = None,
        notifications: NotificationConfig | None = None,
        per_file: bool = False,
    ) -> None:
        self.directory = paths.state_root / f"{job_id}-{identity}"
        self.lock = JobLock(
            job_lock_path(root=paths.lock_root, job_id=job_id, identity_hash=identity)
        )
        self.history = ExecutionHistory(
            schema_version=1, job_id=job_id, identity_hash=identity, current=None, previous=None
        )
        self._progress = progress
        self._stopped = Event()
        self._mutex = Lock()
        self._thread: Thread | None = None
        self._handler: _ExecutionLog | None = None
        self._old_term: signal._HANDLER | None = None
        self._started = monotonic()
        self.execution_id = uuid.uuid4().hex
        self._reporter = ProgressReporter(
            self.directory / "progress.json", execution_id=self.execution_id
        )
        self._result: str | None = None
        self._reason: str | None = None
        self._cleanup_errors: tuple[str, ...] = ()
        self._notifications = BackupNotifications(
            notifications or NotificationConfig(), job_id, self.execution_id, per_file=per_file
        )

    def __enter__(self) -> Execution:
        self.lock.acquire()
        try:
            self._started = monotonic()
            # A stable-size stamp also works on filesystems with coarse timestamps.
            try:
                self.lock.record_execution(self.execution_id)
            except OSError as error:
                _LOG.warning("Execution lock stamp unavailable: %s", error)
            previous = None
            existing = None
            try:
                existing = read_history(
                    self.directory / "execution.json",
                    self.history.job_id,
                    self.history.identity_hash,
                )
                if existing:
                    previous = existing.previous
                    if existing.current:
                        previous = existing.current.model_copy(
                            update={
                                "result": "UNKNOWN",
                                "reason": "Backup completion could not be verified",
                            }
                        )
            except StateError as error:
                _LOG.warning("%s", error)
            try:
                create_time = psutil.Process().create_time()
            except psutil.Error:
                create_time = None
            attempt = Attempt(
                execution_id=self.execution_id,
                started_at=utc_now(),
                pid=os.getpid(),
                create_time=create_time,
            )
            self.history = self.history.model_copy(
                update={"current": attempt, "previous": previous}
            )
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._handler = _ExecutionLog(
                    self.directory / "execution.log", maxBytes=65536, backupCount=1
                )
                os.chmod(self.directory / "execution.log", 0o600)
                self._handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s %(message)s")
                )
                logging.getLogger("vaultkeep").addHandler(self._handler)
            except OSError as error:
                _LOG.warning("Backup execution log unavailable: %s", error)
            self._save()
            if current_thread() is main_thread():
                self._old_term = signal.signal(signal.SIGTERM, self._terminate)
            if previous is not None and existing is not None and existing.current is not None:
                self._notifications.completed(previous)
            self._notifications.started()
            self.report("Preparing backup")
            self._thread = Thread(target=self._heartbeat, name="vaultkeep-execution", daemon=True)
            try:
                self._thread.start()
            except RuntimeError as error:
                self._thread = None
                _LOG.warning("Backup heartbeat unavailable: %s", error)
            return self
        except BaseException:
            self._close()
            raise

    def report(self, phase: str) -> None:
        with self._mutex:
            self._reporter.report(phase)
            self._notifications.report(phase)
        if self._progress is not None:
            # Presentation callbacks are optional telemetry too.
            try:
                self._progress(phase)
            except Exception as error:
                _LOG.warning("Backup progress callback unavailable: %s", error)

    def _heartbeat(self) -> None:
        while not self._stopped.wait(HEARTBEAT_SECONDS):
            with self._mutex:
                self._reporter.heartbeat()
                self._notifications.heartbeat()

    def complete(
        self, result: str, *, errors: Sequence[str] = (), cleanup_errors: Sequence[str] = ()
    ) -> None:
        self._result = {"created": "SUCCESS", "unchanged": "UNCHANGED", "failed": "FAILED"}[result]
        self._cleanup_errors = tuple(concise(error) for error in cleanup_errors[:8])
        if len(cleanup_errors) > 8:
            self._cleanup_errors += (
                f"{len(cleanup_errors) - 8} additional cleanup errors; see execution log",
            )
        if errors:
            self._reason = concise(errors[0])

    @staticmethod
    def _terminate(signum: int, frame: FrameType | None) -> None:
        raise BackupInterrupted(signum)

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._stopped.set()
            if self._thread:
                self._thread.join()
            self._reporter.finish()
            interrupted = isinstance(error, (BackupInterrupted, KeyboardInterrupt, SystemExit))
            result = (
                "INTERRUPTED" if interrupted else "FAILED" if error else self._result or "UNKNOWN"
            )
            if error:
                _LOG.error("Backup %s %s: %s", self.execution_id, result, error, exc_info=error)
            assert self.history.current is not None
            completed = self.history.current.model_copy(
                update={
                    "result": result,
                    "completed_at": utc_now(),
                    "duration_seconds": monotonic() - self._started,
                    "exit_code": failure_exit_code(error)
                    if error
                    else 7
                    if result == "FAILED"
                    else 0,
                    "signal": error.signum if isinstance(error, BackupInterrupted) else None,
                    "reason": failure_reason(error) if error else self._reason,
                    "cleanup_errors": list(self._cleanup_errors),
                }
            )
            self.history = self.history.model_copy(update={"current": None, "previous": completed})
            self._save()
            self._notifications.completed(completed)
        finally:
            self._close()

    def _save(self) -> None:
        try:
            write_json(self.directory / "execution.json", self.history.model_dump(mode="json"))
        except StateError as error:
            _LOG.warning("Backup completion evidence unavailable: %s", error)

    def _close(self) -> None:
        if self._old_term is not None:
            signal.signal(signal.SIGTERM, self._old_term)
        if self._handler:
            logging.getLogger("vaultkeep").removeHandler(self._handler)
            with suppress(OSError):
                self._handler.close()
        self.lock.release()
        self._notifications.close()
