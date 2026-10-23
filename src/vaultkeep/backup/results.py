"""Backup outcomes and public command error classification."""

from __future__ import annotations

from vaultkeep.diagnostics import concise
from vaultkeep.errors import (
    ConfigurationError,
    DestinationCommitDurabilityError,
    DestinationError,
    DestinationFinalizeError,
    HookError,
    LockError,
    StateError,
    TimerError,
)


class BackupInterrupted(BaseException):
    """A termination signal received while this process owns execution."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"Backup interrupted by signal {signum}")


def failure_exit_code(error: BaseException) -> int:
    if isinstance(error, BackupInterrupted):
        return 128 + error.signum
    if isinstance(error, KeyboardInterrupt):
        return 130
    for kind, code in (
        (ConfigurationError, 3),
        (StateError, 15),
        (DestinationError, 5),
        (HookError, 11),
        (LockError, 10),
        (TimerError, 14),
    ):
        if isinstance(error, kind):
            return code
    return 7


def failure_reason(error: BaseException) -> str:
    if isinstance(error, DestinationCommitDurabilityError):
        return "Backup committed; destination durability could not be confirmed"
    if isinstance(error, DestinationFinalizeError):
        return (
            "Permission denied during finalization"
            if "Permission denied" in str(error)
            else "Backup finalization failed"
        )
    return concise(str(error) or type(error).__name__)


def classify_command(exit_code: int | None, marker: str | None) -> str:
    """Interpret the CLI result for callers that launch a backup subprocess."""
    if exit_code == 0:
        return {"run: created": "SUCCESS", "run: unchanged": "UNCHANGED"}.get(
            (marker or "").strip(), "UNKNOWN"
        )
    if exit_code == 10:
        return "DEFERRED"
    if exit_code == 2:
        return "EXECUTION ERROR"
    return "FAILED"
