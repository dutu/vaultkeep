"""Shared configuration preparation, independent of execution entry points."""

from collections.abc import Mapping
from pathlib import Path

from vaultkeep.application import ResolvedApplication, resolve_application, runtime_owner
from vaultkeep.config.models import JobConfig
from vaultkeep.config.runtime import resolve_runtime_config
from vaultkeep.errors import ConfigurationError
from vaultkeep.validation.semantic import validate_semantics, validate_template_semantics

__all__ = ["prepare_config", "prepare_job", "validate_semantics", "validate_template_semantics"]


def prepare_config(
    config: JobConfig,
    *,
    config_path: Path,
    runtime_params: Mapping[str, str] | None = None,
    allow_disabled_retention: bool = False,
) -> JobConfig:
    """Prepare configuration without requiring accounts, sources, or application runtime."""
    parameters = dict(runtime_params or {})
    config = resolve_runtime_config(config, parameters)
    try:
        validate_semantics(config, config_path=config_path, runtime_params=parameters)
    except ConfigurationError as error:
        if not allow_disabled_retention:
            raise
        issues = tuple(issue for issue in error.issues if issue.code != "retention_empty")
        if issues:
            raise ConfigurationError(issues) from error
    return config


def prepare_job(
    config: JobConfig,
    *,
    user_mode: bool,
    config_path: Path,
    runtime_params: Mapping[str, str] | None = None,
) -> tuple[JobConfig, ResolvedApplication | None]:
    """Validate scope and resolve application identity once, without runtime readiness checks."""
    runtime_owner(user_mode, config_path=config_path)
    application = (
        resolve_application(config.application, user_mode=user_mode, config_path=config_path)
        if config.application is not None
        else None
    )
    return prepare_config(
        config, config_path=config_path, runtime_params=runtime_params
    ), application
