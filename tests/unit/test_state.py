"""Tests for local state, reconstruction, and unchanged detection."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from vaultkeep.errors import CredentialContinuityError, StateError
from vaultkeep.state import (
    BackupStateRecord,
    ChangeReason,
    CredentialFingerprint,
    LastRunState,
    LocalState,
    ReconciliationStatus,
    calculate_credential_fingerprint,
    evaluate_unchanged,
    job_identity_hash,
    job_state_path,
    load_local_state,
    reconcile_local_state,
    state_after_created,
    state_after_failed,
    state_after_unchanged,
    write_local_state,
)
from vaultkeep.state import atomic as atomic_module
from vaultkeep.state.local_state import StateLoadStatus

SOURCE_DIGEST = "sha256:" + "a" * 64
CONFIG_FINGERPRINT = "sha256:" + "b" * 64
IDENTITY_HASH = "c" * 16
BACKUP_ID = "d" * 32
RUN_AT = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)


def _backup(
    *,
    backup_id: str = BACKUP_ID,
    created_at_utc: str = "2026-07-23T09:00:00Z",
    source_digest: str = SOURCE_DIGEST,
    config_fingerprint: str = CONFIG_FINGERPRINT,
    encrypted: bool = False,
) -> BackupStateRecord:
    return BackupStateRecord(
        job_id="app",
        backup_id=backup_id,
        created_at_utc=created_at_utc,
        source_digest=source_digest,
        config_fingerprint=config_fingerprint,
        backup_path=f"/mnt/backups/app/{backup_id}/archive.tar.zst",
        application_version="0.1.0.dev0",
        encrypted=encrypted,
    )


def _created_state(
    backup: BackupStateRecord | None = None,
    *,
    credential: CredentialFingerprint | None = None,
) -> LocalState:
    return state_after_created(
        job_id="app",
        identity_hash=IDENTITY_HASH,
        backup=backup or _backup(),
        run_at=RUN_AT,
        application_version="0.1.0.dev0",
        credential_fingerprint=credential,
    )


def _credential(seed: int = 1) -> CredentialFingerprint:
    return CredentialFingerprint(
        device=seed,
        inode=seed + 1,
        size=32,
        mtime_ns=seed + 2,
        ctime_ns=seed + 3,
    )


def test_job_identity_hash_matches_documented_encoding(tmp_path: Path) -> None:
    config_path = tmp_path / "app.yaml"
    expected = hashlib.sha256(os.fsencode(config_path.resolve()) + b"\0" + b"app").hexdigest()[:16]

    assert job_identity_hash(config_path, "app") == expected
    assert job_state_path(config_path, "app", state_root=tmp_path).name == "state.json"
    assert job_state_path(config_path, "app", state_root=tmp_path).parent.name == (
        f"app-{expected}"
    )


def test_atomic_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "job" / "state.json"
    expected = _created_state()

    write_local_state(path, expected)
    loaded = load_local_state(
        path,
        expected_job_id="app",
        expected_identity_hash=IDENTITY_HASH,
    )

    assert loaded.status is StateLoadStatus.LOADED
    assert loaded.state == expected
    assert path.read_bytes().endswith(b"\n")
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "{not-json",
        json.dumps({"state_version": 2}),
        json.dumps({"state_version": 1, "unknown": True}),
    ],
)
def test_empty_malformed_and_incompatible_state_is_unusable(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(payload, encoding="utf-8")

    result = load_local_state(
        path,
        expected_job_id="app",
        expected_identity_hash=IDENTITY_HASH,
    )

    assert result.status is StateLoadStatus.UNUSABLE
    assert result.state is None


def test_logically_inconsistent_state_is_unusable(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "state_version": 1,
                "job_id": "app",
                "job_identity_hash": IDENTITY_HASH,
                "last_successful_backup": None,
                "credential_fingerprint": None,
                "last_unchanged_at_utc": "2026-07-23T09:00:00Z",
                "last_run": None,
                "application_version": "0.1.0.dev0",
            }
        ),
        encoding="utf-8",
    )

    result = load_local_state(
        path,
        expected_job_id="app",
        expected_identity_hash=IDENTITY_HASH,
    )

    assert result.status is StateLoadStatus.UNUSABLE


def test_state_model_rejects_impossible_run_history() -> None:
    with pytest.raises(ValidationError, match="requires a successful backup"):
        LocalState(
            job_id="app",
            job_identity_hash=IDENTITY_HASH,
            last_run=LastRunState(
                at_utc="2026-07-23T09:00:00Z",
                result="created",
            ),
            application_version="0.1.0.dev0",
        )

    created = _created_state()
    with pytest.raises(ValidationError, match="must match"):
        LocalState(
            job_id=created.job_id,
            job_identity_hash=created.job_identity_hash,
            last_successful_backup=created.last_successful_backup,
            last_unchanged_at_utc="2026-07-23T10:00:00Z",
            last_run=LastRunState(
                at_utc="2026-07-23T11:00:00Z",
                result="unchanged",
            ),
            application_version="0.1.0.dev0",
        )


def test_missing_state_is_cache_miss(tmp_path: Path) -> None:
    result = load_local_state(
        tmp_path / "missing.json",
        expected_job_id="app",
        expected_identity_hash=IDENTITY_HASH,
    )

    assert result.status is StateLoadStatus.MISSING


def test_wrong_job_identity_is_unusable(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_local_state(path, _created_state())

    result = load_local_state(
        path,
        expected_job_id="app",
        expected_identity_hash="e" * 16,
    )

    assert result.status is StateLoadStatus.UNUSABLE


def test_atomic_failure_preserves_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    write_local_state(path, _created_state())
    original = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("forced replace failure")

    monkeypatch.setattr(atomic_module.os, "replace", fail_replace)

    with pytest.raises(StateError, match="atomically write"):
        write_local_state(
            path,
            state_after_failed(
                _created_state(),
                run_at=RUN_AT,
                application_version="0.1.0.dev0",
            ),
        )

    assert path.read_bytes() == original
    assert list(tmp_path.glob(".state-*.tmp")) == []


def test_missing_state_without_backups_initializes_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    result = reconcile_local_state(
        path,
        job_id="app",
        identity_hash=IDENTITY_HASH,
        application_version="0.1.0.dev0",
        destination_backups=(),
    )

    assert result.status is ReconciliationStatus.INITIALIZED_EMPTY
    assert result.state.last_successful_backup is None
    assert result.state_written is True
    assert path.exists()


def test_missing_state_reconstructs_newest_backup(tmp_path: Path) -> None:
    older = _backup(
        backup_id="1" * 32,
        created_at_utc="2026-07-23T08:00:00Z",
    )
    newer = _backup(
        backup_id="2" * 32,
        created_at_utc="2026-07-23T09:00:00Z",
    )

    result = reconcile_local_state(
        tmp_path / "state.json",
        job_id="app",
        identity_hash=IDENTITY_HASH,
        application_version="0.1.0.dev0",
        destination_backups=(newer, older),
    )

    assert result.status is ReconciliationStatus.RECONSTRUCTED
    assert result.selected_backup == newer
    assert result.state.last_successful_backup is not None
    assert result.state.last_successful_backup.backup_id == newer.backup_id
    assert result.state.last_unchanged_at_utc is None
    assert result.state.last_run is None


def test_corrupt_state_reconstructs_from_destination(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{corrupt", encoding="utf-8")

    result = reconcile_local_state(
        path,
        job_id="app",
        identity_hash=IDENTITY_HASH,
        application_version="0.1.0.dev0",
        destination_backups=(_backup(),),
    )

    assert result.status is ReconciliationStatus.RECONSTRUCTED
    assert result.state.last_successful_backup is not None
    assert (
        load_local_state(
            path,
            expected_job_id="app",
            expected_identity_hash=IDENTITY_HASH,
        ).status
        is StateLoadStatus.LOADED
    )


def test_equal_timestamps_use_backup_id_tie_breaker(tmp_path: Path) -> None:
    lower = _backup(backup_id="1" * 32)
    higher = _backup(backup_id="f" * 32)

    result = reconcile_local_state(
        tmp_path / "state.json",
        job_id="app",
        identity_hash=IDENTITY_HASH,
        application_version="0.1.0.dev0",
        destination_backups=(higher, lower),
    )

    assert result.selected_backup == higher


def test_stale_state_reconstructs_newest_destination_backup(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    older = _backup(
        backup_id="1" * 32,
        created_at_utc="2026-07-23T08:00:00Z",
    )
    newer = _backup(
        backup_id="2" * 32,
        created_at_utc="2026-07-23T09:00:00Z",
    )
    write_local_state(path, _created_state(older))

    result = reconcile_local_state(
        path,
        job_id="app",
        identity_hash=IDENTITY_HASH,
        application_version="0.1.0.dev0",
        destination_backups=(older, newer),
    )

    assert result.status is ReconciliationStatus.RECONSTRUCTED
    assert result.state.last_successful_backup is not None
    assert result.state.last_successful_backup.backup_id == newer.backup_id


def test_stale_state_without_destination_backup_is_reset(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_local_state(path, _created_state())

    result = reconcile_local_state(
        path,
        job_id="app",
        identity_hash=IDENTITY_HASH,
        application_version="0.1.0.dev0",
        destination_backups=(),
    )

    assert result.status is ReconciliationStatus.INITIALIZED_EMPTY
    assert result.state.last_successful_backup is None
    assert result.state_written is True


def test_valid_state_is_loaded_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = _created_state()
    write_local_state(path, state)

    result = reconcile_local_state(
        path,
        job_id="app",
        identity_hash=IDENTITY_HASH,
        application_version="0.1.0.dev0",
        destination_backups=(_backup(),),
    )

    assert result.status is ReconciliationStatus.LOADED
    assert result.state == state
    assert result.state_written is False


def test_encrypted_reconstruction_establishes_current_credential(
    tmp_path: Path,
) -> None:
    current = _credential()
    verified: list[str] = []
    backup = replace(_backup(encrypted=True), backup_path="/backup/archive.tar.7z")

    result = reconcile_local_state(
        tmp_path / "state.json",
        job_id="app",
        identity_hash=IDENTITY_HASH,
        application_version="0.1.0.dev0",
        destination_backups=(backup,),
        current_credential=current,
        verify_credential=lambda record: not verified.append(record.backup_id),
    )

    assert verified == [backup.backup_id]
    assert result.state.credential_fingerprint == current


def test_encrypted_credential_continuity_failure_blocks_reconstruction(
    tmp_path: Path,
) -> None:
    with pytest.raises(CredentialContinuityError):
        reconcile_local_state(
            tmp_path / "state.json",
            job_id="app",
            identity_hash=IDENTITY_HASH,
            application_version="0.1.0.dev0",
            destination_backups=(_backup(encrypted=True),),
            current_credential=_credential(),
            verify_credential=lambda record: False,
        )


def test_changed_credential_on_loaded_state_is_verified_and_recorded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    previous = _credential(1)
    current = _credential(10)
    backup = _backup(encrypted=True)
    write_local_state(path, _created_state(backup, credential=previous))
    verification_count = 0

    def verify(record: BackupStateRecord) -> bool:
        nonlocal verification_count
        assert record == backup
        verification_count += 1
        return True

    result = reconcile_local_state(
        path,
        job_id="app",
        identity_hash=IDENTITY_HASH,
        application_version="0.1.0.dev0",
        destination_backups=(backup,),
        current_credential=current,
        verify_credential=verify,
    )

    assert result.status is ReconciliationStatus.LOADED
    assert result.state_written is True
    assert result.state.credential_fingerprint == current
    assert verification_count == 1


def test_credential_fingerprint_uses_file_generation_metadata(tmp_path: Path) -> None:
    password = tmp_path / "password"
    password.write_text("first-secret", encoding="utf-8")
    before = calculate_credential_fingerprint(password)
    status = password.stat()

    os.utime(
        password,
        ns=(status.st_atime_ns, status.st_mtime_ns + 2_000_000_000),
    )
    after = calculate_credential_fingerprint(password)

    assert before != after
    assert before.size == len("first-secret")


def test_unchanged_requires_every_safety_condition() -> None:
    state = _created_state()
    backup = _backup()

    decision = evaluate_unchanged(
        state,
        source_digest=SOURCE_DIGEST,
        config_fingerprint=CONFIG_FINGERPRINT,
        current_credential=None,
        destination_backups=(backup,),
    )

    assert decision.unchanged is True
    assert decision.reason is ChangeReason.UNCHANGED


@pytest.mark.parametrize(
    ("state_factory", "source_digest", "config_fingerprint", "backups", "reason"),
    [
        (
            lambda: LocalState(
                job_id="app",
                job_identity_hash=IDENTITY_HASH,
                application_version="0.1.0.dev0",
            ),
            SOURCE_DIGEST,
            CONFIG_FINGERPRINT,
            (_backup(),),
            ChangeReason.NO_SUCCESSFUL_BACKUP,
        ),
        (
            _created_state,
            "sha256:" + "e" * 64,
            CONFIG_FINGERPRINT,
            (_backup(),),
            ChangeReason.SOURCE_CHANGED,
        ),
        (
            _created_state,
            SOURCE_DIGEST,
            "sha256:" + "e" * 64,
            (_backup(),),
            ChangeReason.CONFIG_CHANGED,
        ),
        (
            _created_state,
            SOURCE_DIGEST,
            CONFIG_FINGERPRINT,
            (),
            ChangeReason.BACKUP_MISSING,
        ),
    ],
)
def test_backup_is_required_when_unchanged_safety_check_fails(
    state_factory: object,
    source_digest: str,
    config_fingerprint: str,
    backups: tuple[BackupStateRecord, ...],
    reason: ChangeReason,
) -> None:
    assert callable(state_factory)
    state = state_factory()

    decision = evaluate_unchanged(
        state,
        source_digest=source_digest,
        config_fingerprint=config_fingerprint,
        current_credential=None,
        destination_backups=backups,
    )

    assert decision.unchanged is False
    assert decision.reason is reason


def test_credential_change_requires_backup() -> None:
    established = _credential(1)
    state = _created_state(credential=established)

    decision = evaluate_unchanged(
        state,
        source_digest=SOURCE_DIGEST,
        config_fingerprint=CONFIG_FINGERPRINT,
        current_credential=_credential(10),
        destination_backups=(_backup(),),
    )

    assert decision.reason is ChangeReason.CREDENTIAL_CHANGED


def test_created_unchanged_and_failed_state_transitions() -> None:
    created = _created_state()
    unchanged = state_after_unchanged(
        created,
        run_at=datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
        application_version="0.1.0.dev0",
    )
    failed = state_after_failed(
        unchanged,
        run_at=datetime(2026, 7, 25, 9, 0, tzinfo=UTC),
        application_version="0.1.0.dev0",
    )

    assert created.last_unchanged_at_utc is None
    assert unchanged.last_successful_backup == created.last_successful_backup
    assert unchanged.last_unchanged_at_utc == "2026-07-24T09:00:00Z"
    assert unchanged.last_run is not None
    assert unchanged.last_run.result == "unchanged"
    assert failed.last_successful_backup == unchanged.last_successful_backup
    assert failed.last_unchanged_at_utc == unchanged.last_unchanged_at_utc
    assert failed.last_run is not None
    assert failed.last_run.result == "failed"


def test_unchanged_without_successful_backup_is_rejected() -> None:
    empty = LocalState(
        job_id="app",
        job_identity_hash=IDENTITY_HASH,
        application_version="0.1.0.dev0",
    )

    with pytest.raises(StateError, match="without a successful backup"):
        state_after_unchanged(
            empty,
            run_at=RUN_AT,
            application_version="0.1.0.dev0",
        )


def test_created_state_rejects_backup_from_another_job() -> None:
    backup = replace(_backup(), job_id="other")

    with pytest.raises(StateError, match="job ID"):
        state_after_created(
            job_id="app",
            identity_hash=IDENTITY_HASH,
            backup=backup,
            run_at=RUN_AT,
            application_version="0.1.0.dev0",
        )
