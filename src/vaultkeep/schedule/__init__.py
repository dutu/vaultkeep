"""Systemd schedule rendering and timer-instance management for Debian hosts."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from vaultkeep.config import (
    JobConfig,
    load_config,
    parse_runtime_parameters,
    resolve_runtime_config,
)
from vaultkeep.errors import ConfigurationError, TimerError
from vaultkeep.installation import installation_path, systemd_argument
from vaultkeep.maintenance import command_selection
from vaultkeep.validation import validate_semantics

_WEEKDAYS = {
    "monday": "Mon",
    "tuesday": "Tue",
    "wednesday": "Wed",
    "thursday": "Thu",
    "friday": "Fri",
    "saturday": "Sat",
    "sunday": "Sun",
}
_SYSTEMD_VERSION = re.compile(r"systemd\s+(\d+)")
_UNINSTALLED_PARAMETERS = "no installed parameter selection; use schedule install with --param"

SERVICE_TEMPLATE = """[Unit]
Description=Vaultkeep backup job %i
Wants=network-online.target
After=network-online.target
ConditionPathExists=/etc/vaultkeep/jobs/%i.yaml

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vaultkeep --config /etc/vaultkeep/jobs/%i.yaml run
User=root
Group=root
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
LimitCORE=0
KillMode=control-group
TimeoutStopSec=5min
"""

TIMER_TEMPLATE = """[Unit]
Description=Vaultkeep backup timer %i

[Timer]
Unit=vaultkeep@%i.service
AccuracySec=1us
Persistent=true

