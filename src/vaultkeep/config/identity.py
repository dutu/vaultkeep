"""Strict job selection for inspecting and disabling existing resources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from vaultkeep.config.loader import _schema_issues, load_document
from vaultkeep.config.models import JobIdentityConfig
from vaultkeep.config.runtime import parse_runtime_parameters
from vaultkeep.errors import ConfigurationError, ValidationIssue
from vaultkeep.state.identity import canonical_config_path, job_identity_hash


@dataclass(frozen=True, slots=True)
class JobIdentity:
    source_path: Path
    job_id: str
    params: dict[str, str]

    @property
    def scope(self) -> str:
        return job_identity_hash(self.source_path, self.job_id, runtime_params=self.params)


def resolve_job_identity(
    path: Path,
    document: dict[str, Any],
    *,
    runtime_params: Mapping[str, str],
    trigger_defaults: bool = False,
) -> JobIdentity:
    """Validate only job.id and the parameter selection used by existing identities."""
    job = document.get("job")
    try:
        identity = JobIdentityConfig.model_validate(
            {"id": job["id"]} if isinstance(job, dict) and "id" in job else {}
        )
    except ValidationError as error:
        raise ConfigurationError(
            tuple(
                ValidationIssue(("job", *issue.path), issue.message, issue.code)
                for issue in _schema_issues(error)
            )
        ) from error
    params: dict[str, str] = {}
    if trigger_defaults:
        trigger = document.get("trigger", {})
        if not isinstance(trigger, dict):
            raise ConfigurationError(
                (ValidationIssue(("trigger",), "Cannot identify trigger parameters.", "identity"),)
            )
        try:
            params = TypeAdapter(dict[str, str]).validate_python(
                trigger.get("params", {}), strict=True
            )
        except ValidationError as error:
            raise ConfigurationError(
                tuple(
                    ValidationIssue(("trigger", "params", *issue.path), issue.message, issue.code)
                    for issue in _schema_issues(error)
                )
            ) from error
    params = parse_runtime_parameters([f"{key}={value}" for key, value in params.items()])
    params.update(
        parse_runtime_parameters([f"{key}={value}" for key, value in runtime_params.items()])
    )
    return JobIdentity(canonical_config_path(path), identity.id, params)


def load_job_identity(
    path: Path, *, runtime_params: Mapping[str, str], trigger_defaults: bool = False
) -> JobIdentity:
    return resolve_job_identity(
        path, load_document(path), runtime_params=runtime_params, trigger_defaults=trigger_defaults
    )
