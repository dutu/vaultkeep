"""Trigger command exit codes and the error hierarchy.

Trigger exit codes (see the implementation plan) are distinct from VaultKeep's own
exit codes. Original VaultKeep codes are preserved separately in attempts/receipts
and are never substituted for a Trigger command exit.
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CONFIG = 2
EXIT_BLOCKED = 3
EXIT_BACKUP_FAILED = 4
EXIT_OUTCOME_UNKNOWN = 5
EXIT_INTEGRATION = 6


class TriggerError(Exception):
    """Base class for structured Trigger errors."""

    exit_code = EXIT_INTEGRATION


class UsageError(TriggerError):
    """Command-line usage or argument error."""

    exit_code = EXIT_USAGE


class ConfigurationError(TriggerError):
    """Trigger configuration or job-validation error."""

    exit_code = EXIT_CONFIG


class SelectionError(ConfigurationError):
    """An unknown instance was requested or selected."""


class StateError(TriggerError):
    """Persisted state is corrupt, incompatible, or unusable without repair."""

    exit_code = EXIT_CONFIG


class BlockedError(TriggerError):
    """Execution is blocked or unsafe, including missing runtime prerequisites."""

    exit_code = EXIT_BLOCKED


class BackupFailedError(TriggerError):
    """A VaultKeep execution failed."""

    exit_code = EXIT_BACKUP_FAILED


class OutcomeUnknownError(TriggerError):
    """Execution outcome cannot be established and must not be replayed."""

    exit_code = EXIT_OUTCOME_UNKNOWN


class IntegrationError(TriggerError):
    """A Trigger/VaultKeep integration defect (for example malformed argv)."""

    exit_code = EXIT_INTEGRATION
