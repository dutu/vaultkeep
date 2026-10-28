"""Immutable replacement classes describe configured coverage rather than contents."""

from __future__ import annotations

import json
import posixpath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from vaultkeep.application import ResolvedApplication
from vaultkeep.config.models import JobConfig, JobIdentityConfig, SourceConfig, SourceOptionsConfig
from vaultkeep.errors import SourceDiscoveryError


class ReplacementIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    identity_version: Literal[1]
    source_scope: str
    consistency_mode: Literal["files", "application_offline"]
    application_name: str | None = None
    application_user: str | None = None
    application_data_dir: str | None = None

    @field_validator("identity_version", mode="before")
    @classmethod
    def _version_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("identity_version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        try:
            scope = json.loads(self.source_scope)
            if set(scope) != {"job", "backup_mode", "sources", "exclude", "source_options"}:
                raise ValueError("invalid source coverage fields")
            JobIdentityConfig(id=scope["job"])
            if scope["backup_mode"] not in ("combined", "per_file"):
                raise ValueError("invalid backup mode")
            if not isinstance(scope["sources"], list) or not scope["sources"]:
                raise ValueError("source coverage requires sources")
            for source in scope["sources"]:
                SourceConfig.model_validate(source)
            SourceOptionsConfig.model_validate(scope["source_options"])
            if not isinstance(scope["exclude"], list) or any(
                not isinstance(value, str) or not value for value in scope["exclude"]
            ):
                raise ValueError("invalid exclusions")
            if json.dumps(scope, sort_keys=True, separators=(",", ":")) != self.source_scope:
                raise ValueError("source coverage must use canonical JSON")
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"Invalid replacement source scope: {error}") from error
        identity = (self.application_name, self.application_user, self.application_data_dir)
        if self.consistency_mode == "application_offline":
            if (
                not all(identity)
                or not self.application_data_dir
                or not self.application_data_dir.startswith("/")
            ):
                raise ValueError(
                    "offline identity requires application name, owner and absolute data directory"
                )
        elif any(value is not None for value in identity):
            raise ValueError("files identity cannot include an offline application")
        return self


def replacement_identity(
    config: JobConfig, *, application: ResolvedApplication | None = None
) -> ReplacementIdentity:
    """Store canonical source coverage so histories can be compared without YAML."""
    scope = {
        "job": config.job.id,
        "backup_mode": config.backup_mode,
        "sources": [
            source.model_dump() | {"path": posixpath.normpath(source.path)}
            for source in config.sources
        ],
        "exclude": config.exclude,
        "source_options": config.source_options.model_dump(),
    }
    app = application if config.consistency.mode == "application_offline" else None
    if config.consistency.mode == "application_offline" and app is None:
        raise SourceDiscoveryError(
            "Current-history identity requires a resolved application identity"
        )
    return ReplacementIdentity(
        identity_version=1,
        source_scope=json.dumps(scope, sort_keys=True, separators=(",", ":")),
        consistency_mode=config.consistency.mode,
        application_name=app.name if app else None,
        application_user=app.user if app else None,
        application_data_dir=posixpath.normpath(app.data_dir) if app else None,
    )
