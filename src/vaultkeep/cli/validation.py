"""Complete job validation and read-only automation health reporting."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.config import JobConfig, load_config, resolve_runtime_config
from vaultkeep.errors import (
    ConfigurationError,
    DestinationError,
    StateError,
    TimerError,
    VaultkeepError,
)
from vaultkeep.file_backups import file_key, select_files
from vaultkeep.scheduling import TimerManager
from vaultkeep.state.identity import job_identity_hash, job_state_path
from vaultkeep.state.local_state import StateLoadStatus, load_local_state
from vaultkeep.trigger.config import TriggerConfig
from vaultkeep.trigger.config import load_config as load_trigger_config
from vaultkeep.trigger.errors import StateError as TriggerStateError
from vaultkeep.trigger.errors import TriggerError
from vaultkeep.trigger.models import AttemptOutcome, Observation, parse_utc, utc_now
from vaultkeep.trigger.paths import state_root
from vaultkeep.trigger.preflight import runtime_preflight
from vaultkeep.trigger.service import service_name
from vaultkeep.trigger.state import StateStore
from vaultkeep.trigger.systemd import RealSystemdController
from vaultkeep.validation import validate_semantics
from vaultkeep.workflow import WorkflowPaths, user_workflow_paths, validate_job


@dataclass
class _Report:
    exit_code: int = 0
    incomplete: bool = False

    def problem(self, label: str, detail: str, *, code: int = 3) -> None:
        print(f"{label}: {detail}")
        self.exit_code = self.exit_code or code

    def check(self, label: str, action: Callable[[], object], success: str | None = None) -> bool:
        try:
            action()
        except (VaultkeepError, TriggerError, OSError) as error:
            code = 7 if isinstance(error, VaultkeepError) else 3
            if isinstance(error, (ConfigurationError, TimerError)):
                code = 3
            elif isinstance(error, DestinationError):
                code = 5
            elif isinstance(error, (StateError, TriggerStateError)):
                code = 15
            print(str(error), file=sys.stderr)
            self.problem(label, "failed", code=code)
            return False
        if success is not None:
            print(f"{label}: {success}")
        return True


def _unit_properties(unit: str, *, user_mode: bool) -> dict[str, str]:
    command = ["systemctl"]
    if user_mode:
        command.append("--user")
    command.extend(
        [
            "show",
            "--all",
            "--no-pager",
            "--property=LoadState,ActiveState,SubState,UnitFileState,NextElapseUSecRealtime,Result",
            unit,
        ]
    )
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TimerError(f"cannot inspect {unit}: {error}") from error
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not values.get("LoadState") or (result.returncode and values["LoadState"] != "not-found"):
        raise TimerError(f"cannot inspect {unit}: {result.stderr.strip() or 'incomplete response'}")
    if values["LoadState"] != "not-found" and any(
        not values.get(key) for key in ("ActiveState", "SubState", "UnitFileState")
    ):
        raise TimerError(f"cannot inspect {unit}: incomplete response")
    return values


def _service_health(
    report: _Report, label: str, unit: str, *, user_mode: bool, installed: bool = True
) -> dict[str, str] | None:
    print(f"{label} unit: {unit}")
    if not installed:
        report.incomplete = True
        report.problem(label, "not installed")
    try:
        values = _unit_properties(unit, user_mode=user_mode)
    except TimerError as error:
        report.problem(label, f"unknown — {error}")
        return None
    if values["LoadState"] == "not-found":
        if installed:
            report.incomplete = True
            report.problem(label, "not installed")
        return values
    if not installed:
        return values
    load, active, enabled = (values[key] for key in ("LoadState", "ActiveState", "UnitFileState"))
    activity = {"inactive": "stopped", "activating": "starting"}.get(active, active)
    if active == "active" and label == "Monitor service":
        activity = "running"
    detail = f"installed, {enabled}, {activity}"
    if load != "loaded":
        report.problem(label, f"{detail}; load state: {load}")
    elif enabled not in ("enabled", "enabled-runtime") or active != "active":
        report.problem(label, detail)
    else:
        print(f"{label}: {detail}")
    return values


def _trigger_state_health(report: _Report, config: TriggerConfig) -> None:
    store = StateStore(state_root(config.scope))
    blocked = store.read_blocked(config.job_id)
    state = store.read_instance(config.job_id)
    if blocked is not None:
        report.problem("Trigger state", f"dispatch blocked — {blocked}")
    if state is None:
        report.problem("Monitor updates", "no recorded state")
        return
    now, updated = parse_utc(utc_now()), parse_utc(state.last_monitor_update)
    assert now is not None
    age = (now - updated).total_seconds() if updated is not None else None
    # Use the same observation-gap threshold as the monitor.
    if age is None or age < 0 or age > 2 * config.poll_seconds:
        report.problem("Monitor updates", "stale or unavailable")
    else:
        print(f"Monitor updates: current ({age:.0f}s ago)")
    instance = config.instance_by_id(config.job_id)
    if instance is not None and state.config_fingerprint != instance.fingerprint():
        report.problem("Monitor configuration", "differs from this job; update the monitor")
    if state.last_observation is None:
        report.problem("Process observation", "unavailable")
    elif state.last_observation.observation in (
        Observation.AMBIGUOUS,
        Observation.INSPECTION_UNAVAILABLE,
    ):
        report.problem(
            "Process observation",
            f"{state.last_observation.observation.value} — {state.last_observation.reason}",
        )
    else:
        print(f"Process observation: {state.last_observation.observation.value}")
    if state.review_required:
        report.problem("Trigger state", f"review required — {state.review_reason}")
    if state.attempt_summaries:
        latest = state.attempt_summaries[-1]
        outcome = latest["outcome"]
        if outcome not in (AttemptOutcome.CREATED.value, AttemptOutcome.UNCHANGED.value):
            report.problem("Last Trigger backup", str(outcome))
        else:
            print(f"Last Trigger backup: {outcome}")


def _trigger_health(report: _Report, config_path: Path, params: dict[str, str]) -> None:
    config = load_trigger_config(config_path, runtime_params=params)
    report.check(
        "Trigger runtime prerequisites",
        lambda: runtime_preflight(
            config, RealSystemdController(), state_root(config.scope), validate_backup=False
        ),
        "passed (user manager, worker template, runtime and state permissions)",
    )
    _service_health(report, "Monitor service", service_name(config), user_mode=True)
    report.check("Trigger recovery checks", lambda: _trigger_state_health(report, config))


def _schedule_health(
    report: _Report, config_path: Path, params: dict[str, str], *, user_mode: bool
) -> None:
    manager = TimerManager(user_mode=user_mode)

    def prerequisites() -> None:
        manager.require_environment()
        manager.validate(config_path, runtime_params=params)

    report.check("Schedule runtime prerequisites", prerequisites, "passed (systemd and calendar)")
    try:
        unit, installed = manager.installation_status(config_path, runtime_params=params)
    except TimerError as error:
        report.problem("Timer installation", str(error))
        return
    values = _service_health(report, "Timer", unit, user_mode=user_mode, installed=installed)
    next_run = values.get("NextElapseUSecRealtime") if values is not None else None
    if next_run and next_run not in ("n/a", "0"):
        print(f"Next scheduled run: {next_run}")
    elif installed:
        report.problem("Next scheduled run", "unavailable")
    service = unit.removesuffix(".timer") + ".service"
    try:
        backup = _unit_properties(service, user_mode=user_mode)
    except TimerError as error:
        report.problem("Backup service", str(error))
        return
    if backup["LoadState"] != "loaded":
        report.problem("Backup service", backup["LoadState"])
    elif backup["ActiveState"] == "failed" or backup.get("Result") not in (None, "", "success"):
        report.problem("Last scheduled backup", backup.get("Result") or backup["ActiveState"])
    else:
        print(f"Backup service: {backup['ActiveState']} ({backup['SubState']})")


def _backup_history(
    report: _Report,
    config: JobConfig,
    config_path: Path,
    params: dict[str, str],
    *,
    user_mode: bool,
) -> None:
    root = (user_workflow_paths() if user_mode else WorkflowPaths()).state_root
    files: list[tuple[str | None, str | None]] = (
        [(file_key(path.name), path.name) for path in select_files(config)]
        if config.backup_mode == "per_file"
        else [(None, None)]
    )
    recorded = False
    for key, name in files:
        identity = job_identity_hash(
            config_path, config.job.id, runtime_params=params, file_key=key
        )
        loaded = load_local_state(
            job_state_path(
                config_path, config.job.id, runtime_params=params, file_key=key, state_root=root
            ),
            expected_job_id=config.job.id,
            expected_identity_hash=identity,
        )
        label = f"Last backup ({name})" if name is not None else "Last backup"
        if loaded.status == StateLoadStatus.UNUSABLE:
            report.problem(label, f"state unreadable — {loaded.reason}", code=15)
        elif loaded.state is not None and loaded.state.last_run is not None:
            recorded = True
            result = loaded.state.last_run.result
            if result == "failed":
                report.problem(label, "failed")
            else:
                print(f"{label}: {result} ({loaded.state.last_run.at_utc})")
    if not recorded:
        print("Backup history: no completed run recorded")


def validate_command(
    config_path: Path,
    *,
    schema_only: bool,
    runtime_params: Mapping[str, str],
    user_mode: bool,
) -> int:
    """Validate every applicable part without installing or starting automation."""
    report = _Report()
    try:
        config = load_config(config_path)
        params = dict(config.trigger.params) if config.trigger.enabled else {}
        params.update(runtime_params)
        if not schema_only:
            config = resolve_runtime_config(config, params)
            validate_semantics(config, config_path=config_path, runtime_params=params)
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        report.problem("Configuration", "invalid")
        print("Result: failed")
        return report.exit_code
    print("Configuration: valid")
    print(f"Job: {config.job.id}")
    print(f"Mode: {'user' if user_mode else 'system'}")
    if config.trigger.enabled:
        print("Trigger: enabled")
    elif config.schedule.enabled:
        print("Schedule: enabled")
    else:
        print("Schedule: disabled\nTrigger: disabled")
    if schema_only:
        print("Runtime checks: skipped (--schema-only)")
        print("Result: configuration structure valid")
        return 0
    report.check(
        "Backup sources and destination",
        lambda: validate_job(config_path, runtime_params=params),
        "valid",
    )
    report.check(
        "Backup history checks",
        lambda: _backup_history(report, config, config_path, params, user_mode=user_mode),
    )
    if config.trigger.enabled:
        if not user_mode or os.geteuid() == 0:
            report.problem(
                "Trigger runtime prerequisites",
                "unsupported — run as the application owner with --user, without sudo",
            )
        else:
            report.check("Trigger checks", lambda: _trigger_health(report, config_path, params))
    elif config.schedule.enabled:
        report.check(
            "Schedule checks",
            lambda: _schedule_health(report, config_path, params, user_mode=user_mode),
        )
    result = (
        "setup incomplete" if report.incomplete else "failed" if report.exit_code else "healthy"
    )
    print(f"Result: {result}")
    return report.exit_code
