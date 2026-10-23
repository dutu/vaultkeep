"""Shared descriptions of observed application activity and elapsed time."""

from __future__ import annotations

from pathlib import Path

from vaultkeep.diagnostics import duration, elapsed_seconds
from vaultkeep.trigger.errors import StateError
from vaultkeep.trigger.models import InstanceState, Observation, parse_utc
from vaultkeep.trigger.state import StateStore


def application_status(
    state: InstanceState | None,
    store: StateStore,
    *,
    reliable: bool,
    poll_seconds: float,
    monitor_started_at: str | None,
) -> tuple[str, str]:
    """Use observations and confirmed requests, never arming alone as shutdown proof.

    Arming and request history survive restarts. The existing lifetime lock's write
    time bounds the current session; without it session-specific claims are unsafe.
    """
    if not reliable or state is None or state.last_observation is None:
        return "UNKNOWN", ""
    observation = state.last_observation
    observed = parse_utc(observation.observed_at)
    started = parse_utc(monitor_started_at)
    age = elapsed_seconds(observation.observed_at)
    if age is None or age > 2 * poll_seconds:
        return "UNKNOWN", " (observations stale or unavailable; check monitor journal)"
    if started is None or observed is None or observed < started:
        return "UNKNOWN", " (current session observation unavailable)"
    if observation.observation is Observation.RUNNING:
        return "RUNNING", f" (observed {duration(age)} ago)"
    if observation.observation is Observation.AMBIGUOUS:
        return "UNKNOWN", " (identity ambiguous; check process matching)"
    if observation.observation is Observation.INSPECTION_UNAVAILABLE:
        return "UNKNOWN", " (inspection unavailable; check monitor journal)"
    candidate_since = parse_utc(state.candidate_since)
    if candidate_since is not None and started <= candidate_since <= observed:
        return "CONFIRMING STOP", " (shutdown not yet confirmed)"
    if state.armed or state.candidate_since:
        return "UNKNOWN", " (shutdown not yet confirmed)"

    # A request is created only after a running -> confirmed-stopped transition.
    # latest_request_id is invalidated when the application is seen running again.
    if state.latest_request_id:
        request = state.pending_request
        if request is None or request.request_id != state.latest_request_id:
            attempt_ids = [state.active_attempt_id] if state.active_attempt_id else []
            attempt_ids.extend(
                item["attempt_id"]
                for item in reversed(state.attempt_summaries)
                if item["request_id"] == state.latest_request_id
            )
            request = None
            for attempt_id in attempt_ids:
                if Path(attempt_id).name != attempt_id or attempt_id in (".", ".."):
                    raise StateError(f"invalid last attempt ID: {attempt_id!r}")
                candidate = store.read_request(attempt_id)
                if candidate and (
                    candidate.attempt_id != attempt_id or candidate.instance_id != state.instance_id
                ):
                    raise StateError(f"attempt {attempt_id} has contradictory request ownership")
                if candidate and candidate.request_id == state.latest_request_id:
                    request = candidate
                    break
        if (
            request
            and request.instance_id == state.instance_id
            and request.config_fingerprint == state.config_fingerprint
        ):
            confirmed = parse_utc(request.confirmed_at)
            stopped_age = elapsed_seconds(request.confirmed_at)
            if confirmed and started <= confirmed <= observed and stopped_age is not None:
                return "HAS STOPPED", f" ({duration(stopped_age)} ago)"
        return "UNKNOWN", " (current session observation unavailable)"
    if (
        state.latest_request_id is None
        and not state.attempt_summaries
        and not state.active_attempt_id
        and not state.pending_request
    ):
        return "NOT YET DETECTED", ""
    return "UNKNOWN", ""
