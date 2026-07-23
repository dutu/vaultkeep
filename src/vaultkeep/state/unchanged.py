"""Safe unchanged-decision evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from vaultkeep.state.models import BackupStateRecord, CredentialFingerprint, LocalState


class ChangeReason(StrEnum):
    """Reason for creating or skipping a backup."""

    UNCHANGED = "unchanged"
    NO_SUCCESSFUL_BACKUP = "no_successful_backup"
    SOURCE_CHANGED = "source_changed"
    CONFIG_CHANGED = "config_changed"
    CREDENTIAL_CHANGED = "credential_changed"
    BACKUP_MISSING = "backup_missing"


@dataclass(frozen=True, slots=True)
class ChangeDecision:
    """Unchanged decision and its stable explanation."""

    unchanged: bool
    reason: ChangeReason


def evaluate_unchanged(
    state: LocalState,
    *,
    source_digest: str,
    config_fingerprint: str,
    current_credential: CredentialFingerprint | None,
    destination_backups: tuple[BackupStateRecord, ...],
) -> ChangeDecision:
    """Return unchanged only when state, configuration, credentials, and destination agree."""
    successful = state.last_successful_backup
    if successful is None:
        return ChangeDecision(False, ChangeReason.NO_SUCCESSFUL_BACKUP)
    if successful.source_digest != source_digest:
        return ChangeDecision(False, ChangeReason.SOURCE_CHANGED)
    if successful.config_fingerprint != config_fingerprint:
        return ChangeDecision(False, ChangeReason.CONFIG_CHANGED)
    if state.credential_fingerprint != current_credential:
        return ChangeDecision(False, ChangeReason.CREDENTIAL_CHANGED)
    if not any(
        backup.job_id == state.job_id
        and backup.backup_id == successful.backup_id
        and backup.created_at_utc == successful.created_at_utc
        and backup.source_digest == successful.source_digest
        and backup.config_fingerprint == successful.config_fingerprint
        and backup.backup_path == successful.backup_path
        for backup in destination_backups
    ):
        return ChangeDecision(False, ChangeReason.BACKUP_MISSING)
    return ChangeDecision(True, ChangeReason.UNCHANGED)
