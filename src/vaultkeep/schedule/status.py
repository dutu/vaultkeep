"""Read-only, bounded systemd timer inspection."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vaultkeep.config import JobConfig
from vaultkeep.errors import TimerError
from vaultkeep.schedule import TimerManager


def unit_properties(unit: str, *, user_mode: bool) -> dict[str, str]:
    command = ["systemctl"]
    if user_mode:
        command.append("--user")
    command.extend(
        [
            "show",
            "--all",
            "--no-pager",
            "--property=LoadState,ActiveState,SubState,UnitFileState,NextElapseUSecRealtime,"
            "Result,ExecMainStatus,FragmentPath,DropInPaths,NeedDaemonReload",
            unit,
        ]
    )
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise TimerError(f"cannot inspect {unit}: {error}") from error
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not values.get("LoadState") or (result.returncode and values["LoadState"] != "not-found"):
        raise TimerError(f"cannot inspect {unit}: {result.stderr.strip() or 'incomplete response'}")
    if values["LoadState"] != "not-found" and any(
        not values.get(key) for key in ("ActiveState", "SubState", "UnitFileState")
    ):
        raise TimerError(f"cannot inspect {unit}: incomplete response")
    return values


def schedule_status(
    config_path: Path,
    config: JobConfig,
    *,
    user_mode: bool,
    runtime_params: Mapping[str, str],
) -> dict[str, Any]:
    manager = TimerManager(user_mode=user_mode)
    instance = manager._instance_id(config_path, config.job.id, runtime_params)
    unit = manager._unit(instance)
    result: dict[str, Any] = {
        "enabled": config.schedule.enabled,
        "timer": unit,
        "installed": None,
        "health": "UNKNOWN",
        "next_execution": None,
        "diagnostics": [],
    }
    try:
        result["installed"] = manager._timer_drop_in_path(instance).is_file()
        values = unit_properties(unit, user_mode=user_mode)
        if values["LoadState"] == "not-found":
            result["installed"] = False
        result["health"] = (
            "NOT INSTALLED"
            if values["LoadState"] == "not-found"
            else "FAILED"
            if values["ActiveState"] == "failed"
            else "BLOCKED"
            if values["UnitFileState"] in ("masked", "masked-runtime", "bad")
            else "RUNNING"
            if values["ActiveState"] == "active"
            else "STOPPED"
        )
        result["timer_enabled"] = values.get("UnitFileState") in ("enabled", "enabled-runtime")
        if not config.schedule.enabled and result["timer_enabled"]:
            result["diagnostics"].append("Timer is enabled while scheduling is disabled in the job")
        next_time = values.get("NextElapseUSecRealtime", "")
        if next_time and next_time not in ("n/a", "infinity", "0"):
            result["next_execution"] = next_time
        if values.get("Result") not in (None, "", "success"):
            result["diagnostics"].append("Timer result: " + values["Result"])
    except (TimerError, OSError) as error:
        result["diagnostics"].append(str(error))
        result["read_error"] = True
    return result
