"""Strict local-state and reconstruction models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_BACKUP_ID_PATTERN = r"^[0-9a-f]{32}$"
_IDENTITY_PATTERN = r"^[0-9a-f]{16}$"
_UTC_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"

Digest = Annotated[str, StringConstraints(pattern=_DIGEST_PATTERN)]
BackupId = Annotated[str, StringConstraints(pattern=_BACKUP_ID_PATTERN)]
IdentityHash = Annotated[str, StringConstraints(pattern=_IDENTITY_PATTERN)]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]


def _validate_utc_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    if parsed.tzinfo != UTC:
        raise ValueError("Timestamp must use UTC")
    return value


UtcTimestamp = Annotated[
    str,
    StringConstraints(pattern=_UTC_PATTERN),
    AfterValidator(_validate_utc_timestamp),
]
RunResult = Literal["created", "unchanged", "failed"]
HookPhase = Literal[
    "before_check",
    "before_archive",
    "after_archive",
    "on_success",
    "on_failure",
    "on_unchanged",
]


class StateModel(BaseModel):
    """Common strict and immutable state behavior."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CredentialFingerprint(StateModel):
    """Local password-file generation identity without secret content."""

    fingerprint_version: Literal[1] = 1
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    ctime_ns: int = Field(ge=0)


class HookOutcomeState(StateModel):
    """Persisted non-secret result of one hook execution."""

    phase: HookPhase
    duration_seconds: float = Field(ge=0)
    exit_code: int | None
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


class LastRunState(StateModel):
    """Operational result of the most recent invocation."""

    at_utc: UtcTimestamp
    result: RunResult
    hooks: tuple[HookOutcomeState, ...] = ()


class LastSuccessfulBackup(StateModel):
    """Facts copied from the current referenced destination backup."""

    source_digest: Digest
    config_fingerprint: Digest
    backup_id: BackupId
    created_at_utc: UtcTimestamp
    backup_path: NonEmptyString
    application_version: NonEmptyString


class LocalState(StateModel):
    """Complete state.json schema version 1."""

    state_version: Literal[1] = 1
    job_id: NonEmptyString
    job_identity_hash: IdentityHash
    last_successful_backup: LastSuccessfulBackup | None = None
    credential_fingerprint: CredentialFingerprint | None = None
    last_unchanged_at_utc: UtcTimestamp | None = None
    last_run: LastRunState | None = None
    application_version: NonEmptyString

    @model_validator(mode="after")
    def validate_history_relationships(self) -> Self:
        """Reject operational history that cannot reference a successful backup."""
        if self.last_successful_backup is None:
            if self.last_unchanged_at_utc is not None:
                raise ValueError("last_unchanged_at_utc requires a successful backup")
            if self.last_run is not None and self.last_run.result in {"created", "unchanged"}:
                raise ValueError("created or unchanged result requires a successful backup")
        if (
            self.last_run is not None
            and self.last_run.result == "unchanged"
            and self.last_run.at_utc != self.last_unchanged_at_utc
        ):
            raise ValueError("unchanged run timestamp must match last_unchanged_at_utc")
        return self


@dataclass(frozen=True, slots=True)
class BackupStateRecord:
    """Validated destination facts required for state reconstruction."""

    job_id: str
    backup_id: str
    created_at_utc: str
    source_digest: str
    config_fingerprint: str
    backup_path: str
    application_version: str
    encrypted: bool


def utc_timestamp(value: datetime) -> str:
    """Serialize an aware datetime as canonical UTC with second precision."""
    if value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    utc = value.astimezone(UTC).replace(microsecond=0)
    return utc.isoformat().replace("+00:00", "Z")


def parse_utc_timestamp(value: str) -> datetime:
    """Parse a state timestamp after validating its canonical representation."""
    _validate_utc_timestamp(value)
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
