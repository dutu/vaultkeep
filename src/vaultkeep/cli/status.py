"""Unified job status: collection is independent of rendering and execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vaultkeep.backup.paths import WorkflowPaths, user_workflow_paths
from vaultkeep.backup.status import backup_status
from vaultkeep.cli.output import Console
from vaultkeep.config import resolve_runtime_config
from vaultkeep.config.identity import resolve_job_identity
from vaultkeep.config.loader import load_document, validate_document
from vaultkeep.config.models import JobConfig
from vaultkeep.diagnostics import concise, duration, timestamp
from vaultkeep.errors import ConfigurationError, TimerError
from vaultkeep.schedule.status import schedule_status
from vaultkeep.state.identity import job_identity_hash
from vaultkeep.trigger.config import load_config as load_trigger_config
from vaultkeep.trigger.status import recovery_status, trigger_status


def collect_status(
    config_path: Path,
    *,
    user_mode: bool,
    runtime_params: Mapping[str, str],
    verbose: bool = False,
) -> dict[str, Any]:
    document = load_document(config_path)
    try:
        result = _collect_valid_status(
            config_path,
            validate_document(document),
            user_mode=user_mode,
            runtime_params=runtime_params,
            verbose=verbose,
        )
    except ConfigurationError as error:
        return _collect_recovery_status(
            config_path,
            document,
            error,
            user_mode=user_mode,
            runtime_params=runtime_params,
            verbose=verbose,
        )
    result["configuration"] = {"status": "VALID", "errors": []}
    return result


def _collect_valid_status(
    config_path: Path,
    raw: JobConfig,
    *,
    user_mode: bool,
    runtime_params: Mapping[str, str],
    verbose: bool,
) -> dict[str, Any]:
    parameters = dict(raw.trigger.params) if raw.trigger.enabled else {}
    parameters.update(runtime_params)
    config = resolve_runtime_config(raw, parameters)
    identity = job_identity_hash(config_path, config.job.id, runtime_params=parameters)
    result: dict[str, Any] = {
        "schema_version": 1,
        "job_id": config.job.id,
        "backup": backup_status(
            user_workflow_paths() if user_mode else WorkflowPaths(),
            config.job.id,
            identity,
            verbose=verbose or config.logging.level == "debug",
        ),
    }
    if "trigger" in raw.model_fields_set:
        if user_mode:
            trigger = trigger_status(
                load_trigger_config(config_path, runtime_params=parameters), verbose=verbose
            )
            if trigger is not None:
                result["trigger"] = trigger
        elif config.trigger.enabled or config.trigger.app is not None:
            result["trigger"] = {
                "enabled": config.trigger.enabled,
                "monitor": "UNKNOWN",
                "application": "UNKNOWN",
                "dispatch": "UNKNOWN",
                "diagnostics": [
                    "Trigger supports user mode only; inspect as the application owner"
                ],
            }
    if config.schedule.enabled or config.schedule.interval is not None:
        result["schedule"] = schedule_status(
            config_path, config, user_mode=user_mode, runtime_params=parameters
        )
    return result


def _collect_recovery_status(
    config_path: Path,
    document: dict[str, Any],
    error: ConfigurationError,
    *,
    user_mode: bool,
    runtime_params: Mapping[str, str],
    verbose: bool,
) -> dict[str, Any]:
    from vaultkeep.schedule import TimerManager

    trigger = document.get("trigger")
    identity = resolve_job_identity(
        config_path,
        document,
        runtime_params=runtime_params,
        trigger_defaults=isinstance(trigger, dict) and trigger.get("enabled") is True,
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "job_id": identity.job_id,
        "configuration": {
            "status": "INVALID",
            "errors": [
                {"path": issue.dotted_path, "message": issue.message} for issue in error.issues
            ],
        },
        "backup": backup_status(
            user_workflow_paths() if user_mode else WorkflowPaths(),
            identity.job_id,
            identity.scope,
            verbose=verbose,
        ),
    }
    if user_mode:
        try:
            monitor_identity = resolve_job_identity(
                config_path, document, runtime_params=runtime_params, trigger_defaults=True
            )
            result["trigger"] = recovery_status(monitor_identity)
        except ConfigurationError as identity_error:
            result["trigger"] = {
                "enabled": None,
                "monitor": "UNKNOWN",
                "application": "UNKNOWN",
                "dispatch": "UNKNOWN",
                "diagnostics": [str(identity_error)],
                "read_error": True,
            }
    elif trigger is not None:
        result["trigger"] = {
            "enabled": None,
            "monitor": "UNKNOWN",
            "application": "UNKNOWN",
            "dispatch": "UNKNOWN",
            "diagnostics": ["Inspect Trigger as the application owner with --user"],
        }
    manager = TimerManager(user_mode=user_mode)
    instance = manager._instance_id(config_path, identity.job_id, identity.params)
    schedule: dict[str, Any] = {
        "enabled": None,
        "timer": manager._unit(instance),
        "installed": None,
        "health": "UNKNOWN",
        "next_execution": None,
        "diagnostics": [],
    }
    result["schedule"] = schedule
    try:
        # A schedule identity must retain the normal managed-path/filename rules.
        resolved = manager._managed_path(config_path)
        if resolved.stem != identity.job_id:
            raise TimerError("Timer configuration filename must match job.id")
        values = manager.inspect_instance(resolved, instance, identity.params)
        missing = values["LoadState"] == "not-found"
        schedule["enabled"] = not missing and values["UnitFileState"] in (
            "enabled",
            "enabled-runtime",
        )
        schedule["timer_enabled"] = schedule["enabled"]
        schedule["installed"] = not missing
        schedule["health"] = (
            "NOT INSTALLED"
            if missing
            else "FAILED"
            if values["ActiveState"] == "failed"
            else "RUNNING"
            if values["ActiveState"] == "active"
            else "STOPPED"
            if values["ActiveState"] == "inactive"
            else "UNKNOWN"
        )
        next_time = values.get("NextElapseUSecRealtime")
        if next_time and next_time not in ("n/a", "infinity", "0"):
            schedule["next_execution"] = next_time
    except (TimerError, OSError) as inspection_error:
        schedule["diagnostics"].append(str(inspection_error))
        schedule["read_error"] = True
    return result


def print_status(data: dict[str, Any], output: Console, *, verbose: bool = False) -> None:
    output.heading("Job status")
    output.fields([("Job", data["job_id"])])
    configuration = data.get("configuration")
    if configuration:
        output.fields(
            [("Status", configuration["status"])]
            + [
                ("Problem", concise(f"{issue['path']}: {issue['message']}"))
                for issue in configuration["errors"]
            ],
            section="Configuration",
        )
    backup = data["backup"]
    activity = output.status(backup["activity"])
    current = backup["current"]
    if current:
        phase_age, total_age = current["phase_elapsed_seconds"], current["elapsed_seconds"]
        if phase_age is not None and total_age is not None:
            activity += (
                f": {current['phase']} ({duration(phase_age)} / {duration(total_age)} elapsed)"
            )
    rows = [("Activity", activity)]
    rows.extend(("Detail", concise(item)) for item in backup["diagnostics"])
    output.fields(rows, section="Backup")
    previous = backup["previous"]
    rows = [("Result", output.status(previous["result"]) if previous else "NONE")]
    if previous:
        if previous.get("completed_at"):
            rows.append(("Completed", timestamp(previous["completed_at"])))
        if previous.get("duration_seconds") is not None:
            rows.append(("Duration", duration(previous["duration_seconds"])))
        if previous.get("exit_code") not in (None, 0):
            rows.append(("Exit code", str(previous["exit_code"])))
        if previous.get("signal") is not None:
            rows.append(("Signal", str(previous["signal"])))
        if previous.get("reason"):
            rows.append(("Reason", concise(previous["reason"])))
        if previous.get("log_path") and previous["result"] not in ("SUCCESS", "UNCHANGED"):
            rows.append(("Log", previous["log_path"]))
    output.fields(rows, section="Previous backup")
    rows = []
    if backup.get("details"):
        rows.append(("Execution record", backup["details"]["execution_record"]))
    rows.extend(("Tail", line) for line in backup.get("diagnostic_tail", []))
    if rows:
        output.fields(rows, section="Backup diagnostics")
    if "trigger" in data:
        trigger = data["trigger"]
        monitor = output.status(trigger["monitor"])
        age = trigger.get("monitor_updated_seconds_ago")
        monitor += f" (updated {duration(age)} ago)" if age is not None else " (updated unknown)"
        rows = [
            ("Status", _enablement(trigger["enabled"])),
            ("Monitor", monitor),
            (
                "Application",
                output.status(trigger["application"]) + trigger.get("application_detail", ""),
            ),
            ("Dispatch", trigger["dispatch"]),
        ]
        if trigger.get("last_exit") is not None:
            rows.append(("Last exit", str(trigger["last_exit"])))
        if trigger.get("pending_request_id"):
            rows.append(
                (
                    "Pending request",
                    "Waiting for previous attempt"
                    if trigger.get("active_attempt_id")
                    else "Waiting for dispatch",
                )
            )
        if trigger.get("worker"):
            rows.append(("Worker", trigger["worker"]))
        historical = [item for item in trigger.get("safety", []) if not item["current"]]
        if historical:
            count = len(historical)
            description = (
                f"{count} previous {'attempt' if count == 1 else 'attempts'}"
                if all(item.get("attempt_id") for item in historical)
                else "previous executions"
            )
            rows.append(("Safety", f"REVIEW REQUIRED ({description})"))
        for safety in trigger.get("safety", []):
            if safety["current"] or verbose:
                rows.append(("Safety", safety["reason"]))
                if safety.get("attempt_id"):
                    rows.append(("Attempt", safety["attempt_id"]))
                if safety.get("execution_period"):
                    rows.append(("Period", safety["execution_period"]))
        if trigger.get("safety"):
            rows.append(
                ("Action", trigger["safety_guidance"] if verbose else trigger["safety_action"])
            )
        rows.extend(("Detail", concise(item)) for item in trigger["diagnostics"])
        if trigger.get("log_path"):
            rows.append(("Log", trigger["log_path"]))
        output.fields(rows, section="Trigger")
        if trigger.get("details"):
            output.fields(
                [
                    (key.replace("_", " ").capitalize(), str(value))
                    for key, value in trigger["details"].items()
                ],
                section="Trigger diagnostics",
            )
    if "schedule" in data:
        schedule = data["schedule"]
        rows = [
            ("Status", _enablement(schedule["enabled"])),
            ("Timer", schedule["health"]),
        ]
        if schedule["installed"] is not None:
            rows.append(("Installed", "yes" if schedule["installed"] else "no"))
        if schedule.get("timer_enabled") is not None:
            rows.append(("Timer enabled", "yes" if schedule["timer_enabled"] else "no"))
        if schedule["next_execution"]:
            rows.append(("Next execution", schedule["next_execution"]))
        rows.extend(("Detail", concise(item)) for item in schedule["diagnostics"])
        output.fields(rows, section="Schedule")


def _enablement(enabled: bool | None) -> str:
    return "UNKNOWN" if enabled is None else "ENABLED" if enabled else "DISABLED"


def status_command(
    config_path: Path,
    *,
    user_mode: bool,
    runtime_params: Mapping[str, str],
    json_output: bool,
    output: Console,
    verbose: bool = False,
) -> int:
    data = collect_status(
        config_path, user_mode=user_mode, runtime_params=runtime_params, verbose=verbose
    )
    if json_output:
        print(json.dumps(data, sort_keys=True))
    else:
        print_status(data, output, verbose=verbose)
    return (
        15
        if any(
            data.get(section, {}).get("read_error") for section in ("backup", "trigger", "schedule")
        )
        else 3
        if data.get("configuration", {}).get("status") == "INVALID"
        else 0
    )
