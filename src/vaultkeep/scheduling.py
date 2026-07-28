"""Systemd schedule rendering and timer-instance management for Debian hosts."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.config import JobConfig, load_config, resolve_runtime_config
from vaultkeep.errors import TimerError
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

    jobs_root: Path = Path("/etc/vaultkeep/jobs")
    units_root: Path = Path("/etc/systemd/system")
    registry_path: Path = Path("/var/lib/vaultkeep/systemd-instances.json")


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
            ("systemctl", "--user", "--version")
            if self.user_mode
            else ("systemctl", "--version")
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

    def status(self, config_path: Path, *, runtime_params: Mapping[str, str] | None = None) -> str:
        parameters = dict(runtime_params or {})
        config = self._load_managed(config_path, require_enabled=False, runtime_params=parameters)
        instance = self._instance_id(config_path, config.job.id, parameters)
        return self._systemctl("status", self._unit(instance), check=False)

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
        planned: list[str] = []
        for config_path in sorted(self.paths.jobs_root.glob("*.yaml")):
            config = self._load_managed(config_path, require_enabled=False)
            action = "update" if self._timer_drop_in_path(config.job.id).exists() else "create"
            if not config.schedule.enabled:
                action = "disable"
            planned.append(f"{action} {config.job.id}")
            if not dry_run:
                if config.schedule.enabled:
                    (self.update if action == "update" else self.install)(config_path)
                else:
                    self.disable(config_path)
        return tuple(planned)

    def list(self) -> tuple[str, ...]:
        result: list[str] = []
        for config_path in sorted(self.paths.jobs_root.glob("*.yaml")):
            config = self._load_managed(config_path, require_enabled=False)
            installed = self._timer_drop_in_path(config.job.id).is_file()
            result.append(
                f"{config.job.id}: enabled={config.schedule.enabled} installed={installed}"
            )
        return tuple(result)

    def validate_all(self) -> tuple[str, ...]:
        """Validate every managed job without writing files or changing systemd state."""
        validated: list[str] = []
        self._registry()
        for config_path in sorted(self.paths.jobs_root.glob("*.yaml")):
            config = self._load_managed(config_path, require_enabled=False)
            if not config.schedule.enabled:
                validated.append(f"valid disabled {config.job.id}")
                continue
            rendered = render_schedule(config)
            self._run(("systemd-analyze", "calendar", rendered.on_calendar))
            validated.append(f"valid {config.job.id}: {rendered.on_calendar}")
        return tuple(validated)

    def _load_managed(
        self,
        config_path: Path,
        *,
        require_enabled: bool,
        runtime_params: Mapping[str, str] | None = None,
    ) -> JobConfig:
        resolved = config_path.resolve()
        jobs_root = self.paths.jobs_root.resolve()
        if resolved.parent != jobs_root or resolved.suffix != ".yaml":
            raise TimerError(f"Timer configuration must be directly below {jobs_root}: {resolved}")
        parameters = dict(runtime_params or {})
        config = resolve_runtime_config(load_config(resolved), parameters)
        validate_semantics(config, config_path=resolved, runtime_params=parameters)
        if config.job.id != resolved.stem:
            raise TimerError("Timer configuration filename must match job.id")
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
        _atomic_write(destination, content, mode=0o644)

    def _write_service_drop_in(
        self, config_path: Path, instance: str, runtime_params: Mapping[str, str]
    ) -> None:
        destination = self._service_drop_in_path(instance)
        resolved = config_path.resolve()
        arguments = ["/usr/local/bin/vaultkeep"]
        if self.user_mode:
            arguments.append("--user")
        arguments.extend(("--config", str(resolved)))
        for key, value in sorted(runtime_params.items()):
            arguments.extend(("--param", f"{key}={value}"))
        arguments.append("run")
        command = shlex.join(arguments) if self.user_mode else " ".join(arguments)
        if self.user_mode:
            content = (
                "[Unit]\n"
                f"Description=Vaultkeep user backup job {instance}\n"
                f"ConditionPathExists={resolved}\n\n"
                "[Service]\n"
                "Type=oneshot\n"
                f"ExecStart={command}\n"
                "UMask=0077\n"
                "PrivateTmp=true\n"
                "LimitCORE=0\n"
                "KillMode=control-group\n"
                "TimeoutStopSec=5min\n"
            )
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            _atomic_write(destination, content, mode=0o644)
            return
        if not runtime_params:
            if destination.exists():
                destination.unlink()
                destination.parent.rmdir()
            return
        content = (
            "[Unit]\n"
            "ConditionPathExists=\n"
            f"ConditionPathExists={resolved}\n\n"
            "[Service]\n"
            "ExecStart=\n"
            f"ExecStart={command}\n"
        )
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _atomic_write(destination, content, mode=0o644)

    def _registry(self) -> dict[str, str]:
        if not self.paths.registry_path.exists():
            return {}
        try:
            document = json.loads(self.paths.registry_path.read_text(encoding="utf-8"))
            return dict(document["instances"])
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


def _atomic_write(path: Path, content: str, *, mode: int) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise TimerError(f"Cannot write timer file {path}: {error}") from error


def _write_registry(path: Path, instances: dict[str, str]) -> None:
    _atomic_write(path, json.dumps({"version": 1, "instances": instances}) + "\n", mode=0o600)


def _sanitize_instance_part(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return sanitized or "value"
