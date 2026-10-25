"""Best-effort, structured events for read-only backup verification."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class VerificationProgress:
    phase: Literal["discovery", "backup", "archive"]
    operation: str
    status: Literal["running", "ok", "failed", "complete"] = "running"
    path: Path | None = None
    index: int | None = None
    total: int | None = None
    completed: int | None = None
    bytes_processed: int | None = None
    bytes_total: int | None = None
    error: str | None = None


def report_progress(
    callback: Callable[[VerificationProgress], None] | None, event: VerificationProgress
) -> None:
    """Optional presentation must never change verification results or errors."""
    if callback is not None:
        with suppress(Exception):
            callback(event)
