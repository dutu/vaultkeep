"""Strict configuration models for schema version 2."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    field_validator,
    model_validator,
)

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
    archive_path_mode: Literal["prefix", "preserve"]
    archive_prefix: str | None = None
    exclude: list[NonEmptyString] = Field(default_factory=list)

    @model_validator(mode="after")
    def _archive_prefix_matches_mode(self) -> Self:
        if self.archive_path_mode == "prefix" and self.archive_prefix is None:
            raise ValueError("archive_prefix is required when archive_path_mode is prefix")
        if self.archive_path_mode == "preserve" and self.archive_prefix is not None:
            raise ValueError("archive_prefix is only valid when archive_path_mode is prefix")
        return self


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
    expected_fstype: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.-]+$")] | None = None


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
    interval: Literal["hourly", "daily", "weekly", "monthly"] | None = None
    window: NonEmptyString | None = None
    at: NonEmptyString | None = None
    day: MonthDay | NonEmptyString | None = None
    persistent: bool = True


TriggerSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class TriggerEntrypointConfig(StrictConfigModel):
    """Application entrypoint used as strong process identity."""

    kind: Literal["script", "jar", "java_main"]
    value: NonEmptyString

    @model_validator(mode="after")
    def _absolute_entrypoint(self) -> Self:
        if self.kind != "java_main" and not self.value.startswith(("/", "~/")):
            raise ValueError("script and jar entrypoints must be absolute paths or start with ~/")
        return self


class TriggerMatcherConfig(StrictConfigModel):
    """Strong application identity and optional supporting process predicates."""

    entrypoints: list[TriggerEntrypointConfig] = Field(min_length=1)
    exe_realpath: NonEmptyString | None = None
    argv_tokens: list[Annotated[str, StringConstraints(min_length=1, max_length=4096)]] = Field(
        default_factory=list
    )
    cmdline_regex: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    env_eq: dict[NonEmptyString, NonEmptyString] = Field(default_factory=dict)

    @field_validator("exe_realpath")
    @classmethod
    def _absolute_executable(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("/", "~/")):
            raise ValueError("exe_realpath must be an absolute path or start with ~/")
        return value

    @field_validator("cmdline_regex")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(f"invalid process regex: {error}") from error
        return value

    @model_validator(mode="after")
    def _valid_entrypoints(self) -> Self:
        seen: set[tuple[str, str]] = set()
        for entrypoint in self.entrypoints:
            identity = (entrypoint.kind, entrypoint.value)
            if identity in seen:
                raise ValueError("matcher entrypoints must be unique")
            seen.add(identity)
        return self


class NotificationCategoryConfig(StrictConfigModel):
    """Independently enable one component's notifications."""

    enabled: bool = False


class NotificationsConfig(StrictConfigModel):
    """Shared delivery settings and component-specific notification switches."""

    command: NonEmptyString = "notify-send"
    timeout_ms: Annotated[int, Field(gt=0)] = 5000
    backup: NotificationCategoryConfig = Field(default_factory=NotificationCategoryConfig)
    trigger: NotificationCategoryConfig = Field(default_factory=NotificationCategoryConfig)


class TriggerConfig(StrictConfigModel):
    """Optional application shutdown trigger for this job."""

    enabled: bool = False
    poll_seconds: TriggerSeconds = 2.0
    confirm_seconds: TriggerSeconds = 5.0
    deadline_seconds: TriggerSeconds | None = None
    params: dict[str, str] = Field(default_factory=dict)

    @field_validator("params")
    @classmethod
    def _valid_params(cls, value: dict[str, str]) -> dict[str, str]:
        from vaultkeep.config.runtime import parse_runtime_parameters
        from vaultkeep.errors import ConfigurationError

        try:
            return parse_runtime_parameters([f"{key}={item}" for key, item in value.items()])
        except ConfigurationError as error:
            raise ValueError(str(error)) from error


class ConsistencyConfig(StrictConfigModel):
    """Source-processing consistency policy shared by all initiators."""

    mode: Literal["files", "application_offline"] = "files"


class ApplicationConfig(StrictConfigModel):
    """One logical application and its process owner."""

    _owner_uid: int | None = PrivateAttr(default=None)

    name: Literal["electrum", "bisq", "wasabi"]
    user: NonEmptyString
    data_dir: NonEmptyString
    matcher: TriggerMatcherConfig

    @field_validator("data_dir")
    @classmethod
    def _absolute_data_dir(cls, value: str) -> str:
        if not (value == "~" or value.startswith(("/", "~/"))):
            raise ValueError("data_dir must be absolute or start with ~/")
        return value

    @model_validator(mode="after")
    def _allowed_environment(self) -> Self:
        allowed = {"electrum": {"ELECTRUMDIR"}, "wasabi": {"WASABI_DATADIR"}, "bisq": set()}
        unknown = set(self.matcher.env_eq) - allowed[self.name]
        if unknown:
            raise ValueError(
                "application.matcher.env_eq contains unsupported keys: "
                + ", ".join(sorted(unknown))
            )
        return self


class HookConfig(StrictConfigModel):
    """One shell lifecycle-hook command."""

    command: NonEmptyString
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
    """Complete Vaultkeep configuration schema version 2."""

    config_version: Literal[2]
    job: JobIdentityConfig
    backup_mode: Literal["combined", "per_file"] = "combined"
    sources: list[SourceConfig] = Field(min_length=1)
    exclude: list[NonEmptyString] = Field(default_factory=list)
    source_options: SourceOptionsConfig
    destination: DestinationConfig
    archive: ArchiveConfig
    encryption: EncryptionConfig
    retention: RetentionConfig
    schedule: ScheduleConfig
    consistency: ConsistencyConfig = Field(default_factory=ConsistencyConfig)
    application: ApplicationConfig | None = None
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    logging: LoggingConfig

    @field_validator("config_version", mode="before")
    @classmethod
    def _version_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("config_version must be an integer")
        return value

    @model_validator(mode="after")
    def _exclusive_automatic_execution(self) -> Self:
        if (
            self.trigger.enabled or self.consistency.mode == "application_offline"
        ) and self.application is None:
            raise ValueError(
                "application is required for Trigger or application_offline consistency"
            )
        if self.schedule.enabled and self.trigger.enabled:
            raise ValueError("schedule.enabled and trigger.enabled are mutually exclusive")
        return self

    @model_validator(mode="after")
    def _per_file_source(self) -> Self:
        if self.backup_mode == "per_file":
            if len(self.sources) != 1:
                raise ValueError("per_file backups require exactly one source directory")
            if self.source_options.follow_symlinks:
                raise ValueError("per_file backups require source_options.follow_symlinks: false")
        return self
