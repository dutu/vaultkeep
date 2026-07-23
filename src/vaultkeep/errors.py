"""Structured Vaultkeep error hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

IssuePathPart: TypeAlias = str | int


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


class SourceHashError(VaultkeepError):
    """Source content cannot be hashed."""


class SourceChangedError(SourceHashError):
    """A source entry changed after discovery or during hashing."""


class StateError(VaultkeepError):
    """Local state cannot be safely reconciled or persisted."""


class CredentialContinuityError(StateError):
    """Encrypted-backup credential continuity cannot be established."""
