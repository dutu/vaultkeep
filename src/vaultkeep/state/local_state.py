"""Local-state loading, cache recovery, and credential continuity."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from vaultkeep.errors import CredentialContinuityError
from vaultkeep.state.atomic import write_local_state
from vaultkeep.state.models import (
    BackupStateRecord,
    CredentialFingerprint,
    LastSuccessfulBackup,
    LocalState,
    parse_utc_timestamp,
)

CredentialVerifier = Callable[[BackupStateRecord], bool]


class StateLoadStatus(StrEnum):
    """Result of reading the local cache file."""

    LOADED = "loaded"
    MISSING = "missing"
    UNUSABLE = "unusable"


class ReconciliationStatus(StrEnum):
    """User-visible local-state source."""

    LOADED = "loaded"
    RECONSTRUCTED = "reconstructed"
    INITIALIZED_EMPTY = "initialized_empty"


@dataclass(frozen=True, slots=True)
class StateLoadResult:
    """State file read result without treating cache misses as failures."""

    status: StateLoadStatus
    state: LocalState | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class StateReconciliation:
    """Reconciled state and how it was obtained."""

    status: ReconciliationStatus
    state: LocalState
    selected_backup: BackupStateRecord | None
    state_written: bool


def calculate_credential_fingerprint(password_file: Path) -> CredentialFingerprint:
    """Fingerprint password-file generation metadata without reading its content."""
    status = password_file.stat()
    return CredentialFingerprint(
        device=status.st_dev,
        inode=status.st_ino,
        size=status.st_size,
        mtime_ns=status.st_mtime_ns,
        ctime_ns=status.st_ctime_ns,
    )


def load_local_state(
    path: Path,
    *,
    expected_job_id: str,
    expected_identity_hash: str,
) -> StateLoadResult:
    """Load strict state or classify it as a recoverable cache miss."""
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return StateLoadResult(StateLoadStatus.MISSING, None)
    except (OSError, UnicodeError) as error:
        return StateLoadResult(StateLoadStatus.UNUSABLE, None, str(error))

    if not payload.strip():
        return StateLoadResult(StateLoadStatus.UNUSABLE, None, "state file is empty")
    try:
        state = LocalState.model_validate_json(payload, strict=True)
    except (ValidationError, ValueError) as error:
        return StateLoadResult(StateLoadStatus.UNUSABLE, None, str(error))
    if state.job_id != expected_job_id:
        return StateLoadResult(StateLoadStatus.UNUSABLE, None, "state job ID does not match")
    if state.job_identity_hash != expected_identity_hash:
        return StateLoadResult(
            StateLoadStatus.UNUSABLE,
            None,
            "state job identity does not match",
        )
    return StateLoadResult(StateLoadStatus.LOADED, state)


def reconcile_local_state(
    path: Path,
    *,
    job_id: str,
    identity_hash: str,
    application_version: str,
    destination_backups: Iterable[BackupStateRecord],
    current_credential: CredentialFingerprint | None = None,
    verify_credential: CredentialVerifier | None = None,
) -> StateReconciliation:
    """Load or reconstruct state from the newest valid destination backup."""
    load_result = load_local_state(
        path,
        expected_job_id=job_id,
        expected_identity_hash=identity_hash,
    )
    newest = _newest_backup(destination_backups, job_id)
    state = load_result.state
    write_required = False

    if state is not None and _state_matches_backup(state, newest):
        status = ReconciliationStatus.LOADED
    elif newest is not None:
        state = _state_from_backup(
            newest,
            identity_hash=identity_hash,
        )
        status = ReconciliationStatus.RECONSTRUCTED
        write_required = True
    elif state is not None and state.last_successful_backup is None:
        status = ReconciliationStatus.LOADED
    else:
        state = LocalState(
            job_id=job_id,
            job_identity_hash=identity_hash,
            application_version=application_version,
        )
        status = ReconciliationStatus.INITIALIZED_EMPTY
        write_required = True

    if state.credential_fingerprint != current_credential:
        if (
            current_credential is not None
            and newest is not None
            and newest.encrypted
            and (verify_credential is None or not verify_credential(newest))
        ):
            raise CredentialContinuityError(
                "Current password cannot open the newest encrypted backup."
            )
        state = state.model_copy(
            update={
                "credential_fingerprint": current_credential,
                "application_version": application_version,
            }
        )
        write_required = True

    if write_required:
        write_local_state(path, state)
    return StateReconciliation(status, state, newest, write_required)


def _newest_backup(
    records: Iterable[BackupStateRecord],
    job_id: str,
) -> BackupStateRecord | None:
    matching = tuple(record for record in records if record.job_id == job_id)
    if not matching:
        return None
    return max(
        matching,
        key=lambda record: (parse_utc_timestamp(record.created_at_utc), record.backup_id),
    )


def _state_matches_backup(
    state: LocalState,
    backup: BackupStateRecord | None,
) -> bool:
    successful = state.last_successful_backup
    if successful is None or backup is None:
        return successful is None and backup is None
    return (
        successful.backup_id == backup.backup_id
        and successful.created_at_utc == backup.created_at_utc
        and successful.source_digest == backup.source_digest
        and successful.config_fingerprint == backup.config_fingerprint
        and successful.backup_path == backup.backup_path
        and successful.application_version == backup.application_version
    )


def _state_from_backup(
    backup: BackupStateRecord,
    *,
    identity_hash: str,
) -> LocalState:
    successful = LastSuccessfulBackup(
        source_digest=backup.source_digest,
        config_fingerprint=backup.config_fingerprint,
        backup_id=backup.backup_id,
        created_at_utc=backup.created_at_utc,
        backup_path=backup.backup_path,
        application_version=backup.application_version,
    )
    return LocalState(
        job_id=backup.job_id,
        job_identity_hash=identity_hash,
        last_successful_backup=successful,
        last_unchanged_at_utc=None,
        last_run=None,
        application_version=backup.application_version,
    )