[Install]
WantedBy=timers.target
"""


@dataclass(frozen=True, slots=True)
class RenderedSchedule:
    """The complete generated timer behavior for one validated job."""

    on_calendar: str
    randomized_delay_seconds: int
    fixed_random_delay: bool
    persistent: bool

    def drop_in(self) -> str:
        """Render the owned instance drop-in."""
        fixed = "yes" if self.fixed_random_delay else "no"
        persistent = "true" if self.persistent else "false"
        return (
            "[Timer]\n"
            "OnCalendar=\n"
            f"OnCalendar={self.on_calendar}\n"
            f"RandomizedDelaySec={self.randomized_delay_seconds}s\n"
            f"FixedRandomDelay={fixed}\n"
            "AccuracySec=1us\n"
            f"Persistent={persistent}\n"
        )


@dataclass(frozen=True, slots=True)
class TimerPaths:
    """Filesystem locations used by the timer manager."""

    jobs_root: Path = field(
        default_factory=lambda: installation_path("VAULTKEEP_CONFIG_ROOT") / "jobs"
    )
    units_root: Path = field(default_factory=lambda: installation_path("VAULTKEEP_SYSTEMD_ROOT"))
    registry_path: Path = field(
        default_factory=lambda: installation_path("VAULTKEEP_VAR_ROOT") / "systemd-instances.json"
    )


@dataclass(frozen=True, slots=True)
class _SyncChange:
    action: str
    instance: str
    timer_content: str | None = None
    service_content: str | None = None


def user_timer_paths() -> TimerPaths:
    """Return per-user job, unit, and registry paths for systemd user timers."""
    config_base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    state_base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return TimerPaths(
        jobs_root=config_base / "vaultkeep" / "jobs",
        units_root=config_base / "systemd" / "user",
        registry_path=state_base / "vaultkeep" / "systemd-instances.json",
    )


def render_schedule(config: JobConfig) -> RenderedSchedule:
    """Render the validated local-time systemd calendar schedule for a job."""
    schedule = config.schedule
    if not schedule.enabled:
        raise TimerError("Schedule is disabled")
    if schedule.interval is None:
        raise TimerError("Enabled schedules require interval")
    if schedule.at is not None:
        time_value = schedule.at
    elif schedule.window is not None:
        time_value = schedule.window.split("-", maxsplit=1)[0]
    else:
        raise TimerError("Schedule requires at or window")
    hour, minute = time_value.split(":", maxsplit=1)
    if schedule.interval == "hourly":
        on_calendar = f"*-*-* *:{minute}:00"
    elif schedule.interval == "daily":
        on_calendar = f"*-*-* {hour}:{minute}:00"
    elif schedule.interval == "weekly":
        on_calendar = f"{_WEEKDAYS[str(schedule.day).lower()]} *-*-* {hour}:{minute}:00"
    else:
        if not isinstance(schedule.day, int):
            raise TimerError("Monthly schedule requires a numeric day")
        on_calendar = f"*-*-{schedule.day:02d} {hour}:{minute}:00"
    delay = _window_duration(schedule.window) if schedule.window is not None else 0
    return RenderedSchedule(on_calendar, delay, schedule.window is not None, schedule.persistent)


def _window_duration(window: str) -> int:
    start, end = window.split("-", maxsplit=1)
    return (_minutes(end) - _minutes(start)) * 60


def _minutes(value: str) -> int:
    hour, minute = value.split(":", maxsplit=1)
    return int(hour) * 60 + int(minute)


class TimerManager:
    """Manage only Vaultkeep-owned systemd timer instance drop-ins."""

    def __init__(self, paths: TimerPaths | None = None, *, user_mode: bool = False) -> None:
        self.user_mode = user_mode
        self.paths = paths or (user_timer_paths() if user_mode else TimerPaths())

    def require_environment(self) -> None:
        """Require the root-owned systemd environment promised by the v1 CLI."""
        if not self.user_mode:
            geteuid = getattr(os, "geteuid", None)
            if not callable(geteuid) or geteuid() != 0:
                raise TimerError("Timer management requires root")
        if not Path("/run/systemd/system").is_dir():
            raise TimerError("Timer management requires systemd as the active system manager")
        if shutil.which("systemctl") is None or shutil.which("systemd-analyze") is None:
            raise TimerError("Timer management requires systemctl and systemd-analyze")
        command = (
            ("systemctl", "--user", "--version") if self.user_mode else ("systemctl", "--version")
        )
        version = self._run(command)
        match = _SYSTEMD_VERSION.search(version)
        if match is None or int(match.group(1)) < 247:
            raise TimerError("Timer management requires systemd version 247 or newer")

    def install(
        self, config_path: Path, *, runtime_params: Mapping[str, str] | None = None
    ) -> RenderedSchedule:
        parameters = dict(runtime_params or {})
        config = self._load_managed(config_path, require_enabled=True, runtime_params=parameters)
        rendered = render_schedule(config)
        instance = self._instance_id(config_path, config.job.id, parameters)
        self._write_timer_drop_in(instance, rendered)
        self._write_service_drop_in(config_path, instance, parameters)
        self._daemon_reload()
        self._systemctl("enable", "--now", self._unit(instance))
        self._register(instance)
        return rendered

    def update(
        self, config_path: Path, *, runtime_params: Mapping[str, str] | None = None
    ) -> RenderedSchedule:
        parameters = dict(runtime_params or {})
        config = self._load_managed(config_path, require_enabled=True, runtime_params=parameters)
        rendered = render_schedule(config)
        instance = self._instance_id(config_path, config.job.id, parameters)
        enabled = self._is_enabled(instance)
        self._write_timer_drop_in(instance, rendered)
        self._write_service_drop_in(config_path, instance, parameters)
        self._daemon_reload()
        if enabled:
            self._systemctl("restart", self._unit(instance))
        self._register(instance)
        return rendered

    def disable(
        self, config_path: Path, *, runtime_params: Mapping[str, str] | None = None
    ) -> None:
        parameters = dict(runtime_params or {})
        config = self._load_managed(config_path, require_enabled=False, runtime_params=parameters)
        instance = self._instance_id(config_path, config.job.id, parameters)
        self._systemctl("disable", "--now", self._unit(instance), check=False)
        self._systemctl("clean", "--what=state", self._unit(instance), check=False)

    def remove(self, config_path: Path, *, runtime_params: Mapping[str, str] | None = None) -> None:
        parameters = dict(runtime_params or {})
        config = self._load_managed(config_path, require_enabled=False, runtime_params=parameters)
        instance = self._instance_id(config_path, config.job.id, parameters)
        self.disable(config_path, runtime_params=parameters)
        if self.user_mode:
            for unit_file in (
                self._timer_drop_in_path(instance),
                self._service_drop_in_path(instance),
            ):
                if unit_file.exists():
                    unit_file.unlink()
        else:
            for drop_in in (
                self._timer_drop_in_path(instance),
                self._service_drop_in_path(instance),
            ):
                if drop_in.exists():
                    drop_in.unlink()
                    drop_in.parent.rmdir()
        self._daemon_reload()
        self._unregister(instance)

    def next(self, config_path: Path, *, runtime_params: Mapping[str, str] | None = None) -> str:
        parameters = dict(runtime_params or {})
        config = self._load_managed(config_path, require_enabled=False, runtime_params=parameters)
        instance = self._instance_id(config_path, config.job.id, parameters)
        if not self._timer_drop_in_path(instance).is_file():
            raise TimerError(f"Timer is not installed: {self._unit(instance)}")
        return self._systemctl(
            "show", "--property=NextElapseUSecRealtime", "--value", self._unit(instance)
        ).strip()

    def installation_status(
        self, config_path: Path, *, runtime_params: Mapping[str, str] | None = None
    ) -> tuple[str, bool]:
        """Return the selected timer name and whether its managed files exist."""
        parameters = dict(runtime_params or {})
        config = self._load_managed(config_path, require_enabled=False, runtime_params=parameters)
        instance = self._instance_id(config_path, config.job.id, parameters)
        installed = self._timer_drop_in_path(instance).is_file()
        if self.user_mode or parameters:
            installed = installed and self._service_drop_in_path(instance).is_file()
        return self._unit(instance), installed

    def validate(
        self, config_path: Path, *, runtime_params: Mapping[str, str] | None = None
    ) -> RenderedSchedule:
        config = self._load_managed(
            config_path, require_enabled=False, runtime_params=runtime_params
        )
        rendered = render_schedule(config)
        self._run(("systemd-analyze", "calendar", rendered.on_calendar))
        return rendered

    def sync(self, *, dry_run: bool = False) -> tuple[str, ...]:
        """Plan the whole synchronization, then apply it with rollback on failure."""
        planned: list[str] = []
        changes: list[_SyncChange] = []
        deleted: list[str] = []
        for config_path, config, parameters in self._managed_jobs(deleted=deleted):
            if parameters is None:
                planned.append(f"skip {config.job.id}: {_UNINSTALLED_PARAMETERS}")
                continue
            instance = self._instance_id(config_path, config.job.id, parameters)
            action = "update" if self._timer_drop_in_path(instance).is_file() else "create"
            if not config.schedule.enabled:
                action = "disable"
            planned.append(f"{action} {instance}")
            changes.append(
                _SyncChange(
                    action,
                    instance,
                    self._timer_content(instance, render_schedule(config))
                    if config.schedule.enabled
                    else None,
                    self._service_content(config_path, instance, parameters)
                    if config.schedule.enabled
                    else None,
                )
            )
        for instance in deleted:
            planned.append(f"remove {instance}")
            changes.append(_SyncChange("remove", instance))
        if not dry_run and changes:
            self._apply_sync(changes)
        return tuple(planned)

    def _apply_sync(self, changes: list[_SyncChange]) -> None:
        registry = self._registry()
        files = {self.paths.registry_path}
        for change in changes:
            files.update(
                (
                    self._timer_drop_in_path(change.instance),
                    self._service_drop_in_path(change.instance),
                )
            )
        snapshots: dict[Path, tuple[bytes, int] | None] = {}
        states: dict[str, tuple[bool, bool]] = {}
        try:
            for path in sorted(files):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    snapshots[path] = None
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise TimerError(f"Not a regular managed timer file: {path}")
                snapshots[path] = (path.read_bytes(), stat.S_IMODE(info.st_mode))
            for change in changes:
                enabled = self._is_enabled(change.instance)
                active = self._systemctl("is-active", self._unit(change.instance), check=False)
                states[change.instance] = (enabled, active.strip() == "active")
        except OSError as error:
            raise TimerError(f"Cannot snapshot schedules before sync: {error}") from error

        touched: dict[str, tuple[bool, bool]] = {}
        try:
            for change in changes:
                instance = change.instance
                touched[instance] = states[instance]
                if change.action in ("disable", "remove"):
                    if (
                        change.action == "remove"
                        or self._timer_drop_in_path(instance).is_file()
                        or any(states[instance])
                    ):
                        self._systemctl("disable", "--now", self._unit(instance))
                        self._systemctl("clean", "--what=state", self._unit(instance))
                    if change.action == "remove":
                        self._set_sync_file(self._timer_drop_in_path(instance), None)
                        self._set_sync_file(self._service_drop_in_path(instance), None)
                        self._daemon_reload()
                        registry.pop(instance)
                    continue
                self._set_sync_file(self._timer_drop_in_path(instance), change.timer_content)
                self._set_sync_file(self._service_drop_in_path(instance), change.service_content)
                self._daemon_reload()
                if change.action == "create":
                    self._systemctl("enable", "--now", self._unit(instance))
                elif states[instance][0]:
                    self._systemctl("restart", self._unit(instance))
                registry[instance] = str(self._timer_drop_in_path(instance))
            if any(change.action != "disable" for change in changes):
                self.paths.registry_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_registry(self.paths.registry_path, registry)
        except (Exception, KeyboardInterrupt) as error:
            errors = self._rollback_sync(snapshots, touched)
            detail = f"; rollback incomplete: {'; '.join(errors)}" if errors else "; rolled back"
            reason = "interrupted" if isinstance(error, KeyboardInterrupt) else str(error)
            raise TimerError(f"Schedule synchronization failed: {reason}{detail}") from error

    def _set_sync_file(self, path: Path, content: str | None) -> None:
        if content is not None:
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            _atomic_write(path, content, mode=0o644)
        else:
            path.unlink(missing_ok=True)
            if not self.user_mode:
                try:
                    path.parent.rmdir()
                except OSError as error:
                    if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                        raise

    def _rollback_sync(
        self,
        snapshots: dict[Path, tuple[bytes, int] | None],
        states: dict[str, tuple[bool, bool]],
    ) -> list[str]:
        errors: list[str] = []

        def attempt(
            operation: Callable[..., object], *arguments: object, **keywords: object
        ) -> None:
            try:
                operation(*arguments, **keywords)
            except (OSError, TimerError) as error:
                errors.append(str(error))

        for instance, (enabled, active) in states.items():
            if self._timer_drop_in_path(instance).is_file() or enabled or active:
                attempt(self._systemctl, "stop", self._unit(instance))
                # Remove new enablement links while the unit file still exists.
                if not enabled:
                    attempt(self._systemctl, "disable", self._unit(instance))
        for path, snapshot in snapshots.items():
            if snapshot is None:
                attempt(path.unlink, missing_ok=True)
            else:
                content, mode = snapshot
                attempt(path.parent.mkdir, mode=0o755, parents=True, exist_ok=True)
                attempt(_atomic_write, path, content, mode=mode)
        attempt(self._daemon_reload)
        for instance, (enabled, active) in states.items():
            if enabled:
                attempt(self._systemctl, "enable", self._unit(instance))
            if active:
                attempt(self._systemctl, "restart", self._unit(instance))
        return errors

    def list(self) -> tuple[str, ...]:
        result: list[str] = []
        for config_path, config, parameters in self._managed_jobs():
            if parameters is None:
                result.append(f"skip {config.job.id}: {_UNINSTALLED_PARAMETERS}")
                continue
            instance = self._instance_id(config_path, config.job.id, parameters)
            installed = self._timer_drop_in_path(instance).is_file()
            if self.user_mode or parameters:
                installed = installed and self._service_drop_in_path(instance).is_file()
            result.append(f"{instance}: enabled={config.schedule.enabled} installed={installed}")
        return tuple(result)

    def validate_all(self) -> tuple[str, ...]:
        """Validate every managed job without writing files or changing systemd state."""
        validated: list[str] = []
        for config_path, config, parameters in self._managed_jobs():
            if parameters is None:
                validated.append(f"skip {config.job.id}: {_UNINSTALLED_PARAMETERS}")
                continue
            instance = self._instance_id(config_path, config.job.id, parameters)
            if not config.schedule.enabled:
                validated.append(f"valid disabled {instance}")
                continue
            rendered = render_schedule(config)
            self._run(("systemd-analyze", "calendar", rendered.on_calendar))
            validated.append(f"valid {instance}: {rendered.on_calendar}")
        return tuple(validated)

    def _managed_jobs(
        self, *, deleted: list[str] | None = None
    ) -> list[tuple[Path, JobConfig, dict[str, str] | None]]:
        """Resolve all saved selections before any bulk operation changes timers."""
        registry = self._registry()
        templates = {
            path.resolve(): self._load_template(path)
            for path in sorted(self.paths.jobs_root.glob("*.yaml"))
        }
        selections: dict[Path, list[dict[str, str]]] = {}
        for instance, timer_path in sorted(registry.items()):
            if timer_path != str(self._timer_drop_in_path(instance)):
                raise TimerError(
                    f"Timer ownership registry path does not match instance: {instance}"
                )
            service_path = self._service_drop_in_path(instance)
            if not self.user_mode and not service_path.exists():
                # Without an override, the root service template always uses %i.yaml.
                # Instance names alone cannot identify a parameterized selection.
                config_path = (self.paths.jobs_root / f"{instance}.yaml").resolve()
                parameters: dict[str, str] = {}
            else:
                try:
                    config_path, parameters = command_selection(
                        service_path.read_text(encoding="utf-8"),
                        monitor=False,
                        user=self.user_mode,
                    )
                except (OSError, ValueError) as error:
                    raise TimerError(
                        f"Cannot read timer parameter selection {service_path}: {error}"
                    ) from error
                config_path = config_path.resolve()
                parameters = parse_runtime_parameters(
                    [f"{key}={value}" for key, value in parameters.items()]
                )
            if config_path not in templates:
                if (
                    deleted is not None
                    and config_path.parent == self.paths.jobs_root.resolve()
                    and config_path.suffix == ".yaml"
                    and not config_path.exists()
                    and not config_path.is_symlink()
                    and self._instance_id(config_path, config_path.stem, parameters) == instance
                ):
                    deleted.append(instance)
                    continue
                raise TimerError(
                    f"Registered timer configuration is not a managed job: {config_path}"
                )
            config = templates[config_path]
            if self._instance_id(config_path, config.job.id, parameters) != instance:
                raise TimerError(f"Saved parameters do not match timer instance: {instance}")
            selections.setdefault(config_path, []).append(parameters)

        jobs: list[tuple[Path, JobConfig, dict[str, str] | None]] = []
        for config_path, template in templates.items():
            for parameters in selections.get(config_path, [{}]):
                try:
                    config = resolve_runtime_config(template, parameters)
                    validate_semantics(config, config_path=config_path, runtime_params=parameters)
                except ConfigurationError as error:
                    if config_path in selections or any(
                        issue.code != "param_missing" for issue in error.issues
                    ):
                        raise
                    # Missing values defer runtime validation, but other detectable
                    # template errors must still fail preflight.
                    try:
                        validate_semantics(template, config_path=config_path)
                    except ConfigurationError as validation_error:
                        issues = tuple(
                            issue
                            for issue in validation_error.issues
                            if issue.code != "param_missing"
                        )
                        if issues:
                            raise ConfigurationError(issues) from validation_error
                    jobs.append((config_path, template, None))
                else:
                    jobs.append((config_path, config, parameters))
        return jobs

    def _load_template(self, config_path: Path) -> JobConfig:
        resolved = config_path.resolve()
        jobs_root = self.paths.jobs_root.resolve()
        if resolved.parent != jobs_root or resolved.suffix != ".yaml":
            raise TimerError(f"Timer configuration must be directly below {jobs_root}: {resolved}")
        config = load_config(resolved)
        if config.job.id != resolved.stem:
            raise TimerError("Timer configuration filename must match job.id")
        return config

    def _load_managed(
        self,
        config_path: Path,
        *,
        require_enabled: bool,
        runtime_params: Mapping[str, str] | None = None,
    ) -> JobConfig:
        parameters = dict(runtime_params or {})
        config = resolve_runtime_config(self._load_template(config_path), parameters)
        validate_semantics(config, config_path=config_path.resolve(), runtime_params=parameters)
        if require_enabled and not config.schedule.enabled:
            raise TimerError("Timer installation requires schedule.enabled: true")
        return config

    def _unit(self, instance: str) -> str:
        return f"vaultkeep@{instance}.timer"

    def _timer_drop_in_path(self, instance: str) -> Path:
        if self.user_mode:
            return self.paths.units_root / f"vaultkeep@{instance}.timer"
        return self.paths.units_root / f"vaultkeep@{instance}.timer.d" / "schedule.conf"

    def _service_drop_in_path(self, instance: str) -> Path:
        if self.user_mode:
            return self.paths.units_root / f"vaultkeep@{instance}.service"
        return self.paths.units_root / f"vaultkeep@{instance}.service.d" / "override.conf"

    def _write_timer_drop_in(self, instance: str, rendered: RenderedSchedule) -> None:
        destination = self._timer_drop_in_path(instance)
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _atomic_write(destination, self._timer_content(instance, rendered), mode=0o644)

    def _timer_content(self, instance: str, rendered: RenderedSchedule) -> str:
        if self.user_mode:
            content = (
                "[Unit]\n"
                f"Description=Vaultkeep user backup timer {instance}\n\n"
                "[Timer]\n"
                f"Unit=vaultkeep@{instance}.service\n"
                f"OnCalendar={rendered.on_calendar}\n"
                f"RandomizedDelaySec={rendered.randomized_delay_seconds}s\n"
                f"FixedRandomDelay={'yes' if rendered.fixed_random_delay else 'no'}\n"
                "AccuracySec=1us\n"
                f"Persistent={'true' if rendered.persistent else 'false'}\n\n"
                "[Install]\n"
                "WantedBy=timers.target\n"
            )
        else:
            content = rendered.drop_in()
        return content

    def _write_service_drop_in(
        self, config_path: Path, instance: str, runtime_params: Mapping[str, str]
    ) -> None:
        destination = self._service_drop_in_path(instance)
        content = self._service_content(config_path, instance, runtime_params)
        if content is None:
            if destination.exists():
                destination.unlink()
                destination.parent.rmdir()
            return
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _atomic_write(destination, content, mode=0o644)

    def _service_content(
        self, config_path: Path, instance: str, runtime_params: Mapping[str, str]
    ) -> str | None:
        resolved = config_path.resolve()
        arguments = [str(installation_path("VAULTKEEP_BIN_LINK"))]
        if self.user_mode:
            arguments.append("--user")
        arguments.extend(("--config", str(resolved)))
        for key, value in sorted(runtime_params.items()):
            arguments.extend(("--param", f"{key}={value}"))
        arguments.append("run")
        try:
            command = " ".join(
                systemd_argument(argument, executable=index == 0)
                for index, argument in enumerate(arguments)
            )
        except ValueError as error:
            raise TimerError(str(error)) from error
        condition = str(resolved).replace("%", "%%")
        if self.user_mode:
            content = (
                "[Unit]\n"
                f"Description=Vaultkeep user backup job {instance}\n"
                f"ConditionPathExists={condition}\n\n"
                "[Service]\n"
                "Type=oneshot\n"
                f"ExecStart={command}\n"
                "UMask=0077\n"
                "PrivateTmp=true\n"
                "LimitCORE=0\n"
                "KillMode=control-group\n"
                "TimeoutStopSec=5min\n"
            )
            return content
        if not runtime_params:
            return None
        content = (
            "[Unit]\n"
            "ConditionPathExists=\n"
            f"ConditionPathExists={condition}\n\n"
            "[Service]\n"
            "ExecStart=\n"
            f"ExecStart={command}\n"
        )
        return content

    def _registry(self) -> dict[str, str]:
        if not self.paths.registry_path.exists():
            return {}
        try:
            document = json.loads(self.paths.registry_path.read_text(encoding="utf-8"))
            if document == {}:  # Empty registry written by a fresh installer.
                return {}
            instances = document["instances"]
            if not isinstance(instances, dict) or any(
                not isinstance(instance, str)
                or re.fullmatch(r"[A-Za-z0-9_-]+", instance) is None
                or not isinstance(path, str)
                for instance, path in instances.items()
            ):
                raise ValueError("instances must map valid instance names to timer paths")
            return dict(instances)
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise TimerError(f"Invalid timer ownership registry: {error}") from error

    def _register(self, instance: str) -> None:
        registry = self._registry()
        registry[instance] = str(self._timer_drop_in_path(instance))
        self.paths.registry_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_registry(self.paths.registry_path, registry)

    def _unregister(self, instance: str) -> None:
        registry = self._registry()
        registry.pop(instance, None)
        self.paths.registry_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_registry(self.paths.registry_path, registry)

    def _is_enabled(self, instance: str) -> bool:
        return self._systemctl("is-enabled", self._unit(instance), check=False).strip() == "enabled"

    def _instance_id(
        self, config_path: Path, job_id: str, runtime_params: Mapping[str, str]
    ) -> str:
        if not runtime_params:
            return job_id
        payload = json.dumps(
            {
                "config": str(config_path.resolve()),
                "job": job_id,
                "params": dict(sorted(runtime_params.items())),
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        import hashlib

        suffix = hashlib.sha256(payload.encode("ascii")).hexdigest()[:16]
        summary_parts = [
            f"{_sanitize_instance_part(key)}-{_sanitize_instance_part(value)}"
            for key, value in sorted(runtime_params.items())
        ]
        summary = "--".join(summary_parts)
        if len(summary) > 80:
            summary = summary[:80].rstrip("-")
        return f"{job_id}--{summary}--{suffix}"

    def _daemon_reload(self) -> None:
        self._systemctl("daemon-reload")

    def _systemctl(self, *arguments: str, check: bool = True) -> str:
        prefix = ("systemctl", "--user") if self.user_mode else ("systemctl",)
        return self._run((*prefix, *arguments), check=check)

    def _run(self, command: tuple[str, ...], *, check: bool = True) -> str:
        try:
            completed = subprocess.run(command, check=False, capture_output=True, text=True)
        except OSError as error:
            raise TimerError(f"Cannot run {' '.join(command)}: {error}") from error
        if check and completed.returncode != 0:
            raise TimerError(f"{' '.join(command)} failed: {completed.stderr.strip()}")
        return completed.stdout


def _atomic_write(path: Path, content: str | bytes, *, mode: int) -> None:
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content.encode("utf-8") if isinstance(content, str) else content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise TimerError(f"Cannot write timer file {path}: {error}") from error


def _write_registry(path: Path, instances: dict[str, str]) -> None:
    payload = json.dumps({"version": 1, "instances": instances}, sort_keys=True, indent=2) + "\n"
    _atomic_write(path, payload, mode=0o600)


def _sanitize_instance_part(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return sanitized or "value"
