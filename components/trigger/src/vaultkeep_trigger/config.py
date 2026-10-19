"""TOML configuration loading and strict validation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vaultkeep_trigger.errors import ConfigurationError
from vaultkeep_trigger.paths import resolve_absolute_path

SUPPORTED_APPS = ("electrum", "bisq", "wasabi")
ENTRYPOINT_KINDS = ("script", "jar", "java_main")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
ALLOWED_ENV_KEYS: dict[str, frozenset[str]] = {
    "electrum": frozenset({"ELECTRUMDIR"}),
    "wasabi": frozenset({"WASABI_DATADIR"}),
    "bisq": frozenset(),
}
INSTANCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

MAX_CMD_REGEX_LENGTH = 200
MAX_ARGV_TOKEN_LENGTH = 4096

_TRIGGER_KEYS = {
    "poll_seconds",
    "confirm_seconds",
    "deadline_seconds",
    "log_level",
    "vaultkeep",
    "notifications",
}
_NOTIFICATION_KEYS = {"enabled", "command", "timeout_ms"}
_INSTANCE_KEYS = {
    "id",
    "app",
    "enabled",
    "data_dir",
    "matcher",
    "job",
    "poll_seconds",
    "confirm_seconds",
    "deadline_seconds",
}
_MATCHER_KEYS = {"exe_realpath", "entrypoint", "argv_tokens", "cmdline_regex", "env_eq"}
_ENTRYPOINT_KEYS = {"kind", "value"}
_JOB_KEYS = {"config", "params"}


class _Issues:
    """Collect validation issues so one run reports every problem."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def add(self, message: str) -> None:
        self.messages.append(message)

    def raise_if_any(self) -> None:
        if self.messages:
            raise ConfigurationError("\n".join(self.messages))


@dataclass(frozen=True, slots=True)
class JobSpec:
    """One reviewed VaultKeep job selection."""

    config: Path
    params: dict[str, str] = field(default_factory=dict)

    def argv(self) -> list[str]:
        """Return the VaultKeep argument prefix for this job (without the command)."""
        argv = ["--user", "--config", str(self.config)]
        for key in sorted(self.params):
            argv.extend(["--param", f"{key}={self.params[key]}"])
        return argv

    def canonical(self) -> dict[str, Any]:
        return {"config": str(self.config), "params": dict(sorted(self.params.items()))}


