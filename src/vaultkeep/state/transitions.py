"""Pure local-state transitions for workflow results."""

from __future__ import annotations

from datetime import datetime

from vaultkeep.errors import StateError
from vaultkeep.state.models import (
    BackupStateRecord,
    CredentialFingerprint,
    HookOutcomeState,
    LastRunState,
    LastSuccessfulBackup,
    LocalState,
    utc_timestamp,
)


def state_after_created(
    *,
    job_id: str,
    identity_hash: str,
    backup: BackupStateRecord,
    run_at: datetime,
    application_version: str,
    credential_fingerprint: CredentialFingerprint | None = None,
    hooks: tuple[HookOutcomeState, ...] = (),
) -> LocalState:
    """Create state for a newly committed backup."""
    if backup.job_id != job_id:
        raise StateError("Backup job ID does not match local state job ID")
    successful = LastSuccessfulBackup(
        source_digest=backup.source_digest,
        config_fingerprint=backup.config_fingerprint,
        backup_id=backup.backup_id,
        created_at_utc=backup.created_at_utc,
        backup_path=backup.backup_path,
        application_version=backup.application_version,
    )
    return LocalState(
        job_id=job_id,
        job_identity_hash=identity_hash,
        last_successful_backup=successful,
        credential_fingerprint=credential_fingerprint,
        last_unchanged_at_utc=None,
        last_run=LastRunState(
            at_utc=utc_timestamp(run_at),
            result="created",
            hooks=hooks,
        ),
        application_version=application_version,
    )


def state_after_unchanged(
    state: LocalState,
    *,
    run_at: datetime,
    application_version: str,
    credential_fingerprint: CredentialFingerprint | None = None,
    hooks: tuple[HookOutcomeState, ...] = (),
) -> LocalState:
    """Record an unchanged result without modifying successful-backup facts."""
    if state.last_successful_backup is None:
        raise StateError("Cannot record unchanged without a successful backup")
    timestamp = utc_timestamp(run_at)
    return LocalState(
        job_id=state.job_id,
        job_identity_hash=state.job_identity_hash,
        last_successful_backup=state.last_successful_backup,
        credential_fingerprint=credential_fingerprint,
        last_unchanged_at_utc=timestamp,
        last_run=LastRunState(at_utc=timestamp, result="unchanged", hooks=hooks),
        application_version=application_version,
    )


def state_after_failed(
    state: LocalState,
    *,
    run_at: datetime,
    application_version: str,
    hooks: tuple[HookOutcomeState, ...] = (),
) -> LocalState:
    """Record a failed run while preserving the previous successful state."""
    return LocalState(
        job_id=state.job_id,
        job_identity_hash=state.job_identity_hash,
        last_successful_backup=state.last_successful_backup,
        credential_fingerprint=state.credential_fingerprint,
        last_unchanged_at_utc=state.last_unchanged_at_utc,
        last_run=LastRunState(
            at_utc=utc_timestamp(run_at),
            result="failed",
            hooks=hooks,
        ),
        application_version=application_version,
    )
