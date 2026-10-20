"""Adapt a Vaultkeep YAML job to its shutdown monitor configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vaultkeep.config import load_config as load_job_config
from vaultkeep.errors import ConfigurationError as JobConfigurationError
from vaultkeep.state.identity import job_identity_hash
from vaultkeep.trigger.errors import ConfigurationError
from vaultkeep.trigger.paths import resolve_absolute_path

SUPPORTED_APPS = ("electrum", "bisq", "wasabi")
INSTANCE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


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
    scope: str | None = None
    job_id: str = ""
    params: dict[str, str] = field(default_factory=dict)

    def enabled_instances(self) -> tuple[InstanceConfig, ...]:
        return tuple(instance for instance in self.instances if instance.enabled)

    def instance_by_id(self, instance_id: str) -> InstanceConfig | None:
        for instance in self.instances:
            if instance.id == instance_id:
                return instance
        return None


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


def load_config(
    path: str | os.PathLike[str], *, runtime_params: Mapping[str, str] | None = None
) -> TriggerConfig:
    """Load one complete YAML job; its trigger selects this same job and parameters."""
    source = Path(path).resolve()
    try:
        job = load_job_config(source)
    except JobConfigurationError as error:
        raise ConfigurationError(str(error)) from error
    trigger = job.trigger
    params = dict(trigger.params)
    params.update(runtime_params or {})
    scope = job_identity_hash(source, job.job.id, runtime_params=params)
    instances: tuple[InstanceConfig, ...] = ()
    if trigger.app is not None and trigger.data_dir is not None and trigger.matcher is not None:
        raw = trigger.matcher
        entry = raw.entrypoint
        value = entry.value
        if entry.kind != "java_main":
            value = str(resolve_absolute_path(value, what="trigger.matcher.entrypoint.value"))
        matcher = MatcherSpec(
            exe_realpath=str(
                resolve_absolute_path(raw.exe_realpath, what="trigger.matcher.exe_realpath")
            )
            if raw.exe_realpath
            else None,
            entrypoint=Entrypoint(kind=entry.kind, value=value),
            argv_tokens=tuple(raw.argv_tokens),
            cmdline_regex=raw.cmdline_regex,
            env_eq=dict(raw.env_eq),
        )
        instances = (
            InstanceConfig(
                id=job.job.id,
                app=trigger.app,
                enabled=trigger.enabled,
                data_dir=resolve_absolute_path(trigger.data_dir, what="trigger.data_dir"),
                matcher=matcher,
                job=JobSpec(config=source, params=params),
                poll_seconds=trigger.poll_seconds,
                confirm_seconds=trigger.confirm_seconds,
                deadline_seconds=trigger.deadline_seconds,
            ),
        )
    # Invoke this installation's own CLI, not an independently configured core binary.
    executable = Path(sys.executable).parent / "vaultkeep"
    return TriggerConfig(
        poll_seconds=trigger.poll_seconds,
        confirm_seconds=trigger.confirm_seconds,
        deadline_seconds=trigger.deadline_seconds,
        log_level=job.logging.level.upper(),
        vaultkeep=executable,
        notifications=NotificationConfig(**trigger.notifications.model_dump()),
        instances=instances,
        source_path=source,
        scope=scope,
        job_id=job.job.id,
        params=params,
    )


def validate_vaultkeep_executable(config: TriggerConfig) -> None:
    if not config.vaultkeep.is_file() or not os.access(config.vaultkeep, os.X_OK):
        raise ConfigurationError(f"Vaultkeep executable is unavailable: {config.vaultkeep}")
