"""Complete job validation and read-only automation health reporting."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from vaultkeep.cli.output import Console, configure_logging
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

_LOG = logging.getLogger("vaultkeep.cli.validation")


@dataclass
class _Report:
    output: Console
    exit_code: int = 0
    incomplete: bool = False
    section: str = "Configuration"
    sections: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    issues: list[tuple[str, bool]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def row(self, label: str, detail: str) -> None:
        self.sections.setdefault(self.section, []).append((label, detail))

    def problem(
        self,
        label: str,
        detail: str,
        *,
        code: int = 3,
        issue: str | None = None,
        diagnostic: bool = False,
    ) -> None:
        self.row(label, detail)
        message = issue or f"{label}: {detail}."
        if (message, diagnostic) not in self.issues:
            self.issues.append((message, diagnostic))
        self.exit_code = self.exit_code or code

    def warning(self, message: str) -> None:
        self.row("Executable version", "WARNING")
        self.issues.append((message, False))

    def check(self, label: str, action: Callable[[], object], success: str | None = None) -> bool:
        _LOG.debug("Checking %s", label)
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
            self.problem(label, "FAILED", code=code, issue=str(error), diagnostic=True)
            return False
        if success is not None:
            self.row(label, "OK")
            _LOG.info("%s: %s", label, success)
        return True

    def render(self, job: str | None, user_mode: bool, *, schema_only: bool = False) -> int:
        result = (
            "SETUP INCOMPLETE"
            if self.incomplete
            else "FAILED"
            if self.exit_code
            else "VALID"
            if schema_only
            else "OK"
        )
        self.output.heading("Validation")
        rows = [("Job", job)] if job is not None else []
        rows.extend([("Mode", "user" if user_mode else "system"), ("Result", result)])
        self.output.fields(rows)
        for section, fields in self.sections.items():
            self.output.fields(fields, section=section)
        if any(not diagnostic for _, diagnostic in self.issues) or self.actions:
            self.output.print()
        for message, diagnostic in self.issues:
            self.output.print(f"Issue: {message}", file=sys.stderr if diagnostic else None)
        for action in self.actions:
            self.output.print(f"Next: {action}")
        return self.exit_code


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
    _LOG.info("%s unit: %s", label, unit)
    if not installed:
        report.incomplete = True
        report.problem(
            label,
            "NOT INSTALLED",
            issue=f"{label} is not installed; automatic backups are unavailable.",
        )
    try:
        values = _unit_properties(unit, user_mode=user_mode)
    except TimerError as error:
        report.problem(label, f"unknown — {error}")
        return None
    if values["LoadState"] == "not-found":
        if installed:
            report.incomplete = True
            report.problem(
                label,
                "NOT INSTALLED",
                issue=f"{label} is not installed; automatic backups are unavailable.",
            )
        return values
    if not installed:
        return values
    load, active, enabled = (values[key] for key in ("LoadState", "ActiveState", "UnitFileState"))
    activity = {
        "active": "RUNNING",
        "inactive": "STOPPED",
        "failed": "FAILED",
        "activating": "starting",
    }.get(active, active)
    _LOG.debug("%s properties: %s", label, values)
    if load != "loaded":
        report.problem(label, "FAILED", issue=f"{label} cannot load ({load}).")
    elif enabled not in ("enabled", "enabled-runtime") or active != "active":
        report.problem(
            label,
            activity,
            issue=f"{label} is {activity.lower()} with enablement {enabled}; "
            "check its service state before relying on automatic backups.",
        )
    else:
        report.row(label, activity)
    report.row(
        f"{label} enablement", {"enabled": "ENABLED", "disabled": "DISABLED"}.get(enabled, enabled)
    )
    return values


def _trigger_state_health(
    report: _Report, config: TriggerConfig, *, monitor_missing: bool = False
) -> None:
    store = StateStore(state_root(config.scope))
    blocked = store.read_blocked(config.job_id)
    state = store.read_instance(config.job_id)
    if blocked is not None:
        report.problem("Trigger state", f"dispatch blocked — {blocked}")
    if state is None:
        if monitor_missing:
            report.row("Monitor updates", "no recorded state")
        else:
            report.problem("Monitor updates", "no recorded state")
        return
    now, updated = parse_utc(utc_now()), parse_utc(state.last_monitor_update)
    assert now is not None
    age = (now - updated).total_seconds() if updated is not None else None
    # Use the same observation-gap threshold as the monitor.
    if age is None or age < 0 or age > 2 * config.poll_seconds:
        if monitor_missing:
            report.row("Monitor updates", "stale or unavailable")
        else:
            report.problem("Monitor updates", "stale or unavailable")
    else:
        report.row("Monitor updates", f"current ({age:.0f}s ago)")
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
        report.row("Process observation", state.last_observation.observation.value)
    if state.review_required:
        report.problem("Trigger state", f"review required — {state.review_reason}")
    if state.attempt_summaries:
        latest = state.attempt_summaries[-1]
        outcome = latest["outcome"]
        if outcome not in (AttemptOutcome.CREATED.value, AttemptOutcome.UNCHANGED.value):
            report.problem("Last Trigger backup", str(outcome))
        else:
            report.row("Last Trigger backup", str(outcome))


def _trigger_health(report: _Report, config_path: Path, params: dict[str, str]) -> None:
    config = load_trigger_config(config_path, runtime_params=params)
    report.check(
        "Trigger runtime prerequisites",
        lambda: runtime_preflight(
            config,
            RealSystemdController(),
            state_root(config.scope),
            validate_backup=False,
            warning=report.warning,
        ),
        "passed (user manager, worker template, runtime and state permissions)",
    )
    values = _service_health(report, "Monitor service", service_name(config), user_mode=True)
    missing = values is not None and values["LoadState"] == "not-found"
    report.check(
        "Trigger recovery checks",
        lambda: _trigger_state_health(report, config, monitor_missing=missing),
    )


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
        report.row("Next scheduled run", next_run)
    elif installed and values is not None and values["LoadState"] != "not-found":
        report.problem("Next scheduled run", "unavailable")
    service = unit.removesuffix(".timer") + ".service"
    try:
        backup = _unit_properties(service, user_mode=user_mode)
    except TimerError as error:
        report.problem("Backup service", str(error))
        return
    if backup["LoadState"] == "not-found" and report.incomplete:
        report.row("Backup service", "NOT INSTALLED")
    elif backup["LoadState"] != "loaded":
        report.problem("Backup service", backup["LoadState"])
    elif backup["ActiveState"] == "failed" or backup.get("Result") not in (None, "", "success"):
        report.problem("Last scheduled backup", backup.get("Result") or backup["ActiveState"])
    else:
        report.row("Backup service", f"{backup['ActiveState']} ({backup['SubState']})")


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
            timestamp = parse_utc(loaded.state.last_run.at_utc)
            report.row(
                label,
                timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
                if timestamp
                else loaded.state.last_run.at_utc,
            )
            result_label = f"Last result ({name})" if name is not None else "Last result"
            if result == "failed":
                report.problem(result_label, result)
            else:
                report.row(result_label, result)
    if not recorded:
        report.row("History", "no completed run recorded")


def validate_command(
    config_path: Path,
    *,
    schema_only: bool,
    runtime_params: Mapping[str, str],
    user_mode: bool,
    no_color: bool = False,
) -> int:
    """Validate every applicable part without installing or starting automation."""
    report = _Report(Console(no_color=no_color))
    try:
        config = load_config(config_path)
        params = dict(config.trigger.params) if config.trigger.enabled else {}
        params.update(runtime_params)
        if not schema_only:
            config = resolve_runtime_config(config, params)
            validate_semantics(config, config_path=config_path, runtime_params=params)
    except ConfigurationError as error:
        report.problem("Configuration", "FAILED", issue=str(error), diagnostic=True)
        return report.render(None, user_mode)
    configure_logging(config.logging.level, no_color=no_color)
    report.row("Configuration", "VALID")
    if schema_only:
        report.row("Runtime checks", "skipped (--schema-only)")
        report.issues.append(
            ("Runtime checks were skipped; readiness has not been checked.", False)
        )
        report.section = "Automation"
        if config.trigger.enabled:
            report.row("Trigger", "ENABLED")
        elif config.schedule.enabled:
            report.row("Schedule", "ENABLED")
        else:
            report.row("Schedule", "DISABLED")
            report.row("Trigger", "DISABLED")
        return report.render(config.job.id, user_mode, schema_only=True)
    report.check(
        "Backup paths",
        lambda: validate_job(config_path, runtime_params=params),
        "valid",
    )
    report.section = "Backup"
    report.check(
        "Backup history checks",
        lambda: _backup_history(report, config, config_path, params, user_mode=user_mode),
    )
    if config.trigger.enabled:
        report.section = "Trigger monitoring"
        report.row("Trigger", "ENABLED")
        if not user_mode or os.geteuid() == 0:
            report.problem(
                "Trigger runtime prerequisites",
                "unsupported — run as the application owner with --user, without sudo",
            )
        else:
            report.check("Trigger checks", lambda: _trigger_health(report, config_path, params))
    elif config.schedule.enabled:
        report.section = "Scheduling"
        report.row("Schedule", "ENABLED")
        report.check(
            "Schedule checks",
            lambda: _schedule_health(report, config_path, params, user_mode=user_mode),
        )
    else:
        report.section = "Automation"
        report.row("Schedule", "DISABLED")
        report.row("Trigger", "DISABLED")
    if report.incomplete:
        command = ["vaultkeep", *(["--user"] if user_mode else []), "--config", str(config_path)]
        for key, value in params.items():
            command.extend(["--param", f"{key}={value}"])
        command.extend(["trigger" if config.trigger.enabled else "schedule", "install"])
        report.actions.append(shlex.join(command))
    return report.render(config.job.id, user_mode)
