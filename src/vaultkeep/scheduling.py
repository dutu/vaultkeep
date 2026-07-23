"""Systemd schedule rendering and timer-instance management for Debian hosts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.config import JobConfig, load_config
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


def render_schedule(config: JobConfig) -> RenderedSchedule:
    """Render the validated local-time systemd calendar schedule for a job."""
    schedule = config.schedule
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

    def __init__(self, paths: TimerPaths | None = None) -> None:
        self.paths = paths or TimerPaths()

    def require_environment(self) -> None:
        """Require the root-owned systemd environment promised by the v1 CLI."""
        geteuid = getattr(os, "geteuid", None)
        if not callable(geteuid) or geteuid() != 0:
            raise TimerError("Timer management requires root")
        if not Path("/run/systemd/system").is_dir():
            raise TimerError("Timer management requires systemd as the active system manager")
        if shutil.which("systemctl") is None or shutil.which("systemd-analyze") is None:
            raise TimerError("Timer management requires systemctl and systemd-analyze")
        version = self._run(("systemctl", "--version"))
        match = _SYSTEMD_VERSION.search(version)
        if match is None or int(match.group(1)) < 247:
            raise TimerError("Timer management requires systemd version 247 or newer")

    def install(self, config_path: Path) -> RenderedSchedule:
        config = self._load_managed(config_path, require_enabled=True)
        rendered = render_schedule(config)
        self._write_drop_in(config.job.id, rendered)
        self._daemon_reload()
        self._systemctl("enable", "--now", self._unit(config.job.id))
        self._register(config.job.id)
        return rendered

    def update(self, config_path: Path) -> RenderedSchedule:
        config = self._load_managed(config_path, require_enabled=True)
        rendered = render_schedule(config)
        enabled = self._is_enabled(config.job.id)
        self._write_drop_in(config.job.id, rendered)
        self._daemon_reload()
        if enabled:
            self._systemctl("restart", self._unit(config.job.id))
        self._register(config.job.id)
        return rendered

    def disable(self, config_path: Path) -> None:
        config = self._load_managed(config_path, require_enabled=False)
        self._systemctl("disable", "--now", self._unit(config.job.id), check=False)
        self._systemctl("clean", "--what=state", self._unit(config.job.id), check=False)

    def remove(self, config_path: Path) -> None:
        config = self._load_managed(config_path, require_enabled=False)
        self.disable(config_path)
        drop_in = self._drop_in_path(config.job.id)
        if drop_in.exists():
            drop_in.unlink()
            drop_in.parent.rmdir()
        self._daemon_reload()
        self._unregister(config.job.id)

    def next(self, config_path: Path) -> str:
        config = self._load_managed(config_path, require_enabled=False)
        if not self._drop_in_path(config.job.id).is_file():
            raise TimerError(f"Timer is not installed: {self._unit(config.job.id)}")
        return self._systemctl(
            "show", "--property=NextElapseUSecRealtime", "--value", self._unit(config.job.id)
        ).strip()

    def status(self, config_path: Path) -> str:
        config = self._load_managed(config_path, require_enabled=False)
        return self._systemctl("status", self._unit(config.job.id), check=False)

    def validate(self, config_path: Path) -> RenderedSchedule:
        config = self._load_managed(config_path, require_enabled=False)
        rendered = render_schedule(config)
        self._run(("systemd-analyze", "calendar", rendered.on_calendar))
        return rendered

    def sync(self, *, dry_run: bool = False) -> tuple[str, ...]:
        planned: list[str] = []
        for config_path in sorted(self.paths.jobs_root.glob("*.yaml")):
            config = self._load_managed(config_path, require_enabled=False)
            action = "update" if self._drop_in_path(config.job.id).exists() else "create"
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
            installed = self._drop_in_path(config.job.id).is_file()
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
            rendered = render_schedule(config)
            self._run(("systemd-analyze", "calendar", rendered.on_calendar))
            validated.append(f"valid {config.job.id}: {rendered.on_calendar}")
        return tuple(validated)

    def _load_managed(self, config_path: Path, *, require_enabled: bool) -> JobConfig:
        resolved = config_path.resolve()
        jobs_root = self.paths.jobs_root.resolve()
        if resolved.parent != jobs_root or resolved.suffix != ".yaml":
            raise TimerError(f"Timer configuration must be directly below {jobs_root}: {resolved}")
        config = load_config(resolved)
        validate_semantics(config, config_path=resolved)
        if config.job.id != resolved.stem:
            raise TimerError("Timer configuration filename must match job.id")
        if require_enabled and not config.schedule.enabled:
            raise TimerError("Timer installation requires schedule.enabled: true")
        return config

    def _unit(self, job_id: str) -> str:
        return f"vaultkeep@{job_id}.timer"

    def _drop_in_path(self, job_id: str) -> Path:
        return self.paths.units_root / f"vaultkeep@{job_id}.timer.d" / "schedule.conf"

    def _write_drop_in(self, job_id: str, rendered: RenderedSchedule) -> None:
        destination = self._drop_in_path(job_id)
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _atomic_write(destination, rendered.drop_in(), mode=0o644)

    def _registry(self) -> dict[str, str]:
        if not self.paths.registry_path.exists():
            return {}
        try:
            document = json.loads(self.paths.registry_path.read_text(encoding="utf-8"))
            return dict(document["instances"])
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise TimerError(f"Invalid timer ownership registry: {error}") from error

    def _register(self, job_id: str) -> None:
        registry = self._registry()
        registry[job_id] = str(self._drop_in_path(job_id))
        self.paths.registry_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_registry(self.paths.registry_path, registry)

    def _unregister(self, job_id: str) -> None:
        registry = self._registry()
        registry.pop(job_id, None)
        self.paths.registry_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_registry(self.paths.registry_path, registry)

    def _is_enabled(self, job_id: str) -> bool:
        return self._systemctl("is-enabled", self._unit(job_id), check=False).strip() == "enabled"

    def _daemon_reload(self) -> None:
        self._systemctl("daemon-reload")

    def _systemctl(self, *arguments: str, check: bool = True) -> str:
        return self._run(("systemctl", *arguments), check=check)

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
