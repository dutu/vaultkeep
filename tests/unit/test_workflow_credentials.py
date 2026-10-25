"""Credential continuity through the production backup workflow."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vaultkeep.archive import ArchiveArtifact, ArchiveBuildRequest
from vaultkeep.archive import passwords as passwords_module
from vaultkeep.archive import verification as verification_module
from vaultkeep.archive.checksums import calculate_archive_sha256, write_checksum_sidecar
from vaultkeep.archive.process import CommandResult
from vaultkeep.backup import workflow
from vaultkeep.destination.damage import DAMAGE_MARKER_NAME
from vaultkeep.errors import ArchiveCreationError, CredentialContinuityError
from vaultkeep.state import LocalState, calculate_credential_fingerprint, job_state_path


@pytest.fixture
def encrypted_job(
    tmp_path: Path, valid_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[Path]]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"source data")
    destination = tmp_path / "backups"
    destination.mkdir()
    password = tmp_path / "password"
    password.write_bytes(b"secret\n")
    password.chmod(0o600)
    valid_config["sources"][0]["path"] = str(source)
    valid_config["destination"]["root"] = str(destination)
    valid_config["archive"]["format"] = "tar.7z"
    valid_config["encryption"] = {"mode": "password", "password_file": str(password)}
    valid_config["retention"] = dict(hourly=1, daily=0, weekly=0, monthly=0, yearly=0)
    job = tmp_path / "app.yaml"
    job.write_text(json.dumps(valid_config))

    # Password ownership/path validation has separate tests; /tmp is writable
    # by other users. Keep real password loading and generation fingerprints.
    monkeypatch.setattr(
        passwords_module, "_validate_password_path", lambda path, **kwargs: path.lstat()
    )
    monkeypatch.setattr(passwords_module, "_disable_core_dumps", lambda: None)

    def build(request: ArchiveBuildRequest, **kwargs: Any) -> ArchiveArtifact:
        request.archive_path.write_bytes(b"encrypted archive")
        digest = calculate_archive_sha256(request.archive_path)
        write_checksum_sidecar(request.archive_path, request.checksum_path, digest)
        return ArchiveArtifact(
            archive_format=request.archive_format,
            archive_path=request.archive_path,
            checksum_path=request.checksum_path,
            sha256=digest,
            size=request.archive_path.stat().st_size,
            source_digest=request.expected_source_digest,
        )

    tested: list[Path] = []

    def test_password(arguments: Sequence[str | Path], *, terminal_input: bytes) -> CommandResult:
        assert arguments[:-1] == (Path("/usr/bin/7z"), "t", "-bd", "-sccUTF-8", "--")
        archive = Path(arguments[-1])
        assert archive.is_file()
        tested.append(archive)
        if terminal_input != b"secret\n":
            raise ArchiveCreationError("Wrong password")
        return CommandResult(0, b"", b"")

    monkeypatch.setattr(workflow, "build_archive", build)
    monkeypatch.setattr(verification_module, "run_command", test_password)
    return job, tested


def _run(job: Path) -> workflow.CommandResult:
    return workflow.run_backup(
        job,
        paths=workflow.WorkflowPaths(
            job.parent / "state", job.parent / "cache", job.parent / "locks"
        ),
        user_mode=True,
    )


def _state_path(job: Path) -> Path:
    return job_state_path(job, "app", state_root=job.parent / "state")


def _invalidate_state(path: Path, state_status: str) -> None:
    if state_status == "missing":
        path.unlink()
    elif state_status == "corrupt":
        path.write_text("invalid state")


@pytest.mark.parametrize("state_status", ["loaded", "missing", "corrupt"])
def test_corrupt_encrypted_backup_is_replaced_before_credential_recovery(
    encrypted_job: tuple[Path, list[Path]], state_status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, tested = encrypted_job
    identifiers = iter(("f" * 32, "1" * 32))
    monkeypatch.setattr(
        workflow, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(identifiers)))
    )
    created_at = workflow.datetime.now().astimezone()
    monkeypatch.setattr(workflow, "datetime", SimpleNamespace(now=lambda: created_at))
    first = _run(job)
    assert first.archive_path is not None
    first.archive_path.write_bytes(b"corrupted encrypted bytes")
    _invalidate_state(_state_path(job), state_status)

    result = _run(job)

    assert result.result == "created"
    assert result.archive_path != first.archive_path
    assert first.archive_path.read_bytes() == b"corrupted encrypted bytes"
    assert (first.archive_path.parent / DAMAGE_MARKER_NAME).is_file()
    assert tested == []  # A damaged archive cannot establish password continuity.
    assert _run(job).result == "unchanged"


@pytest.mark.parametrize("state_status", ["loaded", "missing", "corrupt"])
@pytest.mark.parametrize("source_changed", [False, True])
def test_same_password_establishes_continuity_before_unchanged_or_creation(
    encrypted_job: tuple[Path, list[Path]], state_status: str, source_changed: bool
) -> None:
    job, tested = encrypted_job
    first = _run(job)
    assert first.result == "created"
    assert tested == []
    state_path = _state_path(job)
    previous = LocalState.model_validate_json(state_path.read_bytes())
    password = job.parent / "password"
    # Atomically replacing the file guarantees a different fingerprint while
    # preserving the passphrase, without depending on clock resolution.
    replacement = job.parent / "replacement"
    replacement.write_bytes(b"secret\n")
    replacement.chmod(0o600)
    replacement.replace(password)
    current = calculate_credential_fingerprint(password)
    assert current != previous.credential_fingerprint
    _invalidate_state(state_path, state_status)
    if source_changed:
        (job.parent / "source" / "data").write_bytes(b"changed source")

    result = _run(job)

    assert result.result == ("created" if source_changed else "unchanged")
    assert tested == [first.archive_path]
    assert LocalState.model_validate_json(state_path.read_bytes()).credential_fingerprint == current


@pytest.mark.parametrize("state_status", ["loaded", "missing", "corrupt"])
@pytest.mark.parametrize("source_changed", [False, True])
def test_changed_password_blocks_run_without_adopting_fingerprint_or_pruning(
    encrypted_job: tuple[Path, list[Path]],
    monkeypatch: pytest.MonkeyPatch,
    state_status: str,
    source_changed: bool,
) -> None:
    job, tested = encrypted_job
    first = _run(job)
    assert first.result == "created"
    state_path = _state_path(job)
    _invalidate_state(state_path, state_status)
    original_state = state_path.read_bytes() if state_path.exists() else None
    (job.parent / "password").write_bytes(b"different secret\n")
    if source_changed:
        (job.parent / "source" / "data").write_bytes(b"changed source")
    artifacts = {
        path: path.read_bytes() for path in (job.parent / "backups").rglob("*") if path.is_file()
    }

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Continuity failure must block unchanged detection, creation, and retention")

    monkeypatch.setattr(workflow, "evaluate_unchanged", unexpected)
    monkeypatch.setattr(workflow, "build_archive", unexpected)
    monkeypatch.setattr(workflow, "execute_prune_plan", unexpected)

    with pytest.raises(CredentialContinuityError):
        _run(job)

    assert tested == [first.archive_path]
    assert (state_path.read_bytes() if state_path.exists() else None) == original_state
    assert {
        path: path.read_bytes() for path in (job.parent / "backups").rglob("*") if path.is_file()
    } == artifacts


def test_established_password_fingerprint_skips_reverification(
    encrypted_job: tuple[Path, list[Path]],
) -> None:
    job, tested = encrypted_job
    assert _run(job).result == "created"
    assert _run(job).result == "unchanged"
    assert tested == []
