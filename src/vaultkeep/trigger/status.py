"""Read-only human reports for trigger activity and retained execution evidence."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from vaultkeep.cli.output import Console
from vaultkeep.trigger.errors import StateError
from vaultkeep.trigger.models import InstanceState, Observation, Receipt, parse_utc, utc_now
from vaultkeep.trigger.monitor import unit_name_for
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import RealSystemdController, UnitState
from vaultkeep.trigger.version import DIAGNOSTICS_CAP_BYTES


def _timestamp(value: str | None) -> str:
    parsed = parse_utc(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC") if parsed else "unknown"


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


def _receipt(
    store: StateStore, attempt_id: str, instance_id: str, request_id: str | None = None
) -> Receipt | None:
    if Path(attempt_id).name != attempt_id or attempt_id in (".", ".."):
        raise StateError(f"invalid last attempt ID: {attempt_id!r}")
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
    # Only extract a path from the finalizer's explicit delimiter; never guess it
    # from current configuration, a receipt-supplied log path, or arbitrary output.
    finalization = re.search(
        r"Cannot atomically finalize backup (.+?): \[Errno \d+\] ([^\n]+)", diagnostics
    )
    if finalization:
        path, error = finalization.groups()
        if "Permission denied" in error:
            reason, action = (
                "Permission denied during finalization",
                "Check destination permissions",
            )
        else:
            reason, action = (
                "Backup finalization failed",
                "Check destination access and diagnostics",
            )
        return [("Reason", reason), ("Backup", path), ("Action", action)]
    failures = [line.strip() for line in diagnostics.splitlines() if line.startswith("FAILED:")]
    if failures:
        reason = "".join(c if c.isprintable() else " " for c in failures[-1][7:].strip())
        if len(reason) <= 160:
            return [
                ("Reason", reason),
                ("Action", "Inspect diagnostics and correct the reported error"),
            ]
    return [
        ("Reason", "Not recorded or unavailable"),
        ("Action", "Inspect the attempt diagnostics and worker journal"),
    ]


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
    result = {
        "created": "SUCCESS",
        "unchanged": "UNCHANGED",
        "result_unknown": "SUCCESS (backup result unknown)",
        "backup_failed": "FAILED",
        "blocked": "BLOCKED",
        "deferred": "DEFERRED",
        "integration_error": "EXECUTION ERROR",
        "outcome_unknown": "UNKNOWN",
    }[outcome]
    result = output.status(result)
    if (
        state.review_required
        and summary.get("review_required")
        and outcome in ("created", "unchanged")
    ):
        result += " (source consistency uncertain)"
    if summary.get("exit_code") is not None and outcome not in (
        "created",
        "unchanged",
        "result_unknown",
    ):
        result += f" (exit code {summary['exit_code']})"
    if summary.get("signal") is not None:
        result += f" (signal {summary['signal']})"
    rows = [("Result", result)]
    if receipt:
        rows.append(("Completed", _timestamp(receipt.ended_at)))
    elif summary.get("recorded_at"):
        # recorded_at is reconciliation time, not backup completion time.
        rows.append(("Recorded", _timestamp(summary["recorded_at"])))
    if outcome == "result_unknown":
        rows.append(("Detail", "Command succeeded; archive creation was not confirmed"))
    elif outcome not in ("created", "unchanged"):
        rows.append(("Attempt", attempt_id))
        if summary.get("reason"):
            reason = summary["reason"]
            reason = "".join(c if c.isprintable() else " " for c in reason)
            rows.append(("Reason", reason if len(reason) <= 160 else "See diagnostic details"))
            rows.append(
                (
                    "Action",
                    "Check process matching and monitor logs"
                    if outcome == "blocked"
                    else "Inspect the attempt diagnostics and worker journal",
                )
            )
        elif outcome == "backup_failed":
            rows.extend(_failure_details(store, attempt_id))
        else:
            rows.extend(
                [
                    ("Reason", "Not recorded or unavailable"),
                    ("Action", "Inspect the attempt diagnostics and worker journal"),
                ]
            )
        if outcome == "outcome_unknown":
            rows.append(
                ("Impact", "Backup completion could not be verified; do not replay this attempt")
            )
    if any(label == "Backup" for label, _ in rows):
        rows = [(label, value) for label, value in rows if label != "Attempt"]
    section = (
        "Last backup"
        if outcome in ("created", "unchanged", "result_unknown", "backup_failed")
        else "Last completed attempt"
    )
    output.fields(rows, section=section)
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
) -> None:
    worker = None
    backup = activity.upper()
    unit = None
    if active_attempt_id is not None and activity == "active":
        unit = unit_name_for(active_attempt_id, scope)
        worker = RealSystemdController().unit_state(unit)
        backup = _worker_status(worker)
    rows = [("Backup", backup)]
    if state is None:
        rows.append(("State", "No monitoring state recorded"))
    elif enabled:
        rows.append(("Trigger armed", "YES" if state.armed else "NO"))
    if active_attempt_id and activity == "active":
        receipt = _receipt(store, active_attempt_id, instance_id)
        if receipt:
            rows.append(
                (
                    "Child completed",
                    f"{_timestamp(receipt.ended_at)}; worker awaiting reconciliation",
                )
            )
        if backup in ("UNKNOWN", "STOPPED", "FAILED"):
            rows.extend(
                [
                    ("Attempt", active_attempt_id),
                    (
                        "Detail",
                        "Awaiting reconciliation; worker state does not establish backup outcome",
                    ),
                    ("Action", "Check monitor health and worker journal"),
                ]
            )
    if state and state.pending_request:
        rows.append(
            (
                "Pending backup",
                "Waiting for previous attempt" if active_attempt_id else "Waiting for dispatch",
            )
        )
    output.fields(rows, section="Current activity")
    health: list[tuple[str, str]] = []
    if enabled and monitor != "RUNNING":
        health.extend(
            [
                ("Problem", "Monitoring is not confirmed running"),
                ("Impact", "Application shutdowns may not trigger backups"),
                (
                    "Action",
                    "Install the monitor after validation"
                    if monitor == "NOT INSTALLED"
                    else "Check monitor service and journal; validate the job before restarting",
                ),
            ]
        )
    elif enabled:
        updated = parse_utc(state.last_monitor_update) if state else None
        now = parse_utc(utc_now())
        age = (now - updated).total_seconds() if now and updated else None
        if age is None or age < 0 or age > 2 * poll_seconds:
            health.extend(
                [
                    ("Problem", "Monitor updates are stale or unavailable"),
                    ("Impact", "Lock ownership does not confirm responsive observation"),
                    ("Action", "Check the monitor journal and validate the job"),
                ]
            )
    if blocked_reason:
        health.extend(
            [
                ("Problem", "Backup dispatch blocked by recovery state"),
                ("Reason", blocked_reason),
                (
                    "Action",
                    "Preserve state and follow recovery instructions before restarting the monitor",
                ),
            ]
        )
    if state:
        observation = state.last_observation
        if (
            enabled
            and observation
            and observation.observation
            in (
                Observation.AMBIGUOUS,
                Observation.INSPECTION_UNAVAILABLE,
            )
        ):
            health.extend(
                [
                    (
                        "Last observation",
                        "Application identity is ambiguous"
                        if observation.observation is Observation.AMBIGUOUS
                        else "Application inspection unavailable",
                    ),
                    ("Impact", "Automatic backup dispatch waits for reliable process observation"),
                    ("Action", "Check process matching and inspection permissions"),
                ]
            )
        if (
            enabled
            and state.pending_request
            and state.rejection
            and state.rejection.request_id == state.pending_request.request_id
            and state.rejection.consecutive
        ):
            health.extend(
                [
                    ("Submission rejections", str(state.rejection.consecutive)),
                    ("Action", "Check worker installation and systemd user permissions"),
                ]
            )
    output.fields(health, section="Monitoring health")
    if state:
        completed = [
            item for item in state.attempt_summaries if item["attempt_id"] != active_attempt_id
        ]
        last = completed[-1] if completed else None
        if last:
            _last_attempt(output, store, state, last, verbose=verbose)
        _outstanding(output, store, state, last["attempt_id"] if last else None)
    if verbose:
        details = [("Instance", instance_id)]
        if active_attempt_id:
            details.append(("Active attempt", active_attempt_id))
        if unit:
            details.append(("Worker service", unit))
        if worker:
            details.append(
                ("Worker state", f"{worker.load_state}/{worker.active_state}/{worker.sub_state}")
            )
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
    if summary.get("exit_code") is not None:
        rows.append(("Backup exit code", str(summary["exit_code"])))
    if summary.get("signal") is not None:
        rows.append(("Backup signal", str(summary["signal"])))
    if summary.get("reason"):
        rows.append(("Attempt reason", summary["reason"]))
    # Read only this attempt's fixed log path, never a path supplied by a receipt.
    if Path(attempt_id).name != attempt_id or attempt_id in (".", ".."):
        raise StateError(f"invalid last attempt ID: {attempt_id!r}")
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
