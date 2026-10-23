"""Unified job status: collection is independent of rendering and execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vaultkeep.backup.paths import WorkflowPaths, user_workflow_paths
from vaultkeep.backup.status import backup_status
from vaultkeep.cli.output import Console
from vaultkeep.config import load_config, resolve_runtime_config
from vaultkeep.diagnostics import concise, duration, timestamp
from vaultkeep.schedule.status import schedule_status
from vaultkeep.state.identity import job_identity_hash
from vaultkeep.trigger.config import load_config as load_trigger_config
from vaultkeep.trigger.status import trigger_status


def collect_status(
    config_path: Path,
    *,
    user_mode: bool,
    runtime_params: Mapping[str, str],
    verbose: bool = False,
) -> dict[str, Any]:
    raw = load_config(config_path)
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


def print_status(data: dict[str, Any], output: Console, *, verbose: bool = False) -> None:
    output.heading("Job status")
    output.fields([("Job", data["job_id"])])
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
            ("Status", "ENABLED" if trigger["enabled"] else "DISABLED"),
            ("Monitor", monitor),
            (
                "Application",
                output.status(trigger["application"]) + trigger.get("application_detail", ""),
            ),
            ("Dispatch", trigger["dispatch"]),
        ]
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
            ("Status", "ENABLED" if schedule["enabled"] else "DISABLED"),
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
        else 0
    )
