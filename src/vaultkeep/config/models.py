"""Strict configuration models for schema version 1."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
JobId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]
NonNegativeInteger = Annotated[int, Field(ge=0)]
HookTimeout = Annotated[int, Field(ge=1, le=3600)]
CompressionLevel = Annotated[int, Field(ge=1, le=19)]
MonthDay = Annotated[int, Field(ge=1, le=28)]


class StrictConfigModel(BaseModel):
    """Common strict and immutable model behavior."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class JobIdentityConfig(StrictConfigModel):
    """Job identity."""

    id: JobId


class SourceConfig(StrictConfigModel):
    """One configured source."""

    path: NonEmptyString
    exclude: list[NonEmptyString] = Field(default_factory=list)


class SourceOptionsConfig(StrictConfigModel):
    """Source traversal behavior."""

    follow_symlinks: bool = False
    cross_filesystems: bool = False
    ignore_missing: bool = False


class DestinationConfig(StrictConfigModel):
    """Backup destination settings."""

    root: NonEmptyString
    name_template: NonEmptyString
    marker_file: NonEmptyString | None = None
    require_mount: bool


class ArchiveConfig(StrictConfigModel):
    """Archive format settings."""

    format: Literal["tar.zst", "tar.7z"]
    compression_level: CompressionLevel = 6


class EncryptionConfig(StrictConfigModel):
    """Archive encryption settings."""

    mode: Literal["none", "password"]
    password_file: NonEmptyString | None = None


class RetentionConfig(StrictConfigModel):
    """Calendar-bucket retention counts."""

    hourly: NonNegativeInteger
    daily: NonNegativeInteger
    weekly: NonNegativeInteger
    monthly: NonNegativeInteger
    yearly: NonNegativeInteger


class ScheduleConfig(StrictConfigModel):
    """Systemd schedule settings."""

    enabled: bool
    interval: Literal["hourly", "daily", "weekly", "monthly"]
    window: NonEmptyString | None = None
    at: NonEmptyString | None = None
    day: MonthDay | NonEmptyString | None = None
    persistent: bool = True


class HookConfig(StrictConfigModel):
    """One direct lifecycle-hook command."""

    command: list[NonEmptyString] = Field(min_length=1)
    timeout_seconds: HookTimeout = 300


class HooksConfig(StrictConfigModel):
    """Lifecycle-hook phase configuration."""

    before_check: HookConfig | None = None
    before_archive: HookConfig | None = None
    after_archive: HookConfig | None = None
    on_success: HookConfig | None = None
    on_failure: HookConfig | None = None
    on_unchanged: HookConfig | None = None


class LoggingConfig(StrictConfigModel):
    """Operational logging settings."""

    level: Literal["error", "warning", "info", "debug"]
    include_command_output: bool = False


class JobConfig(StrictConfigModel):
    """Complete Vaultkeep configuration schema version 1."""

    config_version: Literal[1]
    job: JobIdentityConfig
    sources: list[SourceConfig] = Field(min_length=1)
    exclude: list[NonEmptyString] = Field(default_factory=list)
    source_options: SourceOptionsConfig
    destination: DestinationConfig
    archive: ArchiveConfig
    encryption: EncryptionConfig
    retention: RetentionConfig
    schedule: ScheduleConfig
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    logging: LoggingConfig
