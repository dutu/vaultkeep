"""Map VaultKeep exit codes and stdout markers to attempt outcomes."""

from __future__ import annotations

from vaultkeep_trigger.models import AttemptOutcome

# VaultKeep dispatcher exit codes (see the implementation plan) — authoritative.
VK_SUCCESS = 0
VK_INVALID_ARGUMENT = 2
VK_CONFIGURATION = 3
VK_DESTINATION = 5
VK_GENERIC = 7
VK_LOCK = 10
VK_HOOK = 11
VK_TIMER = 14
VK_STATE = 15

#: Exit codes that VaultKeep's *status* table uses but the reviewed dispatcher never emits.
UNEMITTED_STATUS_CODES = frozenset({4, 8, 9})


def parse_marker_line(stdout_tail: str | None) -> str | None:
    """Return the recognized trailing ``run:`` marker, if any."""
    if stdout_tail is None:
        return None
    line = stdout_tail.strip()
    if line in ("run: created", "run: unchanged"):
        return line
    return None


def classify_exit(exit_code: int | None, stdout_tail: str | None) -> AttemptOutcome:
    """Classify a completed VaultKeep child into an attempt outcome."""
    marker = parse_marker_line(stdout_tail)
    if exit_code == VK_SUCCESS:
        if marker == "run: created":
            return AttemptOutcome.CREATED
        if marker == "run: unchanged":
            return AttemptOutcome.UNCHANGED
        return AttemptOutcome.RESULT_UNKNOWN
    if exit_code == VK_LOCK:
        return AttemptOutcome.DEFERRED
    if exit_code == VK_INVALID_ARGUMENT:
        return AttemptOutcome.INTEGRATION_ERROR
    return AttemptOutcome.BACKUP_FAILED