@dataclass(frozen=True, slots=True)
class Entrypoint:
    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class MatcherSpec:
    """Strong process identity plus optional supporting predicates."""

    exe_realpath: str | None = None
    entrypoint: Entrypoint | None = None
    argv_tokens: tuple[str, ...] = ()
    cmdline_regex: str | None = None
    env_eq: dict[str, str] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "exe_realpath": self.exe_realpath,
            "entrypoint": (
                {"kind": self.entrypoint.kind, "value": self.entrypoint.value}
                if self.entrypoint
                else None
            ),
            "argv_tokens": list(self.argv_tokens),
            "cmdline_regex": self.cmdline_regex,
            "env_eq": dict(sorted(self.env_eq.items())),
        }


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    id: str
    app: str
    enabled: bool
    data_dir: Path
    matcher: MatcherSpec
    job: JobSpec
    poll_seconds: float
    confirm_seconds: float
    deadline_seconds: float | None

    def fingerprint(self) -> str:
        """Return a stable fingerprint of the instance's execution-relevant settings."""
        payload = {
            "id": self.id,
            "app": self.app,
            "enabled": self.enabled,
            "data_dir": str(self.data_dir),
            "matcher": self.matcher.canonical(),
            "job": self.job.canonical(),
            "deadline_seconds": self.deadline_seconds,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    enabled: bool = False
    command: str = "notify-send"
    timeout_ms: int = 5000


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    poll_seconds: float
    confirm_seconds: float
    deadline_seconds: float | None
    log_level: str
    vaultkeep: Path
    notifications: NotificationConfig
    instances: tuple[InstanceConfig, ...]
    source_path: Path

    def enabled_instances(self) -> tuple[InstanceConfig, ...]:
        return tuple(instance for instance in self.instances if instance.enabled)

    def instance_by_id(self, instance_id: str) -> InstanceConfig | None:
        for instance in self.instances:
            if instance.id == instance_id:
                return instance
        return None


def _require_table(value: Any, *, path: str, issues: _Issues) -> dict[str, Any]:
    if not isinstance(value, dict):
        issues.add(f"{path} must be a table")
        return {}
    return value


def _reject_unknown(table: dict[str, Any], allowed: set[str] | frozenset[str], path: str) -> None:
    unknown = sorted(set(table) - set(allowed))
    for key in unknown:
        raise ConfigurationError(f"{path}.{key} is not a supported key")


def _require_str(value: Any, *, path: str, issues: _Issues) -> str | None:
    if not isinstance(value, str) or not value:
        issues.add(f"{path} must be a non-empty string")
        return None
    return value


def _require_bool(value: Any, *, path: str, issues: _Issues, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        issues.add(f"{path} must be a boolean")
        return default
    return value


def _require_positive_number(
    value: Any, *, path: str, issues: _Issues, allow_none: bool = False
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.add(f"{path} must be a finite positive number")
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        issues.add(f"{path} must be a finite positive number")
        return None
    return number


def _require_positive_int(value: Any, *, path: str, issues: _Issues) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        issues.add(f"{path} must be a positive integer")
        return None
    return value


def _parse_matcher(raw: Any, *, app: str, path: str, issues: _Issues) -> MatcherSpec:
    table = _require_table(raw, path=path, issues=issues)
    if not table:
        issues.add(f"{path} must declare a strong identity: exe_realpath and/or entrypoint")
        return MatcherSpec()
    try:
        _reject_unknown(table, _MATCHER_KEYS, path)
    except ConfigurationError as error:
        issues.add(str(error))
        return MatcherSpec()

    exe_realpath: str | None = None
    raw_exe = table.get("exe_realpath")
    if raw_exe is not None:
        value = _require_str(raw_exe, path=f"{path}.exe_realpath", issues=issues)
        if value is not None:
            resolved = resolve_absolute_path(value, what=f"{path}.exe_realpath")
            exe_realpath = str(resolved)

    entrypoint: Entrypoint | None = None
    raw_entry = table.get("entrypoint")
    if raw_entry is not None:
        entry_table = _require_table(raw_entry, path=f"{path}.entrypoint", issues=issues)
        if entry_table:
            try:
                _reject_unknown(entry_table, _ENTRYPOINT_KEYS, f"{path}.entrypoint")
            except ConfigurationError as error:
                issues.add(str(error))
            kind = _require_str(
                entry_table.get("kind"), path=f"{path}.entrypoint.kind", issues=issues
            )
            value = _require_str(
                entry_table.get("value"), path=f"{path}.entrypoint.value", issues=issues
            )
            if kind is not None and kind not in ENTRYPOINT_KINDS:
                issues.add(f"{path}.entrypoint.kind must be one of {', '.join(ENTRYPOINT_KINDS)}")
            elif kind is not None and value is not None:
                if kind in ("script", "jar"):
                    resolved = resolve_absolute_path(value, what=f"{path}.entrypoint.value")
                    value = str(resolved)
                entrypoint = Entrypoint(kind=kind, value=value)

    if exe_realpath is None and entrypoint is None:
        issues.add(f"{path} must declare a strong identity: exe_realpath and/or entrypoint")
    if exe_realpath is not None and entrypoint is None:
        issues.add(
            f"{path} executable alone is not a supported strong identity; add entrypoint "
            "so unrelated processes with unreadable executable metadata can be excluded"
        )

    argv_tokens: list[str] = []
    raw_tokens = table.get("argv_tokens")
    if raw_tokens is not None:
        if not isinstance(raw_tokens, list):
            issues.add(f"{path}.argv_tokens must be an array of strings")
        else:
            for index, token in enumerate(raw_tokens):
                if not isinstance(token, str) or not token:
                    issues.add(f"{path}.argv_tokens[{index}] must be a non-empty string")
                    continue
                if len(token) > MAX_ARGV_TOKEN_LENGTH:
                    issues.add(
                        f"{path}.argv_tokens[{index}] exceeds the {MAX_ARGV_TOKEN_LENGTH} "
                        "character token cap"
                    )
                    continue
                argv_tokens.append(token)

    cmdline_regex: str | None = None
    raw_regex = table.get("cmdline_regex")
    if raw_regex is not None:
        pattern = _require_str(raw_regex, path=f"{path}.cmdline_regex", issues=issues)
        if pattern is not None:
            if len(pattern) > MAX_CMD_REGEX_LENGTH:
                issues.add(f"{path}.cmdline_regex exceeds the {MAX_CMD_REGEX_LENGTH} character cap")
            else:
                try:
                    re.compile(pattern)
                except re.error as error:
                    issues.add(f"{path}.cmdline_regex is not a valid pattern: {error}")
                else:
                    cmdline_regex = pattern

    env_eq: dict[str, str] = {}
    raw_env = table.get("env_eq")
    if raw_env is not None:
        env_table = _require_table(raw_env, path=f"{path}.env_eq", issues=issues)
        allowed_keys = ALLOWED_ENV_KEYS.get(app, frozenset())
        for key, value in env_table.items():
            if key not in allowed_keys:
                issues.add(f"{path}.env_eq.{key} is not allowed for app '{app}'")
                continue
            if not isinstance(value, str) or not value:
                issues.add(f"{path}.env_eq.{key} must be a non-empty string")
                continue
            env_eq[key] = value

    return MatcherSpec(
        exe_realpath=exe_realpath,
        entrypoint=entrypoint,
        argv_tokens=tuple(argv_tokens),
        cmdline_regex=cmdline_regex,
        env_eq=env_eq,
    )


def matcher_from_canonical(data: dict[str, Any]) -> MatcherSpec:
    """Rebuild a :class:`MatcherSpec` from a frozen request snapshot."""
    entry = data.get("entrypoint")
    entrypoint = Entrypoint(kind=entry["kind"], value=entry["value"]) if entry else None
    return MatcherSpec(
        exe_realpath=data.get("exe_realpath"),
        entrypoint=entrypoint,
        argv_tokens=tuple(data.get("argv_tokens") or ()),
        cmdline_regex=data.get("cmdline_regex"),
        env_eq=dict(data.get("env_eq") or {}),
    )


def _parse_job(raw: Any, *, path: str, issues: _Issues) -> JobSpec:
    table = _require_table(raw, path=path, issues=issues)
    if not table:
        issues.add(f"{path} is required")
        return JobSpec(config=Path("/"))
    try:
        _reject_unknown(table, _JOB_KEYS, path)
    except ConfigurationError as error:
        issues.add(str(error))

    config_value = _require_str(table.get("config"), path=f"{path}.config", issues=issues)
    config_path = (
        resolve_absolute_path(config_value, what=f"{path}.config")
        if config_value is not None
        else Path("/")
    )

    params: dict[str, str] = {}
    raw_params = table.get("params")
    if raw_params is not None:
        params_table = _require_table(raw_params, path=f"{path}.params", issues=issues)
        for key, value in params_table.items():
            if not isinstance(value, str):
                issues.add(f"{path}.params.{key} must be a string value")
                continue
            params[key] = value

    return JobSpec(config=config_path, params=params)


def _parse_instance(
    raw: Any,
    *,
    index: int,
    defaults: tuple[float, float, float | None],
    issues: _Issues,
) -> InstanceConfig | None:
    path = f"instance[{index}]"
    table = _require_table(raw, path=path, issues=issues)
    if not table:
        return None
    try:
        _reject_unknown(table, _INSTANCE_KEYS, path)
    except ConfigurationError as error:
        issues.add(str(error))

    instance_id = _require_str(table.get("id"), path=f"{path}.id", issues=issues)
    if instance_id is not None and not INSTANCE_ID_PATTERN.match(instance_id):
        issues.add(f"{path}.id must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}} and be a safe filename")
        instance_id = None

    app = _require_str(table.get("app"), path=f"{path}.app", issues=issues)
    if app is not None and app not in SUPPORTED_APPS:
        issues.add(f"{path}.app must be one of {', '.join(SUPPORTED_APPS)}")
        app = None

    data_dir_value = _require_str(table.get("data_dir"), path=f"{path}.data_dir", issues=issues)
    data_dir = (
        resolve_absolute_path(data_dir_value, what=f"{path}.data_dir")
        if data_dir_value is not None
        else Path("/")
    )

    enabled = _require_bool(
        table.get("enabled"), path=f"{path}.enabled", issues=issues, default=False
    )

    default_poll, default_confirm, default_deadline = defaults
    poll_seconds = (
        _require_positive_number(
            table.get("poll_seconds"), path=f"{path}.poll_seconds", issues=issues
        )
        if "poll_seconds" in table
        else default_poll
    )
    confirm_seconds = (
        _require_positive_number(
            table.get("confirm_seconds"), path=f"{path}.confirm_seconds", issues=issues
        )
        if "confirm_seconds" in table
        else default_confirm
    )
    if "deadline_seconds" in table:
        deadline_seconds = _require_positive_number(
            table.get("deadline_seconds"),
            path=f"{path}.deadline_seconds",
            issues=issues,
            allow_none=True,
        )
    else:
        deadline_seconds = default_deadline

    matcher = _parse_matcher(
        table.get("matcher"), app=app or "", path=f"{path}.matcher", issues=issues
    )
    job = _parse_job(table.get("job"), path=f"{path}.job", issues=issues)

    if instance_id is None or app is None:
        return None

    return InstanceConfig(
        id=instance_id,
        app=app,
        enabled=enabled,
        data_dir=data_dir,
        matcher=matcher,
        job=job,
        poll_seconds=poll_seconds if poll_seconds is not None else default_poll,
        confirm_seconds=confirm_seconds if confirm_seconds is not None else default_confirm,
        deadline_seconds=deadline_seconds,
    )


def load_config(path: str | os.PathLike[str]) -> TriggerConfig:
    """Load and validate the Trigger TOML configuration file."""
    source = Path(path)
    try:
        raw_bytes = source.read_bytes()
    except OSError as error:
        raise ConfigurationError(f"cannot read configuration {source}: {error}") from error
    try:
        document = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigurationError(f"cannot parse configuration {source}: {error}") from error

    issues = _Issues()
    try:
        _reject_unknown(document, {"trigger", "instance"}, "<root>")
    except ConfigurationError as error:
        issues.add(str(error))

    trigger = _require_table(document.get("trigger"), path="trigger", issues=issues)
    if trigger:
        try:
            _reject_unknown(trigger, _TRIGGER_KEYS, "trigger")
        except ConfigurationError as error:
            issues.add(str(error))

    poll_seconds = (
        _require_positive_number(
            trigger.get("poll_seconds"), path="trigger.poll_seconds", issues=issues
        )
        if "poll_seconds" in trigger
        else 2.0
    )
    confirm_seconds = (
        _require_positive_number(
            trigger.get("confirm_seconds"), path="trigger.confirm_seconds", issues=issues
        )
        if "confirm_seconds" in trigger
        else 5.0
    )
    if "deadline_seconds" in trigger:
        deadline_seconds = _require_positive_number(
            trigger.get("deadline_seconds"),
            path="trigger.deadline_seconds",
            issues=issues,
            allow_none=True,
        )
    else:
        deadline_seconds = None

    log_level = trigger.get("log_level", "INFO")
    if not isinstance(log_level, str) or log_level.upper() not in LOG_LEVELS:
        issues.add(f"trigger.log_level must be one of {', '.join(LOG_LEVELS)}")
        log_level = "INFO"
    else:
        log_level = log_level.upper()

    vaultkeep_value = _require_str(
        trigger.get("vaultkeep"), path="trigger.vaultkeep", issues=issues
    )
    vaultkeep = (
        resolve_absolute_path(vaultkeep_value, what="trigger.vaultkeep")
        if vaultkeep_value is not None
        else Path("/")
    )

    raw_notifications = trigger.get("notifications", {})
    notifications = NotificationConfig()
    notifications_table = _require_table(
        raw_notifications, path="trigger.notifications", issues=issues
    )
    if notifications_table:
        try:
            _reject_unknown(notifications_table, _NOTIFICATION_KEYS, "trigger.notifications")
        except ConfigurationError as error:
            issues.add(str(error))
        enabled = _require_bool(
            notifications_table.get("enabled"),
            path="trigger.notifications.enabled",
            issues=issues,
            default=False,
        )
        command = notifications_table.get("command", "notify-send")
        if not isinstance(command, str) or not command:
            issues.add("trigger.notifications.command must be a non-empty string")
            command = "notify-send"
        if "timeout_ms" in notifications_table:
            timeout_ms = _require_positive_int(
                notifications_table.get("timeout_ms"),
                path="trigger.notifications.timeout_ms",
                issues=issues,
            )
        else:
            timeout_ms = 5000
        notifications = NotificationConfig(
            enabled=enabled,
            command=command,
            timeout_ms=timeout_ms if timeout_ms is not None else 5000,
        )

    raw_instances = document.get("instance", [])
    instances: list[InstanceConfig] = []
    if not isinstance(raw_instances, list):
        issues.add("instance must be an array of tables")
    else:
        defaults = (
            poll_seconds if poll_seconds is not None else 2.0,
            confirm_seconds if confirm_seconds is not None else 5.0,
            deadline_seconds,
        )
        seen: set[str] = set()
        for index, raw_instance in enumerate(raw_instances):
            instance = _parse_instance(raw_instance, index=index, defaults=defaults, issues=issues)
            if instance is None:
                continue
            if instance.id in seen:
                issues.add(f"instance[{index}].id '{instance.id}' is duplicated")
                continue
            seen.add(instance.id)
            instances.append(instance)

    issues.raise_if_any()

    return TriggerConfig(
        poll_seconds=poll_seconds if poll_seconds is not None else 2.0,
        confirm_seconds=confirm_seconds if confirm_seconds is not None else 5.0,
        deadline_seconds=deadline_seconds,
        log_level=log_level,
        vaultkeep=vaultkeep,
        notifications=notifications,
        instances=tuple(instances),
        source_path=source,
    )


def validate_vaultkeep_executable(config: TriggerConfig) -> None:
    """Ensure the configured VaultKeep executable is an absolute, runnable file."""
    executable = config.vaultkeep
    if not executable.is_absolute():
        raise ConfigurationError("trigger.vaultkeep must be an absolute path")
    if not executable.exists():
        raise ConfigurationError(f"trigger.vaultkeep does not exist: {executable}")
    if not executable.is_file():
        raise ConfigurationError(f"trigger.vaultkeep is not a file: {executable}")
    if not os.access(executable, os.X_OK):
        raise ConfigurationError(f"trigger.vaultkeep is not executable: {executable}")
