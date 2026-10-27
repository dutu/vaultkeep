"""Structured Vaultkeep error hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

type IssuePathPart = str | int


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One independently detectable configuration problem."""

    path: tuple[IssuePathPart, ...]
    message: str
    code: str

    @property
    def dotted_path(self) -> str:
        """Return a human-readable configuration path."""
        result = ""
        for part in self.path:
            if isinstance(part, int):
                result += f"[{part}]"
            else:
                result += f".{part}" if result else part
        return result or "<root>"


class VaultkeepError(Exception):
    """Base class for structured Vaultkeep application errors."""

    config_path: Path | None = None


class ConfigurationError(VaultkeepError):
    """One or more parse, schema, or semantic configuration errors."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        if not issues:
            raise ValueError("ConfigurationError requires at least one issue")
        self.issues = issues
        super().__init__(self._render())

    def _render(self) -> str:
        count = len(self.issues)
        heading = (
            "Configuration contains 1 error:"
            if count == 1
            else f"Configuration contains {count} errors:"
        )
        details = [
            f"{index}. {issue.dotted_path}\n   {issue.message}"
            for index, issue in enumerate(self.issues, start=1)
        ]
        return f"{heading}\n\n" + "\n\n".join(details)


class SourceDiscoveryError(VaultkeepError):
    """Source traversal cannot produce a valid immutable snapshot."""


class ApplicationIdentityError(SourceDiscoveryError):
    """An application identity cannot be resolved from a configuration field."""

    def __init__(self, issue: ValidationIssue) -> None:
        self.issue = issue
        super().__init__(issue.message)


class SourceHashError(VaultkeepError):
    """Source content cannot be hashed."""


class SourceChangedError(SourceHashError):
    """A source entry changed after discovery or during hashing."""


class StateError(VaultkeepError):
    """Local state cannot be safely reconciled or persisted."""


class CredentialContinuityError(StateError):
    """Encrypted-backup credential continuity cannot be established."""


class ArchiveError(VaultkeepError):
    """Archive processing cannot complete safely."""


class ArchiveCreationError(ArchiveError):
    """An archive tool or archive write operation failed."""


class ArchiveVerificationError(ArchiveError):
    """A created archive does not satisfy the v1 integrity contract."""


class ArchiveCorruptionError(ArchiveVerificationError):
    """Demonstrated invalid archive data, distinct from operational verification errors."""


class ArchiveChecksumMismatchError(ArchiveCorruptionError):
    """Readable archive content differs from its recorded checksum."""


class PasswordFileError(ArchiveError):
    """A password file or passphrase violates the v1 security contract."""


class PlaintextCleanupError(ArchiveError):
    """A private plaintext archive could not be removed before commit."""


class DestinationError(VaultkeepError):
    """Destination processing cannot complete safely."""


class DestinationFinalizeError(DestinationError):
    """A staging directory cannot be atomically committed without overwrite."""


class DestinationCommitDurabilityError(DestinationFinalizeError):
    """The backup was committed but the destination-directory flush failed."""


class DestinationTemplateError(DestinationError):
    """A destination naming template cannot produce a safe backup name."""


class ManifestError(DestinationError):
    """A backup manifest cannot be created or validated safely."""


class UnsupportedManifestError(ManifestError):
    """A recognized history uses an unsupported immutable destination format."""


class DestinationDiscoveryError(DestinationError):
    """The destination root cannot be inspected safely."""


class RetentionError(DestinationError):
    """A complete retention plan cannot be calculated."""


class PruneError(DestinationError):
    """A destination backup cannot be safely removed."""

    def __init__(
        self,
        message: str,
        *,
        removed: tuple[str, ...] = (),
        corrupt_backup_ids: tuple[str, ...] = (),
    ) -> None:
        self.removed = removed
        self.corrupt_backup_ids = corrupt_backup_ids
        super().__init__(message)


class HookError(VaultkeepError):
    """A lifecycle hook cannot be validated or executed successfully."""


class TimerError(VaultkeepError):
    """Systemd timer management cannot complete safely."""


class LockError(VaultkeepError):
    """A second invocation attempted to run an already locked backup job."""
