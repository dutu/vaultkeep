"""Bounded execution evidence, independent of archive and dispatch records."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vaultkeep.diagnostics import parse_utc
from vaultkeep.errors import StateError


class Attempt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    started_at: str
    pid: int = Field(gt=0)
    create_time: float | None = Field(default=None, gt=0)
    result: Literal["SUCCESS", "UNCHANGED", "FAILED", "INTERRUPTED", "UNKNOWN"] | None = None
    completed_at: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    exit_code: int | None = None
    signal: int | None = Field(default=None, ge=1)
    cleanup_errors: list[str] = Field(default_factory=list)
    reason: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def timestamps(self) -> Self:
        started, ended = parse_utc(self.started_at), parse_utc(self.completed_at)
        if started is None or (
            self.completed_at is not None and (ended is None or ended < started)
        ):
            raise ValueError("invalid execution timestamps")
        if self.result is None and self.completed_at is not None:
            raise ValueError("unfinished execution has a completion time")
        if self.result not in (None, "UNKNOWN") and ended is None:
            raise ValueError("completion evidence requires a completion time")
        if self.result in ("SUCCESS", "UNCHANGED") and (
            self.exit_code != 0 or self.signal is not None
        ):
            raise ValueError("contradictory successful completion")
        if self.result in ("FAILED", "INTERRUPTED") and self.exit_code in (None, 0):
            raise ValueError("failure requires a nonzero exit code")
        if self.result not in (None, "UNKNOWN") and self.duration_seconds is None:
            raise ValueError("completion evidence requires a duration")
        return self


class ExecutionHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    job_id: str
    identity_hash: str
    current: Attempt | None
    previous: Attempt | None

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid execution schema version")
        return value

    @model_validator(mode="after")
    def lifecycle(self) -> Self:
        if self.current and self.current.result is not None:
            raise ValueError("active execution cannot contain a result")
        if self.previous and self.previous.result is None:
            raise ValueError("previous execution requires an outcome")
        if (
            self.current
            and self.previous
            and (
                self.current.execution_id == self.previous.execution_id
                or self.current.started_at < self.previous.started_at
            )
        ):
            raise ValueError("contradictory execution ordering")
        return self


def read_history(path: Path, job_id: str, identity: str) -> ExecutionHistory | None:
    try:
        with path.open("rb") as stream:
            payload = stream.read(16385)
        if len(payload) > 16384:
            raise ValueError("execution record exceeds size limit")
        history = ExecutionHistory.model_validate_json(payload)
        if history.job_id != job_id or history.identity_hash != identity:
            raise ValueError("execution record belongs to another job")
        return history
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise StateError(f"Execution evidence unavailable: {error}") from error
