"""Read-only Trigger monitoring, dispatch, and recovery diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vaultkeep.config.identity import JobIdentity
from vaultkeep.diagnostics import concise, elapsed_seconds
from vaultkeep.locking import job_lock_owner
from vaultkeep.trigger import service
from vaultkeep.trigger.activity import application_status
from vaultkeep.trigger.config import TriggerConfig
from vaultkeep.trigger.errors import BlockedError, StateError, TriggerError
from vaultkeep.trigger.models import Receipt, parse_utc
from vaultkeep.trigger.monitor import unit_name_for
from vaultkeep.trigger.paths import monitor_lock_path, state_root
from vaultkeep.trigger.service import find_service, path_scope, service_name
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import RealSystemdController, UnitState


def recovery_status(identity: JobIdentity | Path) -> dict[str, Any]:
    """Inspect an existing monitor without trusting execution settings in the YAML."""
    result: dict[str, Any] = {
        "enabled": None,
        "monitor": "UNKNOWN",
        "application": "UNKNOWN",
        "dispatch": "UNKNOWN",
        "diagnostics": [],
        "details": {"monitor_service": service_name(identity)},
    }
    try:
        installed = find_service(identity if isinstance(identity, Path) else identity.source_path)
        if installed is None:
            result.update(enabled=False, monitor="NOT INSTALLED")
            if isinstance(identity, Path):
                return result
        else:
            identity = installed.identity
            values = installed.values
            result["details"].update(
                installed_service=installed.unit,
                installed_execution_identity=f"{identity.job_id}-{identity.scope}",
                installed_params=identity.params,
                state_directory=str(state_root(identity.scope)),
            )
            if installed.legacy:
                result["diagnostics"].append(
                    "Legacy Trigger service; run trigger update to migrate"
                )
            result["enabled"] = values["UnitFileState"] in ("enabled", "enabled-runtime")
            result["monitor"] = (
                "RESTARTING"
                if values["SubState"] == "auto-restart"
                else "FAILED"
                if values["ActiveState"] == "failed"
                else "RUNNING"
                if values["ActiveState"] == "active"
                else "STARTING"
                if values["ActiveState"] == "activating"
                else "STOPPED"
                if values["ActiveState"] == "inactive"
                else "UNKNOWN"
            )
            exit_status = values.get("ExecMainStatus", "")
            if exit_status.isdecimal():
                result["last_exit"] = int(exit_status)
        assert isinstance(identity, JobIdentity)
        store = StateStore(state_root(identity.scope))
        blocked = store.read_blocked(identity.job_id)
        state = store.read_instance(identity.job_id)
        if result["monitor"] == "RUNNING":
            # Recovery inspection cannot validate freshness against invalid YAML's
            # poll interval. ActiveState alone is not a monitor health claim.
            result["monitor"] = "UNKNOWN"
            result["diagnostics"].append(
                "Monitor process is active; validate YAML and monitor freshness "
                "before trusting health"
            )
        activity, attempt = store.dispatch_activity(identity.job_id, state, blocked)
        result["dispatch"] = "BLOCKED" if blocked else activity.upper()
        result["active_attempt_id"] = attempt
        if blocked:
            result["diagnostics"].append(blocked)
    except (TriggerError, OSError) as error:
        result["diagnostics"].append(str(error))
        result["read_error"] = True
    return result


def _monitor_status(scope: str | None) -> str:
    """Observe the lifetime lock without acquiring it or changing its timestamps."""
    try:
        owner = job_lock_owner(monitor_lock_path(scope))
    except BlockedError:
        return "unknown"
    return "unknown" if owner is None else "running" if owner else "stopped"


def _monitor_started_at(scope: str | None) -> str | None:
    """The held lifetime lock is rewritten once when a monitoring session starts."""
    try:
        written = monitor_lock_path(scope).stat().st_mtime
        return datetime.fromtimestamp(written, UTC).isoformat()
    except (BlockedError, OSError, ValueError, OverflowError):
        return None


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


def _validate_attempt_id(attempt_id: str) -> None:
    if not attempt_id or Path(attempt_id).name != attempt_id or attempt_id in (".", ".."):
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


def trigger_status(config: TriggerConfig, *, verbose: bool = False) -> dict[str, Any] | None:
    """Return configured monitoring or retained recovery evidence; omit unused placeholders."""
    configured = bool(config.enabled_instances())
    diagnostics: list[str] = []
    result: dict[str, Any] = {
        "enabled": bool(config.enabled_instances()),
        "monitor": "UNKNOWN",
        "monitor_updated_seconds_ago": None,
        "application": "UNKNOWN",
        "application_detail": "",
        "dispatch": "UNKNOWN",
        "pending_request_id": None,
        "active_attempt_id": None,
        "diagnostics": diagnostics,
    }
    blocked = None
    try:
        # An unused disabled Trigger must not make manual status depend on a
        # running user manager. Local units still reveal changed installations.
        installed = (
            find_service(config.source_path)
            if configured or any(service.units_root().glob("vaultkeep-trigger@*.service"))
            else None
        )
        configured = configured or installed is not None
        if installed and (installed.legacy or installed.identity.scope != config.scope):
            result = recovery_status(config.source_path)
            result["details"]["desired_execution_identity"] = f"{config.job_id}-{config.scope}"
            result["details"]["desired_params"] = config.params
            result["monitor"] = "UPDATE REQUIRED"
            result["diagnostics"].append(
                "Installed execution identity differs or uses legacy naming; run trigger update"
            )
            return result
        store = StateStore(state_root(config.scope))
        if verbose or config.log_level == "DEBUG":
            result["details"] = {
                "monitor_service": service_name(config),
                "state_directory": str(store.root),
            }
            if installed:
                result["details"].update(
                    installed_execution_identity=(
                        f"{installed.identity.job_id}-{installed.identity.scope}"
                    ),
                    installed_params=installed.identity.params,
                )
        blocked = store.read_blocked(config.job_id)
        if blocked:
            diagnostics.extend(
                [concise(blocked), "Preserve records and follow recovery instructions"]
            )
        state = store.read_instance(config.job_id)
        runtime = _monitor_status(path_scope(config.source_path)).upper()
        activity, attempt_id = store.dispatch_activity(config.job_id, state, blocked)
        configured = (
            configured
            or state is not None
            or blocked is not None
            or attempt_id is not None
            or runtime == "RUNNING"
        )
        if not configured:
            return None
        monitor = runtime
        if runtime == "STOPPED":
            unit = RealSystemdController().unit_state(service_name(config))
            if unit:
                configured = configured or unit.load_state != "not-found"
                monitor = (
                    "RESTARTING"
                    if unit.sub_state == "auto-restart"
                    else "FAILED"
                    if unit.active_state == "failed"
                    else "NOT INSTALLED"
                    if unit.load_state == "not-found"
                    else "STARTING"
                    if unit.active_state == "activating"
                    else "STOPPED"
                )
        started_at = (
            _monitor_started_at(path_scope(config.source_path)) if runtime == "RUNNING" else None
        )
        age = elapsed_seconds(state.last_monitor_update) if state else None
        result["monitor_updated_seconds_ago"] = age
        if runtime == "RUNNING":
            started = parse_utc(started_at)
            updated = parse_utc(state.last_monitor_update) if state else None
            if age is None or age > 2 * config.poll_seconds:
                monitor = "UNKNOWN"
                diagnostics.append("Monitor updates stale or unavailable; check monitor journal")
            elif started is None or updated is None or updated < started:
                monitor = "UNKNOWN"
                diagnostics.append("Awaiting current-session monitor update; check monitor journal")
        if monitor in ("STOPPED", "FAILED", "NOT INSTALLED") and config.enabled_instances():
            diagnostics.append(
                "Check monitor service and journal; install after validation if missing"
            )
        result["monitor"] = monitor
        result["dispatch"] = activity.upper()
        result["active_attempt_id"] = attempt_id
        instance = config.instance_by_id(config.job_id)
        application, recency = application_status(
            state,
            store,
            reliable=(
                instance is not None
                and instance.enabled
                and monitor == "RUNNING"
                and blocked is None
                and state is not None
                and state.config_fingerprint == instance.fingerprint()
            ),
            poll_seconds=config.poll_seconds,
            monitor_started_at=started_at,
        )
        result["application"], result["application_detail"] = application, recency
        if (
            instance
            and instance.enabled
            and state
            and state.config_fingerprint != instance.fingerprint()
        ):
            diagnostics.append("Configuration changed; validate and update monitor")
        if state is None:
            diagnostics.append("No monitoring state recorded")
        if state and state.pending_request:
            result["pending_request_id"] = state.pending_request.request_id
            if state.rejection and (
                state.rejection.request_id == state.pending_request.request_id
                and state.rejection.consecutive
            ):
                diagnostics.append("Submission rejected; check worker installation and permissions")
        if attempt_id:
            receipt = _receipt(store, attempt_id, config.job_id)
            request = store.read_request(attempt_id)
            if (
                request is None
                or request.instance_id != config.job_id
                or request.attempt_id != attempt_id
                or (receipt and receipt.request_id != request.request_id)
                or (
                    state is not None
                    and state.active_attempt_id == attempt_id
                    and state.active_request_id is not None
                    and state.active_request_id != request.request_id
                )
            ):
                raise StateError(f"attempt {attempt_id} has contradictory request ownership")
            if receipt:
                result["dispatch"] = (
                    "BLOCKED" if receipt.outcome.value == "blocked" else "AWAITING RECONCILIATION"
                )
                if receipt.blocked_reason:
                    diagnostics.append(concise(receipt.blocked_reason))
            else:
                worker = RealSystemdController().unit_state(unit_name_for(attempt_id, config.scope))
                result["worker"] = _worker_status(worker)
                diagnostics.append(
                    "Dispatch outcome awaits reconciliation; check monitor and worker journal"
                )
            launch = store.read_launch(attempt_id)
            if launch:
                if launch.attempt_id != attempt_id:
                    raise StateError(f"attempt {attempt_id} has contradictory launch ownership")
                result["dispatched_seconds_ago"] = elapsed_seconds(launch.dispatched_at)
        if blocked:
            result["dispatch"] = "BLOCKED"
        if state:
            # Summaries remain orchestration evidence. Only initiation problems and
            # unresolved safety annotations belong here; backup results live in Backup.
            for summary in state.attempt_summaries:
                receipt = _receipt(
                    store, summary["attempt_id"], config.job_id, summary["request_id"]
                )
                if receipt and receipt.outcome.value != summary["outcome"]:
                    raise StateError("contradictory completion outcomes")
            if state.attempt_summaries:
                last = state.attempt_summaries[-1]
                if last["outcome"] in (
                    "blocked",
                    "deferred",
                    "integration_error",
                    "outcome_unknown",
                ):
                    diagnostics.append(
                        {
                            "blocked": "Previous dispatch BLOCKED; backup was not started",
                            "deferred": "Previous dispatch DEFERRED; job lock was held",
                            "integration_error": (
                                "Previous dispatch EXECUTION ERROR; check worker diagnostics"
                            ),
                            "outcome_unknown": (
                                "Previous dispatch outcome UNKNOWN; do not replay the attempt"
                            ),
                        }[last["outcome"]]
                    )
                    if last.get("reason"):
                        diagnostics.append(concise(last["reason"]))
                    log = store.log_path(last["attempt_id"])
                    if log.is_file():
                        result["log_path"] = str(log)
            for summary in state.attempt_summaries:
                if summary["outcome"] in ("outcome_unknown", "result_unknown"):
                    diagnostics.append(
                        "Worker outcome UNKNOWN; preserve dispatch records "
                        "and do not replay the attempt"
                    )
            if verbose or config.log_level == "DEBUG":
                result["details"].update(
                    {
                        "instance": config.job_id,
                        "monitor_session": started_at,
                        "armed": state.armed,
                        "observation": state.last_observation.observation.value
                        if state.last_observation
                        else None,
                        "observation_reason": state.last_observation.reason
                        if state.last_observation
                        else None,
                    }
                )
    except (TriggerError, OSError, UnicodeError) as error:
        configured = True
        diagnostics.append(str(error))
        result["dispatch"] = "BLOCKED" if blocked else "UNKNOWN"
        result["application"] = "UNKNOWN"
        result["read_error"] = True
    return result if configured else None
