"""The foreground monitor: observe instances, confirm shutdowns, dispatch workers."""

from __future__ import annotations

import fcntl
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType

from vaultkeep.progress import STALE_SECONDS, read_progress
from vaultkeep.trigger.activity import application_status
from vaultkeep.trigger.config import INSTANCE_ID_PATTERN, InstanceConfig, TriggerConfig
from vaultkeep.trigger.detection import DetectionResult, detect
from vaultkeep.trigger.environment import build_child_environment
from vaultkeep.trigger.errors import BlockedError, StateError
from vaultkeep.trigger.lifecycle import ExecutionContext, LifecycleEvent, progress
from vaultkeep.trigger.models import (
    UNCERTAIN_OUTCOMES,
    AttemptOutcome,
    InstanceState,
    LaunchRecord,
    Observation,
    Receipt,
    RejectionState,
    Request,
    new_uuid,
    parse_utc,
    utc_now,
)
from vaultkeep.trigger.notify import Notifier
from vaultkeep.trigger.paths import monitor_lock_path
from vaultkeep.trigger.process import ProcessProvider, boot_id
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import (
    SubmissionClass,
    SubmissionResult,
    SystemdController,
)
from vaultkeep.trigger.version import (
    BLOCKED_WAIT_SECONDS,
    COMPLETED_ATTEMPT_RETENTION,
    CONTENTION_RETRY_DELAY_SECONDS,
    REJECTION_ESCALATION_THRESHOLD,
    UNCERTAIN_HISTORY_CAP,
)
from vaultkeep.version import installed_version

_LOG = logging.getLogger("vaultkeep.trigger.monitor")
_ACTIVITY_UPDATE_SECONDS = 10


def unit_name_for(attempt_id: str, scope: str | None = None) -> str:
    instance = f"{scope}_{attempt_id}" if scope else attempt_id
    return f"vaultkeep-trigger-exec@{instance}.service"


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _to_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _retry_timestamp() -> str:
    return _to_iso(_now_dt() + timedelta(seconds=CONTENTION_RETRY_DELAY_SECONDS))


