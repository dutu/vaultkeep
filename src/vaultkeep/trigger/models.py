"""Typed models for observations, requests, attempts, receipts, and instance state."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from vaultkeep.trigger.version import (
    INSTANCE_STATE_SCHEMA_VERSION,
    LAUNCH_SCHEMA_VERSION,
    RECEIPT_SCHEMA_VERSION,
    REQUEST_SCHEMA_VERSION,
)


def utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string with a trailing Z."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO 8601 UTC timestamp produced by :func:`utc_now`."""
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def new_uuid() -> str:
    """Return a fresh UUIDv4 string."""
    return str(uuid.uuid4())


def _kind(value: object) -> str:
    return type(value).__name__


def _required_str(data: dict[str, Any], key: str, *, path: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{path}.{key} must be a string, got {_kind(value)}")
    return value


def _optional_str(data: dict[str, Any], key: str, *, path: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path}.{key} must be a string or null, got {_kind(value)}")
    return value


def _required_bool(data: dict[str, Any], key: str, *, path: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{path}.{key} must be a boolean, got {_kind(value)}")
    return value


def _optional_bool(data: dict[str, Any], key: str, *, path: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{path}.{key} must be a boolean, got {_kind(value)}")
    return value


def _required_int(data: dict[str, Any], key: str, *, path: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path}.{key} must be an integer, got {_kind(value)}")
    return value


def _optional_int(data: dict[str, Any], key: str, *, path: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path}.{key} must be an integer or null, got {_kind(value)}")
    return value


def _required_number(data: dict[str, Any], key: str, *, path: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path}.{key} must be a number, got {_kind(value)}")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"{path}.{key} must be a finite number") from error
    if not math.isfinite(number):
        raise ValueError(f"{path}.{key} must be a finite number")
    return number


def _optional_number(data: dict[str, Any], key: str, *, path: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    return _required_number(data, key, path=path)


def _required_dict(data: dict[str, Any], key: str, *, path: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{path}.{key} must be a table, got {_kind(value)}")
    return value


def _optional_dict(data: dict[str, Any], key: str, *, path: str) -> dict[str, Any] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{path}.{key} must be a table or null, got {_kind(value)}")
    return value


def _string_map(data: dict[str, Any], key: str, *, path: str) -> dict[str, str]:
    value = _optional_dict(data, key, path=path)
    if value is None:
        return {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str) or not isinstance(item_value, str):
            raise ValueError(f"{path}.{key} must map strings to strings")
    return dict(value)


def _string_list(data: dict[str, Any], key: str, *, path: str) -> list[str]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{path}.{key} must be an array of strings")
    return list(value)


class Observation(str, Enum):
    """One sanitized poll result."""

    RUNNING = "running"
    STOPPED = "stopped"
    AMBIGUOUS = "ambiguous"
    INSPECTION_UNAVAILABLE = "inspection_unavailable"


class AttemptOutcome(str, Enum):
    """Recorded outcome vocabulary for an attempt or receipt."""

    CREATED = "created"
    UNCHANGED = "unchanged"
    RESULT_UNKNOWN = "result_unknown"
    DEFERRED = "deferred"
    BLOCKED = "blocked"
    BACKUP_FAILED = "backup_failed"
    INTEGRATION_ERROR = "integration_error"
    OUTCOME_UNKNOWN = "outcome_unknown"


# Outcomes that must never be replayed for the same request.
TERMINAL_OUTCOMES = frozenset(
    {
        AttemptOutcome.CREATED,
        AttemptOutcome.UNCHANGED,
        AttemptOutcome.RESULT_UNKNOWN,
        AttemptOutcome.BACKUP_FAILED,
        AttemptOutcome.INTEGRATION_ERROR,
        AttemptOutcome.OUTCOME_UNKNOWN,
    }
)

# Successful-execution vocabulary for command exits.
SUCCESS_OUTCOMES = frozenset(
    {AttemptOutcome.CREATED, AttemptOutcome.UNCHANGED, AttemptOutcome.RESULT_UNKNOWN}
)

# Outcomes that carry uncertainty and count toward the uncertain-history cap.
UNCERTAIN_OUTCOMES = frozenset(
    {
        AttemptOutcome.RESULT_UNKNOWN,
        AttemptOutcome.OUTCOME_UNKNOWN,
        AttemptOutcome.BLOCKED,
    }
)


def _attempt_summaries(data: dict[str, Any], *, path: str) -> list[dict[str, Any]]:
    value = data.get("attempt_summaries")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{path}.attempt_summaries must be an array, got {_kind(value)}")
    known_outcomes = {item.value for item in AttemptOutcome}
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        entry_path = f"{path}.attempt_summaries[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{entry_path} must be a table, got {_kind(item)}")
        _required_str(item, "attempt_id", path=entry_path)
        _required_str(item, "request_id", path=entry_path)
        outcome = _required_str(item, "outcome", path=entry_path)
        if outcome not in known_outcomes:
            raise ValueError(f"{entry_path}.outcome is not a known attempt outcome")
        _optional_str(item, "reason", path=entry_path)
        _optional_bool(item, "review_required", path=entry_path, default=False)
        _optional_int(item, "exit_code", path=entry_path)
        _optional_int(item, "signal", path=entry_path)
        _optional_str(item, "recorded_at", path=entry_path)
        result.append(dict(item))
    return result


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """One sanitized poll result."""

    observation: Observation
    observed_at: str
    boot_id: str | None = None
    pid: int | None = None
    create_time: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation": self.observation.value,
            "observed_at": self.observed_at,
            "boot_id": self.boot_id,
            "pid": self.pid,
            "create_time": self.create_time,
            "evidence": self.evidence,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ObservationRecord:
        path = "observation"
        return cls(
            observation=Observation(_required_str(data, "observation", path=path)),
            observed_at=_required_str(data, "observed_at", path=path),
            boot_id=_optional_str(data, "boot_id", path=path),
            pid=_optional_int(data, "pid", path=path),
            create_time=_optional_number(data, "create_time", path=path),
            evidence=_optional_dict(data, "evidence", path=path) or {},
            reason=_optional_str(data, "reason", path=path),
        )


@dataclass(frozen=True, slots=True)
class Request:
    """An immutable shutdown request with a frozen execution snapshot."""

    request_id: str
    attempt_id: str
    instance_id: str
    created_at: str
    confirmed_at: str
    boot_id: str | None
    config_fingerprint: str
    app: str
    data_dir: str
    matcher: dict[str, Any]
    job_config: str
    job_params: dict[str, str]
    vaultkeep: str
    environment: dict[str, str]
    preflight_version: str | None
    deadline_seconds: float | None
    schema_version: int = REQUEST_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "instance_id": self.instance_id,
            "created_at": self.created_at,
            "confirmed_at": self.confirmed_at,
            "boot_id": self.boot_id,
            "config_fingerprint": self.config_fingerprint,
            "app": self.app,
            "data_dir": self.data_dir,
            "matcher": self.matcher,
            "job_config": self.job_config,
            "job_params": dict(sorted(self.job_params.items())),
            "vaultkeep": self.vaultkeep,
            "environment": dict(sorted(self.environment.items())),
            "preflight_version": self.preflight_version,
            "deadline_seconds": self.deadline_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Request:
        path = "request"
        return cls(
            request_id=_required_str(data, "request_id", path=path),
            attempt_id=_required_str(data, "attempt_id", path=path),
            instance_id=_required_str(data, "instance_id", path=path),
            created_at=_required_str(data, "created_at", path=path),
            confirmed_at=_required_str(data, "confirmed_at", path=path),
            boot_id=_optional_str(data, "boot_id", path=path),
            config_fingerprint=_required_str(data, "config_fingerprint", path=path),
            app=_required_str(data, "app", path=path),
            data_dir=_required_str(data, "data_dir", path=path),
            matcher=_required_dict(data, "matcher", path=path),
            job_config=_required_str(data, "job_config", path=path),
            job_params=_string_map(data, "job_params", path=path),
            vaultkeep=_required_str(data, "vaultkeep", path=path),
            environment=_string_map(data, "environment", path=path),
            preflight_version=_optional_str(data, "preflight_version", path=path),
            deadline_seconds=_optional_number(data, "deadline_seconds", path=path),
            schema_version=(
                _required_int(data, "schema_version", path=path)
                if "schema_version" in data
                else REQUEST_SCHEMA_VERSION
            ),
        )


@dataclass(frozen=True, slots=True)
class LaunchRecord:
    """Evidence that a worker start may have been submitted."""

    attempt_id: str
    unit_name: str
    dispatched_at: str
    rejected_before_queue: bool = False
    rejection_code: int | None = None
    rejection_reason: str | None = None
    schema_version: int = LAUNCH_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "unit_name": self.unit_name,
            "dispatched_at": self.dispatched_at,
            "rejected_before_queue": self.rejected_before_queue,
            "rejection_code": self.rejection_code,
            "rejection_reason": self.rejection_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LaunchRecord:
        path = "launch"
        return cls(
            attempt_id=_required_str(data, "attempt_id", path=path),
            unit_name=_required_str(data, "unit_name", path=path),
            dispatched_at=_required_str(data, "dispatched_at", path=path),
            rejected_before_queue=_optional_bool(
                data, "rejected_before_queue", path=path, default=False
            ),
            rejection_code=_optional_int(data, "rejection_code", path=path),
            rejection_reason=_optional_str(data, "rejection_reason", path=path),
            schema_version=(
                _required_int(data, "schema_version", path=path)
                if "schema_version" in data
                else LAUNCH_SCHEMA_VERSION
            ),
        )


@dataclass(frozen=True, slots=True)
class Receipt:
    """A worker's immutable persisted outcome."""

    attempt_id: str
    request_id: str
    instance_id: str
    outcome: AttemptOutcome
    started_at: str
    ended_at: str
    duration_seconds: float
    exit_code: int | None = None
    signal: int | None = None
    vaultkeep_version: str | None = None
    result: str | None = None
    marker_line: str | None = None
    diagnostics_ref: str | None = None
    diagnostics_truncated: bool = False
    blocked_reason: str | None = None
    integration_error: str | None = None
    schema_version: int = RECEIPT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "instance_id": self.instance_id,
            "outcome": self.outcome.value,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "vaultkeep_version": self.vaultkeep_version,
            "result": self.result,
            "marker_line": self.marker_line,
            "diagnostics_ref": self.diagnostics_ref,
            "diagnostics_truncated": self.diagnostics_truncated,
            "blocked_reason": self.blocked_reason,
            "integration_error": self.integration_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Receipt:
        path = "receipt"
        return cls(
            attempt_id=_required_str(data, "attempt_id", path=path),
            request_id=_required_str(data, "request_id", path=path),
            instance_id=_required_str(data, "instance_id", path=path),
            outcome=AttemptOutcome(_required_str(data, "outcome", path=path)),
            started_at=_required_str(data, "started_at", path=path),
            ended_at=_required_str(data, "ended_at", path=path),
            duration_seconds=_required_number(data, "duration_seconds", path=path),
            exit_code=_optional_int(data, "exit_code", path=path),
            signal=_optional_int(data, "signal", path=path),
            vaultkeep_version=_optional_str(data, "vaultkeep_version", path=path),
            result=_optional_str(data, "result", path=path),
            marker_line=_optional_str(data, "marker_line", path=path),
            diagnostics_ref=_optional_str(data, "diagnostics_ref", path=path),
            diagnostics_truncated=_optional_bool(
                data, "diagnostics_truncated", path=path, default=False
            ),
            blocked_reason=_optional_str(data, "blocked_reason", path=path),
            integration_error=_optional_str(data, "integration_error", path=path),
            schema_version=(
                _required_int(data, "schema_version", path=path)
                if "schema_version" in data
                else RECEIPT_SCHEMA_VERSION
            ),
        )


@dataclass(slots=True)
class RejectionState:
    """Per-logical-request submission-rejection bookkeeping."""

    request_id: str
    consecutive: int = 0
    processed_attempts: list[str] = field(default_factory=list)
    start_notified: bool = False
    escalation_attempted: bool = False
    reminders_suppressed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "consecutive": self.consecutive,
            "processed_attempts": list(self.processed_attempts),
            "start_notified": self.start_notified,
            "escalation_attempted": self.escalation_attempted,
            "reminders_suppressed": self.reminders_suppressed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RejectionState:
        path = "rejection"
        return cls(
            request_id=_required_str(data, "request_id", path=path),
            consecutive=(
                _required_int(data, "consecutive", path=path) if "consecutive" in data else 0
            ),
            processed_attempts=_string_list(data, "processed_attempts", path=path),
            start_notified=_optional_bool(data, "start_notified", path=path, default=False),
            escalation_attempted=_optional_bool(
                data, "escalation_attempted", path=path, default=False
            ),
            reminders_suppressed=_optional_bool(
                data, "reminders_suppressed", path=path, default=False
            ),
        )


@dataclass(slots=True)
class InstanceState:
    """Monitor-owned durable state for one instance."""

    instance_id: str
    schema_version: int = INSTANCE_STATE_SCHEMA_VERSION
    config_fingerprint: str | None = None
    boot_id: str | None = None
    armed: bool = False
    candidate_since: str | None = None
    last_observation: ObservationRecord | None = None
    pending_request: Request | None = None
    latest_request_id: str | None = None
    # Process-local clocks must never be restored from persisted UTC evidence.
    candidate_monotonic: float | None = field(default=None, repr=False, compare=False)
    pending_reconfirmation_required: bool = field(default=False, repr=False, compare=False)
    pending_created_at: str | None = None
    retry_not_before: str | None = None
    superseded_drop_count: int = 0
    active_attempt_id: str | None = None
    active_request_id: str | None = None
    active_outcome_pending: bool = False
    active_review_required: bool = False
    active_review_reason: str | None = None
    blocked_wait_started_at: str | None = None
    blocking_attempt_id: str | None = None
    wait_notification_sent: bool = False
    review_required: bool = False
    review_reason: str | None = None
    rejection: RejectionState | None = None
    notification_markers: list[str] = field(default_factory=list)
    attempt_summaries: list[dict[str, Any]] = field(default_factory=list)
    last_monitor_update: str | None = None
    monitor_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "instance_id": self.instance_id,
            "config_fingerprint": self.config_fingerprint,
            "boot_id": self.boot_id,
            "armed": self.armed,
            "candidate_since": self.candidate_since,
            "last_observation": (
                self.last_observation.to_dict() if self.last_observation else None
            ),
            "pending_request": (self.pending_request.to_dict() if self.pending_request else None),
            "pending_created_at": self.pending_created_at,
            "latest_request_id": self.latest_request_id,
            "retry_not_before": self.retry_not_before,
            "superseded_drop_count": self.superseded_drop_count,
            "active_attempt_id": self.active_attempt_id,
            "active_request_id": self.active_request_id,
            "active_outcome_pending": self.active_outcome_pending,
            "active_review_required": self.active_review_required,
            "active_review_reason": self.active_review_reason,
            "blocked_wait_started_at": self.blocked_wait_started_at,
            "blocking_attempt_id": self.blocking_attempt_id,
            "wait_notification_sent": self.wait_notification_sent,
            "review_required": self.review_required,
            "review_reason": self.review_reason,
            "rejection": self.rejection.to_dict() if self.rejection else None,
            "notification_markers": list(self.notification_markers),
            "attempt_summaries": [dict(item) for item in self.attempt_summaries],
            "last_monitor_update": self.last_monitor_update,
            "monitor_version": self.monitor_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstanceState:
        path = "instance"
        active_attempt_id = _optional_str(data, "active_attempt_id", path=path)
        observation_data = _optional_dict(data, "last_observation", path=path)
        pending_data = _optional_dict(data, "pending_request", path=path)
        rejection_data = _optional_dict(data, "rejection", path=path)

        active_review_default = False
        if active_attempt_id is not None and "active_review_required" not in data:
            active_review_default = data.get("review_required", False)
            if not isinstance(active_review_default, bool):
                raise ValueError(
                    f"{path}.review_required must be a boolean, got {_kind(active_review_default)}"
                )

        return cls(
            instance_id=_required_str(data, "instance_id", path=path),
            schema_version=(
                _required_int(data, "schema_version", path=path)
                if "schema_version" in data
                else INSTANCE_STATE_SCHEMA_VERSION
            ),
            config_fingerprint=_optional_str(data, "config_fingerprint", path=path),
            boot_id=_optional_str(data, "boot_id", path=path),
            armed=_optional_bool(data, "armed", path=path, default=False),
            candidate_since=_optional_str(data, "candidate_since", path=path),
            last_observation=(
                ObservationRecord.from_dict(observation_data)
                if observation_data is not None
                else None
            ),
            pending_request=(Request.from_dict(pending_data) if pending_data is not None else None),
            pending_created_at=_optional_str(data, "pending_created_at", path=path),
            latest_request_id=_optional_str(data, "latest_request_id", path=path),
            retry_not_before=_optional_str(data, "retry_not_before", path=path),
            superseded_drop_count=(
                _required_int(data, "superseded_drop_count", path=path)
                if "superseded_drop_count" in data
                else 0
            ),
            active_attempt_id=active_attempt_id,
            active_request_id=_optional_str(data, "active_request_id", path=path),
            active_outcome_pending=_optional_bool(
                data, "active_outcome_pending", path=path, default=False
            ),
            active_review_required=_optional_bool(
                data,
                "active_review_required",
                path=path,
                default=active_review_default,
            ),
            active_review_reason=_optional_str(data, "active_review_reason", path=path),
            blocked_wait_started_at=_optional_str(data, "blocked_wait_started_at", path=path),
            blocking_attempt_id=_optional_str(data, "blocking_attempt_id", path=path),
            wait_notification_sent=_optional_bool(
                data, "wait_notification_sent", path=path, default=False
            ),
            review_required=_optional_bool(data, "review_required", path=path, default=False),
            review_reason=_optional_str(data, "review_reason", path=path),
            rejection=(
                RejectionState.from_dict(rejection_data) if rejection_data is not None else None
            ),
            notification_markers=_string_list(data, "notification_markers", path=path),
            attempt_summaries=_attempt_summaries(data, path=path),
            last_monitor_update=_optional_str(data, "last_monitor_update", path=path),
            monitor_version=_optional_str(data, "monitor_version", path=path),
            pending_reconfirmation_required=pending_data is not None,
        )
