"""Read-only human reports for trigger activity and retained execution evidence."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from vaultkeep.cli.output import Console
from vaultkeep.progress import STALE_SECONDS, read_progress
from vaultkeep.trigger.errors import StateError
from vaultkeep.trigger.models import InstanceState, Observation, Receipt, parse_utc, utc_now
from vaultkeep.trigger.monitor import unit_name_for
from vaultkeep.trigger.process import PsutilProcessProvider
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import RealSystemdController, UnitState
from vaultkeep.trigger.version import DIAGNOSTICS_CAP_BYTES


def _timestamp(value: str | None, *, seconds: bool = True) -> str:
    parsed = parse_utc(value)
    pattern = "%Y-%m-%d %H:%M:%S UTC" if seconds else "%Y-%m-%d %H:%M UTC"
    return parsed.strftime(pattern) if parsed else "unknown"


def elapsed_seconds(timestamp: str | None) -> float | None:
    parsed, now = parse_utc(timestamp), parse_utc(utc_now())
    if parsed is None or now is None:
        return None
    seconds = (now - parsed).total_seconds()
    return seconds if seconds >= 0 else None


def duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _concise(reason: str) -> str:
    reason = "".join(c if c.isprintable() else " " for c in reason).strip()
    return reason if len(reason) <= 160 else "See diagnostic details"


def _log_rows(store: StateStore, attempt_id: str) -> list[tuple[str, str]]:
    path = store.log_path(attempt_id)
    try:
        return [("Log", str(path))] if path.is_file() else []
    except OSError:
        return []


def _application(
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
                _validate_attempt_id(attempt_id)
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
        return "UNKNOWN", " (current session shutdown evidence unavailable)"
    if (
        state.latest_request_id is None
        and not state.attempt_summaries
        and not state.active_attempt_id
        and not state.pending_request
    ):
        return "NOT YET DETECTED", ""
    return "UNKNOWN", ""


def _worker_status(worker: UnitState | None) -> str:
    """Describe service lifetime without inferring the backup child outcome."""
    if worker is None:
        return "UNKNOWN"
    if worker.load_state == "loaded":
        if worker.active_state == "activating":
            return "STARTING"
        if (worker.active_state, worker.sub_state) == ("active", "running"):
            return "RUNNING"
        if worker.active_state == "deactivating":
            return "STOPPING"
    if worker.job_pending:
        return "QUEUED"
    if worker.active_state == "failed":
        return "FAILED"
    if not worker.is_active_or_pending():
        return "STOPPED"
    return "UNKNOWN"


def _backup_progress(store: StateStore, attempt_id: str) -> tuple[str, str] | None:
    record = read_progress(store.progress_path(attempt_id), attempt_id)
    if record is None:
        return None
    started = parse_utc(record.started_at)
    phase_started = parse_utc(record.phase_started_at)
    updated = parse_utc(record.updated_at)
    now = parse_utc(utc_now())
    if (
        started is None
        or phase_started is None
        or updated is None
        or now is None
        or not started <= phase_started <= updated <= now
        or (now - updated).total_seconds() > STALE_SECONDS
    ):
        return "UNKNOWN", " (backup progress stale or invalid; check worker journal)"
    if record.state == "finished":
        return "FINISHING", " (awaiting backup result)"
    if PsutilProcessProvider.alive(record.pid, record.create_time) is not True:
        return "UNKNOWN", " (backup process unavailable; awaiting result)"
    phase_age = duration((now - phase_started).total_seconds())
    total_age = duration((now - started).total_seconds())
    status = "RUNNING" if record.state == "running" else "PREPARING"
    return status, f": {record.phase} ({phase_age} / {total_age} elapsed)"


def _validate_attempt_id(attempt_id: str) -> None:
    if Path(attempt_id).name != attempt_id or attempt_id in (".", ".."):
        raise StateError(f"invalid last attempt ID: {attempt_id!r}")


def _receipt(
    store: StateStore, attempt_id: str, instance_id: str, request_id: str | None = None
) -> Receipt | None:
    _validate_attempt_id(attempt_id)
    receipt = store.read_receipt(attempt_id)
    if receipt and (
        receipt.attempt_id != attempt_id
        or receipt.instance_id != instance_id
        or (request_id is not None and receipt.request_id != request_id)
    ):
        raise StateError(f"attempt {attempt_id} has contradictory receipt ownership")
    return receipt


def _failure_details(store: StateStore, attempt_id: str) -> list[tuple[str, str]]:
    try:
        with store.log_path(attempt_id).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - DIAGNOSTICS_CAP_BYTES))
            data = handle.read(DIAGNOSTICS_CAP_BYTES)
    except OSError:
        data = b""
    diagnostics = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", data.decode("utf-8", "replace"))
    diagnostics = "".join(
        c if c.isprintable() or c == "\n" else f"\\x{ord(c):02x}" for c in diagnostics
    )
    # Recognize the finalizer's explicit error without exposing its destination
    # path or internal exception representation in the concise reason.
    finalization = re.search(
        r"Cannot atomically finalize backup (.+?): \[Errno \d+\] ([^\n]+)", diagnostics
    )
    if finalization:
        _, error = finalization.groups()
        reason = (
            "Permission denied during finalization"
            if "Permission denied" in error
            else "Backup finalization failed"
        )
        return [("Reason", reason)]
    failures = [line.strip() for line in diagnostics.splitlines() if line.startswith("FAILED:")]
    if failures:
        return [("Reason", _concise(failures[-1][7:].strip()))]
    return [("Reason", "Not recorded or unavailable")]


def _result(outcome: str) -> str:
    return {
        "created": "SUCCESS",
        "unchanged": "UNCHANGED",
        "result_unknown": "SUCCESS (backup result unknown)",
        "backup_failed": "FAILED",
        "blocked": "BLOCKED",
        "deferred": "DEFERRED",
        "integration_error": "EXECUTION ERROR",
        "outcome_unknown": "UNKNOWN",
    }[outcome]


def _last_attempt(
    output: Console,
    store: StateStore,
    state: InstanceState,
    summary: dict[str, Any],
    *,
    verbose: bool,
) -> None:
    attempt_id = summary["attempt_id"]
    receipt = _receipt(store, attempt_id, state.instance_id, summary["request_id"])
    outcome = summary["outcome"]
    if receipt and receipt.outcome.value != outcome:
        raise StateError(f"attempt {attempt_id} has contradictory completion outcomes")
    result = _result(outcome)
    result = output.status(result)
    if summary.get("exit_code") is not None and outcome not in (
        "created",
        "unchanged",
        "result_unknown",
    ):
        result += f" (exit code {summary['exit_code']})"
    if summary.get("signal") is not None:
        result += f" (signal {summary['signal']})"
    rows = [("Result", result)]
    if receipt and parse_utc(receipt.ended_at) is not None:
        rows.append(("Completed", _timestamp(receipt.ended_at, seconds=False)))
    if outcome == "result_unknown":
        rows.append(("Detail", "Command succeeded; archive creation was not confirmed"))
    elif outcome not in ("created", "unchanged"):
        if summary.get("reason"):
            rows.append(("Reason", _concise(summary["reason"])))
        elif outcome == "backup_failed":
            rows.extend(_failure_details(store, attempt_id))
        else:
            rows.append(("Reason", "Not recorded or unavailable"))
        rows.extend(_log_rows(store, attempt_id))
        if outcome == "outcome_unknown":
            rows.append(
                ("Impact", "Backup completion could not be verified; do not replay this attempt")
            )
    output.fields(rows, section="Previous attempt")
    if verbose:
        output.fields(
            _diagnostic_rows(store, summary),
            section="Attempt diagnostics",
        )


def _review_problem(reason: str | None) -> str:
    if reason:
        if "monitoring gap" in reason:
            return "Monitoring was interrupted during possible backup execution"
        if "observation was delayed" in reason:
            return "Application observation was delayed during backup execution"
        if "observation was running" in reason:
            return "Application restarted during backup execution"
    return "Application stopped state could not be verified during backup execution"


def _outstanding(
    output: Console, store: StateStore, state: InstanceState, last_id: str | None
) -> None:
    if not state.review_required:
        return
    # A later success cannot establish the safety of an earlier archive.
    # BLOCKED annotations remain historical: that attempt did not invoke backup.
    unresolved = [
        summary
        for summary in state.attempt_summaries
        if summary.get("review_required") and summary["outcome"] != "blocked"
    ]
    identified = False
    for index, summary in enumerate(unresolved):
        attempt_id = summary["attempt_id"]
        if summary["outcome"] == "outcome_unknown" and attempt_id == last_id:
            identified = True  # Explained with the last result already.
            continue
        receipt = _receipt(store, attempt_id, state.instance_id, summary["request_id"])
        problem = (
            "Backup outcome could not be established"
            if summary["outcome"] == "outcome_unknown"
            else _review_problem(state.review_reason if index == 0 else None)
        )
        rows = [("Problem", problem), ("Attempt", attempt_id)]
        if receipt:
            rows.append(
                (
                    "Execution period",
                    f"{_timestamp(receipt.started_at)} — {_timestamp(receipt.ended_at)}",
                )
            )
        else:
            rows.append(("Period", "Exact execution interval unavailable"))
        rows.extend(
            [
                (
                    "Impact",
                    "Execution and backup completion are unknown"
                    if summary["outcome"] == "outcome_unknown"
                    else "Source consistency is uncertain; archive corruption is not established",
                ),
                (
                    "Action",
                    "Review backup and app activity; obtain a new backup with the app stopped",
                ),
            ]
        )
        output.fields(rows, section="Outstanding issue")
        identified = True
    if state.active_review_required and state.active_attempt_id not in {
        item["attempt_id"] for item in unresolved
    }:
        output.fields(
            [
                ("Problem", _review_problem(state.active_review_reason)),
                ("Attempt", state.active_attempt_id or "unknown"),
                ("Impact", "Source consistency may be affected"),
                (
                    "Action",
                    "Check application activity and review the backup when this attempt ends",
                ),
            ],
            section="Outstanding issue",
        )
        identified = True
    if not identified:
        output.fields(
            [
                ("Problem", "Earlier backup execution requires review"),
                ("Affected attempt", "Not identified in retained state"),
                ("Detail", "Unresolved execution-safety annotation; exact cause unavailable"),
                ("Impact", "Backup safety could not be verified"),
                ("Action", "Review retained attempt records and backup consistency"),
            ],
            section="Outstanding issue",
        )


def print_instance(
    output: Console,
    state: InstanceState | None,
    blocked_reason: str | None,
    *,
    instance_id: str,
    activity: str,
    active_attempt_id: str | None,
    scope: str | None,
    store: StateStore,
    verbose: bool,
    monitor: str,
    enabled: bool,
    poll_seconds: float,
    monitor_started_at: str | None = None,
    config_fingerprint: str | None = None,
) -> None:
    worker = None
    backup = activity.upper()
    unit = None
    receipt = None
    launch = None
    elapsed = ""
    if active_attempt_id is not None and activity == "active":
        unit = unit_name_for(active_attempt_id, scope)
        receipt = _receipt(store, active_attempt_id, instance_id)
        live_progress = _backup_progress(store, active_attempt_id) if receipt is None else None
        if receipt:
            backup = "COMPLETED"
            elapsed = " (worker awaiting reconciliation)"
        elif live_progress is not None:
            backup, elapsed = live_progress
        # Live progress identifies the backup process directly. Slow/unavailable
        # systemd probes must not hide a fresh phase report in the normal view.
        if verbose or (receipt is None and live_progress is None):
            worker = RealSystemdController().unit_state(unit)
        if receipt is None and live_progress is None:
            backup = _worker_status(worker)
            if backup in ("STARTING", "RUNNING"):
                backup = "UNKNOWN"
                elapsed = " (backup progress unavailable)"
        if verbose or (receipt is None and live_progress is None):
            launch = store.read_launch(active_attempt_id)
            if launch and launch.attempt_id != active_attempt_id:
                raise StateError(f"attempt {active_attempt_id} has contradictory launch ownership")
            age = elapsed_seconds(launch.dispatched_at) if launch else None
            if receipt is None and live_progress is None and age is not None:
                elapsed += f" ({duration(age)} since dispatch)"

    application, recency = _application(
        state,
        store,
        reliable=(
            enabled
            and monitor == "RUNNING"
            and not blocked_reason
            and (
                config_fingerprint is None
                or (state is not None and state.config_fingerprint == config_fingerprint)
            )
        ),
        poll_seconds=poll_seconds,
        monitor_started_at=monitor_started_at,
    )
    if enabled and state and config_fingerprint and state.config_fingerprint != config_fingerprint:
        recency = " (configuration changed; validate and update monitor)"
    rows = [("Application", output.status(application) + recency)]
    if blocked_reason and activity != "active":
        backup = "BLOCKED"
    rows.append(("Backup", output.status(backup) + elapsed))
    if state is None:
        rows.append(("State", "No monitoring state recorded"))
    if active_attempt_id and activity == "active":
        if receipt:
            rows.append(("Result", output.status(_result(receipt.outcome.value))))
        elif backup in ("UNKNOWN", "STOPPED", "FAILED"):
            rows.extend(
                [
                    ("Attempt", active_attempt_id),
                    ("Detail", "Outcome awaits reconciliation; check monitor and worker journal"),
                ]
            )
        if state and state.active_attempt_id == active_attempt_id and state.active_review_required:
            rows.append(("Safety", _review_problem(state.active_review_reason)))
            rows.append(("Action", "Check application activity and monitor journal"))
            rows.extend(_log_rows(store, active_attempt_id))
    if blocked_reason:
        rows.append(("Dispatch", "BLOCKED (recovery state; preserve records)"))
        rows.append(("Reason", _concise(blocked_reason)))
        rows.append(("Action", "Follow recovery instructions before restarting the monitor"))
    if state and state.pending_request:
        if active_attempt_id:
            rows.append(("Pending backup", "Waiting for previous attempt"))
        if (
            state.rejection
            and state.rejection.request_id == state.pending_request.request_id
            and state.rejection.consecutive
        ):
            rows.append(
                ("Dispatch", "Submission rejected; check worker installation and permissions")
            )
    output.fields(rows, section="Current activity")
    completed = (
        [item for item in state.attempt_summaries if item["attempt_id"] != active_attempt_id]
        if state
        else []
    )
    last = completed[-1] if completed else None
    if last and state:
        _last_attempt(output, store, state, last, verbose=verbose)
    else:
        output.fields([("Result", "NONE")], section="Previous attempt")
    if verbose:
        if state:
            _outstanding(output, store, state, last["attempt_id"] if last else None)
        details = [("Instance", instance_id)]
        if active_attempt_id:
            details.append(("Active attempt", active_attempt_id))
        if unit:
            details.append(("Worker service", unit))
        if worker:
            details.append(
                ("Worker state", f"{worker.load_state}/{worker.active_state}/{worker.sub_state}")
            )
        if launch:
            details.append(("Dispatched", _timestamp(launch.dispatched_at)))
        if receipt:
            details.append(("Child started", _timestamp(receipt.started_at)))
            details.append(("Child completed", _timestamp(receipt.ended_at)))
        if monitor_started_at:
            details.append(("Monitor session", _timestamp(monitor_started_at)))
        if state and state.last_observation:
            details.append(("Observation", state.last_observation.observation.value))
            if state.last_observation.reason:
                details.append(("Observation reason", state.last_observation.reason))
        if state:
            details.append(("Review reason", state.review_reason or "none"))
            details.append(("Stored armed", "yes" if state.armed else "no"))
            details.append(("Configuration fingerprint", state.config_fingerprint or "unknown"))
            if state.pending_request:
                details.append(("Pending request", state.pending_request.request_id))
            if state.blocked_wait_started_at:
                details.append(("Waiting since", _timestamp(state.blocked_wait_started_at)))
        output.fields(details, section="Monitor diagnostics")


def _diagnostic_rows(store: StateStore, summary: dict[str, Any]) -> list[tuple[str, str]]:
    outcome = summary["outcome"]
    rows = [("Last attempt outcome", str(outcome))]
    if summary.get("review_required"):
        rows.append(
            (
                "Review annotation",
                "recorded (execution was blocked)" if outcome == "blocked" else "recorded",
            )
        )
    attempt_id = summary["attempt_id"]
    rows.append(("Last attempt", attempt_id))
    if summary.get("recorded_at"):
        rows.append(("Recorded", _timestamp(summary["recorded_at"])))
    if summary.get("exit_code") is not None:
        rows.append(("Backup exit code", str(summary["exit_code"])))
    if summary.get("signal") is not None:
        rows.append(("Backup signal", str(summary["signal"])))
    if summary.get("reason"):
        rows.append(("Attempt reason", summary["reason"]))
    # Read only this attempt's fixed log path, never a path supplied by a receipt.
    _validate_attempt_id(attempt_id)
    if outcome in ("created", "unchanged", "result_unknown"):
        return rows
    path = store.log_path(attempt_id)
    rows.append(("Diagnostics path", str(path)))
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - DIAGNOSTICS_CAP_BYTES))
            diagnostics = handle.read(DIAGNOSTICS_CAP_BYTES)
    except FileNotFoundError:
        rows.append(("Diagnostics", "unavailable (not recorded or no longer retained)"))
        return rows
    except OSError as error:
        rows.append(("Diagnostics", f"unavailable ({error})"))
        return rows
    lines = [line for line in diagnostics.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        rows.append(("Diagnostics", "no output recorded"))
        return rows
    for index, line in enumerate(lines[-10:]):
        # Keep child output from injecting terminal control sequences or unbounded rows.
        line = "".join(character if character.isprintable() else " " for character in line)
        if len(line) > 500:
            line = line[:497] + "..."
        rows.append(("Diagnostic tail" if index == 0 else "", line))
    return rows