class MonitorLock:
    """Non-blocking exclusive lifetime lock for the monitor."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            owner = self._read_owner()
            os.close(fd)
            raise BlockedError(
                f"another monitor holds {self._path}" + (f" (pid {owner})" if owner else "")
            ) from error
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd

    def _read_owner(self) -> str | None:
        try:
            return self._path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> MonitorLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


@dataclass(slots=True)
class _Budget:
    """Per-tick reminder of which instances must be polled now."""

    next_due: dict[str, float]


@dataclass(slots=True)
class _Activity:
    identity: tuple[str, str, str, str | None]
    started_at: float
    notified_at: float


class Monitor:
    """Own instance lifecycle state and dispatch workers."""

    def __init__(
        self,
        config: TriggerConfig,
        *,
        store: StateStore,
        provider: ProcessProvider,
        controller: SystemdController,
        notifier: Notifier,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.config = config
        self.store = store
        self.provider = provider
        self.controller = controller
        self.notifier = notifier
        self._clock = clock
        self._sleep = sleep
        self._stop = False
        self._states: dict[str, InstanceState] = {}
        self._preflight_version: str | None = None
        self._last_poll: dict[str, float] = {}
        self._last_activity: dict[str, _Activity] = {}
        self._last_application: dict[str, str] = {}
        self._started_at = utc_now()
        self._blocked_instances: dict[str, str] = {}
        for instance_id in self.store.list_blocked_ids():
            try:
                reason = self.store.read_blocked(instance_id)
            except StateError as error:
                reason = str(error)
            if reason is not None:
                self._blocked_instances[instance_id] = reason

    # -- lifecycle ------------------------------------------------------------
    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        from vaultkeep.trigger.preflight import runtime_preflight

        with MonitorLock(monitor_lock_path(self.config.scope)):
            self._preflight_version = runtime_preflight(
                self.config, self.controller, self.store.root
            )
            self.store.ensure()
            self._startup_reconcile()
            budget = _Budget(next_due={})
            while not self._stop:
                self.tick(budget)
                self._sleep(self.config.poll_seconds)

    def _startup_reconcile(self) -> None:
        self._adopt_unassociated_attempts()
        ids = set(self.store.list_instance_ids()) | {i.id for i in self.config.enabled_instances()}
        for instance_id in sorted(ids):
            self._guard_instance(instance_id, lambda: self._startup_instance(instance_id))

    def _startup_instance(self, instance_id: str) -> None:
        instance = self.config.instance_by_id(instance_id)
        state = self._load_state(instance_id)
        state.candidate_since = None
        state.candidate_monotonic = None
        if state.active_attempt_id is not None:
            self._annotate_monitor_gap(state)
            self._reconcile_attempt(state, instance, state.active_attempt_id)
        self._maybe_notify_overdue(state)
        self._notify_activity(state)
        if instance is None or not instance.enabled:
            if state.active_attempt_id is not None or state.pending_request is not None:
                state.latest_request_id = ""
            state.pending_request = None
            state.armed = False
        state.pending_reconfirmation_required = state.pending_request is not None
        self._update_blocked_wait(state)
        self._persist(state)
        self._cleanup(state)

    def _guard_instance(self, instance_id: str, operation: Callable[[], None]) -> None:
        if instance_id in self._blocked_instances:
            return
        try:
            operation()
        except StateError as error:
            self.store.write_blocked(instance_id, str(error))
            self._blocked_instances[instance_id] = str(error)
            _LOG.error("%s: dispatch blocked by state error: %s", instance_id, error)
            # Persistence failures still abort the monitor; uncertain state must not
            # authorize dispatch when its blocking decision cannot be made durable.

    def _annotate_monitor_gap(
        self,
        state: InstanceState,
        reason: str = "monitoring gap overlapped possible execution",
    ) -> None:
        attempt_id = state.active_attempt_id
        if attempt_id is None:
            return
        launch = self.store.read_launch(attempt_id)
        receipt = self.store.read_receipt(attempt_id)
        if receipt is None and (launch is None or launch.rejected_before_queue):
            return
        if receipt is not None:
            # A final-check refusal proves the backup child never ran. A receipt
            # already reconciled before the gap proves that child had finished.
            if receipt.outcome is AttemptOutcome.BLOCKED or state.active_outcome_pending:
                return
            observed = parse_utc(
                state.last_observation.observed_at if state.last_observation else None
            )
            started = parse_utc(receipt.started_at)
            ended = parse_utc(receipt.ended_at)
            now = parse_utc(utc_now())
            if (
                started is not None
                and ended is not None
                and observed is not None
                and now is not None
                and started <= ended <= observed <= now
            ):
                return
        self._flag_review(state, attempt_id, reason)

    # -- state ----------------------------------------------------------------
    def _load_state(self, instance_id: str) -> InstanceState:
        if instance_id in self._states:
            return self._states[instance_id]
        existing = self.store.read_instance(instance_id)
        if existing is None:
            existing = InstanceState(instance_id=instance_id)
        self._states[instance_id] = existing
        return existing

    def _persist(self, state: InstanceState) -> None:
        state.last_monitor_update = utc_now()
        state.monitor_version = installed_version()
        self.store.write_instance(state)

    @staticmethod
    def _current_boot() -> str | None:
        return boot_id()

    # -- tick -----------------------------------------------------------------
    def tick(self, budget: _Budget) -> None:
        now = self._clock()
        self._reconcile_retired_instances()
        for instance in self.config.enabled_instances():
            due = budget.next_due.get(instance.id, 0.0)
            if now < due:
                continue
            budget.next_due[instance.id] = now + instance.poll_seconds
            self._guard_instance(instance.id, lambda: self._process_instance(instance))

    def run_once(self) -> None:
        """Process every enabled instance once, ignoring poll scheduling.

        Useful for tests and for a controlled first pass after startup.
        """
        self.store.ensure()
        self._reconcile_retired_instances()
        for instance in self.config.enabled_instances():
            self._guard_instance(instance.id, lambda: self._process_instance(instance))

    def _reconcile_retired_instances(self) -> None:
        for instance_id in self.store.list_instance_ids():
            instance = self.config.instance_by_id(instance_id)
            if instance is not None and instance.enabled:
                continue
            self._guard_instance(
                instance_id, lambda: self._reconcile_retired(instance_id, instance)
            )

    def _reconcile_retired(self, instance_id: str, instance: InstanceConfig | None) -> None:
        state = self._load_state(instance_id)
        if state.active_attempt_id is not None or state.pending_request is not None:
            state.latest_request_id = ""
        state.pending_request = None
        state.armed = False
        if state.active_attempt_id is not None:
            self._annotate_monitor_gap(
                state, "instance disabled or removed while execution was unresolved"
            )
            self._reconcile_attempt(state, instance, state.active_attempt_id)
        self._maybe_notify_overdue(state)
        self._notify_activity(state)
        self._update_blocked_wait(state)
        self._persist(state)
        self._cleanup(state)

    def _process_instance(self, instance: InstanceConfig) -> None:
        state = self._load_state(instance.id)
        previous_poll = self._last_poll.get(instance.id)
        if (
            state.active_attempt_id is not None
            and previous_poll is not None
            and self._clock() - previous_poll
            > 2 * max(instance.poll_seconds, self.config.poll_seconds)
        ):
            self._annotate_monitor_gap(state, "process observation was delayed during execution")
        self._reconcile_attempt_if_active(state, instance)
        self._handle_observation(instance, state)
        self._maybe_notify_overdue(state)
        self._update_blocked_wait(state)
        self._dispatch(state, instance)
        self._notify_activity(state)
        self._persist(state)
        self._cleanup(state)

    # -- observation ----------------------------------------------------------
    def _handle_observation(self, instance: InstanceConfig, state: InstanceState) -> None:
        detection = detect(instance, self.provider)
        self._last_poll[instance.id] = self._clock()
        context = self._execution_context()
        events = progress(
            state,
            instance,
            detection,
            context,
            monotonic_now=self._clock(),
        )
        for event in events:
            self._log_event(instance.id, event)
        self._notify_application(instance, state)

        # A restart or monitoring gap during an active attempt is review_required.
        if (
            state.active_attempt_id is not None
            and not state.active_outcome_pending
            and detection.observation is not Observation.STOPPED
        ):
            self._mark_review_required(
                state,
                detection,
                attempt_id=state.active_attempt_id,
            )

    def _execution_context(self) -> ExecutionContext:
        return ExecutionContext(
            vaultkeep=str(self.config.vaultkeep),
            environment=build_child_environment(),
            preflight_version=self._preflight_version,
        )

    def _notify_application(self, instance: InstanceConfig, state: InstanceState) -> None:
        if not self.config.notifications.enabled:
            return
        application, _detail = application_status(
            state,
            self.store,
            reliable=True,
            poll_seconds=max(instance.poll_seconds, self.config.poll_seconds),
            monitor_started_at=self._started_at,
        )
        previous = self._last_application.get(instance.id)
        self._last_application[instance.id] = application
        # The first fresh observation establishes the baseline after startup.
        if previous is None or previous == application:
            return
        self.notifier.notify(
            "VaultKeep Trigger: application state changed",
            f"instance {instance.id}: Application {previous} → {application}",
        )

    @staticmethod
    def _log_event(instance_id: str, event: LifecycleEvent) -> None:
        detail = f" ({event.detail})" if event.detail else ""
        _LOG.info("%s: %s%s", instance_id, event.kind, detail)

    def _mark_review_required(
        self, state: InstanceState, detection: DetectionResult, *, attempt_id: str
    ) -> None:
        reason = (
            f"instance observation was {detection.observation.value} during attempt "
            f"{attempt_id}" + (f": {detection.reason}" if detection.reason else "")
        )
        self._flag_review(state, attempt_id, reason)

    @staticmethod
    def _flag_review(state: InstanceState, attempt_id: str, reason: str) -> None:
        # Keep attribution in the existing reason fields. An older aggregate
        # warning with no known reason must not be reassigned to a newer attempt.
        reason = f"attempt {attempt_id}: {reason}"
        if not state.review_required:
            state.review_reason = reason
        state.review_required = True
        if state.active_attempt_id == attempt_id:
            state.active_review_required = True
            state.active_review_reason = state.active_review_reason or reason
        for summary in state.attempt_summaries:
            if summary.get("attempt_id") == attempt_id:
                summary["review_required"] = True

    # -- dispatch -------------------------------------------------------------
    def _dispatch(self, state: InstanceState, instance: InstanceConfig) -> None:
        if state.instance_id in self._blocked_instances:
            return
        if state.active_attempt_id is not None:
            return
        request = state.pending_request
        if request is None:
            return
        if state.pending_reconfirmation_required:
            return
        if state.retry_not_before is not None:
            ready = parse_utc(state.retry_not_before)
            if ready is not None and _now_dt() < ready:
                return
            state.retry_not_before = None
        if not self._request_is_current(state, instance, request):
            _LOG.warning("%s: dropping stale pending request %s", instance.id, request.request_id)
            state.pending_request = None
            return
        if (
            state.last_observation is None
            or state.last_observation.observation is not Observation.STOPPED
        ):
            return
        if self._history_bound_exceeded(state, instance):
            _LOG.warning(
                "%s: dispatch blocked: attempt history bound cannot be enforced", instance.id
            )
            return

        attempt_id = new_uuid()
        frozen = replace(request, attempt_id=attempt_id)
        self.store.write_request(frozen)
        state.active_attempt_id = attempt_id
        state.active_request_id = request.request_id
        state.active_review_required = False
        state.active_review_reason = None
        state.latest_request_id = request.request_id
        state.pending_request = None
        state.pending_created_at = None
        state.retry_not_before = None
        self.store.write_instance(state)

        unit = unit_name_for(attempt_id, self.config.scope)
        launch = LaunchRecord(attempt_id=attempt_id, unit_name=unit, dispatched_at=utc_now())
        self.store.write_launch(launch)

        self._ensure_rejection_state(state, request.request_id)
        self._notify_start_once(state, instance, request.request_id)

        result = self.controller.start_unit(unit)
        self._handle_submission(state, instance, request, attempt_id, launch, result)

    def _handle_submission(
        self,
        state: InstanceState,
        instance: InstanceConfig,
        request: Request,
        attempt_id: str,
        launch: LaunchRecord,
        result: SubmissionResult,
    ) -> None:
        if result.classification is SubmissionClass.REJECTED:
            rejected = replace(
                launch,
                rejected_before_queue=True,
                rejection_code=result.exit_code,
                rejection_reason=result.stderr,
            )
            self.store.write_launch(rejected)
            self._finish_rejection(state, instance, request, attempt_id, result.stderr)
            _LOG.warning(
                "%s: start rejected before queueing (code %s); retrying later",
                instance.id,
                result.exit_code,
            )
            return
        if result.classification is SubmissionClass.INTEGRATION_ERROR:
            self._record_summary(
                state,
                attempt_id=attempt_id,
                request_id=request.request_id,
                outcome=AttemptOutcome.INTEGRATION_ERROR,
                reason=result.stderr or "invalid start arguments",
            )
            state.active_attempt_id = None
            state.active_request_id = None
            _LOG.error("%s: start integration error: %s", instance.id, result.stderr)
            return
        if result.classification is SubmissionClass.ACCEPTED:
            if state.rejection is not None:
                state.rejection.consecutive = 0
            return
        # INCONCLUSIVE: keep the active association; reconciliation resolves it.
        _LOG.warning(
            "%s: inconclusive start result (code %s); keeping attempt %s unresolved",
            instance.id,
            result.exit_code,
            attempt_id,
        )

    def _record_rejection(
        self,
        state: InstanceState,
        request: Request,
        attempt_id: str,
    ) -> None:
        rejection = self._ensure_rejection_state(state, request.request_id)
        if attempt_id in rejection.processed_attempts:
            return
        rejection.processed_attempts.append(attempt_id)
        rejection.consecutive += 1
        if (
            rejection.consecutive >= REJECTION_ESCALATION_THRESHOLD
            and not rejection.escalation_attempted
        ):
            rejection.escalation_attempted = True
            rejection.reminders_suppressed = True
            self._notify_once(
                state,
                f"{request.request_id}:rejection-escalated",
                "VaultKeep Trigger: repeated submission rejection",
                f"instance {state.instance_id}: {rejection.consecutive} consecutive rejections",
            )
        elif rejection.consecutive >= REJECTION_ESCALATION_THRESHOLD:
            rejection.reminders_suppressed = True

    def _finish_rejection(
        self,
        state: InstanceState,
        instance: InstanceConfig | None,
        request: Request,
        attempt_id: str,
        reason: str | None,
    ) -> None:
        self._record_rejection(state, request, attempt_id)
        self._record_summary(
            state,
            attempt_id=attempt_id,
            request_id=request.request_id,
            outcome=AttemptOutcome.BLOCKED,
            reason=reason or "submission rejected",
        )
        self._clear_active(state)
        self._requeue_current(state, instance, request)
        self._persist(state)

    def _requeue_current(
        self, state: InstanceState, instance: InstanceConfig | None, request: Request
    ) -> None:
        if not self._request_is_current(state, instance, request):
            return
        state.pending_request = replace(request, attempt_id="")
        state.pending_created_at = utc_now()
        state.retry_not_before = _retry_timestamp()

    def _clear_active(self, state: InstanceState) -> None:
        self._last_activity.pop(state.instance_id, None)
        state.active_attempt_id = None
        state.active_request_id = None
        state.active_outcome_pending = False
        state.active_review_required = False
        state.active_review_reason = None

    def _notify_once(
        self,
        state: InstanceState,
        marker: str,
        title: str,
        body: str,
        *,
        activity: bool = False,
        finished: bool = False,
        ended_at: str | None = None,
    ) -> None:
        if marker in state.notification_markers:
            return
        state.notification_markers.append(marker)
        self._persist(state)
        if finished:
            local_time = (parse_utc(ended_at) or _now_dt()).astimezone()
            body += f"\n{local_time:%Y-%m-%d %H:%M:%S %Z} (local time)"
        self.notifier.notify(
            title, body, key=state.instance_id if activity else None, finished=finished
        )

    def _notify_activity(self, state: InstanceState) -> None:
        """Publish observed phases, never treating optional telemetry as a result."""
        attempt_id = state.active_attempt_id
        if not self.config.notifications.enabled or attempt_id is None:
            return
        activity = "finishing"
        phase = "Finishing backup; awaiting result"
        phase_started_at = None
        phase_elapsed = 0.0
        if not state.active_outcome_pending:
            record = read_progress(self.store.progress_path(attempt_id), attempt_id)
            started = parse_utc(record.started_at) if record else None
            phase_started = parse_utc(record.phase_started_at) if record else None
            updated = parse_utc(record.updated_at) if record else None
            now = _now_dt()
            if (
                record is None
                or started is None
                or phase_started is None
                or updated is None
                or not started <= phase_started <= updated <= now
                or (now - updated).total_seconds() > STALE_SECONDS
                or (
                    record.state != "finished"
                    and self.provider.alive(record.pid, record.create_time) is not True
                )
            ):
                # Avoid inventing activity before the first progress record. If
                # previously observed progress becomes unusable, stop claiming it
                # is still running on the desktop.
                if state.instance_id not in self._last_activity:
                    return
                activity, phase = "unknown", "Backup progress unavailable; awaiting result"
            elif record.state != "finished":
                activity, phase = record.state, record.phase
                phase_started_at = record.phase_started_at
                phase_elapsed = (now - phase_started).total_seconds()
        current = (attempt_id, activity, phase, phase_started_at)
        monotonic_now = self._clock()
        previous = self._last_activity.get(state.instance_id)
        if previous is None or previous.identity != current:
            previous = _Activity(current, monotonic_now - phase_elapsed, monotonic_now)
            self._last_activity[state.instance_id] = previous
        elif monotonic_now - previous.notified_at < _ACTIVITY_UPDATE_SECONDS:
            return
        else:
            elapsed = max(0, int(monotonic_now - previous.started_at))
            phase += f" ({elapsed}s)"
        previous.notified_at = monotonic_now
        self.notifier.notify(
            f"VaultKeep Trigger: backup {activity}",
            f"instance {state.instance_id}: {phase}",
            key=state.instance_id,
        )

    @staticmethod
    def _ensure_rejection_state(state: InstanceState, request_id: str) -> RejectionState:
        rejection = state.rejection
        if rejection is None or rejection.request_id != request_id:
            rejection = RejectionState(request_id=request_id)
            state.rejection = rejection
        return rejection

    def _notify_start_once(
        self, state: InstanceState, instance: InstanceConfig, request_id: str
    ) -> None:
        marker = f"{request_id}:start-notified"
        rejection = state.rejection
        if rejection is not None and rejection.start_notified:
            return
        if marker in state.notification_markers:
            return
        if rejection is not None:
            rejection.start_notified = True
        self._notify_once(
            state,
            marker,
            "VaultKeep Trigger: backup requested",
            f"instance {instance.id}: confirmed shutdown, starting backup",
            activity=True,
        )

    # -- reconciliation -------------------------------------------------------
    def _reconcile_attempt_if_active(self, state: InstanceState, instance: InstanceConfig) -> None:
        if state.active_attempt_id is None:
            return
        self._reconcile_attempt(state, instance, state.active_attempt_id)

    @staticmethod
    def _attempt_has_summary(state: InstanceState, attempt_id: str) -> bool:
        return any(item.get("attempt_id") == attempt_id for item in state.attempt_summaries)

    def _reconcile_attempt(
        self, state: InstanceState, instance: InstanceConfig | None, attempt_id: str
    ) -> None:
        request = self.store.read_request(attempt_id)
        if request is None:
            raise StateError(f"active attempt {attempt_id} has no request")
        if request.attempt_id != attempt_id or request.instance_id != state.instance_id:
            raise StateError(f"attempt {attempt_id} has contradictory request ownership")
        receipt = self.store.read_receipt(attempt_id)
        if receipt is not None:
            if (
                receipt.attempt_id != attempt_id
                or receipt.request_id != request.request_id
                or receipt.instance_id != request.instance_id
            ):
                raise StateError(f"attempt {attempt_id} has contradictory receipt ownership")
            if state.rejection is not None and state.rejection.request_id == request.request_id:
                state.rejection.consecutive = 0
            if not self._attempt_has_summary(state, attempt_id):
                self._apply_receipt(state, receipt)
            if receipt.outcome is AttemptOutcome.BLOCKED:
                self._resolve_blocked_review(state, receipt)
            # The receipt describes the child; systemd still owns the worker/job lifetime.
            unit_state = self.controller.unit_state(unit_name_for(attempt_id, self.config.scope))
            if unit_state is None or unit_state.is_active_or_pending():
                state.active_outcome_pending = True
                return
            self._complete_receipt(state, receipt)
            self._clear_active(state)
            if receipt.outcome is AttemptOutcome.DEFERRED:
                self._requeue_current(state, instance, request)
            return
        launch = self.store.read_launch(attempt_id)
        if launch is None:
            # No start was submitted by this protocol: adopt and leave pending.
            self._record_summary(
                state,
                attempt_id=attempt_id,
                request_id=request.request_id,
                outcome=AttemptOutcome.BLOCKED,
                reason="start was not submitted",
            )
            self._clear_active(state)
            self._requeue_current(state, instance, request)
            return
        if (
            launch.unit_name != unit_name_for(attempt_id, self.config.scope)
            or launch.attempt_id != attempt_id
        ):
            raise StateError(f"attempt {attempt_id} has contradictory launch ownership")
        unit_state = self.controller.unit_state(launch.unit_name)
        if unit_state is None:
            _LOG.warning(
                "%s: manager unavailable while reconciling %s", state.instance_id, attempt_id
            )
            return
        if unit_state.is_active_or_pending():
            if state.rejection is not None and state.rejection.request_id == request.request_id:
                state.rejection.consecutive = 0
            if launch.rejected_before_queue:
                self.store.write_launch(replace(launch, rejected_before_queue=False))
            return
        if launch.rejected_before_queue:
            self._finish_rejection(state, instance, request, attempt_id, launch.rejection_reason)
            return
        # Worker absent, no receipt, and no proof of rejection: outcome unknown.
        self._record_summary(
            state,
            attempt_id=attempt_id,
            request_id=request.request_id,
            outcome=AttemptOutcome.OUTCOME_UNKNOWN,
            reason="worker is absent and no completion evidence was recorded",
            review_required=True,
        )
        self._flag_review(
            state, attempt_id, f"attempt {attempt_id} outcome is unknown; manual review required"
        )
        self._clear_active(state)
        self._notify_once(
            state,
            f"{attempt_id}:unknown-notified",
            "VaultKeep Trigger: outcome unknown",
            f"instance {state.instance_id}: attempt {attempt_id} requires review",
            activity=True,
            finished=True,
        )

    def _apply_receipt(
        self,
        state: InstanceState,
        receipt: Receipt,
    ) -> None:
        review = state.active_review_required
        self._record_summary(
            state,
            attempt_id=receipt.attempt_id,
            request_id=receipt.request_id,
            outcome=receipt.outcome,
            reason=receipt.blocked_reason or receipt.integration_error,
            review_required=review,
            exit_code=receipt.exit_code,
            signal=receipt.signal,
        )
        state.active_outcome_pending = True

    @staticmethod
    def _resolve_blocked_review(state: InstanceState, receipt: Receipt) -> None:
        """Resolve only this attempt's risk when its final check prevented execution.

        Historical review annotations and the worker receipt remain intact. Missing
        aggregate attribution is not proof that this was the only outstanding risk.
        """
        summary = next(
            item for item in state.attempt_summaries if item["attempt_id"] == receipt.attempt_id
        )
        if (
            summary["outcome"] != receipt.outcome.value
            or summary["request_id"] != receipt.request_id
        ):
            raise StateError(f"attempt {receipt.attempt_id} has contradictory receipt summary")
        reason = state.review_reason or ""
        # Older observation warnings also identify the attempt in a fixed prefix.
        legacy_prefixes = [
            f"instance observation was {observation.value} during attempt {receipt.attempt_id}"
            for observation in Observation
        ]
        attributable = reason.startswith(f"attempt {receipt.attempt_id}: ") or any(
            reason == prefix or reason.startswith(prefix + ":") for prefix in legacy_prefixes
        )
        if state.active_review_required or (state.review_required and attributable):
            summary["review_required"] = True
            _LOG.info(
                "%s: execution review resolved for attempt %s: final check blocked backup (%s)",
                state.instance_id,
                receipt.attempt_id,
                state.active_review_reason or state.review_reason,
            )
        state.active_review_required = False
        state.active_review_reason = None
        if not state.review_required or not attributable:
            return
        other_reviews = [
            summary
            for summary in state.attempt_summaries
            if summary.get("review_required")
            and summary["outcome"] != AttemptOutcome.BLOCKED.value
            and summary["attempt_id"] != receipt.attempt_id
        ]
        if other_reviews:
            oldest = other_reviews[0]
            state.review_reason = (
                f"attempt {oldest['attempt_id']}: execution safety requires review"
            )
        else:
            state.review_required = False
            state.review_reason = None

    def _complete_receipt(self, state: InstanceState, receipt: Receipt) -> None:
        """Announce completion only after systemd confirms the entire attempt ended."""
        if receipt.outcome is AttemptOutcome.DEFERRED:
            return
        review = state.active_review_required
        self._notify_terminal(state, receipt)
        if review:
            self._notify_once(
                state,
                f"{receipt.attempt_id}:review-notified",
                "VaultKeep Trigger: review required",
                f"instance {state.instance_id}: {state.active_review_reason}",
            )

    def _notify_terminal(self, state: InstanceState, receipt: Receipt) -> None:
        marker = f"{receipt.attempt_id}:terminal-notified"
        if marker in state.notification_markers:
            return
        if receipt.outcome is AttemptOutcome.CREATED:
            body = "new archive created"
        elif receipt.outcome is AttemptOutcome.UNCHANGED:
            body = "no change; no new archive"
        elif receipt.outcome is AttemptOutcome.RESULT_UNKNOWN:
            body = "completed with an unknown result"
        elif receipt.outcome is AttemptOutcome.BLOCKED:
            body = f"blocked: {receipt.blocked_reason}"
        elif receipt.outcome is AttemptOutcome.INTEGRATION_ERROR:
            body = f"integration error: {receipt.integration_error}"
        else:
            body = f"failed (exit {receipt.exit_code}, signal {receipt.signal})"
        if state.active_review_required:
            body += "; review required"
        self._notify_once(
            state,
            marker,
            "VaultKeep Trigger: backup finished",
            f"instance {state.instance_id}: {body}",
            activity=True,
            finished=True,
            ended_at=receipt.ended_at,
        )

    def _record_summary(
        self,
        state: InstanceState,
        *,
        attempt_id: str,
        request_id: str,
        outcome: AttemptOutcome,
        reason: str | None = None,
        review_required: bool = False,
        exit_code: int | None = None,
        signal: int | None = None,
    ) -> None:
        if self._attempt_has_summary(state, attempt_id):
            return
        state.attempt_summaries.append(
            {
                "attempt_id": attempt_id,
                "request_id": request_id,
                "outcome": outcome.value,
                "reason": reason,
                "review_required": review_required,
                "exit_code": exit_code,
                "signal": signal,
                "recorded_at": utc_now(),
            }
        )

    # -- blocked wait ---------------------------------------------------------
    def _update_blocked_wait(self, state: InstanceState) -> None:
        if state.pending_request is not None and state.active_attempt_id is not None:
            if (
                state.blocked_wait_started_at is None
                or state.blocking_attempt_id != state.active_attempt_id
            ):
                state.blocked_wait_started_at = utc_now()
                state.blocking_attempt_id = state.active_attempt_id
                state.wait_notification_sent = (
                    f"{state.active_attempt_id}:wait-notified" in state.notification_markers
                )
            self._maybe_notify_blocked_wait(state)
        else:
            state.blocked_wait_started_at = None
            state.blocking_attempt_id = None
            state.wait_notification_sent = False

    def _maybe_notify_blocked_wait(self, state: InstanceState) -> None:
        if state.wait_notification_sent:
            return
        if state.blocking_attempt_id is None:
            return
        request = self.store.read_request(state.blocking_attempt_id)
        if request is None or request.deadline_seconds is not None:
            return
        started = parse_utc(state.blocked_wait_started_at)
        if started is None:
            return
        if (_now_dt() - started).total_seconds() < BLOCKED_WAIT_SECONDS:
            return
        state.wait_notification_sent = True
        self._notify_once(
            state,
            f"{state.blocking_attempt_id}:wait-notified",
            "VaultKeep Trigger: backup blocked",
            f"instance {state.instance_id}: pending request waiting behind "
            f"attempt {state.blocking_attempt_id}",
        )

    # -- advisory deadlines ---------------------------------------------------
    def _maybe_notify_overdue(self, state: InstanceState) -> None:
        if state.active_attempt_id is None:
            return
        marker = f"{state.active_attempt_id}:overdue-notified"
        if marker in state.notification_markers:
            return
        request = self.store.read_request(state.active_attempt_id)
        if request is None or request.deadline_seconds is None:
            return
        launch = self.store.read_launch(state.active_attempt_id)
        if launch is None:
            return
        started = parse_utc(launch.dispatched_at)
        if started is None:
            return
        if (_now_dt() - started).total_seconds() < request.deadline_seconds:
            return
        self._notify_once(
            state,
            marker,
            "VaultKeep Trigger: backup overdue",
            f"instance {state.instance_id}: attempt {state.active_attempt_id} exceeded its "
            "advisory deadline; the worker continues",
        )

    # -- currency / recovery --------------------------------------------------
    def _request_is_current(
        self, state: InstanceState, instance: InstanceConfig | None, request: Request
    ) -> bool:
        if instance is None or not instance.enabled:
            return False
        if request.vaultkeep != str(self.config.vaultkeep):
            return False
        if state.latest_request_id is not None and state.latest_request_id != request.request_id:
            return False
        if (
            state.pending_request is not None
            and state.pending_request.request_id != request.request_id
        ):
            return False
        if request.config_fingerprint != instance.fingerprint():
            return False
        return request.boot_id == self._current_boot() or request.boot_id is None

    def _adopt_unassociated_attempts(self) -> None:
        known: set[str] = set()
        for instance_id in self.store.list_instance_ids():
            self._guard_instance(instance_id, lambda: self._collect_association(instance_id, known))
        for attempt_id in self.store.list_attempt_ids():
            if attempt_id in known:
                continue
            try:
                request = self.store.read_request(attempt_id)
            except StateError as error:
                _LOG.error(
                    "unassociated attempt %s has unreadable ownership; preserving evidence: %s",
                    attempt_id,
                    error,
                )
                continue
            if request is None:
                continue
            if not isinstance(request.instance_id, str) or not INSTANCE_ID_PATTERN.fullmatch(
                request.instance_id
            ):
                _LOG.error(
                    "unassociated attempt %s has invalid ownership; preserving evidence", attempt_id
                )
                continue
            self._guard_instance(
                request.instance_id, lambda: self._adopt_attempt(attempt_id, request)
            )

    def _collect_association(self, instance_id: str, known: set[str]) -> None:
        state = self._load_state(instance_id)
        if state.active_attempt_id is not None:
            known.add(state.active_attempt_id)

    def _adopt_attempt(self, attempt_id: str, request: Request) -> None:
        state = self._load_state(request.instance_id)
        if request.attempt_id != attempt_id:
            raise StateError(f"attempt {attempt_id} has contradictory request ownership")
        if self._attempt_has_summary(state, attempt_id):
            return
        launch = self.store.read_launch(attempt_id)
        receipt = self.store.read_receipt(attempt_id)
        matches_pending = (
            state.pending_request is not None
            and state.pending_request.request_id == request.request_id
        )
        if (
            launch is None
            and receipt is None
            and not matches_pending
            and attempt_id not in (state.active_attempt_id, state.blocking_attempt_id)
        ):
            # There is affirmative proof no worker was submitted by this protocol.
            # Never revive obsolete work merely because its request file survived.
            self._record_summary(
                state,
                attempt_id=attempt_id,
                request_id=request.request_id,
                outcome=AttemptOutcome.BLOCKED,
                reason="unreferenced request: start was never submitted",
            )
            self.store.write_instance(state)
            return
        if state.active_attempt_id is not None:
            if state.active_attempt_id != attempt_id:
                raise StateError(f"instance {state.instance_id} has multiple unreconciled attempts")
            return
        state.active_attempt_id = attempt_id
        state.active_request_id = request.request_id
        if matches_pending:
            state.pending_request = None
        self.store.write_instance(state)

    # -- retention ------------------------------------------------------------
    def history_root(self) -> Path:
        return self.store.attempts_dir

    def _attempts_by_instance(self, instance_id: str) -> list[tuple[str, Request]]:
        result: list[tuple[str, Request]] = []
        for attempt_id in self.store.list_attempt_ids():
            try:
                request = self.store.read_request(attempt_id)
            except StateError:
                continue
            if request is None or request.instance_id != instance_id:
                continue
            result.append((attempt_id, request))
        return result

    def _history_bound_exceeded(self, state: InstanceState, instance: InstanceConfig) -> bool:
        attempts = self._attempts_by_instance(instance.id)
        protected = [
            attempt_id
            for attempt_id, request in attempts
            if self._attempt_is_protected(state, attempt_id, request)
        ]
        return len(protected) > COMPLETED_ATTEMPT_RETENTION + UNCERTAIN_HISTORY_CAP

    def _attempt_is_protected(
        self, state: InstanceState, attempt_id: str, request: Request
    ) -> bool:
        return self.store.attempt_is_protected(state, attempt_id, request)

    def _cleanup(self, state: InstanceState) -> None:
        entries: list[tuple[str, dict]] = []
        attempt_ids = {
            attempt_id for attempt_id, _ in self._attempts_by_instance(state.instance_id)
        }
        for summary in state.attempt_summaries:
            if summary.get("attempt_id") in attempt_ids:
                entries.append((summary["attempt_id"], summary))
        # Never delete active/unresolved/recovery-referenced attempts.
        protected = {
            state.active_attempt_id,
            state.blocking_attempt_id,
        } - {None}

        summarized = [e for e in entries if e[0] not in protected]
        uncertain = [e for e in summarized if AttemptOutcome(e[1]["outcome"]) in UNCERTAIN_OUTCOMES]
        for attempt_id, _ in uncertain[: max(0, len(uncertain) - UNCERTAIN_HISTORY_CAP)]:
            self.store.remove_attempt(attempt_id)
        remaining = [e for e in summarized if self.store.attempt_dir(e[0]).is_dir()]
        for attempt_id, _ in remaining[: max(0, len(remaining) - COMPLETED_ATTEMPT_RETENTION)]:
            self.store.remove_attempt(attempt_id)
        self._prune_notification_markers(state)

    def _prune_notification_markers(self, state: InstanceState) -> None:
        scopes = {
            state.active_attempt_id,
            state.blocking_attempt_id,
            state.active_request_id,
            state.latest_request_id,
        }
        if state.pending_request is not None:
            scopes.add(state.pending_request.request_id)
        if state.rejection is not None:
            scopes.add(state.rejection.request_id)
        for attempt_id, request in self._attempts_by_instance(state.instance_id):
            scopes.update((attempt_id, request.request_id))
        if not all(isinstance(marker, str) for marker in state.notification_markers):
            raise StateError(f"instance {state.instance_id} has malformed notification markers")
        retained = [
            marker for marker in state.notification_markers if marker.partition(":")[0] in scopes
        ]
        if retained != state.notification_markers:
            state.notification_markers = retained
            self._persist(state)
