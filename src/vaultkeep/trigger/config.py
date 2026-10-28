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

from vaultkeep.application import (
    MatcherSpec,
    ResolvedApplication,
    execution_policy,
    matcher_from_canonical,
)
from vaultkeep.config import JobConfig
from vaultkeep.config import load_config as load_job_config
from vaultkeep.errors import ApplicationIdentityError
from vaultkeep.errors import ConfigurationError as JobConfigurationError
from vaultkeep.notifications.notifier import NotificationConfig
from vaultkeep.state.identity import job_identity_hash
from vaultkeep.trigger.errors import ConfigurationError
from vaultkeep.validation import prepare_job

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
    owner_uid: int
    execution_policy: dict[str, object]

    def fingerprint(self) -> str:
        """Return a stable fingerprint of the instance's execution-relevant settings."""
        payload = {
            "execution_policy": self.execution_policy,
            "owner_uid": self.owner_uid,
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
    try:
        job, application = prepare_job(
            job, user_mode=True, config_path=source, runtime_params=params
        )
    except (ApplicationIdentityError, JobConfigurationError) as error:
        raise ConfigurationError(str(error)) from error
    return from_prepared_job(job, application, source=source, params=params)


def from_prepared_job(
    job: JobConfig,
    application: ResolvedApplication | None,
    *,
    source: Path,
    params: Mapping[str, str],
) -> TriggerConfig:
    """Adapt prepared facts without reloading configuration or resolving identity."""
    trigger = job.trigger
    params = dict(params)
    scope = job_identity_hash(source, job.job.id, runtime_params=params)
    instances: tuple[InstanceConfig, ...] = ()
    if application is not None:
        instances = (
            InstanceConfig(
                id=job.job.id,
                app=application.name,
                enabled=trigger.enabled,
                data_dir=Path(application.data_dir),
                matcher=matcher_from_canonical(application.matcher.model_dump()),
                job=JobSpec(config=source, params=params),
                poll_seconds=trigger.poll_seconds,
                confirm_seconds=trigger.confirm_seconds,
                deadline_seconds=trigger.deadline_seconds,
                owner_uid=application.owner_uid,
                execution_policy=execution_policy(job, application),
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
        notifications=NotificationConfig(
            enabled=job.notifications.trigger.enabled,
            command=job.notifications.command,
            timeout_ms=job.notifications.timeout_ms,
        ),
        instances=instances,
        source_path=source,
        scope=scope,
        job_id=job.job.id,
        params=params,
    )


def validate_vaultkeep_executable(config: TriggerConfig) -> None:
    if not config.vaultkeep.is_file() or not os.access(config.vaultkeep, os.X_OK):
        raise ConfigurationError(f"Vaultkeep executable is unavailable: {config.vaultkeep}")
